# Periodic rollups: design and rollout plan

Date: 2026-11-13
Status: **plan only — nothing here is implemented.** Every figure marked
"measured" was read from production read-only; every figure marked "projected"
is arithmetic from those measurements and is labelled as such.

## Why this document exists

`REVIEW.md` established that the dashboard is expensive: 3,458 ms of database
time for one anonymous `timeRange=1w` request before the single-pass rewrite,
1,618 ms after it, against a table that will hold ~12.7M QSOs after a year.
Every one of those answers is an aggregate over individual QSO rows.

A rollup precomputes those aggregates at a coarser grain. That is the obvious
next step, and the obvious next step is also where most rollups fail: they are
designed around what a dashboard shows today, and then a filter is added, or a
percentage is needed, and the rollup cannot answer it and nobody notices until
a number on the page is wrong.

So this document starts from the opposite direction. It enumerates **every read
of `qsos` in the codebase**, records the shape each one needs, and only then
proposes tables — and states plainly which ones a rollup cannot serve, so that
the answer "read the base table" is chosen rather than discovered.

## The decision this document is really arguing

There are three plausible architectures:

1. **No rollup.** Keep the single-pass CTE from v1.2.13 and add a cache.
2. **A daily-grain summary per talkgroup, plus a per-callsign daily summary.**
3. **A second database** (ClickHouse, TimescaleDB, DuckDB over Parquet).

The recommendation is **2, narrowly scoped, for the long ranges only, with the
base table remaining the source of truth for anything short or interactive.**

The reason it is 2 and not 1 is not throughput — it is that a rollup is the only
answer to a problem a cache cannot touch: **retention.** `qsos` currently grows
without limit (~17.7 GB projected per year of QSO rows at today's rate, measured
table 5,158 MB for 106 days). The QSO retention buttons only *delete*; they do
not keep history. Once a rollup exists, history can be kept at a cost that does
not scale with row count, and raw rows can be aged out. That is the strategic
prize, and it is worth more than the latency.

The reason it is not 3 is in "Alternatives" below: the workload is one
read-heavy dashboard, the team is one person, and a second database is a second
backup, restore, monitoring and failure mode.

---

## Part 1 — What the queries need

Sixteen read sites reach `qsos`. They reduce to eight distinct shapes.

### S1. Live feed — every second, per viewer

`live_views.live_qsos_api` and `live_consumers`, both
`filter(stop_at >= now() - 3 min).order_by("-stop_at")[:100]`.

**Grain needed:** individual rows, with `stop_at`.
**Rollup can serve it?** No — and it must not try. This is 0.3 ms on the
`stop_at` index (H5, v1.2.6). Replacing it with an aggregate would be slower.

### S2. LastHeard lists — paged individual rows

`public_views.lastheard` (`order_by("-start_at")[:limit]`) and the live
consumer's filtered variant.

**Grain needed:** rows, with all the descriptive columns (`source_name`,
`destination_name`, `slot`, …).
**Rollup can serve it?** No. Row-level.

### S3. Dashboard payload — summary, two top-N lists, histogram

`dashboard_stats.load_dashboard_stats`, one CTE producing:

- `qso_count`, `sum(duration_ms)`
- `count(DISTINCT source_call)` over a validity predicate
- `count(DISTINCT talkgroup_id)`
- `min(start_at)`, `max(start_at)`
- `count(*) FILTER (start_at >= now()-24h)`
- top-N talkgroups by `count(*)`, with `sum(duration_ms)`,
  `count(DISTINCT source_call)` and `max(start_at)` per talkgroup
- top-N callsigns by `count(*)`, with `sum(duration_ms)` and
  `count(DISTINCT talkgroup_id)` per `(source_call, source_name)`
- histogram: `count(*)` bucketed by `floor(epoch(start_at - origin) / width)`

Filters: time range, `source_call ILIKE %c%`, talkgroup continent, talkgroup
country, talkgroup id `IN (…)`.

