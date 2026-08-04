"""Authenticated account, session, and admin-login routes."""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg.errors import UniqueViolation
from starlette.responses import Response

from . import web
from .auth import (
    hash_password,
    is_bcrypt_hash,
    issue_admin_token,
    new_email_verification_token,
    new_password_reset_token,
    new_session_token,
    session_token_hash,
    verify_admin_token,
    verify_password,
)
from .email import EmailDeliveryError
from .i18n import translate


router = APIRouter()
UTC = web.UTC
AUTHENTICATED_TIME_RANGES = web.AUTHENTICATED_TIME_RANGES
settings = web.settings


def get_store():
    return web.get_store()


def request_locale(request):
    return web.request_locale(request)


def _escape(value):
    return web._escape(value)


def _format_datetime(value):
    return web._format_datetime(value)


def _account_page(*args, **kwargs):
    return web._account_page(*args, **kwargs)


def _account_page_with_metrics(*args, **kwargs):
    return web._account_page_with_metrics(*args, **kwargs)


async def _form_fields(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
            return {str(key): str(value) for key, value in data.items()}
        except ValueError:
            return {}
    return {key: values[-1] for key, values in parse_qs(body).items()}


def _current_user_impl(request: Request) -> dict[str, Any] | None:
    request.state.user_session_checked = True
    token = request.cookies.get("session_token")
    if not token:
        request.state.user_session_valid = False
        return None
    user = get_store().user_by_session(
        session_token_hash(token), settings.session_hours * 60 * 60
    )
    request.state.user_session_valid = user is not None
    return user


def _current_user(request: Request) -> dict[str, Any] | None:
    """Resolve through web.py when callers override its compatibility seam."""
    if getattr(web, "_current_user", None) is not _current_user:
        return web._current_user(request)
    return _current_user_impl(request)


def _dashboard_access_error(
    request: Request,
    *,
    callsign: str | None = None,
    time_range: str | None = None,
) -> JSONResponse | None:
    """Require a signed-in user for callsign searches and extended ranges."""
    requires_authentication = bool(callsign and callsign.strip()) or (
        time_range in AUTHENTICATED_TIME_RANGES
    )
    if requires_authentication and _current_user(request) is None:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    return None


def _refresh_user_session_cookie(request: Request, response: Response) -> None:
    """Keep the browser cookie aligned with the sliding database expiry."""
    if not getattr(request.state, "user_session_checked", False):
        return
    token = request.cookies.get("session_token")
    if not token:
        return
    if getattr(request.state, "user_session_valid", False):
        response.set_cookie(
            "session_token",
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
        )
    else:
        response.delete_cookie("session_token")


def _user_redirect(user_id: int) -> RedirectResponse:
    token = new_session_token()
    get_store().create_session(
        user_id,
        session_token_hash(token),
        datetime.now(tz=UTC) + timedelta(hours=settings.session_hours),
    )
    response = RedirectResponse("/user/profile", status_code=303)
    response.set_cookie(
        "session_token", token, max_age=settings.session_hours * 3600,
        httponly=True, samesite="lax", secure=settings.cookie_secure,
    )
    return response


def _admin_allowed(request: Request) -> bool:
    return verify_admin_token(request.cookies.get("admin_session"), settings.admin_password)


def _admin_redirect(query: str = "") -> RedirectResponse:
    suffix = f"?{query}" if query else ""
    return RedirectResponse(f"/admin{suffix}", status_code=303)


def _validation_error(message: str, path: str) -> HTMLResponse:
    return _account_page(path, f'<div class="card"><p class="error">{_escape(message)}</p><p><a class="button" href="/user/{path.lower()}">Back</a></p></div>')


@router.get("/user/register", response_class=HTMLResponse)
def user_register_page(request: Request) -> Response:
    locale = request_locale(request)
    return _account_page(
        translate(locale, "user.register"),
        f"""
<section class="card form"><h1>{_escape(translate(locale, "user.createAccount"))}</h1><p class="muted">{_escape(translate(locale, "user.registerPrompt"))}</p>
<form method="post" action="/user/register"><label for="callsign">{_escape(translate(locale, "user.callsign"))}</label><input id="callsign" name="callsign" required maxlength="16" autocomplete="username">
<label for="name">{_escape(translate(locale, "user.name"))}</label><input id="name" name="name" required maxlength="120" autocomplete="name">
<label for="email">{_escape(translate(locale, "user.email"))}</label><input id="email" type="email" name="email" required maxlength="240" autocomplete="email">
<label for="password">{_escape(translate(locale, "user.password"))}</label><input id="password" type="password" name="password" required minlength="8" autocomplete="new-password">
<button class="button" type="submit">{_escape(translate(locale, "user.createAccountButton"))}</button></form><p class="muted">{_escape(translate(locale, "user.alreadyRegistered"))} <a href="/user/login">{_escape(translate(locale, "user.login"))}</a>.</p></section>
""",
        locale,
    )


@router.post("/user/register")
async def user_register(request: Request) -> Response:
    locale = request_locale(request)
    fields = await _form_fields(request)
    callsign = fields.get("callsign", "").strip().upper()
    name = fields.get("name", "").strip()
    email = fields.get("email", "").strip().lower()
    password = fields.get("password", "")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9/-]{1,15}", callsign):
        return _account_page(translate(locale, "user.register"), f'<section class="card"><p class="error">{_escape(translate(locale, "user.invalidCallsign"))}</p><a class="button" href="/user/register">{_escape(translate(locale, "common.dashboard"))}</a></section>', locale)
    if not name or len(name) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _account_page(translate(locale, "user.register"), f'<section class="card"><p class="error">{_escape(translate(locale, "user.invalidNameEmail"))}</p><a class="button" href="/user/register">{_escape(translate(locale, "common.dashboard"))}</a></section>', locale)
    if len(password) < 8:
        return _account_page(translate(locale, "user.register"), f'<section class="card"><p class="error">{_escape(translate(locale, "user.passwordMin"))}</p><a class="button" href="/user/register">{_escape(translate(locale, "common.dashboard"))}</a></section>', locale)
    try:
        user = get_store().create_user(callsign, name, email, hash_password(password))
    except UniqueViolation:
        return _account_page(translate(locale, "user.register"), f'<section class="card"><p class="error">{_escape(translate(locale, "user.duplicate"))}</p><a class="button" href="/user/register">{_escape(translate(locale, "common.dashboard"))}</a></section>', locale)
    token = new_email_verification_token()
    try:
        get_store().create_email_verification(
            user["id"],
            session_token_hash(token),
            datetime.now(tz=UTC) + timedelta(hours=settings.email_verification_hours),
        )
        web.send_verification_email(email, callsign, locale, token)
    except EmailDeliveryError:
        get_store().delete_user(user["id"])
        return _account_page(
            translate(locale, "user.register"),
            f'<section class="card"><h1>{_escape(translate(locale, "emailVerification.invalidTitle"))}</h1><p class="error">{_escape(translate(locale, "emailVerification.deliveryFailed"))}</p><a class="button" href="/user/register">{_escape(translate(locale, "user.register"))}</a></section>',
            locale,
        )
    return _account_page(
        translate(locale, "emailVerification.sentTitle"),
        f'<section class="card form"><h1>{_escape(translate(locale, "emailVerification.sentTitle"))}</h1><p class="success">{_escape(translate(locale, "emailVerification.sent"))}</p><a class="button" href="/user/login">{_escape(translate(locale, "emailVerification.login"))}</a></section>',
        locale,
    )


