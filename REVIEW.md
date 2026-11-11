# Security, Concurrency, and Architecture Review

Date: 2026-11-11
Scope: the whole repository at `main` (`a19afa8`) — the Django 6.1 / Channels
service in `bminfo/` (~2.9k lines of application code plus ~0.9k of tests), the
Docker/Compose stack, the Rancher Fleet bundle in `fleet/bminfo/`, the SOPS-era
`gitops/` tree, and the CI workflow. The running deployment in the `bminfo`
namespace was also inspected and measured.
Reviewer: automated review, not a formal audit. This document finds defects; it
does not prove their absence. **No behavioural change accompanies this
document** — every finding below is recorded, not applied.

## What was done

- Read every Python module in `bminfo/` (config, models, views, consumers,
  collector, mailer, forms, i18n, admin tools, management commands), all 13
  templates, `Dockerfile`, `entrypoint.sh`, `requirements.txt`, both Compose
  files, the CI workflow, the Fleet bundle, and the SOPS manifests.
- Ran the project's own gates against a real PostgreSQL 16 instance:
  `manage.py check` (clean), `makemigrations --check --dry-run` (no changes),
  `manage.py test dashboard.tests` (68 tests, pass), and
  `node --test tests/donation_reminder.test.cjs` (8 tests, pass).
- Ran `manage.py check --deploy` to get Django's own security checklist. It
  warns W004 (no HSTS), W008 (no HTTPS redirect), W009 (weak `SECRET_KEY`), W012
  and W016 (non-secure cookies) — three of which match what production actually
  resolves (H1, M3, L11).
- Ran targeted probes against the real code and against the running service:
  anonymous access to every route, malformed query parameters, registration
  enumeration, registration-mail delivery to arbitrary addresses, 25 sequential
  failed logins, weak-password acceptance, `GET /logout/`, `/status/` behaviour
  with the database unavailable, and the `rebuild_qsos` / `purge_unverified_users`
  interactions (see H3 and H4 for the reproductions).
- Measured the live service: anonymous dashboard latency at two time ranges, a
  10-request concurrent burst, deep-offset pagination cost, and the cost of the
  admin data-quality query (91 s — see M1).
