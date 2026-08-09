from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection, transaction
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import QSO, RawEvent, ServiceHeartbeat, Talkgroup, User, UserSession
from .qso_rules import NON_QSO_DESTINATION_IDS, raw_event_threshold_seconds


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
    return render(request, "admin/maintenance.html", {"quality": {}, "users": User.objects.only("id", "callsign", "email", "is_active").order_by("callsign")})


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
    threshold = float(__import__("os").getenv("KERCHUNK_THRESHOLD_SECONDS", "3")); exclude = int(__import__("os").getenv("EXCLUDE_LOCAL_TALKGROUP", "9")); rebuilt = 0
    with transaction.atomic():
        QSO.objects.all().delete()
        for raw in RawEvent.objects.filter(event_type__iexact="session-stop").iterator(chunk_size=2000):
            payload = raw.payload or {}
            def value(*names):
                for name in names:
                    if payload.get(name) not in (None, ""): return payload[name]
                return None
            destination = value("DestinationID", "destination_id")
            try: destination = int(destination) if destination is not None else None
            except (TypeError, ValueError): destination = None
            if not raw.start_at or not raw.stop_at or (raw.stop_at - raw.start_at).total_seconds() < threshold or destination is None or destination in NON_QSO_DESTINATION_IDS or destination == exclude or destination > 999999: continue
            tg = Talkgroup.objects.filter(talkgroup_id=destination).first(); destination_name = value("DestinationName", "destination_name") or (tg.name if tg else None)
            if not destination_name: continue
            if tg is None: tg = Talkgroup.objects.create(talkgroup_id=destination, name=str(destination_name))
            QSO.objects.update_or_create(session_id=raw.session_id, defaults={"source_id": value("SourceID", "source_id"), "source_call": value("SourceCall", "source_call"), "source_name": value("SourceName", "source_name"), "talkgroup_id": destination, "destination_call": value("DestinationCall", "destination_call"), "destination_name": destination_name, "context_id": value("ContextID", "context_id"), "link_call": value("LinkCall", "link_call"), "link_name": value("LinkName", "link_name"), "link_type_name": value("LinkTypeName", "link_type_name"), "slot": value("Slot", "slot"), "master": value("Master", "master"), "talker_alias": value("TalkerAlias", "talker_alias"), "rssi": value("RSSI", "rssi"), "ber": value("BER", "ber"), "start_at": raw.start_at, "stop_at": raw.stop_at, "duration_ms": round((raw.stop_at - raw.start_at).total_seconds() * 1000), "payload": payload})
            rebuilt += 1
    messages.success(request, f"Rebuilt {rebuilt} QSOs from raw events.")
    return redirect("admin-maintenance")


@staff_member_required
@require_http_methods(["POST"])
def clear_irrelevant_raw_events(request):
    threshold = raw_event_threshold_seconds(); candidates = []; seen = set()
    for raw in RawEvent.objects.order_by("session_id", "event_type", "-received_at").iterator(chunk_size=2000):
        key = (raw.session_id, raw.event_type.lower()); duplicate = key in seen; seen.add(key)
        below = raw.start_at and raw.stop_at and (raw.stop_at - raw.start_at).total_seconds() < threshold
        if raw.event_type.lower() != "session-stop" or below or duplicate:
            candidates.append(raw.pk)
    deleted, _ = RawEvent.objects.filter(pk__in=candidates).delete()
    with connection.cursor() as cursor:
        cursor.execute("VACUUM (ANALYZE, PARALLEL 0) raw_events")
    messages.success(request, f"Deleted {deleted} irrelevant raw events and compacted the table.")
    return redirect("admin-maintenance")


def _retention_cutoff(months):
    return timezone.now() - timedelta(days=30 * months)


@staff_member_required
@require_http_methods(["POST"])
def clear_raw_events(request, months):
    if months not in {1, 2, 3, 6}: return JsonResponse({"error": "unsupported retention period"}, status=400)
    cutoff = _retention_cutoff(months); raw_ids = list(RawEvent.objects.filter(received_at__lt=cutoff).values_list("id", flat=True)); deleted, _ = RawEvent.objects.filter(id__in=raw_ids).delete()
    with connection.cursor() as cursor: cursor.execute("VACUUM (ANALYZE, PARALLEL 0) raw_events")
    messages.success(request, f"Deleted {deleted} raw events. QSOs were left unchanged because raw events are independent."); return redirect("admin-maintenance")


@staff_member_required
def retention_counts(request, kind, months):
    if months not in {1, 2, 3, 6} or kind not in {"raw-events", "qsos"}:
        return JsonResponse({"error": "unsupported retention period"}, status=400)
    cutoff = _retention_cutoff(months)
    count = RawEvent.objects.filter(received_at__lt=cutoff).count() if kind == "raw-events" else QSO.objects.filter(start_at__lt=cutoff).count()
    return JsonResponse({"kind": kind, "months": months, "count": count})


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
