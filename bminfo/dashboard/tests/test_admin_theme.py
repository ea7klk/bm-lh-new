"""The admin must stay navigable and recognisably part of this application.

The Django admin is a separate presentation layer: it ships its own templates
and its own stylesheet, so nothing prevents it from drifting into a different
product with no way back. These tests pin the three things that make it belong
here -- a link back to the site on every page, the application stylesheet, and
the translated labels -- so a Django upgrade that renames a block or a variable
fails the build instead of silently reverting the theme.
"""

import pathlib

from django.test import SimpleTestCase, TestCase

from dashboard.i18n import SUPPORTED_LOCALES, catalog
from dashboard.models import User

ROOT = pathlib.Path(__file__).resolve().parents[3]
ADMIN_TEMPLATES = ROOT / "bminfo" / "templates" / "admin"
ADMIN_CSS = ROOT / "bminfo" / "dashboard" / "static" / "dashboard" / "admin.css"

# Pages a staff user reaches in normal operation: the index, a changelist, an
# edit form, and the custom maintenance screen.
ADMIN_PAGES = ("/admin/", "/admin/dashboard/user/", "/admin/dashboard/qso/", "/administration/")


class AdminChromeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("ADMIN", "admin@example.com", "admin-password", name="Administrator")
        self.client.force_login(self.admin)

    def get(self, path, data=None):
        response = self.client.get(path, data or {})
        self.assertEqual(response.status_code, 200, f"{path} did not render")
        return response.content.decode()

    def test_every_admin_page_links_back_to_the_site(self):
        for path in ADMIN_PAGES:
            with self.subTest(path=path):
                html = self.get(path)
                self.assertIn('class="bm-return"', html)
                self.assertIn('href="/"', html)
                self.assertIn("Back to the site", html)

    def test_every_admin_page_uses_the_application_stylesheet(self):
        for path in ADMIN_PAGES:
            with self.subTest(path=path):
                self.assertIn("dashboard/admin.css", self.get(path))

    def test_every_admin_page_carries_the_application_wordmark(self):
        for path in ADMIN_PAGES:
            with self.subTest(path=path):
                html = self.get(path)
                self.assertIn("BrandMeister Statistics by EA7KLK", html)
                self.assertIn("brandmeister-logo.png", html)

    def test_admin_index_offers_the_application_pages(self):
        html = self.get("/admin/")
        for target in ('href="/"', 'href="/user/live-qsos/"', 'href="/reports/"',
                       'href="/administration/"', 'href="/about/"'):
            with self.subTest(target=target):
                self.assertIn(target, html)

    def test_admin_index_still_shows_recent_actions(self):
        """The index template is copied from Django; this is the part that drifts."""
        self.assertIn("Recent actions", self.get("/admin/"))

    def test_dark_mode_is_not_offered(self):
        """The app has one light theme; a dead toggle would be worse than none."""
        html = self.get("/admin/")
        self.assertNotIn("dark_mode.css", html)
        self.assertNotIn("theme.js", html)
        self.assertNotIn("theme-toggle", html)

    def test_the_favicon_matches_the_application(self):
        self.assertIn("favicon-32.png", self.get("/admin/"))

    def test_labels_follow_the_requested_locale(self):
        expected = {
            "en": "Back to the site",
            "es": "Volver al sitio",
            "de": "Zur\u00fcck zur Website",
            "fr": "Retour au site",
        }
        for locale, text in expected.items():
            with self.subTest(locale=locale):
                # Query data must be passed positionally: a lang= keyword would
                # land in the WSGI environ, not in request.GET.
                html = self.get("/admin/", {"lang": locale})
                self.assertIn(text, html)

    def test_the_document_language_matches_the_labels(self):
        """A screen reader must not be told "English" over Spanish labels."""
        for locale in ("es", "de", "fr"):
            with self.subTest(locale=locale):
                html = self.get("/admin/", {"lang": locale})
                self.assertIn(f'<html lang="{locale}"', html)


