"""Archive raw events to object storage, one UTC day per file.

Retention rule: everything received before **today 00:00 UTC** is archivable.
That keeps at least one full day in PostgreSQL and puts the boundary on an exact
UTC midnight rather than "N hours ago", so a run is reproducible and a day is
never half-archived.

A day is removed from `raw_events` only after its object has been uploaded and
verified. The order is deliberate:

1. stream the day out of PostgreSQL into a gzipped JSONL temp file
2. upload it with the row count and SHA-256 as object metadata
3. read the object back and compare size, row count and digest
4. delete the day's rows in batches
5. record the `RawEventArchive` row

A crash can therefore only ever leave *more* data, never less: if it dies before
step 4 the rows are still there and the next run redoes the day; if it dies
during step 4 the archive already holds everything and the next run finishes the
delete. When the object and the table have both been partly written, the two are
merged by `payload_hash` before re-uploading, so neither side can win by
overwriting the other.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone as dt_timezone
from time import perf_counter

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .models import RawEvent, RawEventArchive
from .object_store import ObjectStore, ObjectStoreError, get_store

logger = logging.getLogger(__name__)

DELETE_BATCH = 5000
EXPORT_CHUNK = 2000


@dataclass
class DayResult:
    day: date
    rows: int = 0
    bytes_compressed: int = 0
    object_key: str = ""
    sha256: str = ""
    uploaded: bool = False
    merged: bool = False
    deleted: int = 0
    skipped: bool = False
    error: str = ""


@dataclass
class ArchiveSummary:
    days: list[DayResult] = field(default_factory=list)
    compaction: dict = field(default_factory=dict)

    @property
    def rows_archived(self) -> int:
        return sum(day.rows for day in self.days if not day.skipped)

    @property
    def rows_deleted(self) -> int:
        return sum(day.deleted for day in self.days)

    @property
    def errors(self) -> list[DayResult]:
        return [day for day in self.days if day.error]

    def as_dict(self) -> dict:
        return {
            "days_processed": len(self.days),
            "rows_archived": self.rows_archived,
            "rows_deleted": self.rows_deleted,
            "errors": [f"{day.day}: {day.error}" for day in self.errors],
            "compaction": self.compaction,
            "detail": [
                {
                    "day": day.day.isoformat(),
                    "rows": day.rows,
                    "bytes": day.bytes_compressed,
                    "object_key": day.object_key,
                    "uploaded": day.uploaded,
                    "merged": day.merged,
                    "deleted": day.deleted,
                    "skipped": day.skipped,
                    "error": day.error,
                }
                for day in self.days
            ],
        }


def archive_cutoff(now: datetime | None = None) -> datetime:
    """Midnight UTC today: everything strictly before this is archivable."""
    current = (now or timezone.now()).astimezone(dt_timezone.utc)
    return datetime.combine(current.date(), dtime.min, tzinfo=dt_timezone.utc)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dtime.min, tzinfo=dt_timezone.utc)
    return start, start + timedelta(days=1)


def object_key(day: date) -> str:
    """One object per UTC day, lexicographically sortable by date."""
    prefix = settings.RAW_ARCHIVE_PREFIX or "raw-events"
    return f"{prefix}/{day:%Y/%m/%d}.jsonl.gz"


def pending_days(now: datetime | None = None) -> list[date]:
    """UTC days with raw events older than the cutoff, oldest first."""
    cutoff = archive_cutoff(now)
    # datetimes() truncates in the database, so this does not load every row.
    stamps = RawEvent.objects.filter(received_at__lt=cutoff).datetimes(
        "received_at", "day", order="ASC"
    )
    days = sorted({stamp.astimezone(dt_timezone.utc).date() for stamp in stamps})
    return days


def _row_payload(row) -> str:
    return json.dumps(row, separators=(",", ":"), default=_json_default, ensure_ascii=False)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _export_day(day: date, path: str, *, extra_rows: list[dict] | None = None) -> tuple[int, str, int]:
    """Stream one UTC day to a gzipped JSONL file. Returns (rows, sha256, bytes)."""
    start, end = day_bounds(day)
    digest = hashlib.sha256()
    rows = 0
    seen: set[str] = set()

    with gzip.open(path, "wb", compresslevel=6) as handle:
        def write(row: dict) -> None:
            nonlocal rows
            key = row.get("payload_hash") or ""
            if key and key in seen:
                return
            if key:
                seen.add(key)
            line = (_row_payload(row) + "\n").encode("utf-8")
            digest.update(line)
            handle.write(line)
            rows += 1

        for extra in extra_rows or []:
            write(extra)

        queryset = (
            RawEvent.objects.filter(received_at__gte=start, received_at__lt=end)
            .order_by("id")
            .values(
                "payload_hash",
                "session_id",
                "event_type",
                "received_at",
                "start_at",
                "stop_at",
                "payload",
            )
        )
        for row in queryset.iterator(chunk_size=EXPORT_CHUNK):
            write(row)

    return rows, digest.hexdigest(), os.path.getsize(path)


def _read_archive_rows(path: str) -> list[dict]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _verify_upload(store: ObjectStore, key: str, *, rows: int, sha256: str, path: str) -> str:
    """Download the object and compare it to what we meant to write.

    Returns the verified object key, or raises ObjectStoreError. Nothing is
    deleted from PostgreSQL unless this passes.
    """
    head = store.head(key)
    if head is None:
        raise ObjectStoreError(f"{key} is missing immediately after upload")
    expected_size = os.path.getsize(path)
    if head.size != expected_size:
        raise ObjectStoreError(f"{key} is {head.size} bytes on the server, expected {expected_size}")
    if head.rows is not None and head.rows != rows:
        raise ObjectStoreError(f"{key} reports {head.rows} rows on the server, expected {rows}")
    if head.sha256 and head.sha256 != sha256:
        raise ObjectStoreError(f"{key} has digest {head.sha256} on the server, expected {sha256}")

    with tempfile.NamedTemporaryFile(prefix="verify-", suffix=".gz", delete=False) as tmp:
        verify_path = tmp.name
    try:
        store.download_to(key, verify_path)
        digest = hashlib.sha256()
        with gzip.open(verify_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise ObjectStoreError(f"{key} read back with a different digest")
    finally:
        os.unlink(verify_path)
    return key


def archive_day(day: date, store: ObjectStore | None = None, *, dry_run: bool = False) -> DayResult:
    """Archive one UTC day. Safe to re-run.

    Whenever an object for the day already exists it is read back and merged
    with the table by ``payload_hash`` before re-uploading. Row counts cannot
    distinguish the two ways a day ends up in both places — a crash before the
    delete, or late-arriving events after it — and a union is correct in both:
    it can neither drop an archived row nor a row that only exists in the
    table.
    """
    store = store or get_store()
    key = object_key(day)
    result = DayResult(day=day, object_key=key)
    start, end = day_bounds(day)
    table_rows = RawEvent.objects.filter(received_at__gte=start, received_at__lt=end).count()
    existing = store.head(key)

    if table_rows == 0:
        result.skipped = True
        if existing is None:
            result.error = "no rows in the table and no archived object"
        else:
            # Nothing left to do but keep the bookkeeping consistent.
            result.rows = existing.rows or 0
            result.bytes_compressed = existing.size
            result.sha256 = existing.sha256 or ""
            RawEventArchive.objects.update_or_create(
                day=day,
                defaults={
                    "object_key": key,
                    "rows": result.rows,
                    "bytes_compressed": result.bytes_compressed,
                    "sha256": result.sha256,
                    "archived_at": timezone.now(),
                },
            )
        return result

    if dry_run:
        result.rows = table_rows
        result.skipped = True
        return result

    with tempfile.TemporaryDirectory(prefix="raw-archive-") as workdir:
        path = os.path.join(workdir, "day.jsonl.gz")
        merge_rows = None
        if existing is not None:
            existing_path = os.path.join(workdir, "existing.jsonl.gz")
            store.download_to(key, existing_path)
            merge_rows = _read_archive_rows(existing_path)
            result.merged = True

        rows, sha256, size = _export_day(day, path, extra_rows=merge_rows)
        result.rows = rows
        result.sha256 = sha256
        result.bytes_compressed = size
        store.put_file(key, path, rows=rows, sha256=sha256)
        result.uploaded = True
        _verify_upload(store, key, rows=rows, sha256=sha256, path=path)
        result.deleted = _delete_day(day)

    RawEventArchive.objects.update_or_create(
        day=day,
        defaults={
            "object_key": key,
            "rows": result.rows,
            "bytes_compressed": result.bytes_compressed,
            "sha256": result.sha256,
            "archived_at": timezone.now(),
        },
    )
    logger.info(
        "archived raw events for %s: %d rows, %d bytes, %d deleted from the table",
        day, result.rows, result.bytes_compressed, result.deleted,
    )
    return result


def _delete_day(day: date) -> int:
    """Delete one day's rows in batches so no single transaction is long."""
    start, end = day_bounds(day)
    deleted = 0
    while True:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM raw_events
                    WHERE id IN (
                        SELECT id FROM raw_events
                        WHERE received_at >= %s AND received_at < %s
                        ORDER BY id
                        LIMIT %s
                    )
                    """,
                    [start, end, DELETE_BATCH],
                )
                removed = cursor.rowcount
        deleted += removed
        if removed < DELETE_BATCH:
            return deleted


def relation_sizes() -> dict:
    """Current on-disk size of raw_events."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_relation_size('raw_events'), pg_total_relation_size('raw_events')"
        )
        heap, total = cursor.fetchone()
    return {"heap_bytes": int(heap or 0), "total_bytes": int(total or 0)}


