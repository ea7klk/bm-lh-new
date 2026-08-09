from django.test import RequestFactory, SimpleTestCase

from dashboard.i18n import SUPPORTED_LOCALES, catalog, catalogs, locale_from_request, normalize_locale, translate


class TranslationTests(SimpleTestCase):
    def tearDown(self):
        catalogs.cache_clear()

    def test_supported_locales_have_catalogs(self):
        all_catalogs = catalogs()
        self.assertEqual(set(SUPPORTED_LOCALES), set(all_catalogs))
        for locale in SUPPORTED_LOCALES:
            self.assertIn("common", catalog(locale))

    def test_locale_normalization_handles_language_tags_and_unknown_values(self):
        self.assertEqual(normalize_locale("es-ES"), "es")
        self.assertEqual(normalize_locale("DE"), "de")
        self.assertEqual(normalize_locale("pt-BR"), "en")
        self.assertEqual(normalize_locale(None), "en")

    def test_translate_formats_values_and_falls_back_to_english(self):
        self.assertEqual(translate("en", "emailVerification.greeting", callsign="EA7KLK"), "Hello EA7KLK,")
        self.assertEqual(translate("es", "emailVerification.greeting", callsign="EA7KLK"), "Hola EA7KLK,")
        self.assertEqual(translate("es", "common.missingKey"), "common.missingKey")

    def test_locale_from_request_prefers_query_cookie_then_header(self):
        factory = RequestFactory()
        self.assertEqual(locale_from_request(factory.get("/", {"lang": "de"})), "de")
        request = factory.get("/", HTTP_ACCEPT_LANGUAGE="fr-FR,fr;q=0.9")
        request.COOKIES["bm_lang"] = "es"
        self.assertEqual(locale_from_request(request), "es")
        self.assertEqual(locale_from_request(factory.get("/", HTTP_ACCEPT_LANGUAGE="fr-FR")), "fr")
