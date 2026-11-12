"""Regenerate QSOs from raw events, using both the table and the archive.

Order matters and is deliberate. The rebuild walks:

1. the rows still in `raw_events` (the most recent day or two), then
2. archived days **newest first**, streaming each gzipped file from object
   storage.

`session_id` is the QSO primary key and the upsert is idempotent, so walking
from newest to oldest would also work; going oldest-last keeps the most recent
reading of a session if the same session somehow appears twice.

Unlike the old implementation this never deletes the QSO table first. Rows are
upserted, so a rebuild that dies half way leaves a usable table, and a rebuild
cannot destroy QSOs whose raw event is temporarily unavailable (for example
while its archive object is being read). That ordering hazard was H3 in
REVIEW.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from datetime import datetime, timezone as dt_timezone

from django.db import transaction

from .models import QSO, RawEvent, Talkgroup
from .qso_rules import NON_QSO_DESTINATION_IDS

logger = logging.getLogger(__name__)

BATCH = 1000


@dataclass
class RebuildStats:
    scanned: int = 0
    rebuilt: int = 0
    skipped: int = 0
    sources: list[str] = None

    def __post_init__(self):
        if self.sources is None:
            self.sources = []

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "rebuilt": self.rebuilt,
            "skipped": self.skipped,
            "sources": self.sources,
        }


def iter_database_rows(*, newest_first: bool = False):
    """Yield ``(source, row)`` for events still present in PostgreSQL."""
    order = "-received_at" if newest_first else "received_at"
    queryset = (
        RawEvent.objects.filter(event_type__iexact="session-stop")
        .order_by(order)
        .values("payload_hash", "session_id", "event_type", "received_at", "start_at", "stop_at", "payload")
    )
    for row in queryset.iterator(chunk_size=BATCH):
        yield "database", row


def iter_archive_rows(*, store=None):
    """Yield ``(source, row)`` from the archive, newest day first."""
    from .archiver import iter_archived_payloads

    for day, row in iter_archived_payloads(store):
        yield day.isoformat(), row


def _as_datetime(value):
    """Coerce a value from the table or from JSON into an aware datetime."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def _candidate(payload: dict, start_at, stop_at, *, threshold: float, exclude_talkgroup: int):
    """Return the QSO field dict for one event, or None if it is not a QSO."""
    start_at = _as_datetime(start_at)
    stop_at = _as_datetime(stop_at)
    if not start_at or not stop_at:
        return None
    if (stop_at - start_at).total_seconds() < threshold:
        return None

    def value(*names):
        for name in names:
            if payload.get(name) not in (None, ""):
                return payload[name]
        return None

    destination = value("DestinationID", "destination_id")
    try:
        destination = int(destination) if destination is not None else None
    except (TypeError, ValueError):
        destination = None
    if destination is None or destination in NON_QSO_DESTINATION_IDS:
        return None
    if destination == exclude_talkgroup or destination > 999999:
        return None
    return {
        "session_id": None,  # filled by the caller
        "source_id": value("SourceID", "source_id"),
        "source_call": value("SourceCall", "source_call"),
        "source_name": value("SourceName", "source_name"),
        "talkgroup_id": destination,
        "destination_call": value("DestinationCall", "destination_call"),
        "destination_name": value("DestinationName", "destination_name"),
        "context_id": value("ContextID", "context_id"),
        "link_call": value("LinkCall", "link_call"),
        "link_name": value("LinkName", "link_name"),
        "link_type_name": value("LinkTypeName", "link_type_name"),
        "slot": value("Slot", "slot"),
        "master": value("Master", "master"),
        "talker_alias": value("TalkerAlias", "talker_alias"),
        "rssi": value("RSSI", "rssi"),
        "ber": value("BER", "ber"),
        "start_at": start_at,
        "stop_at": stop_at,
        "duration_ms": round((stop_at - start_at).total_seconds() * 1000),
        "payload": payload,
    }


def rebuild_from_sources(*, threshold: float, exclude_talkgroup: int, include_archive: bool = True,
                         store=None, limit_days: int | None = None, dry_run: bool = False,
                         progress=None) -> RebuildStats:
    """Upsert QSOs from the table, then from the archive.

    `limit_days` caps how many archived days are read, newest first; it is for
    spot checks and for keeping a synchronous caller bounded.
    """
    stats = RebuildStats()
    stats.sources.append("database")

    def flush(batch):
        if dry_run or not batch:
            return 0
        talkgroup_ids = {row["talkgroup_id"] for row in batch}
        known = {tg.talkgroup_id for tg in Talkgroup.objects.filter(talkgroup_id__in=talkgroup_ids)}
        for missing in talkgroup_ids - known:
            name = next((row["destination_name"] for row in batch if row["talkgroup_id"] == missing and row["destination_name"]), None)
            if name:
                Talkgroup.objects.get_or_create(talkgroup_id=missing, defaults={"name": str(name)})
        with transaction.atomic():
            QSO.objects.bulk_create(
                [QSO(**row) for row in batch],
                update_conflicts=True,
                update_fields=[
                    "source_id", "source_call", "source_name", "talkgroup",
                    "destination_call", "destination_name", "context_id", "link_call",
                    "link_name", "link_type_name", "slot", "master", "talker_alias",
                    "rssi", "ber", "start_at", "stop_at", "duration_ms", "payload",
                ],
                unique_fields=["session_id"],
            )
        return len(batch)

    def consume(rows):
        batch = []
        seen: set[str] = set()
        for _source, source_row in rows:
            stats.scanned += 1
            payload = source_row.get("payload") or {}
            session_id = source_row.get("session_id")
            if not session_id or session_id in seen:
                stats.skipped += 1
                continue
            seen.add(session_id)
            fields = _candidate(
                payload,
                source_row.get("start_at"),
                source_row.get("stop_at"),
                threshold=threshold,
                exclude_talkgroup=exclude_talkgroup,
            )
            if fields is None:
                stats.skipped += 1
                continue
            fields["session_id"] = session_id
            batch.append(fields)
            if len(batch) >= BATCH:
                stats.rebuilt += flush(batch)
                batch = []
                if progress:
                    progress(stats)
        if batch:
            stats.rebuilt += flush(batch)

    consume(iter_database_rows())

    if include_archive:
        stats.sources.append("archive")
        archive_rows = iter_archive_rows(store=store)
        if limit_days is not None:
            archive_rows = _limit_days(archive_rows, limit_days)
        consume(archive_rows)

    if progress:
        progress(stats)
    return stats


def _limit_days(rows, limit_days: int):
    """Yield ``(source, row)`` from at most ``limit_days`` distinct archive days.

    The archive is walked newest first, so this reads the newest days only.
    """
    seen: set[str] = set()
    for source, row in rows:
        if source not in seen:
            if len(seen) >= limit_days:
                return
            seen.add(source)
        yield source, row
