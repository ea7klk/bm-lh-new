import hashlib
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
import socketio
from django.db import close_old_connections, connections, transaction
from django.utils import timezone as django_timezone

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


def store_event(payload, threshold, exclude_talkgroup, raw_threshold=None):
    values = _event_values(payload)
    if raw_threshold is None:
        raw_threshold = raw_event_threshold_seconds()
    if values["event_type"].casefold() == "session-stop" and values["start_at"] is not None and values["stop_at"] is not None:
        if (values["stop_at"] - values["start_at"]).total_seconds() < raw_threshold:
            return False
    RawEvent.objects.update_or_create(
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
    if values["event_type"].casefold() != "session-stop":
        return False
    if values["start_at"] is None or values["stop_at"] is None:
        return False
    duration_ms = round((values["stop_at"] - values["start_at"]).total_seconds() * 1000)
    if duration_ms < round(threshold * 1000):
        return False
    destination_id = values["destination_id"]
    if destination_id in NON_QSO_DESTINATION_IDS:
        QSO.objects.filter(session_id=values["session_id"]).delete()
        return False
    if destination_id is None or destination_id == exclude_talkgroup or destination_id > 999999:
        return False
    talkgroup = Talkgroup.objects.filter(talkgroup_id=destination_id).first()
    if destination_id == 214001:
        talkgroup, _ = Talkgroup.objects.update_or_create(
            talkgroup_id=214001,
            defaults={"name": "Sala Andalucía", "country": "ES", "continent": "Europe", "full_country_name": "Spain", "last_updated": django_timezone.now()},
        )
    destination_name = values["destination_name"] or (talkgroup.name if talkgroup else None)
    if not destination_name:
        return False
    if talkgroup is None:
        talkgroup = Talkgroup.objects.create(talkgroup_id=destination_id, name=str(destination_name))
    # Use a database-native upsert. Lastheard can deliver the same completed
    # session on multiple callback threads, and update_or_create still has an
    # insert race when two transactions observe the missing primary key at
    # the same time. ON CONFLICT makes the session-id uniqueness guarantee
    # atomic at PostgreSQL level.
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
                payload=payload,
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
            "payload",
        ],
        unique_fields=["session_id"],
    )
    return True


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


def run_collector(stop_event):
    threshold = float(os.getenv("KERCHUNK_THRESHOLD_SECONDS", "3"))
    raw_threshold = raw_event_threshold_seconds()
    exclude_talkgroup = int(os.getenv("EXCLUDE_LOCAL_TALKGROUP", "9"))
    removed, _ = QSO.objects.filter(talkgroup_id__in=NON_QSO_DESTINATION_IDS).delete()
    if removed:
        logger.info("removed %d non-QSO service destinations from qsos", removed)
    socket_client = socketio.Client(reconnection=True, reconnection_delay=5, reconnection_delay_max=60)

    @socket_client.event
    def connect():
        logger.info("connected to BrandMeister Lastheard")
        socket_client.emit("join", os.getenv("BM_JOIN", "everything"))

    @socket_client.event
    def disconnect():
        logger.warning("disconnected from BrandMeister Lastheard")

    @socket_client.on("mqtt")
    def on_mqtt(data):
        payload = decode(data)
        if payload is None:
            return
        try:
            close_old_connections()
            with transaction.atomic():
                if store_event(payload, threshold, exclude_talkgroup, raw_threshold):
                    logger.debug("stored QSO")
        except Exception:
            logger.exception("could not store BrandMeister event")
        finally:
            # Django's request middleware does not run for Socket.IO callback
            # threads.  Explicitly release their thread-local connection after
            # every event so a burst cannot leave one connection per callback
            # thread until CONN_MAX_AGE expires.
            connections.close_all()

    while not stop_event.is_set():
        try:
            socket_client.connect(
                os.getenv("BM_URL", "https://api.brandmeister.network"),
                socketio_path=os.getenv("BM_SOCKETIO_PATH", "/lh/socket.io"),
                transports=["websocket"],
            )
            socket_client.wait()
        except Exception:
            if not stop_event.is_set():
                logger.exception("collector connection failed; retrying")
                stop_event.wait(10)
        finally:
            if socket_client.connected:
                socket_client.disconnect()


def heartbeat_loop(stop_event):
    interval = max(int(os.getenv("COLLECTOR_HEARTBEAT_SECONDS", "30")), 5)
    while not stop_event.is_set():
        try:
            close_old_connections()
            ServiceHeartbeat.objects.update_or_create(
                service_name="collector", defaults={"last_seen_at": django_timezone.now()}
            )
        except Exception:
            logger.exception("collector heartbeat update failed")
        finally:
            connections.close_all()
        stop_event.wait(interval)


def talkgroup_loop(stop_event):
    url = os.getenv("TALKGROUPS_URL", "https://api.brandmeister.network/v2/talkgroup")
    try:
        close_old_connections()
        if not Talkgroup.objects.exists():
            logger.info("no talkgroup metadata found; performing initial synchronization")
            sync_talkgroups(url, force=True)
        else:
            repair_talkgroups()
    except Exception:
        logger.exception("initial talkgroup metadata repair failed")
    finally:
        connections.close_all()
    while not stop_event.is_set():
        now = django_timezone.now()
        next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        if stop_event.wait(max((next_run - now).total_seconds(), 1)):
            break
        try:
            close_old_connections()
            logger.info("synchronized %d talkgroups", sync_talkgroups(url, force=True))
        except Exception:
            logger.exception("talkgroup synchronization failed")
        finally:
            connections.close_all()
