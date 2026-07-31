"""Admin API and maintenance endpoints.

The admin page itself remains with the page renderer, but its data and action
endpoints live here so the application entry point does not also own the
maintenance API.  Shared authentication and storage helpers are looked up
from ``bminfo.web`` when a request is handled, preserving the existing
configuration and test seams.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.responses import Response


router = APIRouter()


def _web():
    from . import web

    return web


def _authentication_required():
    return JSONResponse({"error": "admin authentication required"}, status_code=401)


@router.get("/admin/stats")
def admin_stats(request: Request) -> JSONResponse:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    return JSONResponse(jsonable_encoder(web.get_store().admin_statistics()))


@router.get("/admin/users")
def admin_users(request: Request) -> JSONResponse:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    return JSONResponse(jsonable_encoder(web.get_store().list_users()))


@router.get("/admin/postgres")
def admin_postgres(request: Request) -> JSONResponse:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    return JSONResponse(jsonable_encoder(web.get_store().postgres_overview()))


@router.post("/admin/postgres/analyze")
def admin_postgres_analyze(request: Request) -> Response:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    web.get_store().analyze_postgres()
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"analyzed": True})
    return web._admin_redirect()


@router.post("/admin/maintenance/rebuild-qsos")
def admin_rebuild_qsos(request: Request) -> Response:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    result = web.get_store().rebuild_qsos_from_raw_events(
        web.settings.kerchunk_threshold_seconds
    )
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse(jsonable_encoder(result))
    return web._admin_redirect(
        urlencode(
            {
                "notice": "rebuild",
                "count": result["qsos_rebuilt"],
                "raw": result["raw_events_scanned"],
            }
        )
    )


@router.post("/admin/maintenance/irrelevant-raw-events")
def admin_clear_irrelevant_raw_events(request: Request) -> Response:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    result = web.get_store().clear_irrelevant_raw_events()
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse(jsonable_encoder(result))
    return web._admin_redirect(
        urlencode(
            {
                "notice": "irrelevant-raw",
                "count": result["raw_events_deleted"],
                "retained": result["raw_events_retained"],
            }
        )
    )


@router.post("/admin/maintenance/raw-events/{months}")
def admin_clear_raw_events(months: int, request: Request) -> Response:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    if months not in web.ADMIN_RETENTION_MONTHS:
        return JSONResponse(
            {"error": "retention period must be 1, 2, 3, or 6 months"},
            status_code=400,
        )
    result = web.get_store().clear_old_raw_events(months)
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse(jsonable_encoder(result))
    return web._admin_redirect(
        urlencode(
            {
                "notice": "raw",
                "months": months,
                "count": result["raw_events_deleted"],
                "qsos": result["qsos_deleted"],
            }
        )
    )


@router.post("/admin/maintenance/qsos/{months}")
def admin_clear_qsos(months: int, request: Request) -> Response:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    if months not in web.ADMIN_RETENTION_MONTHS:
        return JSONResponse(
            {"error": "retention period must be 1, 2, 3, or 6 months"},
            status_code=400,
        )
    result = web.get_store().clear_old_qsos(months)
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse(jsonable_encoder(result))
    return web._admin_redirect(
        urlencode(
            {
                "notice": "qso",
                "months": months,
                "count": result["qsos_deleted"],
            }
        )
    )


@router.post("/admin/users/{user_id}/status")
async def admin_user_status(user_id: int, request: Request) -> Response:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    fields = await web._form_fields(request)
    active = fields.get("active", "false").lower() in {"1", "true", "yes", "on"}
    updated = web.get_store().set_user_active(user_id, active)
    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"updated": updated, "active": active})
    return web._admin_redirect()


@router.post("/admin/users/{user_id}/delete")
def admin_user_delete_page(user_id: int, request: Request) -> Response:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    deleted = web.get_store().delete_user(user_id)
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"deleted": deleted})
    return web._admin_redirect()


@router.post("/admin/users/{user_id}/expire-sessions")
def admin_user_expire_sessions(user_id: int, request: Request) -> Response:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    expired = web.get_store().expire_user_sessions(user_id)
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"expired": expired, "user_id": user_id})
    return web._admin_redirect(urlencode({"notice": "sessions", "count": expired}))


@router.delete("/admin/users/{user_id}")
def admin_user_delete(user_id: int, request: Request) -> JSONResponse:
    web = _web()
    if not web._admin_allowed(request):
        return _authentication_required()
    return JSONResponse({"deleted": web.get_store().delete_user(user_id)})