- Built a local copy of the database at production row count and weekly density
  (3.6M QSOs; 244,883 rows in the last seven days against production's 245,816)
  and ran the real ASGI application against it at 1, 3, 6, 9 and 12 gunicorn
  workers, at several client concurrencies, recording throughput, latency and
  peak database connections. Then measured the effect of a covering index, a
  `stop_at` index, a single-pass CTE rewrite, `work_mem`, and partition-based
  retention. All of it is reported in "Scalability".
- Inspected the deployment: the `web-env` secret's non-secret values, the
  settings actually resolved inside the running pod, container identity,
  resource limits, network policies, database connection usage, table sizes,
  and the age distribution of retained data.

Side effects on production, stated for honesty: one `_maintenance_stats()` call
(91 s of database time, the same work the maintenance page performs on every
visit), ten concurrent dashboard requests, and a handful of single requests. No
writes were performed against production data.

## Summary

- **Secret material**: broken, and it is a configuration accident rather than a
  code defect. Production's `SECRET_KEY` **is the nine-character administrator
  password** (H1). Django's own check flags the resulting key as unusable for
  security purposes.
- **Availability**: the anonymous dashboard is expensive and uncached, and the
  single-replica deployment has no rate limiting in front of it. Ten concurrent
  requests double-to-triple response times and saturate all three workers (H2).
- **Data integrity in the admin tools**: the maintenance page offers two
  irreversible operations that are unsafe in combination and one that cannot
  possibly finish (H3, M1). Currently latent; one operator click away.
- **The unverified-account purge added in v1.2.4 is over-broad**: it deletes
  accounts that staff deliberately disabled, because "never verified" is
  inferred from `is_active=False` rather than recorded (H4).
- **Authentication**: password hashing, token handling, session handling and
  CSRF are sound (see "Checked and clean"). There is no rate limiting anywhere,
  no password policy at all, and registration will send mail to any address
  anyone types (M2, M4).
- **Architecture**: workable, but synchronous heavy computation sits on the
  request path and in the admin tools, and the app has no caching layer and no
  background job runner — which is the root cause of H2 and M1.
- **Scalability**: the database is the ceiling, not the web tier. Raising
  gunicorn workers from 3 to 12 bought ~10% throughput and then made latency
  worse; the measured database-only ceiling is 2.8 dashboard requests/second
  against the application's 1.5. The workload is also nowhere near write-bound
  (0.4 writes/s against ~66,000 rows/s of measured capacity), so a move to a
  non-relational store would not help — see "Scalability", including why
  NoSQL is the wrong lever here and what the right ones are, with measured
  effects.
- **The cheapest large win found**: `qsos` has no index on `stop_at`, so each
  open live-QSO page full-scans 3.6M rows every second. One index takes that
  query from 858 ms to 0.136 ms (H5).

The findings that matter most are H1 (a nine-character password signing every
session cookie), H5 (every open live-QSO page full-scanning the QSO table once a
second, for want of one index) and H2 (a handful of unauthenticated requests can
take the site out of rotation). None of them requires anyone to break any code:
they are consequences of configuration, of a missing index, and of putting
repeated expensive work in a request. The first and cheapest of the three —
H5 — is a single migration.

---

## High

### H1. Production `SECRET_KEY` is the nine-character admin password — OPEN

**Files:** `bminfo/config/settings.py:12`,
`gitops/production/apps/web/environment.sops.yaml` (`APP_SECRET_KEY: ""`)

**What the code does:**

```python
SECRET_KEY = os.getenv("APP_SECRET_KEY") or os.getenv("ADMIN_PASSWORD", "unsafe-development-key")
```

**What production does.** The `web-env` secret carries `APP_SECRET_KEY` as a
present-but-empty key. An empty string is falsy, so the `or` falls through to
`ADMIN_PASSWORD`. Verified inside the running pod rather than inferred:

```text
SECRET_KEY_LEN 9
SECRET_KEY_IS_ADMIN_PASSWORD True
SECRET_KEY_IS_FALLBACK False
```

**Why it matters.** `SECRET_KEY` derives the signing keys for the session
cookie, the CSRF token, the messages cookie, and `django.contrib.sessions`.
Anyone who learns the administrator password can therefore also forge a session
cookie for *any* account without knowing that account's password — and the
value in question is a nine-character human password, stored encrypted in SOPS
and materialised into a Kubernetes secret, with a lifetime that predates this
repository (`APP_SECRET_KEY: ""` arrived with `bc35335`, "Move production BMInfo
manifests into application repository").

Two consequences follow even without an attacker:

1. Rotating the admin password silently invalidates every user session, because
   it rotates the signing key.
2. A key this short is guessable in a way a 50-byte random key is not. Django
   flags it as `security.W009` when the fallback is reached.

**How it happened.** `5b2e830` ("Rename release image and clean application
environment") introduced the `APP_SECRET_KEY or ADMIN_PASSWORD` fallback to make
the stack startable without a secret. The fallback is the defect: it turns a
missing secret into a *silently weaker* configuration instead of a startup
failure. An empty `APP_SECRET_KEY` in the manifest then takes the weak path
without anyone noticing, because nothing fails.

**Fix (not applied).** Two parts, both small:

- Generate a real key (`python -c 'import secrets;print(secrets.token_urlsafe(64))'`),
  put it in `environment.sops.yaml`, and re-encrypt with `sops`.
- Change the setting to fail closed: if `APP_SECRET_KEY` is absent or shorter
  than 50 characters, raise `ImproperlyConfigured` rather than falling back to
  the admin password. Keep the `unsafe-development-key` default only when
  `DEBUG` is true, and log loudly otherwise.

Deploying this invalidates all current sessions. That is the correct outcome,
but it is a production-impacting change and was not made under a documentation
mandate.

### H2. Ten unauthenticated requests saturate the service — OPEN

**Files:** `bminfo/dashboard/core_views.py:79-121` (`dashboard_payload`),
`fleet/bminfo/workloads.yaml` (web deployment: `replicas: 1`, readiness probe),
`bminfo/entrypoint.sh:29-33` (`--workers 3`)

**Reachable path.** `GET /api/dashboard/?timeRange=1w` with no credentials. The
view runs four aggregates plus a per-row Python pass over the range:

- `qsos.aggregate(...)` — count, sum, three distinct counts over ~238k rows;
- a `values(...).annotate(...)` grouping for talkgroups;
- the same for callsigns;
- `_histogram()` (`core_views.py:68-76`), which downloads every `start_at` in
  the range into Python and buckets it;
- a second `filter(start_at__gte=now-24h).count()`.

Nothing is cached and nothing limits how often it runs.

**Measured, anonymously, against production:**

```text
/api/dashboard/?timeRange=24h   0.585s  0.452s  0.455s
/api/dashboard/?timeRange=1w    4.318s  4.146s  4.309s
```

The 1w range is the largest an anonymous visitor may request — `restricted_request`
(`utils.py:62-63`) gates `2w` and above behind login, but a week is unauthenticated
and costs four seconds of a worker.

**Ten concurrent requests, then one afterwards:**

```text
10 parallel /api/dashboard/?timeRange=1w  ->  7.27s ... 9.66s each (wall 9.67s)
single  /api/dashboard/?timeRange=24h after -> 0.555s
```

Ten requests, from a site with twelve active accounts, tripled latency. With
`--workers 3` the service is fully occupied; the queue is what turns 4 s into
9.7 s.

**The escalation path is the readiness probe.** Each worker is busy, so the
probe (`urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=3)`,
`failureThreshold: 3`, `periodSeconds: 10`) cannot be served within three
seconds. Three consecutive failures mark the single replica `NotReady`; the
Service drops its only endpoint and every user gets a 503 while the process is
still perfectly healthy. **This part is reasoned from configuration and was not
observed** — I did not sustain load long enough to flip the probe — but the
mechanism is direct: `/health/` competes for the same three workers as the query
that is blocking them, and there is no second replica to absorb it.

**Fix (not applied).** In increasing order of effort: (a) cache the payload for
20–30 s keyed by the normalised query, which cuts repeat cost to nothing for the
common case of a dashboard polling itself every 10 s (`index.html:33` already
polls); (b) tighten `timeRange` for anonymous visitors from one week to one or
two days, since the week range is what makes the request expensive; (c) add a
Traefik `ratelimit` middleware in front of the application; (d) serve
`/health/` from a route that does not share the worker budget, or give the
deployment a second replica so a single saturated pod cannot remove the service.
"Scalability" quantifies each of these: the database alone serves 2.8
dashboard requests/second, the application 1.5, and a 20–30 s cache moves the
endpoint to the network ceiling because a payload serialises in 0.2 ms.

### H3. "Rebuild QSOs" silently destroys every QSO whose raw event was pruned — OPEN, irreversible

**Files:** `bminfo/dashboard/admin_views.py:109-129` (`rebuild_qsos`),
`admin_views.py:152-158` (`clear_raw_events`), `templates/admin/maintenance.html:7-9`

**Reachable path.** `/administration/` → "Rebuild QSOs from raw events". The
view is `@staff_member_required` and `POST`, guarded only by a JavaScript
`confirm()`. Its first statement inside the transaction is:

```python
QSO.objects.all().delete()
```

It then re-creates QSOs by iterating `RawEvent` rows. Any QSO whose raw event no
longer exists — because an operator used the *other* button on the same page,
"Delete raw events older than N month(s)" — is simply never re-created.

**Reproduction.** On a scratch database: one QSO with a surviving raw event, one
QSO whose raw event had been pruned, then POST to `/administration/rebuild-qsos/`.

```text
BEFORE: ['pruned-1', 'retained-1']
AFTER:  ['retained-1']
```

No error, no warning, `messages.success("Rebuilt 1 QSOs from raw events.")`.

**Why it matters now, and why it is latent.** Production currently has **zero**
QSOs without a matching raw event (`count(*) = 0`), because raw events have
never been pruned: `raw_events` holds 12,821,081 rows covering 2026-07-30 to
today, 21 GB. Today a rebuild is harmless. The moment anyone uses the retention
buttons — which `README.md` actively recommends as capacity management ("budget
40–60 GB … then provision at least 80 GB") — the two adjacent actions combine
into silent, irreversible loss of the pruned window's QSO history. There is no
backup path in the bundle (`storage.yaml` defines PVs, not snapshots) and no
confirmation beyond a browser dialog.

**Aggravating detail.** The rebuild also cannot complete at this data size: it
performs a `Talkgroup` lookup and an `update_or_create` per raw event, roughly
38M statements for 12.8M events, inside one `transaction.atomic()` opened by a
web request that `gunicorn --timeout 120` will kill. The transaction then rolls
back, so a killed rebuild is not itself destructive — but each attempt holds a
multi-hundred-megabyte transaction open for two minutes, which delays vacuum and
bloats the tables, and the operator is told nothing.

**Fix (not applied).** Rebuild must never delete rows it cannot regenerate:
delete only QSOs whose `session_id` has a raw event, or better, build into a
temporary table and swap. The retention and rebuild buttons should not sit next
to each other without a stated ordering requirement. For this data size the
rebuild belongs in a management command run from a job, with `--dry-run` and
batching, not in a request.

### H4. The unverified-account purge deletes accounts that staff deliberately disabled — OPEN

**Files:** `bminfo/dashboard/management/commands/purge_unverified_users.py:36-43`,
`fleet/bminfo/workloads.yaml` (CronJob `purge-unverified-users`, `17 3 * * *`)

**Reachable path.** Any staff member disables an account through the Django
admin (`is_active = False`). The nightly CronJob then treats it as a pending
registration and deletes it — permanently — once it is older than
`UNVERIFIED_USER_RETENTION_DAYS` (default 5).

**Reproduction.** A legacy account (active, no `email_verified_at` — the shape of
the two migrated accounts in production), disabled by staff as a moderation
action:

```text
before: active=True verified_at=None
then:   is_active -> False        (staff moderation)
purge:  "Deleted 1 unverified account(s) ... MODERATED"
account still exists after purge: False
```

**Why it matters.** The command infers *intent* ("this registration was never
completed") from *state* (`is_active=False and email_verified_at IS NULL`).
Those are not the same thing:

- Accounts migrated from the legacy application are active with a NULL
  verification stamp. Disabling one for any reason is a normal moderation
  action and immediately arms the purge. Production has two such accounts.
- An account created in the Django admin with "Active" unticked is a
  deliberately pre-provisioned account, and it has no verification stamp
  either.
- The deletion is silent and irreversible: no audit row, no notification, and
  the management command's stdout goes to a CronJob log with
  `successfulJobsHistoryLimit: 1`.

The regression test added with the feature
(`test_keeps_active_legacy_accounts_without_verification_stamp`) covers the
active case only; deactivation is the hole.

**Fix (not applied).** Record the condition instead of inferring it. At
registration the application already creates an `EmailVerificationToken`; the
predicate should require evidence of a pending verification —
`exists(unconsumed or expired token) AND email_verified_at IS NULL` — or, more
durably, add a `verification_sent_at` column set at registration and purge only
rows where it is non-NULL. A staff-set flag should be able to veto the purge
outright. The defect is live rather than theoretical, but it needs a staff
moderation action first, so it is not an emergency — merely urgent.

### H5. Every open live-QSO page full-scans the entire QSO table once per second — OPEN

**Files:** `bminfo/dashboard/live_consumers.py:41-51,53-71` (`stream` / `rows`),
`bminfo/dashboard/live_views.py:16-19`, `bminfo/dashboard/models.py` (indexes)

**Reachable path.** Any logged-in user opens `/user/live-qsos/`. The websocket
consumer starts a loop that sleeps one second and then calls `rows()`, which
filters on `stop_at >= now() - 30 minutes` and orders by `stop_at`. The `qsos`
table has indexes on `start_at`, `source_id`, `destination_id` and `session_id`
— **there is no index on `stop_at`**, so PostgreSQL has no way to find "the last
thirty minutes" except by reading the table.

**Measured against production** (`EXPLAIN ANALYZE`, read-only):

```text
-> Parallel Seq Scan on qsos  (actual time=616.112..638.626 rows=274 loops=3)
Execution Time: 858.623 ms
```

Six hundred and sixteen milliseconds before the first row comes back, for a
result set of 69 rows, because 3.6M rows (4.8 GB) must be read to find them.
The consumer then does this **again one second later**, and again, for as long
as the page stays open.

**Why it matters.** One open live page costs 0.86 s of wall-clock database time
per second, and because the plan runs three ways in parallel that is roughly
**two and a half CPU-seconds per second** — several cores saturating continuously
on a twelve-core node, to return 69 rows. Ten people watching the live feed
would exhaust the machine while doing almost no useful work. This is the most
expensive operation in the application per unit of value, and the one place
where a single ordinary user can degrade the whole service.

**The fix is one index.** Adding `qsos (stop_at DESC)` to the bench copy (same
row count and density as production) turns the same query into:

```text
-> Index Scan Backward using qsos_stop_idx on qsos
Execution Time: 0.136 ms
```

**858 ms → 0.136 ms, a factor of about 6,300**, with no code change, no schema
migration (Django migrations can add the index) and no change in behaviour. The
`live_views.live_qsos_api` endpoint runs the same unfiltered query and is fixed
by the same index.

**Secondary concern, not fixed by the index.** The polling loop is one query per
connection per second. That is tolerable at 25 users once the query is cheap
(25 queries/s against a 70-row index range), but the design does not scale
linearly: it needs a shared poller with a channel layer, or `LISTEN/NOTIFY`, to
serve hundreds of viewers from one query. Recorded under "Scalability" with the
other options.

---

## Medium

### M1. The maintenance page performs a 91-second table scan on every visit, and one admin action cannot fit in memory — OPEN

**Files:** `bminfo/dashboard/admin_views.py:18-79` (`_maintenance_stats`),
`templates/admin/maintenance.html:14` (auto-fetch on load),
`admin_views.py:135-145` (`clear_irrelevant_raw_events`),
`admin_views.py:154-158` (`clear_raw_events`)

`/administration/` renders, then immediately fetches
`/administration/data-quality/`, which runs a single CTE materialising
`raw_events` with computed columns and seven `FILTER` aggregates plus
`percentile_cont(0.95)`. Measured inside the production pod:

```text
maintenance_stats_seconds 91.53
```

Ninety-one seconds of database time on **every page view**, against a worker
timeout of 120 s (`entrypoint.sh:31`) and three total workers. Two staff members
opening the page at once occupy two of them. The result is not cached and is not
needed for the page to render; the metrics panel could load on demand.

Two further admin actions are sized for a dataset this deployment has not had
for months:

- `clear_irrelevant_raw_events` iterates all raw events building `candidates`
  (a list of PKs) and `seen` (a set of `(session_id, event_type)` tuples). At
  12.8M rows that is roughly 1 GB of live Python objects in a container whose
  memory *request* is 64 MiB and whose limit is unset — an OOM kill is the
  likely outcome, in a worker the readiness probe also depends on.
- `clear_raw_events` materialises every matching PK into a list and passes them
  back in one `id__in=[...]` delete. At the 1-month retention that is millions
  of IDs in a single SQL statement.

**Fix (not applied).** Move all three to management commands run as Jobs
(the CronJob pattern already exists), batch deletes, and replace the ad-hoc
metrics query with a materialised view refreshed on a schedule, or compute the
page's metrics from `pg_stat_user_tables` estimates plus a bounded sample.
"Scalability" adds the measured cost of the retention path: deleting 1M rows
costs 1.28 s plus 4.32 s of `VACUUM`, against 147 ms to drop an equivalent
partition.

### M2. No throttling on authentication, registration, or mail; registration is an unauthenticated mail sender — OPEN

**Files:** `bminfo/dashboard/forms.py:9-35`, `bminfo/dashboard/auth_views.py:61-79`,
`bminfo/dashboard/auth_views.py:95-108`

There is no rate limiting anywhere: no cache-backed counter, no lockout, no
`django-axes`, no Traefik middleware. Probed:

```text
25 sequential failed logins for one account -> statuses {200}, account still active
```

Each failed login runs a PBKDF2 comparison; each registration runs a PBKDF2 hash
*and* an SMTP send. Registration accepts any address and mails it:

```text
3 registrations to victim0..2@example.org -> emails sent to arbitrary addresses: 3
```

The content is a fixed template whose only attacker-controlled element is the
callsign in the greeting, so this is not an open relay — but it is an
unauthenticated, unthrottled way to make this domain send mail to arbitrary
addresses, and a way to burn worker CPU at will. Password reset is bounded by
"existing active account only", so it can only bomb a known user.

**Fix (not applied).** A small cache-backed throttle keyed on client address for
`/login/`, `/register/`, `/forgot-password/` (Django's cache framework is not
configured at all — `CACHES` is absent, so LocMem is in use per worker, which
would make a throttle inconsistent across workers; a database table or the
reverse proxy is the honest place for it). Traefik's `ratelimit` middleware is
the cheapest correct answer for a single-replica deployment.

### M3. `COOKIE_SECURE=false` in production, which serves HTTPS — OPEN

**Files:** `bminfo/config/settings.py:94-95`, `.env.example`,
`gitops/production/apps/web/environment.sops.yaml`

`README.md` says: "Use `COOKIE_SECURE=true` … when the application is served
over HTTPS." Production is served over HTTPS and resolves:

```text
COOKIE_SECURE False CSRF False
```

so the session and CSRF cookies are not `Secure`. Edge behaviour softens this:
`http://bminfo.ea7klk.es/` returns `308` to HTTPS before the request reaches the
application. But a cookie without `Secure` is still sent by the browser on that
initial plaintext request, so on a hostile network the first hop leaks the
session cookie before the redirect happens. Nothing here prevents header
inspection over that hop.

**Fix (not applied).** Set `COOKIE_SECURE: "true"` in the SOPS manifest. One-line
change, requires re-encryption.

### M4. No password policy, and a nine-character administrator password — OPEN

**Files:** `bminfo/config/settings.py` (no `AUTH_PASSWORD_VALIDATORS`),
`bminfo/dashboard/forms.py:38-63`

`AUTH_PASSWORD_VALIDATORS` is absent, so Django's validator chain (common
password, numeric, similarity, length) is never run. The only constraint is
`min_length=8` on the form fields. Probed:

```text
RegistrationForm with "12345678"      -> valid
RegistrationForm with "password"      -> valid
```

Combined with H1 — the admin password is nine characters and *is* the signing
key — the practical password strength of this deployment is the strength of that
one string.

**Fix (not applied).** Add the four standard validators. Any policy that rejects
`password` is an improvement; the migration cost is zero because Django only
validates at set-password time (existing hashes keep working).

### M5. `/status/` is public and leaks counts and full database error text — OPEN

**Files:** `bminfo/dashboard/public_views.py:29-40`

Anonymous, unauthenticated:

```json
{"status":"ok","database":{"status":"healthy","connection":"ok"},
 "collector":{"status":"healthy","last_seen_at":"..."},
 "tables":{"raw_events":12821148,"qsos":3595524},"active_users":12}
```

and on failure the handler returns `str(exc)` verbatim. Reproduced by pointing
the connection at a closed port:

```text
503 {"status":"unhealthy", ..., "error":"connection failed: connection to
server at \"127.0.0.1\", port 1 failed: Connection refused ..."}
```

In production that would disclose the database host, port, and driver message to
anyone who asks during an incident — precisely when an attacker is paying
attention. Table counts and active user totals are also business information.

**Fix (not applied).** Log the exception, return a fixed reason string. If an
uptime monitor needs this endpoint, keep `/health/` (which reveals nothing) for
the probe and gate `/status/` behind staff.

### M6. Unauthenticated 500s on malformed query parameters — OPEN

**Files:** `bminfo/dashboard/core_views.py:136-137`, `public_views.py:52`

```python
limit = min(max(int(request.GET.get("limit", 100)), 1), 500)
```

`dashboard_payload` wraps its `rows` parse in `try/except (TypeError,
ValueError)`; these three sites do not. Confirmed against production:

```text
GET /api/qsos/?limit=abc            -> 500
GET /public/lastheard?limit=abc     -> 500
```

Not a vulnerability, but it is unauthenticated error-page generation, one
traceback per request, and it is the kind of difference that makes an error log
useless during an incident.

**Fix (not applied).** Reuse one clamped-integer helper with a default for all
five call sites.

### M7. Container runs as root, with no limits, and there is no NetworkPolicy — OPEN

**Files:** `bminfo/Dockerfile` (no `USER`), `fleet/bminfo/workloads.yaml`,
`docker-compose.yml`

```text
kubectl exec deploy/web -- id      -> uid=0(root) gid=0(root)
resources                          -> {"requests":{"cpu":"25m","memory":"64Mi"}}   (no limits)
pod securityContext                -> {}
networkpolicies (namespace bminfo) -> none
```

Three compounding issues: the application process is root inside its container
(the Django service never needs to be); the only resource declaration is a 64 MiB
memory *request* with no limit, so a runaway admin query (M1) can consume node
memory rather than being killed; and without a NetworkPolicy, any pod in the
cluster can reach PostgreSQL on `postgres:5432` — the database has no
authentication boundary beyond its password.

**Fix (not applied).** `USER 10001` in the Dockerfile with a matching
`runAsNonRoot`/`readOnlyRootFilesystem` context, real requests and limits sized
from observed usage, and a default-deny NetworkPolicy allowing only
`web`/`pgadmin`/CronJob → `postgres:5432`.

### M8. A user who misses the 48-hour verification window has no recovery path — OPEN

**Files:** `bminfo/dashboard/auth_views.py:61-93`, `urls.py`

Registration creates the account inactive and sends one link valid for
`EMAIL_VERIFICATION_HOURS` (48). There is no "resend verification" endpoint, and
`/forgot-password/` only considers `is_active=True` accounts, so an unverified
user cannot reset their way in either. After 48 hours the token is dead; the
callsign and email remain taken, so re-registering fails with "That callsign or
email is already registered" until the five-day purge removes the row.

The user is stuck for up to three days with a misleading message, and the only
self-service path out is to wait for H4's job to delete their account. This is a
support burden and, given H4, an interaction worth fixing at the same time.

**Fix (not applied).** A resend endpoint that reissues a token for an inactive,
unverified account, rate-limited (M2), and a clearer message on the duplicate
registration path when the existing row is an unverified signup.

---

## Low / notes

- **L1. Logout is reachable by GET.** `logout_view` has no method restriction
  and `urls.py` maps `logout/` to it, so a third-party page can log a user out
  with an `<img>` tag. Confirmed: `GET /logout/ -> 302`. Accepted as-is by many
  sites, but it is a one-line fix (`@require_POST`), and the UI already posts.
- **L2. Registration enumerates accounts.** `RegistrationForm.clean_callsign` /
  `clean_email` answer "That callsign or email is already registered", which is
  a direct oracle for "does this person have an account here". Login does not
  enumerate without the correct password (good), but it does distinguish
  "Please confirm your email address before logging in" from "Invalid
  email/callsign or password" once the password is right. Accepted by design —
  a unique-callsign system has to say *something* — but it should be a decision,
  not an accident.
- **L3. Email change can 500 on a uniqueness race.** `start_email_change`
  checks that `new_email` is unused; `confirm_email_change` does not re-check
  before `save()`. If the address is registered by someone else between the two
  confirmations, the save raises `IntegrityError` and the user gets a 500. No
  takeover is possible (the unique constraint holds), but the error is ugly and
  the pending request is left behind.
- **L4. Stored XSS in the maintenance page's confirm dialogs (requires admin
  input).** `maintenance.html:11` interpolates `{{ user.callsign }}` into an
  inline `onsubmit` handler. Django escapes it as `&#x27;`, which the HTML
  parser decodes back to `'` before the JavaScript is parsed, so a callsign
  containing a quote breaks out of the string. Registration validates callsigns
  with `[A-Z0-9][A-Z0-9/-]{1,15}`, so ordinary signups cannot do this; a
  Django-admin-created or legacy-imported callsign can. Staff-only impact.
  Fix by validating callsign at the model level and passing the value through
  `json_script`/`data-` attributes instead of inline JS.
- **L5. `ALLOWED_HOSTS = ['*']` in production.** Confirmed from the pod. Host
  header attacks have little to bite on here (reset and verification links use
  `APP_PUBLIC_URL`, which is fixed), but a wildcard is unnecessary for a
  single-host deployment and removes a layer of defence.
- **L6. `ADMIN_PASSWORD` is re-applied on every container start.**
  `entrypoint.sh:6` runs `bootstrap_admin`, which calls `user.set_password(...)`
  unconditionally. A password changed in the Django admin is silently reverted
  at the next restart. Intended for idempotent bootstrap; surprising in
  operation.
- **L7. Retention deletes do not return disk.** `VACUUM (ANALYZE)` reclaims
  space for reuse inside the table but never truncates it. Space is reused by
  new inserts, so this is steady-state-fine, but "deleted N rows and compacted
  the table" overstates what happens; freeing space requires `pg_repack` or
  `VACUUM FULL` with its exclusive lock.
- **L8. Callsign ownership cannot be verified — trust model.** Email ownership is
  verified; callsign ownership is not, so anyone can register an active
  operator's callsign and the profile page will present that operator's QSO
  statistics as their own. Impact is limited to the UI (QSO data is public and
  there is no transmission capability here, unlike an APRS-connected BBS), but
  the application should say plainly that an account proves control of an email
  address, not of a callsign.
- **L9. Verification links are consumed by GET.** Mail scanners and preview
  fetchers follow links, which can consume a single-use token before the human
  clicks it. Standard trade-off; worth knowing when a user reports an "invalid
  link" immediately after registering.
- **L10. The collector opens and closes a database connection per event.**
  `on_mqtt` calls `close_old_connections()` then `connections.close_all()` in a
  `finally` for every message (`collector.py:281-298`). At ~34k events/day it is
  harmless; it is also avoidable churn (see "Checked and clean" — the reasoning
  for doing it is sound, the cost is not measured).
- **L11. No HSTS.** `SECURE_HSTS_SECONDS` is unset, so no `Strict-Transport-Security`
  header is emitted. Django's default `X-Content-Type-Options`, `X-Frame-Options`
  and `Referrer-Policy` headers *are* present (verified on the live host).
- **L12. Vestigial model fields.** `UserSession` is only ever *deleted*
  (`auth_views.py:55`, `admin_views.py:182`) — nothing creates rows, so the
  "expire sessions" button for that table is a no-op. `User.last_login` exists
  alongside the custom `last_login_at`, and only `last_login_at` is written.
  Harmless, but two of the maintenance tools built on them are decorative.

---

## Architecture review

The application is a conventional Django monolith with a single-threaded
ingestion path bolted on, and most of the findings above trace to three
structural choices rather than to individual bugs.

**1. Expensive computation lives on the request path, with no cache layer.**
`dashboard_payload` recomputes four aggregates and a Python-side histogram on
every poll; the dashboard polls itself every 10 seconds (`index.html:33`).
`_report_rows` (`report_views.py:78-155`) is worse: it iterates every QSO in the
range in Python, then runs about a half-dozen aggregate queries, then builds the
whole selector dataset a second time. Five entry points call it for the same
filters — `reports`, `report_api`, `report_csv`, `report_excel`, `report_pdf` —
with no memoisation between them, so a page view plus its three export buttons
recompute an identical multi-minute dataset four more times.
There is no `CACHES` configuration and no materialised view. This is the direct
cause of H2 and the reason M1 exists.

**2. There is no background job runner; jobs hide inside HTTP requests.** The
only scheduled work is a thread inside the collector process
(`maintenance.nightly_cleanup`), and the CronJob added in v1.2.4. Everything
else that is slow or destructive — rebuild, retention, talkgroup sync, the
data-quality scan — is an admin POST that must finish inside
`gunicorn --timeout 120`. Two of them cannot, and one of them would OOM the
worker first (M1). The fix is not more careful code in those views; it is to
stop performing long-running work in a request. A `Job` per operation, with the
existing CronJob pattern, is already available in this bundle.

**3. Configuration fails open instead of closed.** `SECRET_KEY` falls back to a
password (H1). `ALLOWED_HOSTS` defaults to `*` (L5). `COOKIE_SECURE` defaults to
false and is false in the manifest that serves HTTPS (M3). SMTP, by contrast,
fails *closed* — registration refuses to create an account when the mail cannot
be sent (`auth_views.py:72-78`), which is the right pattern. The same
discipline applied to the three settings above would have prevented the most
serious finding in this document, and each is a small `raise
ImproperlyConfigured` away.

**Smaller structural notes.**

- `views.py` exists only as a compatibility shim re-exporting names from four
  modules. It invites imports from a module that owns no behaviour.
- The collector, the web server, and their shared lifecycle are one container
  with one entrypoint: a web deploy restarts ingestion, and the collector's
  connection budget is implicitly part of the web pod's process group. It works
  at this size; it means "deploy" and "restart collection" are the same action.
- `QSO.talkgroup` uses `on_delete=PROTECT` with `db_constraint=False`
  (`models.py:91-100`). Django will refuse to delete a referenced talkgroup;
  PostgreSQL will not, so any raw-SQL or manual delete can orphan rows and the
  ORM cannot see it. The mismatch is deliberate (the FK targets a natural key),
  but the integrity guarantee now lives only in application code.
- The `Talkgroup` classification tables in `talkgroups.py` are hand-maintained
  and `repair_talkgroups()` rewrites every row on each collector start and on
  each admin sync. At 1,856 talkgroups this is cheap; it is also a small
  dataset that would be better as data than as code.

---

## Scalability

This section is measurement-driven. Everything below was run either against the
production database read-only or against a local copy loaded to the same weekly
density (3.6M QSO rows, 244,883 in the last seven days versus production's
245,816). Where a number comes from a synthetic copy rather than production it
is labelled.

### What this workload actually is

| Property | Measured value |
| --- | --- |
| Queryable rows | 3.6M QSOs, 4.8 GB |
| Audit rows | 12.8M raw events, 21 GB |
| Ingest rate | ~34,000 QSOs/day = **0.4 writes/second** |
| Measured write capacity | 100k rows inserted in 1.5 s = **~66,000 rows/second** |
| Write headroom | roughly **five orders of magnitude** |
| Accounts | 13 total, 12 active; documented target 25 concurrent |
| Dashboard query cost | 4 range scans per request, plus 2 cheaper queries |
| Dashboard refresh | each open dashboard polls every 10 seconds |
| Live-QSO cost | 1 full table scan **per second per open page** (H5): 0.86 s wall, ~2.5 CPU-s |
| Disk | 146 GB free of 435 GB (64% used), ~26 GB in tables |

Two conclusions follow immediately, and they shape everything else:

1. **This is not a write-scaling problem.** Production writes four rows a
   second into a database that can take sixty-six thousand. Any architecture
   chosen for write throughput — which is what NoSQL systems are mostly for — is
   solving a problem this deployment does not have.
2. **This is a read-repetition problem.** A handful of expensive aggregate
   queries are re-executed, unchanged, many times per minute per user, and one
   query (H5) is re-executed every second per user without a usable index.

### Why "more gunicorn workers" is not the answer

I ran the real ASGI application (`gunicorn` + `UvicornWorker`, the same flags as
`entrypoint.sh`) against the production-density copy and measured
`/api/dashboard/?timeRange=1w`, the single most expensive public endpoint.

| Workers | Clients | Throughput | p50 latency |
| --- | --- | --- | --- |
| 1 | 10 | 1.1 req/s | 8.69 s |
| 3 | 10 | 1.4 req/s | 6.28 s |
| 6 | 10 | 1.6 req/s | 5.84 s |
| 9 | 10 | 1.5 req/s | 6.05 s |
| 12 | 10 | 1.6 req/s | 5.85 s |
| 6 | 12 | 1.3 req/s | 8.29 s |
| 12 | 16 | 1.4 req/s | 11.11 s |
| — database only, no web layer | 10 | **2.8 req/s** | — |
| — trivial endpoint `/health/` | 10 | **727 req/s** | 0.01 s |

Three findings:

- **Single-request latency does not improve at all with more workers.** One
  request took 2.85 s with 3 workers and 2.77 s with 12. Its cost is the four
  range scans, and no amount of parallelism in the web tier makes one request
  cheaper.
- **Throughput plateaus at about half the database's own ceiling.** With no web
  layer at all, PostgreSQL serves 2.8 dashboard-equivalents per second. The
  application tops out at 1.5–1.6. Workers stop helping at 6; beyond that they
  queue.
- **Oversubscribing makes it worse.** At 12 workers and 16 clients, throughput
  fell to 1.4 req/s *and* p50 rose to 11.1 s — worse than 3 workers. The web
  tier competes with itself for database time.

The web tier is not the constraint: `/health/` sustains 727 requests per second
on three workers, roughly five hundred times the dashboard's rate. Raising
`APP_WORKERS` from 3 to 12 would buy roughly 10%, then cost latency. The honest
recommendation is to **keep 3 workers, or go to 4–6 at most, and fix the
queries** — which is also what the `AGENTS.md` rule about keeping the worker
budget aligned with the connection budget implies.

### The connection budget constrains worker count anyway

Each thread that touches the database holds a connection for `CONN_MAX_AGE`
(60 s). Connections are therefore not `workers × 1`: they track the number of
*threads that have recently served a database-touching view*, and they
accumulate.

Measured with 6 workers and 12 concurrent clients, sampling once every two
seconds through one sustained run:

```text
t+2s   12      t+10s  18
t+4s   12      t+12s  21
t+6s   12      t+14s  24
t+8s   12      t+16s  24      t+20s  30   (still climbing)
```

It starts at exactly one connection per in-flight request and then grows because
of thread churn, not worker count: `ASGIHandler` enters a `ThreadSensitiveContext`
per request, and asgiref shuts that context's single-thread executor down when
the request ends. Each request therefore runs on a fresh thread, and a thread
that touches the database holds a connection for `CONN_MAX_AGE`. Runs with more
workers and clients reached 34 (6 workers, 12 clients) and 45 (12 workers,
16 clients).

That gives a planning rule which fits every measurement above:

> open connections ≈ `CONN_MAX_AGE` × database-touching requests per second,
> capped by concurrency.

At the ~2 requests/second this application sustains, a 60-second
`CONN_MAX_AGE` allows up to **120 connections — the entire PostgreSQL budget**,
from the web service alone, before the collector, pgAdmin or the CronJob are
counted. The failure mode is `FATAL: sorry, too many clients already` on
arbitrary requests: an availability incident caused by a tuning change.

So worker scaling has two independent ceilings: the query ceiling above, and the
connection ceiling here. **Lower `CONN_MAX_AGE` (15–30 s) or put PgBouncer in
transaction mode in front of the database before raising `APP_WORKERS`.** The
pooler is the better answer because it decouples connection count from both
worker count and `CONN_MAX_AGE`.

### Where the time actually goes

Query plans and timings for one anonymous `timeRange=1w` dashboard request.
Production figures are from `EXPLAIN (ANALYZE)`; the rest from the
production-density copy.

| Operation | Copy | Production | Note |
| --- | --- | --- | --- |
| Summary aggregate (count, sum, 2 distinct, max) | 616 ms | 1,139 ms | over half is `count(DISTINCT source_call)` sorting 10 MB to disk |
| — same, `work_mem` 64 MB | 481 ms | not measured | sort stays in memory |
| Callsign group-by (top 50) | 702 ms | 1,164 ms | ~9 MB external merge |
| Talkgroup group-by (top 50) | 240 ms | 361 ms | |
| Histogram (every `start_at`) | 32 ms | 794 ms | 245k rows to the application; the copy gets index-only scans, production does not |
| Python fetch + bucket (245k rows → 29 buckets) | 550–650 ms | — | pure CPU, one worker's full attention |
| `first()` for `firstQsoAt` | one extra range scan | one extra range scan | |
| **Live-QSO poll (H5)** | 216 ms | **859 ms** | full table scan, every second per open page |
| Live-QSO poll with `stop_at` index | **0.136 ms** | not applied | measured on the copy only |
| Admin data-quality scan (M1) | — | 91.5 s | every maintenance page view |

The dashboard issues **six SQL statements** per request, of which four scan the
same range independently. Measured both ways, on the same data and the same
range:

- **copy, four separate queries:** 616 + 32 + 240 + 702 ms = **1,590 ms**
- **copy, one materialised-CTE query returning all four answers:** **1,045 ms**
  (990 ms at `work_mem` 64 MB) — about **1.6×**
- **production, four separate queries:** 1,139 + 794 + 361 + 1,164 ms =
  **3,458 ms**
- **production, one materialised-CTE query:** **1,618 ms** — about **2.1×**

The rewrite wins more on production than on the copy, because the copy gets
index-only scans and production pays heap access for the same rows. Either way
the mechanism is the same: the range is read once instead of four times.

Note the ratio between the production and copy figures for the histogram: the
copy reads an all-visible table and gets an index-only scan; production pays
heap accesses because 12.8M rows are being written continuously and the
visibility map lags. Index-only scans are not available for free on a live
table.

### Changes ranked by measured return

Each entry states what was measured, not what is hoped for.

1. **Index `qsos (stop_at DESC)` — 858 ms → 0.136 ms (≈6,300×).** Fixes H5 and
   `live_qsos_api`. One index, no code change. This is by far the highest return
   available and should be the first change made to this system.
2. **Cache the dashboard payload for 20–30 s.** Every anonymous visitor with
   the same filters receives a byte-identical response, and each dashboard
   polls itself every 10 s, so a 25 s cache collapses a viewer's 2.5 polls per
   window into one computation — about **2.5× for a single viewer and roughly
   25× at ten concurrent viewers**, growing with traffic. Cost per cached
   request is serialisation, measured at **0.198 ms for a 26 KB payload**,
   against 2.8 s of query work: the endpoint stops being database-bound. Django
   has no `CACHES` configured at all today, so this needs one decision (LocMem
   per worker, or Redis shared across workers — the latter is required the
   moment there is more than one replica).
3. **Single-pass query rewrite — 2.1× on production (3,458 → 1,618 ms), 1.6× on
   the copy (1,590 → 1,045 ms).** One materialised range CTE feeding summary,
   histogram and both top-N lists, replacing four independent scans. Medium
   effort, no schema change, same results — verified by comparing the four
   answers against the four separate queries on the copy.
4. **Covering index `(start_at DESC) INCLUDE (source_call, destination_id,
   duration_ms)` — about 9% end-to-end** (1.4 → 1.5 req/s; summary 619 → 541 ms).
   Modest, because the distinct-count sort dominates once heap access is
   removed. Worth doing alongside 3, not instead of it.
5. **`work_mem` 8 MB → 64 MB for these queries — 22% on the summary aggregate
   (619 → 481 ms) and 5% on the callsign group-by.** The distinct counts spill
   10 MB to disk every time. Set it per role or per transaction, not globally:
   at 120 connections a global 64 MB is a 7.7 GB worst case.
6. **Partition `raw_events` by month — retention becomes O(1).** Measured on a
   1M-row probe: `DELETE` of 995,675 rows took 1.28 s plus **4.32 s of
   `VACUUM`**, versus **147 ms** to drop an equivalent table. At production size
   (21 GB, 12.8M rows) the current admin path additionally builds a multi-million
   element Python list of primary keys first. Monthly partitions with
   `DETACH`/`DROP` make retention a metadata operation, keep each index small,
   and make the 91-second maintenance query (M1) tractable table-by-table.
7. **PgBouncer in transaction mode, or a shorter `CONN_MAX_AGE`.** Not a speed
   fix; it removes the connection ceiling described above, so worker count,
   collector, pgAdmin and CronJobs stop competing for 120 slots, and it stops
   connection consumption from scaling with request rate.
8. **Pre-aggregated rollups — about 3× at this size, and they cannot replace the
   live path.** A daily distinct-triple rollup `(day, talkgroup, source_call)`
   for one week contains 80,000 rows against 244,778 raw rows. Real, but not
   transformative, and `COUNT(DISTINCT)` is not additive across buckets, so
   rollups answer "unique callsigns in this range" only approximately (or need
   an HLL extension). Worth doing after 1–3 and only if 3 is insufficient.
9. **Move `raw_events` payloads out of PostgreSQL — up to 80% smaller database.**
   21 of the 26 GB is an append-only JSONB audit trail that is never queried by
   field. Writing Parquet objects to storage, or a columnar table, removes the
   dominant share of backup time, `VACUUM` cost and disk growth while keeping
   PostgreSQL for everything that is queried.
10. **Channels channel layer for live updates.** Once the `stop_at` index makes
    each poll cheap (0.14 ms), one query per second per viewer is sustainable to
    a few hundred users. Beyond that, the correct shape is one shared poller
    publishing to a channel layer (Redis) that fans out to all consumers —
    turning N queries per second into 1.

### Would moving to a non-relational database help?

Short answer: **no, and it would make this particular workload slower and more
expensive to operate.** The reasoning is about the shape of the queries, not
about technology preference.

**What this application asks of its data store**, taken from the code:

- Exact `COUNT(DISTINCT source_call)` and `COUNT(DISTINCT destination_id)` over
  a window the user chooses at request time (`core_views.py:92`).
- `GROUP BY destination_id ORDER BY count(*) DESC LIMIT 50` — top-N aggregation
  over the same arbitrary window.
- Joins to `talkgroups` for name, country, continent — against a table that is
  **rewritten**. `repair_talkgroups()` and `sync_talkgroups()` update the
  classification of all 1,856 rows on every collector start and nightly at
  02:00 (`talkgroups.py:87-105`, `collector.py:340-366`).
- Substring search: `source_call__icontains`.
- `percentile_cont(0.95)` over ingestion delay (`admin_views.py:59-62`).
- Transactional identity: unique callsign and email, single-use verification
  tokens consumed under `select_for_update()`, and FK cascade when a user is
  purged.
- Range deletes for retention, and `VACUUM` afterwards.

That list is unusually hostile to non-relational stores: it is dominated by
ad-hoc exact aggregation with joins, which is the one thing relational engines
are good at and the thing key-value and document stores are worst at.

**Engine by engine.**

- **DynamoDB** — no aggregation operators at all. Every metric above becomes a
  GSI scan plus client-side computation, or a set of counter items updated on
  write (which introduces hot partitions and still cannot answer "distinct
  callsigns in an arbitrary window"). `COUNT(DISTINCT)` and percentiles do not
  exist. It wins on write throughput, where we need 0.4/s.
- **MongoDB** — `$group`/`$sum` cover the sums and counts; `$addToSet` gives
  distinct but materialises arrays and is bounded by the 100 MB group limit, so
  it works for a week and gets risky for a quarter; `$percentile` exists in 7.0+;
  TTL indexes would solve retention elegantly. But the talkgroup join becomes
  either denormalisation — and then a nightly repair rewrites millions of
  documents — or a `$lookup` on every read. Feasible; strictly more work, no
  measured gain.
- **Cassandra / ScyllaDB** — excellent write throughput and natural time-bucket
  partitioning, and essentially no aggregation. You would hand-maintain counter
  tables, and `COUNT(DISTINCT)` is impossible. Wrong tool for a read-aggregation
  workload.
- **ClickHouse** — genuinely good at exactly this: columnar, sorted by
  `start_at`, `uniqExact` for distinct counts, `AggregatingMergeTree`
  materialised views, `TTL` for retention. It would answer the one-week
  dashboard in tens of milliseconds. But it is not an OLTP database: no unique
  constraints, no cheap single-row update, asynchronous mutations, and it cannot
  hold the user and token tables.
- **TimescaleDB** — PostgreSQL with hypertables, continuous aggregates,
  compression and `add_retention_policy`. Keeps every query, the ORM, the joins,
  the constraints and the transactions; adds the partitioning that fixes
  retention and the pre-aggregation that fixes the dashboard. **This is the only
  "change the database" option that is a strict improvement with no rewrite.**

**Why migration is the wrong lever here.** The cost of the current problem is
*repetition*: the same 245k-row scan, run again and again for the same answer.
A cache eliminates 90–95% of those; an index eliminates 99.98% of the live poll.
Neither has anything to do with the storage engine. Moving to a document or
key-value store would preserve the repetition (N users still trigger N
aggregations) while removing the two things that currently make the queries
affordable — a planner that can answer ad-hoc aggregate queries with an index,
and `COUNT(DISTINCT)` that is exact and cheap.

**Where a non-relational store genuinely fits** is not the query path but the
audit path. `raw_events` is 21 GB of append-only JSONB, indexed only by
`session_id` and `received_at`. That is a log, not a relational data set, and
object storage or a columnar store would serve it better and shrink the database
by roughly 80%. That is the migration worth doing — a smaller blast radius than
replacing `qsos`, and a much larger operational payoff.

**If QSO volume grows 10–100×**, the right move is a second, derived store:
keep PostgreSQL as the system of record for identity and ingestion, and feed
ClickHouse (or a Timescale continuous aggregate) for analytics. That is additive,
reversible, and keeps the ORM's transactional guarantees where they matter.

### Capacity plan by tier

| Tier | Trigger | Changes |
| --- | --- | --- |
| **Now** (12 accounts, 25 target) | — | `stop_at` index (H5), 20–30 s response cache, single-pass query, covering index, `work_mem` 64 MB for the aggregate queries. Keep 3 workers. |
| **~100–500 concurrent readers** | sustained >5 req/s on the dashboard, or probe flaps | Add PgBouncer, then 4–6 workers; partition `raw_events`; move the admin maintenance work to Jobs (M1); put a read replica behind the analytics endpoints. |
| **~1,000+ concurrent, or 10× ingest** | write latency or query times growing without a code change | Timescale continuous aggregates (or ClickHouse fed by the collector) for analytics; Redis channel layer for live updates; move `raw_events` to object storage. Keep PostgreSQL for identity, tokens and ingestion. |
| **Any tier** | — | Do not raise `APP_WORKERS` past ~6 without PgBouncer. Measured: no throughput gain, worse latency, and a hard connection ceiling. |


---

## Checked and clean

Traced and found sound, so the scope of the "clean" claim is honest.

- **Password storage.** Django's default `PBKDF2PasswordHasher` (the first entry
  in `PASSWORD_HASHERS`), 1,500,000 iterations at Django 6.1 defaults,
  per-password salt, constant-time comparison inside `check_password`.
- **Verification and reset tokens.** 32 bytes from `secrets.token_urlsafe`
  (`auth_views.py:23-25`), stored only as SHA-256, single-use via
  `consumed_at`, expiry checked in the query (`expires_at__gt=now`), and
  consumed under `select_for_update()` inside a transaction. Tokens are never
  logged and never appear in URLs other than the mailed link.
- **Session handling.** `django.contrib.auth.login` rotates the session key;
  `logout` flushes it; `session fixation` does not apply. `UserSession`, though
  vestigial (L12), is deleted on logout as intended.
- **CSRF.** `CsrfViewMiddleware` is installed, every state-changing form carries
  `{% csrf_token %}`, and a token-less POST to `/register/` returns **403**
  (verified against production). State-changing admin operations are POST-only.
- **SQL injection.** Every query uses the ORM. The only raw SQL is a `SELECT 1`
  liveness probe, `VACUUM (ANALYZE)` over four fixed table names, and the CTE in
  `_maintenance_stats`, which binds its one parameter (`%s`). No user input is
  interpolated into SQL text.
- **XSS in the dashboard.** All dynamic text reaching `innerHTML` passes through
  the `esc()` helper in `index.html` and `live_qsos.html`, and translations are
  delivered through `json_script`, not string interpolation. The maintenance
  page writes its own metrics payload into `innerHTML` unescaped, but that
  payload is staff-only and contains fixed keys and numeric values. The one
  exception is L4, which is an inline handler rather than a render path.
- **Authorization.** `require_scope` correctly gates the callsign filter and the
  ranges above one week, and it is applied to every public data endpoint
  (`/public/lastheard`, `/public/lastheard/grouped`,
  `/public/lastheard/callsigns`, `/user/talkgroups`). Anonymous access to
  `/reports/`, `/user/live-qsos/`, `/administration/`, `/admin/` all redirect
  (302). The websocket consumer rejects unauthenticated connections with 4401
  (`live_consumers.py:14-17`).
- **Collector idempotency.** QSO writes are a PostgreSQL `ON CONFLICT DO UPDATE`
  keyed on `session_id` (`collector.py:148-203`), so duplicate deliveries and
  reconnect replays cannot duplicate rows. Duration is `round((Stop-Start)*1000)`
  and kerchunks are excluded strictly below the threshold, keeping exact
  equality — matching the invariant in `AGENTS.md`.
- **Ingestion thread safety.** `connections.close_all()` is per-thread, so the
  collector, heartbeat, talkgroup and cleanup threads cannot close each other's
  connections; each releases its own after work. The pattern is correct even if
  it is chattier than necessary (L10).
- **Registration fail-closed on mail failure.** If `send_verification` raises,
  the half-created user is deleted and an error is shown
  (`auth_views.py:70-78`), so no orphan account can exist without a delivered
  link. Verified by an existing test.
- **Unverified accounts cannot log in.** `CallsignAuthenticationForm` checks
  `is_active` after the password check (`forms.py:27-31`), so unverified
  accounts are unusable without revealing anything to a wrong password.
- **Translation completeness.** All four locales carry exactly 541 keys with no
  missing or extra entries, satisfying the "update every supported locale"
  rule in `AGENTS.md`.
- **Database sizing against the documented estimate.** `README.md` budgets
  40–60 GB of data for six months. At 3.5 months the tables hold 21 GB
  (`raw_events`) + 4.8 GB (`qsos`) ≈ 26 GB, so the estimate is on track. Node
  headroom is 146 GB free of 435 GB.
- **Connection budget at the current worker count.** 24 of 120 connections in
  use (18 idle, 1 active) with three Uvicorn workers, one collector, pgAdmin and
  the CronJob — consistent with the documented "three workers, 120 connections"
  target. The caveat is in "Scalability": under concurrent load a worker holds
  several connections, not one (measured 34 at 6 workers and 45 at 12), so the
  budget is not at risk today but is the reason worker count should not be
  raised without a pooler.
- **Secret hygiene in Git.** `.env` and its variants are ignored; `git log --all
  -- .env` is empty; the `gitops/` and Fleet secrets are SOPS-encrypted
  (`ENC[AES256_GCM,...]`); no credential appears in `README.md`,
  `.env.example` (placeholders only) or the CI workflow.
- **CI integrity.** The workflow runs on `ubuntu-latest` with a PostgreSQL
  service, the top-level `permissions` is `contents: read`, and
  `packages: write` is granted only to the image-publish job. No
  `pull_request_target`, no self-hosted runner, no secret in the test job. The
  release path checks out the tag and pushes to `ghcr.io` only.
- **Image pinning.** Both the web deployment and the purge CronJob reference the
  image by tag **and** digest (`v1.2.5@sha256:62523a34…`), so a repointed tag
  cannot silently change what production runs.
- **The purge command's own SQL.** `purge_unverified_users` filters on
  `is_active=False`, `email_verified_at__isnull=True`, non-staff, non-superuser
  and `created_at < cutoff`, locks candidates with `select_for_update()`, and
  relies on FK cascade to remove tokens and sessions. Boundaries are tested
  (`--days`, `--dry-run`, staff protection, cascades, retention setting). Its
  one logic defect is the domain assumption in H4, not the query.
- **Backup/restore posture of the bundle.** PVs and PVCs are defined and owned
  by this bundle (`storage.yaml`), with `reclaimPolicy` and node pinning set.
  There is no snapshot schedule — noted under H3, since it is what makes
  rebuild loss unrecoverable.

---

## Suggested order of work

1. **H5 — add an index on `qsos (stop_at)`.** One migration, no code change, and
   it takes the live-QSO query from 858 ms to 0.136 ms. Nothing else in this
   document offers a 6,000× improvement for one line. Do it first because it is
   the cheapest thing that is also a live production defect.
2. **H1 — replace the signing key.** Generate a real `APP_SECRET_KEY`, re-encrypt
   the SOPS manifest, and make the setting fail closed. It is the only finding
   whose exploitation is "log in with the password you already have". Expect all
   sessions to be invalidated.
3. **H4 — narrow the purge predicate before it is trusted.** It runs nightly at
   03:17. Record verification intent (token existence or a
   `verification_sent_at` column) instead of inferring it from `is_active`,
   and let a staff flag veto deletion.
4. **H2 + M1 — get heavy work off the request path.** A 20–30 s cache on the
   dashboard payload alone moves the endpoint from 1.5 requests/second to the
   network ceiling, because the payload serialises in 0.2 ms (see
   "Scalability"). Then the single-pass query rewrite (2–3× less database work),
   the covering index and 64 MB `work_mem` for these aggregates. The admin
   maintenance tools belong in Jobs, batched, with the metrics query precomputed.
5. **H3 — make rebuild non-destructive.** Regenerate only what has a raw event;
   move the operation out of the request path.
6. **M2 — throttle login, registration and password reset** at the edge or in a
   shared store, and stop accepting unbounded mail sends.
7. **M3, M4, M5, M6, M7 — the cheap correctness and hygiene set.** One-line
   settings (`COOKIE_SECURE`, `ALLOWED_HOSTS`, password validators), a shared
   integer-parse helper, a fixed error string on `/status/`, and a non-root
   container with limits and a NetworkPolicy.
8. **M8 + L2 + L9 — the account-flow edges.** A resend endpoint removes the
   three-day dead zone and makes the enumeration trade-off a conscious one.
9. **Scalability follow-ups** — partition `raw_events` by month so retention is
   a metadata operation, add PgBouncer before touching `APP_WORKERS`, and move
   `raw_events` payloads to object storage to shrink the database by ~80%. If
   volume grows 10×, add Timescale continuous aggregates or ClickHouse as a
   derived analytics store; do not migrate the primary database (see
   "Scalability").
10. **L1, L3, L4, L6, L7 — close out** as their own small changes.

## Limits

This review read the code, ran the project's own gates, and probed both the
code and the live deployment. It did not perform a penetration test, did not
attempt to guess or crack any credential, and did not sustain load long enough
to force the readiness-probe failure described in H2 — that escalation path is
reasoned from the probe's 3-second timeout, the 3-worker budget, and the
observed saturation, not observed end to end.

The 91-second `_maintenance_stats()` figure is a single measurement on the
production database at 12.8M raw events; it will vary with cache state and
should be read as an order of magnitude, not a constant. The same applies to the
latency numbers in H2, which were single samples (three for the baseline, one
burst of ten).

**The scalability benchmark, and what it does not prove.** The worker, index and
query-rewrite figures come from a local PostgreSQL 16 instance loaded to
production row count and weekly density, driven through the real ASGI
application. The comparison is therefore like-for-like between configurations,
which is what the recommendations rest on; the absolute numbers are not
production numbers. Differences worth naming: the copy ran with
`shared_buffers` 128 MB (production 256 MB), with JIT disabled (the extracted
binary has no LLVM), over loopback with no TLS, on the same twelve-core host that
runs the live cluster, and with a warm page cache. The single production
dashboard request takes 4.3 s from outside while the copy answers in 2.8 s
locally, and the histogram query is 794 ms on production against 32 ms on the
copy — partly the missing TLS and network, partly the visibility-map effect
described above. Treat the copy's figures as relative, and the production
`EXPLAIN` figures (4.3 s request, 858 ms live poll, 91.5 s maintenance scan) as
absolute.

Load was deliberately bounded — at most 16 concurrent clients and 48 requests
per configuration — because this host also serves the live site. Sustained
saturation behaviour (what happens after ten minutes at the ceiling, whether the
readiness probe actually flaps) was not observed. The `stop_at` index was
verified by measurement on the copy only; it has not been applied to production.

Database and cluster state were inspected read-only. No production writes were
performed, no secrets were printed, and no configuration was changed. Backup and
restore behaviour, the SOPS key custody, Traefik's TLS configuration beyond the
observed redirect, and pgAdmin's own security are outside this review's scope;
pgAdmin is exposed on `bminfo-admin.ea7klk.es` and is a separate management
surface worth its own look.
