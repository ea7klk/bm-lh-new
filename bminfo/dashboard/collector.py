import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from django.db import close_old_connections, connections, transaction
from django.utils import timezone as django_timezone

from .lastheard_client import LastheardClient
from .models import QSO, RawEvent, ServiceHeartbeat, Talkgroup
from .qso_rules import NON_QSO_DESTINATION_IDS, raw_event_threshold_seconds
from .talkgroups import PINNED_TALKGROUPS, classify_talkgroup, repair_talkgroups


UTC = timezone.utc
logger = logging.getLogger(__name__)


def _value(payload, *names):
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _number(value, integer=False):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return int(result) if integer else result
    except (TypeError, ValueError):
        return None


def _datetime(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if abs(number) >= 100_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
        except ValueError:
            return None


def decode(data):
    payload = data.get("payload") if isinstance(data, dict) and "payload" in data else data
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    return dict(payload) if isinstance(payload, dict) else None


def _session_id(payload):
    native = _value(payload, "SessionID", "SessionId", "session_id")
    if native not in (None, ""):
        return str(native), True
    basis = {key: payload.get(key) for key in ("Event", "Start", "Stop", "SourceID", "DestinationID", "ContextID", "Slot")}
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"derived-{digest}", False


def _payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _event_values(payload):
    session_id, native = _session_id(payload)
    return {
        "session_id": session_id,
        "is_native": native,
        "event_type": str(_value(payload, "Event", "event") or "UNKNOWN"),
        "start_at": _datetime(_value(payload, "Start", "start")),
        "stop_at": _datetime(_value(payload, "Stop", "stop")),
        "source_id": _number(_value(payload, "SourceID", "source_id"), integer=True),
        "source_call": _value(payload, "SourceCall", "source_call"),
        "source_name": _value(payload, "SourceName", "source_name"),
        "destination_id": _number(_value(payload, "DestinationID", "destination_id"), integer=True),
        "destination_call": _value(payload, "DestinationCall", "destination_call"),
        "destination_name": _value(payload, "DestinationName", "destination_name"),
        "context_id": _number(_value(payload, "ContextID", "context_id"), integer=True),
        "link_call": _value(payload, "LinkCall", "link_call"),
        "link_name": _value(payload, "LinkName", "link_name"),
        "link_type_name": _value(payload, "LinkTypeName", "link_type_name"),
        "slot": _number(_value(payload, "Slot", "slot"), integer=True),
        "master": _value(payload, "Master", "master"),
        "talker_alias": _value(payload, "TalkerAlias", "talker_alias"),
        "rssi": _number(_value(payload, "RSSI", "rssi")),
        "ber": _number(_value(payload, "BER", "ber")),
    }


# How many rows to write between progress log lines.
PROGRESS_LOG_EVERY = 100


@dataclass(frozen=True)
class StoreResult:
    """What storing one event produced.

    ``bool()`` answers "was a QSO written", which is what the collector and its
    tests have always asked of the return value. The row count is separate so
    callers that report throughput can count raw events too.
    """

    raw_written: bool = False
    qso_written: bool = False
    talkgroup_created: bool = False
    raw_inserted: bool = False
    qso_inserted: bool = False

    def __bool__(self) -> bool:
        return self.qso_written

    @property
    def rows_written(self) -> int:
        """Collected rows written, including updates of duplicate deliveries."""
        return int(self.raw_written) + int(self.qso_written)

    @property
    def rows_inserted(self) -> int:
        """New rows added: the raw event plus any QSO.

        LastHeard delivers the same event more than once — on several callback
        threads, and again after a reconnect — and each delivery updates the
        existing row. Counting writes therefore runs several times higher than
        the number of rows the table actually gains: about 1M writes a day
        against 130k archived rows. This counts only true inserts, so the
        progress log reflects how much data is being added.

        Talkgroup catalogue rows are reference data, not collected events, so
        they are excluded.
        """
        return int(self.raw_inserted) + int(self.qso_inserted)


class WriteCounter:
    """Counts new rows inserted since the collector started.

    Logs a running total every ``step`` rows so an operator can tell at a glance
    that ingestion is still moving, without enabling DEBUG. The count is reset
    only by restarting the process: it lives for the whole life of
    ``run_collector``, so a reconnect inside the retry loop does not reset it.

    Events arrive on several Socket.IO callback threads, so the total is guarded
    by a lock. The log line is emitted outside the lock to keep the critical
    section to an integer add.
    """

    def __init__(self, step: int = PROGRESS_LOG_EVERY):
        self._step = max(int(step), 1)
        self._lock = threading.Lock()
        self._total = 0

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    @property
    def step(self) -> int:
        return self._step

    def add(self, written: int) -> int | None:
        """Record ``written`` new rows; return the total if a step was crossed."""
        if written <= 0:
            return None
        with self._lock:
            previous = self._total
            self._total += written
            total = self._total
            crossed = total // self._step > previous // self._step
        if crossed:
            logger.info(
                "collector progress: %d rows inserted since restart", total
            )
            return total
        return None


def store_event(payload, threshold, exclude_talkgroup, raw_threshold=None) -> StoreResult:
    """Store one decoded event.

    Returns a `StoreResult`. ``bool(result)`` is true only when a QSO was
    upserted, which is the meaning callers have always relied on; the raw event
    may still have been recorded, and `rows_written` reports that.
    """
    values = _event_values(payload)
    if raw_threshold is None:
        raw_threshold = raw_event_threshold_seconds()
    if values["event_type"].casefold() == "session-stop" and values["start_at"] is not None and values["stop_at"] is not None:
        if (values["stop_at"] - values["start_at"]).total_seconds() < raw_threshold:
            return StoreResult()
    _, raw_inserted = RawEvent.objects.update_or_create(
        payload_hash=_payload_hash(payload),
        defaults={
            "session_id": values["session_id"],
            "event_type": values["event_type"],
            "received_at": django_timezone.now(),
            "start_at": values["start_at"],
            "stop_at": values["stop_at"],
            "payload": payload,
        },
    )
    # The raw event is recorded from here on.
    raw = StoreResult(raw_written=True, raw_inserted=raw_inserted)
    if values["event_type"].casefold() != "session-stop":
        return raw
    if values["start_at"] is None or values["stop_at"] is None:
        return raw
    duration_ms = round((values["stop_at"] - values["start_at"]).total_seconds() * 1000)
    if duration_ms < round(threshold * 1000):
        return raw
    destination_id = values["destination_id"]
    if destination_id in NON_QSO_DESTINATION_IDS:
        # Removing a stale QSO is a delete, not an insertion, so it is not
        # counted; the raw event above still was.
        QSO.objects.filter(session_id=values["session_id"]).delete()
        return raw
    if destination_id is None or destination_id == exclude_talkgroup or destination_id > 999999:
        return raw
    talkgroup = Talkgroup.objects.filter(talkgroup_id=destination_id).first()
    if destination_id == 214001:
        talkgroup, _ = Talkgroup.objects.update_or_create(
            talkgroup_id=214001,
            defaults={"name": "Sala Andalucía", "country": "ES", "continent": "Europe", "full_country_name": "Spain", "last_updated": django_timezone.now()},
        )
    destination_name = values["destination_name"] or (talkgroup.name if talkgroup else None)
    if not destination_name:
        return raw
    talkgroup_created = False
    if talkgroup is None:
        talkgroup = Talkgroup.objects.create(talkgroup_id=destination_id, name=str(destination_name))
        talkgroup_created = True
    # Use a database-native upsert. Lastheard can deliver the same completed
    # session on multiple callback threads, and update_or_create still has an
    # insert race when two transactions observe the missing primary key at
    # the same time. ON CONFLICT makes the session-id uniqueness guarantee
    # atomic at PostgreSQL level.
    # bulk_create with ON CONFLICT cannot report whether it inserted or updated,
    # so newness is checked first. The upsert below still guarantees correctness
    # under a race; this only affects what the counter reports.
    qso_inserted = not QSO.objects.filter(session_id=values["session_id"]).exists()
    QSO.objects.bulk_create(
        [
            QSO(
                session_id=values["session_id"],
                source_id=values["source_id"],
                source_call=values["source_call"],
                source_name=values["source_name"],
                talkgroup_id=talkgroup.talkgroup_id,
                destination_call=values["destination_call"],
                destination_name=destination_name,
                context_id=values["context_id"],
                link_call=values["link_call"],
                link_name=values["link_name"],
                link_type_name=values["link_type_name"],
                slot=values["slot"],
                master=values["master"],
                talker_alias=values["talker_alias"],
                rssi=values["rssi"],
                ber=values["ber"],
                start_at=values["start_at"],
                stop_at=values["stop_at"],
                duration_ms=duration_ms,
                is_native_session_id=values["is_native"],
            )
        ],
        update_conflicts=True,
        update_fields=[
            "source_id",
            "source_call",
            "source_name",
            "talkgroup",
            "destination_call",
            "destination_name",
            "context_id",
            "link_call",
            "link_name",
            "link_type_name",
            "slot",
            "master",
            "talker_alias",
            "rssi",
            "ber",
            "start_at",
            "stop_at",
            "duration_ms",
            "is_native_session_id",
        ],
        unique_fields=["session_id"],
    )
    return StoreResult(
        raw_written=True, qso_written=True, talkgroup_created=talkgroup_created,
        raw_inserted=raw_inserted, qso_inserted=qso_inserted,
    )


def sync_talkgroups(url, *, force=False):
    repair_talkgroups()
    last = ServiceHeartbeat.objects.filter(service_name="talkgroups").values_list("last_seen_at", flat=True).first()
    if not force and last and (django_timezone.now() - last).total_seconds() < 24 * 3600:
        logger.info("talkgroup metadata is fresh; skipping update")
        return 0
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        data = [{"id": key, "name": value} for key, value in data.items()]
    if not isinstance(data, list):
        return 0
    count = 0
    updated_at = django_timezone.now()
    for item in data:
        if not isinstance(item, dict):
            continue
        talkgroup_id = _number(_value(item, "id", "talkgroup_id", "talkgroupId"), integer=True)
        if talkgroup_id is None:
            continue
        country, continent, full_country_name = classify_talkgroup(talkgroup_id)
        name = str(_value(item, "name", "talkgroupName") or talkgroup_id)
        if talkgroup_id == 214001:
            name, country, continent, full_country_name = PINNED_TALKGROUPS[0][1:]
        Talkgroup.objects.update_or_create(
            talkgroup_id=talkgroup_id,
            defaults={
                "name": name,
                "country": country,
                "continent": continent,
                "full_country_name": full_country_name,
                "last_updated": updated_at,
            },
        )
        count += 1
    for talkgroup_id, name, country, continent, full_country_name in PINNED_TALKGROUPS:
        Talkgroup.objects.update_or_create(
            talkgroup_id=talkgroup_id,
            defaults={"name": name, "country": country, "continent": continent, "full_country_name": full_country_name, "last_updated": updated_at},
        )
    ServiceHeartbeat.objects.update_or_create(service_name="talkgroups", defaults={"last_seen_at": updated_at})
    return count


def run_collector(stop_event, connected_event):
    threshold = float(os.getenv("KERCHUNK_THRESHOLD_SECONDS", "3"))
    raw_threshold = raw_event_threshold_seconds()
    exclude_talkgroup = int(os.getenv("EXCLUDE_LOCAL_TALKGROUP", "9"))
    removed, _ = QSO.objects.filter(talkgroup_id__in=NON_QSO_DESTINATION_IDS).delete()
    if removed:
        logger.info("removed %d non-QSO service destinations from qsos", removed)
    # Use one retry owner and interruptible waits so SIGTERM can close an
    # active connection as well as stop connection retries.
    engineio_logger = logging.getLogger("engineio.client")
    engineio_logger.setLevel(logging.WARNING)
    socket_client = LastheardClient(
        reconnection=False,
        handle_sigint=False,
        engineio_logger=engineio_logger,
    )

    @socket_client.event
    def connect():
        logger.info("connected to BrandMeister Lastheard")
        socket_client.emit("join", os.getenv("BM_JOIN", "everything"))
        connected_event.set()

    @socket_client.event
    def disconnect(reason=None):
        connected_event.clear()
        log = logger.info if stop_event.is_set() else logger.warning
        log("disconnected from BrandMeister Lastheard: %s", reason or "unknown reason")

    # One counter for the life of this run, so its total is "since restart" and
    # survives the reconnect loop below. It logs every PROGRESS_LOG_EVERY rows.
    counter = WriteCounter()

    @socket_client.on("mqtt")
    def on_mqtt(data):
        payload = decode(data)
        if payload is None:
            return
        try:
            close_old_connections()
            with transaction.atomic():
                result = store_event(payload, threshold, exclude_talkgroup, raw_threshold)
            written = result.rows_inserted
            if written:
                logger.debug("stored event: %d row(s)", written)
                counter.add(written)
        except Exception:
            logger.exception("could not store BrandMeister event")
        finally:
            # Django's request middleware does not run for Socket.IO callback
            # threads.  Explicitly release their thread-local connection after
            # every event so a burst cannot leave one connection per callback
            # thread until CONN_MAX_AGE expires.
            connections.close_all()

    retry_delay = 5
    while not stop_event.is_set():
        try:
            socket_client.connect(
                os.getenv("BM_URL", "https://api.brandmeister.network"),
                socketio_path=os.getenv("BM_SOCKETIO_PATH", "/lh/socket.io"),
                transports=["websocket"],
            )
            retry_delay = 5
            while socket_client.connected and not stop_event.wait(1):
                pass
        except Exception:
            if not stop_event.is_set():
                logger.exception("collector connection failed; retrying")
        finally:
            connected_event.clear()
            socket_client.shutdown()
        if not stop_event.is_set():
            stop_event.wait(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


def heartbeat_loop(stop_event, connected_event):
    interval = max(int(os.getenv("COLLECTOR_HEARTBEAT_SECONDS", "30")), 5)
    while not stop_event.is_set():
        try:
            close_old_connections()
            # A running heartbeat thread alone does not prove that collection
            # works. Let /status/ degrade during a sustained disconnection.
            if connected_event.is_set():
                ServiceHeartbeat.objects.update_or_create(
                    service_name="collector", defaults={"last_seen_at": django_timezone.now()}
                )
        except Exception:
            logger.exception("collector heartbeat update failed")
        finally:
            connections.close_all()
        stop_event.wait(interval)


def bootstrap_talkgroups():
    """Refresh talkgroup metadata once, at collector start.

    The catalogue used to be re-synced by a loop inside this process; that is now
    a scheduled CronJob (`manage.py sync_talkgroups`). The startup pass stays
    because a fresh install has no catalogue and a restart should repair it:
    `sync_talkgroups(force=False)` is a no-op when the data is fresh.
    """
    url = os.getenv("TALKGROUPS_URL", "https://api.brandmeister.network/v2/talkgroup")
    try:
        close_old_connections()
        if not Talkgroup.objects.exists():
            logger.info("no talkgroup metadata found; performing initial synchronization")
            logger.info("synchronized %d talkgroups", sync_talkgroups(url, force=True))
        else:
            repair_talkgroups()
    except Exception:
        logger.exception("talkgroup metadata bootstrap failed")
    finally:
        connections.close_all()
