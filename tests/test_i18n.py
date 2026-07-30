from bminfo.i18n import (
    LANGUAGE_COOKIE,
    LANGUAGE_COOKIE_MAX_AGE,
    SUPPORTED_LOCALES,
    TRANSLATIONS,
    normalize_locale,
    translate,
)
from bminfo.talkgroups import COUNTRY_NAMES


def test_locale_normalisation_accepts_language_regions():
    assert normalize_locale("es-MX") == "es"
    assert normalize_locale("de_DE") == "de"
    assert normalize_locale("zh-CN") is None


def test_every_supported_locale_has_the_english_catalog_keys():
    def leaf_paths(value, prefix=""):
        paths = set()
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(child, dict):
                paths.update(leaf_paths(child, path))
            else:
                paths.add(path)
        return paths

    english_paths = leaf_paths(TRANSLATIONS["en"])
    for locale in SUPPORTED_LOCALES:
        assert leaf_paths(TRANSLATIONS[locale]) == english_paths


def test_locale_catalog_is_external_and_keeps_machine_country_values():
    assert TRANSLATIONS["es"]["metadata"]["continents"]["Europe"] == "Europa"
    assert TRANSLATIONS["es"]["metadata"]["countries"]["ES"] == "España"
    assert COUNTRY_NAMES["ES"] == "Spain"


def test_language_is_cookie_only():
    assert LANGUAGE_COOKIE == "bm_lang"
    assert LANGUAGE_COOKIE_MAX_AGE == 15 * 24 * 60 * 60
    assert translate("fr", "common.language") == "Langue"
