from django.test import RequestFactory, SimpleTestCase
from django.template.loader import render_to_string
from django.contrib.auth.models import AnonymousUser
from django.utils.html import escape

from dashboard.i18n import SUPPORTED_LOCALES, catalog, catalogs, locale_from_request, normalize_locale, translate


class TranslationTests(SimpleTestCase):
    def tearDown(self):
        catalogs.cache_clear()

    def test_supported_locales_have_catalogs(self):
        all_catalogs = catalogs()
        self.assertEqual(set(SUPPORTED_LOCALES), set(all_catalogs))
        for locale in SUPPORTED_LOCALES:
            self.assertIn("common", catalog(locale))

    def test_donation_dialog_uses_the_selected_locale_on_shared_pages(self):
        expected = {
            "en": "Support BrandMeister Lastheard",
            "es": "Apoya BrandMeister Lastheard",
            "de": "Unterstütze BrandMeister Lastheard",
            "fr": "Soutenez BrandMeister Lastheard",
        }
        for locale, title in expected.items():
            with self.subTest(locale=locale):
                request = RequestFactory().get("/", {"lang": locale})
                html = render_to_string("dashboard/base.html", request=request)
                self.assertIn(title, html)
                self.assertIn('href="https://ko-fi.com/ea7klk"', html)
                self.assertIn('data-donation-days="30"', html)
                self.assertIn('data-donation-days="365"', html)
                self.assertEqual(set(catalog(locale)["donation"]), set(catalog("en")["donation"]))

    def test_locale_normalization_handles_language_tags_and_unknown_values(self):
        self.assertEqual(normalize_locale("es-ES"), "es")
        self.assertEqual(normalize_locale("DE"), "de")
        self.assertEqual(normalize_locale("pt-BR"), "en")
        self.assertEqual(normalize_locale(None), "en")

    def test_dashboard_translates_subtitle_filters_charts_and_tables(self):
        for locale in SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                request = RequestFactory().get("/", {"lang": locale})
                request.user = AnonymousUser()
                labels = catalog(locale)
                html = render_to_string("dashboard/index.html", request=request)
                self.assertIn(f'<p>{escape(labels["home"]["subtitle"])}</p>', html)
                self.assertIn(f'<label for="timeRange">{escape(labels["home"]["timeRange"])}</label>', html)
                for key in ("qsoActivityTitle", "talkTimeTitle", "callsignQsoTitle", "callsignTalkTimeTitle", "talkgroupActivityTitle"):
                    self.assertIn(f'<h2>{escape(labels["home"][key])}</h2>', html)
                self.assertIn(f'<h2>{escape(labels["reports"]["dailyActivity"])}</h2>', html)
                self.assertIn(f'<th>{escape(labels["home"]["lastHeard"])}</th>', html)
                self.assertIn(f'>{escape(labels["home"]["last24h"])}</option>', html)

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
