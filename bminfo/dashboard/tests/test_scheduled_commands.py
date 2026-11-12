"""The commands the CronJobs run, including the migration advisory lock."""

import hashlib
from datetime import timedelta
from io import StringIO
from unittest import mock

import psycopg
from django.core.management import CommandError, call_command
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from dashboard.management.commands.db_migrate import MIGRATION_LOCK_KEY
from dashboard.maintenance import clear_irrelevant_raw_events
from dashboard.models import RawEvent


def make_event(session_id, *, event_type="Session-Stop", seconds=10, when=None, salt=""):
    when = when or timezone.now()
    return RawEvent.objects.create(
        payload_hash=hashlib.sha256(f"{session_id}{event_type}{seconds}{salt}".encode()).hexdigest(),
        session_id=session_id,
        event_type=event_type,
        received_at=when,
        start_at=when - timedelta(seconds=seconds),
        stop_at=when,
        payload={"Event": event_type, "SessionID": session_id},
    )


@override_settings(RAW_EVENT_KERCHUNK_THRESHOLD_SECONDS=3)
class CleanupCommandTests(TransactionTestCase):
    """TransactionTestCase: the real run vacuums, which a wrapping transaction
    would forbid. Production runs this from a Job in autocommit."""

    def setUp(self):
        make_event("keep")                       # session-stop, long enough
        make_event("short", seconds=1)           # kerchunk
        make_event("start", event_type="Session-Start")
        make_event("keep", seconds=10, salt="dup")  # duplicate session-stop

    def test_dry_run_reports_without_deleting(self):
        out = StringIO()
        call_command("cleanup_raw_events", "--dry-run", stdout=out)

        self.assertIn("would delete", out.getvalue())
        self.assertEqual(RawEvent.objects.count(), 4)

    def test_deletes_only_irrelevant_events(self):
        out = StringIO()
        call_command("cleanup_raw_events", stdout=out)

        # Candidates: the Session-Start, the kerchunk, and the older of the two
        # session-stops for the same session. Only the newest "keep" survives.
        remaining = list(RawEvent.objects.values_list("session_id", flat=True))
        self.assertEqual(remaining, ["keep"])
        self.assertIn("deleted 3", out.getvalue())

    def test_second_run_is_a_no_op(self):
        call_command("cleanup_raw_events", stdout=StringIO())
        before = RawEvent.objects.count()

        out = StringIO()
        call_command("cleanup_raw_events", stdout=out)

        self.assertEqual(RawEvent.objects.count(), before)
        self.assertIn("deleted 0", out.getvalue())


class SyncTalkgroupsCommandTests(TestCase):
    def test_forwards_force_and_reports_the_count(self):
        with mock.patch("dashboard.management.commands.sync_talkgroups.sync_talkgroups",
                        return_value=1856) as sync:
            out = StringIO()
            call_command("sync_talkgroups", "--force", stdout=out)

        sync.assert_called_once()
        self.assertTrue(sync.call_args.kwargs["force"])
        self.assertIn("1856", out.getvalue())

    def test_a_failure_becomes_a_command_error(self):
        with mock.patch("dashboard.management.commands.sync_talkgroups.sync_talkgroups",
                        side_effect=RuntimeError("upstream down")):
            with self.assertRaises(CommandError):
                call_command("sync_talkgroups", stdout=StringIO())


class MigrationLockTests(TransactionTestCase):
    """Two pods start at once, so migrations must serialise, not race."""

    def _params(self):
        settings_dict = connections["default"].settings_dict
        return {
            "dbname": settings_dict["NAME"],
            "user": settings_dict["USER"],
            "password": settings_dict["PASSWORD"],
            "host": settings_dict["HOST"],
            "port": settings_dict["PORT"],
        }

    def _lock_holders(self):
        with connections["default"].cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s",
                [MIGRATION_LOCK_KEY],
            )
            return cursor.fetchone()[0]

    def test_lock_is_released_afterwards(self):
        self.assertEqual(self._lock_holders(), 0)

        call_command("db_migrate", stdout=StringIO(), verbosity=0)

        self.assertEqual(self._lock_holders(), 0, "the advisory lock was not released")

    def test_a_second_process_waits_instead_of_migrating(self):
        holder = psycopg.connect(**self._params(), autocommit=True)
        try:
            holder.execute("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_KEY])

            with self.assertRaises(CommandError) as caught:
                call_command("db_migrate", timeout=1, stdout=StringIO(), verbosity=0)

            self.assertIn("advisory lock", str(caught.exception))
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_KEY])
            holder.close()

    def test_lock_is_released_when_migration_fails(self):
        # A failed migration must not leave the next pod waiting forever.
        with mock.patch("dashboard.management.commands.db_migrate.call_command",
                        side_effect=RuntimeError("bad migration")):
            with self.assertRaises(RuntimeError):
                call_command("db_migrate", stdout=StringIO(), verbosity=0)

        self.assertEqual(self._lock_holders(), 0)


