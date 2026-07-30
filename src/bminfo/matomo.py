from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from .config import settings


def _javascript_string(value: str) -> str:
    """Encode configuration safely for an inline JavaScript string literal."""
    return (
        json.dumps(value, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def matomo_script(
    *,
    enabled: bool | None = None,
    url: str | None = None,
    site_id: str | None = None,
) -> str:
    """Return a consent-gated Matomo tracking loader."""
    tracking_enabled = settings.matomo_enabled if enabled is None else enabled
    matomo_url = (settings.matomo_url if url is None else url).strip().rstrip("/")
    matomo_site_id = str(
        settings.matomo_site_id if site_id is None else site_id
    ).strip()

    parsed_url = urlparse(matomo_url)
    if not _valid_configuration(tracking_enabled, parsed_url, matomo_site_id):
        return ""

    tracker_base = f"{matomo_url}/"
    return f"""<!-- Matomo Analytics -->
<script>
  (function() {{
    function consentValue() {{
      var prefix = 'bm_cookie_consent=';
      var item = document.cookie.split(';').map(function(value) {{ return value.trim(); }}).find(function(value) {{ return value.indexOf(prefix) === 0; }});
      return item ? decodeURIComponent(item.slice(prefix.length)) : '';
    }}
    window.__bminfoMatomoLoad = function() {{
      if (window.__bminfoMatomoLoaded || consentValue() !== 'analytics') return;
      window.__bminfoMatomoLoaded = true;
      var _paq = window._paq = window._paq || [];
      _paq.push(['trackPageView']);
      _paq.push(['enableLinkTracking']);
      var u = {_javascript_string(tracker_base)};
      _paq.push(['setTrackerUrl', u + 'matomo.php']);
      _paq.push(['setSiteId', {_javascript_string(matomo_site_id)}]);
      var d = document;
      var g = d.createElement('script');
      var s = d.getElementsByTagName('script')[0];
      g.async = true;
      g.src = u + 'matomo.js';
      s.parentNode.insertBefore(g, s);
    }};
    window.__bminfoMatomoDisable = function() {{
      if (window._paq) {{
        window._paq.push(['disableCookies']);
        window._paq.push(['deleteCookies']);
      }}
    }};
    if (consentValue() === 'analytics') window.__bminfoMatomoLoad();
  }})();
</script>
<!-- End Matomo Analytics -->"""


def _valid_configuration(
    tracking_enabled: bool,
    parsed_url: object,
    matomo_site_id: str,
) -> bool:
    return bool(
        tracking_enabled
        and getattr(parsed_url, "scheme", "") in {"http", "https"}
        and getattr(parsed_url, "netloc", "")
        and not getattr(parsed_url, "query", "")
        and not getattr(parsed_url, "fragment", "")
        and re.fullmatch(r"\d+", matomo_site_id)
    )


def matomo_configured() -> bool:
    """Return whether a valid, enabled Matomo configuration is available."""
    matomo_url = settings.matomo_url.strip().rstrip("/")
    parsed_url = urlparse(matomo_url)
    return _valid_configuration(settings.matomo_enabled, parsed_url, str(settings.matomo_site_id).strip())
