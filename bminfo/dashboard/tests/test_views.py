import json

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase

from dashboard.core_views import dashboard
from dashboard.i18n import catalog
from dashboard.models import User
from dashboard.public_views import health, locale_catalog


class PublicViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_health_returns_django_liveness_payload(self):
        response = health(self.factory.get("/health/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "ok", "application": "django"})

    def test_locale_catalog_returns_supported_locale(self):
        response = locale_catalog(self.factory.get("/locales/es/"), "es")
        self.assertEqual(response.status_code, 200)
        self.assertIn("common", json.loads(response.content))

    def test_locale_catalog_rejects_unknown_locale(self):
        response = locale_catalog(self.factory.get("/locales/xx/"), "xx")
        self.assertEqual(response.status_code, 404)

    def test_registration_notice_is_localized_between_header_and_query_controls(self):
        for locale in ("en", "es", "de", "fr"):
            with self.subTest(locale=locale):
                request = self.factory.get("/", {"lang": locale})
                request.user = AnonymousUser()
                html = dashboard(request).content.decode()
                self.assertIn(catalog(locale)["home"]["registrationNotice"], html)
                self.assertIn(f'<a href="/register/?lang={locale}">{catalog(locale)["home"]["registeredUsers"]}</a>', html)
                notice = html.index('id="registrationNotice"')
                self.assertLess(html.index('</header>'), notice)
                self.assertLess(notice, html.index('id="timeRange"'))

    def test_registration_notice_is_hidden_from_authenticated_users(self):
        request = self.factory.get("/")
        request.user = User(callsign="EA7KLK")
        self.assertNotIn('id="registrationNotice"', dashboard(request).content.decode())
