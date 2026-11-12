"""Single-round-trip aggregation for the dashboard payload.

The dashboard needs several answers over the same filtered range: a summary,
the first and last timestamps, two top-N lists and a histogram. Reading each
with its own query scans the range independently, so this module compiles the
filter into one ``WITH`` clause and derives every answer from that single
materialised set.

The filter itself is not re-implemented here. ``filtered_qsos()`` compiles to
SQL that already carries Django's exact semantics -- the ``UPPER(..) LIKE
UPPER(%s)`` form of ``icontains`` with its wildcards escaped into the
parameter, the LEFT/INNER join the continent and country filters require, and
the ``IN`` list for talkgroups. That statement becomes the CTE body, so the
fast path cannot drift from the ORM path.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

# Accepted work_mem spellings pass through to PostgreSQL; anything else is
# dropped rather than sent, so a typo cannot take the dashboard down.
_WORK_MEM_PATTERN = re.compile(r"^(?!0+\s*(kB|MB|GB)?$)\d{1,7}(kB|MB|GB)?$")

# Values of source_call that are not a real station. Mirrors the
# ``valid_callsign`` predicate that previously lived as Q objects in core_views.
INVALID_EMPTY = ""
INVALID_UNKNOWN = "unknown"
INVALID_PLACEHOLDER = "\u2014"

_VALID_CALLSIGN = (
    "source_call IS NOT NULL"
    " AND source_call <> %s"
    " AND UPPER(source_call) <> UPPER(%s)"
    " AND source_call <> %s"
)

_SQL = f"""
WITH r AS MATERIALIZED (
    {{base}}
),
agg AS (
    SELECT
        count(*) AS qso_count,
        COALESCE(sum(duration_ms), 0) AS duration_ms,
        count(DISTINCT source_call) FILTER (WHERE {_VALID_CALLSIGN}) AS unique_sources,
        count(DISTINCT talkgroup_id) AS unique_talkgroups,
        max(start_at) AS last_qso_at,
        min(start_at) AS first_qso_at,
        count(*) FILTER (WHERE start_at >= %s) AS activity_24h
    FROM r
),
tg AS (
    -- Group on the key alone, then attach the names for the surviving rows.
    -- talkgroup_id is unique, so this yields the same groups as grouping on
    -- the id plus its metadata, without sorting 250k rows of text.
    SELECT
        g.tg_id,
        t.name AS tg_name,
        t.country AS tg_country,
        t.full_country_name AS tg_fcc,
        t.continent AS tg_continent,
        g.qso_count,
        g.duration_ms,
        g.src_count,
        g.last_seen
    FROM (
        SELECT
            talkgroup_id AS tg_id,
            count(*) AS qso_count,
            sum(duration_ms) AS duration_ms,
            count(DISTINCT source_call) AS src_count,
            max(start_at) AS last_seen
        FROM r
        GROUP BY talkgroup_id
    ) g
    LEFT JOIN talkgroups t ON t.talkgroup_id = g.tg_id
    ORDER BY g.qso_count DESC, g.tg_id ASC
    LIMIT %s
),
cs AS (
    SELECT
        source_call,
        source_name,
        count(*) AS qso_count,
        sum(duration_ms) AS duration_ms,
        count(DISTINCT talkgroup_id) AS tg_count
    FROM r
    WHERE {_VALID_CALLSIGN}
    GROUP BY source_call, source_name
    ORDER BY qso_count DESC, source_call ASC
    LIMIT %s
),
hist AS (
    SELECT idx, count(*) AS n
    FROM (
        SELECT floor(extract(epoch FROM (start_at - %s::timestamptz)) / %s)::bigint AS idx
        FROM r
    ) buckets
    WHERE idx >= 0 AND idx < %s
    GROUP BY idx
)
SELECT
    (SELECT row_to_json(a) FROM agg a) AS agg,
    (SELECT COALESCE(json_agg(t), '[]'::json) FROM tg t) AS talkgroups,
    (SELECT COALESCE(json_agg(c), '[]'::json) FROM cs c) AS callsigns,
    (SELECT COALESCE(json_agg(h), '[]'::json) FROM hist h) AS histogram
"""


def _base_statement(qsos):
    """The filtered range as a composable statement carrying Django's params."""
    # Only the columns the aggregates need are carried through the CTE. The
    # talkgroup names are joined back for the top-N list instead of being
    # materialised for every row: at 245k rows a week, four wide text columns
    # nearly double the size of the materialised set.
    #
    # QSO.Meta orders by -start_at; carrying that into the CTE would sort the
    # entire range for no reason, so the ordering is cleared.
    rows = qsos.values(
        "start_at",
        "source_call",
        "source_name",
        "talkgroup_id",
        "duration_ms",
    ).order_by()
    return rows.query.sql_with_params()


def isoformat_or_none(value):
    """Match ``datetime.isoformat()`` exactly, including the offset form."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return datetime.fromisoformat(value).isoformat()


def load_dashboard_stats(qsos, *, start, bucket_seconds, bucket_count, row_limit):
    """Run one query and return the raw aggregates for the dashboard."""
    base_sql, base_params = _base_statement(qsos)

    invalid = (INVALID_EMPTY, INVALID_UNKNOWN, INVALID_PLACEHOLDER)
    # Parameters in the same order as the %s placeholders above: the agg
    # valid-callsign triple, the 24h cutoff, the two limits, the cs triple,
    # then the histogram bucket origin, width and cap.
    params = [
        *base_params,
        *invalid,                              # agg: valid-callsign triple
        timezone.now() - timedelta(hours=24),  # agg: 24h activity cutoff
        row_limit,                             # tg: LIMIT
        *invalid,                              # cs: valid-callsign triple
        row_limit,                             # cs: LIMIT
        start,                                 # hist: bucket origin
        bucket_seconds,                        # hist: bucket width
        bucket_count,                          # hist: bucket cap
    ]

    sql = _SQL.format(base=base_sql)
    work_mem = str(getattr(settings, "DASHBOARD_WORK_MEM", "") or "").strip()
    if not _WORK_MEM_PATTERN.match(work_mem):
        work_mem = None

    # The two count(DISTINCT ...) sorts and the callsign grouping spill about
    # 10 MB each against the default 8 MB work_mem. Raising it for this one
    # statement removes those spills; SET LOCAL scopes it to the transaction so
    # no other query on this connection inherits it.
    with transaction.atomic():
        with connection.cursor() as cursor:
            if work_mem:
                cursor.execute("SELECT set_config('work_mem', %s, true)", [work_mem])
            cursor.execute(sql, params)
            agg, talkgroups, callsigns, histogram = cursor.fetchone()

    return {
        "summary": agg or {},
        "talkgroups": talkgroups or [],
        "callsigns": callsigns or [],
        "histogram": histogram or [],
    }
