# Raw-event archiving to Silo

Raw events are the decoded BrandMeister LastHeard stream: everything the
collector received, kept for auditing and for regenerating the derived `qsos`
table. They grow at roughly **130,000 rows and 95 MB of payload per day**, and by
2026-11-12 `raw_events` was 21 GB — 81% of a 26 GB database, holding every event
since collection began.

They are now moved out of PostgreSQL one UTC day at a time into the Silo bucket
`bminfo-raw`, compressed, and deleted from the table once the object is verified.

- **Bucket**: `bminfo-raw` on `https://silo-api.spainip.es`
- **Object store**: PGSTY Silo 7.x, S3-compatible (MinIO lineage; `mcli` is its client)
- **Lifecycle**: the bucket already expires objects after **365 days**
- **Retention in PostgreSQL**: everything before **today 00:00 UTC** is eligible
  for archiving, so at least one full day is always kept, and the boundary is an
  exact UTC midnight rather than "N hours ago"

## Storage layout

```
bminfo-raw/
└── raw-events/
    └── 2026/
        └── 11/
            ├── 08.jsonl.gz
            ├── 09.jsonl.gz
            └── 10.jsonl.gz
```

- **One object per UTC day**, keyed `raw-events/YYYY/MM/DD.jsonl.gz`. The key is
  derivable from the day alone, so a run is reproducible and a partly finished
  day can be resumed.
- **Format**: gzip-compressed JSON Lines, one raw event per line, in `id` order:

  ```json
  {"payload_hash":"9f2c…","session_id":"214001234","event_type":"Session-Stop",
   "received_at":"2026-11-10T10:00:00+00:00","start_at":"2026-11-10T09:59:52+00:00",
   "stop_at":"2026-11-10T10:00:00+00:00","payload":{"Event":"Session-Stop", …}}
  ```

  The full `payload` is kept, because regenerating a QSO needs its fields.
- **Metadata** on every object, written by the archiver and read back before any
  deletion:

  | Key | Meaning |
  | --- | --- |
  | `rows` | number of events in the file |
  | `sha256` | SHA-256 of the uncompressed JSONL bytes |

  `Content-Type` is `application/gzip`, so a browser or `mcli` can stream it.
- **Size**: about 3 KB per 50 events in the local end-to-end test, i.e. roughly
  **2–4 MB per day** compressed for current traffic. 365 days should stay well
  under 2 GB.

## Permissions: a policy for the `bminfo` user

The user `bminfo` exists. It needs *only* the actions this job performs on *only*
this bucket: list, read, write, and delete its own objects. Nothing cluster-wide,
no other bucket, and no administrative actions.

Save this as `bminfo-raw-rw.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListOnlyThisBucket",
      "Effect": "Allow",
      "Action": [
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads"
      ],
      "Resource": ["arn:aws:s3:::bminfo-raw"]
    },
    {
      "Sid": "ReadWriteObjectsInThisBucket",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": ["arn:aws:s3:::bminfo-raw/*"]
    }
  ]
}
```

Why each group:

- `ListBucket` is required to answer "does this day already exist" (a `HEAD` on a
  missing key is fine, but the rebuild lists archive days and the status page
  reports what is stored).
- The multipart actions matter: a day of events exceeds boto3's 8 MB multipart
  threshold once traffic doubles, and boto3's transfer manager will use multipart
  uploads. Without `AbortMultipartUpload` a failed upload leaves parts behind that
  the bucket's lifecycle rule does not clean up as objects.
- No `s3:CreateBucket`, no `s3:PutBucketPolicy`, no `admin:*`. The archiver never
  changes bucket configuration; the 365-day lifecycle rule stays under your
  control.

Apply it with `mcli` (the Silo client), authenticated as an administrator:

```bash
# One-time: point the client at Silo with an admin identity.
mcli alias set silo https://silo-api.spainip.es ADMIN_USER ADMIN_PASSWORD --api S3v4

# Create the policy and attach it to the existing user.
mcli admin policy create silo bminfo-raw-rw ./bminfo-raw-rw.json
mcli admin policy attach silo bminfo-raw-rw --user bminfo

# Confirm: exactly one policy, and the policy body is what you expect.
mcli admin user info silo bminfo
mcli admin policy info silo bminfo-raw-rw
```