@router.get("/user/verify", response_class=HTMLResponse)
def user_verify(request: Request, token: str = "") -> Response:
    locale = request_locale(request)
    user = get_store().verify_email_token(session_token_hash(token)) if token else None
    if user is None:
        return _account_page(
            translate(locale, "emailVerification.invalidTitle"),
            f'<section class="card form"><h1>{_escape(translate(locale, "emailVerification.invalidTitle"))}</h1><p class="error">{_escape(translate(locale, "emailVerification.invalid"))}</p><a class="button" href="/user/register">{_escape(translate(locale, "user.register"))}</a></section>',
            locale,
        )
    return _account_page(
        translate(locale, "emailVerification.successTitle"),
        f'<section class="card form"><h1>{_escape(translate(locale, "emailVerification.successTitle"))}</h1><p class="success">{_escape(translate(locale, "emailVerification.success"))}</p><a class="button" href="/user/login">{_escape(translate(locale, "emailVerification.login"))}</a></section>',
        locale,
    )


def _password_reset_invalid_page(locale: str) -> HTMLResponse:
    return _account_page(
        translate(locale, "user.resetInvalidTitle"),
        f'<section class="card form"><h1>{_escape(translate(locale, "user.resetInvalidTitle"))}</h1><p class="error">{_escape(translate(locale, "user.resetInvalid"))}</p><a class="button" href="/user/forgot-password">{_escape(translate(locale, "user.forgotPassword"))}</a></section>',
        locale,
    )


