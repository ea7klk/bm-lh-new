from datetime import datetime, timezone

from django.test import SimpleTestCase

from dashboard import collector


class CollectorParsingTests(SimpleTestCase):
    def test_decode_accepts_nested_json_bytes_and_dict_payloads(self):
        payload = {"Event": "Session-Stop", "SessionID": "abc"}

        self.assertEqual(collector.decode({"payload": payload}), payload)
        self.assertEqual(collector.decode({"payload": b'{"SessionID": "abc"}'}), {"SessionID": "abc"})
        self.assertEqual(collector.decode('{"SessionID": "abc"}'), {"SessionID": "abc"})

    def test_decode_rejects_invalid_or_non_mapping_payloads(self):
        self.assertIsNone(collector.decode("not-json"))
        self.assertIsNone(collector.decode(["not", "a", "mapping"]))

    def test_number_rejects_booleans_and_invalid_values(self):
        self.assertEqual(collector._number("42", integer=True), 42)
        self.assertEqual(collector._number("42.5"), 42.5)
        self.assertIsNone(collector._number(True))
        self.assertIsNone(collector._number("invalid"))

    def test_datetime_handles_seconds_milliseconds_and_iso_values(self):
        expected = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)
        timestamp = expected.timestamp()

        self.assertEqual(collector._datetime(timestamp), expected)
        self.assertEqual(collector._datetime(timestamp * 1000), expected)
        self.assertEqual(collector._datetime("2026-08-09T18:00:00Z"), expected)
        self.assertIsNone(collector._datetime("invalid"))

    def test_session_id_prefers_native_id_and_derives_stable_fallback(self):
        native, is_native = collector._session_id({"SessionID": 12345})
        self.assertEqual((native, is_native), ("12345", True))

        event = {"Event": "Session-Stop", "Start": 1, "Stop": 4, "SourceID": 2140001}
        first = collector._session_id(event)
        second = collector._session_id(dict(event))
        self.assertEqual(first, second)
        self.assertFalse(first[1])
        self.assertTrue(first[0].startswith("derived-"))

    def test_event_values_normalizes_aliases_and_types(self):
        values = collector._event_values(
            {
                "Event": "Session-Stop",
                "SessionId": "session-1",
                "Start": 1_000,
                "Stop": 2_000,
                "SourceID": "2140001",
                "DestinationID": "21403",
                "ContextID": "7",
                "Slot": "2",
                "RSSI": "-72.5",
            }
        )

        self.assertEqual(values["session_id"], "session-1")
        self.assertTrue(values["is_native"])
        self.assertEqual(values["source_id"], 2_140_001)
        self.assertEqual(values["destination_id"], 21_403)
        self.assertEqual(values["context_id"], 7)
        self.assertEqual(values["slot"], 2)
        self.assertEqual(values["rssi"], -72.5)
