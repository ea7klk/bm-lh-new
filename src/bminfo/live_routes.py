"""Live QSO routes and presentation helpers.

This module owns the authenticated live stream and its HTML shell. Shared
application services are resolved through bminfo.web at call time so the
legacy test seams and pooled store remain compatible.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
import json
from time import perf_counter
from typing import Any

import psycopg
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from . import web
from .i18n import catalog, translate
from .storage import QSO_NOTIFY_CHANNEL


router = APIRouter()
UTC = web.UTC
TIME_RANGES = web.TIME_RANGES
settings = web.settings


def get_store():
    return web.get_store()


def _current_user(request):
    return web._current_user(request)


def request_locale(request):
    return web.request_locale(request)


def _escape(value):
    return web._escape(value)


def start_time(value):
    return web.start_time(value)


def _public_qso(row):
    return web._public_qso(row)


def _account_page_with_metrics(*args, **kwargs):
    return web._account_page_with_metrics(*args, **kwargs)


def _report_duration(seconds: float | int) -> str:
    seconds = round(float(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    if minutes:
        return f"{minutes}:{seconds:02d}"
    return f"{seconds} s"


def _live_time_range(value: str | None) -> str:
    return value if value in TIME_RANGES else "30m"


def _relative_time(value: datetime | None, locale: str) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    elapsed = max(0, int((datetime.now(tz=UTC) - value).total_seconds()))
    if elapsed < 5:
        return translate(locale, "live.justNow")
    if elapsed < 60:
        amount, unit = elapsed, "secondsUnit"
    elif elapsed < 3600:
        amount, unit = elapsed // 60, "minutesUnit"
    elif elapsed < 86400:
        amount, unit = elapsed // 3600, "hoursUnit"
    else:
        amount, unit = elapsed // 86400, "daysUnit"
    prefix = translate(locale, "live.relativePrefix")
    suffix = translate(locale, "live.relativeSuffix")
    return f"{prefix}{amount} {translate(locale, f'live.{unit}')}{suffix}"


def _live_qso_rows(rows: list[dict[str, Any]], locale: str) -> str:
    if not rows:
        return f'<tr><td colspan="5" class="muted">{_escape(translate(locale, "live.noData"))}</td></tr>'
    return "".join(
        f'<tr><td>{_escape(_relative_time(row["start_at"], locale))}</td>'
        f'<td><span class="live-primary">{_escape(row.get("source_call") or row.get("source_id") or "—")}</span>'
        f' <span class="live-muted">{_escape(row.get("source_name") or "")}</span></td>'
        f'<td><span class="live-primary">{_escape(row.get("destination_name") or "—")}</span>'
        f' <span class="live-muted">({_escape(row.get("destination_id") or "—")})</span></td>'
        f'<td>{_escape(row.get("slot") or "—")}</td>'
        f'<td class="live-duration">{_escape(_report_duration((row.get("duration_ms") or 0) / 1000))}</td></tr>'
        for row in rows
    )


def _live_subscription(data: Any) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    raw_talkgroups = data.get("talkgroups", data.get("talkgroup", []))
    if not isinstance(raw_talkgroups, list):
        raw_talkgroups = [raw_talkgroups]
    talkgroups: set[int] = set()
    for value in raw_talkgroups:
        try:
            talkgroups.add(int(value))
        except (TypeError, ValueError):
            continue
    try:
        rows = int(data.get("rows", 25))
    except (TypeError, ValueError):
        rows = 25
    return {
        "time_range": _live_time_range(str(data.get("timeRange", "30m"))),
        "continent": str(data.get("continent") or "").strip() or None,
        "country": str(data.get("country") or "").strip() or None,
        "callsign": str(data.get("callsign") or "").strip() or None,
        "talkgroups": talkgroups,
        "rows": min(max(rows, 1), 100),
    }


def _live_qso_matches(row: dict[str, Any], subscription: dict[str, Any]) -> bool:
    start_at = row.get("start_at")
    if start_at is None:
        return False
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=UTC)
    if start_at < start_time(subscription["time_range"]):
        return False
    if subscription["continent"] and row.get("continent") != subscription["continent"]:
        return False
    if subscription["country"] and row.get("country") != subscription["country"]:
        return False
    if subscription["talkgroups"] and row.get("destination_id") not in subscription["talkgroups"]:
        return False
    callsign = subscription["callsign"]
    if callsign and callsign.casefold() not in str(row.get("source_call") or "").casefold():
        return False
    return int(row.get("duration_ms") or 0) >= round(
        settings.kerchunk_threshold_seconds * 1000
    )


async def _next_qso_notification(connection: psycopg.AsyncConnection[Any]) -> str:
    async for notification in connection.notifies():
        return notification.payload
    raise RuntimeError("QSO notification stream ended")


async def _send_live_snapshot(
    websocket: WebSocket,
    subscription: dict[str, Any],
) -> None:
    rows = get_store().list_qsos(
        subscription["rows"],
        0,
        subscription["callsign"],
        list(subscription["talkgroups"]),
        start_time(subscription["time_range"]),
        subscription["continent"],
        subscription["country"],
        settings.kerchunk_threshold_seconds,
    )
    await websocket.send_json(
        {
            "type": "snapshot",
            "rows": jsonable_encoder([_public_qso(row) for row in rows]),
        }
    )


@router.websocket("/user/live-qsos/ws")
async def live_qsos_websocket(websocket: WebSocket) -> None:
    if _current_user(websocket) is None:
        await websocket.close(code=4401, reason="authentication required")
        return

    await websocket.accept()
    receive_task: asyncio.Task[Any] | None = None
    notify_task: asyncio.Task[Any] | None = None
    try:
        async with await psycopg.AsyncConnection.connect(settings.database_url) as connection:
            await connection.set_autocommit(True)
            await connection.execute(f"LISTEN {QSO_NOTIFY_CHANNEL}")
            receive_task = asyncio.create_task(websocket.receive_json())
            notify_task = asyncio.create_task(_next_qso_notification(connection))
            subscription: dict[str, Any] | None = None
            while True:
                done, _ = await asyncio.wait(
                    {receive_task, notify_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    message = receive_task.result()
                    receive_task = asyncio.create_task(websocket.receive_json())
                    if isinstance(message, dict) and message.get("type") in {
                        "subscribe",
                        "filters",
                    }:
                        subscription = _live_subscription(message)
                        await _send_live_snapshot(websocket, subscription)
                if notify_task in done:
                    session_id = notify_task.result()
                    notify_task = asyncio.create_task(_next_qso_notification(connection))
                    if subscription is None:
                        continue
                    row = get_store().get_live_qso(session_id)
                    if row is not None and _live_qso_matches(row, subscription):
                        await websocket.send_json(
                            {
                                "type": "qso",
                                "qso": jsonable_encoder(_public_qso(row)),
                            }
                        )
    except WebSocketDisconnect:
        return
    except (OSError, psycopg.Error, RuntimeError):
        with suppress(Exception):
            await websocket.close(code=1011, reason="live stream unavailable")
    finally:
        for task in (receive_task, notify_task):
            if task is not None:
                task.cancel()
        for task in (receive_task, notify_task):
            if task is not None:
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task


@router.get("/user/live-qsos/data")
def live_qsos_data(
    request: Request,
    timeRange: str = "30m",
    continent: str | None = None,
    country: str | None = None,
    talkgroup: list[int] | None = Query(default=None),
    callsign: str | None = None,
    rows: int = Query(default=25, ge=1, le=100),
) -> Any:
    if _current_user(request) is None:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    selected_range = _live_time_range(timeRange)
    selected_continent = continent or None
    selected_country = country or None
    result = get_store().list_qsos(
        rows,
        0,
        callsign,
        talkgroup,
        start_time(selected_range),
        selected_continent,
        selected_country,
        settings.kerchunk_threshold_seconds,
    )
    return [_public_qso(row) for row in result]


@router.get("/user/live-qsos", response_class=HTMLResponse)
def user_live_qsos(
    request: Request,
    timeRange: str = "30m",
    continent: str | None = None,
    country: str | None = None,
    talkgroup: list[int] | None = Query(default=None),
    callsign: str | None = None,
    rows: int = Query(default=25, ge=1, le=100),
) -> Response:
    locale = request_locale(request)
    user = _current_user(request)
    if user is None:
        return RedirectResponse("/user/login", status_code=303)

    selected_range = _live_time_range(timeRange)
    selected_continent = continent or None
    selected_country = country or None
    selected_rows = min(max(rows, 1), 100)
    selected_talkgroups = {str(value) for value in talkgroup or []}
    query_started = perf_counter()
    active_talkgroups = (
        get_store().active_talkgroups(
            start_time(selected_range), selected_continent, selected_country
        )
        if selected_continent and selected_country
        else []
    )
    live_rows = get_store().list_qsos(
        selected_rows,
        0,
        callsign,
        talkgroup,
        start_time(selected_range),
        selected_continent,
        selected_country,
        settings.kerchunk_threshold_seconds,
    )
    query_seconds = perf_counter() - query_started
    continents = get_store().continents()
    continent_options = "".join(
        f'<option value="{_escape(value)}"{" selected" if value == selected_continent else ""}>'
        f'{_escape(catalog(locale).get("metadata", {}).get("continents", {}).get(value, value))}</option>'
        for value in continents
    )
    country_labels = catalog(locale).get("metadata", {}).get("countries", {})
    live_countries = sorted(
        get_store().countries(selected_continent),
        key=lambda row: str(country_labels.get(row["value"], row["label"])).casefold(),
    )
    country_options = "".join(
        f'<option value="{_escape(row["value"])}"{" selected" if row["value"] == selected_country else ""}>'
        f'{_escape(country_labels.get(row["value"], row["label"]))}</option>'
        for row in live_countries
    )
    talkgroup_options = "".join(
        f'<option value="{_escape(row["value"])}"'
        f'{" selected" if str(row["value"]) in selected_talkgroups else ""}>'
        f'{_escape(row["label"])} ({_escape(row["count"])} { _escape(translate(locale, "home.qsos"))})</option>'
        for row in active_talkgroups
    )
    live_texts = json.dumps(
        {
            "noData": translate(locale, "live.noData"),
            "loadError": translate(locale, "live.loadError"),
            "updated": translate(locale, "live.updated"),
            "connecting": translate(locale, "live.connecting"),
            "connected": translate(locale, "live.connected"),
            "reconnecting": translate(locale, "live.reconnecting"),
            "disconnected": translate(locale, "live.disconnected"),
            "allCountries": translate(locale, "home.allCountries"),
            "justNow": translate(locale, "live.justNow"),
            "relativePrefix": translate(locale, "live.relativePrefix"),
            "relativeSuffix": translate(locale, "live.relativeSuffix"),
            "secondsUnit": translate(locale, "live.secondsUnit"),
            "minutesUnit": translate(locale, "live.minutesUnit"),
            "hoursUnit": translate(locale, "live.hoursUnit"),
            "daysUnit": translate(locale, "live.daysUnit"),
            "countries": catalog(locale).get("metadata", {}).get("countries", {}),
        }
    )
    content = f"""
