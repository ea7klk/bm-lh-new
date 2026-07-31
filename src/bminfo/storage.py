from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import threading
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

from .models import BMEvent
from .sessionizer import MAX_TALKGROUP_ID, QSO
from .talkgroups import PINNED_TALKGROUPS


SCHEMA_PATH = Path(__file__).resolve().parent / "migrations" / "001_initial.sql"
NAMED_DESTINATION_SQL = "COALESCE(NULLIF(BTRIM(t.name), ''), NULLIF(BTRIM(q.destination_name), ''))"
DISPLAY_EXCLUDED_DESTINATION_ID = 9
QSO_NOTIFY_CHANNEL = "bminfo_qso_inserted"


class _PooledConnection:
    """Compatibility wrapper that gives each store operation a pooled connection."""

    def __init__(self, pool: Any):
        self._pool = pool
        self._local = threading.local()

    def _active_connection(self) -> Any | None:
        return getattr(self._local, "connection", None)

    @contextmanager
    def cursor(self, *args: Any, **kwargs: Any):
        connection = self._active_connection()
        if connection is not None:
            with connection.cursor(*args, **kwargs) as cursor:
                yield cursor
            return
        with self._pool.connection() as connection:
            with connection.cursor(*args, **kwargs) as cursor:
                yield cursor

    @contextmanager
    def transaction(self):
        if self._active_connection() is not None:
            raise RuntimeError("nested store transactions are not supported")
        with self._pool.connection() as connection:
            self._local.connection = connection
            try:
                with connection.transaction():
                    yield
            finally:
                self._local.connection = None

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        connection = self._active_connection()
        if connection is not None:
            return connection.execute(*args, **kwargs)
        with self._pool.connection() as connection:
            return connection.execute(*args, **kwargs)

    def close(self) -> None:
        self._pool.close()


def _talkgroup_ids(value: int | Sequence[int] | None) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    return [int(item) for item in value]