def _password_reset_sent_page(locale: str) -> HTMLResponse:
    return _account_page(
        translate(locale, "user.resetEmailSentTitle"),
        f'<section class="card form"><h1>{_escape(translate(locale, "user.resetEmailSentTitle"))}</h1><p class="success">{_escape(translate(locale, "user.resetEmailSent"))}</p><a class="button" href="/user/login">{_escape(translate(locale, "user.passwordResetLogin"))}</a></section>',
        locale,
    )


@router.get("/user/forgot-password", response_class=HTMLResponse)
def user_forgot_password_page(request: Request) -> HTMLResponse:
    locale = request_locale(request)
    return _account_page(
        translate(locale, "user.forgotPasswordTitle"),
        f'<section class="card form"><h1>{_escape(translate(locale, "user.forgotPasswordTitle"))}</h1><p class="muted">{_escape(translate(locale, "user.forgotPasswordPrompt"))}</p><form method="post" action="/user/forgot-password"><label for="email">{_escape(translate(locale, "user.email"))}</label><input id="email" type="email" name="email" required maxlength="240" autocomplete="email"><button class="button" type="submit">{_escape(translate(locale, "user.sendResetLink"))}</button></form><p class="muted"><a href="/user/login">{_escape(translate(locale, "user.login"))}</a></p></section>',
        locale,
    )


@router.post("/user/forgot-password")
async def user_forgot_password(request: Request) -> Response:
    locale = request_locale(request)
    fields = await _form_fields(request)
    email = fields.get("email", "").strip().lower()
    user = get_store().user_by_email(email) if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else None
    if user is not None:
        token = new_password_reset_token()
        get_store().create_password_reset(
            user["id"],
            session_token_hash(token),
            datetime.now(tz=UTC) + timedelta(hours=settings.password_reset_hours),
        )
        try:
            web.send_password_reset_email(user["email"], user["callsign"], locale, token)
        except EmailDeliveryError:
            return _account_page(
                translate(locale, "user.forgotPasswordTitle"),
                f'<section class="card form"><h1>{_escape(translate(locale, "user.forgotPasswordTitle"))}</h1><p class="error">{_escape(translate(locale, "user.passwordResetDeliveryFailed"))}</p><a class="button" href="/user/forgot-password">{_escape(translate(locale, "user.forgotPassword"))}</a></section>',
                locale,
            )
    return _password_reset_sent_page(locale)


