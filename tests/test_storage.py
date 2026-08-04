import json
from dataclasses import replace

from bminfo.models import parse_event
from bminfo.sessionizer import make_qso
from bminfo.storage import PostgresStore


class FakeCursor:
    def __init__(self, talkgroup_exists=False):
        self.talkgroup_exists = talkgroup_exists
        self.executed = []
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if "INSERT INTO raw_events" in query:
            self.result = (41,)
        elif "FROM talkgroups" in query:
            self.result = (1,) if self.talkgroup_exists else None

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor

    def cursor(self):
        return self.cursor_instance


class RebuildCursor:
    def __init__(self):
        self.executed = []
        self.result = None
        self.count_queries = 0
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))
        self.rowcount = 0
        if query.strip().startswith("SELECT COUNT(*)::bigint FROM raw_events"):
            self.result = (100,)
        elif "INSERT INTO qsos" in query:
            self.rowcount = 7
        elif query.strip().startswith("SELECT COUNT(*)::bigint FROM qsos"):
            self.result = (7,)

    def fetchone(self):
        return self.result


class RebuildConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.compacted = []

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.cursor_instance

    def execute(self, query):
        self.compacted.append(query)


class CleanupCursor:
    def __init__(self):
        self.executed = []
        self.result = (5, 3)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return self.result


class CleanupConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.compacted = []

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.cursor_instance

    def execute(self, query):
        self.compacted.append(query)


def _qso(destination_name=None, destination_id=214):
    payload = {
        "Event": "Session-Stop",
        "SessionID": "session-storage",
        "SourceID": "1234567",
        "SourceCall": "EA1TEST",
        "DestinationID": destination_id,
        "Start": 1_700_000_000,
        "Stop": 1_700_000_004,
    }
    if destination_name is not None:
        payload["DestinationName"] = destination_name
    event = parse_event({"payload": json.dumps(payload)})
    return event, make_qso(event)


def test_non_displayable_qso_is_kept_only_as_raw_event():
    event, qso = _qso()
    cursor = FakeCursor(talkgroup_exists=False)
    store = object.__new__(PostgresStore)
    store.connection = FakeConnection(cursor)

    assert store.ingest(event, qso) is False
    assert any("INSERT INTO raw_events" in query for query, _ in cursor.executed)
    assert not any("INSERT INTO qsos" in query for query, _ in cursor.executed)


def test_qso_with_display_name_is_inserted():
    event, qso = _qso("Talkgroup 214")
    cursor = FakeCursor()
    store = object.__new__(PostgresStore)
    store.connection = FakeConnection(cursor)

    assert store.ingest(event, qso) is True
    assert any("INSERT INTO qsos" in query for query, _ in cursor.executed)
    assert any("pg_notify" in query for query, _ in cursor.executed)


def test_seven_digit_personal_destination_stays_only_in_raw_events():
    event, qso = _qso("Personal destination")
    cursor = FakeCursor()
    store = object.__new__(PostgresStore)
    store.connection = FakeConnection(cursor)

    assert store.ingest(event, replace(qso, destination_id=1_234_567)) is False
    assert any("INSERT INTO raw_events" in query for query, _ in cursor.executed)
    assert not any("INSERT INTO qsos" in query for query, _ in cursor.executed)


def test_qso_rebuild_uses_set_based_sql_and_threshold():
    cursor = RebuildCursor()
    connection = RebuildConnection(cursor)
    store = object.__new__(PostgresStore)
    store.connection = connection

    result = store.rebuild_qsos_from_raw_events(5)

    insert_query, params = next((query, params) for query, params in cursor.executed if "WITH decoded" in query)
    assert params == (5.0, 9, 999_999)
    assert "DISTINCT ON (d.session_id)" in insert_query
    assert "interval '1 second'" in insert_query
    assert result == {"raw_events_scanned": 100, "eligible_qsos": 7, "qsos_rebuilt": 7}
    assert connection.compacted == ["VACUUM (FULL, ANALYZE) qsos"]
    rebuild_params = next(params for query, params in cursor.executed if "INSERT INTO qsos" in query)
    assert "destination_id <= %s" in next(query for query, params in cursor.executed if "INSERT INTO qsos" in query)
    assert rebuild_params[-1] == 999_999


def test_irrelevant_raw_cleanup_uses_one_delete_and_non_blocking_vacuum():
    cursor = CleanupCursor()
    connection = CleanupConnection(cursor)
    store = object.__new__(PostgresStore)
    store.connection = connection

    result = store.clear_irrelevant_raw_events(7)

    assert len(cursor.executed) == 1
    cleanup_query = cursor.executed[0][0]
    assert "WITH ranked AS MATERIALIZED" in cleanup_query
    assert "candidates AS MATERIALIZED" in cleanup_query
    assert "DELETE FROM raw_events" in cleanup_query
    assert "NOT EXISTS" in cleanup_query
    assert "ROW_NUMBER() OVER" in cleanup_query
    assert "duplicate_rank > 1" in cleanup_query
    assert "start_at + (%s * interval '1 second')" in cleanup_query
    assert cursor.executed[0][1] == (7.0, 7.0)
    assert result == {
        "raw_events_candidates": 5,
        "raw_events_deleted": 3,
        "raw_events_retained": 2,
    }
    assert connection.compacted == ["VACUUM (ANALYZE, PARALLEL 0) raw_events"]


def test_scheduled_irrelevant_raw_cleanup_can_skip_when_another_worker_holds_lock():
    class LockedCursor(CleanupCursor):
        def execute(self, query, params=None):
            self.executed.append((query, params))
            if "pg_try_advisory_xact_lock" in query:
                self.result = (False,)

    cursor = LockedCursor()
    connection = CleanupConnection(cursor)
    store = object.__new__(PostgresStore)
    store.connection = connection

    result = store.clear_irrelevant_raw_events(7, try_advisory_lock=True)

    assert result["cleanup_skipped"] == 1
    assert len(cursor.executed) == 1
    assert connection.compacted == []


def test_pinned_talkgroup_backfill_uses_runtime_threshold():
    cursor = FakeCursor()
    store = object.__new__(PostgresStore)
    store.connection = FakeConnection(cursor)

    store._backfill_pinned_talkgroup_qsos(7)

    query, params = cursor.executed[0]
    assert "interval '1 second'" in query
    assert params[1] == 7.0