**Rollup can serve it?** Partly — see "What a rollup cannot do" below. The
`ILIKE` filter and the arbitrary-width histogram are the two reasons it cannot
serve the whole thing.

### S4. Reports — twelve derived datasets from one range

`report_views._report_rows`. The expensive part is a Python loop over **every
row** of the range, building:

| Output | Grain |
| --- | --- |
| `daily[]` — QSOs and duration per UTC calendar day | day |
| `hourly_activity` — per hour of day | hour-of-day, summed over range |
| `weekday_activity` — per ISO weekday | weekday, summed over range |
| `histogram` — count per bucket | arbitrary width from the range |
| `concurrent_activity` — **distinct** talkgroups and **distinct** sources per bucket | arbitrary width |
| `summary` | range |
| `talkgroups` top 50 — count, duration, distinct sources, max start | range, per talkgroup |
| `callsigns` top 50 — count, duration, distinct talkgroups, max `source_name` | range, per callsign |
| `callsigns` — **distinct country per callsign** (a second query) | range, per callsign × country |
| `selector_talkgroups` / continents / countries — per-range aggregates over the **unfiltered** range | range, per talkgroup |
| `previous_count` — count over the preceding equal-length range | equal-length previous range |
| `talkgroup_growth` — per-talkgroup count over the previous range | previous range, per talkgroup |

**Rollup can serve it?** Several of these are exactly a daily rollup
(`daily[]`, `talkgroups` totals, `callsigns` totals, `previous_count`,
`talkgroup_growth`). The rest are not: see below.

### S5. Profile and account statistics

`auth_views.profile` and `auth_views.api_stats`:
`filter(source_call__iexact=callsign)` with `count`, `sum(duration_ms)`,
`count(DISTINCT talkgroup_id)`, `max(start_at)`, plus top talkgroups.

**Grain needed:** per callsign for the lifetime of the account.
**Rollup can serve it?** Yes — this is the strongest fit in the whole document,
because `iexact` on a `callsign` column is an equality on the upper-cased value.
Note the subtlety: `iexact` is case-insensitive, and the collector upper-cases
callsigns on write (`UserManager.create_user` does `callsign.upper()`; the
collector writes what BrandMeister sends). A rollup keyed on the raw
`source_call` preserves the current behaviour exactly, including the case where
BrandMeister sends a mixed-case callsign. Keying on `upper()` instead would be
a silent behaviour change.

### S6. Grouped LastHeard

`public_views.grouped_lastheard`: per talkgroup over the filtered range —
count, duration, distinct sources, max start.

**Rollup can serve it?** Yes, at day grain.

### S7. Admin retention counts

`admin_views.retention_counts`: `count()` over `start_at < cutoff` for 1/2/3/6
month cutoffs.

**Rollup can serve it?** Yes — and it is cheap either way on the
`start_at` index. Low priority.

### S8. `_maintenance_stats` — the 91-second scan

Reads `raw_events`, not `qsos`. Out of scope for this document; it is a
separate defect in `REVIEW.md` (M1).

### What the sixteen sites reduce to

- **Row-level (S1, S2):** 2 sites, must stay on the base table.
- **Aggregate over a bounded range (S3, S4, S6, S7):** 6 sites, the costly ones.
- **Per-callsign lifetime (S5):** 2 sites, trivially served by a rollup.
- **Writer and admin paths** (`collector`, `rebuild`, QSO deletion): unaffected
  by any read-side rollup, but they are what a rollup must be maintained after.

---

## Part 2 — What a rollup cannot do, and must therefore not pretend to

Four requirements in the query set are structurally impossible to answer from a
summary table without going back to rows. Each one has a design consequence.

### R1. `count(DISTINCT …)` does not compose

The dashboard's "unique callsigns" is `count(DISTINCT source_call)` over the
range. Its per-talkgroup list also wants `count(DISTINCT source_call)` **per
talkgroup**. Summing a per-day distinct count gives *callsign-days*, not
callsigns: a callsign active on 30 days counts 30 times.