@router.get("/user/reset-password", response_class=HTMLResponse)
def user_reset_password_page(request: Request, token: str = "") -> Response:
    locale = request_locale(request)
    token_hash = session_token_hash(token) if token else ""
    if not token_hash or get_store().password_reset_user(token_hash) is None:
        return _password_reset_invalid_page(locale)
    return _account_page(
        translate(locale, "user.resetPasswordTitle"),
        f'<section class="card form"><h1>{_escape(translate(locale, "user.resetPasswordTitle"))}</h1><p class="muted">{_escape(translate(locale, "user.resetPasswordPrompt"))}</p><form method="post" action="/user/reset-password"><input type="hidden" name="token" value="{_escape(token)}"><label for="password">{_escape(translate(locale, "user.newPassword"))}</label><input id="password" type="password" name="password" minlength="8" required autocomplete="new-password"><label for="confirm_password">{_escape(translate(locale, "user.confirmPassword"))}</label><input id="confirm_password" type="password" name="confirm_password" minlength="8" required autocomplete="new-password"><button class="button" type="submit">{_escape(translate(locale, "user.resetPasswordButton"))}</button></form></section>',
        locale,
    )


@router.post("/user/reset-password")
async def user_reset_password(request: Request) -> Response:
    locale = request_locale(request)
    fields = await _form_fields(request)
    token = fields.get("token", "")
    password = fields.get("password", "")
    if len(password) < 8:
        return _account_page(
            translate(locale, "user.resetPasswordTitle"),
            f'<section class="card form"><h1>{_escape(translate(locale, "user.resetPasswordTitle"))}</h1><p class="error">{_escape(translate(locale, "user.passwordMin"))}</p><a class="button" href="/user/reset-password?token={_escape(quote(token))}">{_escape(translate(locale, "user.resetPasswordTitle"))}</a></section>',
            locale,
        )
    if password != fields.get("confirm_password", ""):
        return _account_page(
            translate(locale, "user.resetPasswordTitle"),
            f'<section class="card form"><h1>{_escape(translate(locale, "user.resetPasswordTitle"))}</h1><p class="error">{_escape(translate(locale, "user.passwordMismatch"))}</p><a class="button" href="/user/reset-password?token={_escape(quote(token))}">{_escape(translate(locale, "user.resetPasswordTitle"))}</a></section>',
            locale,
        )
    user = get_store().reset_password(session_token_hash(token), hash_password(password)) if token else None
    if user is None:
        return _password_reset_invalid_page(locale)
    return _account_page(
        translate(locale, "user.resetPasswordTitle"),
        f'<section class="card form"><h1>{_escape(translate(locale, "user.resetPasswordTitle"))}</h1><p class="success">{_escape(translate(locale, "user.passwordResetSuccess"))}</p><a class="button" href="/user/login">{_escape(translate(locale, "user.passwordResetLogin"))}</a></section>',
        locale,
    )


@router.get("/user/login", response_class=HTMLResponse)
def user_login_page(request: Request) -> HTMLResponse:
    locale = request_locale(request)
    return _account_page(
        translate(locale, "user.loginTitle"),
        f"""
<section class="card form"><h1>{_escape(translate(locale, "user.loginTitle"))}</h1><p class="muted">{_escape(translate(locale, "user.loginPrompt"))}</p>
<form method="post" action="/user/login"><label for="login">{_escape(translate(locale, "user.emailOrCallsign"))}</label><input id="login" name="login" required autocomplete="username">
<label for="password">{_escape(translate(locale, "user.password"))}</label><input id="password" type="password" name="password" required autocomplete="current-password">
<button class="button" type="submit">{_escape(translate(locale, "user.loginButton"))}</button></form><p class="muted"><a href="/user/forgot-password">{_escape(translate(locale, "user.forgotPassword"))}</a></p><p class="muted">{_escape(translate(locale, "user.needAccount"))} <a href="/user/register">{_escape(translate(locale, "user.register"))}</a>.</p></section>
""",
        locale,
    )