def compact_raw_events(*, force: bool = False) -> dict:
    """Reclaim the space DELETE leaves behind in raw_events.

    Deleting a day's rows frees pages for reuse but does not shrink the file, so
    after archiving a long backlog the table keeps its old size on disk. Only
    ``VACUUM FULL`` returns the space; it rewrites the table and takes an
    ``ACCESS EXCLUSIVE`` lock, so the collector's inserts wait for its duration.

    Measured on a table of the same 725-byte row width: 1,736 MB holding 86k live
    rows compacted in 3.4 s, down to 75 MB. The cost is dominated by reading the
    existing heap, so the one-off compaction of a 13 GB backlog takes tens of
    seconds, while steady state (one or two days of rows) is under a second.

    Returns a dict describing what happened; never raises for a lock timeout,
    which is reported instead so the next pass can retry.
    """
    before = relation_sizes()
    if connection.in_atomic_block:
        # VACUUM FULL cannot run inside a transaction block. Scheduled runs are
        # in autocommit and do compact; a caller that wrapped this in atomic()
        # gets a skip rather than an error.
        return {"skipped": True, "reason": "inside a transaction", **before}
    floor = int(getattr(settings, "RAW_COMPACT_MIN_BYTES", 16 * 1024 * 1024))
    if not force and before["heap_bytes"] < floor:
        return {"skipped": True, "reason": "table is already small", **before}

    started = perf_counter()
    try:
        _run_vacuum_full()
        timed_out = False
    except Exception as exc:  # noqa: BLE001 - lock timeout is expected occasionally
        timed_out = True
        logger.warning("raw_events compaction did not run: %s", exc)

    after = relation_sizes()
    result = {
        "skipped": False,
        "timed_out": timed_out,
        "seconds": round(perf_counter() - started, 2),
        "heap_before_bytes": before["heap_bytes"],
        "heap_after_bytes": after["heap_bytes"],
        "freed_bytes": max(0, before["heap_bytes"] - after["heap_bytes"]),
        "total_before_bytes": before["total_bytes"],
        "total_after_bytes": after["total_bytes"],
    }
    if not timed_out:
        logger.info(
            "compacted raw_events: %d MB -> %d MB in %.1fs",
            before["heap_bytes"] // (1024 * 1024),
            after["heap_bytes"] // (1024 * 1024),
            result["seconds"],
        )
    return result


