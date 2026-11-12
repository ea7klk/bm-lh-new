# BrandMeister Statistics by EA7KLK

A Django and Channels application that collects the BrandMeister LastHeard
Socket.IO stream and presents live QSOs, grouped statistics, reports, account
management, and administrative maintenance tools.

This is an independent project. It is not affiliated with, endorsed by, or
supported by BrandMeister. See [Branding and disclaimer](#branding-and-disclaimer).

## Structure

```text
bminfo/
├── config/                 Django settings, URLs, and ASGI/WSGI entry points
├── dashboard/              Models, views, collector, reports, and admin tools
│   ├── management/commands Django management commands
│   └── migrations/         Database schema migrations
├── templates/              Dashboard, account, report, and admin templates
├── Dockerfile
├── entrypoint.sh
├── manage.py
└── requirements.txt
```

The container entrypoint applies migrations, collects static files, creates or
updates the configured administrator, starts the BrandMeister collector when
enabled, and serves the ASGI application with Gunicorn/Uvicorn workers.

## Run with Docker Compose

Create a local environment file and replace every placeholder secret:

```bash
cp .env.example .env
docker compose up --build
```

The dashboard is available at <http://localhost:8000>. PostgreSQL and pgAdmin
are included in the stack; PostgreSQL is exposed on port 5432 by default.

The production-oriented `compose-dockge.yaml` uses the published
`ghcr.io/ea7klk/bminfo-new` image and Traefik labels:

```bash
docker compose -f compose-dockge.yaml up -d
```

## Production Kubernetes deployment

The production workloads are deployed by Rancher Fleet from
[`fleet/bminfo`](fleet/bminfo), following `main`. Its GitRepo is
`ea7klk/bminfo` in the `ea7klk` Fleet workspace, registered centrally in
`spainip/infra/fleet/bminfo-gitrepo.yaml`. The application namespace remains
`bminfo`; its dedicated PVs and PVCs are owned by this bundle. The older
[`gitops/production`](gitops/production) tree preserves the SOPS and storage
handoff definitions; do not apply it as a second controller. Traefik,
cluster bootstrap, and shared storage remain in `spainip/infra`.

## Run locally

Python 3.12 is recommended. Start PostgreSQL, then install the Django
dependencies and set the database variables used in `config/settings.py`:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r bminfo/requirements.txt
export APP_SECRET_KEY=development-only
export DB_HOST=localhost
export DB_NAME=bminfo
export DB_USER=bminfo
export DB_PASSWORD=bminfo
python bminfo/manage.py migrate
python bminfo/manage.py bootstrap_admin
python bminfo/manage.py runserver
```

Run the collector separately during local development:

```bash
python bminfo/manage.py collect_brandmeister
```

The collector rejoins the configured room after each reconnect, retries failed
connections with a 5–60 second backoff, and disconnects on shutdown. Its send
queue waits for the full server heartbeat window, avoiding premature Engine.IO
`packet queue is empty` aborts when a server ping is delayed. Genuine transport
failures are still logged and retried; events sent while disconnected may be
missed. The collector heartbeat advances only while connected, so
`/status/` becomes degraded after a sustained disconnection (90 seconds with
the default heartbeat interval). `/health/` remains the web liveness check.

## Donation reminders

Public application pages include a dismissible reminder linking to
<https://ko-fi.com/ea7klk>, translated into the selected English, Spanish,
German, or French locale. The initial reminder appears after a random 1–3
minutes of visible browsing, then 10–20 minutes after dismissal. A shared
browser-storage deadline survives navigation; a visit after 30 minutes away
starts with the initial delay. Web Locks coordinate dialogs across tabs where
supported. Browser background throttling can delay reminders.

The “I've already donated” choices are self-declared: a one-time donation
suppresses reminders for 30 days and a recurring donation for 365 days. The
`bminfo_donation_until` cookie stores only the expiry, uses `SameSite=Lax`, and
is `Secure` on HTTPS. Suppression applies to this browser and requires cookies;
clearing them or switching browsers removes it. No donation verification or
payment information is stored. Ko-fi opens only when the visitor follows the
support link; the reminder itself loads no third-party scripts.

Run reminder behavior tests with `node --test tests/donation_reminder.test.cjs`.

## Unverified account cleanup

Registration creates an inactive account and sends a verification link. Accounts
that never confirm their email address are deleted after
`UNVERIFIED_USER_RETENTION_DAYS` (default 5), so stale signups do not accumulate.

The `purge_unverified_users` management command performs the deletion. It only
considers inactive accounts without `email_verified_at`, skips staff and
superuser accounts, and removes related verification, password-reset, and
session rows through cascading deletes. Preview the matching accounts without
writing:

```bash
docker compose exec web python manage.py purge_unverified_users --dry-run
```

Run it manually, or from a host scheduler, with an optional retention override:

```bash
docker compose exec web python manage.py purge_unverified_users --days 5
```

In the Kubernetes deployment, `fleet/bminfo/workloads.yaml` runs the command
daily at 03:17 UTC in a CronJob that uses the web image and the `web-env`
secret. The CronJob forbids concurrent runs and keeps a short job history.

## Configuration

`.env.example` documents the complete Compose configuration. Important groups
are:

- `POSTGRES_*` for the application database.
- `APP_*` for secrets, hosts, workers, and embedded collector settings.
- `DB_*` for the application database connection and `ADMIN_*` for bootstrap
  administrator settings.
- `BM_*`, `TALKGROUPS_*`, and QSO thresholds for BrandMeister ingestion.
- `SMTP_*` for registration, verification, and password-reset email.
- `UNVERIFIED_USER_RETENTION_DAYS` for deleting accounts that never verified
  their email (default 5).
- `MATOMO_*` for optional consent-gated analytics.
- `TRAEFIK_*` for the Dockge deployment.

Use `COOKIE_SECURE=true` and configure `APP_CSRF_TRUSTED_ORIGINS` when the
application is served over HTTPS. Keep `.env` out of version control.

## Data behavior

One completed BrandMeister `SessionID` represents one QSO. Duration is computed
as `round((Stop - Start) * 1000)` milliseconds. Completed sessions below
`KERCHUNK_THRESHOLD_SECONDS` are excluded from QSO views; values exactly equal
to the threshold are retained. Local talkgroup 9 is excluded by default.

Decoded events are retained for auditing according to
`RAW_EVENT_KERCHUNK_THRESHOLD_SECONDS`. QSO writes remain idempotent by session
ID so reconnects and duplicate deliveries do not duplicate transmissions.

The live-QSO stream polls `qsos` for the last three minutes, newest first,
once per second per connected viewer. Migration `0006` adds
`qsos_stop_idx` on `stop_at` so PostgreSQL answers that poll from an index
instead of scanning the whole table; the index build is the reason a deploy with
existing data takes a few seconds longer than one without. The window is
`LIVE_WINDOW_MINUTES` in `bminfo/dashboard/utils.py` and is shared by the
websocket consumer and `/api/live-qsos/`; it also bounds how far back a
reconnecting viewer can recover QSOs.

## Concurrency and six-month sizing

The default Compose profile is sized for approximately 25 concurrent dashboard
users: three async Gunicorn/Uvicorn workers, a 60-second bounded Django database
connection lifetime, and PostgreSQL with 120 maximum connections. The remaining
connection budget covers the embedded collector, migrations, pgAdmin, and
maintenance tasks. Increase `APP_WORKERS` only after checking CPU and
PostgreSQL connection usage.

As a live baseline on 2026-08-09, the public dashboard reported 238,542 QSOs in
the previous seven days, 10,132 unique callsigns, and 1,061 talkgroups. That is
about 34,077 QSOs per day, or approximately 6.22 million QSOs over 182.5 days
if activity remains stable. The dashboard does not expose raw-event row sizes,
so storage is an estimate: budget 40–60 GB for PostgreSQL data and indexes for
six months, then provision at least 80 GB of usable disk (120 GB is a safer
operational target for WAL, vacuum headroom, and growth). `raw_events` contains
JSON payloads for auditing and is expected to be the dominant table.

Re-estimate after the first week of production with PostgreSQL's relation-size
queries, and keep free disk above 25%:

```sql
SELECT relname,
       pg_size_pretty(pg_total_relation_size(oid)) AS total_size
FROM pg_class
WHERE relname IN ('raw_events', 'qsos')
ORDER BY pg_total_relation_size(oid) DESC;
```

The six-month estimate assumes the current seven-day activity rate and the
current raw-event retention policy; sustained traffic growth or a higher raw
payload volume requires proportional headroom.

## Translations

User-visible text lives in `dashboard/translations.json`, one catalogue per
locale (English, Spanish, German, French). When adding or changing a key, update
all four — `dashboard/tests/test_branding.py` and `test_emails.py` assert key
parity and catch a locale that falls back to English.

Account emails keep one section per purpose (`emailVerification`,
`passwordReset`, `emailChange`). Each section carries its own greeting, expiry,
ignore and footer wording, so a password-reset mail never borrows the
"confirmation link" sentence. The mailer reads those keys from the section it is
sending, and the tests check that each email mentions its own purpose in every
locale.

Counted sentences use CLDR plural categories: `expiryOne` and `expiryOther`
pairs resolved by `translate_count()` in `dashboard/i18n.py`. English, Spanish
and German use the singular only for 1; French also uses it for 0, which is why
the helper exists instead of appending an "s" by hand.

## Branding and disclaimer

The interface is an admin-style shell using the colours and layout conventions
of brandmeister.network (`#dd4b39` accent, `#222d32` sidebar, `#ecf0f5` frame,
230 px sidebar, 50 px header). The wordmark in the sidebar reads
**BrandMeister Statistics by EA7KLK**, not "BrandMeister" alone, so the site does
not present itself as BrandMeister's own service.

