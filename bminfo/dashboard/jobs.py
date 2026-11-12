"""Run maintenance jobs in a worker thread with durable status.

An admin POST must return immediately: archiving a day or rebuilding millions of
QSOs takes minutes. Each job therefore runs in a `threading.Thread` that owns its
own database connection (Django's are thread-local), and writes progress to a
`MaintenanceRun` row so the page can report what happened even after a restart.

Only one run of each kind may be active at a time. The guard is a database row
plus a process-local lock: the row covers several workers, the lock covers the
moment before the row exists.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.db import connections
from django.utils import timezone

from .models import MaintenanceRun

logger = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {
    MaintenanceRun.ARCHIVE: threading.Lock(),
    MaintenanceRun.REBUILD: threading.Lock(),
    MaintenanceRun.COMPACT: threading.Lock(),
}

# A run older than this is treated as abandoned (the process died mid-job).
STALE_AFTER = timedelta(hours=12)


def active_run(kind: str) -> MaintenanceRun | None:
    cutoff = timezone.now() - STALE_AFTER
    return (
        MaintenanceRun.objects.filter(kind=kind, status=MaintenanceRun.RUNNING, started_at__gte=cutoff)
        .order_by("-started_at")
        .first()
    )


def last_run(kind: str) -> MaintenanceRun | None:
    return MaintenanceRun.objects.filter(kind=kind).order_by("-started_at").first()


def start_job(kind: str, target, **kwargs) -> tuple[MaintenanceRun | None, str]:
    """Start `target(run=..., **kwargs)` in a thread.

    Returns (run, reason). `run` is None when a job of this kind is already
    active, and `reason` explains why.
    """
    lock = _locks[kind]
    if not lock.acquire(blocking=False):
        return None, "a job of this kind is already running in this process"

    running = active_run(kind)
    if running is not None:
        lock.release()
        return None, f"a job of this kind has been running since {running.started_at:%Y-%m-%d %H:%M} UTC"

    run = MaintenanceRun.objects.create(kind=kind, status=MaintenanceRun.RUNNING, detail={})

    def worker():
        try:
            result = target(run=run, **kwargs)
            run.status = MaintenanceRun.SUCCEEDED
            # The job body normally writes a descriptive message; only fall back
            # to a generic one if it did not.
            if not run.message:
                run.message = "completed"
            if isinstance(result, dict):
                run.detail = result
        except Exception as exc:  # noqa: BLE001 - recorded and shown to the operator
            logger.exception("maintenance job %s failed", kind)
            run.status = MaintenanceRun.FAILED
            run.message = str(exc)[:2000]
        finally:
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "message", "detail", "finished_at"])
            connections.close_all()
            lock.release()

    threading.Thread(target=worker, name=f"maintenance-{kind}", daemon=True).start()
    return run, "started"


def archive_job(run: MaintenanceRun, *, dry_run: bool = False, limit: int | None = None) -> dict:
    """Archive every eligible day and reclaim disk, updating `run.detail`."""
    from .archiver import archive_pending

    summary = archive_pending(limit=limit, dry_run=dry_run)
    payload = summary.as_dict()
    run.detail = payload
    message = (
        f"{payload['days_processed']} day(s): {payload['rows_archived']} rows archived, "
        f"{payload['rows_deleted']} deleted"
    )
    compaction = payload.get("compaction") or {}
    if compaction and not compaction.get("skipped"):
        message += (
            f"; compacted to {compaction['heap_after_bytes'] // 1048576} MB "
            f"in {compaction['seconds']}s"
        )
    elif compaction.get("skipped"):
        message += f"; no compaction ({compaction.get('reason')})"
    if dry_run:
        message = f"dry run: {payload['days_processed']} day(s) pending"
    run.message = message
    run.save(update_fields=["detail", "message"])
    return payload


def compact_job(run: MaintenanceRun) -> dict:
    """Reclaim disk in raw_events on its own, outside an archive pass."""
    from .archiver import compact_raw_events

    result = compact_raw_events(force=True)
    run.detail = result
    run.message = (
        f"raw_events {result['heap_before_bytes'] // 1048576} MB -> "
        f"{result['heap_after_bytes'] // 1048576} MB in {result['seconds']}s"
        + (" (lock timeout, will retry)" if result.get("timed_out") else "")
    )
    run.save(update_fields=["detail", "message"])
    return result


def rebuild_job(run: MaintenanceRun, *, include_archive: bool = True, limit_days: int | None = None) -> dict:
    """Rebuild QSOs from the table and the archive."""
    import os

    from .rebuild import rebuild_from_sources

    def progress(stats):
        run.detail = stats.as_dict()
        run.save(update_fields=["detail"])

    stats = rebuild_from_sources(
        threshold=float(os.getenv("KERCHUNK_THRESHOLD_SECONDS", "3")),
        exclude_talkgroup=int(os.getenv("EXCLUDE_LOCAL_TALKGROUP", "9")),
        include_archive=include_archive,
        limit_days=limit_days,
        progress=progress,
    )
    payload = stats.as_dict()
    run.detail = payload
    run.message = f"rebuilt {payload['rebuilt']} QSOs from {payload['sources']} ({payload['skipped']} skipped)"
    run.save(update_fields=["detail", "message"])
    return payload
