from django.contrib.staticfiles import finders
from django.test import TestCase

from dashboard.i18n import SUPPORTED_LOCALES, catalog
from dashboard.models import User


class BrandingTests(TestCase):
    """The site presents itself as EA7KLK's own work, not as BrandMeister."""

    BRAND = "BrandMeister Statistics by EA7KLK"

    def setUp(self):
        self.staff = User.objects.create_superuser("EA7KLK", "ea7klk@example.com", "test-password", name="Volker Kerkhoff")

    def _public_pages(self):
        return ["/", "/about/", "/login/", "/register/", "/forgot-password/"]

    def _authenticated_pages(self):
        return ["/", "/about/", "/reports/", "/user/live-qsos/", "/profile/"]

    def test_brand_appears_in_the_header_of_every_public_page(self):
        for path in self._public_pages():
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn('class="sidebar-brand"', html)
                self.assertIn(self.BRAND, html)

    def test_brand_appears_for_signed_in_users(self):
        self.client.force_login(self.staff)
        for path in self._authenticated_pages():
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn(self.BRAND, html)

    def test_role_marketing_staff_page_carries_the_disclaimer(self):
        self.client.force_login(self.staff)
        html = self.client.get("/administration/").content.decode()

        for key in ("work", "data", "affiliation", "asIs"):
            self.assertIn(catalog("en")["disclaimer"][key], html)

    def _assert_disclaimer(self, path):
        html = self.client.get(path).content.decode()
        self.assertIn('class="disclaimer"', html)
        self.assertIn("not affiliated with, endorsed by, or supported by BrandMeister", html)
        self.assertIn("without any claim of completeness, accuracy, or fitness", html)
        self.assertIn("obtained from the BrandMeister LastHeard service", html)

    def test_every_signed_out_page_states_non_affiliation(self):
        # A signed-in visitor is redirected away from these, so stay anonymous.
        for path in self._public_pages():
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
                self._assert_disclaimer(path)

    def test_every_signed_in_page_states_non_affiliation(self):
        self.client.force_login(self.staff)
        for path in self._authenticated_pages():
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
                self._assert_disclaimer(path)

    def test_disclaimer_is_translated(self):
        expected = {
            "de": "Diese Seite ist meine eigene Arbeit.",
            "fr": "Ce site est mon propre travail.",
            "es": "Este sitio es trabajo propio.",
        }
        for locale, sentence in expected.items():
            with self.subTest(locale=locale):
                html = self.client.get("/", {"lang": locale}).content.decode()
                self.assertIn(sentence, html)

    def test_disclaimer_survives_the_dashboard_scripts_footer_update(self):
        # The dashboard rewrites #queryFooter textContent, so the disclaimer must
        # live outside that element or it would be wiped in the browser.
        html = self.client.get("/").content.decode()
        footer_start = html.index('id="queryFooter"')
        disclaimer_start = html.index('class="disclaimer"')

        self.assertGreater(disclaimer_start, footer_start)
        # ...and the disclaimer is not nested inside the element itself.
        query_footer_close = html.index("</div>", footer_start)
        self.assertGreater(disclaimer_start, query_footer_close)

    def test_favicon_points_at_the_brandmeister_logo_asset(self):
        html = self.client.get("/").content.decode()

        self.assertIn('rel="icon"', html)
        self.assertIn("dashboard/favicon-32.png", html)
        self.assertIn("apple-touch-icon", html)
        # The test client does not serve /static; assert collectstatic can find
        # the asset, which is what puts it in the image.
        self.assertIsNotNone(finders.find("dashboard/favicon-32.png"))
        self.assertIsNotNone(finders.find("dashboard/apple-touch-icon.png"))
        self.assertIsNotNone(finders.find("dashboard/brandmeister-logo.png"))

    def test_page_titles_use_the_shared_app_name(self):
        for path in ("/", "/about/", "/login/", "/register/"):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertNotIn("BrandMeister Lastheard", html)

    def test_app_name_is_localized_in_all_locales(self):
        for locale in SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                self.assertTrue(catalog(locale)["common"]["appName"])

    def test_disclaimer_keys_exist_in_every_locale(self):
        for locale in SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                keys = set(catalog(locale)["disclaimer"])
                self.assertEqual(keys, {"work", "data", "affiliation", "asIs"})
                for value in catalog(locale)["disclaimer"].values():
                    self.assertTrue(value.strip(), "disclaimer text must not be empty")