@router.post("/user/login")
async def user_login(request: Request) -> Response:
    locale = request_locale(request)
    fields = await _form_fields(request)
    login = (fields.get("login") or fields.get("email") or fields.get("callsign") or "").strip()
    user = get_store().user_by_login(login)
    if user is None or not verify_password(fields.get("password", ""), user["password_hash"]):
        return _account_page(translate(locale, "user.loginTitle"), f'<section class="card"><p class="error">{_escape(translate(locale, "user.invalidCredentials"))}</p><p class="muted">{_escape(translate(locale, "user.accountMayNeedRegistration"))} <a href="/user/register">{_escape(translate(locale, "user.register"))}</a></p><p><a class="button" href="/user/forgot-password">{_escape(translate(locale, "user.forgotPassword"))}</a></p><a class="button secondary" href="/user/login">{_escape(translate(locale, "user.loginButton"))}</a></section>', locale)
    if not user["is_active"]:
        return _account_page(translate(locale, "user.loginTitle"), f'<section class="card"><p class="error">{_escape(translate(locale, "emailVerification.notVerified"))}</p><a class="button" href="/user/login">{_escape(translate(locale, "user.loginButton"))}</a></section>', locale)
    get_store().mark_user_login(user["id"])
    if is_bcrypt_hash(user["password_hash"]):
        get_store().update_user_password(user["id"], hash_password(fields.get("password", "")))
    return _user_redirect(user["id"])


@router.post("/user/logout")
def user_logout(request: Request) -> RedirectResponse:
    token = request.cookies.get("session_token")
    if token:
        get_store().delete_session(session_token_hash(token))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("session_token")
    return response


@router.get("/user/profile", response_class=HTMLResponse)
def user_profile(request: Request) -> Response:
    locale = request_locale(request)
    user = _current_user(request)
    if user is None:
        return RedirectResponse("/user/login", status_code=303)
    query_started = perf_counter()
    stats = get_store().user_statistics(user["callsign"])
    query_seconds = perf_counter() - query_started
    top_rows = "".join(
        f"<tr><td>{_escape(row['name'])}</td><td>{_escape(row['talkgroup_id'])}</td><td>{_escape(row['qso_count'])}</td><td>{_escape(round(row['duration_seconds'], 1))} s</td></tr>"
        for row in stats["top_talkgroups"]
    ) or f'<tr><td colspan="4" class="muted">{_escape(translate(locale, "user.noQsos"))}</td></tr>'
    content = f"""
<section class="card"><div class="nav"><h1 style="margin:0">{_escape(user['callsign'])}</h1><div><a class="button secondary" href="/user/live-qsos">{_escape(translate(locale, "live.title"))}</a> <a class="button secondary" href="/user/reports">{_escape(translate(locale, "home.reports"))}</a> <form class="inline" method="post" action="/user/logout"><button class="button secondary" type="submit">{_escape(translate(locale, "user.logout"))}</button></form></div></div><p class="muted">{_escape(user['name'])} · {_escape(user['email'])}</p>
<div class="stats"><div class="stat"><small>{_escape(translate(locale, "user.qsoCount"))}</small><strong>{_escape(stats['qso_count'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "home.talkTime"))}</small><strong>{_escape(round(stats['duration_seconds'],1))} s</strong></div><div class="stat"><small>{_escape(translate(locale, "user.uniqueTalkgroups"))}</small><strong>{_escape(stats['unique_talkgroups'])}</strong></div><div class="stat"><small>{_escape(translate(locale, "home.lastHeard"))}</small><strong>{_escape(_format_datetime(stats['last_qso_at']))}</strong></div></div></section>
<section class="card"><h2>{_escape(translate(locale, "user.topTalkgroups"))}</h2><div class="table-wrap"><table><thead><tr><th>{_escape(translate(locale, "home.talkgroup"))}</th><th>{_escape(translate(locale, "user.id"))}</th><th>{_escape(translate(locale, "user.qsoCount"))}</th><th>{_escape(translate(locale, "home.talkTime"))}</th></tr></thead><tbody>{top_rows}</tbody></table></div></section>
<section class="card form"><h2>{_escape(translate(locale, "user.changePassword"))}</h2><form method="post" action="/user/change-password"><label>{_escape(translate(locale, "user.currentPassword"))}</label><input type="password" name="current_password" required><label>{_escape(translate(locale, "user.newPassword"))}</label><input type="password" name="new_password" minlength="8" required><button class="button" type="submit">{_escape(translate(locale, "user.changePasswordButton"))}</button></form></section>
<section class="card form"><h2>{_escape(translate(locale, "user.changeEmail"))}</h2><p class="muted">{_escape(translate(locale, "user.currentEmail"))}: {_escape(user['email'])}</p><form method="post" action="/user/change-email"><label for="new_email">{_escape(translate(locale, "user.newEmail"))}</label><input id="new_email" type="email" name="new_email" required maxlength="240" autocomplete="email"><button class="button" type="submit">{_escape(translate(locale, "user.changeEmailButton"))}</button></form></section>
"""
    return _account_page_with_metrics(
        translate(locale, "user.profile"),
        content,
        locale,
        records_retrieved=stats["qso_count"],
        query_seconds=query_seconds,
        user=user,
    )


