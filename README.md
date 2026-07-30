# BrandMeister statistics

This is a collector and dashboard for the BrandMeister LastHeard Socket.IO stream, with a
responsive grouped-statistics interface modeled after
[bm-lh-nextgen](https://github.com/ea7klk/bm-lh-nextgen).

## Data model

BrandMeister's completed LastHeard records contain an `Event` of `Session-Stop`, a stable
`SessionID`, and Unix timestamps named `Start` and `Stop`. The collector treats one completed
`SessionID` as one transmission/QSO and calculates:

```text
duration_ms = round((Stop - Start) * 1000)
```

Records with duration `< KERCHUNK_THRESHOLD_SECONDS` are excluded from `qsos`; the default is
three seconds. A duration of exactly three seconds is kept. All decoded events, including
filtered kerchunks, start events, and completed sessions without a displayable talkgroup, are
retained in `raw_events` for auditing and reprocessing. The `qsos` table contains only completed
sessions with a destination ID and a non-empty destination name from either the event or synced
talkgroup metadata; local talkgroup 9 is excluded.

The database upsert on `session_id` makes the collector safe against duplicate delivery and
duplicate events caused by reconnects or broad subscriptions. Local talkgroup 9 is excluded by
default, matching the reference application.

Talkgroup metadata is fetched automatically from BrandMeister `/v2/talkgroup` at collector
startup and then every `TALKGROUPS_SYNC_HOURS` hours. Each record is stored with its display name,
country code, full country name, and continent. Country classification follows the MCC and
special-prefix rules in `talkgroups.py`, including global talkgroups beginning with 9 and the
regional exceptions used by `talkgroupsService.js`.

## Run locally

Create a PostgreSQL database, then run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
export DATABASE_URL='postgresql://bminfo:bminfo@localhost:5432/bminfo'
bminfo-collector
```

In a second terminal:

```bash
uvicorn bminfo.web:app --reload
```

Open <http://localhost:8000> for the dashboard. The API endpoints are:

- `GET /api/stats/summary`
- `GET /api/qsos?limit=100&offset=0`
- `GET /public/lastheard/grouped?timeRange=30m&limit=25`
- `GET /public/lastheard/callsigns?timeRange=30m&limit=25`
- `GET /public/stats`
- `GET /public/continents`
- `GET /public/countries?continent=Europe`
- `GET /public/talkgroups?continent=Europe&country=ES`
- `GET /user/live-qsos` and `WebSocket /user/live-qsos/ws` (registered users)
- `GET /admin/postgres` (admin authentication required)
- `POST /admin/postgres/analyze` (admin authentication required)
- `GET /locales/{locale}` (`en`, `es`, `de`, or `fr`)
- `GET /health`
- `GET /status` (database, collector, table-row, and active-user status)

## Pull request merge protection

The `Tests` job runs for every push and pull request, and also for merge-queue groups. To prevent
failed pull requests from being merged, configure the repository's default branch protection
rules or ruleset with `Tests` as a required status check. Enable the option requiring branches to
be up to date before merging if you want the check to run against the latest target branch.

The check name is intentionally stable: `Tests`.

`/health` is a simple liveness check. `/status` performs a database connectivity check and
returns the current `raw_events` and `qsos` row counts, the number of active authenticated users,
and collector heartbeat freshness. It returns HTTP `200` when all services are healthy and
HTTP `503` when the database is unavailable or the collector heartbeat is stale. The collector
heartbeat interval is configurable with `COLLECTOR_HEARTBEAT_SECONDS` (default `30`); the status
endpoint considers the collector stale after three intervals, with a minimum grace period of
90 seconds.

Callsign searches and the extended dashboard ranges (`2w` / last 14 days, `1M` / last month,
`2M` / last 2 months, and `3M` / last 3 months; month ranges use 30-day increments) require an active user session. Anonymous visitors can
still use the public dashboard ranges through one week. Opening the main dashboard while signed
in refreshes the sliding session expiry and shows the signed-in user's callsign in the account
navigation.

The registered-user Live QSOs page keeps one authenticated websocket open. The collector publishes
each qualifying QSO through PostgreSQL `LISTEN/NOTIFY`, and the page receives matching rows without
polling or reloading when filters change. If the connection is interrupted, the page reconnects
automatically.

## Registered users and admin panel

Users can register at <http://localhost:8000/user/register> and sign in at
<http://localhost:8000/user/login>. A profile shows callsign-specific QSO count, total talk
time, unique talkgroups, first/last heard timestamps, and the top talkgroups by activity.
Passwords are stored as salted PBKDF2-SHA256 hashes; the application does not store plaintext
passwords. Password hashes from the reference application are accepted as bcrypt during the
migration and upgraded automatically after the user's first successful login.

Set `ADMIN_PASSWORD` for the web service before opening <http://localhost:8000/admin>. The admin
panel provides registered-user totals, 24-hour network totals, per-user QSO/talk-time metrics,
account activation/deactivation, and deletion. With Docker Compose, put this in `.env`:

```dotenv
ADMIN_PASSWORD=replace-with-a-long-random-password
COOKIE_SECURE=false
# Completed QSOs shorter than this are excluded from qsos and live/report views.
KERCHUNK_THRESHOLD_SECONDS=3
# How often the collector records its process heartbeat.
COLLECTOR_HEARTBEAT_SECONDS=30
# Seven days since the user's last authenticated request.
SESSION_HOURS=168
```

After changing `KERCHUNK_THRESHOLD_SECONDS` in `.env`, recreate both the `web` and
`collector` services so their process environments are refreshed:
`docker-compose up -d --force-recreate web collector`.

Set `COOKIE_SECURE=true` when the application is served over HTTPS. New registrations are active
only after the user confirms the email address. Existing active accounts, including migrated
accounts, remain usable without a new confirmation step.

### Email verification

New registrations receive a localized HTML confirmation message in English, Spanish, German, or
French. Configure the SMTP sender and public application URL in `.env`; the proposed Mailcow
settings for `mail.conxtor.com` and `bm-lh@ea7klk.es` are already included in `.env.example`:

```dotenv
APP_PUBLIC_URL=https://bm.ea7klk.es
SMTP_ENABLED=true
SMTP_HOST=mail.conxtor.com
SMTP_PORT=587
SMTP_USERNAME=bm-lh@ea7klk.es
SMTP_PASSWORD=replace-with-the-mailbox-password
SMTP_USE_TLS=true
SMTP_USE_SSL=false
SMTP_FROM_EMAIL=bm-lh@ea7klk.es
SMTP_FROM_NAME=BrandMeister Lastheard
SMTP_REPLY_TO=bm-lh@ea7klk.es
EMAIL_VERIFICATION_HOURS=48
PASSWORD_RESET_HOURS=1
```

Port `587` uses STARTTLS. Use port `465` with `SMTP_USE_SSL=true` and
`SMTP_USE_TLS=false` if that is how the mail server is configured. Recreate the web service after
changing these values. The login page includes a forgotten-password form; reset links are
single-use and expire after `PASSWORD_RESET_HOURS`.

User sessions use a sliding inactivity timeout. `SESSION_HOURS=168` expires a session after one
week without an authenticated request; authenticated activity refreshes both the database expiry
and the browser cookie.

### Matomo analytics

Matomo tracking is optional and follows the configuration used by the
[reference project](https://github.com/ea7klk/bm-lh-nextgen). Set these variables for the web
service to enable page-view and link tracking on the dashboard, account pages, and admin panel:

```dotenv
MATOMO_ENABLED=true
MATOMO_URL=https://analytics.example.com
MATOMO_SITE_ID=1
```

The script is omitted when tracking is disabled or any value is missing or invalid. `MATOMO_URL`
must be an HTTP(S) Matomo installation URL without query parameters, and `MATOMO_SITE_ID` must be
numeric. When Matomo is enabled, the tracker and its analytics cookies are withheld until the user
explicitly accepts analytics cookies. The banner offers equally prominent accept and reject choices,
and the `Cookie settings` link in the footer allows the choice to be changed or withdrawn. The
consent preference is stored in the `bm_cookie_consent` cookie for 180 days. Login, administration,
language and security cookies are strictly necessary and remain available without analytics consent.

## Languages

The dashboard, account pages, and admin panel support English, Spanish, German, and French. The
language selector stores the choice only in the browser’s `bm_lang` cookie for 15 days. The
application does not read or write a user-language preference in PostgreSQL. If no cookie exists,
the request’s `Accept-Language` header is used and English is the fallback.

### Migrate users from the old bm-lh-nextgen app

The migration copies only the old application's `users` table. It preserves callsigns, names,
email addresses, account status, registration/login timestamps, and compatible password hashes.
User sessions are not migrated, so every user must log in again after the migration.

The export contains password hashes. Keep the generated file private and remove it after a
successful import. The `migration-data/` directory is ignored by Git.

#### Docker migration

1. Start the new application's target PostgreSQL and web services:

   ```bash
   docker compose up -d postgres web
   ```

2. Set the old application's PostgreSQL DSN. When the old database is published on the host,
   use `host.docker.internal` because the migration runs inside a temporary Docker container:

   ```bash
   export REFERENCE_DATABASE_URL='postgresql://olduser:oldpassword@host.docker.internal:15432/old_db'
   ```

   If both databases share a Docker network, use the old database service name instead. The
   target DSN defaults to `postgresql://bminfo:bminfo@postgres:5432/bminfo`; pass
   `--target-dsn` if the target database is elsewhere.

3. Export the old users to a private file on the host:

   ```bash
   scripts/migrate_users_docker.sh export \
     --output /migration/users-export.json
   ```

4. Perform a dry run before writing to the new database:

   ```bash
   scripts/migrate_users_docker.sh import \
     --input /migration/users-export.json \
     --dry-run
   ```

5. Import the users. The default `skip` policy leaves existing target users unchanged:

   ```bash
   scripts/migrate_users_docker.sh import \
     --input /migration/users-export.json \
     --on-conflict skip
   ```

   Use `--on-conflict update` to replace matching callsign/email records, or
   `--on-conflict error` to abort on the first duplicate.

6. Verify the accounts in the admin panel at <http://localhost:8000/admin>. Imported bcrypt
   hashes are accepted and upgraded to the new password-hash format after each user's first
   successful login. Existing inactive accounts remain inactive and can be activated from the
   admin panel.

For a non-Docker migration, run `python scripts/migrate_users.py export` with
`--source-dsn`, then run `python scripts/migrate_users.py import` with `--target-dsn` and the
export file path.

The admin page also includes a read-only PostgreSQL overview with database size, connections,
server/version information, table size estimates, and connection states. The “Refresh planner
statistics” action runs `ANALYZE` only against the application tables.

## License

This project is licensed under [Creative Commons Attribution-NonCommercial-ShareAlike 4.0
International (CC BY-NC-SA 4.0)](https://creativecommons.org/licenses/by-nc-sa/4.0/). See
[`LICENSE`](LICENSE) for the project license notice and canonical legal-code link.

## Run with Docker Compose

Copy `.env.example` to `.env` and set the database and admin credentials before starting the
stack. The Compose file reads all deployment settings from `.env`:

```bash
cp .env.example .env
docker compose up --build
```

The collector and web service share the PostgreSQL container. The default BM subscription is
`everything`; use `BM_JOIN` to subscribe to a narrower source, destination, or repeater filter.

### Deploy with Dockge and Traefik

Use `compose-dockge.yaml` as the Dockge stack file and keep the project `.env` file beside it. It
pulls the published `ghcr.io/ea7klk/bm-lh-new:latest` image for the collector and web services.
The web service is not published directly on a host port; Traefik forwards to its internal port
from `APP_PORT` (default `8000`). Set `TRAEFIK_HOST`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER`,
and the router/service names in `.env` to match the Traefik installation. The external network
named by `TRAEFIK_NETWORK` must already exist on the Docker host.

For the default settings, create it once with:

```bash
docker network create traefik
```

Then deploy `compose-dockge.yaml` from Dockge. Set `COOKIE_SECURE=true` when using the HTTPS
Traefik route.

## Important interpretation

This first version calls each completed BM network session a QSO. A multi-turn conversation is
not directly identifiable from LastHeard alone; grouping separate transmissions into a single
conversation would require a configurable inactivity gap and should be added as a derived view,
not used to overwrite the exact source records.