Every page ends with a disclaimer stating that the site is the author's own
work, that the data comes from the BrandMeister LastHeard service, that it is
not affiliated with or supported by BrandMeister, and that it is provided
as-is without claims of completeness, accuracy, or fitness for a particular
purpose. The text is translated into English, Spanish, German and French
(`disclaimer.*` and `common.appName` in `dashboard/translations.json`), rendered
from `templates/dashboard/base.html` and the maintenance page.

The BrandMeister logo is used as the favicon and in the sidebar; the image files
live in `dashboard/static/dashboard/` and are collected with the other static
assets.

The BrandMeister name and logo belong to BrandMeister. Their use here is
descriptive, to identify the data source; anyone redeploying this project should
review that use against BrandMeister's own trademark policy.

## Main endpoints

- `/` dashboard
- `/user/live-qsos/` authenticated live-QSO view and websocket stream
- `/reports/` authenticated reports and CSV/XLSX/PDF exports
- `/profile/` account profile and statistics
- `/admin/` Django administration
- `/administration/` maintenance and data-quality tools
- `/health/` liveness endpoint
- `/status/` database and collector status
- `/api/dashboard/` dashboard data
- `/api/qsos/` recent QSO data
- `/mockups/` static design mockups with an index

## Design mockups

`bminfo/mockups/` holds four standalone dashboard redesign mockups, served at
`/mockups/` and openable directly from disk. Each is a single HTML file with
inline CSS: no build step, no framework, no external requests. Sample data is
illustrative and does not come from the database.

Two of them (`bm-classic.html`, `bm-rail.html`) reuse the colour values and shell
layout from brandmeister.network's own stylesheet, so a future redesign can stay
inside the wider BrandMeister identity. The logo assets under
`mockups/assets/` come from that site.

The route serves files read-only through `django.views.static.serve`, which
resolves paths under the mockups root and refuses traversal. It is a preview
surface, not part of the application, and can be deleted by removing the two
`mockups` URL patterns and the directory.

## Development checks

```bash
python bminfo/manage.py check
python bminfo/manage.py makemigrations --check --dry-run
python bminfo/manage.py test dashboard.tests
```

Add Django tests alongside new behavior. The suite includes both
`SimpleTestCase` unit tests and PostgreSQL-backed tests; run the complete suite
with Docker Compose or another available PostgreSQL test database.
