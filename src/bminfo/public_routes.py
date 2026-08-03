"""Public and health-related HTTP endpoints.

The route handlers deliberately resolve shared helpers from ``bminfo.web`` at
request time.  That keeps the application composition in one place while
preserving the existing service seams used by tests and runtime overrides.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


router = APIRouter()


def _web():
    from . import web

    return web


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/status")
def status() -> JSONResponse:
    """Expose database, collector, table, and active-user health metrics."""
    web = _web()
    try:
        stale_after_seconds = max(web.settings.collector_heartbeat_seconds * 3, 90)
        payload = web.get_store().status_snapshot(stale_after_seconds)
    except Exception:
        return JSONResponse(
            {
                "status": "unhealthy",
                "database": {"status": "unhealthy", "connection": "failed"},
                "collector": {"status": "unknown"},
                "tables": {"raw_events": None, "qsos": None},
                "active_users": None,
            },
            status_code=503,
        )
    return JSONResponse(
        jsonable_encoder(payload),
        status_code=200 if payload["status"] == "ok" else 503,
    )


@router.get("/locales/{locale}")
def public_locale(locale: str) -> JSONResponse:
    web = _web()
    normalized = web.normalize_locale(locale)
    if normalized not in web.SUPPORTED_LOCALES:
        return JSONResponse({"error": "unsupported locale"}, status_code=404)
    return JSONResponse(web.catalog(normalized))


@router.get("/api/stats/summary")
def stats_summary() -> dict[str, Any]:
    return _web().get_store().summary()


@router.get("/api/qsos")
def qsos(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    callsign: str | None = None,
    talkgroup: list[int] | None = Query(default=None),
) -> Any:
    web = _web()
    access_error = web._dashboard_access_error(request, callsign=callsign)
    if access_error is not None:
        return access_error
    return web.get_store().list_qsos(limit, offset, callsign, talkgroup)


@router.get("/public/stats")
def public_stats(
    request: Request,
    timeRange: str = "24h",
    continent: str | None = None,
    country: str | None = None,
    talkgroup: list[int] | None = Query(default=None),
    callsign: str | None = None,
) -> Any:
    web = _web()
    access_error = web._dashboard_access_error(
        request, time_range=timeRange, callsign=callsign
    )
    if access_error is not None:
        return access_error
    start = web.start_time(timeRange)
    end = web.end_time(timeRange)
    if end is None:
        summary = web.get_store().summary(
            start, continent, country, talkgroup, callsign
        )
        histogram = web.get_store().activity_histogram(
            start,
            web.histogram_bucket_seconds(timeRange),
            continent,
            country,
            talkgroup,
            callsign,
        )
    else:
        summary = web.get_store().summary(
            start, continent, country, talkgroup, callsign, end_time=end
        )
        histogram = web.get_store().activity_histogram(
            start,
            web.histogram_bucket_seconds(timeRange),
            continent,
            country,
            talkgroup,
            callsign,
            end_time=end,
        )
    return {
        "totalEntries": summary["qso_count"],
        "activityRange": summary["qso_count"],
        "activity24h": summary["qso_count"],
        "uniqueCallsigns": summary["unique_sources"],
        "uniqueTalkgroups": summary["unique_destinations"],
        "totalDuration": summary["duration_seconds"],
        "durationRange": summary["duration_seconds"],
        "firstQsoAt": summary["first_qso_at"],
        "lastQsoAt": summary["last_qso_at"],
        "timeRange": timeRange,
        "histogram": histogram,
    }


@router.get("/public/lastheard")
def public_lastheard(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    callsign: str | None = None,
    talkgroup: list[int] | None = Query(default=None),
) -> Any:
    web = _web()
    access_error = web._dashboard_access_error(request, callsign=callsign)
    if access_error is not None:
        return access_error
    rows = web.get_store().list_qsos(
        limit,
        0,
        callsign,
        talkgroup,
        min_duration_seconds=web.settings.kerchunk_threshold_seconds,
    )
    return [web._public_qso(row) for row in rows]


@router.get("/public/lastheard/grouped")
def public_grouped_lastheard(
    request: Request,
    timeRange: str = "5m",
    limit: int = Query(default=25, ge=1, le=50),
    continent: str | None = None,
    country: str | None = None,
    talkgroup: list[int] | None = Query(default=None),
    callsign: str | None = None,
) -> Any:
    web = _web()
    access_error = web._dashboard_access_error(
        request, time_range=timeRange, callsign=callsign
    )
    if access_error is not None:
        return access_error
    start = web.start_time(timeRange)
    end = web.end_time(timeRange)
    if end is None:
        rows = web.get_store().grouped_by_talkgroup(
            start, limit, continent, country, talkgroup, callsign
        )
    else:
        rows = web.get_store().grouped_by_talkgroup(
            start, limit, continent, country, talkgroup, callsign, end_time=end
        )
    return [web._public_talkgroup(row) for row in rows]


@router.get("/public/lastheard/callsigns")
def public_grouped_callsigns(
    request: Request,
    timeRange: str = "5m",
    limit: int = Query(default=25, ge=1, le=50),
    callsign: str | None = None,
    continent: str | None = None,
    country: str | None = None,
    talkgroup: list[int] | None = Query(default=None),
) -> Any:
    web = _web()
    access_error = web._dashboard_access_error(
        request, callsign=callsign, time_range=timeRange
    )
    if access_error is not None:
        return access_error
    start = web.start_time(timeRange)
    end = web.end_time(timeRange)
    if end is None:
        rows = web.get_store().grouped_by_callsign(
            start, limit, callsign, continent, country, talkgroup
        )
    else:
        rows = web.get_store().grouped_by_callsign(
            start, limit, callsign, continent, country, talkgroup, end_time=end
        )
    return [web._public_callsign(row) for row in rows]


@router.get("/public/continents")
def public_continents() -> list[str]:
    return _web().get_store().continents()


@router.get("/public/countries")
def public_countries(continent: str | None = None) -> list[dict[str, str]]:
    return _web().get_store().countries(continent)


@router.get("/public/talkgroups")
def public_talkgroups(
    continent: str | None = None,
    country: str | None = None,
) -> list[dict[str, Any]]:
    return _web().get_store().talkgroups(continent, country)


@router.get("/user/talkgroups")
def active_user_talkgroups(
    request: Request,
    timeRange: str = "30m",
    continent: str | None = None,
    country: str | None = None,
) -> Any:
    web = _web()
    if web._current_user(request) is None:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    access_error = web._dashboard_access_error(request, time_range=timeRange)
    if access_error is not None:
        return access_error
    start = web.start_time(timeRange)
    end = web.end_time(timeRange)
    if end is None:
        rows = web.get_store().active_talkgroups(start, continent, country)
    else:
        rows = web.get_store().active_talkgroups(
            start, continent, country, end_time=end
        )
    return [
        {
            "value": row["value"],
            "label": row["label"],
            "count": row["count"],
            "totalDuration": row["total_duration_ms"] / 1000,
        }
        for row in rows
    ]
