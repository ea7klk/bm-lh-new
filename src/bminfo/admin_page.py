"""Administrative HTML page and maintenance presentation helpers."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from . import web
from .i18n import translate


router = APIRouter()
ADMIN_RETENTION_MONTHS = (1, 2, 3, 6)
settings = web.settings


def get_store():
    return web.get_store()


def request_locale(request):
    return web.request_locale(request)


def _escape(value):
    return web._escape(value)


def _admin_allowed(request):
    return web._admin_allowed(request)


def _account_page_with_metrics(*args, **kwargs):
    return web._account_page_with_metrics(*args, **kwargs)


def _admin_datetime(value):
    return web._admin_datetime(value)


def _admin_redirect(*args, **kwargs):
    return web._admin_redirect(*args, **kwargs)


def issue_admin_token(*args, **kwargs):
    return web.issue_admin_token(*args, **kwargs)


def _format_datetime(value):
    return web._format_datetime(value)


def _admin_seconds(value):
    from .report_routes import _admin_seconds as format_seconds

    return format_seconds(value)


def _admin_hms(value):
    from .report_routes import _admin_hms as format_hms

    return format_hms(value)


def _pgadmin_link(locale: str) -> str:
    if not settings.pgadmin_url:
        return ""
    return (
        f'<a class="button secondary" href="{_escape(settings.pgadmin_url)}" '
        f'target="_blank" rel="noopener noreferrer">'
        f'{_escape(translate(locale, "admin.openPgAdmin"))}</a>'
    )


def _admin_user_row(user: dict[str, Any], locale: str = "en") -> str:
    active = bool(user["is_active"])
    status = translate(locale, "admin.active" if active else "admin.inactive")
    action = translate(locale, "admin.deactivate" if active else "admin.activate")
    active_value = "false" if active else "true"
    confirm_delete = _escape(json.dumps(translate(locale, "admin.confirmDelete")))
    confirm_expire = _escape(json.dumps(translate(locale, "admin.confirmExpireSessions")))
    return f"""
