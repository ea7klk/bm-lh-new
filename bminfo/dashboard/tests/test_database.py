from django.test import TestCase

from dashboard.collector import store_event
from dashboard.models import QSO, RawEvent, Talkgroup, User


class DatabaseModelTests(TestCase):
    def test_user_and_talkgroup_records_persist_with_expected_values(self):
        user = User.objects.create_user(
            callsign="EA7KLK",
            email="ea7klk@example.com",
            password="correct horse battery staple",
        )
        talkgroup = Talkgroup.objects.create(
            talkgroup_id=21403,
            name="Provincial Alicante",
            country="ES",
            continent="Europe",
            full_country_name="Spain",
        )

        self.assertEqual(User.objects.get(callsign="EA7KLK").email, "ea7klk@example.com")
        self.assertTrue(User.objects.get(pk=user.pk).check_password("correct horse battery staple"))
        self.assertEqual(Talkgroup.objects.get(pk=talkgroup.pk).continent, "Europe")


class CollectorDatabaseTests(TestCase):
    @staticmethod
    def payload(*, session_id="session-1", start=1_000, stop=1_003, destination_name="Alicante"):
        return {
            "Event": "Session-Stop",
            "SessionID": session_id,
            "Start": start,
            "Stop": stop,
            "SourceID": 2140001,
            "SourceCall": "EA7KLK",
            "DestinationID": 21403,
            "DestinationName": destination_name,
            "Slot": 1,
        }

    def test_exact_threshold_is_stored_and_duplicate_delivery_is_idempotent(self):
        payload = self.payload()

        self.assertTrue(store_event(payload, threshold=3, exclude_talkgroup=9, raw_threshold=3))
        self.assertTrue(store_event(payload, threshold=3, exclude_talkgroup=9, raw_threshold=3))

        self.assertEqual(RawEvent.objects.count(), 1)
        self.assertEqual(QSO.objects.count(), 1)
        qso = QSO.objects.get(session_id="session-1")
        self.assertEqual(qso.duration_ms, 3_000)
        self.assertEqual(qso.talkgroup_id, 21403)

    def test_raw_event_can_be_retained_without_creating_short_qso(self):
        payload = self.payload(session_id="short-session", stop=1_002)

        self.assertFalse(store_event(payload, threshold=3, exclude_talkgroup=9, raw_threshold=1))

        self.assertEqual(RawEvent.objects.count(), 1)
        self.assertEqual(QSO.objects.count(), 0)

    def test_existing_talkgroup_name_is_used_when_event_name_is_missing(self):
        Talkgroup.objects.create(
            talkgroup_id=21403,
            name="Provincial Alicante",
            country="ES",
            continent="Europe",
            full_country_name="Spain",
        )

        payload = self.payload(session_id="metadata-session", destination_name="")
        self.assertTrue(store_event(payload, threshold=3, exclude_talkgroup=9, raw_threshold=3))

        self.assertEqual(QSO.objects.get(session_id="metadata-session").destination_name, "Provincial Alicante")
