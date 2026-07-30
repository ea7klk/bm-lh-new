from __future__ import annotations

import logging
import threading
import time

import socketio

from .config import settings
from .models import parse_event
from .sessionizer import make_qso
from .storage import PostgresStore
from .talkgroups import sync_talkgroups


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def _talkgroup_sync_loop(stop_event: threading.Event) -> None:
    interval_seconds = max(settings.talkgroups_sync_hours, 1) * 60 * 60
    while not stop_event.is_set():
        try:
            sync_talkgroups(settings.database_url, settings.talkgroups_url)
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
    talkgroup_thread = threading.Thread(
        target=_talkgroup_sync_loop,
        args=(stop_event,),
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
    sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=5,
        reconnection_delay_max=60,
    )

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
        event = parse_event(data)
        if event is None:
            logger.warning("ignored malformed mqtt payload")
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
            logger.info(
                "stored QSO %s: %s -> %s for %.3fs",
                qso.session_id,
                qso.source_call or qso.source_id or "unknown",
                qso.destination_call or qso.destination_id or "unknown",
                qso.duration_ms / 1000,
            )
        elif qso is not None:
            logger.debug("filtered non-displayable QSO %s", qso.session_id)
        elif event.event_type.casefold() == "session-stop":
            logger.debug("filtered kerchunk/invalid session %s", event.session_id)
        else:
            logger.debug("stored non-stop event %s (%s)", event.session_id, event.event_type)

    while True:
        try:
            sio.connect(
                url=settings.bm_url,
                socketio_path=settings.bm_socketio_path,
                transports=["websocket"],
            )
            sio.wait()
        except KeyboardInterrupt:
            logger.info("stopping collector")
            break
        except Exception:
            logger.exception("collector connection failed; retrying in 10 seconds")
            time.sleep(10)
    stop_event.set()
    talkgroup_thread.join(timeout=2)
    heartbeat_thread.join(timeout=2)
    store.close()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