## Access keys

Use a **service account for the `bminfo` user** rather than its login
credentials: service accounts can be rotated and revoked independently of the
user, and the policy is inherited from the user.

```bash
# Let Silo generate the pair (recommended: it also stores the secret hashed).
mcli admin user svcacct add silo bminfo --name bminfo-lastheard-archiver
```

The command prints an `Access Key` and a `Secret Key` **once**. If you prefer to
choose the access key yourself:

```bash
mcli admin user svcacct add silo bminfo \
  --name bminfo-lastheard-archiver \
  --access-key "${S3_ACCESS_KEY_ID}" \
  --secret-key "${S3_SECRET_ACCESS_KEY}"
```

Store the pair in the cluster Secret that the web deployment reads. Keep it out
of Git — the production Secret is SOPS-encrypted, or create a dedicated Secret:

```bash
kubectl -n bminfo create secret generic bminfo-object-store \
  --from-literal=S3_ACCESS_KEY_ID='<access key>' \
  --from-literal=S3_SECRET_ACCESS_KEY='<secret key>'
```

The deployment already references `bminfo-object-store` with `optional: true` in
[`fleet/bminfo/workloads.yaml`](fleet/bminfo/workloads.yaml), so the pod starts
whether or not the Secret exists; archiving simply stays idle until it does.
Endpoint, bucket and prefix come from the same environment.

Verify from inside the cluster after applying the Secret:

```bash
kubectl -n bminfo exec deploy/web -- python manage.py archive_raw_events --check
# → object storage reachable; credentials accepted
```

Rotation: add a second service account, update the Secret, redeploy, then remove
the old account with `mcli admin user svcacct rm silo <access-key>`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `S3_ENDPOINT_URL` | *(empty)* | Silo API endpoint. Archiving is idle while empty. |
| `S3_BUCKET` | `bminfo-raw` | Destination bucket. |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | *(empty)* | Service-account credentials. |
| `S3_REGION` | `us-east-1` | Accepted by Silo, unused for signing. |
| `S3_ADDRESSING_STYLE` | `path` | Silo is path-style (`host/bucket/key`). |
| `RAW_ARCHIVE_PREFIX` | `raw-events` | Key prefix inside the bucket. |
| `RAW_ARCHIVE_INTERVAL_SECONDS` | `3600` | Unused since the archive moved to a CronJob; kept for Compose. |
| `RAW_ARCHIVE_ENABLED` | `true` | Set false to disable the loop; manual runs still work. |
| `RAW_ARCHIVE_COMPACT` | `true` | Reclaim disk after a pass that evicted days. |
| `RAW_COMPACT_MIN_ROWS` | `20000` | Only compact when the pass deleted at least this many rows. |
| `RAW_COMPACT_LOCK_TIMEOUT_SECONDS` | `30` | Give up rather than queue behind another session. |

Archiving requires **all** of endpoint, bucket and both keys. If any is missing
the collector logs the reason once and the loop exits; the admin page shows the
same reason. Nothing fails, and nothing is deleted.

## How a run works

Archiving runs as the `archive-raw-events` CronJob (`manage.py archive_raw_events`),
or on demand from the admin page. It used to be a thread inside the collector
process, which meant a collector restart could interrupt a pass mid-flight.

For each eligible day, oldest first:

1. stream that day's rows out of PostgreSQL into a gzipped JSONL file in a
   temporary directory
2. upload it with `rows` and `sha256` metadata
3. **read the object back** and compare size, row count and digest
4. delete the day's rows in batches of 5,000
5. record the day in `raw_event_archives`

Only step 4 removes data, and it runs only after step 3 passes. A failed upload,
a truncated object, or a wrong digest aborts that day and leaves the rows alone.

### Why re-running is safe

A crash can leave a day in both places. Row counts cannot tell you which case you
are in — a crash before the delete leaves the table as a superset, while events
that arrive late for an already-archived day make the *archive* the smaller one —
so the archiver never picks a winner. **Whenever an object for the day already
exists, it is downloaded, merged with the table's rows by `payload_hash`, and
re-uploaded.** A union can neither drop an archived row nor a row that only
exists in the table, and duplicates are removed by the hash.

