"""Archiving and replay, exercised against an in-memory object store.

The double implements the same interface as the Silo client, so these tests
cover the ordering and verification logic — the part where a mistake loses data
— without touching the network.
"""

import gzip
import hashlib
import io
import json
from datetime import datetime, time as dtime, timedelta, timezone as dt_timezone

from django.test import TestCase, override_settings
from django.utils import timezone

from dashboard.archiver import (
    archive_cutoff,
    archive_day,
    archive_pending,
    day_bounds,
    object_key,
    pending_days,
)
from dashboard.models import QSO, RawEvent, RawEventArchive, Talkgroup
from dashboard.object_store import StoredObject
from dashboard.rebuild import rebuild_from_sources


class FakeStore:
    """In-memory stand-in for the Silo client."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.uploads = 0
        self.deletes = 0
        self.downloads = 0

    def ping(self):
        return None

    def put_file(self, key, path, *, rows, sha256):
        with open(path, "rb") as handle:
            payload = handle.read()
        self.objects[key] = {"data": payload, "rows": rows, "sha256": sha256}
        self.uploads += 1

    def download_to(self, key, path):
        self.downloads += 1
        with open(path, "wb") as handle:
            handle.write(self.objects[key]["data"])

    def head(self, key):
        entry = self.objects.get(key)
        if entry is None:
            return None
        return StoredObject(key=key, size=len(entry["data"]), rows=entry["rows"], sha256=entry["sha256"])

    def list_prefix(self, prefix):
        for key, entry in self.objects.items():
            if key.startswith(prefix):
                yield StoredObject(key=key, size=len(entry["data"]), rows=entry["rows"], sha256=entry["sha256"])

    def delete(self, key):
        self.deletes += 1
        self.objects.pop(key, None)

    def rows_in(self, key):
        with gzip.open(io.BytesIO(self.objects[key]["data"]), "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class CorruptingStore(FakeStore):
    """Reports a different row count than was uploaded, like a truncated object."""

    def head(self, key):
        head = super().head(key)
        if head is None:
            return None
        return StoredObject(key=key, size=head.size, rows=(head.rows or 0) + 5, sha256=head.sha256)


def make_event(session_id, received_at, *, source_call="EA7KLK", destination=214):
    return RawEvent.objects.create(
        payload_hash=hashlib.sha256(session_id.encode()).hexdigest(),
        session_id=session_id,
        event_type="Session-Stop",
        received_at=received_at,
        start_at=received_at - timedelta(seconds=10),
        stop_at=received_at,
        payload={
            "Event": "Session-Stop",
            "SessionID": session_id,
            "SourceCall": source_call,
            "DestinationID": destination,
            "DestinationName": "Spain",
            "Start": 1000,
            "Stop": 1010,
        },
    )


@override_settings(RAW_ARCHIVE_PREFIX="raw-events", S3_BUCKET="bminfo-raw")
class ArchiverTests(TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.now = timezone.now().astimezone(dt_timezone.utc)
        self.today = self.now.date()
        self.yesterday = self.today - timedelta(days=1)
        self.two_days_ago = self.today - timedelta(days=2)

    def _at(self, day, hour=12):
        return datetime.combine(day, dtime(hour, 0), tzinfo=dt_timezone.utc)

    def test_cutoff_is_midnight_utc_today(self):
        cutoff = archive_cutoff()
        self.assertEqual(cutoff.tzinfo, dt_timezone.utc)
        self.assertEqual(cutoff.hour, 0)
        self.assertEqual(cutoff.minute, 0)
        self.assertEqual(cutoff.second, 0)
        self.assertEqual(cutoff.date(), self.today)

    def test_object_key_is_one_file_per_day(self):
        self.assertEqual(object_key(self.yesterday), f"raw-events/{self.yesterday:%Y/%m/%d}.jsonl.gz")

    def test_pending_days_excludes_today_and_is_oldest_first(self):
        make_event("today", self._at(self.today, 1))
        make_event("yesterday", self._at(self.yesterday))
        make_event("older", self._at(self.two_days_ago))

        self.assertEqual(pending_days(), [self.two_days_ago, self.yesterday])

    def test_todays_events_are_never_archived(self):
        make_event("today", self._at(self.today, 1))

        archive_pending(store=self.store)

        self.assertEqual(RawEvent.objects.filter(session_id="today").count(), 1)
        self.assertEqual(self.store.uploads, 0)

    def test_archive_day_uploads_then_deletes_and_records_the_day(self):
        for index in range(3):
            make_event(f"y{index}", self._at(self.yesterday))

        result = archive_day(self.yesterday, self.store)

        key = object_key(self.yesterday)
        self.assertTrue(result.uploaded)
        self.assertEqual(result.rows, 3)
        self.assertEqual(result.deleted, 3)
        self.assertEqual(RawEvent.objects.filter(received_at__gte=self._at(self.yesterday, 0)).count(), 0)

        stored = self.store.rows_in(key)
        self.assertEqual({row["session_id"] for row in stored}, {"y0", "y1", "y2"})
        self.assertEqual(self.store.objects[key]["rows"], 3)

        record = RawEventArchive.objects.get(day=self.yesterday)
        self.assertEqual(record.rows, 3)
        self.assertEqual(record.object_key, key)

    def test_archive_day_is_idempotent(self):
        make_event("y", self._at(self.yesterday))
        archive_day(self.yesterday, self.store)
        uploads_after_first = self.store.uploads

        second = archive_day(self.yesterday, self.store)

        self.assertTrue(second.skipped)
        self.assertEqual(self.store.uploads, uploads_after_first, "a completed day must not be re-uploaded")
        self.assertEqual(RawEventArchive.objects.filter(day=self.yesterday).count(), 1)

    def test_nothing_is_deleted_when_verification_fails(self):
        make_event("y", self._at(self.yesterday))
        corrupting = CorruptingStore()
        corrupting.objects = self.store.objects

        with self.assertRaises(Exception):
            archive_day(self.yesterday, corrupting)

        self.assertEqual(RawEvent.objects.count(), 1, "rows survived a failed verification")
        self.assertFalse(RawEventArchive.objects.exists())

    def test_partial_run_is_merged_rather_than_overwritten(self):
        # Simulate: an earlier run uploaded two rows, then died before deleting.
        make_event("a", self._at(self.yesterday))
        make_event("b", self._at(self.yesterday))
        archive_day(self.yesterday, self.store)
        archived_before = {row["session_id"] for row in self.store.rows_in(object_key(self.yesterday))}

        # New rows arrive for the same day (late delivery) and the table grows.
        make_event("c", self._at(self.yesterday))
        result = archive_day(self.yesterday, self.store)

        archived_after = {row["session_id"] for row in self.store.rows_in(object_key(self.yesterday))}
        self.assertEqual(archived_before, {"a", "b"})
        self.assertEqual(archived_after, {"a", "b", "c"})
        self.assertTrue(result.merged)
        self.assertEqual(RawEvent.objects.count(), 0)

    def test_delete_finishes_when_the_archive_is_already_complete(self):
        # Simulate a crash during the delete: the object is complete but rows
        # remain. Re-running must finish the delete without duplicating rows.
        make_event("a", self._at(self.yesterday))
        make_event("b", self._at(self.yesterday))
        archive_day(self.yesterday, self.store)
        key = object_key(self.yesterday)
        archived = self.store.rows_in(key)

        make_event("a", self._at(self.yesterday))
        make_event("b", self._at(self.yesterday))

        result = archive_day(self.yesterday, self.store)

        self.assertEqual(RawEvent.objects.count(), 0)
        self.assertEqual(len(archived), 2)
        # The object is re-verified and merged by payload_hash, so the rows are
        # still present exactly once.
        self.assertEqual({row["session_id"] for row in self.store.rows_in(key)}, {"a", "b"})
        self.assertEqual(len(self.store.rows_in(key)), 2, "merge must not duplicate rows")
        self.assertEqual(result.deleted, 2)

    def test_archive_pending_handles_days_oldest_first(self):
        make_event("older", self._at(self.two_days_ago))
        make_event("yesterday", self._at(self.yesterday))

        summary = archive_pending(store=self.store)

        self.assertEqual([day.day for day in summary.days], [self.two_days_ago, self.yesterday])
        self.assertEqual(summary.rows_deleted, 2)
        self.assertFalse(summary.errors)
        self.assertEqual(RawEventArchive.objects.count(), 2)

    def test_archive_pending_limit_takes_the_oldest(self):
        make_event("older", self._at(self.two_days_ago))
        make_event("yesterday", self._at(self.yesterday))

        summary = archive_pending(store=self.store, limit=1)

        self.assertEqual([day.day for day in summary.days], [self.two_days_ago])
        self.assertEqual(RawEvent.objects.count(), 1)

    def test_dry_run_changes_nothing(self):
        make_event("y", self._at(self.yesterday))

        summary = archive_pending(store=self.store, dry_run=True)

        self.assertEqual(summary.days[0].rows, 1)
        self.assertEqual(self.store.uploads, 0)
        self.assertEqual(RawEvent.objects.count(), 1)
        self.assertFalse(RawEventArchive.objects.exists())

    def test_day_bounds_cover_exactly_one_utc_day(self):
        start, end = day_bounds(self.yesterday)
        self.assertEqual(start.hour, 0)
        self.assertEqual(start.date(), self.yesterday)
        self.assertEqual(end - start, timedelta(days=1))
        self.assertEqual(end.date(), self.today)


@override_settings(RAW_ARCHIVE_PREFIX="raw-events", S3_BUCKET="bminfo-raw")
class RebuildTests(TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.now = timezone.now().astimezone(dt_timezone.utc)
        self.today = self.now.date()
        Talkgroup.objects.create(talkgroup_id=214, name="Spain", country="ES", continent="Europe", full_country_name="Spain")

    def _at(self, day_offset, hour=12):
        day = self.today - timedelta(days=day_offset)
        return datetime.combine(day, dtime(hour, 0), tzinfo=dt_timezone.utc)

    def test_rebuild_reads_the_database_then_the_archive(self):
        make_event("live", self._at(0, 1))
        make_event("archived", self._at(1))
        archive_pending(store=self.store)
        self.assertEqual(RawEvent.objects.count(), 1)

        stats = rebuild_from_sources(threshold=3, exclude_talkgroup=9, store=self.store)

        self.assertEqual(stats.sources, ["database", "archive"])
        self.assertEqual(set(QSO.objects.values_list("session_id", flat=True)), {"live", "archived"})
        self.assertEqual(stats.rebuilt, 2)

    def test_rebuild_is_not_destructive(self):
        # A QSO that exists but has no readable raw event must survive.
        QSO.objects.create(
            session_id="orphan", source_call="EA7KLK", talkgroup_id=214,
            destination_name="Spain", start_at=self._at(5), stop_at=self._at(5) + timedelta(seconds=10),
            duration_ms=10000, payload={},
        )
        make_event("live", self._at(0, 1))

        rebuild_from_sources(threshold=3, exclude_talkgroup=9, store=self.store)

        self.assertTrue(QSO.objects.filter(session_id="orphan").exists())
        self.assertTrue(QSO.objects.filter(session_id="live").exists())

    def test_rebuild_skips_kerchunks_and_excluded_talkgroups(self):
        short = make_event("short", self._at(0, 1))
        short.stop_at = short.start_at + timedelta(seconds=1)
        short.save(update_fields=["stop_at"])
        make_event("excluded", self._at(0, 2), destination=9)

        rebuild_from_sources(threshold=3, exclude_talkgroup=9, store=self.store)

        self.assertFalse(QSO.objects.exists())

    def test_rebuild_database_only_ignores_the_archive(self):
        make_event("archived", self._at(1))
        archive_pending(store=self.store)
        make_event("live", self._at(0, 1))

        stats = rebuild_from_sources(threshold=3, exclude_talkgroup=9, include_archive=False)

        self.assertEqual(stats.sources, ["database"])
        self.assertEqual(set(QSO.objects.values_list("session_id", flat=True)), {"live"})

    def test_rebuild_limit_days_reads_only_newest_archived_days(self):
        for offset in (1, 2, 3):
            make_event(f"day{offset}", self._at(offset))
        archive_pending(store=self.store)

        stats = rebuild_from_sources(threshold=3, exclude_talkgroup=9, store=self.store, limit_days=1)

        self.assertEqual(stats.rebuilt, 1)
        self.assertEqual(QSO.objects.count(), 1)
