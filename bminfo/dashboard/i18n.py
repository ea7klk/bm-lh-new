"""JSON-backed translations shared by Django views and templates."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SUPPORTED_LOCALES = ("en", "es", "de", "fr")


def _translation_path() -> Path:
    return Path(__file__).with_name("translations.json")


@lru_cache(maxsize=1)
def catalogs() -> dict[str, dict[str, Any]]:
    return json.loads(_translation_path().read_text(encoding="utf-8"))


def normalize_locale(value: str | None) -> str:
    value = (value or "").split("-", 1)[0].lower()
    return value if value in SUPPORTED_LOCALES else "en"


def catalog(locale: str | None = None) -> dict[str, Any]:
    locale = normalize_locale(locale)
    all_catalogs = catalogs()
    return all_catalogs.get(locale, all_catalogs["en"])


def translate(locale: str | None, key: str, **values: Any) -> str:
    value: Any = catalog(locale)
    for part in key.split("."):
        if not isinstance(value, dict):
            value = None
            break
        value = value.get(part)
    if value is None:
        value = translate("en", key, **values) if normalize_locale(locale) != "en" else key
    return str(value).format(**values) if values else str(value)


def plural_category(locale: str | None, count: int) -> str:
    """CLDR plural category for the supported locales.

    English, Spanish and German use "one" only for 1. French also counts 0 as
    "one" ("0 heure"). Plural rules for these four languages have no other
    categories, so two is enough; anything else would be guesswork.
    """
    if normalize_locale(locale) == "fr":
        return "one" if count in (0, 1) else "other"
    return "one" if count == 1 else "other"


def translate_count(locale: str | None, key: str, count: int, **values: Any) -> str:
    """Translate ``key`` using the ``One``/``Other`` variant for ``count``.

    Callers pass a base key such as ``emailVerification.expiry``; the catalogue
    holds ``expiryOne`` and ``expiryOther``. Falling back to ``Other`` keeps a
    partially translated catalogue from raising.
    """
    suffix = "One" if plural_category(locale, count) == "one" else "Other"
    localized = translate(locale, f"{key}{suffix}", **values)
    if localized == f"{key}{suffix}":
        localized = translate(locale, f"{key}Other", **values)
    return localized


def locale_from_request(request) -> str:
    requested = request.GET.get("lang") or request.COOKIES.get("bm_lang")
    if not requested:
        requested = request.headers.get("Accept-Language", "en").split(",", 1)[0]
    return normalize_locale(requested)


def time_range_choices(locale, values):
    labels = catalog(locale)["home"]
    calendar_ranges = {"today", "yesterday", "lastWeek", "lastMonth"}
    return [(value, labels[value if value in calendar_ranges else f"last{value}"]) for value in values]
