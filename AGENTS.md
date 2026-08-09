# AGENTS.md

This guidance applies to the entire repository.

## Project

This repository contains one application: a Python 3.12 Django and Channels
service in `bminfo/`. It collects BrandMeister LastHeard events, stores them
in PostgreSQL, and serves dashboards, live updates, reports, account flows, and
administrative tools.

Read `README.md` before making substantial changes.

## Architecture

- Keep project configuration, root routing, and ASGI/WSGI setup in
  `bminfo/config/`.
- Keep application models, forms, views, consumers, collector logic, and admin
  tools in `bminfo/dashboard/`.
- Put schema changes in Django migrations. Never edit an applied migration to
  change current behavior; create a new migration.
- Put operator workflows in focused Django management commands under
  `bminfo/dashboard/management/commands/`.
- Keep HTML in `bminfo/templates/` and shared translations in
  `bminfo/dashboard/translations.json`.
- Preserve the Channels ASGI routing used by live-QSO websocket consumers.

## Coding conventions

- Match the surrounding Python and Django style, naming, type annotations, and
  module boundaries. Prefer focused changes over unrelated refactors.
- Use the Django ORM and transactions for database access. Avoid raw SQL unless
  the ORM cannot express the operation clearly or efficiently.
- Keep configuration environment-backed through `bminfo/config/settings.py`.
  Document new variables in `.env.example`, `README.md`, and both Compose files.
- Update every supported locale when changing user-visible text.
- Never log or commit passwords, secret keys, SMTP credentials, cookies, or
  tokens.
- Maintain compatibility for documented URLs and response shapes unless a
  breaking change is explicitly requested.
- For concurrency changes, keep the worker budget and PostgreSQL connection
  budget aligned. The default target is three async workers and 120 database
  connections for roughly 25 concurrent users.

## Data invariants

- One completed `SessionID` represents one QSO.
- Calculate duration as `round((Stop - Start) * 1000)` milliseconds.
- Exclude durations strictly below `KERCHUNK_THRESHOLD_SECONDS`; keep values
  exactly equal to it.
- Retain decoded events according to the raw-event threshold for auditing.
- Exclude local talkgroup 9 by default.
- Keep collector writes idempotent by session ID.

## Verification

Install dependencies with:

```bash
python -m pip install -r bminfo/requirements.txt
```

Run the relevant checks before handing off changes:

```bash
python bminfo/manage.py check
python bminfo/manage.py makemigrations --check --dry-run
```

Add regression tests for behavior changes and run Django tests against the
required PostgreSQL test database. Do not weaken tests to make changes pass.

## Change hygiene

- Inspect the working tree before editing and preserve unrelated user changes.
- Keep `docker-compose.yml` and `compose-dockge.yaml` aligned when their service
  contracts change.
- Update `README.md` for endpoint, setup, deployment, configuration, database,
  or operator-workflow changes.
- When changing retention or concurrency defaults, include the sizing basis and
  the assumptions used for capacity planning.
- Do not perform destructive database, Git, or deployment actions unless the
  user explicitly requests them and the exact target is verified.
- Summarize changed files and verification results when handing work back.