<tr><td>{_escape(user['callsign'])}</td><td>{_escape(user['name'])}</td><td>{_escape(user['email'])}</td>
<td>{status}</td><td>{_escape(user['qso_count'])}</td><td>{_escape(round(user['duration_seconds'], 1))} s</td>
<td><form class="inline" method="post" action="/admin/users/{user['id']}/status"><input type="hidden" name="active" value="{active_value}"><button class="button secondary" type="submit">{_escape(action)}</button></form>
<form class="inline" method="post" action="/admin/users/{user['id']}/expire-sessions" onsubmit="return window.confirm({confirm_expire})"><button class="button secondary" type="submit">{_escape(translate(locale, "admin.expireSessions"))}</button></form>
<form class="inline" method="post" action="/admin/users/{user['id']}/delete" onsubmit="return window.confirm({confirm_delete})"><button class="button danger" type="submit">{_escape(translate(locale, "admin.delete"))}</button></form></td></tr>
"""


def _postgres_table_rows(overview: dict[str, Any], locale: str = "en") -> str:
    rows = overview.get("tables", [])
    return "".join(
        f"<tr><td>{_escape(row['table_name'])}</td>"
        f"<td>{_escape(row['estimated_rows'])}</td>"
        f"<td>{_escape(row['total_size'])}</td></tr>"
        for row in rows
    ) or f'<tr><td colspan="3" class="muted">{_escape(translate(locale, "admin.noTables"))}</td></tr>'


def _postgres_connection_rows(overview: dict[str, Any], locale: str = "en") -> str:
    rows = overview.get("connection_states", [])
    return "".join(
        f"<tr><td>{_escape(row['state'])}</td><td>{_escape(row['count'])}</td></tr>"
        for row in rows
    ) or f'<tr><td colspan="2" class="muted">{_escape(translate(locale, "admin.noConnections"))}</td></tr>'


def _admin_retention_period(locale: str, months: int) -> str:
    unit = translate(
        locale,
        "adminMaintenance.month" if months == 1 else "adminMaintenance.months",
    )
    return f"{months} {unit}"


def _admin_maintenance_notice(request: Request, locale: str) -> str:
    notice = request.query_params.get("notice")
    if not notice:
        return ""
    try:
        if notice == "rebuild":
            message = translate(locale, "adminMaintenance.rebuildSuccess").format(
                count=int(request.query_params.get("count", "0")),
                raw=int(request.query_params.get("raw", "0")),
            )
        elif notice == "irrelevant-raw":
            message = translate(locale, "adminMaintenance.irrelevantRawSuccess").format(
                count=int(request.query_params.get("count", "0")),
                retained=int(request.query_params.get("retained", "0")),
            )
        elif notice == "raw":
            months = int(request.query_params.get("months", "0"))
            message = translate(locale, "adminMaintenance.rawDeleteSuccess").format(
                count=int(request.query_params.get("count", "0")),
                period=_admin_retention_period(locale, months),
                qsos=int(request.query_params.get("qsos", "0")),
            )
        elif notice == "qso":
            months = int(request.query_params.get("months", "0"))
            message = translate(locale, "adminMaintenance.qsoDeleteSuccess").format(
                count=int(request.query_params.get("count", "0")),
                period=_admin_retention_period(locale, months),
            )
        elif notice == "sessions":
            message = translate(locale, "admin.sessionsExpired").format(
                count=int(request.query_params.get("count", "0")),
            )
        else:
            return ""
    except (TypeError, ValueError):
        return ""
    return f'<p class="success"><strong>{_escape(translate(locale, "adminMaintenance.success"))}:</strong> {_escape(message)}</p>'


def _admin_retention_row(
    locale: str,
    kind: str,
    months: int,
    counts: dict[str, int],
) -> str:
    if kind == "raw-events":
        count = counts["raw_events"]
        detail = f'{count} raw events · {counts["dependent_qsos"]} {translate(locale, "adminMaintenance.dependentQsos")}'
        confirmation = translate(locale, "adminMaintenance.rawConfirm").format(
            raw=count,
            period=_admin_retention_period(locale, months),
            qsos=counts["dependent_qsos"],
        )
    else:
        count = counts["qsos"]
        detail = f'{count} QSOs'
        confirmation = translate(locale, "adminMaintenance.qsoConfirm").format(
            qsos=count,
            period=_admin_retention_period(locale, months),
        )
    confirm_json = _escape(json.dumps(confirmation))
    return f'''
<div class="nav" style="margin:10px 0;align-items:center"><div><strong>{_escape(translate(locale, "adminMaintenance.olderThan"))} {_escape(_admin_retention_period(locale, months))}</strong><br><span class="muted">{_escape(translate(locale, "adminMaintenance.eligible"))}: {_escape(detail)}</span></div>
<form method="post" action="/admin/maintenance/{kind}/{months}" onsubmit="return window.confirm({confirm_json})"><button class="button danger" type="submit">{_escape(translate(locale, "adminMaintenance.deleteButton"))}</button></form></div>'''


def _admin_async_retention_rows(locale: str, kind: str) -> str:
    rows = []
    for months in ADMIN_RETENTION_MONTHS:
        rows.append(
            f'<div class="nav admin-retention-row" style="margin:10px 0;align-items:center">'
            f'<div><strong>{_escape(translate(locale, "adminMaintenance.olderThan"))} '
            f'{_escape(_admin_retention_period(locale, months))}</strong><br>'
            f'<span class="muted" id="admin-{kind}-{months}-detail">'
            f'{_escape(translate(locale, "adminMaintenance.eligible"))}: …</span></div>'
            f'<form method="post" action="/admin/maintenance/{kind}/{months}" '
            f'data-retention-form data-kind="{kind}" data-months="{months}">'
            f'<button class="button danger" type="submit">'
            f'{_escape(translate(locale, "adminMaintenance.deleteButton"))}</button></form></div>'
        )
    return "".join(rows)


# Legacy synchronous renderer retained temporarily for reference; the active
# route and public helper are provided by admin_panel_async below.
def _legacy_admin_panel(request: Request) -> Response:
    locale = request_locale(request)
    if not _admin_allowed(request):
        return RedirectResponse("/admin/login", status_code=303)
    query_started = perf_counter()
    stats = get_store().admin_statistics()
    users = get_store().list_users()
    postgres = get_store().postgres_overview()
    maintenance = get_store().maintenance_overview(settings.kerchunk_threshold_seconds)
    retention = {months: get_store().retention_counts(months) for months in ADMIN_RETENTION_MONTHS}
    query_seconds = perf_counter() - query_started
    rows = "".join(_admin_user_row(user, locale) for user in users) or f'<tr><td colspan="7" class="muted">{_escape(translate(locale, "admin.registeredUsers"))}</td></tr>'
    postgres_tables = _postgres_table_rows(postgres, locale)
    postgres_connections = _postgres_connection_rows(postgres, locale)
    quality = stats["data_quality"]
    rebuild_confirmation = _escape(json.dumps(translate(locale, "adminMaintenance.rebuildConfirm")))
    irrelevant_raw_confirmation = _escape(
        json.dumps(
            translate(locale, "adminMaintenance.irrelevantRawConfirm").format(
                count=maintenance["irrelevant_raw_events"],
                threshold=settings.kerchunk_threshold_seconds,
            )
        )
    )
    maintenance_notice = _admin_maintenance_notice(request, locale)
    raw_retention_rows = "".join(
        _admin_retention_row(locale, "raw-events", months, retention[months])
        for months in ADMIN_RETENTION_MONTHS
    )
    qso_retention_rows = "".join(
        _admin_retention_row(locale, "qsos", months, retention[months])
        for months in ADMIN_RETENTION_MONTHS
    )
    content = f"""
