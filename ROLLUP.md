# Database size and query performance

Date: 2026-11-13
Status: **the payload removal is implemented (v1.2.29). The rollup is a
proposed later phase, described here in outline only.**

Two separate problems get confused with each other, so they are kept apart:

1. **The database is bigger than the data.** A single column was 71.7% of the
   main table's bytes. Removing it is cheap and does most of what a bigger
   architectural change would do.
2. **Some queries are slow.** That is a query-shape problem, not a storage
   problem, and it has its own answer.

## Part 1 — The database-size change (implemented)

### What was wrong

`qsos.payload` held the complete BrandMeister LastHeard event for each QSO.
Measured against production before removal:

| Quantity | Value |
| --- | --- |
| Average stored per row | 760 bytes |
| Total | **2,668 MB** across 3,680,532 rows |
| Share of the `qsos` heap (3,722 MB) | **71.7%** |
| Rows whose payload duplicates a `raw_events` row | **200 of 200** sampled, byte-identical |
| Queries that read it | none |
| Stored compressed | no |

It was a second copy of data already in `raw_events`, which the archiver ships
daily to Silo. The rebuild reconstructs QSO payloads from `raw_events` and the
archive, never from here — the column's only consumer was the code that wrote it.

### What changed

- Migration `0011_drop_qso_payload` removes the column.
- `collector.py` no longer writes it; `rebuild.py` no longer writes or updates it.
- `test_qso_payload_removal.py` pins the removal at the model, both write paths
  and the recovery path. Three of its six assertions fail against the old code,
  so it is a real guard rather than a comment.

`RemoveField` compiles to `ALTER TABLE ... DROP COLUMN`: metadata-only on
PostgreSQL 16, instant at any table size. **It does not free space on its own** —
live tuples keep the dropped column's bytes until they are rewritten. Reclaiming
them needs `VACUUM FULL qsos` (or the next compaction pass), which requires free
disk equal to the table and holds an exclusive lock while it runs.

### Deploy order, and why

```text
migration  →  collector + web image  →  VACUUM FULL qsos
```

Migration first: `DROP COLUMN` runs ahead of the code in a rolling deploy, and
an old pod that still writes `payload` then fails loudly with
`column "payload" of relation "qsos" does not exist` rather than silently
filling the column with `{}`. The collector logs that failure per event and
continues, so ingestion is not lost — the gap is however long the rollout takes.

A cleaner alternative, at the cost of a short ingestion pause: stop the
collector, migrate, start the collector.

### Effect on size — measured in production after deploy

| | Before | After `VACUUM FULL` |
| --- | --- | --- |
| `qsos` heap | 3,722 MB | **824 MB** |
| `qsos` indexes | 1,434 MB | 1,017 MB |
| `qsos` total | 5,158 MB | **1,841 MB** |
| Whole database | 5,596 MB | **2,310 MB** |
| Bytes per row | 1,012 B | **235 B** |

The heap fell to 22% of its former size — a little better than projected, since
the estimate assumed a fixed per-row overhead. Indexes shrank 29% as a
side effect: they point into heap pages, and a table a quarter the size needs
fewer of them.

`VACUUM FULL` took **41 seconds** on 3.68M rows and held an exclusive lock for
that duration: the collector paused ingestion and a live page poll would have
waited. At this table size that window is acceptable once; it is worth knowing
the next one will be longer as the table regrows.

### The cost paid for it: one collector restart

The deploy rolled the collector to drop the column from its writes. A restart is
not free here: **a QSO is written only when its session ends**, so any session
in flight when the collector disconnects is lost — socket.io does not replay it.
The hour of the rollout finished about 32 QSOs below the hour before it, against
a normal variance of ±600, so the loss is real but small. Over a 10-day baseline
about 419 QSOs a day (1.2%) already arrive with no source callsign, most likely
sessions whose start was missed by an earlier reconnect; this change adds to
that population by whatever was in flight at the moment of the restart.

This is worth recording because it also applies to the rollback path: rolling
back re-adds the column and restarts the collector again.

### Effect on performance

