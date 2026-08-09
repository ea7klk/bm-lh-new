from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from dashboard.utils import histogram_bucket_seconds, qso_duration, range_bounds, restricted_request


class UtilityTests(SimpleTestCase):
    def test_range_bounds_defaults_invalid_values_to_last_24_hours(self):
        now = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)
        start, end = range_bounds("unknown", now=now)
        self.assertEqual(start, now - timedelta(hours=24))
        self.assertIsNone(end)

    def test_calendar_range_bounds_are_day_aligned(self):
        now = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)
        start, end = range_bounds("yesterday", now=now)
        self.assertEqual(start, datetime(2026, 8, 8, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 8, 9, tzinfo=timezone.utc))

    def test_histogram_bucket_scales_with_time_range(self):
        self.assertEqual(histogram_bucket_seconds("5m"), 60)
        self.assertEqual(histogram_bucket_seconds("24h"), 3600)
        self.assertEqual(histogram_bucket_seconds("1w"), 6 * 3600)

    def test_qso_duration_formats_seconds_minutes_and_hours(self):
        self.assertEqual(qso_duration(0), "0 s")
        self.assertEqual(qso_duration(65), "1:05")
        self.assertEqual(qso_duration(3661), "1:01:01")
        self.assertEqual(qso_duration(-4), "0 s")

    def test_restricted_request_detects_authenticated_ranges_and_callsigns(self):
        class Request:
            GET = {"timeRange": "2w"}

        self.assertTrue(restricted_request(Request()))
        Request.GET = {"callsign": "EA7KLK"}
        self.assertTrue(restricted_request(Request()))
        Request.GET = {"timeRange": "1w"}
        self.assertFalse(restricted_request(Request()))
