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

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .models import RawEvent, RawEventArchive
from .object_store import ObjectStore, ObjectStoreError, get_store, storage_configured

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


def archive_pending(*, days: list[date] | None = None, limit: int | None = None,
                    dry_run: bool = False, store: ObjectStore | None = None,
                    now: datetime | None = None) -> ArchiveSummary:
    """Archive every eligible day, oldest first."""
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


def archive_loop(stop_event, *, interval: int | None = None):
    """Background loop started alongside the collector.

    Runs the archive pass, then sleeps. If a pass archived nothing it keeps
    sleeping at the configured interval; there is no burst behaviour because at
    most one day becomes eligible per UTC midnight.
    """
    period = interval or settings.RAW_ARCHIVE_INTERVAL_SECONDS
    problem = None if storage_configured() else "object storage is not configured"
    if not settings.RAW_ARCHIVE_ENABLED:
        logger.info("raw-event archiving is disabled (RAW_ARCHIVE_ENABLED=false)")
        return
    if problem:
        logger.warning("raw-event archiving idle: %s", problem)
        return

    logger.info("raw-event archiving enabled; checking every %ds", period)
    while not stop_event.is_set():
        try:
            summary = archive_pending()
            if summary.days:
                logger.info(
                    "archive pass: %d day(s), %d rows archived, %d rows deleted",
                    len(summary.days), summary.rows_archived, summary.rows_deleted,
                )
            for failure in summary.errors:
                logger.error("archive pass error for %s: %s", failure.day, failure.error)
        except Exception:
            logger.exception("raw-event archive pass failed")
        finally:
            from django.db import connections
            connections.close_all()
        stop_event.wait(period)
