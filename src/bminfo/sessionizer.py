from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import BMEvent


MAX_TALKGROUP_ID = 999_999


@dataclass(frozen=True)
class QSO:
    session_id: str
    is_native_session_id: bool
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
    start_at: datetime
    stop_at: datetime
    duration_ms: int
    payload: dict


def make_qso(
    event: BMEvent,
    kerchunk_threshold_seconds: float = 3.0,
    excluded_destination_id: int | None = 9,
) -> QSO | None:
    """Convert a completed BM session into a QSO, or drop it.

    The threshold is deliberately strict: a duration of exactly 3 seconds is
    retained, while anything shorter is treated as a kerchunk.
    """
    if event.event_type.casefold() != "session-stop":
        return None
    if excluded_destination_id is not None and event.destination_id == excluded_destination_id:
        return None
    if event.destination_id is not None and event.destination_id > MAX_TALKGROUP_ID:
        return None
    if event.start_at is None or event.stop_at is None:
        return None

    duration_seconds = (event.stop_at - event.start_at).total_seconds()
    if duration_seconds < 0 or duration_seconds < kerchunk_threshold_seconds:
        return None

    return QSO(
        session_id=event.session_id,
        is_native_session_id=event.is_native_session_id,
        source_id=event.source_id,
        source_call=event.source_call,
        source_name=event.source_name,
        destination_id=event.destination_id,
        destination_call=event.destination_call,
        destination_name=event.destination_name,
        context_id=event.context_id,
        link_call=event.link_call,
        link_name=event.link_name,
        link_type_name=event.link_type_name,
        slot=event.slot,
        master=event.master,
        talker_alias=event.talker_alias,
        rssi=event.rssi,
        ber=event.ber,
        start_at=event.start_at,
        stop_at=event.stop_at,
        duration_ms=round(duration_seconds * 1000),
        payload=event.payload,
    )
