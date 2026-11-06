from datetime import datetime, timezone
from queue import Empty, Queue
import threading
from unittest import mock

from django.test import SimpleTestCase

from dashboard import collector
from dashboard.lastheard_client import LastheardClient


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


class CollectorConnectionTests(SimpleTestCase):
    def test_writer_allows_full_negotiated_heartbeat_window(self):
        client = LastheardClient(reconnection=False, handle_sigint=False)
        client.eio.ping_interval = 25
        client.eio.ping_timeout = 20
        client.eio.state = "connected"
        client.eio.queue.put(None)
        # Run the real Engine.IO writer, intercepting only the underlying
        # blocking queue read. Its old 30-second deadline cuts short the
        # server's 45-second heartbeat window; it must now wait 50 seconds.
        with mock.patch.object(Queue, "get", return_value=None) as get:
            client.eio._write_loop()
        get.assert_called_once_with(block=True, timeout=50)

    def test_queue_preserves_nonblocking_and_longer_deadlines(self):
        client = LastheardClient(reconnection=False, handle_sigint=False)
        client.eio.ping_interval = 25
        client.eio.ping_timeout = 20
        with self.assertRaises(Empty):
            client.eio.queue.get(block=False)
        with mock.patch.object(Queue, "get", return_value=None) as get:
            client.eio.queue.get(timeout=90)
        get.assert_called_once_with(block=True, timeout=90)

    def run_client(self, client, stop_event, connected_event):
        with (
            mock.patch.object(collector.QSO, "objects") as qsos,
            mock.patch.object(collector, "LastheardClient", return_value=client),
        ):
            qsos.filter.return_value.delete.return_value = (0, {})
            collector.run_collector(stop_event, connected_event)

    def fake_client(self):
        client = mock.Mock(connected=False)
        handlers = {}
        client.event.side_effect = lambda handler: handlers.setdefault(handler.__name__, handler)
        return client, handlers

    def test_reconnect_rejoins_room_and_shutdown_stops_active_connection(self):
        client, handlers = self.fake_client()
        stop_event = threading.Event()
        connected_event = threading.Event()
        waits = []

        def connect(*args, **kwargs):
            client.connected = True
            handlers["connect"]()
            self.assertTrue(connected_event.is_set())

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                client.connected = False
                handlers["disconnect"]("transport error")
                self.assertFalse(connected_event.is_set())
                return False
            if len(waits) == 3:
                stop_event.set()
            return stop_event.is_set()

        client.connect.side_effect = connect
        with mock.patch.object(stop_event, "wait", side_effect=wait), self.assertLogs(collector.logger, level="WARNING"):
            self.run_client(client, stop_event, connected_event)
        self.assertEqual(client.connect.call_count, 2)
        self.assertEqual(client.emit.call_args_list, [mock.call("join", "everything")] * 2)
        self.assertEqual(client.shutdown.call_count, 2)
        self.assertEqual(waits, [1, 5, 1])
        self.assertFalse(connected_event.is_set())

    def test_connection_failure_backs_off_and_stop_interrupts_retry(self):
        client, _ = self.fake_client()
        client.connect.side_effect = ConnectionError("offline")
        stop_event = threading.Event()
        connected_event = threading.Event()
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 6:
                stop_event.set()
            return stop_event.is_set()

        with mock.patch.object(stop_event, "wait", side_effect=wait), self.assertLogs(collector.logger, level="ERROR"):
            self.run_client(client, stop_event, connected_event)
        self.assertEqual(waits, [5, 10, 20, 40, 60, 60])
        self.assertEqual(client.shutdown.call_count, 6)
        self.assertFalse(connected_event.is_set())

    def test_heartbeat_only_advances_while_connected(self):
        for connected in (False, True):
            with self.subTest(connected=connected):
                stop_event = threading.Event()
                connected_event = threading.Event()
                if connected:
                    connected_event.set()
                with (
                    mock.patch.object(collector.ServiceHeartbeat, "objects") as heartbeats,
                    mock.patch.object(collector, "close_old_connections"),
                    mock.patch.object(collector.connections, "close_all"),
                    mock.patch.object(stop_event, "wait", side_effect=lambda _: stop_event.set()),
                ):
                    collector.heartbeat_loop(stop_event, connected_event)
                self.assertEqual(heartbeats.update_or_create.call_count, int(connected))
