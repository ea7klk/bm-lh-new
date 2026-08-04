"""Dashboard and about-page HTML routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import web
from .consent import cookie_consent_markup, cookie_consent_script
from .i18n import translate


router = APIRouter()


def request_locale(request):
    return web.request_locale(request)


def _current_user(request):
    return web._current_user(request)


def _escape(value):
    return web._escape(value)


def _account_page(*args, **kwargs):
    return web._account_page(*args, **kwargs)


def _index_path() -> Path:
    candidates = (
        Path(__file__).resolve().parents[2] / "static" / "index.html",
        Path.cwd() / "static" / "index.html",
        Path("/app/static/index.html"),
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("static/index.html is not available")


def _cookie_labels(locale: str) -> dict[str, str]:
    return {
        key: translate(locale, f"cookies.{key}")
        for key in (
            "title",
            "description",
            "acceptAnalytics",
            "rejectAnalytics",
            "settings",
            "settingsTitle",
            "necessary",
            "necessaryDescription",
            "analytics",
            "analyticsDescription",
            "save",
            "continue",
        )
    }


@router.get("/")
def dashboard(request: Request = None) -> HTMLResponse:
    page = _index_path().read_text(encoding="utf-8")
    user = _current_user(request) if request is not None else None
    locale = request_locale(request) if request is not None else "en"
    extended_range_options = (
        '<option value="2w" data-auth-required>Last 14 days</option>'
        '<option value="1M" data-auth-required>Last 30 days</option>'
        '<option value="lastMonth" data-auth-required>Last month</option>'
        '<option value="2M" data-auth-required>Last 2 months</option>'
        '<option value="3M" data-auth-required>Last 3 months</option>'
        if user is not None
        else ""
    )
    callsign_search = (
        '<div class="control search"><label for="callsign" '
        'data-i18n="home.callsignFilter">Callsign filter</label>'
        '<input id="callsign" data-i18n-placeholder="home.callsignPlaceholder" '
        'placeholder="e.g. EA7KLK"></div>'
        if user is not None
        else ""
    )
    talkgroup_filter = (
        '<div class="control talkgroup-filter" id="talkgroupControl" style="display:none">'
        '<label data-i18n="home.talkgroupFilter">Talkgroups</label>'
        '<div id="talkgroups" class="talkgroup-checkboxes" role="group" aria-describedby="talkgroupFilterHint"></div>'
        '<small id="talkgroupFilterHint" data-i18n="home.talkgroupFilterHint">Select one or more active talkgroups</small></div>'
        if user is not None
        else ""
    )
    page = page.replace("<!-- AUTHENTICATED_CALLSIGN_SEARCH -->", callsign_search, 1)
    page = page.replace("<!-- AUTHENTICATED_TALKGROUP_FILTER -->", talkgroup_filter, 1)
    page = page.replace(
        "<!-- AUTHENTICATED_TIME_RANGES -->", extended_range_options, 1
    )
    page = page.replace(
        '<main class="shell" data-authenticated="false">',
        f'<main class="shell" data-authenticated="{str(user is not None).lower()}">',
        1,
    )
    analytics_enabled = web.matomo_configured()
    page = page.replace(
        "<!-- COOKIE_CONSENT_MARKUP -->",
        cookie_consent_markup(_cookie_labels(locale), analytics_enabled),
        1,
    )
    if user is not None:
        page = page.replace(
            '<a href="/user/login" data-i18n="home.login">Log in</a>',
            "",
            1,
        )
        page = page.replace(
            '<a href="/user/register" data-i18n="home.register">Register</a>',
            "",
            1,
        )
        page = page.replace(
            '<a href="/user/profile" data-i18n="home.myProfile">My profile</a>',
            f'<a href="/user/profile" data-user-callsign>{_escape(user["callsign"])}</a>'
            '<a href="/user/live-qsos" data-i18n="live.title">Live QSOs</a>'
            '<a href="/user/reports" data-i18n="home.reports">Reports</a>'
            '<form class="account-logout" method="post" action="/user/logout">'
            '<button type="submit" data-i18n="user.logout">Log out</button></form>',
            1,
        )
    page = page.replace("</head>", f"{web.matomo_script()}\n</head>", 1)
    page = page.replace(
        "<!-- COOKIE_CONSENT_SCRIPT -->",
        cookie_consent_script(analytics_enabled),
        1,
    )
    return HTMLResponse(page)


@router.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> HTMLResponse:
    locale = request_locale(request)
    user = _current_user(request)
    content = f"""
<section class="card">
  <h1>{_escape(translate(locale, "about.title"))}</h1>
  <p class="muted" style="font-size:16px;line-height:1.7">{_escape(translate(locale, "about.intro"))}</p>
  <p style="padding:14px 16px;border-radius:10px;background:#fff7ed;color:#9a3412;line-height:1.6">{_escape(translate(locale, "about.accountMigration"))}</p>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-top:24px">
    <div style="padding:18px;border-radius:10px;background:#f8f9ff">
      <h2 style="font-size:17px">{_escape(translate(locale, "about.project"))}</h2>
      <p class="muted">BrandMeister Lastheard</p>
      <p><a href="https://github.com/ea7klk/bm-lh-new" target="_blank" rel="noopener noreferrer">{_escape(translate(locale, "about.githubLink"))}</a></p>
      <p class="muted">{_escape(translate(locale, "about.repositoryText"))}</p>
    </div>
    <div style="padding:18px;border-radius:10px;background:#f8f9ff">
      <h2 style="font-size:17px">{_escape(translate(locale, "about.author"))}</h2>
      <p>{_escape(translate(locale, "about.authorName"))}</p>
      <h2 style="font-size:17px;margin-top:22px">{_escape(translate(locale, "about.copyright"))}</h2>
      <p>© 2026 Volker Kerkhoff</p>
    </div>
    <div style="padding:18px;border-radius:10px;background:#f8f9ff">
      <h2 style="font-size:17px">{_escape(translate(locale, "about.license"))}</h2>
      <p class="muted">{_escape(translate(locale, "about.licenseText"))}</p>
      <p><a href="https://creativecommons.org/licenses/by-nc-sa/4.0/" target="_blank" rel="noopener noreferrer">CC BY-NC-SA 4.0</a></p>
    </div>
    <div style="padding:18px;border-radius:10px;background:#f8f9ff">
      <h2 style="font-size:17px">{_escape(translate(locale, "about.cookieTitle"))}</h2>
      <p class="muted">{_escape(translate(locale, "about.cookieText"))}</p>
    </div>
  </div>
  <p style="margin-top:26px"><a class="button secondary" href="/">{_escape(translate(locale, "about.back"))}</a></p>
</section>
"""
    return _account_page(translate(locale, "about.title"), content, locale, user=user)
