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

1. ~~**Measure the T2 multiplier.**~~ — **answered.** Measured against production:
   **1.25 talkgroups per callsign-day**, projecting to ~1.4M rows/year
   (~134 MB) for the talkgroup × callsign table. That is small enough to keep
   per-talkgroup unique-source counts **exact** on coarse grains; the fallback
   in Part 4 is not needed.
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
6. **Decide `payload`'s fate (Part 14).** Drop the column, move it to a side
   table, or keep it. The measurement makes this the highest-value single change
   in the document and the one that needs a product answer — is per-QSO raw
   payload ever needed from `qsos`, given `raw_events` and the Silo archive
   already hold it?
7. **Decide the grain set (Part 13).** Day-and-coarser is clearly worth it; hour
   grain is worth it only if the histogram leaves the base table, and sub-hour
   alignment multiplies its cost. This is a product accuracy decision.
8. **Decide the decomposition rule for partial periods (Part 13).** Largest
   whole periods plus finest-grain remainders. It needs an invariant test
   ("parts sum to whole") because a wrong decomposition double-counts silently.
9. **Confirm no second time-series consumer is hiding (Part 14).** The
   recommendation against a separate TSDB rests on there being no RSSI/BER
   timeline or spectrum feed planned. If one is, revisit before building the
   relational rollups, not after.

## Part 13 — Four grains, and what each one costs

Part 4 proposed day grain only. The question now is a hierarchy: **hour, day,
week, month, year.** The reframing that makes this tractable is that a
hierarchy is not a design choice, it is arithmetic.

**Every coarser grain is derived from the one below it.** A week is the sum of
seven days; a month is the sum of its weeks minus the spill days at either end
(a month is not a whole number of weeks, so month is the sum of **days**, not of
weeks); a year is the sum of twelve months. What matters is that only the
**finest** grain reads `qsos`, and every other grain is a `GROUP BY` over a table
that is already small. Adding week, month and year is then three views and zero
extra scan cost.

**Week, month and year cost nothing to compute but do not make queries
faster.** They are derived from day grain — three `GROUP BY`s over 598k rows a
year — so they are free to add, but a query answered from them saves only the
difference between reading 90 day rows and reading 3 month rows: microseconds
nobody can measure. Their value is different and real: they let the *retention*
story be honest ("we have monthly totals since 2024") and they let a calendar
preset like `lastMonth` be answered as one row instead of a decomposition.

**The grain whose width matches the query is the one that makes it cheaper.**
Two consumers want different widths — calendar axes and histogram buckets — and
they want different things:

| Grain | Calendar axis it serves | Histogram width it serves | Storage (projected) |
| --- | --- | --- | --- |
| hour | the `hourly_activity` chart (hour of day) | 60 s–6 h buckets, approximately | ~1.0M rows/yr, ~96 MB |
| day | the `daily[]` table, `today`, `yesterday` | 1-day and coarser buckets | 598k rows/yr, ~58 MB |
| week | the `lastWeek` preset | 3-day and 7-day buckets | 86k rows/yr, ~8 MB |
| month | the `lastMonth` preset | — | 20k rows/yr, ~2 MB |
| year | — (no preset uses a calendar year) | — | 1.6k rows/yr, ~0.2 MB |

The storage figures are for `rollup_talkgroup_*` at ~96 bytes/row; week, month
and year are trivial. **Storage is not the reason to add or skip a grain.**

### The real cost is the time dimension

Multiple grains make a second dimension necessary, and that is where the
complexity lives. A monthly rollup keyed `(month, talkgroup)` cannot answer
"September plus ten days of October". It answers whole calendar months. Every
range in `utils.range_bounds()` that is not a calendar range — `1M` is 30 days
back from now, not "last calendar month" — ends mid-period.

So the hierarchy needs a `period_kind` dimension:

```text
    rollup_talkgroup_time
      period_kind   text        -- 'hour' | 'day' | 'week' | 'month' | 'year'
      period_start  timestamptz -- UTC-aligned to the kind
      talkgroup_id  int
      qso_count     int
      duration_ms   bigint
      last_start_at timestamptz
      first_start_at timestamptz
      source_count  int         -- distinct sources *within* the period; not summable
      PRIMARY KEY (period_kind, period_start, talkgroup_id)
```

and a resolution step in the router: **decompose the requested range into the
largest whole periods that fit, then answer the two partial ends from the finest
grain that covers them.** A 30-day range becomes one whole month plus two days,
or four whole weeks plus two days — whichever leaves the smaller remainder.

