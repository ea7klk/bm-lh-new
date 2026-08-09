import json

from django.test import RequestFactory, SimpleTestCase

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
