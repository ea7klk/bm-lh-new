from django.http import JsonResponse
from django.shortcuts import render
from django.db import connection
from django.db.models import Count, Max, Sum
from django.utils import timezone
from datetime import timedelta

from .core_views import filtered_qsos, qso_json, require_scope
from .models import QSO, Talkgroup
from .i18n import catalog, normalize_locale
from .utils import range_bounds


def about(request):
    return render(request, "dashboard/about.html")


def locale_catalog(request, locale):
    normalized = normalize_locale(locale)
    if normalized != locale.lower().split("-", 1)[0]:
        return JsonResponse({"error": "unsupported locale"}, status=404)
    return JsonResponse(catalog(normalized))


def health(request):
    return JsonResponse({"status": "ok", "application": "django"})


def status(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        from .models import RawEvent, ServiceHeartbeat, User
        heartbeat = ServiceHeartbeat.objects.filter(service_name="collector").values_list("last_seen_at", flat=True).first()
        age = (timezone.now() - heartbeat).total_seconds() if heartbeat else None
        healthy = age is not None and age <= max(int(__import__("os").getenv("COLLECTOR_HEARTBEAT_SECONDS", "30")) * 3, 90)
        return JsonResponse({"status": "ok" if healthy else "degraded", "database": {"status": "healthy", "connection": "ok"}, "collector": {"status": "healthy" if healthy else "unhealthy", "last_seen_at": heartbeat.isoformat() if heartbeat else None, "age_seconds": age}, "tables": {"raw_events": RawEvent.objects.count(), "qsos": QSO.objects.count()}, "active_users": User.objects.filter(is_active=True).count()}, status=200 if healthy else 503)
    except Exception as exc:
        return JsonResponse({"status": "unhealthy", "database": {"status": "unhealthy", "connection": "failed"}, "error": str(exc)}, status=503)


def public_stats(request):
    from .core_views import dashboard_payload
    return dashboard_payload(request)


def lastheard(request):
    guard = require_scope(request)
    if guard is not None:
        return guard
    limit = min(max(int(request.GET.get("limit", 50)), 1), 500)
    return JsonResponse([qso_json(row) for row in filtered_qsos(request).order_by("-start_at")[:limit]], safe=False)


def grouped_lastheard(request):
    guard = require_scope(request)
    if guard is not None:
        return guard
    rows = filtered_qsos(request).values("talkgroup_id", "talkgroup__name", "talkgroup__country", "talkgroup__full_country_name", "talkgroup__continent").annotate(qso_count=Count("session_id"), duration_ms=Sum("duration_ms"), unique_sources=Count("source_call", distinct=True), last_seen=Max("start_at")).order_by("-qso_count")[:50]
    return JsonResponse([{"value": row["talkgroup_id"], "label": row["talkgroup__name"], "talkgroupId": row["talkgroup_id"], "name": row["talkgroup__name"], "country": row["talkgroup__country"], "fullCountryName": row["talkgroup__full_country_name"], "continent": row["talkgroup__continent"], "qsoCount": row["qso_count"], "durationSeconds": (row["duration_ms"] or 0) / 1000, "uniqueSources": row["unique_sources"], "lastSeen": row["last_seen"].isoformat() if row["last_seen"] else None} for row in rows], safe=False)


def grouped_callsigns(request):
    guard = require_scope(request)
    if guard is not None:
        return guard
    rows = filtered_qsos(request).values("source_call", "source_name").annotate(qso_count=Count("session_id"), duration_ms=Sum("duration_ms"), unique_talkgroups=Count("talkgroup_id", distinct=True)).order_by("-qso_count")[:50]
    return JsonResponse([{"callsign": row["source_call"], "name": row["source_name"], "qsoCount": row["qso_count"], "durationSeconds": (row["duration_ms"] or 0) / 1000, "uniqueTalkgroups": row["unique_talkgroups"]} for row in rows], safe=False)


def active_talkgroups(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication required"}, status=401)
    rows = filtered_qsos(request).values("talkgroup_id", "talkgroup__name").annotate(count=Count("session_id"), duration_ms=Sum("duration_ms")).order_by("-count")
    return JsonResponse([{"value": row["talkgroup_id"], "label": row["talkgroup__name"], "count": row["count"], "totalDuration": (row["duration_ms"] or 0) / 1000} for row in rows], safe=False)
