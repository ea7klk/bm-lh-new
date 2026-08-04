from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import html
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from .auth import session_token_hash
from .admin_routes import (
    admin_clear_qsos,
    admin_clear_irrelevant_raw_events,
    admin_clear_raw_events,
    admin_postgres,
    admin_postgres_analyze,
    admin_rebuild_qsos,
    admin_stats,
    admin_user_delete,
    admin_user_delete_page,
    admin_user_expire_sessions,
    admin_user_status,
    admin_users,
    router as admin_router,
)
from .config import settings
from .consent import cookie_consent_markup, cookie_consent_script
from .email import (
    send_email_change_email,
    send_password_reset_email,
    send_verification_email,
)
from .i18n import (
    LANGUAGE_COOKIE,
    LANGUAGE_COOKIE_MAX_AGE,
    LANGUAGE_INFO,
    normalize_locale,
    translate,
)
from .matomo import matomo_configured, matomo_script
from .public_routes import (
    active_user_talkgroups,
    health,
    public_continents,
    public_countries,
    public_grouped_callsigns,
    public_grouped_lastheard,
    public_lastheard,
    public_locale,
    public_stats,
    public_talkgroups,
    qsos,
    router as public_router,
    stats_summary,
    status,
)
from .storage import PostgresStore


UTC = timezone.utc
logger = logging.getLogger(__name__)
NIGHTLY_RAW_EVENTS_CLEANUP_HOUR = 2
TIME_RANGES = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "6h": 6 * 60 * 60,
    "12h": 12 * 60 * 60,
    "24h": 24 * 60 * 60,
    "today": 24 * 60 * 60,
    "yesterday": 24 * 60 * 60,
    "2d": 2 * 24 * 60 * 60,
    "5d": 5 * 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
    "lastWeek": 7 * 24 * 60 * 60,
    "2w": 14 * 24 * 60 * 60,
    "1M": 30 * 24 * 60 * 60,
    "lastMonth": 31 * 24 * 60 * 60,
    "2M": 60 * 24 * 60 * 60,
    "3M": 90 * 24 * 60 * 60,
}
CALENDAR_TIME_RANGES = frozenset({"today", "yesterday", "lastWeek", "lastMonth"})
AUTHENTICATED_TIME_RANGES = frozenset({"2w", "1M", "lastMonth", "2M", "3M"})

store: PostgresStore | None = None


def startup() -> None:
    get_store().initialize(settings.kerchunk_threshold_seconds)


def shutdown() -> None:
    global store
    if store is not None:
        store.close()
        store = None


