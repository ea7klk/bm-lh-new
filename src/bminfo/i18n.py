from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TRANSLATIONS_PATH = Path(__file__).resolve().parent / "translations.json"
TRANSLATIONS: dict[str, dict[str, Any]] = json.loads(
    TRANSLATIONS_PATH.read_text(encoding="utf-8")
)
LIVE_TRANSLATIONS_PATH = Path(__file__).resolve().parent / "live_translations.json"
LIVE_TRANSLATIONS: dict[str, dict[str, Any]] = json.loads(
    LIVE_TRANSLATIONS_PATH.read_text(encoding="utf-8")
)


def _merge_translations(target: dict[str, Any], additions: dict[str, Any]) -> None:
    for key, value in additions.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_translations(target[key], value)
        else:
            target[key] = value


for _locale, _values in LIVE_TRANSLATIONS.items():
    _merge_translations(TRANSLATIONS.setdefault(_locale, {}), _values)
SUPPORTED_LOCALES = tuple(TRANSLATIONS)
LANGUAGE_COOKIE = "bm_lang"
LANGUAGE_COOKIE_MAX_AGE = 15 * 24 * 60 * 60
LANGUAGE_INFO = {
    "en": {"name": "English", "flag": "🇬🇧"},
    "es": {"name": "Español", "flag": "🇪🇸"},
    "de": {"name": "Deutsch", "flag": "🇩🇪"},
    "fr": {"name": "Français", "flag": "🇫🇷"},
}


def normalize_locale(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower().replace("_", "-")
    direct = {
        "en-us": "en", "en-gb": "en", "es-es": "es", "es-mx": "es",
        "de-de": "de", "de-at": "de", "de-ch": "de", "fr-fr": "fr",
        "fr-ca": "fr", "fr-be": "fr", "fr-ch": "fr",
    }
    if value in SUPPORTED_LOCALES:
        return value
    if value in direct:
        return direct[value]
    prefix = value.split("-", 1)[0]
    return prefix if prefix in SUPPORTED_LOCALES else None


def translate(locale: str, key: str, default: str | None = None) -> str:
    def lookup(catalog: dict[str, Any]) -> Any:
        value: Any = catalog
        for part in key.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    value = lookup(TRANSLATIONS.get(locale, TRANSLATIONS["en"]))
    if value is None:
        value = lookup(TRANSLATIONS["en"])
    return str(value if value is not None else (default or key))


def catalog(locale: str) -> dict[str, Any]:
    return TRANSLATIONS.get(locale, TRANSLATIONS["en"])