@override_settings(RAW_EVENT_KERCHUNK_THRESHOLD_SECONDS=3, RAW_ARCHIVE_COMPACT=True,
                   RAW_COMPACT_MIN_BYTES=0)
class CleanupCompactionTests(TransactionTestCase):
    """A cleanup must reclaim the disk, not just free pages.

    Production deleted 184,735 rows at 02:17 and left 241 MB holding 74k rows,
    because the cleanup vacuumed but never compacted. The decision is based on
    the table's size, so a run that deletes few rows still fixes inherited bloat.
    """

    def _fill(self, count=400):
        now = timezone.now()
        rows = []
        for index in range(count):
            rows.append(RawEvent(
                payload_hash=hashlib.sha256(f"bloat{index}".encode()).hexdigest(),
                session_id=f"bloat{index}",
                event_type="Session-Start",          # every one is a candidate
                received_at=now - timedelta(minutes=index),
                start_at=now, stop_at=now,
                payload={"Event": "Session-Start", "pad": "x" * 400},
            ))
        RawEvent.objects.bulk_create(rows, batch_size=200)

    def _heap_bytes(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_relation_size('raw_events')")
            return cursor.fetchone()[0]

    def test_large_cleanup_compacts_the_table(self):
        self._fill()
        before = self._heap_bytes()

        result = clear_irrelevant_raw_events()

        self.assertEqual(result["deleted"], 400)
        self.assertFalse(result["compaction"].get("skipped"),
                         "a large delete should compact, not just vacuum")
        self.assertLess(self._heap_bytes(), before)

    def test_small_table_only_vacuums(self):
        # Below the size floor a rewrite costs more than it returns.
        self._fill(count=10)
        with override_settings(RAW_COMPACT_MIN_BYTES=64 * 1024 * 1024):
            result = clear_irrelevant_raw_events()

        self.assertEqual(result["deleted"], 10)
        self.assertTrue(result["compaction"].get("skipped"))
        self.assertIn("already small", result["compaction"]["reason"])

    def test_a_small_delete_still_fixes_inherited_bloat(self):
        # 74k rows' worth of dead space with only a few rows deleted this run:
        # the old per-run row threshold skipped exactly this case.
        self._fill(count=200)
        later = timezone.now()
        for index in range(6):
            make_event(f"extra{index}", event_type="Session-Start", when=later)
        result = clear_irrelevant_raw_events()

        self.assertEqual(result["deleted"], 206)
        self.assertFalse(result["compaction"].get("skipped"))

    def test_compaction_can_be_disabled(self):
        self._fill(count=50)
        result = clear_irrelevant_raw_events(compact=False)

        self.assertEqual(result["deleted"], 50)
        self.assertTrue(result["compaction"]["skipped"])

    def test_nothing_to_delete_does_no_work(self):
        result = clear_irrelevant_raw_events()

        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["compaction"], {})

    def test_dry_run_reports_the_count_without_writing(self):
        self._fill(count=25)

        result = clear_irrelevant_raw_events(dry_run=True)

        self.assertEqual(result["deleted"], 25)
        self.assertTrue(result["dry_run"])
        self.assertEqual(RawEvent.objects.count(), 25)

    def test_command_reports_compaction(self):
        self._fill(count=400)
        out = StringIO()

        call_command("cleanup_raw_events", stdout=out)

        self.assertIn("compacted raw_events", out.getvalue())

    def test_command_no_compact_flag(self):
        self._fill(count=400)
        out = StringIO()

        call_command("cleanup_raw_events", "--no-compact", stdout=out)

        self.assertNotIn("compacted raw_events", out.getvalue())
        self.assertIn("no compaction", out.getvalue())
