import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from dashboard.models import QSO, Talkgroup, User


class DashboardDatabaseViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("EA7KLK", "ea7klk@example.com", "password-123", name="Volker")
        self.alicante = Talkgroup.objects.create(
            talkgroup_id=21403,
            name="Provincial Alicante",
            country="ES",
            continent="Europe",
            full_country_name="Spain",
        )
        self.global_tg = Talkgroup.objects.create(
            talkgroup_id=91,
            name="World-wide",
            country="Global",
            continent="Global",
            full_country_name="Global",
        )
        now = timezone.now()
        self.first = self.make_qso("first", self.alicante, "EA7KLK", now - timedelta(minutes=20), 4_000)
        self.second = self.make_qso("second", self.global_tg, "unknown", now - timedelta(minutes=10), 8_000)

    @staticmethod
    def make_qso(session_id, talkgroup, source_call, start_at, duration_ms):
        return QSO.objects.create(
            session_id=session_id,
            source_id=2140001,
            source_call=source_call,
            source_name="Operator",
            talkgroup_id=talkgroup.talkgroup_id,
            destination_name=talkgroup.name,
            start_at=start_at,
            stop_at=start_at + timedelta(milliseconds=duration_ms),
            duration_ms=duration_ms,
            payload={"SessionID": session_id},
        )

    def test_dashboard_api_aggregates_qsos_and_excludes_unknown_callsign(self):
        response = self.client.get("/api/dashboard/?timeRange=24h&rows=10")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["totalEntries"], 2)
        self.assertEqual(data["uniqueCallsigns"], 1)
        self.assertEqual(data["uniqueTalkgroups"], 2)
        self.assertEqual(data["totalDuration"], 12)
        self.assertEqual(
            {row["destinationName"] for row in data["talkgroups"]},
            {"Provincial Alicante", "World-wide"},
        )
        self.assertEqual(data["callsigns"][0]["callsign"], "EA7KLK")
        self.assertTrue(data["histogram"])

    def test_dashboard_and_qso_endpoints_serve_data(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        qsos = self.client.get("/api/qsos/?timeRange=24h&limit=1&offset=1")
        self.assertEqual(qsos.status_code, 200)
        self.assertEqual(len(json.loads(qsos.content)), 1)
        self.assertEqual(json.loads(qsos.content)[0]["sessionId"], "first")

    def test_restricted_ranges_require_authentication(self):
        response = self.client.get("/api/dashboard/?timeRange=2w")
        self.assertEqual(response.status_code, 401)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/dashboard/?timeRange=2w").status_code, 200)

    def test_filter_endpoints_return_catalogs_and_apply_authenticated_scope(self):
        continents = json.loads(self.client.get("/api/continents/").content)
        self.assertEqual(continents, ["Europe", "Global"])
        countries = json.loads(self.client.get("/api/countries/?continent=Europe").content)
        self.assertEqual(countries, [{"value": "ES", "label": "Spain"}])

        anonymous_talkgroups = json.loads(self.client.get("/api/talkgroups/?timeRange=24h").content)
        self.assertEqual({row["id"] for row in anonymous_talkgroups}, {91, 21403})
        self.client.force_login(self.user)
        filtered = json.loads(self.client.get("/api/talkgroups/?timeRange=24h&talkgroup=21403").content)
        self.assertEqual([row["id"] for row in filtered], [21403])
