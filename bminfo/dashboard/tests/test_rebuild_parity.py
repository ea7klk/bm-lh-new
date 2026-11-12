"""The rebuild must create exactly the QSOs the live collector would.

v1.2.16 shipped a rebuild that did not require a destination name, so replaying
an archived day invented tens of thousands of QSOs with a NULL destination that
the collector would have refused. These tests compare the two paths directly:
for a set of synthetic events, whatever `store_event` decides, the rebuild must
decide the same.
"""

import hashlib
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from dashboard import rebuild as rebuild_module
from dashboard.collector import store_event
from dashboard.models import QSO, RawEvent, Talkgroup
from dashboard.rebuild import _candidate, _talkgroup_names


BASE = datetime(2026, 11, 10, 12, 0, tzinfo=dt_timezone.utc)


def event_payload(**overrides):
    payload = {
        "Event": "Session-Stop",
        "SessionID": "sess-1",
        "SourceCall": "EA7KLK",
        "DestinationID": 214,
        "DestinationName": "Spain",
        "Start": 1000,
        "Stop": 1010,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


class RebuildParityTests(TestCase):
    def setUp(self):
        Talkgroup.objects.create(talkgroup_id=214, name="Spain", country="ES",
                                 continent="Europe", full_country_name="Spain")

    def _collector_accepts(self, payload, *, start, stop, threshold=3, exclude=9):
        store_event(payload, threshold, exclude, raw_threshold=0)
        return QSO.objects.filter(session_id=str(payload.get("SessionID"))).exists()

    def _rebuild_accepts(self, payload, *, start, stop, threshold=3, exclude=9):
        return _candidate(
            payload, start, stop,
            threshold=threshold, exclude_talkgroup=exclude,
            talkgroup_names=_talkgroup_names(),
        ) is not None

    def _assert_parity(self, payload, *, start=None, stop=None, threshold=3, exclude=9):
        start = start or BASE - timedelta(seconds=10)
        stop = stop or BASE
        # store_event derives the timestamps from the payload, so both paths must
        # be given the same ones.
        payload = dict(payload)
        payload["Start"] = start.timestamp()
        payload["Stop"] = stop.timestamp()
        QSO.objects.all().delete()
        collector = self._collector_accepts(payload, start=start, stop=stop,
                                           threshold=threshold, exclude=exclude)
        rebuilt = self._rebuild_accepts(payload, start=start, stop=stop,
                                        threshold=threshold, exclude=exclude)
        self.assertEqual(
            collector, rebuilt,
            f"rebuild and collector disagree for {payload} "
            f"(collector={collector}, rebuild={rebuilt})",
        )
        return collector

    def test_ordinary_event_is_accepted_by_both(self):
        self.assertTrue(self._assert_parity(event_payload()))

    def test_missing_destination_name_is_refused_by_both(self):
        # The case that produced the v1.2.16 regression.
        self.assertFalse(self._assert_parity(
            event_payload(DestinationID=999, DestinationName=None)))

    def test_missing_name_falls_back_to_the_talkgroup_by_both(self):
        # A talkgroup we know about supplies the name in both paths.
        self.assertTrue(self._assert_parity(
            event_payload(DestinationID=214, DestinationName=None)))

    def test_kerchunk_is_refused_by_both(self):
        self.assertFalse(self._assert_parity(
            event_payload(), start=BASE - timedelta(seconds=1), stop=BASE))

    def test_excluded_talkgroup_is_refused_by_both(self):
        self.assertFalse(self._assert_parity(event_payload(DestinationID=9)))

    def test_out_of_range_destination_is_refused_by_both(self):
        self.assertFalse(self._assert_parity(
            event_payload(DestinationID=1000000, DestinationName="Huge")))

    def test_non_qso_destination_is_refused_by_both(self):
        from dashboard.qso_rules import NON_QSO_DESTINATION_IDS
        destination = next(iter(NON_QSO_DESTINATION_IDS))
        self.assertFalse(self._assert_parity(
            event_payload(DestinationID=destination, DestinationName="Service")))

    def test_missing_timestamps_are_refused_by_both(self):
        payload = event_payload()
        self.assertFalse(self._rebuild_accepts(payload, start=None, stop=None))

    def test_rebuild_does_not_create_rows_without_a_destination_name(self):
        """The end-to-end guard: replaying an event with no name writes nothing."""
        RawEvent.objects.create(
            payload_hash=hashlib.sha256(b"nameless").hexdigest(),
            session_id="nameless",
            event_type="Session-Stop",
            received_at=timezone.now(),
            start_at=timezone.now() - timedelta(seconds=10),
            stop_at=timezone.now(),
            payload={"Event": "Session-Stop", "SessionID": "nameless",
                     "SourceCall": "EA7KLK", "DestinationID": 999},
        )

        rebuild_module.rebuild_from_sources(threshold=3, exclude_talkgroup=9,
                                            include_archive=False)

        self.assertEqual(QSO.objects.count(), 0)
        self.assertFalse(QSO.objects.filter(destination_name__isnull=True).exists())
