"""Admin surface for the archive: status, buttons, and what was removed."""

import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from dashboard.models import MaintenanceRun, RawEvent, RawEventArchive, User
from dashboard.object_store import configuration_problem


class ArchiveAdminTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("EA7KLK", "ea7klk@example.com", "pw", name="Operator")
        self.client.force_login(self.staff)

    def test_maintenance_page_offers_archive_and_rebuild(self):
        html = self.client.get("/administration/").content.decode()

        self.assertIn("Raw-event archive", html)
        self.assertIn("Archive pending days now", html)
        self.assertIn("Rebuild QSOs from raw events and archive", html)
        # The age-based raw-event deletion buttons are gone.
        self.assertNotIn("Delete irrelevant and duplicate raw events", html)
        self.assertNotIn("Delete raw events (", html)

    def test_status_reports_pending_and_archived_work(self):
        now = timezone.now()
        RawEvent.objects.create(payload_hash="a" * 64, session_id="pending", event_type="Session-Stop",
                                received_at=now - timedelta(days=2), payload={})
        RawEventArchive.objects.create(day=(now - timedelta(days=5)).date(), object_key="k",
                                       rows=10, bytes_compressed=100, sha256="x")

        data = json.loads(self.client.get("/administration/archive/status/").content)

        self.assertGreaterEqual(data["pending_days"], 1)
        self.assertEqual(data["pending_rows"], 1)
        self.assertEqual(data["archived_days"], 1)
        self.assertEqual(data["archived_rows"], 10)
        self.assertEqual(data["newest_archived"], (now - timedelta(days=5)).date().isoformat())
        self.assertIn("archived_days", data)

    def test_status_reports_missing_configuration(self):
        with override_settings(S3_ENDPOINT_URL="", S3_ACCESS_KEY_ID="", S3_SECRET_ACCESS_KEY=""):
            data = json.loads(self.client.get("/administration/archive/status/").content)

        self.assertFalse(data["storage"]["configured"])
        self.assertIn("S3_ENDPOINT_URL", data["storage"]["problem"])

    def test_status_is_staff_only(self):
        self.client.logout()
        response = self.client.get("/administration/archive/status/")
        self.assertEqual(response.status_code, 302)

    def test_archive_button_starts_a_job(self):
        with mock.patch("dashboard.admin_views.jobs.start_job") as start:
            start.return_value = (MaintenanceRun(kind=MaintenanceRun.ARCHIVE), "started")
            response = self.client.post("/administration/archive/")
        self.assertEqual(response.status_code, 302)
        start.assert_called_once()
        self.assertEqual(start.call_args[0][0], MaintenanceRun.ARCHIVE)

    def test_archive_button_reports_a_refusal(self):
        with mock.patch("dashboard.admin_views.jobs.start_job") as start:
            start.return_value = (None, "already running")
            response = self.client.post("/administration/archive/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already running")

    def test_removed_raw_event_delete_routes_are_gone(self):
        self.assertEqual(self.client.post("/administration/raw-events/1/").status_code, 404)
        self.assertEqual(self.client.post("/administration/irrelevant-raw-events/").status_code, 404)
        self.assertEqual(self.client.get("/administration/counts/raw-events/1/").status_code, 400)


class ConfigurationGuardTests(TestCase):
    def test_problem_names_every_missing_setting(self):
        with override_settings(S3_ENDPOINT_URL="", S3_ACCESS_KEY_ID="", S3_SECRET_ACCESS_KEY=""):
            problem = configuration_problem()
        self.assertIn("S3_ENDPOINT_URL", problem)
        self.assertIn("S3_ACCESS_KEY_ID", problem)
        self.assertIn("S3_SECRET_ACCESS_KEY", problem)

    @override_settings(S3_ENDPOINT_URL="https://silo.example", S3_ACCESS_KEY_ID="k",
                       S3_SECRET_ACCESS_KEY="s", S3_BUCKET="bminfo-raw")
    def test_complete_configuration_has_no_problem(self):
        self.assertIsNone(configuration_problem())
