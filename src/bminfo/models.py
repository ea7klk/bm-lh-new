from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Mapping


UTC = timezone.utc


def _first(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def _as_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_epoch_seconds(value: Any) -> float | None:
    """Return Unix seconds; accept BM seconds, milliseconds, and ISO strings."""
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        timestamp = value.replace(tzinfo=UTC if value.tzinfo is None else value.tzinfo)
        return timestamp.timestamp()
    if isinstance(value, Real):
        number = float(value)
    else:
        text = str(value).strip()
        try:
            number = float(text)
        except ValueError:
            try:
                timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return timestamp.timestamp()
    # Current epoch seconds are ~1e9; milliseconds are ~1e12.
    return number / 1000 if abs(number) >= 100_000_000_000 else number


def _as_datetime(value: Any) -> datetime | None:
    seconds = _as_epoch_seconds(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class BMEvent:
    session_id: str
    is_native_session_id: bool
    event_type: str
    received_at: datetime
    start_at: datetime | None
    stop_at: datetime | None
    source_id: int | None
    source_call: str | None
    source_name: str | None
    destination_id: int | None
    destination_call: str | None
    destination_name: str | None
    context_id: int | None
    link_call: str | None
    link_name: str | None
    link_type_name: str | None
    slot: int | None
    master: str | None
    talker_alias: str | None
    rssi: float | None
    ber: float | None
    payload: dict[str, Any]

    @property
    def payload_hash(self) -> str:
        canonical = json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decode_payload(data: Any) -> dict[str, Any] | None:
    """Decode the Socket.IO `mqtt` event's nested JSON payload."""
    payload: Any = data
    if isinstance(data, Mapping) and "payload" in data:
        payload = data["payload"]
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, Mapping):
        return None
    return dict(_jsonable(payload))


def parse_event(data: Any, received_at: datetime | None = None) -> BMEvent | None:
    payload = decode_payload(data)
    if payload is None:
        return None

    received_at = received_at or datetime.now(tz=UTC)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    received_at = received_at.astimezone(UTC)

    native_id = _first(payload, "SessionID", "SessionId", "session_id")
    is_native_session_id = native_id not in (None, "")
    if is_native_session_id:
        session_id = str(native_id)
    else:
        # Some non-session packets have no stable ID. A deterministic key lets
        # the database deduplicate repeats while raw payloads remain inspectable.
        basis = {
            key: payload.get(key)
            for key in (
                "Event", "Start", "Stop", "SourceID", "DestinationID", "ContextID", "Slot"
            )
        }
        session_id = "derived-" + hashlib.sha256(
            json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    return BMEvent(
        session_id=session_id,
        is_native_session_id=is_native_session_id,
        event_type=str(_first(payload, "Event", "event") or "UNKNOWN"),
        received_at=received_at,
        start_at=_as_datetime(_first(payload, "Start", "start")),
        stop_at=_as_datetime(_first(payload, "Stop", "stop")),
        source_id=_as_int(_first(payload, "SourceID", "source_id")),
        source_call=_first(payload, "SourceCall", "source_call"),
        source_name=_first(payload, "SourceName", "source_name"),
        destination_id=_as_int(_first(payload, "DestinationID", "destination_id")),
        destination_call=_first(payload, "DestinationCall", "destination_call"),
        destination_name=_first(payload, "DestinationName", "destination_name"),
        context_id=_as_int(_first(payload, "ContextID", "context_id")),
        link_call=_first(payload, "LinkCall", "link_call"),
        link_name=_first(payload, "LinkName", "link_name"),
        link_type_name=_first(payload, "LinkTypeName", "link_type_name"),
        slot=_as_int(_first(payload, "Slot", "slot")),
        master=None
        if _first(payload, "Master", "master") is None
        else str(_first(payload, "Master", "master")),
        talker_alias=_first(payload, "TalkerAlias", "talker_alias"),
        rssi=_as_float(_first(payload, "RSSI", "rssi")),
        ber=_as_float(_first(payload, "BER", "ber")),
        payload=payload,
    )