@router.get("/user/api/stats")
def user_api_stats(request: Request) -> JSONResponse:
    user = _current_user(request)
    if user is None:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    return JSONResponse(jsonable_encoder(get_store().user_statistics(user["callsign"])))


@router.post("/user/change-password")
async def user_change_password(request: Request) -> Response:
    locale = request_locale(request)
    user = _current_user(request)
    if user is None:
        return RedirectResponse("/user/login", status_code=303)
    fields = await _form_fields(request)
    if not verify_password(fields.get("current_password", ""), user["password_hash"]):
        return _account_page(translate(locale, "user.profile"), f'<section class="card"><p class="error">{_escape(translate(locale, "user.currentPasswordIncorrect"))}</p><a class="button" href="/user/profile">{_escape(translate(locale, "user.profile"))}</a></section>', locale, user=user)
    if len(fields.get("new_password", "")) < 8:
        return _account_page(translate(locale, "user.profile"), f'<section class="card"><p class="error">{_escape(translate(locale, "user.newPasswordMin"))}</p><a class="button" href="/user/profile">{_escape(translate(locale, "user.profile"))}</a></section>', locale, user=user)
    get_store().update_user_password(user["id"], hash_password(fields["new_password"]))
    return RedirectResponse("/user/profile", status_code=303)


@router.post("/user/change-email")
async def user_change_email(request: Request) -> Response:
    locale = request_locale(request)
    user = _current_user(request)
    if user is None:
        return RedirectResponse("/user/login", status_code=303)
    fields = await _form_fields(request)
    new_email = fields.get("new_email", "").strip().lower()
    profile_link = f'<a class="button" href="/user/profile">{_escape(translate(locale, "user.profile"))}</a>'
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new_email):
        return _account_page(
            translate(locale, "user.changeEmail"),
            f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeInvalidEmail"))}</p>{profile_link}</section>',
            locale,
        )
    if new_email == user["email"].lower():
        return _account_page(
            translate(locale, "user.changeEmail"),
            f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeSame"))}</p>{profile_link}</section>',
            locale,
        )
    existing = get_store().user_by_email(new_email)
    if existing is not None and existing.get("id") != user["id"]:
        return _account_page(
            translate(locale, "user.changeEmail"),
            f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeDuplicate"))}</p>{profile_link}</section>',
            locale,
        )

    token = new_email_verification_token()
    get_store().create_email_change(
        user["id"],
        user["email"],
        new_email,
        session_token_hash(token),
        datetime.now(tz=UTC) + timedelta(hours=settings.email_verification_hours),
    )
    try:
        web.send_email_change_email(
            user["email"], user["callsign"], locale, token, "old", new_email
        )
    except EmailDeliveryError:
        get_store().delete_email_change(user["id"])
        return _account_page(
            translate(locale, "user.changeEmail"),
            f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeDeliveryFailed"))}</p>{profile_link}</section>',
            locale,
        )
    return _account_page(
        translate(locale, "user.changeEmail"),
        f'<section class="card form"><p class="success">{_escape(translate(locale, "user.emailChangePending"))}</p>{profile_link}</section>',
        locale,
    )


