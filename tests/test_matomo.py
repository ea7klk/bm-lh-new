from bminfo.matomo import matomo_script
from bminfo.consent import cookie_consent_markup, cookie_consent_script
from bminfo import web


def test_matomo_is_omitted_when_disabled_or_incomplete():
    assert matomo_script(enabled=False, url="https://analytics.example", site_id="1") == ""
    assert matomo_script(enabled=True, url="", site_id="1") == ""
    assert matomo_script(enabled=True, url="https://analytics.example", site_id="") == ""


def test_matomo_script_matches_reference_tracking_flow():
    script = matomo_script(
        enabled=True,
        url="https://analytics.example/matomo/",
        site_id="7",
    )

    assert "<!-- Matomo Analytics -->" in script
    assert "trackPageView" in script
    assert "enableLinkTracking" in script
    assert "https://analytics.example/matomo/" in script
    assert "u + 'matomo.php'" in script
    assert "u + 'matomo.js'" in script
    assert "['setSiteId', \"7\"]" in script
    assert "bm_cookie_consent" in script
    assert "consentValue() === 'analytics'" in script


def test_matomo_rejects_unsafe_configuration():
    assert matomo_script(enabled=True, url="javascript:alert(1)", site_id="1") == ""
    assert matomo_script(enabled=True, url="https://analytics.example", site_id="1<script>") == ""


def test_dashboard_and_account_pages_include_configured_matomo(monkeypatch):
    configured_script = lambda: matomo_script(
        enabled=True,
        url="https://analytics.example",
        site_id="1",
    )
    monkeypatch.setattr(web, "matomo_script", configured_script)
    monkeypatch.setattr(web, "matomo_configured", lambda: True)

    dashboard = web.dashboard().body.decode("utf-8")
    account = web._account_page("Test", "<p>content</p>").body.decode("utf-8")

    assert dashboard.count("<!-- Matomo Analytics -->") == 1
    assert account.count("<!-- Matomo Analytics -->") == 1
    assert 'id="cookieConsent"' in dashboard
    assert 'data-cookie-action="reject"' in dashboard
    assert 'id="cookieSettingsLink"' in account


def test_cookie_consent_is_shown_without_matomo(monkeypatch):
    monkeypatch.setattr(web, "matomo_configured", lambda: False)

    dashboard = web.dashboard().body.decode("utf-8")
    account = web._account_page("Test", "<p>content</p>").body.decode("utf-8")

    assert 'id="cookieConsent"' in dashboard
    assert 'data-i18n="cookies.continue"' in dashboard
    assert "Continue with necessary cookies" in dashboard
    assert 'id="cookieConsent"' in account
    assert 'id="cookieSettingsLink"' not in account


def test_necessary_cookie_consent_script_still_saves_a_choice():
    script = cookie_consent_script(False)

    assert 'if (!analyticsEnabled) return;' not in script
    assert 'value = "necessary"' in script


def test_cookie_consent_script_never_loads_analytics_without_consent():
    script = cookie_consent_script(True)

    assert 'readCookie(consentCookie) !== "analytics"' not in script
    assert 'window.location.reload()' in script
    assert 'document.cookie = consentCookie' in script


def test_cookie_consent_markup_has_explicit_accept_and_reject_choices():
    markup = cookie_consent_markup({key: key for key in (
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
    )})

    assert 'data-cookie-action="accept"' in markup
    assert 'data-cookie-action="reject"' in markup
    assert 'id="analyticsConsent" type="checkbox"' in markup
    assert 'id="cookieSettingsLink"' in markup


def test_cookie_consent_styles_respect_hidden_attribute():
    markup = cookie_consent_markup({key: key for key in ("title", "description", "continue")}, analytics_enabled=False)

    assert 'id="cookieConsent"' in markup
    assert 'hidden aria-labelledby="cookieConsentTitle"' in markup
    assert '.cookie-consent[hidden]{display:none}' in web._account_page("Test", markup).body.decode("utf-8")