class PostgresStore:
    def __init__(self, dsn: str):
        # Keep the pool dependency lazy so tests that inject fake connections
        # do not require psycopg_pool at import time.
        from psycopg_pool import ConnectionPool

        from .config import settings

        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=max(1, settings.postgres_pool_min_size),
            max_size=max(settings.postgres_pool_min_size, settings.postgres_pool_max_size),
            kwargs={"autocommit": True},
        )
        self.connection = _PooledConnection(self.pool)

    def close(self) -> None:
        self.connection.close()

    def heartbeat(self, service_name: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO service_heartbeats (service_name, last_seen_at)
                VALUES (%s, now())
                ON CONFLICT (service_name) DO UPDATE SET
                    last_seen_at = EXCLUDED.last_seen_at
                """,
                (service_name,),
            )

    def initialize(self, kerchunk_threshold_seconds: float = 3.0) -> None:
        # Multiple Uvicorn workers start at the same time. Serialize schema
        # setup and the pinned-TG backfill to avoid concurrent upserts/deletes
        # deadlocking on qsos and talkgroups.
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (918273645,))
                cursor.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            self._ensure_pinned_talkgroups()
            self._backfill_pinned_talkgroup_qsos(kerchunk_threshold_seconds)

    def _ensure_pinned_talkgroups(self) -> None:
        with self.connection.cursor() as cursor:
            for talkgroup_id, name, country, continent, full_country_name in PINNED_TALKGROUPS:
                cursor.execute(
                    """
                    INSERT INTO talkgroups
                        (talkgroup_id, name, country, continent, full_country_name)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (talkgroup_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        country = EXCLUDED.country,
                        continent = EXCLUDED.continent,
                        full_country_name = EXCLUDED.full_country_name,
                        last_updated = now()
                    """,
                    (talkgroup_id, name, country, continent, full_country_name),
                )

    def _backfill_pinned_talkgroup_qsos(self, kerchunk_threshold_seconds: float = 3.0) -> None:
        """Restore displayable completed pinned-TG sessions from raw_events.

        raw_events may contain several stop updates for one session. DISTINCT
        ON plus the primary key on qsos keeps one latest qualifying row per
        session and makes this startup backfill safe to run repeatedly.
        """
        for talkgroup_id, name, _country, _continent, _full_country_name in PINNED_TALKGROUPS:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidates AS (
                        SELECT DISTINCT ON (r.session_id)
                               r.session_id, r.id AS raw_event_id,
                               r.start_at, r.stop_at, r.payload
                        FROM raw_events r
                        WHERE lower(r.event_type) = 'session-stop'
                          AND COALESCE(NULLIF(r.payload->>'DestinationID', ''),
                                       NULLIF(r.payload->>'destination_id', '')) = %s
                          AND r.start_at IS NOT NULL
                          AND r.stop_at IS NOT NULL
                          AND r.stop_at >= r.start_at + (%s * interval '1 second')
                        ORDER BY r.session_id, r.id DESC
                    )
                    INSERT INTO qsos
                        (session_id, raw_event_id, source_id, source_call,
                         source_name, destination_id, destination_call,
                         destination_name, context_id, link_call, link_name,
                         link_type_name, slot, master, talker_alias, rssi, ber,
                         start_at, stop_at, duration_ms, is_native_session_id,
                         payload)
                    SELECT session_id, raw_event_id,
                           NULLIF(COALESCE(NULLIF(payload->>'SourceID', ''),
                                           NULLIF(payload->>'source_id', '')), '')::bigint,
                           NULLIF(COALESCE(NULLIF(payload->>'SourceCall', ''),
                                           NULLIF(payload->>'source_call', '')), ''),
                           NULLIF(COALESCE(NULLIF(payload->>'SourceName', ''),
                                           NULLIF(payload->>'source_name', '')), ''),
                           %s,
                           NULLIF(COALESCE(NULLIF(payload->>'DestinationCall', ''),
                                           NULLIF(payload->>'destination_call', '')), ''),
                           %s,
                           NULLIF(COALESCE(NULLIF(payload->>'ContextID', ''),
                                           NULLIF(payload->>'context_id', '')), '')::bigint,
                           NULLIF(COALESCE(NULLIF(payload->>'LinkCall', ''),
                                           NULLIF(payload->>'link_call', '')), ''),
                           NULLIF(COALESCE(NULLIF(payload->>'LinkName', ''),
                                           NULLIF(payload->>'link_name', '')), ''),
                           NULLIF(COALESCE(NULLIF(payload->>'LinkTypeName', ''),
                                           NULLIF(payload->>'link_type_name', '')), ''),
                           NULLIF(COALESCE(NULLIF(payload->>'Slot', ''),
                                           NULLIF(payload->>'slot', '')), '')::smallint,
                           NULLIF(COALESCE(NULLIF(payload->>'Master', ''),
                                           NULLIF(payload->>'master', '')), ''),
                           NULLIF(COALESCE(NULLIF(payload->>'TalkerAlias', ''),
                                           NULLIF(payload->>'talker_alias', '')), ''),
                           NULLIF(COALESCE(NULLIF(payload->>'RSSI', ''),
                                           NULLIF(payload->>'rssi', '')), '')::real,
                           NULLIF(COALESCE(NULLIF(payload->>'BER', ''),
                                           NULLIF(payload->>'ber', '')), '')::real,
                           start_at, stop_at,
                           ROUND(EXTRACT(EPOCH FROM (stop_at - start_at)) * 1000)::integer,
                           COALESCE(NULLIF(payload->>'SessionID', ''),
                                    NULLIF(payload->>'session_id', '')) IS NOT NULL,
                           payload
                    FROM candidates
                    ON CONFLICT (session_id) DO UPDATE SET
                        raw_event_id = EXCLUDED.raw_event_id,
                        source_id = EXCLUDED.source_id,
                        source_call = EXCLUDED.source_call,
                        source_name = EXCLUDED.source_name,
                        destination_id = EXCLUDED.destination_id,
                        destination_call = EXCLUDED.destination_call,
                        destination_name = EXCLUDED.destination_name,
                        context_id = EXCLUDED.context_id,
                        link_call = EXCLUDED.link_call,
                        link_name = EXCLUDED.link_name,
                        link_type_name = EXCLUDED.link_type_name,
                        slot = EXCLUDED.slot,
                        master = EXCLUDED.master,
                        talker_alias = EXCLUDED.talker_alias,
                        rssi = EXCLUDED.rssi,
                        ber = EXCLUDED.ber,
                        start_at = EXCLUDED.start_at,
                        stop_at = EXCLUDED.stop_at,
                        duration_ms = EXCLUDED.duration_ms,
                        is_native_session_id = EXCLUDED.is_native_session_id,
                        payload = EXCLUDED.payload
                    """,
                    (
                        str(talkgroup_id),
                        float(kerchunk_threshold_seconds),
                        talkgroup_id,
                        name,
                    ),
                )

    def ingest(self, event: BMEvent, qso: QSO | None) -> bool:
        """Persist one stream event and only displayable QSOs."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO raw_events
                    (payload_hash, session_id, event_type, received_at,
                     start_at, stop_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (payload_hash) DO UPDATE
                    SET received_at = raw_events.received_at
                RETURNING id
                """,
                (
                    event.payload_hash,
                    event.session_id,
                    event.event_type,
                    event.received_at,
                    event.start_at,
                    event.stop_at,
                    Jsonb(event.payload),
                ),
            )
            raw_event_id = cursor.fetchone()[0]

            store_qso = qso is not None and self._qso_is_displayable(cursor, qso)
            if store_qso:
                self._insert_qso(cursor, raw_event_id, qso)
                cursor.execute(
                    "SELECT pg_notify(%s, %s)",
                    (QSO_NOTIFY_CHANNEL, qso.session_id),
                )
        return store_qso

    @staticmethod
    def _insert_qso(cursor: Any, raw_event_id: int, qso: QSO) -> None:
        cursor.execute(
            """
            INSERT INTO qsos
                (session_id, raw_event_id, source_id, source_call,
                 source_name, destination_id, destination_call,
                 destination_name, context_id, link_call, link_name,
                 link_type_name, slot, master, talker_alias, rssi, ber,
                 start_at, stop_at, duration_ms, is_native_session_id,
                 payload)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE SET
                raw_event_id = EXCLUDED.raw_event_id,
                source_id = EXCLUDED.source_id,
                source_call = EXCLUDED.source_call,
                source_name = EXCLUDED.source_name,
                destination_id = EXCLUDED.destination_id,
                destination_call = EXCLUDED.destination_call,
                destination_name = EXCLUDED.destination_name,
                context_id = EXCLUDED.context_id,
                link_call = EXCLUDED.link_call,
                link_name = EXCLUDED.link_name,
                link_type_name = EXCLUDED.link_type_name,
                slot = EXCLUDED.slot,
                master = EXCLUDED.master,
                talker_alias = EXCLUDED.talker_alias,
                rssi = EXCLUDED.rssi,
                ber = EXCLUDED.ber,
                start_at = EXCLUDED.start_at,
                stop_at = EXCLUDED.stop_at,
                duration_ms = EXCLUDED.duration_ms,
                is_native_session_id = EXCLUDED.is_native_session_id,
                payload = EXCLUDED.payload
            """,
            (
                qso.session_id,
                raw_event_id,
                qso.source_id,
                qso.source_call,
                qso.source_name,
                qso.destination_id,
                qso.destination_call,
                qso.destination_name,
                qso.context_id,
                qso.link_call,
                qso.link_name,
                qso.link_type_name,
                qso.slot,
                qso.master,
                qso.talker_alias,
                qso.rssi,
                qso.ber,
                qso.start_at,
                qso.stop_at,
                qso.duration_ms,
                qso.is_native_session_id,
                Jsonb(qso.payload),
            ),
        )

    @staticmethod
    def _qso_is_displayable(cursor: Any, qso: QSO) -> bool:
        """Match the dashboard's destination visibility rules before inserting."""
        if (
            qso.destination_id is None
            or qso.destination_id == DISPLAY_EXCLUDED_DESTINATION_ID
            or qso.destination_id > MAX_TALKGROUP_ID
        ):
            return False
        if qso.destination_name and qso.destination_name.strip():
            return True
        cursor.execute(
            """
            SELECT 1
            FROM talkgroups
            WHERE talkgroup_id = %s
              AND NULLIF(BTRIM(name), '') IS NOT NULL
            """,
            (qso.destination_id,),
        )
        return cursor.fetchone() is not None

    def list_qsos(
        self,
        limit: int = 100,
        offset: int = 0,
        callsign: str | None = None,
        talkgroup: int | Sequence[int] | None = None,
        start_time: datetime | None = None,
        continent: str | None = None,
        country: str | None = None,
        min_duration_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        clauses = [f"{NAMED_DESTINATION_SQL} IS NOT NULL"]
        params: list[Any] = []
        if start_time is not None:
            clauses.insert(0, "q.start_at >= %s")
            params.append(start_time)
        if continent and continent != "All":
            clauses.append("COALESCE(t.continent, 'Other') = %s")
            params.append(continent)
        if country:
            clauses.append("COALESCE(t.country, 'XX') = %s")
            params.append(country)
        if min_duration_seconds is not None:
            clauses.append("q.duration_ms >= %s")
            params.append(round(float(min_duration_seconds) * 1000))
        if callsign:
            clauses.append("q.source_call ILIKE %s")
            params.append(f"%{callsign}%")
        talkgroup_ids = _talkgroup_ids(talkgroup)
        if talkgroup_ids:
            clauses.append("q.destination_id = ANY(%s)")
            params.append(talkgroup_ids)
        params.extend([limit, offset])
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT q.session_id, q.source_id, q.source_call, q.source_name,
                       q.destination_id, q.destination_call,
                       {NAMED_DESTINATION_SQL} AS destination_name,
                       COALESCE(t.country, 'XX') AS country,
                       COALESCE(t.full_country_name, 'Other') AS full_country_name,
                       COALESCE(t.continent, 'Other') AS continent,
                       q.context_id, q.link_call, q.link_name, q.slot, q.start_at,
                       q.stop_at, q.duration_ms, q.talker_alias
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE {' AND '.join(clauses)}
                ORDER BY start_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def summary(
        self,
        start_time: datetime | None = None,
        continent: str | None = None,
        country: str | None = None,
        talkgroup: int | Sequence[int] | None = None,
        callsign: str | None = None,
    ) -> dict[str, Any]:
        clauses = [f"{NAMED_DESTINATION_SQL} IS NOT NULL"]
        params: list[Any] = []
        if start_time is not None:
            clauses.insert(0, "q.start_at >= %s")
            params.append(start_time)
        if continent and continent != "All":
            clauses.append("COALESCE(t.continent, 'Other') = %s")
            params.append(continent)
        if country:
            clauses.append("COALESCE(t.country, 'XX') = %s")
            params.append(country)
        if callsign and callsign.strip():
            clauses.append("q.source_call ILIKE %s")
            params.append(f"%{callsign.strip()}%")
        talkgroup_ids = _talkgroup_ids(talkgroup)
        if talkgroup_ids:
            clauses.append("q.destination_id = ANY(%s)")
            params.append(talkgroup_ids)
        where_clause = "WHERE " + " AND ".join(clauses)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms,
                       COUNT(DISTINCT q.source_id)::bigint AS unique_sources,
                       COUNT(DISTINCT q.destination_id)::bigint AS unique_destinations,
                       COUNT(*) FILTER (WHERE q.start_at >= now() - interval '24 hours')::bigint AS activity_24h,
                       COALESCE(SUM(q.duration_ms) FILTER (WHERE q.start_at >= now() - interval '24 hours'), 0)::bigint AS duration_24h_ms,
                       MIN(q.start_at) AS first_qso_at,
                       MAX(q.start_at) AS last_qso_at
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                {where_clause}
                """,
                tuple(params),
            )
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
        result = dict(zip(columns, row))
        result["duration_seconds"] = result.pop("duration_ms") / 1000
        result["duration_24h_seconds"] = result.pop("duration_24h_ms") / 1000
        return result

    def activity_histogram(
        self,
        start_time: datetime,
        bucket_seconds: int,
        continent: str | None = None,
        country: str | None = None,
        talkgroup: int | Sequence[int] | None = None,
        callsign: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return fixed-width QSO histogram buckets, including empty buckets."""
        bucket_seconds = min(max(int(bucket_seconds), 60), 7 * 24 * 60 * 60)
        clauses = [
            "q.start_at >= %s",
            "q.destination_id IS NOT NULL",
            "q.destination_id <> 9",
            f"{NAMED_DESTINATION_SQL} IS NOT NULL",
        ]
        params: list[Any] = [bucket_seconds, start_time]
        if callsign and callsign.strip():
            clauses.append("q.source_call ILIKE %s")
            params.append(f"%{callsign.strip()}%")
        if continent and continent != "All":
            clauses.append("COALESCE(t.continent, 'Other') = %s")
            params.append(continent)
        if country:
            clauses.append("COALESCE(t.country, 'XX') = %s")
            params.append(country)
        talkgroup_ids = _talkgroup_ids(talkgroup)
        if talkgroup_ids:
            clauses.append("q.destination_id = ANY(%s)")
            params.append(talkgroup_ids)
        where_clause = " AND ".join(clauses)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH settings AS (
                    SELECT %s::double precision AS stride,
                           %s::timestamptz AS start_at
                ), buckets AS (
                    SELECT generate_series(
                        date_bin(stride * interval '1 second', start_at,
                                 TIMESTAMPTZ '1970-01-01'),
                        date_bin(stride * interval '1 second', now(),
                                 TIMESTAMPTZ '1970-01-01'),
                        stride * interval '1 second'
                    ) AS bucket
                    FROM settings
                ), activity AS (
                    SELECT date_bin(%s * interval '1 second', q.start_at,
                                    TIMESTAMPTZ '1970-01-01') AS bucket,
                           COUNT(*)::bigint AS qso_count,
                           COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms
                    FROM qsos q
                    LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                    WHERE {where_clause}
                    GROUP BY bucket
                )
                SELECT buckets.bucket,
                       COALESCE(activity.qso_count, 0)::bigint AS qso_count,
                       COALESCE(activity.duration_ms, 0)::bigint AS duration_ms
                FROM buckets
                LEFT JOIN activity USING (bucket)
                ORDER BY buckets.bucket
                """,
                [bucket_seconds, start_time, bucket_seconds, *params[1:]],
            )
            return [
                {
                    "bucket": row[0],
                    "qso_count": row[1],
                    "duration_seconds": row[2] / 1000,
                }
                for row in cursor.fetchall()
            ]

    def grouped_by_talkgroup(
        self,
        start_time: datetime,
        limit: int = 25,
        continent: str | None = None,
        country: str | None = None,
        talkgroup: int | Sequence[int] | None = None,
        callsign: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "q.start_at >= %s",
            "q.destination_id IS NOT NULL",
            "q.destination_id <> 9",
            f"{NAMED_DESTINATION_SQL} IS NOT NULL",
        ]
        params: list[Any] = [start_time]
        if continent and continent != "All":
            clauses.append("COALESCE(t.continent, 'Other') = %s")
            params.append(continent)
        if country:
            clauses.append("COALESCE(t.country, 'XX') = %s")
            params.append(country)
        if callsign and callsign.strip():
            clauses.append("q.source_call ILIKE %s")
            params.append(f"%{callsign.strip()}%")
        talkgroup_ids = _talkgroup_ids(talkgroup)
        if talkgroup_ids:
            clauses.append("q.destination_id = ANY(%s)")
            params.append(talkgroup_ids)
        params.append(min(max(limit, 1), 50))
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT q.destination_id AS talkgroup_id,
                       {NAMED_DESTINATION_SQL} AS destination_name,
                       COALESCE(t.country, 'XX') AS country,
                       COALESCE(t.full_country_name, 'Other') AS full_country_name,
                       COALESCE(t.continent, 'Other') AS continent,
                       COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS total_duration_ms,
                       COUNT(DISTINCT q.source_call)::bigint AS unique_sources,
                       MAX(q.start_at) AS last_seen_at
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE {' AND '.join(clauses)}
                GROUP BY q.destination_id, {NAMED_DESTINATION_SQL}, t.country,
                         t.full_country_name, t.continent
                ORDER BY qso_count DESC, last_seen_at DESC
                LIMIT %s
                """,
                params,
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def grouped_by_callsign(
        self,
        start_time: datetime,
        limit: int = 25,
        callsign: str | None = None,
        continent: str | None = None,
        country: str | None = None,
        talkgroup: int | Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "q.start_at >= %s",
            "q.source_call IS NOT NULL",
            "q.destination_id <> 9",
            f"{NAMED_DESTINATION_SQL} IS NOT NULL",
        ]
        params: list[Any] = [start_time]
        if callsign:
            clauses.append("q.source_call ILIKE %s")
            params.append(f"%{callsign}%")
        if continent and continent != "All":
            clauses.append("COALESCE(t.continent, 'Other') = %s")
            params.append(continent)
        if country:
            clauses.append("COALESCE(t.country, 'XX') = %s")
            params.append(country)
        talkgroup_ids = _talkgroup_ids(talkgroup)
        if talkgroup_ids:
            clauses.append("q.destination_id = ANY(%s)")
            params.append(talkgroup_ids)
        params.append(min(max(limit, 1), 50))
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT q.source_call AS callsign,
                       MAX(q.source_name) AS source_name,
                       COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS total_duration_ms,
                       COUNT(DISTINCT q.destination_id)::bigint AS unique_talkgroups,
                       MAX(q.start_at) AS last_seen_at
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE {' AND '.join(clauses)}
                GROUP BY q.source_call
                ORDER BY qso_count DESC, last_seen_at DESC
                LIMIT %s
                """,
                params,
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def active_talkgroups(
        self,
        start_time: datetime,
        continent: str | None = None,
        country: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "q.start_at >= %s",
            "q.destination_id IS NOT NULL",
            "q.destination_id <> 9",
            f"{NAMED_DESTINATION_SQL} IS NOT NULL",
        ]
        params: list[Any] = [start_time]
        if continent and continent != "All":
            clauses.append("COALESCE(t.continent, 'Other') = %s")
            params.append(continent)
        if country:
            clauses.append("COALESCE(t.country, 'XX') = %s")
            params.append(country)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT q.destination_id AS value,
                       {NAMED_DESTINATION_SQL} AS label,
                       COUNT(*)::bigint AS count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS total_duration_ms
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE {' AND '.join(clauses)}
                GROUP BY q.destination_id, {NAMED_DESTINATION_SQL}
                ORDER BY count DESC, label, value
                """,
                params,
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def continents(self) -> list[str]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT continent FROM talkgroups "
                "WHERE continent IS NOT NULL AND NULLIF(BTRIM(name), '') IS NOT NULL "
                "ORDER BY continent"
            )
            return [row[0] for row in cursor.fetchall()]

    def countries(self, continent: str | None = None) -> list[dict[str, str]]:
        clauses = ["NULLIF(BTRIM(name), '') IS NOT NULL"]
        params: list[Any] = []
        if continent and continent != "All":
            clauses.append("continent = %s")
            params.append(continent)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT country AS value, full_country_name AS label
                FROM talkgroups
                WHERE {' AND '.join(clauses)}
                ORDER BY label
                """,
                params,
            )
            return [{"value": row[0], "label": row[1]} for row in cursor.fetchall()]

    def talkgroups(
        self,
        continent: str | None = None,
        country: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["talkgroup_id <> 9", "NULLIF(BTRIM(name), '') IS NOT NULL"]
        params: list[Any] = []
        if continent and continent != "All":
            clauses.append("continent = %s")
            params.append(continent)
        if country:
            clauses.append("country = %s")
            params.append(country)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT talkgroup_id, name, country, full_country_name, continent,
                       last_updated
                FROM talkgroups
                WHERE {' AND '.join(clauses)}
                ORDER BY talkgroup_id
                """,
                params,
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_qso(self, session_id: str) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM qsos WHERE session_id = %s",
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))

    def get_live_qso(self, session_id: str) -> dict[str, Any] | None:
        """Return one displayable QSO with current talkgroup metadata."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT q.session_id, q.source_id, q.source_call, q.source_name,
                       q.destination_id, q.destination_call,
                       {NAMED_DESTINATION_SQL} AS destination_name,
                       COALESCE(t.country, 'XX') AS country,
                       COALESCE(t.full_country_name, 'Other') AS full_country_name,
                       COALESCE(t.continent, 'Other') AS continent,
                       q.context_id, q.link_call, q.link_name, q.slot,
                       q.start_at, q.stop_at, q.duration_ms, q.talker_alias
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE q.session_id = %s
                  AND {NAMED_DESTINATION_SQL} IS NOT NULL
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))

    def create_user(
        self,
        callsign: str,
        name: str,
        email: str,
        password_hash: str,
    ) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (callsign, name, email, password_hash, is_active)
                VALUES (%s, %s, %s, %s, FALSE)
                RETURNING id, callsign, name, email, is_active, created_at,
                          last_login_at, email_verified_at
                """,
                (callsign, name, email, password_hash),
            )
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))

    def create_email_verification(
        self,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM email_verification_tokens WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO email_verification_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, token_hash, expires_at),
            )

    def delete_user(self, user_id: int) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))

    def verify_email_token(self, token_hash: str) -> dict[str, Any] | None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE email_verification_tokens
                    SET consumed_at = now()
                    WHERE token_hash = %s
                      AND consumed_at IS NULL
                      AND expires_at > now()
                    RETURNING user_id
                    """,
                    (token_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                cursor.execute(
                    """
                    UPDATE users
                    SET is_active = TRUE, email_verified_at = now()
                    WHERE id = %s
                    RETURNING id, callsign, name, email, is_active,
                              created_at, last_login_at, email_verified_at
                    """,
                    (row[0],),
                )
                user = cursor.fetchone()
                if user is None:
                    return None
                columns = [column.name for column in cursor.description]
                return dict(zip(columns, user))

    def create_password_reset(
        self,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, token_hash, expires_at),
            )

    def create_email_change(
        self,
        user_id: int,
        old_email: str,
        new_email: str,
        old_token_hash: str,
        expires_at: datetime,
    ) -> None:
        """Start a new two-address email change confirmation flow."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM email_change_requests WHERE user_id = %s",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO email_change_requests
                    (user_id, old_email, new_email, old_token_hash, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, old_email, new_email, old_token_hash, expires_at),
            )

    def delete_email_change(self, user_id: int) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM email_change_requests WHERE user_id = %s",
                (user_id,),
            )

    def confirm_old_email_change(
        self,
        token_hash: str,
        new_token_hash: str,
    ) -> dict[str, Any] | None:
        """Consume the old-address confirmation and arm the new-address token."""
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT r.id, r.user_id, r.old_email, r.new_email,
                           u.callsign
                    FROM email_change_requests r
                    JOIN users u ON u.id = r.user_id
                    WHERE r.old_token_hash = %s
                      AND r.old_confirmed_at IS NULL
                      AND r.expires_at > now()
                      AND lower(u.email) = lower(r.old_email)
                    FOR UPDATE
                    """,
                    (token_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                cursor.execute(
                    """
                    UPDATE email_change_requests
                    SET old_confirmed_at = now(), new_token_hash = %s
                    WHERE id = %s
                    """,
                    (new_token_hash, row[0]),
                )
                return {
                    "id": row[0],
                    "user_id": row[1],
                    "old_email": row[2],
                    "new_email": row[3],
                    "callsign": row[4],
                }

    def confirm_new_email_change(self, token_hash: str) -> dict[str, Any] | None:
        """Apply a new email only after both confirmation links were used."""
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT r.id, r.user_id, r.new_email, u.callsign
                    FROM email_change_requests r
                    JOIN users u ON u.id = r.user_id
                    WHERE r.new_token_hash = %s
                      AND r.old_confirmed_at IS NOT NULL
                      AND r.new_confirmed_at IS NULL
                      AND r.expires_at > now()
                    FOR UPDATE
                    """,
                    (token_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                cursor.execute(
                    """
                    SELECT 1 FROM users
                    WHERE lower(email) = lower(%s) AND id <> %s
                    """,
                    (row[2], row[1]),
                )
                if cursor.fetchone() is not None:
                    return {
                        "error": "duplicate",
                        "user_id": row[1],
                        "callsign": row[3],
                    }
                cursor.execute(
                    """
                    UPDATE users
                    SET email = %s
                    WHERE id = %s
                    RETURNING id, callsign, name, email, is_active,
                              created_at, last_login_at, email_verified_at
                    """,
                    (row[2], row[1]),
                )
                user = cursor.fetchone()
                if user is None:
                    return None
                columns = [column.name for column in cursor.description]
                cursor.execute(
                    """
                    UPDATE email_change_requests
                    SET new_confirmed_at = now()
                    WHERE id = %s
                    """,
                    (row[0],),
                )
                cursor.execute(
                    "DELETE FROM email_change_requests WHERE id = %s",
                    (row[0],),
                )
                return dict(zip(columns, user))

    def password_reset_user(self, token_hash: str) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.*
                FROM password_reset_tokens p
                JOIN users u ON u.id = p.user_id
                WHERE p.token_hash = %s
                  AND p.consumed_at IS NULL
                  AND p.expires_at > now()
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))

    def reset_password(
        self,
        token_hash: str,
        password_hash: str,
    ) -> dict[str, Any] | None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE password_reset_tokens
                    SET consumed_at = now()
                    WHERE token_hash = %s
                      AND consumed_at IS NULL
                      AND expires_at > now()
                    RETURNING user_id
                    """,
                    (token_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                cursor.execute(
                    """
                    UPDATE users
                    SET password_hash = %s
                    WHERE id = %s
                    RETURNING id, callsign, name, email, is_active,
                              created_at, last_login_at, email_verified_at
                    """,
                    (password_hash, row[0]),
                )
                user = cursor.fetchone()
                if user is None:
                    return None
                columns = [column.name for column in cursor.description]
                return dict(zip(columns, user))

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (email,))
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))

    def user_by_login(self, login: str) -> dict[str, Any] | None:
        """Find a user by either email or callsign, matching both app generations."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM users
                WHERE lower(email) = lower(%s) OR lower(callsign) = lower(%s)
                LIMIT 1
                """,
                (login, login),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))

    def user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, callsign, name, email, is_active, created_at, last_login_at FROM users WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))

    def user_by_session(
        self,
        token_hash: str,
        inactivity_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            if inactivity_seconds is None:
                cursor.execute(
                    """
                    SELECT u.id, u.callsign, u.name, u.email, u.is_active,
                           u.created_at, u.last_login_at, u.password_hash
                    FROM user_sessions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.token_hash = %s
                      AND s.expires_at > now()
                      AND u.is_active = TRUE
                    """,
                    (token_hash,),
                )
            else:
                cursor.execute(
                    """
                    UPDATE user_sessions s
                    SET expires_at = now() + (%s * INTERVAL '1 second')
                    FROM users u
                    WHERE s.token_hash = %s
                      AND s.expires_at > now()
                      AND u.id = s.user_id
                      AND u.is_active = TRUE
                    RETURNING s.user_id
                    """,
                    (inactivity_seconds, token_hash),
                )
                session_row = cursor.fetchone()
                if session_row is None:
                    return None
                cursor.execute(
                    """
                    SELECT id, callsign, name, email, is_active,
                           created_at, last_login_at, password_hash
                    FROM users
                    WHERE id = %s
                    """,
                    (session_row[0],),
                )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column.name for column in cursor.description]
            return dict(zip(columns, row))

    def create_session(self, user_id: int, token_hash: str, expires_at: datetime) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_sessions WHERE expires_at <= now()")
            cursor.execute(
                "INSERT INTO user_sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
                (user_id, token_hash, expires_at),
            )

    def delete_session(self, token_hash: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_sessions WHERE token_hash = %s", (token_hash,))

    def expire_user_sessions(self, user_id: int) -> int:
        """Expire every active login session belonging to one user."""
        with self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_sessions WHERE user_id = %s", (user_id,))
            return int(cursor.rowcount)

    def mark_user_login(self, user_id: int) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))

    def user_statistics(self, callsign: str) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms,
                       COUNT(DISTINCT q.destination_id)::bigint AS unique_talkgroups,
                       MIN(q.start_at) AS first_qso_at,
                       MAX(q.start_at) AS last_qso_at
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE lower(q.source_call) = lower(%s)
                  AND {NAMED_DESTINATION_SQL} IS NOT NULL
                """,
                (callsign,),
            )
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
            stats = dict(zip(columns, row))
            stats["duration_seconds"] = stats.pop("duration_ms") / 1000
            cursor.execute(
                f"""
                SELECT q.destination_id AS talkgroup_id,
                       {NAMED_DESTINATION_SQL} AS name,
                       COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE lower(q.source_call) = lower(%s)
                  AND {NAMED_DESTINATION_SQL} IS NOT NULL
                GROUP BY q.destination_id, {NAMED_DESTINATION_SQL}
                ORDER BY qso_count DESC
                LIMIT 10
                """,
                (callsign,),
            )
            stats["top_talkgroups"] = [
                {
                    "talkgroup_id": row[0],
                    "name": row[1],
                    "qso_count": row[2],
                    "duration_seconds": row[3] / 1000,
                }
                for row in cursor.fetchall()
            ]
            return stats

    def user_report(
        self,
        callsign: str | None,
        start_time: datetime,
        continent: str | None = None,
        country: str | None = None,
        talkgroup: int | Sequence[int] | None = None,
        histogram_bucket_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Return report-ready aggregates and detail rows for the selected filters."""
        clauses = [
            "q.start_at >= %s",
            "q.destination_id IS NOT NULL",
            "q.destination_id <> 9",
            f"{NAMED_DESTINATION_SQL} IS NOT NULL",
        ]
        params: list[Any] = [start_time]
        if callsign and callsign.strip():
            clauses.insert(1, "q.source_call ILIKE %s")
            params.append(f"%{callsign.strip()}%")
        if continent and continent != "All":
            clauses.append("COALESCE(t.continent, 'Other') = %s")
            params.append(continent)
        if country:
            clauses.append("COALESCE(t.country, 'XX') = %s")
            params.append(country)
        talkgroup_ids = _talkgroup_ids(talkgroup)
        if talkgroup_ids:
            clauses.append("q.destination_id = ANY(%s)")
            params.append(talkgroup_ids)
        where_clause = " AND ".join(clauses)

        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms,
                       COUNT(DISTINCT q.destination_id)::bigint AS unique_talkgroups,
                       COUNT(DISTINCT q.start_at::date)::bigint AS active_days,
                       MIN(q.start_at) AS first_qso_at,
                       MAX(q.start_at) AS last_qso_at
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE {where_clause}
                """,
                params,
            )
            columns = [column.name for column in cursor.description]
            summary = dict(zip(columns, cursor.fetchone()))
            summary["duration_seconds"] = summary.pop("duration_ms") / 1000

            cursor.execute(
                f"""
                SELECT q.start_at::date AS day,
                       COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE {where_clause}
                GROUP BY q.start_at::date
                ORDER BY day
                """,
                params,
            )
            daily = [
                {"day": row[0], "qso_count": row[1], "duration_seconds": row[2] / 1000}
                for row in cursor.fetchall()
            ]

            cursor.execute(
                f"""
                SELECT q.destination_id AS talkgroup_id,
                       {NAMED_DESTINATION_SQL} AS name,
                       COALESCE(t.country, 'XX') AS country,
                       COALESCE(t.full_country_name, 'Other') AS full_country_name,
                       COALESCE(t.continent, 'Other') AS continent,
                       COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms,
                       MAX(q.start_at) AS last_seen_at
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE {where_clause}
                GROUP BY q.destination_id, {NAMED_DESTINATION_SQL}, t.country,
                         t.full_country_name, t.continent
                ORDER BY qso_count DESC, last_seen_at DESC
                """,
                params,
            )
            talkgroups = [
                {
                    "talkgroup_id": row[0],
                    "name": row[1],
                    "country": row[2],
                    "full_country_name": row[3],
                    "continent": row[4],
                    "qso_count": row[5],
                    "duration_seconds": row[6] / 1000,
                    "last_seen_at": row[7],
                }
                for row in cursor.fetchall()
            ]

            cursor.execute(
                f"""
                SELECT q.source_call AS callsign,
                       MAX(q.source_name) AS source_name,
                       STRING_AGG(
                           DISTINCT COALESCE(t.full_country_name, 'Other'),
                           ', ' ORDER BY COALESCE(t.full_country_name, 'Other')
                       ) AS countries,
                       COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms,
                       COUNT(DISTINCT q.destination_id)::bigint AS unique_talkgroups,
                       MAX(q.start_at) AS last_seen_at
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE {where_clause}
                  AND q.source_call IS NOT NULL
                GROUP BY q.source_call
                ORDER BY qso_count DESC, last_seen_at DESC
                """,
                params,
            )
            callsigns = [
                {
                    "callsign": row[0],
                    "source_name": row[1],
                    "countries": row[2],
                    "qso_count": row[3],
                    "duration_seconds": row[4] / 1000,
                    "unique_talkgroups": row[5],
                    "last_seen_at": row[6],
                }
                for row in cursor.fetchall()
            ]

        histogram = self.activity_histogram(
            start_time,
            histogram_bucket_seconds,
            continent,
            country,
            talkgroup,
            callsign,
        )
        return {
            "summary": summary,
            "daily": daily,
            "histogram": histogram,
            "talkgroups": talkgroups,
            "callsigns": callsigns,
        }

    def admin_statistics(self) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)::bigint AS total_users,
                       COUNT(*) FILTER (WHERE is_active)::bigint AS active_users,
                       COUNT(*) FILTER (WHERE NOT is_active)::bigint AS inactive_users,
                       MIN(created_at) AS first_registered_at,
                       MAX(created_at) AS last_registered_at
                FROM users
                """
            )
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
            result = dict(zip(columns, row))
            cursor.execute(
                f"""
                SELECT COUNT(*)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms), 0)::bigint AS duration_ms,
                       COUNT(DISTINCT q.source_id)::bigint AS unique_sources,
                       COUNT(DISTINCT q.destination_id)::bigint AS unique_talkgroups
                FROM qsos q
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                WHERE q.start_at >= now() - interval '24 hours'
                  AND {NAMED_DESTINATION_SQL} IS NOT NULL
                """
            )
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
            result.update(dict(zip(columns, row)))
            result["duration_seconds"] = result.pop("duration_ms") / 1000
            return result

    def status_snapshot(self, collector_stale_after_seconds: int) -> dict[str, Any]:
        """Return the operational state needed by the public status endpoint."""
        stale_after_seconds = max(int(collector_stale_after_seconds), 1)
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("database health check failed")
            cursor.execute(
                """
                SELECT now() AS database_time,
                       (SELECT COUNT(*)::bigint FROM raw_events) AS raw_events,
                       (SELECT COUNT(*)::bigint FROM qsos) AS qsos,
                       (SELECT COUNT(DISTINCT s.user_id)::bigint
                          FROM user_sessions s
                          JOIN users u ON u.id = s.user_id
                         WHERE s.expires_at > now()
                           AND u.is_active) AS active_users,
                       (SELECT MAX(last_seen_at)
                          FROM service_heartbeats
                         WHERE service_name = 'collector') AS collector_last_seen
                """
            )
            columns = [column.name for column in cursor.description]
            result = dict(zip(columns, cursor.fetchone()))

        database_time = result["database_time"]
        collector_last_seen = result["collector_last_seen"]
        collector_age_seconds: float | None = None
        if collector_last_seen is not None:
            collector_age_seconds = max(
                0.0,
                (database_time - collector_last_seen).total_seconds(),
            )
        collector_healthy = (
            collector_age_seconds is not None
            and collector_age_seconds <= stale_after_seconds
        )
        return {
            "status": "ok" if collector_healthy else "degraded",
            "database": {
                "status": "healthy",
                "connection": "ok",
                "server_time": database_time,
            },
            "collector": {
                "status": "healthy" if collector_healthy else "unhealthy",
                "last_seen_at": collector_last_seen,
                "age_seconds": collector_age_seconds,
                "stale_after_seconds": stale_after_seconds,
            },
            "tables": {
                "raw_events": int(result["raw_events"]),
                "qsos": int(result["qsos"]),
            },
            "active_users": int(result["active_users"]),
        }

    def postgres_overview(self) -> dict[str, Any]:
        """Return safe, aggregate PostgreSQL diagnostics for the admin UI."""
        table_names = [
            "raw_events",
            "qsos",
            "talkgroups",
            "users",
            "user_sessions",
            "service_heartbeats",
        ]
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT current_database() AS database_name,
                       current_user AS database_user,
                       version() AS version,
                       pg_postmaster_start_time() AS server_started_at,
                       now() AS server_time,
                       pg_size_pretty(pg_database_size(current_database())) AS database_size,
                       pg_database_size(current_database())::bigint AS database_size_bytes,
                       COUNT(*) FILTER (WHERE state = 'active')::bigint AS active_connections,
                       COUNT(*)::bigint AS total_connections
                FROM pg_stat_activity
                WHERE datname = current_database()
                """
            )
            overview = dict(zip((column.name for column in cursor.description), cursor.fetchone()))
            cursor.execute(
                """
                SELECT relname AS table_name,
                       n_live_tup::bigint AS estimated_rows,
                       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
                       pg_total_relation_size(relid)::bigint AS total_size_bytes
                FROM pg_stat_user_tables
                WHERE relname = ANY(%s)
                ORDER BY relname
                """,
                (table_names,),
            )
            columns = [column.name for column in cursor.description]
            overview["tables"] = [dict(zip(columns, row)) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT COALESCE(state, 'unknown') AS state, COUNT(*)::bigint AS count
                FROM pg_stat_activity
                WHERE datname = current_database()
                GROUP BY state
                ORDER BY state
                """
            )
            overview["connection_states"] = [
                {"state": row[0], "count": row[1]} for row in cursor.fetchall()
            ]
        return overview

    def analyze_postgres(self) -> None:
        """Refresh planner statistics for the application tables."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "ANALYZE raw_events, qsos, talkgroups, users, user_sessions, service_heartbeats"
            )

    @staticmethod
    def _validate_retention_months(months: int) -> int:
        months = int(months)
        if months not in {1, 2, 3, 6}:
            raise ValueError("retention period must be 1, 2, 3, or 6 months")
        return months

    def retention_counts(self, months: int) -> dict[str, int]:
        """Return records eligible for the selected retention cleanup."""
        months = self._validate_retention_months(months)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(DISTINCT r.id)::bigint,
                       COUNT(q.session_id)::bigint
                FROM raw_events r
                LEFT JOIN qsos q ON q.raw_event_id = r.id
                WHERE r.received_at < now() - (%s * interval '1 month')
                """,
                (months,),
            )
            raw_events, dependent_qsos = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*)::bigint
                FROM qsos
                WHERE start_at < now() - (%s * interval '1 month')
                """,
                (months,),
            )
            qsos = cursor.fetchone()[0]
        return {
            "raw_events": int(raw_events),
            "dependent_qsos": int(dependent_qsos),
            "qsos": int(qsos),
        }

    def maintenance_overview(self) -> dict[str, int]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*)::bigint FROM raw_events),
                    (SELECT COUNT(*)::bigint FROM qsos)
                """
            )
            raw_events, qsos = cursor.fetchone()
        return {"raw_events": int(raw_events), "qsos": int(qsos)}

    def rebuild_qsos_from_raw_events(self, kerchunk_threshold_seconds: float) -> dict[str, int]:
        """Rebuild displayable QSOs from raw events in one set-based SQL operation."""
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*)::bigint FROM raw_events"
                )
                raw_events_scanned = int(cursor.fetchone()[0])
                cursor.execute("DELETE FROM qsos")

                cursor.execute(
                    """
                    WITH decoded AS (
                        SELECT r.id AS raw_event_id,
                               r.session_id,
                               r.start_at,
                               r.stop_at,
                               r.payload,
                               NULLIF(COALESCE(NULLIF(r.payload->>'SourceID', ''),
                                               NULLIF(r.payload->>'source_id', '')), '')::bigint AS source_id,
                               NULLIF(COALESCE(NULLIF(r.payload->>'SourceCall', ''),
                                               NULLIF(r.payload->>'source_call', '')), '') AS source_call,
                               NULLIF(COALESCE(NULLIF(r.payload->>'SourceName', ''),
                                               NULLIF(r.payload->>'source_name', '')), '') AS source_name,
                               NULLIF(COALESCE(NULLIF(r.payload->>'DestinationID', ''),
                                               NULLIF(r.payload->>'destination_id', '')), '')::bigint AS destination_id,
                               NULLIF(COALESCE(NULLIF(r.payload->>'DestinationCall', ''),
                                               NULLIF(r.payload->>'destination_call', '')), '') AS destination_call,
                               NULLIF(COALESCE(NULLIF(r.payload->>'DestinationName', ''),
                                               NULLIF(r.payload->>'destination_name', '')), '') AS destination_name,
                               NULLIF(COALESCE(NULLIF(r.payload->>'ContextID', ''),
                                               NULLIF(r.payload->>'context_id', '')), '')::bigint AS context_id,
                               NULLIF(COALESCE(NULLIF(r.payload->>'LinkCall', ''),
                                               NULLIF(r.payload->>'link_call', '')), '') AS link_call,
                               NULLIF(COALESCE(NULLIF(r.payload->>'LinkName', ''),
                                               NULLIF(r.payload->>'link_name', '')), '') AS link_name,
                               NULLIF(COALESCE(NULLIF(r.payload->>'LinkTypeName', ''),
                                               NULLIF(r.payload->>'link_type_name', '')), '') AS link_type_name,
                               NULLIF(COALESCE(NULLIF(r.payload->>'Slot', ''),
                                               NULLIF(r.payload->>'slot', '')), '')::smallint AS slot,
                               NULLIF(COALESCE(NULLIF(r.payload->>'Master', ''),
                                               NULLIF(r.payload->>'master', '')), '') AS master,
                               NULLIF(COALESCE(NULLIF(r.payload->>'TalkerAlias', ''),
                                               NULLIF(r.payload->>'talker_alias', '')), '') AS talker_alias,
                               NULLIF(COALESCE(NULLIF(r.payload->>'RSSI', ''),
                                               NULLIF(r.payload->>'rssi', '')), '')::real AS rssi,
                               NULLIF(COALESCE(NULLIF(r.payload->>'BER', ''),
                                               NULLIF(r.payload->>'ber', '')), '')::real AS ber,
                               COALESCE(NULLIF(r.payload->>'SessionID', ''),
                                        NULLIF(r.payload->>'session_id', '')) IS NOT NULL AS is_native_session_id
                        FROM raw_events r
                        WHERE lower(r.event_type) = 'session-stop'
                          AND r.start_at IS NOT NULL
                          AND r.stop_at IS NOT NULL
                          AND r.stop_at >= r.start_at + (%s * interval '1 second')
                    ), candidates AS (
                        SELECT DISTINCT ON (d.session_id) d.*
                        FROM decoded d
                        LEFT JOIN talkgroups t ON t.talkgroup_id = d.destination_id
                        WHERE d.destination_id IS NOT NULL
                          AND d.destination_id <> %s
                          AND d.destination_id <= %s
                          AND (
                              NULLIF(BTRIM(d.destination_name), '') IS NOT NULL
                              OR NULLIF(BTRIM(t.name), '') IS NOT NULL
                          )
                        ORDER BY d.session_id, d.raw_event_id DESC
                    )
                    INSERT INTO qsos
                        (session_id, raw_event_id, source_id, source_call,
                         source_name, destination_id, destination_call,
                         destination_name, context_id, link_call, link_name,
                         link_type_name, slot, master, talker_alias, rssi, ber,
                         start_at, stop_at, duration_ms, is_native_session_id,
                         payload)
                    SELECT session_id, raw_event_id, source_id, source_call,
                           source_name, destination_id, destination_call,
                           destination_name, context_id, link_call, link_name,
                           link_type_name, slot, master, talker_alias, rssi, ber,
                           start_at, stop_at,
                           ROUND(EXTRACT(EPOCH FROM (stop_at - start_at)) * 1000)::integer,
                           is_native_session_id, payload
                    FROM candidates
                    ON CONFLICT (session_id) DO UPDATE SET
                        raw_event_id = EXCLUDED.raw_event_id,
                        source_id = EXCLUDED.source_id,
                        source_call = EXCLUDED.source_call,
                        source_name = EXCLUDED.source_name,
                        destination_id = EXCLUDED.destination_id,
                        destination_call = EXCLUDED.destination_call,
                        destination_name = EXCLUDED.destination_name,
                        context_id = EXCLUDED.context_id,
                        link_call = EXCLUDED.link_call,
                        link_name = EXCLUDED.link_name,
                        link_type_name = EXCLUDED.link_type_name,
                        slot = EXCLUDED.slot,
                        master = EXCLUDED.master,
                        talker_alias = EXCLUDED.talker_alias,
                        rssi = EXCLUDED.rssi,
                        ber = EXCLUDED.ber,
                        start_at = EXCLUDED.start_at,
                        stop_at = EXCLUDED.stop_at,
                        duration_ms = EXCLUDED.duration_ms,
                        is_native_session_id = EXCLUDED.is_native_session_id,
                        payload = EXCLUDED.payload
                    """,
                    (
                        float(kerchunk_threshold_seconds),
                        DISPLAY_EXCLUDED_DESTINATION_ID,
                        MAX_TALKGROUP_ID,
                    ),
                )
                eligible_qsos = max(0, cursor.rowcount)

                cursor.execute("SELECT COUNT(*)::bigint FROM qsos")
                qso_count = int(cursor.fetchone()[0])

        self._compact_tables("qsos")
        return {
            "raw_events_scanned": raw_events_scanned,
            "eligible_qsos": eligible_qsos,
            "qsos_rebuilt": qso_count,
        }

    def clear_old_raw_events(self, months: int) -> dict[str, int]:
        """Delete old raw events and their dependent QSOs, then compact both tables."""
        months = self._validate_retention_months(months)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM qsos
                    WHERE raw_event_id IN (
                        SELECT id
                        FROM raw_events
                        WHERE received_at < now() - (%s * interval '1 month')
                    )
                    """,
                    (months,),
                )
                qsos_deleted = cursor.rowcount
                cursor.execute(
                    """
                    DELETE FROM raw_events
                    WHERE received_at < now() - (%s * interval '1 month')
                    """,
                    (months,),
                )
                raw_events_deleted = cursor.rowcount

        self._compact_tables("raw_events", "qsos")
        return {
            "months": months,
            "raw_events_deleted": int(raw_events_deleted),
            "qsos_deleted": int(qsos_deleted),
        }

    def clear_old_qsos(self, months: int) -> dict[str, int]:
        """Delete old QSOs and compact the QSO table."""
        months = self._validate_retention_months(months)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM qsos
                    WHERE start_at < now() - (%s * interval '1 month')
                    """,
                    (months,),
                )
                qsos_deleted = cursor.rowcount

        self._compact_tables("qsos")
        return {"months": months, "qsos_deleted": int(qsos_deleted)}

    def _compact_tables(self, *table_names: str) -> None:
        allowed = {"raw_events", "qsos"}
        for table_name in table_names:
            if table_name not in allowed:
                raise ValueError("unsupported table for compaction")
            self.connection.execute(f"VACUUM (FULL, ANALYZE) {table_name}")

    def list_users(self) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT u.id, u.callsign, u.name, u.email, u.is_active,
                       u.created_at, u.last_login_at,
                       COUNT(q.session_id) FILTER (WHERE {NAMED_DESTINATION_SQL} IS NOT NULL)::bigint AS qso_count,
                       COALESCE(SUM(q.duration_ms) FILTER (WHERE {NAMED_DESTINATION_SQL} IS NOT NULL), 0)::bigint AS duration_ms,
                       COUNT(DISTINCT q.destination_id) FILTER (WHERE {NAMED_DESTINATION_SQL} IS NOT NULL)::bigint AS unique_talkgroups
                FROM users u
                LEFT JOIN qsos q ON lower(q.source_call) = lower(u.callsign)
                LEFT JOIN talkgroups t ON t.talkgroup_id = q.destination_id
                GROUP BY u.id
                ORDER BY u.created_at DESC
                """
            )
            columns = [column.name for column in cursor.description]
            users = []
            for row in cursor.fetchall():
                user = dict(zip(columns, row))
                user["duration_seconds"] = user.pop("duration_ms") / 1000
                users.append(user)
            return users

    def set_user_active(self, user_id: int, active: bool) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET is_active = %s WHERE id = %s RETURNING id",
                (active, user_id),
            )
            return cursor.fetchone() is not None

    def delete_user(self, user_id: int) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE id = %s RETURNING id", (user_id,))
            return cursor.fetchone() is not None