Verified end to end against a real S3 endpoint: after archiving 50 events for a
day and then inserting one late arrival, a re-run produced **51 rows, no
duplicates, with all 50 originals intact**, and deleted only the new row from the
table.

### Deletion and disk space

Rows are deleted in batches so no single transaction is long. `DELETE` frees
pages for reuse but **never shrinks the file**, so after archiving a long backlog
the table keeps its old size on disk. At the first production run the table had
dropped from 12.98M to 11.8M rows while remaining 21 GB.

The archive pass therefore ends with **one compaction**, which runs
`VACUUM (FULL, ANALYZE, PARALLEL 0) raw_events` and returns the space.

- **Once per pass, never per day.** `VACUUM FULL` rewrites the whole table, so
  running it after each of a hundred archived days would cost far more than the
  disk it saves. A single compaction runs after the pass finishes.
- **Only when something was evicted.** It runs when the pass deleted at least
  `RAW_COMPACT_MIN_ROWS` rows (default 20,000), and is skipped for a small table.
- **Measured cost.** On a table of the same row width, 1,736 MB holding 86k live
  rows compacted in **3.4 s**, down to **75 MB**. The cost is dominated by reading
  the existing heap, so the one-off compaction of a 13 GB backlog takes tens of
  seconds, while steady state — one or two days of rows — is under a second. In
  the local end-to-end run: 85 MB → 8 KB in 0.19 s.
- **It blocks writes while it runs.** `VACUUM FULL` takes an `ACCESS EXCLUSIVE`
  lock, so the collector's inserts wait for its duration. That is why it is
  batched into one occurrence per pass rather than run per day, and why
  `RAW_COMPACT_LOCK_TIMEOUT_SECONDS` (default 30) makes it give up rather than
  queue behind another session. A lock timeout is recorded in the run detail and
  the next pass retries; it never fails the archive.
- **Disable it** with `RAW_ARCHIVE_COMPACT=false`, or per run with
  `archive_raw_events --no-compact`.

`pg_repack` would avoid the lock but is not installed in the PostgreSQL image and
would need a custom image plus `CREATE EXTENSION`; the lock window here is short
enough that it is not worth it.

## Operating

- **Automatic**: the `archive-raw-events` CronJob runs daily at 01:17 UTC, so the
  previous day is complete before it is archived. It is not a thread inside the
  application any more: the archive is a discrete job with its own pod, its own
  deadline (`activeDeadlineSeconds: 3600`) and `concurrencyPolicy: Forbid`, so a
  restart of the web or collector pods cannot interrupt it and a stuck run cannot
  overlap the next one.
- **Admin page** (`/administration/`, staff only):
  - an *archive status* panel showing the bucket, cutoff, pending days and rows,
    archived days, rows and size, the ten most recent archived days, and the
    state of the last archive and rebuild jobs;
  - the size `raw_events` occupies on disk, next to the archive totals;
  - **Archive pending days now**, which runs the same pass in the background,
    compaction included;
  - **Compact raw_events now**, to reclaim space without archiving anything;
  - **Rebuild QSOs from raw events and archive**.
- **CLI**:

  ```bash
  python manage.py archive_raw_events --check          # bucket and credentials
  python manage.py archive_raw_events --dry-run        # what is pending
  python manage.py archive_raw_events                  # archive everything eligible
  python manage.py archive_raw_events --day 2026-11-01 # one specific day
  python manage.py archive_raw_events --limit 3        # oldest three days
  python manage.py archive_raw_events --no-compact     # skip the disk reclaim
  python manage.py archive_raw_events --compact-only   # reclaim without archiving
  ```

  It exits non-zero if any day failed, and nothing is deleted for a failed day.

## Rebuilding QSOs

`qsos` is derived data and can always be regenerated from raw events. The
rebuild walks:

1. the raw events still in PostgreSQL (roughly the last day), then
2. archived days **newest first**, streaming each gzipped object.

```bash
python manage.py rebuild_qsos                 # table, then the whole archive
python manage.py rebuild_qsos --days 7        # only the newest 7 archived days
python manage.py rebuild_qsos --database-only # ignore object storage
```