The only exact solutions are (a) a summary at the same grain as the entity being
counted — `(day, talkgroup, source_call)` rows, so a range is a `GROUP BY` on
talkgroup with `count(DISTINCT source_call)` over distinct pairs; or (b)
HyperLogLog per bucket, which is approximate.

**Consequence:** the talkgroup rollup carries a companion `(day, talkgroup,
source_call)` table if exact per-talkgroup distinct counting is required. This is
the biggest sizing decision in the document, and it is why the sizing section
distinguishes a *summary* rollup from an *entity* rollup.

### R2. `count(DISTINCT talkgroup_id)` **does** compose

Unlike callsigns, the distinct talkgroup count is bounded by the 1,856
talkgroups and is per-day listable: a small side table of
`(day, talkgroup_id)` lets a range answer with `count(DISTINCT talkgroup_id)`
over the days. Cheap. Worth doing.

### R3. Arbitrary-width histograms defeat day grain

The histogram bucket width comes from `histogram_bucket_seconds()`, which maps
sixteen range presets to widths from 60 s to 7 days (measured, `utils.py:44-54`).
For `1w`, buckets are 6 hours; for `1M`, a day; for `3M`, 3 days. A
**day-grain** rollup can answer the day/3-day/7-day widths exactly by summing
whole days, but it can never answer a 6-hour bucket without a hierarchical
smaller grain.

**Consequence:** either (a) the histogram keeps reading the base table, or
(b) a second, hour-grain rollup exists for ranges up to a week. Option (a) is
the recommendation: the histogram is already the cheapest of the heavy queries
(32 ms on the copy, 794 ms on production, `REVIEW.md`), and the production cost
is heap-visibility, not row count — which index-only scans on a rollup would
also fix only for the rollup's own table.

### R4. `source_call ILIKE '%…%'` cannot be answered from a summary

The callsign filter is a substring match (`icontains`). A rollup grouped by
talkgroup cannot tell which of its callsigns matched a substring. Even an
entity-grain rollup can only serve it if the rollup itself is grouped by
callsign.

**Consequence:** any request carrying a `callsign` filter — which is exactly
the "advanced query" surface for registered users — reads the base table. That
is fine: `restricted_request()` already routes those to authenticated users,
and the authenticated audience is small.

### R5. The previous-range comparison crosses an arbitrary boundary

`talkgroup_growth` and `traffic_trend` compare the selected range against the
preceding equal-length range. With preset ranges ending "now", the previous
range almost never aligns to UTC midnight.

**Consequence:** a day-grain rollup answers the previous range **approximately**
by summing whole days, or exactly by also reading a partial boundary day from
the base table. The recommendation is: sum whole days from the rollup, and
accept up to one day of boundary error *on the trend line only*, documented on
the page — or read the two boundary days from the base table, which is cheap
because it is two days, not a month. Prefer the latter; it keeps the number
exact.

### The rule this produces

> A rollup answers a query **only** when the query's grain is coarser than or
> equal to the rollup's grain, and its filters are equality or set-membership on
> dimensions the rollup carries. Anything else falls back to the base table,
> and the fallback is a deliberate, tested path — not an error.

---

## Part 3 — Measured inputs

From production, read-only, 2026-11-13:

| Quantity | Value |
| --- | --- |
| QSO rows | 3,679,181 |
| Date span | 106 days (2026-07-30 → 2026-11-13) |
| Average QSOs/day | 34,709 |
| Projected QSOs/year | ~12.7M |
| `qsos` total size (heap + indexes) | 5,158 MB |
| Projected growth | ~17.8 GB/year of QSO rows |
| Distinct callsigns, all time | 35,607 |
| Distinct callsigns, last day | 3,055 |
| Talkgroups in catalogue | 1,856 |
| Talkgroups with traffic | 1,640 |

---

## Part 4 — Proposed rollups

Three tables, in ascending order of cost and descending order of value.

### T1. `rollup_talkgroup_daily` — the workhorse

