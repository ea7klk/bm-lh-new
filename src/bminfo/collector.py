from __future__ import annotations

import logging
import signal
import threading

import socketio

from .config import settings
from .models import parse_event
from .sessionizer import is_below_kerchunk_threshold, make_qso
from .storage import PostgresStore
from .talkgroups import sync_talkgroups


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
STORED_QSO_LOG_BLOCK = 100


def _log_stored_qso_progress(stored_qso_count: int) -> None:
    """Log successful QSO storage in coarse progress blocks."""
    if stored_qso_count > 0 and stored_qso_count % STORED_QSO_LOG_BLOCK == 0:
        logger.info("stored %d QSOs (progress block of %d)", stored_qso_count, STORED_QSO_LOG_BLOCK)


def _talkgroup_sync_loop(store: PostgresStore, stop_event: threading.Event) -> None:
    # The sync function also checks the persisted last-update marker. Keeping
    # this loop at or above 24 hours avoids needless wakeups and API calls.
    interval_seconds = max(settings.talkgroups_sync_hours, 24) * 60 * 60
    while not stop_event.is_set():
        try:
            sync_talkgroups(store, settings.talkgroups_url)
        except Exception:
            logger.exception("talkgroup metadata synchronization failed")
        stop_event.wait(interval_seconds)


def _collector_heartbeat_loop(stop_event: threading.Event) -> None:
    interval_seconds = max(settings.collector_heartbeat_seconds, 5)
    heartbeat_store = PostgresStore(settings.database_url)
    try:
        while not stop_event.is_set():
            try:
                heartbeat_store.heartbeat("collector")
            except Exception:
                logger.exception("collector heartbeat update failed")
            stop_event.wait(interval_seconds)
    finally:
        heartbeat_store.close()


def run() -> None:
    store = PostgresStore(settings.database_url)
    store.initialize(settings.kerchunk_threshold_seconds)
    stop_event = threading.Event()
    inflight_lock = threading.Lock()
    inflight_done = threading.Event()
    inflight_done.set()
    inflight_events = 0
    talkgroup_thread = threading.Thread(
        target=_talkgroup_sync_loop,
        args=(store, stop_event),
        name="talkgroup-sync",
        daemon=True,
    )
    heartbeat_thread = threading.Thread(
        target=_collector_heartbeat_loop,
        args=(stop_event,),
        name="collector-heartbeat",
        daemon=True,
    )
    talkgroup_thread.start()
    heartbeat_thread.start()
    # The talkgroup sync runs in its own thread. The stream connection starts
    # immediately below and therefore does not wait for the metadata API.
    sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=5,
        reconnection_delay_max=60,
    )
    stored_qso_count = 0

    def request_shutdown(signum: int, _frame: object) -> None:
        """Stop receiving new events and close the stream on container shutdown."""
        if stop_event.is_set():
            return
        logger.info("shutdown requested (signal %s)", signum)
        stop_event.set()
        try:
            if sio.connected:
                sio.disconnect()
        except Exception:
            logger.exception("failed to disconnect from BrandMeister cleanly")

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    @sio.event
    def connect() -> None:
        logger.info("connected to BrandMeister LastHeard")
        sio.emit("join", settings.bm_join)
        logger.info("subscribed to LastHeard stream: %s", settings.bm_join)

    @sio.event
    def disconnect() -> None:
        logger.warning("disconnected from BrandMeister LastHeard")

    @sio.on("mqtt")
    def on_mqtt(data: object) -> None:
        nonlocal stored_qso_count, inflight_events
        with inflight_lock:
            if stop_event.is_set():
                return
            inflight_events += 1
            inflight_done.clear()
        try:
            event = parse_event(data)
            if event is None:
                logger.warning("ignored malformed mqtt payload")
                return
            # Session-stop contains all fields needed to build a completed QSO.
            # Session-start/update and other administrative packets are not used
            # by the sessionizer, so do not persist them in the raw-event audit
            # table. The explicit admin cleanup handles historical rows.
            if event.event_type.casefold() != "session-stop":
                logger.debug("ignored non-QSO event %s (%s)", event.session_id, event.event_type)
                return
            if is_below_kerchunk_threshold(
                event, settings.kerchunk_threshold_seconds
            ):
                duration_seconds = (event.stop_at - event.start_at).total_seconds()
                logger.debug(
                    "ignored below-threshold raw event %s: %.3fs below %.3fs threshold",
                    event.session_id,
                    duration_seconds,
                    settings.kerchunk_threshold_seconds,
                )
                return
            qso = make_qso(
                event,
                settings.kerchunk_threshold_seconds,
                settings.exclude_local_talkgroup,
            )
            threshold_ms = round(settings.kerchunk_threshold_seconds * 1000)
            if qso is not None and qso.duration_ms < threshold_ms:
                logger.debug(
                    "filtered kerchunk/invalid session %s: %.3fs below %.3fs threshold",
                    event.session_id,
                    qso.duration_ms / 1000,
                    settings.kerchunk_threshold_seconds,
                )
                qso = None
            stored = store.ingest(event, qso)
            if stored:
                stored_qso_count += 1
                _log_stored_qso_progress(stored_qso_count)
            elif qso is not None:
                logger.debug("filtered non-displayable QSO %s", qso.session_id)
            else:
                logger.debug("filtered kerchunk/invalid session %s", event.session_id)
        finally:
            with inflight_lock:
                inflight_events -= 1
                if inflight_events == 0:
                    inflight_done.set()

    try:
        while not stop_event.is_set():
            try:
                sio.connect(
                    url=settings.bm_url,
                    socketio_path=settings.bm_socketio_path,
                    transports=["websocket"],
                )
                sio.wait()
            except KeyboardInterrupt:
                request_shutdown(signal.SIGINT, None)
            except Exception:
                if stop_event.is_set():
                    break
                logger.exception("collector connection failed; retrying in 10 seconds")
                stop_event.wait(10)
    finally:
        stop_event.set()
        try:
            if sio.connected:
                sio.disconnect()
        except Exception:
            logger.exception("failed to disconnect from BrandMeister during cleanup")
        if not inflight_done.wait(settings.collector_shutdown_timeout_seconds):
            logger.warning(
                "timed out after %ss waiting for %s in-flight event(s)",
                settings.collector_shutdown_timeout_seconds,
                inflight_events,
            )
        talkgroup_thread.join(timeout=2)
        heartbeat_thread.join(timeout=2)
        store.close()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
