"""The single-pass dashboard aggregation must return exactly what the
previous multi-query implementation returned.

The reference below is the original implementation, copied verbatim apart from
the histogram helper, which is shared. Every payload is compared field by
field across a matrix of filters so a difference in grouping, NULL handling,
ordering or timestamp formatting fails the build.
"""

from datetime import timedelta
from unittest import mock

from django.db.models import Count, Max, Q, Sum
from django.test import RequestFactory, TestCase
from django.utils import timezone

from dashboard.core_views import _bucket_count, _histogram_payload, dashboard_payload, filtered_qsos
from dashboard.models import QSO, Talkgroup, User
from dashboard.utils import histogram_bucket_seconds, range_bounds


def reference_payload(request):
    """The pre-rewrite implementation, kept here as the oracle."""
    try:
        row_limit = min(max(int(request.GET.get("rows", 50)), 10), 100)
    except (TypeError, ValueError):
        row_limit = 50
    qsos = filtered_qsos(request)
    start, end = range_bounds(request.GET.get("timeRange"))
    valid_callsign = ~Q(source_call__isnull=True) & ~Q(source_call__exact="") & ~Q(source_call__iexact="unknown") & ~Q(source_call__exact="—")
    callsign_qsos = qsos.filter(valid_callsign)
    summary = qsos.aggregate(
        qso_count=Count("session_id"),
        duration_ms=Sum("duration_ms"),
        unique_sources=Count("source_call", distinct=True, filter=valid_callsign),
        unique_talkgroups=Count("talkgroup_id", distinct=True),
        last_qso_at=Max("start_at"),
    )
    first = qsos.order_by("start_at").values_list("start_at", flat=True).first()
    talkgroups = list(qsos.values(
        "talkgroup_id", "talkgroup__name", "talkgroup__country",
        "talkgroup__full_country_name", "talkgroup__continent",
    ).annotate(
        count=Count("session_id"), totalDuration=Sum("duration_ms"),
        uniqueSources=Count("source_call", distinct=True), lastSeen=Max("start_at"),
    ).order_by("-count", "talkgroup_id")[:row_limit])
    callsigns = list(callsign_qsos.values("source_call", "source_name").annotate(
        count=Count("session_id"), totalDuration=Sum("duration_ms"),
        uniqueTalkgroups=Count("talkgroup_id", distinct=True),
    ).order_by("-count", "source_call")[:row_limit])

    for row in talkgroups:
        row.update(
            destinationId=row.pop("talkgroup_id"), destinationName=row.pop("talkgroup__name"),
            country=row.pop("talkgroup__country"), fullCountryName=row.pop("talkgroup__full_country_name"),
            continent=row.pop("talkgroup__continent"),
            totalDuration=(row.pop("totalDuration") or 0) / 1000,
            lastSeen=row.pop("lastSeen").isoformat() if row.get("lastSeen") else None,
        )
    for row in callsigns:
        row["callsign"] = row.pop("source_call")
        row["sourceName"] = row.pop("source_name")
        row["totalDuration"] = (row["totalDuration"] or 0) / 1000
    callsigns_by_time = sorted(callsigns, key=lambda row: (-row["totalDuration"], row["callsign"]))
    talkgroups_by_time = sorted(talkgroups, key=lambda row: (-row["totalDuration"], row["destinationId"]))

    bucket_seconds = histogram_bucket_seconds(request.GET.get("timeRange"))
    count = _bucket_count(start, end, bucket_seconds)
    counts = {}
    for value in qsos.values_list("start_at", flat=True):
        index = int((value - start).total_seconds() / max(bucket_seconds, 1))
        if 0 <= index < count:
            counts[index] = counts.get(index, 0) + 1

    return {
        "totalEntries": summary["qso_count"] or 0,
        "activityRange": summary["qso_count"] or 0,
        "activity24h": qsos.filter(start_at__gte=timezone.now() - timedelta(hours=24)).count(),
        "uniqueCallsigns": summary["unique_sources"] or 0,
        "uniqueTalkgroups": summary["unique_talkgroups"] or 0,
        "totalDuration": (summary["duration_ms"] or 0) / 1000,
        "firstQsoAt": first.isoformat() if first else None,
        "lastQsoAt": summary["last_qso_at"].isoformat() if summary["last_qso_at"] else None,
        "talkgroups": talkgroups,
        "talkgroupsByTime": talkgroups_by_time,
        "callsigns": callsigns,
        "callsignsByTime": callsigns_by_time,
        "rows": row_limit,
        "histogram": _histogram_payload(start, bucket_seconds, count, counts),
        "recordsRetrieved": summary["qso_count"] or 0,
    }


# A fixed instant so both implementations see the same "now".
FROZEN_NOW = timezone.now().replace(microsecond=0)


class EquivalenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("EA7KLK", "ea7klk@example.com", "test-password", name="Operator")
        spain = Talkgroup.objects.create(talkgroup_id=214, name="Spain", country="ES", continent="Europe", full_country_name="Spain")
        alicante = Talkgroup.objects.create(talkgroup_id=21403, name="Alicante", country="ES", continent="Europe", full_country_name="Spain")
        germany = Talkgroup.objects.create(talkgroup_id=262, name="Deutschland", country="DE", continent="Europe", full_country_name="Germany")
        brazil = Talkgroup.objects.create(talkgroup_id=724, name="Brazil", country="BR", continent="South America", full_country_name="Brazil")

        now = timezone.now()
        rows = []
        # A spread of ages inside a week, several talkgroups, and callsigns that
        # exercise the "not a real station" filtering.
        for index in range(120):
            minutes = index * 80  # up to ~6.6 days
            talkgroup = (spain, alicante, germany, brazil)[index % 4]
            call = ("EA7KLK", "DL1ABC", "M0XYZ", "EA7KLK", "")[index % 5]
            rows.append(QSO(
                session_id=f"eq-{index}",
                source_id=index,
                source_call=None if index % 17 == 0 else call,
                source_name="Operator" if index % 3 else None,
                talkgroup_id=talkgroup.talkgroup_id,
                destination_name=talkgroup.name,
                start_at=now - timedelta(minutes=minutes),
                stop_at=now - timedelta(minutes=minutes) + timedelta(seconds=5 + index % 30),
                duration_ms=(5 + index % 30) * 1000,
                payload={},
            ))
        # A QSO whose talkgroup no longer exists: the join is nullable.
        rows.append(QSO(
            session_id="eq-orphan", source_call="EA7KLK", source_name="Operator",
            talkgroup_id=999999, destination_name="Gone",
            start_at=now - timedelta(minutes=30), stop_at=now - timedelta(minutes=29),
            duration_ms=60000, payload={},
        ))
        # A callsign that must be excluded by the distinct-source count.
        rows.append(QSO(
            session_id="eq-unknown", source_call="unknown", source_name=None,
            talkgroup_id=spain.talkgroup_id, destination_name="Spain",
            start_at=now - timedelta(minutes=45), stop_at=now - timedelta(minutes=44),
            duration_ms=60000, payload={},
        ))
        QSO.objects.bulk_create(rows)

    def _compare(self, params, *, authenticated=True):
        request = RequestFactory().get("/api/dashboard/", params)
        request.user = self.user
        # range_bounds() and the 24h cutoff both read the clock, and the fast
        # path and the reference each call them separately. Freeze time so the
        # two implementations are compared, not the microseconds between calls.
        frozen = FROZEN_NOW
        with mock.patch("django.utils.timezone.now", return_value=frozen):
            fast = dashboard_payload(request).content
            expected = reference_payload(request)
        actual = __import__("json").loads(fast)

        # querySeconds is timing, not data.
        actual.pop("querySeconds", None)
        self.assertEqual(set(actual), set(expected), "response keys changed")
        for key in expected:
            self.assertEqual(actual[key], expected[key], f"mismatch in {key!r} for {params}")
        return actual

    def test_matches_the_reference_for_every_filter_combination(self):
        cases = [
            {},
            {"timeRange": "24h"},
            {"timeRange": "1w"},
            {"timeRange": "1M"},
            {"timeRange": "today"},
            {"timeRange": "yesterday"},
            {"timeRange": "lastWeek"},
            {"rows": "10"},
            {"rows": "100"},
            {"rows": "not-a-number"},
            {"continent": "Europe"},
            {"continent": "All"},
            {"country": "ES"},
            {"talkgroup": ["21403"]},
            {"talkgroup": ["21403", "262"]},
            {"callsign": "ea7"},
            {"callsign": "DL1"},
            {"callsign": "no-such-callsign"},
            {"callsign": "%"},
            {"callsign": "_"},
            {"callsign": "100%"},
            {"timeRange": "1M", "continent": "Europe", "rows": "25"},
            {"timeRange": "1w", "country": "ES", "callsign": "EA7"},
            {"timeRange": "24h", "talkgroup": ["214"], "rows": "100"},
            {"timeRange": "3M", "rows": "10", "continent": "South America"},
        ]
        for params in cases:
            with self.subTest(params=params):
                self._compare(params)

    def test_matches_when_the_range_is_empty(self):
        QSO.objects.all().delete()
        payload = self._compare({"timeRange": "24h"})
        self.assertEqual(payload["totalEntries"], 0)
        self.assertEqual(payload["talkgroups"], [])
        self.assertEqual(payload["callsigns"], [])
        self.assertIsNone(payload["firstQsoAt"])
        self.assertIsNone(payload["lastQsoAt"])
        self.assertTrue(all(bucket["qso_count"] == 0 for bucket in payload["histogram"]))

    def test_wildcards_in_the_callsign_filter_are_escaped_like_the_orm(self):
        # A literal '%' or '_' must not widen the match. Both paths use the same
        # compiled statement, so they agree; this pins the intent.
        wildcard = self._compare({"callsign": "%"})
        self.assertEqual(wildcard["totalEntries"], 0)

    def test_row_limit_is_applied_through_the_single_query(self):
        payload = self._compare({"rows": "10", "timeRange": "1M"})
        self.assertLessEqual(len(payload["talkgroups"]), 10)
        self.assertLessEqual(len(payload["callsigns"]), 10)

    def test_thumbnail_lists_are_sorted_by_duration(self):
        payload = self._compare({"timeRange": "1w"})
        durations = [row["totalDuration"] for row in payload["talkgroupsByTime"]]
        self.assertEqual(durations, sorted(durations, reverse=True))

    def test_anonymous_request_is_restricted_the_same_way(self):
        request = RequestFactory().get("/api/dashboard/", {"timeRange": "3M"})
        from django.contrib.auth.models import AnonymousUser
        request.user = AnonymousUser()
        self.assertEqual(dashboard_payload(request).status_code, 401)