```text
    day              date          -- UTC calendar day of start_at
    talkgroup_id     int
    qso_count        int
    duration_ms      bigint
    last_start_at    timestamptz
    first_start_at   timestamptz
    source_count     int           -- count(DISTINCT source_call) within the day
    PRIMARY KEY (day, talkgroup_id)
```

**Sizing (projected):** 1,640 active talkgroups × 365 days ≈ **598,600 rows/year**,
≈ **58 MB/year** at ~96 bytes/row with its index. One year of this table is
about 1.1% of one year of `qsos`.

**Serves:** S3's top talkgroups and totals (except distinct-source-per-
talkgroup, see T2), S4's `daily[]` (summed across talkgroups), `talkgroups`
top-50 totals, `previous_count` and `talkgroup_growth` (whole days plus boundary
reads), S6, S7.

**Cannot serve:** per-talkgroup distinct callsigns; anything narrower than a day.

### T2. `rollup_talkgroup_callsign_daily` — the one that makes R1 exact

```text
    day              date
    talkgroup_id     int
    source_call      text
    qso_count        int
    duration_ms      bigint
    PRIMARY KEY (day, talkgroup_id, source_call)
```

**Sizing (projected):** this is the expensive one. If each active callsign talks
on an average of *k* talkgroups per day, the row count is
`days × active_callsigns × k`. At 3,055 daily callsigns and k=1.5 that is
≈ 1.7M rows/year ≈ 160 MB/year; at k=3, ≈ 3.3M rows ≈ 330 MB. The multiplier is
unknown and should be measured before this table is created — one query against
production, not a guess.

**Serves:** exact `count(DISTINCT source_call)` per talkgroup over any range,
which is the dashboard's talkgroup list and the report's `unique_sources`.

**Decision point:** if measured *k* makes this table larger than ~500 MB/year,
prefer option (b): drop `unique_sources` from the long-range views or mark it
approximate, and keep the exact number only for ranges short enough to read the
base table. An approximate number silently presented as exact is the failure
mode this document exists to prevent.

### T3. `rollup_callsign_daily` — profile and lifetime stats

```text
    day              date
    source_call      text          -- raw, as written, matching iexact semantics
    qso_count        int
    duration_ms      bigint
    last_start_at    timestamptz
    first_start_at   timestamptz
    talkgroup_count  int           -- distinct talkgroups that day; sums are NOT totals
    PRIMARY KEY (day, source_call)
```

**Sizing (projected):** 3,055 active callsigns/day × 365 ≈ **1.1M rows/year**,
≈ **107 MB/year**.

**Serves:** S5 in full for ranges coarser than a day (lifetime), and the
report's top-callsign list totals.

**Note:** `talkgroup_count` per day is **not** summable into "distinct talkgroups
over the range" (R1 again). The profile page's "unique talkgroups" for the
lifetime therefore either reads the base table for the count, or uses a separate
`(callsign, talkgroup_id)` lifetime table — for a single callsign that is a few
thousand rows and cheap to compute on demand with an index on
`(source_call, talkgroup_id)`.

### Deliberately not proposed

- **An hour-grain rollup.** 24× the rows of T1 for one consumer — the
  histogram — that is already the cheapest heavy query. Revisit only if the
  histogram moves to the top of the latency budget.
- **A continent/country grain.** `talkgroup_id` determines continent and country
  (Talkgroup carries both, and `repair_talkgroups()` keeps them consistent), so
  filtering by continent and summing T1 by talkgroup is exact. A
  continent-grain table would duplicate that join for no gain.
- **A slot-grain table.** Slot is a filter on nothing today; adding a dimension
  before the query set asks for it is how rollups become expensive and unused.

---

## Part 5 — How the data stays correct

A rollup that is silently wrong is worse than no rollup. Four mechanisms, in
order of strength:

### 1. Boundaries are UTC calendar days, closed at the start

`day = (start_at AT TIME ZONE 'UTC')::date`, with `start_at >= day` and
`start_at < day + 1`. Half-open, like every range in `utils.range_bounds()`,
so a QSO at exactly midnight lands in one day only.

