# BrandMeister statistics

A Django and Channels application that collects the BrandMeister LastHeard
Socket.IO stream and presents live QSOs, grouped statistics, reports, account
management, and administrative maintenance tools.

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
`ghcr.io/ea7klk/bm-lh-new` image and Traefik labels:

```bash
docker compose -f compose-dockge.yaml up -d
```

## Run locally

Python 3.12 is recommended. Start PostgreSQL, then install the Django
dependencies and set the database variables used in `config/settings.py`:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r bminfo/requirements.txt
export DJANGO_SECRET_KEY=development-only
export DJANGO_POSTGRES_HOST=localhost
export DJANGO_POSTGRES_DB=bminfo
export DJANGO_POSTGRES_USER=bminfo
export DJANGO_POSTGRES_PASSWORD=bminfo
python bminfo/manage.py migrate
python bminfo/manage.py bootstrap_admin
python bminfo/manage.py runserver
```

Run the collector separately during local development:

```bash
python bminfo/manage.py collect_brandmeister
```

## Configuration

`.env.example` documents the complete Compose configuration. Important groups
are:

- `POSTGRES_*` for the application database.
- `DJANGO_*` for secrets, hosts, workers, administrator bootstrap, and the
  embedded collector.
- `BM_*`, `TALKGROUPS_*`, and QSO thresholds for BrandMeister ingestion.
- `SMTP_*` for registration, verification, and password-reset email.
- `MATOMO_*` for optional consent-gated analytics.
- `TRAEFIK_*` for the Dockge deployment.

Use `COOKIE_SECURE=true` and configure `DJANGO_CSRF_TRUSTED_ORIGINS` when the
application is served over HTTPS. Keep `.env` out of version control.

## Data behavior

One completed BrandMeister `SessionID` represents one QSO. Duration is computed
as `round((Stop - Start) * 1000)` milliseconds. Completed sessions below
`KERCHUNK_THRESHOLD_SECONDS` are excluded from QSO views; values exactly equal
to the threshold are retained. Local talkgroup 9 is excluded by default.

Decoded events are retained for auditing according to
`RAW_EVENT_KERCHUNK_THRESHOLD_SECONDS`. QSO writes remain idempotent by session
ID so reconnects and duplicate deliveries do not duplicate transmissions.

## Concurrency and six-month sizing

The default Compose profile is sized for approximately 25 concurrent dashboard
users: three async Gunicorn/Uvicorn workers, a 60-second bounded Django database
connection lifetime, and PostgreSQL with 120 maximum connections. The remaining
connection budget covers the embedded collector, migrations, pgAdmin, and
maintenance tasks. Increase `DJANGO_WORKERS` only after checking CPU and
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

## Development checks

```bash
python bminfo/manage.py check
python bminfo/manage.py makemigrations --check --dry-run
python bminfo/manage.py test dashboard.tests
```

Add Django tests alongside new behavior. The suite includes both
`SimpleTestCase` unit tests and PostgreSQL-backed tests; run the complete suite
with Docker Compose or another available PostgreSQL test database.
