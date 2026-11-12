from __future__ import annotations

from time import perf_counter

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from .dashboard_stats import isoformat_or_none, load_dashboard_stats
from .models import QSO, Talkgroup
from .utils import histogram_bucket_seconds, histogram_label, range_bounds, restricted_request


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


def _bucket_count(start, end, bucket_seconds):
    finish = end or timezone.now()
    return max(1, min(120, int((finish - start).total_seconds() / max(bucket_seconds, 1)) + 1))


def _histogram_payload(start, bucket_seconds, count, counts_by_index):
    """Shape the histogram, filling empty buckets exactly as before."""
    payload = []
    for index in range(count):
        bucket_start = start + __import__("datetime").timedelta(seconds=index * bucket_seconds)
        payload.append({
            "bucket": bucket_start.isoformat(),
            "qso_count": counts_by_index.get(index, 0),
            "label": histogram_label(bucket_start, bucket_seconds),
        })
    return payload


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
    bucket_seconds = histogram_bucket_seconds(request.GET.get("timeRange"))
    bucket_count = _bucket_count(start, end, bucket_seconds)

    # One query: the filtered range is materialised once and every answer is
    # derived from it. See dashboard_stats for why the filter is not re-written.
    stats = load_dashboard_stats(
        qsos,
        start=start,
        bucket_seconds=bucket_seconds,
        bucket_count=bucket_count,
        row_limit=row_limit,
    )
    summary = stats["summary"]

    # Key names are the published response shape and are read directly by the
    # dashboard scripts (r.count, r.totalDuration, ...). Do not rename them.
    talkgroups = [
        {
            "count": row["qso_count"],
            "totalDuration": (row["duration_ms"] or 0) / 1000,
            "uniqueSources": row["src_count"],
            "lastSeen": isoformat_or_none(row["last_seen"]),
            "destinationId": row["tg_id"],
            "destinationName": row["tg_name"],
            "country": row["tg_country"],
            "fullCountryName": row["tg_fcc"],
            "continent": row["tg_continent"],
        }
        for row in stats["talkgroups"]
    ]
    callsigns = [
        {
            "count": row["qso_count"],
            "totalDuration": (row["duration_ms"] or 0) / 1000,
            "uniqueTalkgroups": row["tg_count"],
            "callsign": row["source_call"],
            "sourceName": row["source_name"],
        }
        for row in stats["callsigns"]
    ]
    # The two by-time lists are re-sorted views of the same top-N rows.
    callsigns_by_time = sorted(callsigns, key=lambda row: (-row["totalDuration"], row["callsign"]))
    talkgroups_by_time = sorted(talkgroups, key=lambda row: (-row["totalDuration"], row["destinationId"]))

    histogram_counts = {row["idx"]: row["n"] for row in stats["histogram"]}

    return JsonResponse({
        "totalEntries": summary.get("qso_count") or 0,
        "activityRange": summary.get("qso_count") or 0,
        "activity24h": summary.get("activity_24h") or 0,
        "uniqueCallsigns": summary.get("unique_sources") or 0,
        "uniqueTalkgroups": summary.get("unique_talkgroups") or 0,
        "totalDuration": (summary.get("duration_ms") or 0) / 1000,
        "firstQsoAt": isoformat_or_none(summary.get("first_qso_at")),
        "lastQsoAt": isoformat_or_none(summary.get("last_qso_at")),
        "talkgroups": talkgroups,
        "talkgroupsByTime": talkgroups_by_time,
        "callsigns": callsigns,
        "callsignsByTime": callsigns_by_time,
        "rows": row_limit,
        "histogram": _histogram_payload(start, bucket_seconds, bucket_count, histogram_counts),
        "recordsRetrieved": summary.get("qso_count") or 0,
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
