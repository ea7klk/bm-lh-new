import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from dashboard import jobs
from dashboard.models import MaintenanceRun, QSO, RawEvent, ServiceHeartbeat, Talkgroup, User


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
        QSO.objects.create(
            session_id="old-qso", source_call="EA7KLK", talkgroup_id=21403,
            destination_name="Alicante", start_at=now - timedelta(days=40),
            stop_at=now - timedelta(days=40) + timedelta(seconds=5), duration_ms=5000,
        )
        valid = self.client.get("/administration/counts/qsos/1/")
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(json.loads(valid.content)["count"], 1)
        invalid = self.client.get("/administration/counts/qsos/4/")
        self.assertEqual(invalid.status_code, 400)
        invalid_kind = self.client.get("/administration/counts/other/1/")
        self.assertEqual(invalid_kind.status_code, 400)

    def test_raw_events_are_no_longer_deleted_by_age(self):
        # They are archived to object storage and pruned a day at a time, so the
        # age-based counter and the delete button are gone.
        now = timezone.now()
        RawEvent.objects.create(payload_hash="b" * 64, session_id="old", event_type="Session-Start",
                                received_at=now - timedelta(days=40), payload={})
        self.assertEqual(self.client.get("/administration/counts/raw-events/1/").status_code, 400)
        self.assertEqual(self.client.post("/administration/raw-events/1/").status_code, 404)
        self.assertEqual(self.client.post("/administration/irrelevant-raw-events/").status_code, 404)

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
        def run_inline(kind, target, **kwargs):
            # The view normally hands this to a thread; run the same body inline
            # so the assertion is deterministic (and never reaches object storage).
            run = MaintenanceRun.objects.create(kind=kind, status=MaintenanceRun.RUNNING)
            target(run=run, **kwargs)
            # Mirror what jobs.start_job's worker does on success.
            run.status = MaintenanceRun.SUCCEEDED
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "finished_at"])
            run.refresh_from_db()
            return run, "started"

        with mock.patch.dict(
            "os.environ",
            {"KERCHUNK_THRESHOLD_SECONDS": "3", "EXCLUDE_LOCAL_TALKGROUP": "9"},
            clear=False,
        ), mock.patch("dashboard.admin_views.jobs.start_job", side_effect=run_inline):
            response = self.client.post("/administration/rebuild-qsos/", {"include_archive": "0"})
        self.assertEqual(response.status_code, 302)
        run = MaintenanceRun.objects.filter(kind=MaintenanceRun.REBUILD).latest("started_at")
        self.assertEqual(run.status, MaintenanceRun.SUCCEEDED)
        self.assertEqual(QSO.objects.get(session_id="rebuild-session").duration_ms, 4_000)

    def test_talkgroup_update_reports_success_or_failure(self):
        with mock.patch("dashboard.collector.sync_talkgroups", return_value=4):
            response = self.client.post("/administration/update-talkgroups/")
        self.assertEqual(response.status_code, 302)
        with mock.patch("dashboard.collector.sync_talkgroups", side_effect=RuntimeError("unavailable")):
            response = self.client.post("/administration/update-talkgroups/")
        self.assertEqual(response.status_code, 302)