<section class="card"><div class="nav"><h1 style="margin:0">{_escape(translate(locale, "admin.title"))}</h1><form method="post" action="/admin/logout"><button class="button secondary" type="submit">{_escape(translate(locale, "admin.logout"))}</button></form></div>
<div class="stats"><div class="stat"><small>{_escape(translate(locale, "admin.registeredUsers"))}</small><strong>{_escape(stats['total_users'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.activeUsers"))}</small><strong>{_escape(stats['active_users'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.qso24"))}</small><strong>{_escape(stats['qso_count'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.talkTime24"))}</small><strong>{_escape(_admin_hms(stats['duration_seconds']))}</strong></div></div></section>
{maintenance_notice}
<section class="card"><h2>{_escape(translate(locale, "admin.dataQuality"))}</h2><p class="muted">{_escape(translate(locale, "admin.dataQualityDescription"))}</p>
<div class="stats data-quality-stats"><div class="stat"><small>{_escape(translate(locale, "admin.rawEvents"))}</small><strong>{_escape(quality['raw_events'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.storedQsos"))}</small><strong>{_escape(quality['stored_qsos'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.displayablePercentage"))}</small><strong>{_escape(quality['displayable_qso_percentage'])}%</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.lastEvent"))}</small><strong>{_escape(_admin_datetime(quality['last_event_at']))}</strong></div></div>
<div class="table-wrap"><table><thead><tr><th>{_escape(translate(locale, "admin.dataQualityMetric"))}</th><th>{_escape(translate(locale, "admin.value"))}</th></tr></thead><tbody>
<tr><td>{_escape(translate(locale, "admin.rawVsQso"))}</td><td>{_escape(quality['raw_events'])} / {_escape(quality['stored_qsos'])}</td></tr>
<tr><td>{_escape(translate(locale, "admin.kerchunksFiltered"))}</td><td>{_escape(quality['kerchunks_filtered'])}</td></tr>
<tr><td>{_escape(translate(locale, "admin.duplicateRawEvents"))}</td><td>{_escape(quality['duplicate_raw_events'])}</td></tr>
<tr><td>{_escape(translate(locale, "admin.invalidSessionStops"))}</td><td>{_escape(quality['invalid_session_stops'])}</td></tr>
<tr><td>{_escape(translate(locale, "admin.negativeDurations"))}</td><td>{_escape(quality['negative_durations'])}</td></tr>
<tr><td>{_escape(translate(locale, "admin.longDurations"))}</td><td>{_escape(quality['unusually_long_durations'])}</td></tr>
<tr><td>{_escape(translate(locale, "admin.averageIngestionDelay"))}</td><td>{_escape(_admin_seconds(quality['average_ingestion_delay_seconds']))}</td></tr>
<tr><td>{_escape(translate(locale, "admin.p95IngestionDelay"))}</td><td>{_escape(_admin_seconds(quality['p95_ingestion_delay_seconds']))}</td></tr>
<tr><td>{_escape(translate(locale, "admin.maxIngestionDelay"))}</td><td>{_escape(_admin_seconds(quality['max_ingestion_delay_seconds']))}</td></tr>
<tr><td>{_escape(translate(locale, "admin.invalidIngestionDelays"))}</td><td>{_escape(quality['invalid_ingestion_delays'])}</td></tr>
<tr><td>{_escape(translate(locale, "admin.negativeIngestionDelays"))}</td><td>{_escape(quality['negative_ingestion_delays'])}</td></tr>
<tr><td>{_escape(translate(locale, "admin.eventLag"))}</td><td>{_escape(_admin_seconds(quality['collector_lag_seconds']))}</td></tr>
<tr><td>{_escape(translate(locale, "admin.collectorHeartbeatLag"))}</td><td>{_escape(_admin_seconds(quality['collector_heartbeat_lag_seconds']))}</td></tr>
</tbody></table></div></section>
<section class="card"><h2>{_escape(translate(locale, "admin.registeredUsers"))}</h2><p class="muted">{_escape(translate(locale, "admin.userMetrics"))}</p><div class="table-wrap"><table><thead><tr><th>{_escape(translate(locale, "user.callsign"))}</th><th>{_escape(translate(locale, "user.name"))}</th><th>{_escape(translate(locale, "user.email"))}</th><th>{_escape(translate(locale, "admin.status"))}</th><th>{_escape(translate(locale, "user.qsoCount"))}</th><th>{_escape(translate(locale, "home.talkTime"))}</th><th>{_escape(translate(locale, "admin.actions"))}</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section class="card"><div class="nav"><h2 style="margin:0">{_escape(translate(locale, "admin.postgresql"))}</h2><div>{_pgadmin_link(locale)} <form class="inline" method="post" action="/admin/postgres/analyze"><button class="button secondary" type="submit">{_escape(translate(locale, "admin.refreshPlanner"))}</button></form></div></div>
<div class="stats"><div class="stat"><small>{_escape(translate(locale, "admin.database"))}</small><strong>{_escape(postgres['database_name'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.databaseSize"))}</small><strong>{_escape(postgres['database_size'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.activeConnections"))}</small><strong>{_escape(postgres['active_connections'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "admin.totalConnections"))}</small><strong>{_escape(postgres['total_connections'])}</strong></div></div>
<p class="muted"><strong>{_escape(translate(locale, "admin.serverStarted"))}:</strong> {_escape(_format_datetime(postgres['server_started_at']))} · <strong>{_escape(translate(locale, "admin.serverTime"))}:</strong> {_escape(_format_datetime(postgres['server_time']))}</p>
<p class="muted"><strong>{_escape(translate(locale, "admin.version"))}:</strong> {_escape(postgres['version'])}</p>
<div class="charts"><div><h3>{_escape(translate(locale, "admin.applicationTables"))}</h3><div class="table-wrap"><table><thead><tr><th>{_escape(translate(locale, "admin.database"))}</th><th>{_escape(translate(locale, "admin.estimatedRows"))}</th><th>{_escape(translate(locale, "admin.totalSize"))}</th></tr></thead><tbody>{postgres_tables}</tbody></table></div></div><div><h3>{_escape(translate(locale, "admin.connectionStates"))}</h3><div class="table-wrap"><table><thead><tr><th>{_escape(translate(locale, "admin.state"))}</th><th>{_escape(translate(locale, "admin.connections"))}</th></tr></thead><tbody>{postgres_connections}</tbody></table></div></div></div></section>
<section class="card"><h2>{_escape(translate(locale, "adminMaintenance.title"))}</h2><p class="muted">{_escape(translate(locale, "adminMaintenance.description"))}</p>
<div class="card" style="background:#f8f9ff;box-shadow:none;margin:0 0 16px;padding:18px"><h3>{_escape(translate(locale, "adminMaintenance.rebuildTitle"))}</h3><p class="muted">{_escape(translate(locale, "adminMaintenance.rebuildDescription"))}</p><p><strong>{_escape(translate(locale, "adminMaintenance.currentQsos"))}:</strong> {_escape(maintenance['qsos'])} · <strong>{_escape(translate(locale, "adminMaintenance.rawEventsScanned"))}:</strong> {_escape(maintenance['raw_events'])}</p><form method="post" action="/admin/maintenance/rebuild-qsos" onsubmit="return window.confirm({rebuild_confirmation})"><button class="button" type="submit">{_escape(translate(locale, "adminMaintenance.rebuildButton"))}</button></form></div>
<div class="card" style="background:#fff7ed;box-shadow:none;margin:0 0 16px;padding:18px"><h3>{_escape(translate(locale, "adminMaintenance.irrelevantRawTitle"))}</h3><p class="muted">{_escape(translate(locale, "adminMaintenance.irrelevantRawDescription"))}</p><p><strong>{_escape(translate(locale, "adminMaintenance.irrelevantRawEligible"))}:</strong> {_escape(maintenance['irrelevant_raw_events'])}</p><form method="post" action="/admin/maintenance/irrelevant-raw-events" onsubmit="return window.confirm({irrelevant_raw_confirmation})"><button class="button danger" type="submit">{_escape(translate(locale, "adminMaintenance.deleteButton"))}</button></form></div>
<h3>{_escape(translate(locale, "adminMaintenance.rawTitle"))}</h3>{raw_retention_rows}
<h3>{_escape(translate(locale, "adminMaintenance.qsoTitle"))}</h3>{qso_retention_rows}
</section>
"""
    return _account_page_with_metrics(
        translate(locale, "admin.title"),
        content,
        locale,
        records_retrieved=len(users),
        query_seconds=query_seconds,
    )


# Async admin route implementation follows.


@router.get("/admin", response_class=HTMLResponse)
def admin_panel_async(request: Request) -> Response:
    """Return a fast admin shell; expensive sections hydrate independently."""
    locale = request_locale(request)
    if not _admin_allowed(request):
        return RedirectResponse("/admin/login", status_code=303)

    def tr(key: str) -> str:
        return translate(locale, key)

    client_text = {
        "loading": "…",
        "error": "—",
        "eligible": tr("adminMaintenance.eligible"),
        "active": tr("admin.active"),
        "inactive": tr("admin.inactive"),
        "activate": tr("admin.activate"),
        "deactivate": tr("admin.deactivate"),
        "expireSessions": tr("admin.expireSessions"),
        "delete": tr("admin.delete"),
        "confirmDelete": tr("admin.confirmDelete"),
        "confirmExpireSessions": tr("admin.confirmExpireSessions"),
        "noData": tr("admin.registeredUsers"),
        "noTables": tr("admin.noTables"),
        "noConnections": tr("admin.noConnections"),
        "rawVsQso": tr("admin.rawVsQso"),
        "kerchunksFiltered": tr("admin.kerchunksFiltered"),
        "duplicateRawEvents": tr("admin.duplicateRawEvents"),
        "invalidSessionStops": tr("admin.invalidSessionStops"),
        "negativeDurations": tr("admin.negativeDurations"),
        "longDurations": tr("admin.longDurations"),
        "averageIngestionDelay": tr("admin.averageIngestionDelay"),
        "p95IngestionDelay": tr("admin.p95IngestionDelay"),
        "maxIngestionDelay": tr("admin.maxIngestionDelay"),
        "invalidIngestionDelays": tr("admin.invalidIngestionDelays"),
        "negativeIngestionDelays": tr("admin.negativeIngestionDelays"),
        "eventLag": tr("admin.eventLag"),
        "collectorHeartbeatLag": tr("admin.collectorHeartbeatLag"),
        "rebuildConfirm": tr("adminMaintenance.rebuildConfirm"),
        "irrelevantRawConfirm": tr("adminMaintenance.irrelevantRawConfirm"),
        "rawConfirm": tr("adminMaintenance.rawConfirm"),
        "qsoConfirm": tr("adminMaintenance.qsoConfirm"),
        "dependentQsos": tr("adminMaintenance.dependentQsos"),
        "kerchunkThreshold": settings.kerchunk_threshold_seconds,
        "periods": {str(months): _admin_retention_period(locale, months) for months in ADMIN_RETENTION_MONTHS},
    }
    client_text_json = json.dumps(client_text, ensure_ascii=False).replace("</", "<\\/")
    loading = _escape(client_text["loading"])
    maintenance_notice = _admin_maintenance_notice(request, locale)
    rebuild_confirm = _escape(json.dumps(tr("adminMaintenance.rebuildConfirm")))
    irrelevant_confirm = _escape(
        json.dumps(
            tr("adminMaintenance.irrelevantRawConfirm")
            .replace("{count}", "0")
            .replace("{threshold}", str(settings.kerchunk_threshold_seconds))
        )
    )
    content = f"""
<style>
.admin-loading{{color:#9ca3af;animation:admin-pulse 1.2s ease-in-out infinite alternate}}
@keyframes admin-pulse{{from{{opacity:.45}}to{{opacity:1}}}}
.admin-retention-row{{border-bottom:1px solid #e8eaf0;padding-bottom:10px}}
</style>
<section class="card"><div class="nav"><h1 style="margin:0">{_escape(tr("admin.title"))}</h1><form method="post" action="/admin/logout"><button class="button secondary" type="submit">{_escape(tr("admin.logout"))}</button></form></div>
<div class="stats"><div class="stat"><small>{_escape(tr("admin.registeredUsers"))}</small><strong id="adminTotalUsers" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.activeUsers"))}</small><strong id="adminActiveUsers" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.qso24"))}</small><strong id="adminQso24" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.talkTime24"))}</small><strong id="adminTalkTime24" class="admin-loading">{loading}</strong></div></div></section>
{maintenance_notice}
<section class="card"><h2>{_escape(tr("admin.dataQuality"))}</h2><p class="muted">{_escape(tr("admin.dataQualityDescription"))}</p>
<div class="stats data-quality-stats"><div class="stat"><small>{_escape(tr("admin.rawEvents"))}</small><strong id="adminQualityRaw" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.storedQsos"))}</small><strong id="adminQualityQsos" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.displayablePercentage"))}</small><strong id="adminQualityPercentage" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.lastEvent"))}</small><strong id="adminQualityLastEvent" class="admin-loading">{loading}</strong></div></div>
<div class="table-wrap"><table><thead><tr><th>{_escape(tr("admin.dataQualityMetric"))}</th><th>{_escape(tr("admin.value"))}</th></tr></thead><tbody id="adminQualityRows"><tr><td colspan="2" class="muted">{loading}</td></tr></tbody></table></div></section>
<section class="card"><h2>{_escape(tr("admin.registeredUsers"))}</h2><p class="muted">{_escape(tr("admin.userMetrics"))}</p><div class="table-wrap"><table><thead><tr><th>{_escape(tr("user.callsign"))}</th><th>{_escape(tr("user.name"))}</th><th>{_escape(tr("user.email"))}</th><th>{_escape(tr("admin.status"))}</th><th>{_escape(tr("user.qsoCount"))}</th><th>{_escape(tr("home.talkTime"))}</th><th>{_escape(tr("admin.actions"))}</th></tr></thead><tbody id="adminUsersRows"><tr><td colspan="7" class="muted">{loading}</td></tr></tbody></table></div></section>
<section class="card"><div class="nav"><h2 style="margin:0">{_escape(tr("admin.postgresql"))}</h2><div>{_pgadmin_link(locale)} <form class="inline" method="post" action="/admin/postgres/analyze"><button class="button secondary" type="submit">{_escape(tr("admin.refreshPlanner"))}</button></form></div></div>
<div class="stats"><div class="stat"><small>{_escape(tr("admin.database"))}</small><strong id="adminDatabase" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.databaseSize"))}</small><strong id="adminDatabaseSize" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.activeConnections"))}</small><strong id="adminActiveConnections" class="admin-loading">{loading}</strong></div><div class="stat"><small>{_escape(tr("admin.totalConnections"))}</small><strong id="adminTotalConnections" class="admin-loading">{loading}</strong></div></div>
<p class="muted"><strong>{_escape(tr("admin.serverStarted"))}:</strong> <span id="adminServerStarted" class="admin-loading">{loading}</span> · <strong>{_escape(tr("admin.serverTime"))}:</strong> <span id="adminServerTime" class="admin-loading">{loading}</span></p>
<p class="muted"><strong>{_escape(tr("admin.version"))}:</strong> <span id="adminVersion" class="admin-loading">{loading}</span></p>
<div class="charts"><div><h3>{_escape(tr("admin.applicationTables"))}</h3><div class="table-wrap"><table><thead><tr><th>{_escape(tr("admin.database"))}</th><th>{_escape(tr("admin.estimatedRows"))}</th><th>{_escape(tr("admin.totalSize"))}</th></tr></thead><tbody id="adminPostgresTables"><tr><td colspan="3" class="muted">{loading}</td></tr></tbody></table></div></div><div><h3>{_escape(tr("admin.connectionStates"))}</h3><div class="table-wrap"><table><thead><tr><th>{_escape(tr("admin.state"))}</th><th>{_escape(tr("admin.connections"))}</th></tr></thead><tbody id="adminPostgresConnections"><tr><td colspan="2" class="muted">{loading}</td></tr></tbody></table></div></div></div></section>
<section class="card"><h2>{_escape(tr("adminMaintenance.title"))}</h2><p class="muted">{_escape(tr("adminMaintenance.description"))}</p>
<div class="card" style="background:#f8f9ff;box-shadow:none;margin:0 0 16px;padding:18px"><h3>{_escape(tr("adminMaintenance.rebuildTitle"))}</h3><p class="muted">{_escape(tr("adminMaintenance.rebuildDescription"))}</p><p><strong>{_escape(tr("adminMaintenance.currentQsos"))}:</strong> <span id="adminMaintenanceQsos" class="admin-loading">{loading}</span> · <strong>{_escape(tr("adminMaintenance.rawEventsScanned"))}:</strong> <span id="adminMaintenanceRaw" class="admin-loading">{loading}</span></p><form method="post" action="/admin/maintenance/rebuild-qsos" data-confirm="{rebuild_confirm}"><button class="button" type="submit">{_escape(tr("adminMaintenance.rebuildButton"))}</button></form></div>
<div class="card" style="background:#fff7ed;box-shadow:none;margin:0 0 16px;padding:18px"><h3>{_escape(tr("adminMaintenance.irrelevantRawTitle"))}</h3><p class="muted">{_escape(tr("adminMaintenance.irrelevantRawDescription"))}</p><p><strong>{_escape(tr("adminMaintenance.irrelevantRawEligible"))}:</strong> <span id="adminMaintenanceIrrelevant" class="admin-loading">{loading}</span></p><form method="post" action="/admin/maintenance/irrelevant-raw-events" data-confirm="{irrelevant_confirm}"><button class="button danger" type="submit">{_escape(tr("adminMaintenance.deleteButton"))}</button></form></div>
<h3>{_escape(tr("adminMaintenance.rawTitle"))}</h3><div id="adminRawRetention">{_admin_async_retention_rows(locale, "raw-events")}</div>
<h3>{_escape(tr("adminMaintenance.qsoTitle"))}</h3><div id="adminQsoRetention">{_admin_async_retention_rows(locale, "qsos")}</div>
</section>
<script>
(() => {{
  const text = {client_text_json};
  const months = {list(ADMIN_RETENTION_MONTHS)!r};
  const locale = document.documentElement.lang || "en";
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, character => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[character]));
  const setValue = (id, value) => {{ const element = document.getElementById(id); if (element) {{ element.textContent = value ?? text.error; element.classList.remove("admin-loading"); }} }};
  const dateValue = value => value ? new Date(value).toLocaleString(locale) : "—";
  const secondsValue = value => value == null ? "—" : String(Number(value).toFixed(1)) + " s";
  const hms = value => {{ let total = Math.max(0, Math.round(Number(value || 0))); const hours=Math.floor(total/3600); total%=3600; const minutes=Math.floor(total/60); const seconds=total%60; return String(hours)+":"+String(minutes).padStart(2,"0")+":"+String(seconds).padStart(2,"0"); }};
  const getJson = url => fetch(url, {{cache:"no-store", headers:{{Accept:"application/json"}}}}).then(response => {{ if (!response.ok) throw new Error(response.status); return response.json(); }});
  const showError = ids => ids.forEach(id => setValue(id, text.error));
  const replaceTemplate = (template, values) => Object.keys(values).reduce((result, key) => result.replaceAll("{{" + key + "}}", String(values[key])), template);
  const confirmForms = () => document.querySelectorAll("[data-confirm]").forEach(form => {{ if (form.dataset.confirmBound) return; form.dataset.confirmBound = "1"; form.addEventListener("submit", event => {{ if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) event.preventDefault(); }}); }});
  function renderStats(data) {{
    const quality = data.data_quality || {{}};
    setValue("adminTotalUsers", data.total_users); setValue("adminActiveUsers", data.active_users); setValue("adminQso24", data.qso_count); setValue("adminTalkTime24", hms(data.duration_seconds));
    setValue("adminQualityRaw", quality.raw_events); setValue("adminQualityQsos", quality.stored_qsos); setValue("adminQualityPercentage", String(quality.displayable_qso_percentage ?? "—") + "%"); setValue("adminQualityLastEvent", dateValue(quality.last_event_at));
    const rows = [
      [text.rawVsQso, String(quality.raw_events ?? "—") + " / " + String(quality.stored_qsos ?? "—")],
      [text.kerchunksFiltered, quality.kerchunks_filtered], [text.duplicateRawEvents, quality.duplicate_raw_events], [text.invalidSessionStops, quality.invalid_session_stops],
      [text.negativeDurations, quality.negative_durations], [text.longDurations, quality.unusually_long_durations], [text.averageIngestionDelay, secondsValue(quality.average_ingestion_delay_seconds)],
      [text.p95IngestionDelay, secondsValue(quality.p95_ingestion_delay_seconds)], [text.maxIngestionDelay, secondsValue(quality.max_ingestion_delay_seconds)],
      [text.invalidIngestionDelays, quality.invalid_ingestion_delays], [text.negativeIngestionDelays, quality.negative_ingestion_delays],
      [text.eventLag, secondsValue(quality.collector_lag_seconds)], [text.collectorHeartbeatLag, secondsValue(quality.collector_heartbeat_lag_seconds)]
    ];
    document.getElementById("adminQualityRows").innerHTML = rows.map(row => "<tr><td>" + escapeHtml(row[0]) + "</td><td>" + escapeHtml(row[1]) + "</td></tr>").join("");
  }}
  function renderUsers(users) {{
    if (!users.length) {{ document.getElementById("adminUsersRows").innerHTML = '<tr><td colspan="7" class="muted">' + escapeHtml(text.noData) + '</td></tr>'; return; }}
    document.getElementById("adminUsersRows").innerHTML = users.map(user => {{
      const active = Boolean(user.is_active); const action = active ? text.deactivate : text.activate; const status = active ? text.active : text.inactive;
      return '<tr><td>' + escapeHtml(user.callsign) + '</td><td>' + escapeHtml(user.name) + '</td><td>' + escapeHtml(user.email) + '</td><td>' + escapeHtml(status) + '</td><td>' + escapeHtml(user.qso_count) + '</td><td>' + escapeHtml(Number(user.duration_seconds || 0).toFixed(1)) + ' s</td><td>' +
        '<form class="inline" method="post" action="/admin/users/' + user.id + '/status"><input type="hidden" name="active" value="' + (active ? 'false' : 'true') + '"><button class="button secondary" type="submit">' + escapeHtml(action) + '</button></form> ' +
        '<form class="inline" method="post" action="/admin/users/' + user.id + '/expire-sessions" data-confirm="' + escapeHtml(text.confirmExpireSessions) + '"><button class="button secondary" type="submit">' + escapeHtml(text.expireSessions) + '</button></form> ' +
        '<form class="inline" method="post" action="/admin/users/' + user.id + '/delete" data-confirm="' + escapeHtml(text.confirmDelete) + '"><button class="button danger" type="submit">' + escapeHtml(text.delete) + '</button></form></td></tr>';
    }}).join("");
    confirmForms();
  }}
  function renderPostgres(data) {{
    setValue("adminDatabase", data.database_name); setValue("adminDatabaseSize", data.database_size); setValue("adminActiveConnections", data.active_connections); setValue("adminTotalConnections", data.total_connections);
    setValue("adminServerStarted", dateValue(data.server_started_at)); setValue("adminServerTime", dateValue(data.server_time)); setValue("adminVersion", data.version);
    document.getElementById("adminPostgresTables").innerHTML = (data.tables || []).map(row => '<tr><td>' + escapeHtml(row.table_name) + '</td><td>' + escapeHtml(row.estimated_rows) + '</td><td>' + escapeHtml(row.total_size) + '</td></tr>').join('') || '<tr><td colspan="3" class="muted">' + escapeHtml(text.noTables) + '</td></tr>';
    document.getElementById("adminPostgresConnections").innerHTML = (data.connection_states || []).map(row => '<tr><td>' + escapeHtml(row.state) + '</td><td>' + escapeHtml(row.count) + '</td></tr>').join('') || '<tr><td colspan="2" class="muted">' + escapeHtml(text.noConnections) + '</td></tr>';
  }}
  function renderMaintenance(data) {{
    setValue("adminMaintenanceQsos", data.qsos); setValue("adminMaintenanceRaw", data.raw_events); setValue("adminMaintenanceIrrelevant", data.irrelevant_raw_events);
    const rebuild = document.querySelector('form[action="/admin/maintenance/rebuild-qsos"]'); const irrelevant = document.querySelector('form[action="/admin/maintenance/irrelevant-raw-events"]');
    if (rebuild) rebuild.dataset.confirm = text.rebuildConfirm;
    if (irrelevant) irrelevant.dataset.confirm = replaceTemplate(text.irrelevantRawConfirm, {{count: data.irrelevant_raw_events, threshold: text.kerchunkThreshold}});
  }}
  function renderRetention(kind, months, data) {{
    const isRaw = kind === "raw-events"; const count = isRaw ? data.raw_events : data.qsos;
    const detail = isRaw ? String(data.raw_events) + " raw events · " + String(data.dependent_qsos) + " " + text.dependentQsos : String(data.qsos) + " QSOs";
    setValue("admin-" + kind + "-" + months + "-detail", text.eligible + ": " + detail);
    const form = document.querySelector('form[data-kind="' + kind + '"][data-months="' + months + '"]');
    if (form) {{ form.dataset.confirm = replaceTemplate(isRaw ? text.rawConfirm : text.qsoConfirm, {{raw: data.raw_events, qsos: data.qsos, period: text.periods[String(months)]}}); confirmForms(); }}
  }}
  getJson("/admin/stats").then(renderStats).catch(() => showError(["adminTotalUsers","adminActiveUsers","adminQso24","adminTalkTime24","adminQualityRaw","adminQualityQsos","adminQualityPercentage","adminQualityLastEvent"]));
  getJson("/admin/users").then(renderUsers).catch(() => document.getElementById("adminUsersRows").innerHTML = '<tr><td colspan="7" class="muted">' + escapeHtml(text.error) + '</td></tr>');
  getJson("/admin/postgres").then(renderPostgres).catch(() => showError(["adminDatabase","adminDatabaseSize","adminActiveConnections","adminTotalConnections","adminServerStarted","adminServerTime","adminVersion"]));
  getJson("/admin/maintenance").then(renderMaintenance).catch(() => showError(["adminMaintenanceQsos","adminMaintenanceRaw","adminMaintenanceIrrelevant"]));
  months.forEach(month => getJson("/admin/maintenance/" + month).then(data => {{ renderRetention("raw-events", month, data); renderRetention("qsos", month, data); }}).catch(() => {{ setValue("admin-raw-events-" + month + "-detail", text.error); setValue("admin-qsos-" + month + "-detail", text.error); }}));
  confirmForms();
}})();
</script>
"""
    return _account_page_with_metrics(
        tr("admin.title"),
        content,
        locale,
        records_retrieved=0,
        query_seconds=0,
    )


admin_panel = admin_panel_async
