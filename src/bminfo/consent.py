from __future__ import annotations

from html import escape
from typing import Mapping


COOKIE_CONSENT_COOKIE = "bm_cookie_consent"
COOKIE_CONSENT_MAX_AGE = 180 * 24 * 60 * 60


def cookie_consent_markup(
    labels: Mapping[str, str],
    analytics_enabled: bool = True,
) -> str:
    """Render the consent banner and preferences dialog with localized labels."""

    def label(key: str) -> str:
        return escape(str(labels.get(key, key)))

    if analytics_enabled:
        actions = f'''
    <div class="cookie-consent-actions">
      <button type="button" class="button" data-cookie-action="accept" data-i18n="cookies.acceptAnalytics">{label("acceptAnalytics")}</button>
      <button type="button" class="button secondary" data-cookie-action="reject" data-i18n="cookies.rejectAnalytics">{label("rejectAnalytics")}</button>
      <button type="button" class="cookie-settings-link" data-cookie-action="settings" data-i18n="cookies.settings">{label("settings")}</button>
    </div>'''
        settings_markup = f'''
    <div id="cookieSettings" class="cookie-settings" hidden>
      <h3 data-i18n="cookies.settingsTitle">{label("settingsTitle")}</h3>
      <label class="cookie-option">
        <input type="checkbox" checked disabled>
        <span><strong data-i18n="cookies.necessary">{label("necessary")}</strong><small data-i18n="cookies.necessaryDescription">{label("necessaryDescription")}</small></span>
      </label>
      <label class="cookie-option">
        <input id="analyticsConsent" type="checkbox">
        <span><strong data-i18n="cookies.analytics">{label("analytics")}</strong><small data-i18n="cookies.analyticsDescription">{label("analyticsDescription")}</small></span>
      </label>
      <button type="button" class="button" data-cookie-action="save" data-i18n="cookies.save">{label("save")}</button>
    </div>'''
        settings_link = f'<a href="#cookieConsent" id="cookieSettingsLink" class="cookie-settings-footer" data-cookie-action="settings" data-i18n="cookies.settings">{label("settings")}</a>'
    else:
        actions = f'''
    <div class="cookie-consent-actions">
      <button type="button" class="button" data-cookie-action="reject" data-i18n="cookies.continue">{label("continue")}</button>
    </div>'''
        settings_markup = ""
        settings_link = ""

    return f"""
<section id="cookieConsent" class="cookie-consent" hidden aria-labelledby="cookieConsentTitle">
  <div class="cookie-consent-card" role="dialog" aria-modal="false">
    <h2 id="cookieConsentTitle" data-i18n="cookies.title">{label("title")}</h2>
    <p data-i18n="cookies.description">{label("description")}</p>
    {actions}
    {settings_markup}
  </div>
</section>
{settings_link}
"""


def cookie_consent_script(analytics_enabled: bool) -> str:
    """Return the client-side consent manager without loading third-party code."""

    enabled = "true" if analytics_enabled else "false"
    return f"""
<script>
(function() {{
  var analyticsEnabled = {enabled};
  var consentCookie = "{COOKIE_CONSENT_COOKIE}";
  var maxAge = {COOKIE_CONSENT_MAX_AGE};
  var banner = document.getElementById("cookieConsent");
  var settings = document.getElementById("cookieSettings");
  var analyticsCheckbox = document.getElementById("analyticsConsent");
  var readCookie = function(name) {{
    var prefix = name + "=";
    var item = document.cookie.split(";").map(function(value) {{ return value.trim(); }}).find(function(value) {{ return value.indexOf(prefix) === 0; }});
    return item ? decodeURIComponent(item.slice(prefix.length)) : "";
  }};
  var writeConsent = function(value) {{
    document.cookie = consentCookie + "=" + encodeURIComponent(value) + "; Max-Age=" + maxAge + "; Path=/; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
  }};
  var showSettings = function() {{
    if (!analyticsEnabled || !banner) return;
    banner.hidden = false;
    if (settings) settings.hidden = false;
    if (analyticsCheckbox) analyticsCheckbox.checked = readCookie(consentCookie) === "analytics";
  }};
  var save = function(value) {{
    if (!analyticsEnabled || value !== "analytics") value = "necessary";
    writeConsent(value);
    if (value !== "analytics" && window.__bminfoMatomoDisable) window.__bminfoMatomoDisable();
    window.location.reload();
  }};
  window.__bminfoCookieConsent = {{ showSettings: showSettings, save: save }};
  if (banner) {{
    banner.hidden = readCookie(consentCookie) !== "";
    document.querySelectorAll("[data-cookie-action]").forEach(function(control) {{
      control.addEventListener("click", function() {{
        var action = control.getAttribute("data-cookie-action");
        if (action === "accept") save("analytics");
        if (action === "reject") save("necessary");
        if (action === "settings") showSettings();
        if (action === "save") save(analyticsCheckbox && analyticsCheckbox.checked ? "analytics" : "necessary");
      }});
    }});
  }}
}})();
</script>
"""
