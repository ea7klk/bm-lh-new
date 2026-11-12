"""Nightly raw-event cleanup, driven by a scheduled command.

This used to be a thread inside the collector process. With archiving in place
`raw_events` holds about one day of data, so the scan below is bounded: it used
to walk 12.8M rows and build a multi-million-element Python list, which is why it
ran in a worker that could be OOM-killed. At one day it is ~250k rows.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection, transaction

from .archiver import compact_raw_events
from .models import RawEvent
from .qso_rules import raw_event_threshold_seconds

logger = logging.getLogger(__name__)

DELETE_BATCH = 5000


def irrelevant_raw_event_ids() -> list[int]:
    """Ids of raw events that are duplicates, non-session-stop, or kerchunks.

    Keeps the newest session-stop for each (session_id, event type); everything
    else is a candidate for deletion.
    """
    threshold = raw_event_threshold_seconds()
    candidates: list[int] = []
    seen: set[tuple[str, str]] = set()
    rows = (
        RawEvent.objects.order_by("session_id", "event_type", "-received_at")
        .values_list("pk", "session_id", "event_type", "start_at", "stop_at")
        .iterator(chunk_size=2000)
    )
    for pk, session_id, event_type, start_at, stop_at in rows:
        kind = (event_type or "").lower()
        key = (session_id, kind)
        duplicate = key in seen
        seen.add(key)
        below = bool(start_at and stop_at and (stop_at - start_at).total_seconds() < threshold)
        if kind != "session-stop" or below or duplicate:
            candidates.append(pk)
    return candidates


def clear_irrelevant_raw_events(*, dry_run: bool = False, compact: bool | None = None) -> dict:
    """Delete the rows `irrelevant_raw_event_ids` returns, then reclaim the disk.

    Returns ``{"deleted": n, "compaction": {...}}``. A dry run reports the count
    without writing.

    Deleting leaves the freed pages in the file, so a cleanup must compact
    afterwards or the table keeps its old size on disk: a production run deleted
    184,735 rows and left 241 MB holding 74k.

    The decision to compact is based on the table's size, not on how many rows
    this run deleted. Bloat accumulates across runs — a later cleanup that only
    removes a few thousand rows still inherits a table that needs rewriting — so
    gating on the per-run row count left production bloated. `compact_raw_events`
    applies the size floor (`RAW_COMPACT_MIN_BYTES`) and rewrites only when the
    table is large enough for the rewrite to be worth it.
    """
    if compact is None:
        compact = bool(getattr(settings, "RAW_ARCHIVE_COMPACT", True))

    ids = irrelevant_raw_event_ids()
    if dry_run:
        return {"deleted": len(ids), "compaction": {}, "dry_run": True}

    deleted = 0
    for start in range(0, len(ids), DELETE_BATCH):
        chunk = ids[start:start + DELETE_BATCH]
        with transaction.atomic():
            removed, _ = RawEvent.objects.filter(pk__in=chunk).delete()
        deleted += removed

    if deleted == 0:
        return {"deleted": 0, "compaction": {}}

    if connection.in_atomic_block:
        # VACUUM cannot run inside a transaction block; a caller that wrapped
        # this in atomic() (an admin request, a test) gets the deletes only.
        logger.info("skipping maintenance of raw_events: inside a transaction")
        return {"deleted": deleted, "compaction": {"skipped": True, "reason": "inside a transaction"}}

    if compact:
        compaction = compact_raw_events()
        if compaction.get("skipped"):
            # Too small to be worth rewriting: still mark the freed pages
            # reusable for the next inserts.
            with connection.cursor() as cursor:
                cursor.execute("VACUUM (ANALYZE, PARALLEL 0) raw_events")
    else:
        # Small delete, or compaction disabled: plain VACUUM still makes the
        # freed pages reusable for the next inserts.
        with connection.cursor() as cursor:
            cursor.execute("VACUUM (ANALYZE, PARALLEL 0) raw_events")
        compaction = {"skipped": True, "reason": "compaction disabled"}
    return {"deleted": deleted, "compaction": compaction}
