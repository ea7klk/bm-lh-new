import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from dashboard.models import QSO, RawEvent, ServiceHeartbeat, Talkgroup, User


class AdminViewDatabaseTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("ADMIN", "admin@example.com", "admin-password", name="Administrator")
        self.client.force_login(self.admin)

    def test_maintenance_page_and_data_quality_report(self):
        now = timezone.now()
        RawEvent.objects.create(
            payload_hash="a" * 64,
            session_id="quality-session",
            event_type="Session-Stop",
            received_at=now,
            start_at=now - timedelta(seconds=2),
            stop_at=now,
            payload={"Event": "Session-Stop"},
        )
        ServiceHeartbeat.objects.create(service_name="collector", last_seen_at=now)
        self.assertEqual(self.client.get("/administration/").status_code, 200)
        response = self.client.get("/administration/data-quality/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["raw_events"], 1)
        self.assertEqual(json.loads(response.content)["kerchunks_filtered"], 1)

    def test_retention_counts_validate_period_and_report_rows(self):
        now = timezone.now()
        RawEvent.objects.create(payload_hash="b" * 64, session_id="old", event_type="Session-Start", received_at=now - timedelta(days=40), payload={})
        valid = self.client.get("/administration/counts/raw-events/1/")
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(json.loads(valid.content)["count"], 1)
        invalid = self.client.get("/administration/counts/raw-events/4/")
        self.assertEqual(invalid.status_code, 400)
        invalid_kind = self.client.get("/administration/counts/other/1/")
        self.assertEqual(invalid_kind.status_code, 400)

    def test_rebuild_qsos_recreates_displayable_session_stop(self):
        now = timezone.now()
        Talkgroup.objects.create(talkgroup_id=21403, name="Alicante")
        RawEvent.objects.create(
            payload_hash="c" * 64,
            session_id="rebuild-session",
            event_type="Session-Stop",
            received_at=now,
            start_at=now - timedelta(seconds=4),
            stop_at=now,
            payload={"Event": "Session-Stop", "SessionID": "rebuild-session", "SourceID": 2140001, "SourceCall": "EA7KLK", "DestinationID": 21403, "DestinationName": "Alicante"},
        )
        with mock.patch.dict(
            "os.environ",
            {"KERCHUNK_THRESHOLD_SECONDS": "3", "EXCLUDE_LOCAL_TALKGROUP": "9"},
            clear=False,
        ):
            response = self.client.post("/administration/rebuild-qsos/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(QSO.objects.get(session_id="rebuild-session").duration_ms, 4_000)

    def test_talkgroup_update_reports_success_or_failure(self):
        with mock.patch("dashboard.collector.sync_talkgroups", return_value=4):
            response = self.client.post("/administration/update-talkgroups/")
        self.assertEqual(response.status_code, 302)
        with mock.patch("dashboard.collector.sync_talkgroups", side_effect=RuntimeError("unavailable")):
            response = self.client.post("/administration/update-talkgroups/")
        self.assertEqual(response.status_code, 302)