### 2. Only closed days are rolled up

The current day is always incomplete. A rollup job that includes today's partial
day produces a number that grows during the day and disagrees with the base
table on every read. **The rollup covers days strictly before today (UTC)**;
today is always read from the base table. This is the same cutoff rule the
archiver already uses (`00:00 UTC`, `OBJECT-STORAGE.md`), so the two jobs share
a vocabulary.

### 3. The rollup is a derived, rebuildable projection

`rollup_*` tables are never written by request handlers. They are written by one
management command, `rollup_qsos`, which is idempotent per day:
`INSERT … ON CONFLICT (day, …) DO UPDATE`, inside a transaction, for a list of
days. Re-running any day is safe and produces the same result. Rebuilding the
whole history is a single `--from` invocation. Nothing depends on the order in
which days were written.

### 4. Corrections flow through the collector's existing delete-and-reinsert

The collector can delete and re-insert a QSO (`collector.py:233-259`), the
rebuild rewrites history (`rebuild.py`), and the admin can delete QSOs by age.
Any of these can invalidate a closed day. Two options:

- **Recompute a window.** After a rebuild or a QSO deletion, enqueue the
  affected days for recomputation. Simple, exact, and the rebuild already knows
  its own date range.
- **Checksum reconciliation.** The verification job (below) compares rollup
  totals to base totals and flags any day that disagrees, then corrects it.

The recommendation is both: the rebuild enqueues explicitly because it knows
what it touched, and a nightly reconciliation catches everything else.

### 5. The guard that matters: a parity test against the base table

`REVIEW.md` already describes the pattern that proved the single-pass rewrite —
compare the new answer with the old one on the same data. The rollup needs the
same thing, as a test and as a periodic job:

- For a sample of days (all of them during the first weeks, then a rotating
  tenth), recompute every rollup aggregate from `qsos` and compare to the
  stored row. Any mismatch is a failure.
- At request level: the rollup path and the base-table path must return
  byte-identical JSON for the same range and filters, compared in a test the
  way `test_dashboard_single_pass.py` compares the four answers. This is the
  only check that catches a rollup that is internally consistent but does not
  match what the page used to show.

---

## Part 6 — How requests choose a path

Not a cache: a **router**, one function, one rule.

```text
def stats_source(request):
    """Rollup or base table, decided by what the request can be answered from."""
    # A callsign substring filter cannot be answered from a summary (R4).
    if request.GET.get("callsign", "").strip():
        return BASE
    # Ranges the dashboard shows live, and anything shorter than a day,
    # are cheaper on the base table than on a rollup plus boundary reads.
    if range_is_shorter_than(1 day) or range_overlaps_today():
        return BASE
    # Histograms narrower than a day cannot be served from a day rollup (R3).
    if bucket_seconds(request) < 86400:
        return BASE
    return ROLLUP
```

Two properties worth insisting on:

- **One decision point.** The dashboard, the reports and the API all call the
  router. Two call sites with their own rules is how a page and its own export
  end up disagreeing.
- **The decision is observable.** The payload reports which path answered
  (a `"source": "rollup" | "base"` field or a response header). Without it,
  nobody can tell whether the rollup is doing anything, and a silently
  always-falling-back router looks like a working optimisation.

### Where the boundary reads go

For a range that starts or ends mid-day, the rollup answers the whole days and
the base table answers at most two partial days. That is a small addition to
the existing single-pass query: union the rollup aggregates with the base-table
aggregates over the boundary intervals, then do the outer aggregation once. The
`count(DISTINCT …)` across the union is safe only where the two sides are
disjoint — whole days and partial days are, by construction.

---

## Part 7 — How it is populated

One management command, `rollup_qsos`, following the shape of the commands that
already exist (`archive_raw_events`, `cleanup_raw_events`):