def _next_nightly_raw_events_cleanup(
    now: datetime | None = None,
) -> datetime:
    """Return the next 02:00 local-time cleanup instant in UTC."""
    local_now = (now or datetime.now(tz=UTC)).astimezone(_calendar_timezone())
    target = local_now.replace(
        hour=NIGHTLY_RAW_EVENTS_CLEANUP_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_now >= target:
        target += timedelta(days=1)
    return target.astimezone(UTC)


async def _nightly_raw_events_cleanup_loop() -> None:
    """Run irrelevant raw-event cleanup once per local calendar day."""
    while True:
        target = _next_nightly_raw_events_cleanup()
        delay_seconds = max((target - datetime.now(tz=UTC)).total_seconds(), 1.0)
        logger.info(
            "scheduled irrelevant raw-event cleanup for %s",
            target.astimezone(_calendar_timezone()).isoformat(),
        )
        await asyncio.sleep(delay_seconds)
        operation = asyncio.create_task(
            asyncio.to_thread(
                get_store().clear_irrelevant_raw_events,
                settings.kerchunk_threshold_seconds,
                try_advisory_lock=True,
            )
        )
        try:
            result = await asyncio.shield(operation)
            if result.get("cleanup_skipped"):
                logger.info(
                    "irrelevant raw-event cleanup skipped; another web worker owns the run"
                )
            else:
                logger.info(
                    "nightly irrelevant raw-event cleanup completed: candidates=%d deleted=%d retained=%d",
                    result["raw_events_candidates"],
                    result["raw_events_deleted"],
                    result["raw_events_retained"],
                )
        except asyncio.CancelledError:
            # Do not close the pooled store while a database operation is still
            # running in a worker thread during application shutdown.
            with suppress(Exception):
                await operation
            raise
        except Exception:
            logger.exception("nightly irrelevant raw-event cleanup failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup()
    cleanup_task = asyncio.create_task(_nightly_raw_events_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        shutdown()


app = FastAPI(title="BrandMeister Statistics", lifespan=lifespan)
app.include_router(public_router)
app.include_router(admin_router)


def detect_locale(request: Request) -> str:
    """Match bm-lh-nextgen: cookie, query parameter, Accept-Language, English."""
    cookie_locale = normalize_locale(request.cookies.get(LANGUAGE_COOKIE))
    if cookie_locale:
        return cookie_locale
    query_locale = normalize_locale(request.query_params.get("lang"))
    if query_locale:
        return query_locale
    for language in request.headers.get("accept-language", "").split(","):
        locale = normalize_locale(language.split(";", 1)[0])
        if locale:
            return locale
    return "en"


def request_locale(request: Request) -> str:
    return getattr(request.state, "locale", None) or detect_locale(request)


@app.middleware("http")
async def language_middleware(request: Request, call_next: Any) -> Response:
    locale = detect_locale(request)
    request.state.locale = locale
    response = await call_next(request)
    if not normalize_locale(request.cookies.get(LANGUAGE_COOKIE)):
        query_locale = normalize_locale(request.query_params.get("lang"))
        if query_locale:
            response.set_cookie(
                LANGUAGE_COOKIE,
                query_locale,
                max_age=LANGUAGE_COOKIE_MAX_AGE,
                httponly=False,
                samesite="lax",
                secure=settings.cookie_secure,
            )
    response.headers["Content-Language"] = locale
    _refresh_user_session_cookie(request, response)
    return response


def get_store() -> PostgresStore:
    global store
    if store is None:
        store = PostgresStore(settings.database_url)
    return store


def _calendar_timezone() -> Any:
    try:
        return ZoneInfo(settings.app_timezone)
    except ZoneInfoNotFoundError:
        return UTC


def calendar_range_bounds(time_range: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    if time_range not in CALENDAR_TIME_RANGES:
        return None
    local_now = (now or datetime.now(tz=UTC)).astimezone(_calendar_timezone())
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if time_range == "today":
        start = today
        end = local_now
    elif time_range == "yesterday":
        start = today - timedelta(days=1)
        end = today
    elif time_range == "lastWeek":
        this_monday = today - timedelta(days=today.weekday())
        end = this_monday
        start = end - timedelta(days=7)
    else:
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def start_time(time_range: str) -> datetime:
    bounds = calendar_range_bounds(time_range)
    if bounds is not None:
        return bounds[0]
    return datetime.now(tz=UTC) - timedelta(seconds=TIME_RANGES.get(time_range, TIME_RANGES["5m"]))


def end_time(time_range: str) -> datetime | None:
    bounds = calendar_range_bounds(time_range)
    return bounds[1] if bounds is not None else None


def histogram_bucket_seconds(time_range: str) -> int:
    """Choose readable histogram bands for the selected dashboard period."""
    bounds = calendar_range_bounds(time_range)
    seconds = (
        int((bounds[1] - bounds[0]).total_seconds())
        if bounds is not None
        else TIME_RANGES.get(time_range, TIME_RANGES["5m"])
    )
    if seconds <= 15 * 60:
        return 60
    if seconds <= 2 * 60 * 60:
        return 5 * 60
    if seconds <= 12 * 60 * 60:
        return 30 * 60
    if seconds <= 24 * 60 * 60:
        return 60 * 60
    if seconds <= 7 * 24 * 60 * 60:
        return 6 * 60 * 60
    if seconds <= 31 * 24 * 60 * 60:
        return 24 * 60 * 60
    if seconds <= 62 * 24 * 60 * 60:
        return 3 * 24 * 60 * 60
    return 7 * 24 * 60 * 60


def _public_qso(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sessionId": row.get("session_id"),
        "sourceId": row.get("source_id"),
        "sourceCall": row.get("source_call"),
        "sourceName": row.get("source_name"),
        "destinationId": row.get("destination_id"),
        "destinationCall": row.get("destination_call"),
        "destinationName": row.get("destination_name"),
        "country": row.get("country"),
        "fullCountryName": row.get("full_country_name"),
        "continent": row.get("continent"),
        "contextId": row.get("context_id"),
        "linkCall": row.get("link_call"),
        "linkName": row.get("link_name"),
        "slot": row.get("slot"),
        "start": row.get("start_at"),
        "stop": row.get("stop_at"),
        "duration": (row.get("duration_ms") or 0) / 1000,
        "talkerAlias": row.get("talker_alias"),
    }


def _public_talkgroup(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "destinationId": row.get("talkgroup_id"),
        "destinationName": row.get("destination_name"),
        "country": row.get("country"),
        "fullCountryName": row.get("full_country_name"),
        "continent": row.get("continent"),
        "count": row.get("qso_count", 0),
        "totalDuration": (row.get("total_duration_ms") or 0) / 1000,
        "uniqueSources": row.get("unique_sources", 0),
        "lastSeen": row.get("last_seen_at"),
    }


def _public_callsign(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "callsign": row.get("callsign"),
        "sourceName": row.get("source_name"),
        "count": row.get("qso_count", 0),
        "totalDuration": (row.get("total_duration_ms") or 0) / 1000,
        "uniqueTalkgroups": row.get("unique_talkgroups", 0),
        "lastSeen": row.get("last_seen_at"),
    }


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _format_datetime(value: Any) -> str:
    return "—" if value is None else str(value).replace("+00:00", " UTC")


def _admin_datetime(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value).replace("+00:00", " UTC")


def _subpage_account_links(locale: str, user: dict[str, Any] | None = None) -> str:
    if user is None:
        return (
            f'<a href="/">{_escape(translate(locale, "common.dashboard"))}</a>'
            f'<a href="/user/login">{_escape(translate(locale, "home.login"))}</a>'
            f'<a href="/user/register">{_escape(translate(locale, "home.register"))}</a>'
            f'<a href="/user/profile">{_escape(translate(locale, "home.myProfile"))}</a>'
            f'<a href="/admin">{_escape(translate(locale, "home.admin"))}</a>'
        )
    return (
        f'<a href="/">{_escape(translate(locale, "common.dashboard"))}</a>'
        f'<a href="/user/profile">{_escape(user["callsign"])}</a>'
        f'<a href="/user/live-qsos">{_escape(translate(locale, "live.title"))}</a>'
        f'<a href="/user/reports">{_escape(translate(locale, "home.reports"))}</a>'
        f'<a href="/admin">{_escape(translate(locale, "home.admin"))}</a>'
        f'<form class="account-logout" method="post" action="/user/logout">'
        f'<button type="submit">{_escape(translate(locale, "user.logout"))}</button></form>'
    )


def _account_page(
    title: str,
    content: str,
    locale: str = "en",
    user: dict[str, Any] | None = None,
) -> HTMLResponse:
    return _account_page_with_metrics(title, content, locale, user=user)


def _account_page_with_metrics(
    title: str,
    content: str,
    locale: str = "en",
    records_retrieved: int = 0,
    query_seconds: float = 0.0,
    user: dict[str, Any] | None = None,
) -> HTMLResponse:
    locale = normalize_locale(locale) or "en"
    language_options = "".join(
        f'<option value="{code}"{" selected" if code == locale else ""}>{info["flag"]} {info["name"]}</option>'
        for code, info in LANGUAGE_INFO.items()
    )
    analytics_enabled = matomo_configured()
    consent_markup = (
        cookie_consent_markup(_cookie_labels(locale), analytics_enabled)
    )
    metrics = translate(locale, "common.recordsRetrieved").format(
        records=int(records_retrieved),
        seconds=f"{max(float(query_seconds), 0.0):.3f}",
    )
    return HTMLResponse(
        f"""
<!doctype html><html lang="{locale}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(title)} · BrandMeister</title>
{matomo_script()}
<style>
body{{margin:0;min-height:100vh;padding:22px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#1f2937}}
.shell{{max-width:1440px;margin:auto}}.panel,.card{{background:#fff;border-radius:14px;box-shadow:0 18px 55px #1e153a33;margin-bottom:20px}}.card{{padding:28px}}
.hero{{padding:27px 30px;text-align:center}}.hero h1{{margin:0 0 8px;font-size:clamp(26px,4vw,38px);letter-spacing:-.03em}}.hero p{{margin:0;color:#6b7280}}.live{{display:inline-flex;align-items:center;gap:7px;margin-top:14px;padding:5px 11px;border-radius:999px;background:#ecfdf3;color:#15803d;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em}}.live i{{width:8px;height:8px;background:#22c55e;border-radius:50%;box-shadow:0 0 0 4px #bbf7d0;display:inline-block}}.account-links{{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:8px;margin-top:15px;font-size:13px}}.account-links a,.account-logout button{{display:inline-block;padding:8px 12px;border:0;border-radius:8px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;font:inherit;font-weight:750;text-decoration:none;cursor:pointer}}.account-links a:hover,.account-logout button:hover{{filter:brightness(1.08)}}.account-logout{{display:inline;margin:0}}.subpage-nav{{display:flex;justify-content:flex-end;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:20px}}.subpage-nav .language{{color:#fff}}
h1,h2{{margin-top:0}}h1{{text-align:center}}.muted{{color:#6b7280}}.nav{{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:12px;flex-wrap:wrap}}
.nav a,.button{{display:inline-block;padding:9px 14px;border-radius:8px;border:0;background:linear-gradient(135deg,#667eea,#764ba2);color:white;text-decoration:none;font-weight:700;cursor:pointer}}
.nav a.secondary,.button.secondary{{background:#f1f3ff;color:#5457bd}}.language{{display:flex;align-items:center;gap:7px;color:#fff;font-size:13px;font-weight:700}}.language select{{padding:7px 9px;border:0;border-radius:7px;background:#fff;color:#374151;font:inherit}}.form{{max-width:520px;margin:auto}}label{{display:block;margin:13px 0 5px;font-size:13px;font-weight:700;color:#4b5563}}input{{width:100%;height:42px;padding:0 11px;border:2px solid #e5e7eb;border-radius:8px;box-sizing:border-box;font:inherit}}input:focus{{outline:0;border-color:#667eea}}.form .button{{margin-top:18px;width:100%}}
.error{{padding:12px;border-radius:8px;background:#fff1f2;color:#be123c;margin:0 0 15px}}.success{{padding:12px;border-radius:8px;background:#ecfdf3;color:#15803d;margin:0 0 15px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.stat{{background:#f8f9ff;border-radius:10px;padding:16px}}.stat small{{color:#6b7280;text-transform:uppercase;font-weight:800;letter-spacing:.05em}}.stat strong{{display:block;font-size:25px;margin-top:7px}}.data-quality-stats .stat{{text-align:center}}.data-quality-stats .stat strong{{font-size:clamp(14px,1.5vw,25px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.charts{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}.charts h3{{margin:0 0 10px;font-size:15px}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:11px;border-bottom:1px solid #e8eaf0;text-align:left;font-size:13px}}th{{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff}}.table-wrap{{overflow:auto;border:1px solid #e8eaf0;border-radius:9px}}.inline{{display:inline}}.danger{{background:#dc3545}}.warning{{color:#b45309;font-weight:700}}
.cookie-consent{{position:fixed;z-index:1000;left:16px;right:16px;bottom:16px;display:flex;justify-content:center}}.cookie-consent[hidden]{{display:none}}.cookie-consent-card{{max-width:760px;width:100%;padding:20px 22px;background:#fff;border:1px solid #e5e7eb;border-radius:14px;box-shadow:0 18px 55px #1e153a55}}.cookie-consent-card h2,.cookie-consent-card h3{{margin:0 0 8px}}.cookie-consent-card p{{margin:0;color:#4b5563;line-height:1.5;font-size:14px}}.cookie-consent-actions{{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-top:15px}}.cookie-consent-actions .button{{width:auto;margin:0}}.cookie-settings-link,.cookie-settings-footer{{border:0;background:none;color:#5457bd;text-decoration:underline;cursor:pointer;font:inherit;font-size:13px}}.cookie-settings{{margin-top:15px;padding-top:15px;border-top:1px solid #e5e7eb}}.cookie-option{{display:flex;align-items:flex-start;gap:9px;margin:11px 0;font-weight:400}}.cookie-option input{{width:auto;height:auto;margin-top:3px}}.cookie-option span{{display:flex;flex-direction:column;gap:3px}}.cookie-option small{{color:#6b7280;font-weight:400;line-height:1.4}}.cookie-settings-footer{{display:block;margin:24px auto 0;color:#fff}}.page-footer{{color:#fff;text-align:center;font-size:12px;line-height:1.6;padding:4px 0 8px}}.page-footer a{{color:#fff}}
@media(max-width:700px){{body{{padding:10px}}.card{{padding:20px}}.stats{{grid-template-columns:repeat(2,1fr)}}.charts{{grid-template-columns:1fr}}table{{min-width:760px}}}}
</style></head><body><main class="shell"><header class="panel hero"><h1>🔊 {_escape(translate(locale, "home.title"))}</h1><p>{_escape(translate(locale, "home.subtitle"))}</p><span class="live"><i></i> {_escape(translate(locale, "home.liveFeed"))}</span><div class="account-links">{_subpage_account_links(locale, user)}</div></header><div class="subpage-nav"><label class="language">{_escape(translate(locale, "common.language"))}<select id="language">{language_options}</select></label></div>{content}{consent_markup}<footer class="page-footer"><span>{_escape(metrics)}</span></footer></main><script>document.getElementById('language').addEventListener('change',function(){{document.cookie='{LANGUAGE_COOKIE}='+encodeURIComponent(this.value)+'; Max-Age={LANGUAGE_COOKIE_MAX_AGE}; Path=/; SameSite=Lax';window.location.reload();}});</script>{cookie_consent_script(analytics_enabled)}</body></html>
"""
    )


# Feature routers are imported after the shared application helpers and
# account layout are defined. Their modules resolve those services through this
# module at request time, preserving the existing API and test seams without
# keeping all feature code in this entrypoint.
from .dashboard_routes import (  # noqa: E402
    _cookie_labels,
    _index_path,
    about_page,
    dashboard,
    router as dashboard_router,
)
from .auth_routes import (  # noqa: E402
    _admin_allowed,
    _admin_redirect,
    _current_user,
    _dashboard_access_error,
    _form_fields,
    _password_reset_invalid_page,
    _password_reset_sent_page,
    _refresh_user_session_cookie,
    _user_redirect,
    _validation_error,
    admin_login,
    admin_login_page,
    router as auth_router,
    user_change_email,
    user_change_email_confirm,
    user_change_password,
    user_forgot_password,
    user_forgot_password_page,
    user_login,
    user_login_page,
    user_logout,
    user_profile,
    user_register,
    user_register_page,
    user_reset_password,
    user_reset_password_page,
    user_verify,
    user_api_stats,
)
from .admin_page import (  # noqa: E402
    ADMIN_RETENTION_MONTHS,
    _admin_retention_row,
    _admin_user_row,
    admin_panel_async,
    router as admin_page_router,
)
from .live_routes import (  # noqa: E402
    _live_qso_matches,
    _live_subscription,
    _live_qso_rows,
    live_qsos_data,
    live_qsos_websocket,
    router as live_router,
    user_live_qsos,
)
from .report_routes import (  # noqa: E402
    _limit_report_entries,
    _pdf_bar_chart,
    _pdf_histogram,
    _report_callsign_chart_rows,
    _report_csv,
    _report_histogram_label,
    _report_pdf,
    user_report_csv,
    user_report_excel,
    user_report_pdf,
    user_reports,
    router as report_router,
)

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(live_router)
app.include_router(report_router)
app.include_router(admin_page_router)

# Keep the most-used report helpers available from bminfo.web for callers and
# existing tests while their implementation lives in report_routes.py.
from .report_routes import (  # noqa: E402,F401
    _admin_hms,
    _admin_seconds,
    _report_bar_rows,
    _report_concurrency_chart,
    _report_duration,
    _report_excel,
    _report_histogram,
    _report_hour_rows,
    _report_page,
    _report_query,
    _report_time_range,
    _report_weekday_rows,
)

admin_panel = admin_panel_async
