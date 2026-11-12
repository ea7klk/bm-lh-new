"""The collector's write counter and progress logging.

An operator needs a periodic sign that ingestion is still moving without turning
on DEBUG, and the count has to be a running total since the process started.
"""

import logging
import threading

from django.test import SimpleTestCase, TestCase

from dashboard.collector import PROGRESS_LOG_EVERY, WriteCounter, store_event
from dashboard.models import QSO, RawEvent, Talkgroup


class WriteCounterTests(SimpleTestCase):
    def test_default_step_is_one_hundred(self):
        self.assertEqual(PROGRESS_LOG_EVERY, 100)
        self.assertEqual(WriteCounter().step, 100)

    def test_logs_on_each_hundredth_row(self):
        counter = WriteCounter()
        messages = []

        class Collect(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = Collect(level=logging.INFO)
        target = logging.getLogger("dashboard.collector")
        target.addHandler(handler)
        previous_level = target.level
        target.setLevel(logging.INFO)
        try:
            for _ in range(250):
                counter.add(1)
        finally:
            target.removeHandler(handler)
            target.setLevel(previous_level)

        self.assertEqual(counter.total, 250)
        self.assertEqual(len(messages), 2, messages)
        self.assertIn("100 rows written since start", messages[0])
        self.assertIn("200 rows written since start", messages[1])

    def test_logs_exactly_at_multiples_and_not_between_them(self):
        counter = WriteCounter()

        for _ in range(99):
            counter.add(1)
        # Nothing logged yet: 99 is below the first step.
        with self.assertNoLogs("dashboard.collector", level="INFO"):
            counter.add(0)

        with self.assertLogs("dashboard.collector", level="INFO") as captured:
            counter.add(1)
        self.assertIn("100 rows written since start", captured.output[0])

        for _ in range(99):
            counter.add(1)
        with self.assertNoLogs("dashboard.collector", level="INFO"):
            counter.add(0)

        with self.assertLogs("dashboard.collector", level="INFO") as captured:
            counter.add(1)
        self.assertIn("200 rows written since start", captured.output[0])

    def test_counts_are_cumulative(self):
        counter = WriteCounter()
        for _ in range(7):
            counter.add(15)

        self.assertEqual(counter.total, 105)

    def test_a_batch_crossing_a_boundary_logs_once(self):
        counter = WriteCounter()
        with self.assertLogs("dashboard.collector", level="INFO") as captured:
            result = counter.add(250)

        self.assertEqual(result, 250)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("250 rows written", captured.output[0])

    def test_zero_and_negative_writes_are_ignored(self):
        counter = WriteCounter()
        self.assertIsNone(counter.add(0))
        self.assertIsNone(counter.add(-5))
        self.assertEqual(counter.total, 0)

    def test_a_step_below_one_is_coerced(self):
        self.assertEqual(WriteCounter(step=0).step, 1)
        self.assertEqual(WriteCounter(step=-10).step, 1)

    def test_add_waits_for_the_lock(self):
        """The critical section is guarded, not merely GIL-protected.

        CPython's GIL makes a lost update hard to reproduce, so a threads-and-
        then-check-total test proves very little. Holding the lock and asserting
        that add() blocks is deterministic, and it is what actually guarantees
        the total is exact under concurrent Socket.IO callbacks.
        """
        counter = WriteCounter()
        started = threading.Event()
        finished = threading.Event()

        def worker():
            started.set()
            counter.add(1)
            finished.set()

        counter._lock.acquire()
        try:
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.2), "add() did not wait for the lock")
        finally:
            counter._lock.release()

        self.assertTrue(finished.wait(2), "add() never completed after the lock was released")
        self.assertEqual(counter.total, 1)

    def test_many_writers_produce_the_exact_total(self):
        """Smoke check for gross errors under contention (see the lock test)."""
        counter = WriteCounter()
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(500):
                counter.add(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(counter.total, 4000)


CONFIGURED = dict(threshold=3, exclude_talkgroup=9, raw_threshold=3)


class StoreEventRowCountTests(TestCase):
    """`store_event` reports rows written, and stays truthiness-compatible."""

    def payload(self, **overrides):
        base = {
            "Event": "Session-Stop",
            "SessionID": "sess-count",
            "SourceCall": "EA7KLK",
            "DestinationID": 214,
            "DestinationName": "Spain",
            "Start": 1000.0,
            "Stop": 1010.0,
        }
        base.update(overrides)
        return {key: value for key, value in base.items() if value is not None}

    def test_a_completed_session_writes_two_rows(self):
        result = store_event(self.payload(), **CONFIGURED)

        self.assertEqual(result.rows_written, 2)
        self.assertTrue(result, "a stored QSO must stay truthy")
        self.assertEqual(RawEvent.objects.count(), 1)
        self.assertEqual(QSO.objects.count(), 1)

    def test_a_non_session_stop_writes_only_the_raw_event(self):
        result = store_event(self.payload(Event="Session-Start"), **CONFIGURED)

        self.assertEqual(result.rows_written, 1)
        self.assertFalse(result, "the boolean still means 'QSO written'")
        self.assertEqual(QSO.objects.count(), 0)

    def test_a_kerchunk_writes_nothing(self):
        result = store_event(self.payload(Start=1000.0, Stop=1000.5), **CONFIGURED)

        self.assertEqual(result.rows_written, 0)
        self.assertFalse(result)
        self.assertEqual(RawEvent.objects.count(), 0)

    def test_an_excluded_talkgroup_writes_only_the_raw_event(self):
        result = store_event(self.payload(DestinationID=9), **CONFIGURED)

        self.assertEqual(result.rows_written, 1)
        self.assertEqual(QSO.objects.count(), 0)

    def test_a_service_destination_writes_only_the_raw_event(self):
        from dashboard.qso_rules import NON_QSO_DESTINATION_IDS

        result = store_event(self.payload(DestinationID=next(iter(NON_QSO_DESTINATION_IDS))), **CONFIGURED)

        self.assertEqual(result.rows_written, 1)
        self.assertEqual(QSO.objects.count(), 0)

    def test_a_missing_destination_name_writes_only_the_raw_event(self):
        result = store_event(self.payload(DestinationID=999, DestinationName=None), **CONFIGURED)

        self.assertEqual(result.rows_written, 1)
        self.assertEqual(QSO.objects.count(), 0)

    def test_a_known_talkgroup_supplies_the_missing_name(self):
        Talkgroup.objects.create(talkgroup_id=214, name="Spain", country="ES",
                                 continent="Europe", full_country_name="Spain")

        result = store_event(self.payload(DestinationName=None), **CONFIGURED)

        self.assertEqual(result.rows_written, 2)
        self.assertFalse(result.talkgroup_created, "the catalogue row already existed")
        self.assertEqual(QSO.objects.get().destination_name, "Spain")

    def test_an_unknown_talkgroup_is_created_but_not_counted_as_rows(self):
        result = store_event(self.payload(DestinationID=21450, DestinationName="Alicante"), **CONFIGURED)

        self.assertTrue(result.talkgroup_created)
        # The catalogue row is reference data, so the throughput count is the
        # raw event plus the QSO only.
        self.assertEqual(result.rows_written, 2)
