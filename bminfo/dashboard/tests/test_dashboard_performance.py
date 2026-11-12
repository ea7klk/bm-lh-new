"""The dashboard's memory override and covering index, and their limits."""

import json
from datetime import timedelta

from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from dashboard.core_views import dashboard_payload
from dashboard.models import QSO, Talkgroup, User
from django.test import RequestFactory


class WorkMemValidationTests(SimpleTestCase):
    def _normalised(self, configured):
        """Mirror the guard in dashboard_stats without touching the database."""
        import re

        pattern = re.compile(r"^(?!0+\s*(kB|MB|GB)?$)\d{1,7}(kB|MB|GB)?$")
        value = str(configured or "").strip()
        return value if pattern.match(value) else None

    def test_accepts_postgres_units(self):
        for value in ("64MB", "131072", "1GB", "512kB", "8192"):
            with self.subTest(value=value):
                self.assertEqual(self._normalised(value), value)

    def test_rejects_anything_that_is_not_a_size(self):
        # A malformed value must not reach the database, where it would raise
        # and turn the dashboard into a 500.
        for value in ("64 MB", "64MB; DROP TABLE qsos", "big", "0", "-8MB", "64mb", ""):
            with self.subTest(value=value):
                self.assertIsNone(self._normalised(value))


class WorkMemScopeTests(TransactionTestCase):
    """SET LOCAL must not leak onto the pooled connection.

    TransactionTestCase rather than TestCase: TestCase wraps each test in a
    transaction that never commits, so SET LOCAL would still be in effect when
    the assertion runs. Real requests run in autocommit, where the COMMIT at the
    end of atomic() is what resets it.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("EA7KLK", "ea7klk@example.com", "pw", name="Operator")
        talkgroup = Talkgroup.objects.create(talkgroup_id=214, name="Spain", country="ES", continent="Europe", full_country_name="Spain")
        now = timezone.now()
        for index in range(30):
            QSO.objects.create(
                session_id=f"wm-{index}", source_call="EA7KLK", source_name="Operator",
                talkgroup_id=talkgroup.talkgroup_id, destination_name="Spain",
                start_at=now - timedelta(minutes=index),
                stop_at=now - timedelta(minutes=index) + timedelta(seconds=8),
                duration_ms=8000, payload={},
            )

    def _session_work_mem(self):
        with connection.cursor() as cursor:
            cursor.execute("SHOW work_mem")
            return cursor.fetchone()[0]

    def test_override_does_not_persist_after_the_request(self):
        before = self._session_work_mem()
        request = RequestFactory().get("/api/dashboard/", {"timeRange": "24h"})
        request.user = self.user
        self.assertEqual(dashboard_payload(request).status_code, 200)
        after = self._session_work_mem()

        self.assertEqual(before, after, "SET LOCAL leaked beyond its transaction")

    @override_settings(DASHBOARD_WORK_MEM="96MB")
    def test_configured_value_is_accepted(self):
        request = RequestFactory().get("/api/dashboard/", {"timeRange": "24h"})
        request.user = self.user
        self.assertEqual(dashboard_payload(request).status_code, 200)
        self.assertEqual(self._session_work_mem(), self._session_work_mem())

    @override_settings(DASHBOARD_WORK_MEM="not a size")
    def test_a_bad_setting_does_not_break_the_dashboard(self):
        request = RequestFactory().get("/api/dashboard/", {"timeRange": "24h"})
        request.user = self.user
        response = dashboard_payload(request)

        self.assertEqual(response.status_code, 200)
        self.assertGreater(json.loads(response.content)["totalEntries"], 0)

    @override_settings(DASHBOARD_WORK_MEM="")
    def test_empty_setting_falls_back_to_the_server_value(self):
        before = self._session_work_mem()
        request = RequestFactory().get("/api/dashboard/", {"timeRange": "24h"})
        request.user = self.user
        self.assertEqual(dashboard_payload(request).status_code, 200)
        self.assertEqual(self._session_work_mem(), before)


class CoveringIndexTests(SimpleTestCase):
    def test_the_index_covers_every_column_the_dashboard_selects(self):
        from dashboard.models import QSO

        index = next((i for i in QSO._meta.indexes if i.name == "qsos_cover_idx"), None)
        self.assertIsNotNone(index, "qsos_cover_idx is missing from QSO.Meta.indexes")
        self.assertEqual(list(index.fields), ["-start_at"])
        # Without source_name the planner ignores the index entirely, so the
        # column list is load-bearing, not cosmetic.
        self.assertEqual(
            sorted(index.include),
            sorted(["source_call", "source_name", "talkgroup", "duration_ms"]),
        )

    def test_the_dashboard_selects_nothing_outside_the_index(self):
        """If a new column reaches the CTE, the index stops being covering."""
        from dashboard.dashboard_stats import _base_statement
        from dashboard.core_views import filtered_qsos
        from django.test import RequestFactory

        base, _ = _base_statement(filtered_qsos(RequestFactory().get("/api/dashboard/")))
        selected = set()
        inside = base.split("SELECT", 1)[1].split("FROM", 1)[0]
        for part in inside.split(","):
            alias = part.rsplit(" AS ", 1)[-1].strip().strip('"')
            selected.add(alias)
        self.assertEqual(
            selected,
            {"start_at", "source_call", "source_name", "talkgroup_id", "duration_ms"},
            "the CTE selects a different column set than the index covers",
        )
