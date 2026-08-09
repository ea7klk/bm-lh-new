from unittest import mock

from django.test import SimpleTestCase

from dashboard.qso_rules import NON_QSO_DESTINATION_IDS, raw_event_threshold_seconds


class QsoRuleTests(SimpleTestCase):
    def test_non_qso_destinations_are_fixed(self):
        self.assertEqual(NON_QSO_DESTINATION_IDS, frozenset({4000, 9990}))

    def test_raw_event_threshold_prefers_specific_setting(self):
        with mock.patch.dict("os.environ", {"KERCHUNK_THRESHOLD_SECONDS": "3", "RAW_EVENT_KERCHUNK_THRESHOLD_SECONDS": "1.5"}):
            self.assertEqual(raw_event_threshold_seconds(), 1.5)

    def test_raw_event_threshold_falls_back_to_qso_threshold(self):
        with mock.patch.dict("os.environ", {"KERCHUNK_THRESHOLD_SECONDS": "4"}, clear=True):
            self.assertEqual(raw_event_threshold_seconds(), 4.0)