This is a real piece of logic, and it has a failure mode that no test will catch
unless it is written down: **the sum of the parts must equal the whole.** A
monthly total that includes a week that spills into the next month double-counts.
The parity test (Part 5) catches it only if the decomposition is exercised for
every preset range, not just the round ones.

### What actually has to change in the application

Stated per layer, because "add rollups" hides a week of work in the router.

**1. Models and migration.** The table above, plus `rollup_callsign_time` at day
and coarser grain. Four indexes: `(period_kind, period_start)` for range scans,
`(talkgroup_id, period_kind, period_start)` for the per-talkgroup lists.
Existing migration conventions apply — a new migration, never an edited one.

**2. `dashboard_stats.py`.** The single-pass CTE stays as the base-table path.
A second builder produces the rollup SQL: pick whole periods, union the boundary
intervals from the base table, aggregate the union. The subtlety is
`count(DISTINCT source_call)` across the union. Whole periods and partial
boundaries are disjoint by construction, so a `UNION ALL` followed by
`GROUP BY talkgroup_id` and `count(DISTINCT source_call)` over
`(period, talkgroup, source)` rows is exact — but only where those rows exist.
At day grain they come from T2; at month grain they must come from **day** rows,
because distinct sources do not deduplicate across a month entry. This means
`source_count` on coarse grains can serve "how many sources in this period",
never "how many sources in this range".

**3. `core_views.dashboard_payload`.** Gains the `source` field in the payload
(Part 6) and nothing else — the router decides; the view reports.

**4. `report_views._report_rows`.** This is the largest change, because it is
the most Python-shaped code in the application: it loops over every row to build
`daily`, `hourly`, `weekday`, the histogram and the per-bucket distinct sets.
Three of those map directly onto rollup rows (`daily` ← day totals;
`hourly_activity` ← the hour-of-day axis of day grain; `weekday_activity` ← a
`GROUP BY isodow` over day grain) and fall out of a single query. The histogram
and `concurrent_activity` do not (R3), so the report gains a **hybrid** path:
range-level tables from the rollup, histogram and concurrency from the base
table when the range is long. The alternative — dropping concurrency for long
ranges — is a product decision, not an engineering one, and should be stated on
the page if taken.

**5. `auth_views.profile` / `api_stats`.** Switch to `rollup_callsign_time` for
anything coarser than a day; keep the base table for today. Lifetime totals
become an index-only scan of ~366 rows per callsign-year.

**6. `public_views` (`grouped_lastheard`, `grouped_callsigns`,
`active_talkgroups`).** These are per-range aggregates over the unfiltered
range, so they can use `rollup_talkgroup_time` at day and coarser. `lastheard`
and the live feed cannot, and should not try (S1, S2).

**7. `collector.py`, `rebuild.py`, the admin retention buttons.** Unchanged for
reading, but every one of them can invalidate a closed period and must enqueue
recomputation (Part 5.4). The collector's correction path
(`collector.py:233-259`) deletes and re-inserts a QSO; if that touches a closed
day, the day's rows are stale until recomputed.

**8. New management command `rollup_qsos`** with `--day`, `--from`, `--grain`,
`--verify`. Hour grain rolls up the closed hours of yesterday and today; day
grain rolls up closed days; week/month/year recompute from day at their boundary.
The `test_runtime_topology.py` schedule assertion gains a fifth entry.

**9. `test_runtime_topology.py` / new tests.** Parity per grain (Part 5.5), the
decomposition invariant (parts sum to whole), and a test that a coarse-grain
range and its finer-grain equivalent return identical JSON.

### Is it worth it?

Measured inputs, and the projections they support.

**Performance.** The dashboard's range presets and which grain serves them:

| Range | Rows scanned today | Rows after hierarchy | Likely served by |
| --- | --- | --- | --- |
| 24h and shorter | ≤35k | unchanged | base table (overlaps today) |
| 2d, 5d, 1w | 70k–245k | 2–7 day rows (or 1 week + days) | day/week rollup for the closed part |
| 2w, 1M | 500k–1.04M | 30–60 day rows | day/month rollup |
| 2M, 3M | 2.1M–3.1M | 60–90 day rows | day/month rollup |
| `lastWeek`, `lastMonth` | a calendar period | calendar weeks/months | week/month rollup exactly |