class AdminLocalisationTests(TestCase):
    """Django's own admin chrome must be in the same language as our labels.

    The app translates its strings from translations.json without activating a
    locale; Django's admin strings come from Django's catalogues and only switch
    when one is activated. Rendering one over the other produces a half-German
    page, which is why the middleware exists.
    """

    EXPECTED = {
        "en": ("en", "Log in"),
        "es": ("es", "Iniciar sesión"),
        "de": ("de", "Anmelden"),
        "fr": ("fr", "Connexion"),
    }

    def test_the_login_page_is_fully_localised(self):
        # The login page needs no session, so it is the cheapest page to read
        # Django's own translations from.
        for locale, (lang, django_string) in self.EXPECTED.items():
            with self.subTest(locale=locale):
                response = self.client.get("/admin/login/", {"lang": locale})
                html = response.content.decode()
                self.assertIn(f'<html lang="{lang}"', html)
                self.assertIn(django_string, html)

    def test_the_scoped_middleware_does_not_leak_to_other_urls(self):
        from django.test import RequestFactory
        from django.utils import translation

        from dashboard.middleware import AdminLocaleMiddleware

        before = translation.get_language()
        seen = {}

        def get_response(request):
            seen[request.path] = translation.get_language()
            return "ok"

        middleware = AdminLocaleMiddleware(get_response)
        factory = RequestFactory()
        middleware(factory.get("/admin/", {"lang": "de"}))
        middleware(factory.get("/", {"lang": "de"}))

        self.assertEqual(seen["/admin/"], "de")
        self.assertNotEqual(seen["/"], "de", "an app page must not be re-formatted per locale")
        self.assertEqual(translation.get_language(), before, "activation leaked after the request")


class AdminTranslationTests(SimpleTestCase):
    """Every locale must carry the admin labels the chrome renders."""

    REQUIRED = ("backToApp", "administration", "appLinks", "appLinksHint", "maintenance")

    def test_all_locales_define_the_admin_labels(self):
        for locale in SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                admin = catalog(locale)["admin"]
                for key in self.REQUIRED:
                    self.assertTrue(admin.get(key), f"{locale} is missing admin.{key}")

    def test_admin_labels_are_translated_not_copied(self):
        """Exact values, so a locale that falls back to English is visible here.

        Note that "Administration" is correct French, which is why this asserts
        expected strings rather than inequality with the English catalogue.
        """
        expected = {
            "en": {"backToApp": "Back to the site", "maintenance": "Maintenance"},
            "es": {"backToApp": "Volver al sitio", "maintenance": "Mantenimiento"},
            "de": {"backToApp": "Zur\u00fcck zur Website", "maintenance": "Wartung"},
            "fr": {"backToApp": "Retour au site", "maintenance": "Maintenance"},
        }
        english = catalog("en")["admin"]
        for locale, strings in expected.items():
            with self.subTest(locale=locale):
                translated = catalog(locale)["admin"]
                self.assertEqual(set(translated), set(english))
                for key, text in strings.items():
                    self.assertEqual(translated[key], text)


class AdminTemplateTests(SimpleTestCase):
    def test_admin_overrides_extend_djangos_base_not_their_own_name(self):
        """Extending admin/base_site.html from a file of that name recurses."""
        for name in ("base_site.html", "index.html"):
            with self.subTest(template=name):
                text = (ADMIN_TEMPLATES / name).read_text()
                self.assertNotIn(f'{{% extends "admin/{name}" %}}', text)

    def test_theme_stylesheet_exists_and_is_square_and_on_brand(self):
        css = ADMIN_CSS.read_text()
        self.assertIn("--accent: #dd4b39", css.replace("--primary: #dd4b39", "--accent: #dd4b39"))
        self.assertIn("#c0392b", css, "links need the higher-contrast accent variant")
        self.assertIn("--header-bg: #222d32", css)