```text
python manage.py rollup_qsos                    # every closed day not yet rolled up
python manage.py rollup_qsos --day 2026-11-01   # one day, idempotent
python manage.py rollup_qsos --from 2026-07-30  # backfill
python manage.py rollup_qsos --verify --sample 0.1  # recompute and compare
```

**Scheduling.** It belongs wherever the other recurring work lives. Right now
that is a Kubernetes CronJob (`fleet/bminfo/workloads.yaml`), and
`test_runtime_topology.py` already asserts that scheduled commands exist and do
not overlap. A rollup of one closed day reads ~35k rows and writes ~1.6k
(T1 + T3) — well under a second — so it can run hourly or daily. Running it
**hourly** is preferable to daily: it means a late-arriving collector write to
yesterday can be picked up within the hour, and the reconciliation window stays
small. The schedule must sit after `archive-raw-events` (01:17) in the existing
ordering assertion.

**Backfill** is the same command over a date range. At 35k rows/day the whole
five-month history is ~3.7M rows, minutes of work, not hours.

### Retention, once the rollup exists

This is the strategic payoff, and it should be an explicit decision rather than
a side effect:

- Keep `qsos` rows for a bounded period (the proposal: **13 months**, so
  year-on-year comparison on the same calendar date is possible) and let the
  rollup carry everything older.
- The retention buttons already exist (`/administration/qsos/<months>/`); the
  change is that they stop being "loss of history" and become "loss of row-level
  detail", which is what the live feed and lastheard lists need and the
  dashboards do not.
- The boundary must be documented on the page: a range older than the retention
  window is answered from aggregate data and cannot show individual QSOs. This
  is the honest version of "we keep the numbers but not every row".

---

## Part 8 — Migration and rollout

Each step independently deployable and independently revertable.

1. **Create the tables and the command.** No reads change. Run the backfill
   against production read-only, and compare every aggregate for every day
   against the base table. This is where a design error is cheap.
2. **Dark launch the router.** Compute both paths for a sample of live
   requests, compare, log mismatches, serve the base-table answer. Watch for a
   week.
3. **Flip long ranges to the rollup.** Reports at `1M` and longer first — they
   are authenticated, less sensitive to a wrong number than the public
   dashboard, and slow enough that the win is obvious.
4. **Flip the public dashboard** for ranges of a day and longer.
5. **Only then** consider QSO retention, as its own change with its own
   announcement.

### Verification per step

```bash
python bminfo/manage.py check
python bminfo/manage.py makemigrations --check --dry-run
python bminfo/manage.py test dashboard.tests
python bminfo/manage.py rollup_qsos --verify --sample 1.0   # every day matches
```

Plus, at step 2 and after each flip, the parity test: the same request answered
both ways must produce identical JSON.

---

## Part 9 — What this is expected to buy

Stated as expectations with their basis, because a rollup that is not measured
is a story.

| Query | Now (production, measured unless noted) | After | Basis |
| --- | --- | --- | --- |
| Dashboard `1w`, anonymous | 1,618 ms DB time (v1.2.13) | not improved much | Range overlaps today ⇒ base table (Part 6). The dashboard's most common range gets nothing from this. |
| Reports `1M` | Python loop over ~1M rows | seconds → tens of ms | 30 rollup rows/day × 30 days instead of 1M-row scan |
| Reports `3M` | loop over ~3.1M rows | seconds → tens of ms | same, 3,000 rows |
| Dashboard `3M` | ~1M rows scanned | tens of ms | rollup totals + boundary days |
| Profile page | scan of one callsign's rows | index-only on T3 | lifetime sums from ~366 rows/callsign/year |
| Dashboard histogram, `1w` | 794 ms | unchanged | R3: base table |

**The honest summary:** this does not make the most common dashboard view
faster. It makes the *long* views — which are currently the slowest and the
closest to unsupportable — cheap, and it is the prerequisite for bounded
retention. If the goal were only the `1w` dashboard, a cache would be the
cheaper answer, and "Alternatives" says so.

---

## Part 10 — Alternatives, and why each was not chosen

### A cache in front of the dashboard