For `3M`, the report currently loops over ~3.1M rows in Python. Against 90 day
rows per talkgroup (1,640 × 90 = 148k rows, ~14 MB) it is an index scan of a
small table. The win is one to two orders of magnitude on the long ranges, which
is where the application is currently close to unsupportable.

**The histogram is the exception, twice.** Narrow buckets cannot come from day
or coarser, and the hour grain only approximates them unless the hour is
aligned to the bucket origin. Hour-aligned rolls (60 s–6 h presets span hours)
are exact only if the range origin is hour-aligned, which for a "now"-backed
range it is not. So either the histogram keeps reading the base table, or the
hour rollup is stored at a **sub-hour** grain (15 minutes), which multiplies its
row count by four and its storage to ~380 MB/year — still small, but it is no
longer free.

**Storage.** The full hierarchy, projected: hour 96 MB + day 58 MB + week 8 MB
+ month 2 MB + year 0.2 MB + callsign-day 134 MB + talkgroup×callsign-day
134 MB ≈ **430 MB/year** against **17.8 GB/year** of raw QSO rows. The
rollups are **2.4% of the data they replace.**

**The verdict.** The grain hierarchy is **worth it for day and coarser**, and
that is what should be built: the work of adding week/month/year on top of day
is three views and a decomposition function, for a few megabytes. The **hour**
grain is worth it only if the histogram moves off the base table, and that depends
on whether sub-hour accuracy is required — a decision worth making explicitly
(Part 11, question 4). Do not build hour grain first.

What the hierarchy does **not** fix: the 24h/1w dashboard, which overlaps today
and stays on the base table. The most important number in this document is still
that this whole design leaves the most common view unchanged.

---

## Part 14 — Moving QSO data to a time-series database

The question: move `qsos` to a TSDB entirely, and leave users, sessions and
tokens in PostgreSQL.

This is a bigger change than every other section combined, so it is worth being
precise about **what is actually causing the problem**, because the measurement
below changes the recommended answer.

### What the measurements say

| Measurement | Value | Source |
| --- | --- | --- |
| `qsos` heap | 3,722 MB | production, `pg_relation_size` |
| `qsos` indexes | 1,434 MB | production, `pg_indexes_size` |
| `qsos` total | 5,158 MB | production |
| Database total | 5,596 MB | production |
| **Share of the database that is QSO data** | **92%** | 5,158 / 5,596 |
| Everything else in PostgreSQL | **~440 MB** | users 296 kB, sessions 64 kB, talkgroups 704 kB, raw events 427 MB |
| Bytes per QSO row | ~1,012 B | 3,722 MB / 3.68M rows |
| **`payload` column share of the heap** | **71.7%** (2,668 MB of 3,722 MB) | `sum(pg_column_size(payload))`, measured |

**What the `payload` column actually is.** Answered by inspecting it rather than
by reading the code: it is the complete BrandMeister LastHeard event that
produced the QSO — every field, not a subset.

```json
{"BER": 0, "RSSI": -66, "Slot": 1, "Stop": 1794587890, "Event": "Session-Stop",
 "Route": null, "Start": 1794587881, "State": 2, "Master": "2402", "FlagSet": 0,
 "LinkCall": "YL3RIM", "LinkName": "MMDVM Host", "LinkType": 1, "SourceID": 2470113,
 "CallTypes": ["Group", "Voice", "Call"], "ContextID": 247003, "LossCount": 0,
 "SessionID": "f1f30d9a-…", "SourceCall": "YL3QH", …}
```

**Is it relevant? No.** Three independent checks:

1. **It is redundant.** Sampled 200 recent QSOs: **200 of 200** have a
   `raw_events` row whose `payload::text` is byte-identical. The QSO payload is
   copied from the raw event when the QSO is written, so it is a second copy of
   a row that is already stored, and already archived to Silo.
2. **It is read by no query.** Sixteen read sites; none selects `payload`. The
   JSON serialiser `qso_json()` does not touch it, the dashboard, reports, live
   feed, LastHeard lists, profile and exports all name their columns explicitly.
   `RawEventAdmin` shows `payload` read-only, but that is the *raw event's*
   payload, not this one.
3. **The rebuild does not need it.** `rebuild.py` reconstructs QSO payloads from
   `raw_events.payload` (and from the Silo archive) — never from an existing
   `qsos.payload`. The only thing that reads `qsos.payload` is `rebuild.py:191`,
   which **writes** the payload it just recomputed. So the column's sole
   consumer is the process that regenerates it.

