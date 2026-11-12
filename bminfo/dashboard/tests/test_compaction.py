"""Compaction of raw_events after archiving.

The important properties are that it runs *once per pass* rather than once per
archived day, that it is skipped when nothing was evicted, and that a lock
timeout is reported rather than raised so the next pass can retry.
"""

import hashlib
from datetime import datetime, time as dtime, timedelta, timezone as dt_timezone
from unittest import mock

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from dashboard import archiver
from dashboard.archiver import archive_pending, compact_raw_events
from dashboard.models import RawEvent


class FakeStore:
    def __init__(self):
        self.objects = {}

    def ping(self):
        return None

    def put_file(self, key, path, *, rows, sha256):
        with open(path, "rb") as handle:
            self.objects[key] = {"data": handle.read(), "rows": rows, "sha256": sha256}

    def download_to(self, key, path):
        with open(path, "wb") as handle:
            handle.write(self.objects[key]["data"])

    def head(self, key):
        from dashboard.object_store import StoredObject
        entry = self.objects.get(key)
        if entry is None:
            return None
        return StoredObject(key=key, size=len(entry["data"]), rows=entry["rows"], sha256=entry["sha256"])

    def list_prefix(self, prefix):
        return iter([])

    def delete(self, key):
        self.objects.pop(key, None)


def make_event(session_id, received_at):
    RawEvent.objects.create(
        payload_hash=hashlib.sha256(session_id.encode()).hexdigest(),
        session_id=session_id,
        event_type="Session-Stop",
        received_at=received_at,
        start_at=received_at - timedelta(seconds=5),
        stop_at=received_at,
        payload={"Event": "Session-Stop", "SessionID": session_id,
                 "SourceCall": "EA7KLK", "DestinationID": 214, "DestinationName": "Spain"},
    )


@override_settings(RAW_ARCHIVE_PREFIX="raw-events", S3_BUCKET="bminfo-raw",
                   RAW_ARCHIVE_COMPACT=True, RAW_COMPACT_MIN_ROWS=1)
class CompactionTests(TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.today = timezone.now().astimezone(dt_timezone.utc).date()

    def _at(self, days_ago, hour=12):
        day = self.today - timedelta(days=days_ago)
        return datetime.combine(day, dtime(hour, 0), tzinfo=dt_timezone.utc)

    def test_compaction_runs_once_per_pass_not_once_per_day(self):
        for offset in (1, 2, 3):
            make_event(f"d{offset}", self._at(offset))

        with mock.patch.object(archiver, "compact_raw_events", return_value={"skipped": False}) as compact:
            archive_pending(store=self.store)

        self.assertEqual(compact.call_count, 1, "compaction must not run per archived day")

    def test_compaction_is_skipped_when_nothing_was_evicted(self):
        # Nothing eligible: no days, so no compaction attempt at all.
        with mock.patch.object(archiver, "compact_raw_events") as compact:
            summary = archive_pending(store=self.store)
        compact.assert_not_called()
        self.assertEqual(summary.compaction, {})

    def test_compaction_is_skipped_below_the_row_threshold(self):
        make_event("one", self._at(1))
        with override_settings(RAW_COMPACT_MIN_ROWS=1000):
            with mock.patch.object(archiver, "compact_raw_events") as compact:
                summary = archive_pending(store=self.store)
        compact.assert_not_called()
        self.assertTrue(summary.compaction.get("skipped"))
        self.assertIn("only 1 row", summary.compaction["reason"])

    def test_compaction_can_be_disabled(self):
        make_event("one", self._at(1))
        with mock.patch.object(archiver, "compact_raw_events") as compact:
            archive_pending(store=self.store, compact=False)
        compact.assert_not_called()

    def test_dry_run_never_compacts(self):
        make_event("one", self._at(1))
        with mock.patch.object(archiver, "compact_raw_events") as compact:
            archive_pending(store=self.store, dry_run=True)
        compact.assert_not_called()

    def test_compaction_result_is_reported_in_the_summary(self):
        make_event("one", self._at(1))
        payload = {"skipped": False, "heap_before_bytes": 1000, "heap_after_bytes": 10,
                   "freed_bytes": 990, "seconds": 0.5, "timed_out": False}
        with mock.patch.object(archiver, "compact_raw_events", return_value=payload):
            summary = archive_pending(store=self.store)
        self.assertEqual(summary.compaction, payload)
        self.assertEqual(summary.as_dict()["compaction"], payload)


@override_settings(RAW_COMPACT_LOCK_TIMEOUT_SECONDS=5)
class CompactRawEventsTests(TransactionTestCase):
    """TransactionTestCase because VACUUM cannot run inside a transaction.

    Production is in autocommit when the archiver runs, so this matches the real
    execution context; TestCase's wrapping transaction would make VACUUM fail
    with "cannot run inside a transaction block".
    """

    def _grow(self):
        now = timezone.now()
        for index in range(400):
            make_event(f"c{index}", now - timedelta(minutes=index))

    def test_vacuum_full_reclaims_space(self):
        self._grow()
        RawEvent.objects.filter(session_id__startswith="c1").delete()

        result = compact_raw_events(force=True)

        self.assertFalse(result["skipped"])
        self.assertFalse(result["timed_out"])
        self.assertIn("heap_before_bytes", result)
        self.assertGreaterEqual(result["freed_bytes"], 0)

    def test_lock_timeout_is_reported_not_raised(self):
        self._grow()
        # Another session holding the table: VACUUM FULL gives up after the lock
        # timeout. That must be recorded, not raised, so the next pass retries.
        with mock.patch.object(
            archiver, "_run_vacuum_full",
            side_effect=Exception("canceling statement due to lock timeout"),
        ):
            result = compact_raw_events(force=True)

        self.assertTrue(result["timed_out"])
        self.assertFalse(result["skipped"])

    def test_small_table_is_skipped_without_force(self):
        result = compact_raw_events()
        self.assertTrue(result["skipped"])