**Small, and not the reason to do it.** A narrower tuple means fewer pages for
the same rows, which helps broad scans a little. But the dashboard's heavy query
already reads a covering index that never included `payload`, so the queries
optimised in v1.2.13 will barely move. What genuinely improves is everything
proportional to table size: `VACUUM` cost, backup and restore time, and the
compaction pass itself.

The honest summary: **this makes the database smaller and makes a few things
cheaper, but it does not make the slow queries fast.**

### What this does not change

`raw_events` keeps its own `payload`, and should: it is the system of record for
recovery, it is what the archiver writes to Silo, and it is what the rebuild
reads. It is bounded by the one-day retention cutoff, so it does not grow
without limit — but at current traffic it adds about 95 MB of uncompressed JSON
a day, and it is the next storage candidate if one is wanted. Not now: unlike the
QSO copy it is read, by the two paths that recover history.

## Part 2 — The rollup, if it is done later

The rollup is **not implemented**. This is the short version of the design, kept
so it does not have to be rediscovered; the full analysis (every query site, the
grain arithmetic, the sizing) is in this file's git history.

### The one table worth building first

```text
rollup_talkgroup_daily
  day             date          -- UTC calendar day
  talkgroup_id    int
  qso_count       int
  duration_ms     bigint
  source_count    int           -- distinct sources within that day
  first_start_at  timestamptz
  last_start_at   timestamptz
  PRIMARY KEY (day, talkgroup_id)
```

Projected at **~598,600 rows/year (~58 MB)** — about 1% of a year of QSO rows.

It serves the largest share of the expensive queries: the dashboard's
per-talkgroup lists and totals, the report's daily activity and per-talkgroup
summaries, top-callsign totals, the previous-period comparison, and the grouped
LastHeard endpoints.

### The three rules that make it safe

1. **Only closed days.** Today is always incomplete, so the current UTC day is
   always read from `qsos`. Same 00:00 UTC cutoff as the archiver.
2. **Derived and rebuildable.** One idempotent command, `rollup_qsos`, writing
   with `ON CONFLICT DO UPDATE`. Never written by request handlers.
3. **Parity under test.** The rollup path and the base-table path must return
   identical JSON for the same range. Without that test, a rollup that is
   internally consistent but disagrees with the page stays invisible.

And the rule that keeps it from lying about what it can answer: a query uses the
rollup **only** when its grain is coarser than the table's and its filters are
equality on carried dimensions. Substring callsign filters, sub-day histograms,
and per-talkgroup distinct counts across days fall back to `qsos` —
deliberately, and documented on the page.

### What it buys

Long ranges only: reports at 1M and 3M go from looping over one to three million
rows to reading tens of thousands of summary rows — one to two orders of
magnitude. The `24h` and `1w` dashboard, which is the most common view, overlaps
today and stays on the base table either way.

### When it is worth doing

When one of these becomes true, and not before:

- Long-range reports become a used feature rather than an occasional one.
- QSO retention is wanted, so history must survive without row-level detail.
- `qsos` grows past what single-node PostgreSQL manages comfortably.

If only responsiveness matters, a short-TTL cache in front of the dashboard is
much less work and helps the common case that a rollup does not.

## Part 3 — Moving QSOs to a time-series database

Not recommended, and the measurement is the reason.

QSO data is 92% of the database by size, which makes the idea superficially
attractive. But after the payload removal `qsos` is ~2.5 GB and grows ~8.5 GB a
year, and after a rollup its history would not be needed at all. A second
database would be bought to solve a storage problem that one column caused, and
would cost:

- the single-pass query's guarantee of matching the ORM — it reuses the ORM's own
  SQL, and a non-SQL store has no queryset to reuse;
- the `talkgroups` foreign key join, replaced by denormalised geography or
  application-side joins;
- the collector's single transaction across raw event and QSO;
- a second backup, restore, monitoring and secret surface.

**Revisit if** ingestion grows by an order of magnitude, or a genuinely
time-series consumer appears (per-transmission RSSI/BER timelines). If it does,
**TimescaleDB first** — same PostgreSQL, same backups, continuous aggregates
replacing the custom rollup command. The extension is not in the current
Postgres image, so adopting it means a new image.
