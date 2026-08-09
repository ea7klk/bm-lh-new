import csv
import io
import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from dashboard.models import QSO, Talkgroup, User


class ReportViewDatabaseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("EA7KLK", "ea7klk@example.com", "password-123", name="Volker")
        self.talkgroup = Talkgroup.objects.create(
            talkgroup_id=21403,
            name="Provincial Alicante",
            country="ES",
            continent="Europe",
            full_country_name="Spain",
        )
        now = timezone.now()
        self.create_qso("current-1", "EA7KLK", now - timedelta(hours=2), 4_000)
        self.create_qso("current-2", "EA5ABC", now - timedelta(hours=1), 8_000)
        self.create_qso("previous", "EA7KLK", now - timedelta(hours=30), 2_000)
        self.client.force_login(self.user)

    def create_qso(self, session_id, source_call, start_at, duration_ms):
        return QSO.objects.create(
            session_id=session_id,
            source_id=2140001,
            source_call=source_call,
            source_name="Operator",
            talkgroup_id=self.talkgroup.talkgroup_id,
            destination_name=self.talkgroup.name,
            start_at=start_at,
            stop_at=start_at + timedelta(milliseconds=duration_ms),
            duration_ms=duration_ms,
            payload={"SessionID": session_id},
        )

    def test_report_api_builds_summary_trends_and_concurrency_data(self):
        response = self.client.get("/reports/data/?timeRange=24h")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["summary"]["qso_count"], 2)
        self.assertEqual(data["summary"]["unique_sources"], 2)
        self.assertEqual(data["traffic_trend"]["previous_qsos"], 1)
        self.assertEqual(data["traffic_trend"]["qso_change_percent"], 100.0)
        self.assertTrue(data["histogram"])
        self.assertEqual(data["talkgroups"][0]["name"], "Provincial Alicante")
        self.assertTrue(data["concurrent_activity"])

    def test_report_page_and_csv_export_are_available(self):
        page = self.client.get("/reports/?timeRange=24h")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "BrandMeister")

        response = self.client.get("/reports/export.csv?timeRange=24h")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=brandmeister-report.csv", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0][0], "Talkgroup")
        self.assertIn("EA7KLK", response.content.decode())

    def test_excel_and_pdf_exports_return_documents(self):
        excel = self.client.get("/reports/export.xlsx?timeRange=24h")
        self.assertEqual(excel.status_code, 200)
        self.assertEqual(excel["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertTrue(excel.content.startswith(b"PK"))

        pdf = self.client.get("/reports/export.pdf?timeRange=24h")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
