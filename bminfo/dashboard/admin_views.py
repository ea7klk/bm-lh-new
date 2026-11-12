from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import jobs
from .models import QSO, MaintenanceRun, RawEvent, RawEventArchive, ServiceHeartbeat, User, UserSession
from .qso_rules import raw_event_threshold_seconds


def _maintenance_stats():
    now = timezone.now()
    raw_threshold = raw_event_threshold_seconds()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH event_values AS (
                SELECT
                    session_id,
                    lower(event_type) AS event_type,
                    start_at,
                    stop_at,
                    received_at,
                    EXTRACT(EPOCH FROM (stop_at - start_at)) AS duration_seconds,
                    EXTRACT(EPOCH FROM (received_at - stop_at)) AS delay_seconds
                FROM raw_events
            )
            SELECT
                COUNT(*) AS raw_events,
                COUNT(*) - COUNT(DISTINCT (session_id, event_type)) AS duplicate_raw_events,
                COUNT(*) FILTER (
                    WHERE event_type = 'session-stop'
                      AND (start_at IS NULL OR stop_at IS NULL)
                ) AS invalid_session_stops,
                COUNT(*) FILTER (
                    WHERE event_type = 'session-stop'
                      AND duration_seconds < 0
                ) AS negative_durations,
                COUNT(*) FILTER (
                    WHERE event_type = 'session-stop'
                      AND duration_seconds >= 0
                      AND duration_seconds < %s
                ) AS kerchunks_filtered,
                COUNT(*) FILTER (
                    WHERE event_type = 'session-stop'
                      AND duration_seconds > 3600
                ) AS unusually_long_durations,
                AVG(delay_seconds) FILTER (
                    WHERE event_type = 'session-stop'
                      AND delay_seconds BETWEEN 0 AND 604800
                ) AS average_ingestion_delay_seconds,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY delay_seconds) FILTER (
                    WHERE event_type = 'session-stop'
                      AND delay_seconds BETWEEN 0 AND 604800
                ) AS p95_ingestion_delay_seconds,
                MAX(delay_seconds) FILTER (
                    WHERE event_type = 'session-stop'
                      AND delay_seconds BETWEEN 0 AND 604800
                ) AS maximum_ingestion_delay_seconds,
                MAX(received_at) AS last_event_at,
                (SELECT COUNT(*) FROM qsos) AS qsos
            FROM event_values
            """,
            (raw_threshold,),
        )
        row = cursor.fetchone()
    heartbeat = ServiceHeartbeat.objects.filter(service_name="collector").values_list("last_seen_at", flat=True).first()
    raw_count = row[0] or 0
    qso_count = row[10] or 0
    def rounded(value):
        return round(float(value), 2) if value is not None else None
    return {"raw_events": raw_count, "qsos": qso_count, "kerchunks_filtered": row[4] or 0, "duplicate_raw_events": row[1] or 0, "invalid_session_stops": row[2] or 0, "negative_durations": row[3] or 0, "unusually_long_durations": row[5] or 0, "average_ingestion_delay_seconds": rounded(row[6]), "p95_ingestion_delay_seconds": rounded(row[7]), "maximum_ingestion_delay_seconds": rounded(row[8]), "displayable_qso_percentage": round(qso_count * 100 / raw_count, 2) if raw_count else 0, "last_event_at": row[9], "collector_lag_seconds": round((now - heartbeat).total_seconds(), 2) if heartbeat else None, "collector_heartbeat_at": heartbeat}


@staff_member_required
def maintenance(request):
    return render(request, "admin/maintenance.html", {
        "quality": {},
        "users": User.objects.only("id", "callsign", "email", "is_active").order_by("callsign"),
        "archive_job": jobs.last_run(MaintenanceRun.ARCHIVE),
        "rebuild_job": jobs.last_run(MaintenanceRun.REBUILD),
        "compact_job": jobs.last_run(MaintenanceRun.COMPACT),
        "archived_days": RawEventArchive.objects.count(),
    })


@staff_member_required
def data_quality(request):
    return JsonResponse(_maintenance_stats(), safe=False)


@staff_member_required
@require_http_methods(["POST"])
def update_talkgroups(request):
    from .collector import sync_talkgroups

    url = __import__("os").getenv("TALKGROUPS_URL", "https://api.brandmeister.network/v2/talkgroup")
    try:
        count = sync_talkgroups(url, force=True)
    except Exception as exc:
        messages.error(request, f"Talkgroup update failed: {exc}")
    else:
        messages.success(request, f"Talkgroup list updated successfully ({count} BrandMeister records processed).")
    return redirect("admin-maintenance")


@staff_member_required
@require_http_methods(["POST"])
def rebuild_qsos(request):
    """Start a QSO rebuild from the table plus the archive, in the background."""
    run, reason = jobs.start_job(
        MaintenanceRun.REBUILD,
        jobs.rebuild_job,
        include_archive=request.POST.get("include_archive", "1") == "1",
    )
    if run is None:
        messages.error(request, f"Rebuild not started: {reason}.")
    else:
        messages.success(
            request,
            "QSO rebuild started in the background. It reads the raw events still in the "
            "database first, then the newest archive files. Progress appears below.",
        )
    return redirect("admin-maintenance")


@staff_member_required
@require_http_methods(["POST"])
def archive_now(request):
    """Start an archive pass for every day older than today 00:00 UTC."""
    run, reason = jobs.start_job(MaintenanceRun.ARCHIVE, jobs.archive_job)
    if run is None:
        messages.error(request, f"Archive not started: {reason}.")
    else:
        messages.success(request, "Archiving started in the background, oldest day first.")
    return redirect("admin-maintenance")


@staff_member_required
@require_http_methods(["POST"])
def compact_raw_events(request):
    """Run VACUUM FULL on raw_events to return disk after archiving."""
    run, reason = jobs.start_job(MaintenanceRun.COMPACT, jobs.compact_job)
    if run is None:
        messages.error(request, f"Compaction not started: {reason}.")
    else:
        messages.success(
            request,
            "Compaction started in the background. It rewrites raw_events and briefly "
            "blocks writes while it runs.",
        )
    return redirect("admin-maintenance")


@staff_member_required
def archive_status(request):
    """JSON: what is pending, what is archived, and the state of both jobs."""
    from .archiver import archive_cutoff, pending_days, relation_sizes
    from .object_store import configuration_problem

    cutoff = archive_cutoff()
    pending = pending_days()
    pending_rows = RawEvent.objects.filter(received_at__lt=cutoff).count()
    archives = list(RawEventArchive.objects.order_by("-day")[:30])
    newest = archives[0] if archives else None
    total = RawEventArchive.objects.aggregate(rows=Sum("rows"), bytes=Sum("bytes_compressed"))

    def run_state(kind):
        run = jobs.last_run(kind)
        if run is None:
            return None
        return {
            "status": run.status,
            "message": run.message,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "detail": run.detail,
            "active": jobs.active_run(kind) is not None,
        }

    return JsonResponse({
        "storage": {
            "configured": configuration_problem() is None,
            "problem": configuration_problem(),
            "bucket": settings.S3_BUCKET,
            "endpoint": settings.S3_ENDPOINT_URL,
            "prefix": settings.RAW_ARCHIVE_PREFIX,
        },
        "cutoff": cutoff.isoformat(),
        "pending_days": len(pending),
        "pending_rows": pending_rows,
        "oldest_pending": pending[0].isoformat() if pending else None,
        "archived_days": RawEventArchive.objects.count(),
        "archived_rows": total["rows"] or 0,
        "archived_bytes": total["bytes"] or 0,
        "newest_archived": newest.day.isoformat() if newest else None,
        "recent": [
            {"day": a.day.isoformat(), "rows": a.rows, "bytes": a.bytes_compressed,
             "key": a.object_key, "archived_at": a.archived_at.isoformat()}
            for a in archives[:10]
        ],
        "table": relation_sizes(),
        "archive_job": run_state(MaintenanceRun.ARCHIVE),
        "rebuild_job": run_state(MaintenanceRun.REBUILD),
        "compact_job": run_state(MaintenanceRun.COMPACT),
    })


def _retention_cutoff(months):
    return timezone.now() - timedelta(days=30 * months)


@staff_member_required
def retention_counts(request, kind, months):
    # Raw events are no longer deleted by age: they are archived to object
    # storage and then removed a day at a time. Only QSO retention remains a
    # manual action, so any other kind is rejected.
    if months not in {1, 2, 3, 6} or kind != "qsos":
        return JsonResponse({"error": "unsupported retention period"}, status=400)
    cutoff = _retention_cutoff(months)
    return JsonResponse({"kind": kind, "months": months, "count": QSO.objects.filter(start_at__lt=cutoff).count()})


@staff_member_required
@require_http_methods(["POST"])
def clear_qsos(request, months):
    if months not in {1, 2, 3, 6}: return JsonResponse({"error": "unsupported retention period"}, status=400)
    deleted, _ = QSO.objects.filter(start_at__lt=_retention_cutoff(months)).delete()
    with connection.cursor() as cursor: cursor.execute("VACUUM (ANALYZE, PARALLEL 0) qsos")
    messages.success(request, f"Deleted {deleted} QSOs and compacted the table."); return redirect("admin-maintenance")


@staff_member_required
@require_http_methods(["POST"])
def expire_sessions(request, user_id):
    user = User.objects.get(pk=user_id); UserSession.objects.filter(user=user).delete()
    from django.contrib.sessions.models import Session
    deleted = 0
    for session in Session.objects.filter(expire_date__gt=timezone.now()):
        try:
            if str(session.get_decoded().get("_auth_user_id")) == str(user.pk): session.delete(); deleted += 1
        except Exception: continue
    messages.success(request, f"Expired {deleted} sessions for {user.callsign}."); return redirect("admin-maintenance")
