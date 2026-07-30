import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

import bminfo.web as web
import bminfo.email as email_module
from bminfo.email import (
    render_password_reset_email,
    render_verification_email,
    verification_url,
)
from bminfo.i18n import SUPPORTED_LOCALES


def _request(locale="en", body=b""):
    async def request_body():
        return body

    return SimpleNamespace(
        cookies={},
        query_params=SimpleNamespace(get=lambda name, default=None: default),
        headers={"content-type": "application/x-www-form-urlencoded", "accept-language": "en"},
        state=SimpleNamespace(locale=locale),
        body=request_body,
    )


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_verification_email_has_localized_html_and_text(locale):
    subject, text_body, html_body = render_verification_email("EA7KLK", "token-123", locale)

    assert subject != "emailVerification.subject"
    assert "token-123" in text_body
    assert "token-123" in html_body
    assert 'target' not in html_body
    assert "BrandMeister Lastheard" in html_body
    assert "emailVerification.button" not in html_body


def test_verification_url_uses_configured_public_url(monkeypatch):
    monkeypatch.setattr(
        email_module,
        "settings",
        replace(email_module.settings, app_public_url="https://bm.example.test"),
    )

    assert verification_url("abc/123", "es") == (
        "https://bm.example.test/user/verify?token=abc/123&lang=es"
    )


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_password_reset_email_has_localized_html_and_text(locale):
    subject, text_body, html_body = render_password_reset_email("EA7KLK", "reset-123", locale)

    assert subject != "passwordReset.subject"
    assert "reset-123" in text_body
    assert "reset-123" in html_body
    assert "passwordReset.button" not in html_body


def test_about_page_contains_project_and_license_links():
    page = web.about_page(_request()).body.decode("utf-8")

    assert "https://github.com/ea7klk/bm-lh-new" in page
    assert "https://creativecommons.org/licenses/by-nc-sa/4.0/" in page
    assert "Volker Kerkhoff (EA7KLK)" in page
    assert "previous application" in page
    assert "Essential cookies" in page


def test_dashboard_links_to_about_page():
    page = web.dashboard().body.decode("utf-8")

    assert 'href="/about"' in page


def test_login_page_has_password_reset_link():
    page = web.user_login_page(_request()).body.decode("utf-8")

    assert 'href="/user/forgot-password"' in page


def test_forgot_password_page_has_email_form():
    page = web.user_forgot_password_page(_request()).body.decode("utf-8")

    assert 'action="/user/forgot-password"' in page
    assert 'type="email"' in page


def test_forgot_password_sends_reset_email(monkeypatch):
    calls = {}

    class ResetStore:
        def user_by_email(self, email):
            calls["lookup"] = email
            return {"id": 7, "email": email, "callsign": "EA7KLK"}

        def create_password_reset(self, user_id, token_hash, expires_at):
            calls["reset"] = (user_id, token_hash, expires_at)

    def fake_send(email, callsign, locale, token):
        calls["email"] = (email, callsign, locale, token)
        return token

    monkeypatch.setattr(web, "get_store", lambda: ResetStore())
    monkeypatch.setattr(web, "send_password_reset_email", fake_send)
    request = _request(body=b"email=volker%40example.com")

    response = asyncio.run(web.user_forgot_password(request))

    assert response.status_code == 200
    assert calls["lookup"] == "volker@example.com"
    assert calls["reset"][0] == 7
    assert calls["email"][:3] == ("volker@example.com", "EA7KLK", "en")
    assert "Check your inbox" in response.body.decode("utf-8")


def test_reset_password_changes_password(monkeypatch):
    calls = {}

    class ResetStore:
        def password_reset_user(self, token_hash):
            return {"id": 7, "callsign": "EA7KLK"}

        def reset_password(self, token_hash, password_hash):
            calls["reset"] = (token_hash, password_hash)
            return {"id": 7, "callsign": "EA7KLK"}

    monkeypatch.setattr(web, "get_store", lambda: ResetStore())
    request = _request(
        body=b"token=raw-token&password=newpassword&confirm_password=newpassword"
    )

    response = asyncio.run(web.user_reset_password(request))

    assert response.status_code == 200
    assert calls["reset"][0] == web.session_token_hash("raw-token")
    assert "password has been changed" in response.body.decode("utf-8")


def test_failed_login_suggests_new_account(monkeypatch):
    class LoginStore:
        def user_by_login(self, login):
            return None

    monkeypatch.setattr(web, "get_store", lambda: LoginStore())
    request = _request(body=b"login=EA7KLK&password=wrongpass")

    response = asyncio.run(web.user_login(request))
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "new installation" in body
    assert 'href="/user/register"' in body


def test_email_verification_endpoint_activates_user(monkeypatch):
    class VerificationStore:
        def verify_email_token(self, token_hash):
            assert token_hash == web.session_token_hash("raw-token")
            return {"id": 7, "callsign": "EA7KLK"}

    monkeypatch.setattr(web, "get_store", lambda: VerificationStore())
    response = web.user_verify(_request(), token="raw-token")

    assert response.status_code == 200
    assert "Email confirmed" in response.body.decode("utf-8")


def test_registration_creates_unverified_user_and_sends_email(monkeypatch):
    calls = {}

    class RegistrationStore:
        def create_user(self, callsign, name, email, password_hash):
            calls["user"] = (callsign, name, email, password_hash)
            return {"id": 7, "callsign": callsign}

        def create_email_verification(self, user_id, token_hash, expires_at):
            calls["verification"] = (user_id, token_hash, expires_at)

        def delete_user(self, user_id):
            calls["deleted"] = user_id

    def fake_send(email, callsign, locale, token):
        calls["email"] = (email, callsign, locale, token)
        return token

    monkeypatch.setattr(web, "get_store", lambda: RegistrationStore())
    monkeypatch.setattr(web, "send_verification_email", fake_send)
    request = _request(
        body=b"callsign=EA7KLK&name=Volker+Kerkhoff&email=volker%40example.com&password=longpassword"
    )

    response = asyncio.run(web.user_register(request))

    assert response.status_code == 200
    assert "Check your inbox" in response.body.decode("utf-8")
    assert calls["user"][0] == "EA7KLK"
    assert calls["verification"][0] == 7
    assert calls["email"][0] == "volker@example.com"
