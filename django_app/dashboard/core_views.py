from __future__ import annotations

from collections import defaultdict
from time import perf_counter

from django.db.models import Count, Max, Q, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from .i18n import locale_from_request
from .models import QSO, Talkgroup
from .utils import AUTHENTICATED_RANGES, histogram_bucket_seconds, histogram_label, range_bounds, restricted_request


def filtered_qsos(request, *, authenticated_only=False):
    if authenticated_only and not request.user.is_authenticated:
        return QSO.objects.none()
    start, end = range_bounds(request.GET.get("timeRange"))
    filters = Q(start_at__gte=start)
    if end is not None:
        filters &= Q(start_at__lt=end)
    callsign = request.GET.get("callsign", "").strip()
    if callsign:
        filters &= Q(source_call__icontains=callsign)
    continent = request.GET.get("continent")
    country = request.GET.get("country")
    if continent and continent != "All":
        filters &= Q(talkgroup__continent=continent)
    if country and country != "All":
        filters &= Q(talkgroup__country=country)
    selected = [value for value in request.GET.getlist("talkgroup") if value.isdigit()]
    if selected:
        filters &= Q(talkgroup_id__in=[int(value) for value in selected])
    return QSO.objects.filter(filters).select_related("talkgroup")


def require_scope(request):
    if restricted_request(request) and not request.user.is_authenticated:
        return JsonResponse({"error": "authentication required"}, status=401)
    return None


def qso_json(qso, relative=False):
    data = {
        "sessionId": qso.session_id,
        "sourceId": qso.source_id,
        "sourceCall": qso.source_call,
        "sourceName": qso.source_name,
        "destinationId": qso.talkgroup_id,
        "destinationCall": qso.destination_call,
        "destinationName": qso.destination_name or (qso.talkgroup.name if qso.talkgroup else None),
        "country": qso.talkgroup.country if qso.talkgroup else None,
        "fullCountryName": qso.talkgroup.full_country_name if qso.talkgroup else None,
        "continent": qso.talkgroup.continent if qso.talkgroup else None,
        "start": qso.start_at.isoformat() if qso.start_at else None,
        "stop": qso.stop_at.isoformat() if qso.stop_at else None,
        "duration": (qso.duration_ms or 0) / 1000,
        "slot": qso.slot,
        "timeAgo": None,
    }
    if relative:
        from .utils import relative_time
        data["timeAgo"] = relative_time(qso.stop_at or qso.start_at)
    return data


def _histogram(qsos, start, end, bucket_seconds):
    finish = end or timezone.now()
    count = max(1, min(120, int((finish - start).total_seconds() / max(bucket_seconds, 1)) + 1))
    buckets = [0] * count
    for value in qsos.values_list("start_at", flat=True):
        index = int((value - start).total_seconds() / max(bucket_seconds, 1))
        if 0 <= index < count:
            buckets[index] += 1
    return [{"bucket": (bucket_start := start + __import__("datetime").timedelta(seconds=index * bucket_seconds)).isoformat(), "qso_count": value, "label": histogram_label(bucket_start, bucket_seconds)} for index, value in enumerate(buckets)]


