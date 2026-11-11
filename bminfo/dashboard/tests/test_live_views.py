from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from dashboard.live_consumers import LiveQSOConsumer
from dashboard.models import QSO, Talkgroup, User
from dashboard.utils import LIVE_WINDOW_MINUTES


class LiveWindowTests(TestCase):
    """The live page shows a three-minute trailing window.

    Ages are written as absolute values, not derived from LIVE_WINDOW_MINUTES,
    so these tests fail if the window grows as well as if it shrinks.
    """

    def setUp(self):
        self.user = User.objects.create_user("EA7LIVE", "live@example.com", "live-password", name="Live", is_active=True)
        self.talkgroup = Talkgroup.objects.create(talkgroup_id=21403, name="Alicante", country="ES", continent="Europe", full_country_name="Spain")

    def _qso(self, session_id, minutes_ago, *, source_call="EA7LIVE"):
        stop = timezone.now() - timedelta(minutes=minutes_ago)
        return QSO.objects.create(
            session_id=session_id,
            source_call=source_call,
            source_name="Operator",
            talkgroup_id=self.talkgroup.talkgroup_id,
            destination_name="Alicante",
            start_at=stop - timedelta(seconds=5),
            stop_at=stop,
            duration_ms=5000,
        )

    def _consumer(self, filters=None):
        consumer = LiveQSOConsumer(scope={})
        consumer.filters = filters or {}
        return consumer

    def test_window_is_three_minutes(self):
        self.assertEqual(LIVE_WINDOW_MINUTES, 3)

    def test_consumer_returns_only_qsos_inside_the_window(self):
        self._qso("recent", 1)
        self._qso("ten-minutes-old", 10)
        self._qso("half-an-hour-old", 31)

        sessions = [qso.session_id for qso in self._consumer().queryset()]

        self.assertEqual(sessions, ["recent"])

    def test_qso_inside_the_boundary_is_kept(self):
        # Slightly inside three minutes, so the assertion cannot race the clock.
        self._qso("inside", 2.75)
        self._qso("outside", 3.5)

        sessions = [qso.session_id for qso in self._consumer().queryset()]

        self.assertEqual(sessions, ["inside"])

    def test_after_filter_excludes_already_delivered_rows(self):
        self._qso("older", 2)
        self._qso("newer", 1)
        cutoff = timezone.now() - timedelta(minutes=1.5)

        sessions = [qso.session_id for qso in self._consumer().queryset(after=cutoff)]

        self.assertEqual(sessions, ["newer"])

    def test_filters_still_apply_inside_the_window(self):
        self._qso("mine", 1, source_call="EA7LIVE")
        self._qso("theirs", 1, source_call="DG0XYZ")

        sessions = [qso.session_id for qso in self._consumer({"callsign": "EA7LIVE"}).queryset()]

        self.assertEqual(sessions, ["mine"])

    def test_rest_endpoint_uses_the_same_window(self):
        self._qso("recent", 1)
        self._qso("ten-minutes-old", 10)
        self.client.force_login(self.user)

        payload = self.client.get("/api/live-qsos/").json()

        self.assertEqual([row["sessionId"] for row in payload], ["recent"])

    def test_live_page_requires_authentication(self):
        self.assertEqual(self.client.get("/user/live-qsos/").status_code, 302)
        self.assertEqual(self.client.get("/api/live-qsos/").status_code, 302)
