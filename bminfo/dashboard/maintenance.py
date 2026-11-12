"""Nightly raw-event cleanup, driven by a scheduled command.

This used to be a thread inside the collector process. With archiving in place
`raw_events` holds about one day of data, so the scan below is bounded: it used
to walk 12.8M rows and build a multi-million-element Python list, which is why it
ran in a worker that could be OOM-killed. At one day it is ~250k rows.
"""

from __future__ import annotations

import logging

from django.db import connection, transaction

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


def clear_irrelevant_raw_events(*, dry_run: bool = False) -> int:
    """Delete those rows in batches, then vacuum.

    Returns the number deleted, or for a dry run the number that would be.
    """
    ids = irrelevant_raw_event_ids()
    if dry_run or not ids:
        return len(ids)

    deleted = 0
    for start in range(0, len(ids), DELETE_BATCH):
        chunk = ids[start:start + DELETE_BATCH]
        with transaction.atomic():
            removed, _ = RawEvent.objects.filter(pk__in=chunk).delete()
        deleted += removed

    # VACUUM cannot run inside a transaction block. Scheduled runs are in
    # autocommit and do vacuum; a caller that wraps this in atomic() (an admin
    # request, a test) gets the deletes without the vacuum rather than an error.
    if connection.in_atomic_block:
        logger.info("skipping VACUUM of raw_events: inside a transaction")
    else:
        with connection.cursor() as cursor:
            cursor.execute("VACUUM (ANALYZE, PARALLEL 0) raw_events")
    return deleted
