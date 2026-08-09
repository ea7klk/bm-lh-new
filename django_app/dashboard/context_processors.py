import os

from .i18n import catalog, locale_from_request, translate


def app_context(request):
    locale = locale_from_request(request)
    return {
        "app_locale": locale,
        "labels": catalog(locale),
        "translate": lambda key, **values: translate(locale, key, **values),
        "analytics_consent": request.COOKIES.get("cookie_consent") == "analytics",
        "matomo_enabled": bool(os.getenv("MATOMO_ENABLED", "false").lower() == "true"),
        "matomo_url": os.getenv("MATOMO_URL", "").rstrip("/"),
        "matomo_site_id": os.getenv("MATOMO_SITE_ID", ""),
    }