**Considered seriously.** It is less work than a rollup and helps the most
common case, which the rollup does not.

A short-TTL response cache (10–60 s) on the dashboard payload keyed by
`(range, filters)` would collapse the per-viewer cost to one computation per
window, and `python-socketio` + Redis is already in the stack's vocabulary.
It does not require schema changes, parity tests against the base table, or a
backfill.

**Why not chosen as the primary answer:** it does nothing for long ranges that
change slowly, nothing for reports (which vary by export format and time), and
nothing at all for retention. Its worst case is also a thundering herd on
expiry. But it is **complementary, not redundant**: with a rollup, the long
ranges become cheap; with a cache, the common ranges become cheap. If the goal
is responsiveness rather than history, do the cache first — it is a day's work
against the rollup's weeks.

### A columnar database (ClickHouse, DuckDB over Parquet)

**The right answer at a different scale.** A columnar store turns these
aggregates into scans of a few columns, and would serve `count(DISTINCT …)` and
arbitrary histograms far better than a relational rollup. It is also what Silo
plus Parquet almost becomes: the archived raw events are already one gzipped
file per day (`OBJECT-STORAGE.md`), which is a columnar lake one query engine
away.

**Why not now:** the operational cost is a second system to back up, restore,
monitor and secure for a workload of one dashboard and a handful of report
views. The rollup is ~58–330 MB/year of extra tables in a database that already
exists, backed up, and understood. Revisit when (a) queries need grains the
rollup cannot carry, or (b) the archive becomes a query source in its own
right — at which point the archive *is* the columnar store, and DuckDB over the
existing Parquet/JSON objects is a far smaller step than adopting ClickHouse.

### A PostgreSQL materialised view

Tempting, and wrong for this. A materialised view over `qsos` aggregates the
whole table, cannot be incremental, and a refresh over 12.7M rows is a full
scan — the very cost being removed. The rollup is an incrementally-maintained
equivalent of one, and the difference is the whole point.

### TimescaleDB continuous aggregates

Timescale's continuous aggregates are effectively this design, maintained by
the database, with time-partitioned chunks. It is a defensible alternative to
hand-rolling the command: less code to own, more infrastructure to operate
(the extension, its own upgrade path, and its interaction with the existing
PG16 image). Worth a spike if the team is comfortable operating the extension;
the rollup is designed here to be the option that needs no extension.

---

## Part 11 — Open questions to resolve before implementing

1. **Measure the T2 multiplier.** `distinct (day, source_call, talkgroup)` in
   production over a representative fortnight gives the real size of the
   exact-distinct-source table. One query. It decides whether per-talkgroup
   unique-source counts are exact or dropped on long ranges.
2. **Decide the retention window.** 13 months is the proposal. It depends on
   whether year-on-year comparison on the same date is a product requirement.
3. **Decide where the router's boundary falls.** A day is proposed. The report
   page's smallest long range is `1M`; the dashboard's largest short range is
   `1w`, which overlaps today and therefore already uses the base table.
4. **Decide the histogram's fate.** Accept the base-table read for narrow
   buckets, or add an hour-grain table later. The proposal is: accept it.
5. **Decide whether `source_name` belongs in a rollup.** It is carried into the
   callsign list via `max(source_name)`; a rollup would carry "last seen name"
   per callsign-day. This is a display field and tolerates approximation, unlike
   a count — but it should be a stated choice, not an accident.

## Part 12 — Limits of this plan

- Sizing figures are projections from one 106-day window at current traffic.
  Growth, a traffic spike, or a BrandMeister feed change moves them.
- No rollup here has been benchmarked: the tables do not exist. The
  "expected to buy" column is reasoning from measured inputs, not measurement.
- The query enumeration is of the code as of this date. A new view with a new
  grain is a new requirement for the router, and the parity test is the only
  thing that will notice it.
- This plan does not address `_maintenance_stats` (S8), which remains the
  slowest query in the application (91 s) and is in `REVIEW.md` as M1.