**It must apply the collector's rules exactly.** The rebuild decides which events
become QSOs, and it has to decide the same way the live collector does — same
kerchunk threshold, same excluded and out-of-range destinations, and crucially
the same requirement for a destination name, falling back to the talkgroup's
name.

v1.2.16 got that last rule wrong: it accepted events with no destination name,
so replaying two archived days invented **70,104 QSOs** that the live path would
never have created, roughly doubling those days' totals. The rows were identified
(they were the only QSOs with a NULL destination name created during the run) and
deleted, and `dashboard/tests/test_rebuild_parity.py` now compares both paths on
the same events, including that case. Any change to the QSO rules belongs in both
places, and that test is what keeps them in step.

Two properties matter:

- **It is not destructive.** Rows are upserted by `session_id`; the table is never
  emptied first. A rebuild that stops early leaves a usable table, and a QSO whose
  raw event is temporarily unreadable is not destroyed. (The previous
  implementation deleted every QSO up front — finding H3 in `REVIEW.md`.)
- **Order.** Older data is read last, so if a session somehow appears in both a
  recent raw event and an archived day, the most recent reading wins.

Reading months of archive back is slow: expect minutes per month of data. Use
`--days` for a spot check and let the full run happen in the background.

## Failure modes

| Symptom | Meaning | Action |
| --- | --- | --- |
| `object storage is not configured; missing …` | a setting is empty | add the Secret values |
| `cannot reach bucket 'bminfo-raw': …` | wrong credentials, wrong endpoint, or missing permission | `archive_raw_events --check`, then `mcli admin user info silo bminfo` |
| `AccessDenied` on upload | policy missing `s3:PutObject` or attached to the wrong user | check `mcli admin policy info silo bminfo-raw-rw` |
| `… reports N rows on the server, expected M` | a truncated or wrong object | nothing was deleted; re-run, the day is re-exported |
| a day fails repeatedly | inspect `python manage.py archive_raw_events --day DATE` output | rows remain in the table until it succeeds |
| pending days grow | the loop is disabled or storage is down | the admin panel shows pending count and oldest day |
| `compaction did not run: … lock timeout` | another session held the table | nothing lost; the next pass retries, or use **Compact raw_events now** |

Deleting a `raw_event_archives` row does **not** delete the object and does not
bring rows back; it only makes the status page under-report. Restore by reading
the object and re-inserting, or simply rebuild QSOs from the archive.

## Restoring an archived day to PostgreSQL

Rarely needed — the rebuild reads the archive directly — but for a full restore:

```bash
python - <<'PY'
import boto3, gzip, json, os
c = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
                 aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                 aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"])
body = c.get_object(Bucket="bminfo-raw", Key="raw-events/2026/11/10.jsonl.gz")["Body"].read()
rows = [json.loads(line) for line in gzip.decompress(body).decode().splitlines() if line.strip()]
print(len(rows), "rows ready to insert")
PY
```

Then insert with `RawEvent.objects.bulk_create(..., ignore_conflicts=True)`; the
unique `payload_hash` makes the restore idempotent.

## What this replaced

The admin page used to offer four raw-event deletion actions: *Delete irrelevant
and duplicate raw events*, and *Delete raw events older than 1/2/3/6 months*.
Both are gone, along with their views and routes, because deleting raw events by
age is no longer safe once part of the history lives only in object storage.

What remains on the page: update talkgroups, rebuild QSOs (now archive-aware),
QSO retention (unrelated to raw events), expire sessions, and the new archive
panel. The QSO retention counter endpoint now rejects `raw-events`.

## Cost and growth

At ~3 MB/day compressed, a year of archives is roughly 1 GB in the bucket, and
the bucket expires objects after 365 days. In PostgreSQL the effect is larger:
`raw_events` stops growing beyond one day. Against the 2026-11-12 baseline
(21 GB, 81% of the database) a steady state of one or two days is a few hundred
MB, and the database should settle near 5–6 GB dominated by `qsos`.

The index bloat that made `raw_events` expensive — 8.3 GB of indexes on 13 GB of
heap, including a redundant `varchar_pattern_ops` index on `payload_hash` — is
also retired as rows are deleted, though the index files keep their allocated
size until reindexed. `REVIEW.md` ("raw_events retention") records both.