<style>
.live-controls{{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start}}
.live-controls label{{display:block;margin:0 0 5px;font-size:12px;font-weight:800;color:#6b7280;text-transform:uppercase;letter-spacing:.04em}}
.live-controls select,.live-controls input,.live-controls button{{height:40px;padding:0 10px;border:2px solid #e5e7eb;border-radius:8px;background:#fff;font:inherit}}
.live-controls input{{min-width:185px}}.live-controls select[multiple]{{height:100px;min-width:240px}}
.live-controls button{{margin-top:21px;border:0;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;font-weight:700;cursor:pointer}}
.live-toggle{{height:40px;margin-top:21px;display:flex;align-items:center;gap:8px;color:#6b7280;font-size:13px;font-weight:600}}.live-toggle input{{width:auto;height:auto;margin:0;accent-color:#667eea}}
.live-table-wrap{{max-height:62vh;overflow:auto;border:1px solid #e8eaf0;border-radius:9px}}
.live-table{{min-width:760px;width:100%;border-collapse:collapse}}
.live-table th,.live-table td{{padding:8px 10px;border-bottom:1px solid #e8eaf0;text-align:left;font-size:13px;white-space:nowrap}}
.live-table th{{position:sticky;top:0;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.live-table tbody tr:hover{{background:#faf9ff}}.live-primary{{font-weight:750;color:#5b5bd6}}.live-muted{{color:#6b7280;font-size:12px}}.live-duration{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#6b7280}}
.live-status{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin:12px 0 0;color:#6b7280;font-size:12px}}.live-status strong{{color:#15803d}}
@media(max-width:700px){{.live-controls{{flex-direction:column}}.live-controls>div,.live-controls input,.live-controls select,.live-controls button{{width:100%}}.live-controls button{{margin-top:8px}}}}
</style>
<section class="card"><div class="nav"><div><h1 style="margin:0;text-align:left">{_escape(translate(locale, "live.title"))}</h1><p class="muted">{_escape(translate(locale, "live.subtitle"))} {_escape(user["callsign"])}</p></div><div><a class="button secondary" href="/user/reports">{_escape(translate(locale, "home.reports"))}</a> <form class="inline" method="post" action="/user/logout"><button class="button secondary" type="submit">{_escape(translate(locale, "user.logout"))}</button></form></div></div>
<form id="liveForm" class="live-controls" method="get" action="/user/live-qsos">
<div><label for="liveContinent">{_escape(translate(locale, "home.continent"))}</label><select id="liveContinent" name="continent"><option value="">{_escape(translate(locale, "home.allContinents"))}</option>{continent_options}</select></div>
<div id="liveCountryControl"{"" if selected_continent else " style=\"display:none\""}><label for="liveCountry">{_escape(translate(locale, "home.country"))}</label><select id="liveCountry" name="country"><option value="">{_escape(translate(locale, "home.allCountries"))}</option>{country_options}</select></div>
<div><label for="liveCallsign">{_escape(translate(locale, "home.callsignFilter"))}</label><input id="liveCallsign" name="callsign" value="{_escape(callsign or "")}" placeholder="{_escape(translate(locale, "home.callsignPlaceholder"))}"></div>
<div id="liveTalkgroupControl"{"" if active_talkgroups else " style=\"display:none\""}><label for="liveTalkgroups">{_escape(translate(locale, "home.talkgroupFilter"))}</label><select id="liveTalkgroups" name="talkgroup" multiple>{talkgroup_options}</select></div>
<div><label for="liveRows">{_escape(translate(locale, "home.rows"))}</label><select id="liveRows" name="rows">{"".join(f'<option{" selected" if value == selected_rows else ""}>{value}</option>' for value in (10, 15, 25, 40, 50, 100))}</select></div>
</form></section>
<section class="card"><div class="live-table-wrap"><table class="live-table"><thead><tr><th>{_escape(translate(locale, "live.howLongAgo"))}</th><th>{_escape(translate(locale, "home.source"))}</th><th>{_escape(translate(locale, "home.talkgroup"))}</th><th>{_escape(translate(locale, "home.slot"))}</th><th>{_escape(translate(locale, "home.duration"))}</th></tr></thead><tbody id="liveQsoRows">{_live_qso_rows(live_rows, locale)}</tbody></table></div><div class="live-status"><span id="liveMode"><strong>{_escape(translate(locale, "live.connecting"))}</strong></span><span id="liveUpdated">—</span></div></section>
<script>
const liveTimeRange={json.dumps(selected_range)};
const liveRangeSeconds={TIME_RANGES[selected_range]};
const liveLocale={json.dumps(locale)};
const liveTexts={live_texts};
const liveForm=document.getElementById('liveForm'), liveRows=document.getElementById('liveQsoRows'), liveMode=document.getElementById('liveMode'), liveUpdated=document.getElementById('liveUpdated'), liveRowsSelect=document.getElementById('liveRows');
let liveSocket=null, liveReconnectTimer=null, liveCallsignTimer=null;
const liveEntries=new Map();
const liveEscape=value=>String(value??'').replace(/[&<>\"']/g,character=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[character]));
const relativeTime=value=>{{const elapsed=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/1000));if(elapsed<5)return liveTexts.justNow;let amount,unit;if(elapsed<60){{amount=elapsed;unit='secondsUnit';}}else if(elapsed<3600){{amount=Math.floor(elapsed/60);unit='minutesUnit';}}else if(elapsed<86400){{amount=Math.floor(elapsed/3600);unit='hoursUnit';}}else{{amount=Math.floor(elapsed/86400);unit='daysUnit';}}return `${{liveTexts.relativePrefix}}${{amount}} ${{liveTexts[unit]}}${{liveTexts.relativeSuffix}}`;}};
const liveDuration=value=>{{const seconds=Math.round(Number(value||0)),hours=Math.floor(seconds/3600),minutes=Math.floor(seconds%3600/60),rest=seconds%60;return hours?`${{hours}}:${{String(minutes).padStart(2,'0')}}:${{String(rest).padStart(2,'0')}}`:minutes?`${{minutes}}:${{String(rest).padStart(2,'0')}}`:`${{rest}} sec`;}};
const selectedTalkgroups=()=>Array.from(document.getElementById('liveTalkgroups')?.selectedOptions||[]).map(option=>Number(option.value)).filter(Number.isFinite);
const subscription=()=>({{type:'subscribe',timeRange:liveTimeRange,continent:document.getElementById('liveContinent').value||null,country:document.getElementById('liveCountry').value||null,callsign:document.getElementById('liveCallsign').value.trim()||null,talkgroups:selectedTalkgroups(),rows:Number(liveRowsSelect.value)}});
function renderLive(){{const cutoff=Date.now()-liveRangeSeconds*1000;const rows=Array.from(liveEntries.values()).filter(row=>new Date(row.start).getTime()>=cutoff).sort((a,b)=>new Date(b.start)-new Date(a.start)).slice(0,Number(liveRowsSelect.value));liveRows.innerHTML=rows.length?rows.map(row=>`<tr><td>${{liveEscape(relativeTime(row.start))}}</td><td><span class="live-primary">${{liveEscape(row.sourceCall||row.sourceId||'—')}}</span> <span class="live-muted">${{liveEscape(row.sourceName||'')}}</span></td><td><span class="live-primary">${{liveEscape(row.destinationName||'—')}}</span> <span class="live-muted">(${{liveEscape(row.destinationId||'—')}})</span></td><td>${{liveEscape(row.slot||'—')}}</td><td class="live-duration">${{liveEscape(liveDuration(row.duration))}}</td></tr>`).join(''):`<tr><td colspan="5" class="muted">${{liveEscape(liveTexts.noData)}}</td></tr>`;}}
function setLiveMode(text){{liveMode.innerHTML=`<strong>${{liveEscape(text)}}</strong>`;}}
function sendSubscription(){{if(liveSocket?.readyState===WebSocket.OPEN)liveSocket.send(JSON.stringify(subscription()));}}
function handleLiveMessage(message){{if(message.type==='snapshot'){{liveEntries.clear();(message.rows||[]).forEach(row=>liveEntries.set(row.sessionId,row));renderLive();}}else if(message.type==='qso'&&message.qso){{liveEntries.set(message.qso.sessionId,message.qso);renderLive();}}liveUpdated.textContent=`${{liveTexts.updated}} ${{new Date().toLocaleTimeString(liveLocale)}}`;}}
function connectLive(){{if(liveSocket&&liveSocket.readyState<=WebSocket.OPEN)return;setLiveMode(liveTexts.connecting);const protocol=location.protocol==='https:'?'wss':'ws';liveSocket=new WebSocket(`${{protocol}}://${{location.host}}/user/live-qsos/ws`);liveSocket.addEventListener('open',()=>{{setLiveMode(liveTexts.connected);sendSubscription();}});liveSocket.addEventListener('message',event=>{{try{{handleLiveMessage(JSON.parse(event.data));}}catch(_){{}}}});liveSocket.addEventListener('error',()=>setLiveMode(liveTexts.disconnected));liveSocket.addEventListener('close',event=>{{if(event.code===4401){{window.location='/user/login';return;}}setLiveMode(liveTexts.reconnecting);clearTimeout(liveReconnectTimer);liveReconnectTimer=setTimeout(connectLive,2000);}});}}
async function loadCountries(){{const continent=document.getElementById('liveContinent').value;const country=document.getElementById('liveCountry');const countryControl=document.getElementById('liveCountryControl');if(!continent){{countryControl.style.display='none';country.innerHTML=`<option value="">${{liveEscape(liveTexts.allCountries||'All countries')}}</option>`;document.getElementById('liveTalkgroupControl').style.display='none';document.getElementById('liveTalkgroups').innerHTML='';sendSubscription();return;}}const previous=country.value;const response=await fetch('/public/countries?continent='+encodeURIComponent(continent),{{cache:'no-store'}});const countries=await response.json();countries.sort((a,b)=>new Intl.Collator(liveLocale,{{sensitivity:'base',numeric:true}}).compare(liveTexts.countries[a.value]||a.label,liveTexts.countries[b.value]||b.label));country.innerHTML=`<option value="">${{liveEscape(liveTexts.allCountries||'All countries')}}</option>`+countries.map(item=>`<option value="${{liveEscape(item.value)}}">${{liveEscape(liveTexts.countries[item.value]||item.label)}}</option>`).join('');countryControl.style.display=countries.length?'block':'none';if(countries.some(item=>item.value===previous))country.value=previous;await loadTalkgroups();sendSubscription();}}
async function loadTalkgroups(){{const continent=document.getElementById('liveContinent').value,country=document.getElementById('liveCountry').value,control=document.getElementById('liveTalkgroupControl'),select=document.getElementById('liveTalkgroups');if(!continent||!country){{control.style.display='none';select.innerHTML='';return;}}const wanted=new Set(selectedTalkgroups().map(String));const params=new URLSearchParams({{timeRange:liveTimeRange,continent,country}});const response=await fetch('/user/talkgroups?'+params,{{cache:'no-store'}});if(!response.ok){{control.style.display='none';return;}}const groups=await response.json();select.innerHTML=groups.map(item=>`<option value="${{liveEscape(item.value)}}"${{wanted.has(String(item.value))?' selected':''}}>${{liveEscape(item.label)}} (${{liveEscape(item.count)}})</option>`).join('');control.style.display=groups.length?'block':'none';}}
document.getElementById('liveContinent').addEventListener('change',()=>loadCountries().catch(()=>setLiveMode(liveTexts.disconnected)));document.getElementById('liveCountry').addEventListener('change',()=>loadTalkgroups().then(sendSubscription).catch(()=>setLiveMode(liveTexts.disconnected)));document.getElementById('liveTalkgroups').addEventListener('change',sendSubscription);liveRowsSelect.addEventListener('change',()=>{{renderLive();sendSubscription();}});document.getElementById('liveCallsign').addEventListener('input',()=>{{clearTimeout(liveCallsignTimer);liveCallsignTimer=setTimeout(sendSubscription,300);}});liveForm.addEventListener('submit',event=>event.preventDefault());setInterval(renderLive,5000);renderLive();connectLive();
</script>
"""
    return _account_page_with_metrics(
        translate(locale, "live.title"),
        content,
        locale,
        records_retrieved=len(live_rows),
        query_seconds=query_seconds,
        user=user,
    )