@router.get("/user/change-email/confirm", response_class=HTMLResponse)
def user_change_email_confirm(
    request: Request,
    token: str = "",
    stage: str = "",
) -> Response:
    locale = request_locale(request)
    profile_link = f'<a class="button" href="/user/profile">{_escape(translate(locale, "user.profile"))}</a>'
    if not token or stage not in {"old", "new"}:
        return _account_page(
            translate(locale, "user.emailChangeInvalidTitle"),
            f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeInvalid"))}</p>{profile_link}</section>',
            locale,
        )
    if stage == "old":
        new_token = new_email_verification_token()
        change = get_store().confirm_old_email_change(
            session_token_hash(token), session_token_hash(new_token)
        )
        if change is None:
            return _account_page(
                translate(locale, "user.emailChangeInvalidTitle"),
                f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeInvalid"))}</p>{profile_link}</section>',
                locale,
            )
        try:
            web.send_email_change_email(
                change["new_email"],
                change["callsign"],
                locale,
                new_token,
                "new",
                change["new_email"],
            )
        except EmailDeliveryError:
            return _account_page(
                translate(locale, "user.emailChangeConfirmOldTitle"),
                f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeDeliveryFailed"))}</p>{profile_link}</section>',
                locale,
            )
        return _account_page(
            translate(locale, "user.emailChangeConfirmOldTitle"),
            f'<section class="card form"><p class="success">{_escape(translate(locale, "user.emailChangeOldConfirmed"))}</p>{profile_link}</section>',
            locale,
        )

    result = get_store().confirm_new_email_change(session_token_hash(token))
    if result is None:
        return _account_page(
            translate(locale, "user.emailChangeInvalidTitle"),
            f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeInvalid"))}</p>{profile_link}</section>',
            locale,
        )
    if result.get("error") == "duplicate":
        return _account_page(
            translate(locale, "user.emailChangeConfirmNewTitle"),
            f'<section class="card form"><p class="error">{_escape(translate(locale, "user.emailChangeDuplicate"))}</p>{profile_link}</section>',
            locale,
        )
    return _account_page(
        translate(locale, "user.emailChangeSuccessTitle"),
        f'<section class="card form"><p class="success">{_escape(translate(locale, "user.emailChangeSuccess"))}</p>{profile_link}</section>',
        locale,
    )


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request) -> HTMLResponse:
    locale = request_locale(request)
    message = translate(locale, "admin.setPassword") if not settings.admin_password else ""
    warning = f'<p class="error">{message}</p>' if message else ""
    content = f'<section class="card form"><h1>{_escape(translate(locale, "admin.login"))}</h1>{warning}<form method="post" action="/admin/login"><label>{_escape(translate(locale, "admin.password"))}</label><input type="password" name="password" required><button class="button" type="submit">{_escape(translate(locale, "admin.open"))}</button></form></section>'
    return _account_page(translate(locale, "admin.login"), content, locale)


@router.post("/admin/login")
async def admin_login(request: Request) -> Response:
    locale = request_locale(request)
    fields = await _form_fields(request)
    if not settings.admin_password or not hmac.compare_digest(fields.get("password", ""), settings.admin_password):
        return _account_page(translate(locale, "admin.login"), f'<section class="card"><p class="error">{_escape(translate(locale, "admin.invalidPassword"))}</p><a class="button" href="/admin/login">{_escape(translate(locale, "admin.open"))}</a></section>', locale)
    response = _admin_redirect()
    response.set_cookie("admin_session", issue_admin_token(settings.admin_password), max_age=8 * 60 * 60, httponly=True, samesite="lax", secure=settings.cookie_secure)
    return response


@router.post("/admin/logout")
def admin_logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("admin_session")
    return response