def dashboard_payload(request):
    started = perf_counter()
    guard = require_scope(request)
    if guard is not None:
        return guard
    try:
        row_limit = min(max(int(request.GET.get("rows", 50)), 10), 100)
    except (TypeError, ValueError):
        row_limit = 50
    qsos = filtered_qsos(request)
    start, end = range_bounds(request.GET.get("timeRange"))
    valid_callsign = ~Q(source_call__isnull=True) & ~Q(source_call__exact="") & ~Q(source_call__iexact="unknown") & ~Q(source_call__exact="—")
    callsign_qsos = qsos.filter(valid_callsign)
    summary = qsos.aggregate(qso_count=Count("session_id"), duration_ms=Sum("duration_ms"), unique_sources=Count("source_call", distinct=True, filter=valid_callsign), unique_talkgroups=Count("talkgroup_id", distinct=True), last_qso_at=Max("start_at"))
    first = qsos.order_by("start_at").values_list("start_at", flat=True).first()
    talkgroups = list(qsos.values("talkgroup_id", "talkgroup__name", "talkgroup__country", "talkgroup__full_country_name", "talkgroup__continent").annotate(count=Count("session_id"), totalDuration=Sum("duration_ms"), uniqueSources=Count("source_call", distinct=True), lastSeen=Max("start_at")).order_by("-count", "talkgroup_id")[:row_limit])
    callsigns = list(callsign_qsos.values("source_call", "source_name").annotate(count=Count("session_id"), totalDuration=Sum("duration_ms"), uniqueTalkgroups=Count("talkgroup_id", distinct=True)).order_by("-count", "source_call")[:row_limit])
    for row in talkgroups:
        row.update(destinationId=row.pop("talkgroup_id"), destinationName=row.pop("talkgroup__name"), country=row.pop("talkgroup__country"), fullCountryName=row.pop("talkgroup__full_country_name"), continent=row.pop("talkgroup__continent"), totalDuration=(row.pop("totalDuration") or 0) / 1000, lastSeen=row.pop("lastSeen").isoformat() if row.get("lastSeen") else None)
    for row in callsigns:
        row["callsign"] = row.pop("source_call")
        row["sourceName"] = row.pop("source_name")
        row["totalDuration"] = (row["totalDuration"] or 0) / 1000
    callsigns_by_time = sorted(callsigns, key=lambda row: (-row["totalDuration"], row["callsign"]))
    talkgroups_by_time = sorted(talkgroups, key=lambda row: (-row["totalDuration"], row["destinationId"]))
    return JsonResponse({
        "totalEntries": summary["qso_count"] or 0,
        "activityRange": summary["qso_count"] or 0,
        "activity24h": qsos.filter(start_at__gte=timezone.now() - __import__("datetime").timedelta(hours=24)).count(),
        "uniqueCallsigns": summary["unique_sources"] or 0,
        "uniqueTalkgroups": summary["unique_talkgroups"] or 0,
        "totalDuration": (summary["duration_ms"] or 0) / 1000,
        "firstQsoAt": first.isoformat() if first else None,
        "lastQsoAt": summary["last_qso_at"].isoformat() if summary["last_qso_at"] else None,
        "talkgroups": talkgroups,
        "talkgroupsByTime": talkgroups_by_time,
        "callsigns": callsigns,
        "callsignsByTime": callsigns_by_time,
        "rows": row_limit,
        "histogram": _histogram(qsos, start, end, histogram_bucket_seconds(request.GET.get("timeRange"))),
        "recordsRetrieved": summary["qso_count"] or 0,
        "querySeconds": round(perf_counter() - started, 4),
    })


def dashboard(request):
    return render(request, "dashboard/index.html", {"authenticated": request.user.is_authenticated, "records_retrieved": "—", "query_seconds": "—"})


def dashboard_api(request):
    return dashboard_payload(request)


def qsos_api(request):
    guard = require_scope(request)
    if guard is not None:
        return guard
    limit = min(max(int(request.GET.get("limit", 100)), 1), 500)
    offset = max(int(request.GET.get("offset", 0)), 0)
    return JsonResponse([qso_json(row) for row in filtered_qsos(request).order_by("-start_at")[offset:offset + limit]], safe=False)


def talkgroups_api(request):
    guard = require_scope(request)
    if guard is not None:
        return guard
    qsos = filtered_qsos(request)
    active = set(qsos.values_list("talkgroup_id", flat=True).distinct())
    rows = Talkgroup.objects.filter(talkgroup_id__in=active).order_by("talkgroup_id") if request.user.is_authenticated else Talkgroup.objects.all().order_by("talkgroup_id")
    return JsonResponse([{"id": row.talkgroup_id, "name": row.name, "country": row.country, "fullCountryName": row.full_country_name, "continent": row.continent} for row in rows], safe=False)


def continents_api(request):
    return JsonResponse(sorted(set(Talkgroup.objects.values_list("continent", flat=True))), safe=False)


def countries_api(request):
    rows = Talkgroup.objects.all()
    continent = request.GET.get("continent")
    if continent and continent != "All":
        rows = rows.filter(continent=continent)
    data = rows.values("country", "full_country_name").distinct().order_by("full_country_name")
    return JsonResponse([{"value": row["country"], "label": row["full_country_name"]} for row in data], safe=False)