**How much space it uses — measured on production, 2026-11-13:**

| Quantity | Value |
| --- | --- |
| Rows | 3,680,532 |
| Average stored size per row | **760 bytes** |
| Total stored | **2,668 MB** |
| Share of the `qsos` heap (3,722 MB) | **71.7%** |
| `raw_events` TOAST used for `qsos` | 0 bytes |

**And it is stored uncompressed.** This is the part worth knowing if the column
is kept: `pg_column_size` is *larger* than the decompressed text for every row
sampled (median stored 756 B against 617 B of text), and across 400 random rows
the maximum stored value is 801 B — the inline cap. PostgreSQL inlines a value
under ~2 kB after trying to compress it, and only compresses it if the result
fits in that same window. This payload compresses to about 10% (measured:
`gzip -9` over 500 payloads, 310,029 B → 32,355 B; pglz compresses the JSON text
to 627 B against 760 B jsonb), so the compressed form would fit easily — but the
per-row limit is applied *before* the compressed size is considered, so every
row takes the inline-uncompressed path and the compressor's work is thrown away.
Nothing is TOASTed, the TOAST table for `qsos` is empty, and the heap pays the
full 760 B per row.

**A table at ~1,012 B/row whose useful content is ~200 B/row is not a
time-series problem. It is a factor-of-five storage mistake in one column.**
At the current rate that column alone is ~9.3 GB/year of heap, for data already
stored in `raw_events` and already archived to Silo. This corrects an earlier
reading of this document, which assumed the column was TOASTed and compressed.

### Therefore: three options, in increasing order of cost