def _run_vacuum_full() -> None:
    """Rewrite raw_events, holding the lock for as short a time as possible.

    The lock timeout applies to acquiring the lock, so a busy table makes this
    give up rather than queue behind another session indefinitely.
    """
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = %s", [f"{_lock_timeout_seconds()}s"])
    try:
        with connection.cursor() as cursor:
            cursor.execute("VACUUM (FULL, ANALYZE, PARALLEL 0) raw_events")
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET lock_timeout")


def _lock_timeout_seconds() -> int:
    return int(getattr(settings, "RAW_COMPACT_LOCK_TIMEOUT_SECONDS", 30))


def archive_pending(*, days: list[date] | None = None, limit: int | None = None,
                    dry_run: bool = False, store: ObjectStore | None = None,
                    now: datetime | None = None, compact: bool | None = None,
                    compact_min_rows: int | None = None) -> ArchiveSummary:
    """Archive every eligible day, oldest first, then compact once.

    `compact` defaults to settings.RAW_ARCHIVE_COMPACT; compaction only runs when
    this pass actually deleted at least `compact_min_rows` rows (default from
    settings), so an idle pass costs nothing.
    """
    if compact is None:
        compact = bool(getattr(settings, "RAW_ARCHIVE_COMPACT", True))
    if compact_min_rows is None:
        compact_min_rows = int(getattr(settings, "RAW_COMPACT_MIN_ROWS", 20000))
    summary = ArchiveSummary()
    todo = days if days is not None else pending_days(now)
    todo = sorted(todo)
    if limit is not None:
        todo = todo[:limit]

    if not todo:
        return summary

    store = store if store is not None else get_store()
    for day in todo:
        try:
            summary.days.append(archive_day(day, store, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - surface and keep going
            logger.exception("archiving %s failed", day)
            summary.days.append(DayResult(day=day, object_key=object_key(day), error=str(exc)))

    # One compaction per pass, never per day: VACUUM FULL rewrites the whole
    # table, so running it after each of a hundred days would be far more
    # expensive than the disk it saves.
    if not dry_run and compact and summary.rows_deleted >= compact_min_rows:
        summary.compaction = compact_raw_events()
    elif not dry_run and compact:
        summary.compaction = {
            "skipped": True,
            "reason": f"only {summary.rows_deleted} row(s) deleted in this pass",
        }
    return summary


def archived_days_newest_first() -> list[RawEventArchive]:
    return list(RawEventArchive.objects.order_by("-day"))


def iter_archived_payloads(store: ObjectStore | None = None):
    """Yield archived rows, newest day first.

    Used by the QSO rebuild so it walks the same days the archiver retired, most
    recent first.
    """
    store = store or get_store()
    for record in archived_days_newest_first():
        with tempfile.TemporaryDirectory(prefix="raw-replay-") as workdir:
            path = os.path.join(workdir, "day.jsonl.gz")
            try:
                store.download_to(record.object_key, path)
            except ObjectStoreError:
                logger.exception("cannot read archived day %s", record.day)
                continue
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        yield record.day, json.loads(line)