**Option 1 — drop `payload` from `qsos` (a day's work).**

There is nothing to externalise: the same bytes already exist in
`raw_events.payload` and in the Silo archive, and the rebuild reconstructs them
from there. So this is a `DROP COLUMN` plus deleting the write in
`collector.py` and the field from the rebuild's `update_fields`, not a migration
of data.

Make it a nullable column, or move it to a separate `qso_payload` table keyed by
`session_id` that is written only when an operator asks for it (or never, since
`raw_events` + the Silo archive already hold the source data). Expected effect,
projected from the measured column share:

- heap 3,722 MB → roughly **1.0–1.2 GB** for the same rows (the remaining
  columns are ~200 B/row plus TOAST overhead);
- indexes shrink somewhat (the primary key and the covering index do not carry
  `payload`, so their share is unchanged; the heap-level saving is the bulk);
- total `qsos` ≈ **5,158 MB → ~2.5 GB**, and the annual growth roughly
  **17.8 GB → ~8.5 GB**, of which the rest could be trimmed the same way.

The parity risk is real but bounded: dropping a column changes nothing that a
query reads, and the collector and rebuild write it, so `collector.py` and
`rebuild.py` are the only writers to touch. The migration is a `DROP COLUMN` —
PostgreSQL 16 marks the column dropped without rewriting the table, so the space
comes back as the table is rewritten by the next compaction pass
(`archiver.py`, which already runs `VACUUM FULL`).

**This is the recommendation, and it should be step 0 of any of the other
options.** It is ~72% of the heap for about 1% of the work, and it makes
every subsequent option cheaper because everything downstream deals with a
five-times-smaller table.

**Option 2 — the rollup hierarchy (Parts 4 and 13) with bounded retention.**

Build the day-and-coarser rollups, then cap `qsos` at a retention window
(13 months proposed). Storage becomes: bounded `qsos` (a plateau) + ~430 MB/year
of rollups. Historical queries keep working from the rollups forever.

**Option 3 — a dedicated TSDB for `qsos`.**

If QSO rows leave PostgreSQL entirely, the app has two databases and a new class
of problem. Before choosing it, be clear about what it buys and what it costs.

**What a TSDB buys.** TimescaleDB over the existing PostgreSQL is the smallest
step: it is a PostgreSQL extension, so `qsos` stays SQL, the ORM mostly keeps
working, and continuous aggregates are the rollup hierarchy maintained by the
database instead of by our command. Partitioning by time (Part 6 of the earlier
analysis in `REVIEW.md`) also gives fast range deletion and index locality.

**What a separate TSDB (InfluxDB, ClickHouse) costs.** Every one of these is
true today and would stop being true:

- `filtered_qsos()` returns a Django queryset built from ORM `Q` objects and
  composed into a raw SQL CTE with `query.sql_with_params()`
  (`dashboard_stats._base_statement`). A non-SQL store has no queryset; the
  single-pass query would be rewritten in that store's language, and the
  "the fast path cannot drift from the ORM path" property — which the code
  currently gets for free by reusing the ORM's own SQL — is lost.
- `QSO.talkgroup` is a join to `talkgroups` with a `PROTECT` FK used for
  continent/country filters. Moving `qsos` out means denormalising geography
  onto every QSO row (more storage, and a sync problem when
  `sync_talkgroups` changes a classification) or cross-database joins in the
  application.
- `collector.py` writes inside `transaction.atomic()` with `close_all()` per
  event; the write path would become two writes to two stores with no shared
  transaction — so a QSO can exist in one and not the other.
- `rebuild.py`, the admin retention buttons, `retention_counts` and
  `_maintenance_stats` all assume one database.
- Backups, restores, monitoring, the NetworkPolicy, the CronJob manifest and the
  Secrets inventory all double. `SECRETS.md` and `fleet/bminfo/workloads.yaml`
  each gain a service.

The Python client landscape matters too: `timescaledb` on PyPI is the
**asyncpg-based driver at 0.2.x**, not the extension; the extension itself is
not in the current `postgres:16` image (measured: `pg_available_extensions`
returns nothing for `timescaledb` or `pg_partman`), so adopting it means a new
image and an extension version to track. That is a real but small step compared
with a second product.

**Recommendation.** Do not move `qsos` to a separate TSDB now. The measured
evidence does not support it: after Option 1, the QSO table is ~2.5 GB and
grows ~8.5 GB/year; after Option 1 plus the rollup hierarchy, historical queries
never touch it and its size is capped by retention. A second database would be
bought to solve a storage problem that one column caused and one retention
policy fixed.

**Revisit TSDB if any of these become true:**

1. Queries need a grain the rollup cannot carry — sub-hour concurrency over a
   month, or per-second breakdowns.
2. Ingestion grows by an order of magnitude (say >350k QSOs/day), where
   PostgreSQL's write amplification becomes the bottleneck before reads are.
3. A second, genuinely time-series consumer appears — live spectrum data, RSSI/BER
   timelines at per-transmission resolution — that wants downsampling and
   continuous aggregates natively.
4. The archive becomes a query source: the Silo files are already one Parquet
   conversion away from being queryable with DuckDB, which is the cheapest way
   to satisfy "query history without a second database server".

At that point, **TimescaleDB first**: same PostgreSQL, same backups, same
Secrets, ORM mostly intact, and continuous aggregates replace the custom rollup
command. A separate store only if Timescale's chunking is measurably inadequate,
which nothing in these measurements suggests.

### The coarse answer, if the question is "is it worth it at all"

| Change | Work | Storage effect | Performance effect |
| --- | --- | --- | --- |
| Drop `payload` | ~1 day | −~60% of `qsos` | small (less heap to scan) |
| Day+ rollups | ~1–2 weeks | +~430 MB/yr | long ranges 10–100× |
| Retention at 13 months | ~1 day + policy | caps QSO growth | none |
| Hour grain | +3 days | +96 MB/yr | histogram only, approximately |
| Separate TSDB | weeks–months | −92% of DB | long ranges only |

The first three together cost about two weeks and turn an unbounded 17.8 GB/year
into a ~430 MB/year plateau for history with bounded rows. The separate TSDB
costs weeks more, removes 92% of the database, and leaves the same 1w dashboard
exactly as slow — because that query reads the base table either way.

---

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
- The four-grain sizing in Part 13 projects from measured cardinals (2,728
  distinct talkgroup-hours/day, 1,640 active talkgroups, 3,055 callsigns/day,
  1.25 talkgroups per callsign-day) and an assumed ~96 bytes/rollup row. Only
  the row counts are measured; the byte estimate is not, and a rollup row with
  text keys and TIMESTAMPTZ columns could plausibly be twice that.
- The TSDB analysis in Part 14 is an assessment, not a benchmark. No second
  database was provisioned to compare, and the Timescale continuous-aggregate
  timings quoted (none are) would need a spike on a copy before anyone commits.
- The `payload` saving (Part 14, Option 1) is measured — 2,668 MB stored, 71.7%
  of the heap. PostgreSQL's `DROP COLUMN` reclaims no space until the table is
  rewritten, so the shrink arrives with the next compaction, not on the deploy,
  and `VACUUM FULL` on a 3.7 GB table needs free disk equal to its size and takes
  it offline for the duration.
