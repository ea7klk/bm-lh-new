# Dependency and runtime currency audit

Date: 2026-11-13
Scope: the Python runtime the application ships on, the Django version it pins,
and every direct package in `bminfo/requirements.txt`.
Status: **findings only — no version has been changed.** This document is the
remediation plan; applying it is a separate, deployable change.

Versions are stated as of the audit date. Re-check before acting on them:
`python --version`, `pip list --outdated`, and the upstream pages named below.

## Verdict

- **Python 3.12 is out of bugfix support** and has been since 2025-04-02. It
  still receives security fixes until 2028, but no ordinary bug or regression
  fixes: 3.12.16 is effectively the end of the line for non-security issues.
- **Django is 3 patch releases behind, two of which are security releases.**
  Production pins `Django==6.1` exactly; the current release is 6.1.3.
- **Two dependencies are a major version behind** (`gunicorn` 23 vs 26.x/27,
  `reportlab` 4 vs 5). Everything else is current.
- **7 of 11 direct dependencies are already at their latest release.**
- **Builds are not reproducible**: `requirements.txt` uses open ranges for 10 of
  11 packages and the Docker base image is an unpinned floating tag. Production
  and a local checkout have already resolved different versions of `psycopg`
  (3.3.9 vs 3.3.8) from identical source.

## How this was checked

Not from changelogs alone. Every claim below was measured on this host:

- Read the pinned versions from `bminfo/requirements.txt`, `bminfo/Dockerfile`,
  `.github/workflows/ci.yml`, and the local `.venv`.
- Read the versions **actually installed in the running production image** with
  `kubectl exec -n bminfo deploy/web -- pip list`.
- Queried PyPI for the current release of each direct dependency and the Python
  version classifiers each one supports.
- Queried endoflife.date for the support status of Python 3.11–3.15 and Django
  5.x–6.x.
- Ran `pip-audit` against `requirements.txt` and against the proposed upgrade
  set.
- Built four throwaway environments — Python 3.12, 3.14.8 and 3.15.0 with the
  proposed versions — and ran the **full 258-test suite** in each, then booted
  gunicorn behind the Uvicorn worker and checked `/health/`, the lifespan
  handshake and graceful shutdown.
- Diffed the rendered report output between `reportlab` 4.5.1 and 5.1.0.

## Python

| Where | Version | Status |
| --- | --- | --- |
| Production image (`python:3.12-slim`) | 3.12.16 | current 3.12 patch |
| CI (`actions/setup-python`) | 3.12 (latest patch) | current 3.12 patch |
| Local `.venv` / system `python3` | 3.12.3 | **13 patches behind 3.12.16** |
| `README.md`, `AGENTS.md` | "Python 3.12" | — |

Upstream position:

| Series | Latest | Bugfix support ends | Security (EOL) |
| --- | --- | --- | --- |
| 3.12 | 3.12.16 | **2025-04-02 (passed)** | 2028-10-31 |
| 3.13 | 3.13.18 | **2026-10-01 (passed)** | 2029-10-31 |
| 3.14 | 3.14.10 | 2027-10-01 | 2030-10-31 |
| 3.15 | 3.15.2 | 2028-10-09 | 2031-10-31 |

**Finding.** The project runs on a series that has had no bugfix releases for
eighteen months, and 3.13 has already left bugfix support too. 3.14 is the only
release currently in full support, with 3.15 the newest stable.

**Why this matters concretely.** Python bugfix releases carry fixes for the
interpreter itself — crashes, incorrect results, regressions in `asyncio`,
`ssl`, `sqlite3`. A service on a security-only series keeps those bugs. There is
no single CVE to point at; the cost is a tax of unfixed defects, not one
exploitable hole.

**Measured compatibility.** The suite passes unchanged on all three:

```text
Python 3.12.x, Django 6.1.3, gunicorn 26.2.0, reportlab 5.1.0   258 tests OK
Python 3.14.8, Django 6.1.3, gunicorn 26.2.0, reportlab 5.1.0   258 tests OK
Python 3.15.0, Django 6.1.3, gunicorn 26.2.0, reportlab 5.1.0   258 tests OK
```

gunicorn with the Uvicorn worker was booted on 3.14.8 and 3.15.0 as well:
`/health/` and `/` returned 200, every worker logged `Application startup
complete`, no `ASGI 'lifespan' protocol appears unsupported`, and `SIGTERM`
produced `Application shutdown complete` on each worker.

**Recommended target: 3.14.** It is a year old, in full bugfix support until
October 2027, and every dependency declares support for it. 3.15 works but was
released a month before this audit; adopt it in roughly six months, when the
ecosystem has caught up — note that neither `uvicorn` nor `gunicorn` yet lists a
3.15 classifier, even though both ran correctly here.

**The local environment is also stale.** `.venv` is on 3.12.3 while production
runs 3.12.16, so a local bug that depends on an interpreter fix cannot be
reproduced the way production behaves. Rebuild it on whatever series is adopted.

## Django

| Where | Version |
| --- | --- |
| `bminfo/requirements.txt` | `Django==6.1` (exact pin) |
| Production image | 6.1 |
| Current release | **6.1.3** (2026-11-03) |
| 6.1 series EOL | 2027-12-31 |

The pin is exact, which is good practice, but it was never advanced. Two of the
three missed releases are security releases, and their own release notes state
what they fix:

- **6.1.2** (2026-10-06) — "fixes one security issue with severity 'low', three
  security issues with severity 'moderate', and provides a fix for an
  insufficient security mitigation in 6.1."
- **6.1.3** (2026-11-03) — "fixes five security issues with severity 'low', one
  security issue with severity 'moderate', and several bugs in 6.1.2. It also
  adds compatibility with Python 3.15."

### Are we actually exposed?

A version scanner is not a risk assessment. `pip-audit` reports one advisory for
`Django==6.1`:

```text
Name   Version ID              Fix Versions
django 6.1     PYSEC-2026-3717 5.2.17,6.0.8,6.1.1
```

That is **CVE-2026-15830**, a denial of service in GeoDjango's
`GEOSGeometry` when parsing deeply nested `GEOMETRYCOLLECTION` values. `django.contrib.gis`
is not in `INSTALLED_APPS`, no spatial field, lookup or `GeometryField` exists in
this codebase, and the GEOS library is not installed. **Not reachable here.**

The advisories in 6.1.2/6.1.3, with reachability checked against this codebase:

| CVE | Issue | Reachable here? |
| --- | --- | --- |
| CVE-2026-77050 | DoS in `get_supported_language_variant()` via very long language codes | No — no `LocaleMiddleware`; the admin locale comes from our own normalised value |
| CVE-2026-84429 | DoS in HTTP header parsing | **Yes, in principle** — affects every Django request |
| CVE-2026-87890 | Request forgery via spatial lookup byte values | No — no GIS |
| CVE-2026-87975 | Privilege abuse in model formsets with editable primary keys | No — no admin inlines or formsets |
| CVE-2026-108147 | Memory exhaustion in `MultiPartParser` via base64 uploads | No — the app accepts no file uploads |
| CVE-2026-111211 | Log injection via unescaped geometry form values | No — no GIS |
| CVE-2026-111362 | SSRF via `crs` in GeoJSON geometries | No — no GIS |
| CVE-2026-113593 | Private-data exposure via empty/missing `Vary` headers | No — no `CacheMiddleware`, no `cache_page()` |
| CVE-2026-113733 | Password hash disclosure to view-only admin users with custom user models | No — `dashboard.User` *is* `AUTH_USER_MODEL` and inherits no password field (the advisory's two conditions) |
| CVE-2026-114582 | SSRF via `SpatialReference` string input | No — no GIS |

**Conclusion:** one advisory is plausibly reachable (header-parsing DoS), the
rest are not, and none is a "drop everything" emergency. This is still the
cheapest fix in the document — a patch bump with no code change — and it should
be taken because the *next* Django advisory will not be so conveniently out of
reach, and because being three releases behind means the code has never been
tested against the fixes that shipped in between.

## Direct dependencies

Resolved means what the production image actually installed on 2026-11-13.

| Package | Constraint | Resolved | Latest | Action |
| --- | --- | --- | --- | --- |
| `Django` | `==6.1` | 6.1 | 6.1.3 | **bump to `==6.1.3`** |
| `psycopg[binary]` | `>=3.2,<4` | 3.3.9 | 3.3.9 | ok |
| `gunicorn` | `>=23,<24` | 23.0.0 | 27.0.1 | **bump to `>=26.2,<27`** |
| `uvicorn[standard]` | `>=0.30,<1` | 0.54.0 | 0.54.0 | ok |
| `channels` | `>=4.2,<5` | 4.3.2 | 4.3.2 | ok |
| `openpyxl` | `>=3.1,<4` | 3.1.5 | 3.1.5 | ok |
| `reportlab` | `>=4.2,<5` | 4.5.1 | 5.1.0 | **bump to `>=5.1,<6`** |
| `python-socketio[client]` | `>=5.16,<6` | 5.18.1 | 5.18.1 | ok |
| `python-engineio` | `>=4.13,<5` | 4.15.1 | 4.15.1 | ok |
| `requests` | `>=2.31,<3` | 2.34.2 | 2.34.2 | ok |
| `boto3` | `>=1.35,<2` | 1.44.2 | 1.44.2 | ok |

### gunicorn 23 → 26

Four majors behind. The range `>=23,<24` was written when 23 was current and
never revisited, so the HTTP server has missed every change since:

- **24.0.0** — security: raises minimum versions of `eventlet`, `gevent` and
  `tornado` for CVEs in those worker backends. None of them is the worker used
  here (`UvicornWorker`), so this release does not fix a hole in this
  deployment, but it shows the series is where security fixes land.
- **25.0.0** — deprecates the `eventlet` worker.
- **26.0.0** — **breaking**: removes the `eventlet` worker. Also a security
  section (body-framing corrections, a hardened HTTP parser). Unaffected by the
  removal: the entrypoint uses `--worker-class uvicorn.workers.UvicornWorker`.
- **26.2.0** — lands cleartext HTTP/2 and an **HTTP/2 security fix**, plus
  streaming request bodies so a client that never ends a stream cannot grow a
  buffer without bound.
- **26.2.1 / 26.2.2** — tagged on GitHub but **never published to PyPI** (no
  files exist for either), so 26.2.0 is the newest installable 26.x.
- **27.0.0** (2026-11-10) — **breaking**: rejects requests whose
  `Transfer-Encoding` does not end with `chunked`. **27.0.1** fixes an ASGI
  keep-alive stall.

**Recommendation: `>=26.2,<27`.** It carries the HTTP/2 and request-body
hardening with three months of exposure. 27.0.1 also booted and served
correctly in a smoke test, but it is a day old and carries a breaking change in
request framing — exactly the kind of change to take deliberately, not as a
side effect of a security patch. Revisit 27 once it has aged.

### reportlab 4 → 5

The PDF report is the only consumer, through `platypus` (`SimpleDocTemplate`,
`Paragraph`, `Table`), `graphics.shapes` for the charts, and `lib.colors`.

Rather than trusting that the drawing and layout engine is unchanged, both
majors were used to render the same report from the same data and the output was
compared:

```text
reportlab 4.5.1 → 6 pages, 6610 characters extracted, QSO/Talkgroup sections present
reportlab 5.1.0 → 6 pages, 6610 characters extracted, QSO/Talkgroup sections present
```

The XLSX export is produced by `openpyxl` and is unchanged: identical sheet
names (`Talkgroups`, `Callsigns`) and 102 identical rows. `test_report_views.py`
also passes on 5.1.0, asserting `200`, `application/pdf` and the `%PDF` header.

**Recommendation: `>=5.1,<6`.** No behavioural difference observed. Keep the
report test as the guard — it is the only thing standing between a future
reportlab release and silently malformed PDFs.

## Reproducibility

Two problems, both visible in the table above:

1. **Ten of eleven packages have open ranges.** Two builds of the same commit at
   different times can install different versions. This has already happened:
   production runs `psycopg` 3.3.9 while the local `.venv` has 3.3.8, from
   identical source.
2. **The base image is a floating tag.** `FROM python:3.12-slim` currently
   resolves to 3.12.16, and will silently become 3.12.17, or move to a new
   Debian base, on whatever day the image is next built.

The fix is to keep `requirements.txt` as the human-authored constraint list and
add a compiled lock file that CI and the image build both install:

```bash
python -m pip install pip-tools
pip-compile --generate-hashes --output-file=bminfo/requirements.lock bminfo/requirements.txt
```

Then `Dockerfile` installs `-r requirements.lock` and `ci.yml` installs the same
file, so the tested set and the shipped set are provably identical. Pin the base
image by digest as well:

```bash
docker buildx imagetools inspect python:3.14-slim --format '{{json .Manifest.Digest}}'
# FROM python:3.14-slim@sha256:<digest>
```

A digest pin makes the base image an explicit, reviewable dependency rather than
a tag that changes under the build. Renovate or Dependabot can then propose
updates as pull requests, which is what makes currency sustainable instead of a
yearly archaeology exercise like this one.

## Remediation plan

Ordered so that the most valuable change is also the safest. Each step is
independently deployable and independently revertable.

1. **Django `==6.1` → `==6.1.3`.** One line, no code change, 258 tests pass,
   closes the reachable header-parsing DoS. Deploy this alone; it is the highest
   value per unit of risk in the document.
2. **`reportlab >=4.2,<5` → `>=5.1,<6`.** One line. PDF and XLSX output verified
   identical; `test_report_views.py` covers it.
3. **`gunicorn >=23,<24` → `>=26.2,<27`.** One line. Boot, lifespan and graceful
   shutdown verified on 3.12/3.14/3.15. Watch the web pod's logs for one
   rollout; the worker class is unchanged, so the risk is in request framing.
4. **Add the lock file and pin the base image digest.** No runtime change, but it
   is what stops this audit from being necessary again in the same form.
5. **Move the runtime to Python 3.14.** `Dockerfile`, `ci.yml`, `README.md` and
   `AGENTS.md`. Verified green on 3.14.8 including gunicorn. Do this as a
   separate deploy from steps 1–3 so a failure has one obvious cause. Rebuild
   `.venv` on 3.14 at the same time, so local and production stop diverging.
6. **Add a CI matrix**: 3.12 (or whatever is currently in production) plus 3.14,
   so the next Python upgrade is proven by CI rather than by an investigation.
   Add a scheduled `pip-audit` job so an advisory surfaces as a failed run
   instead of waiting for someone to ask.

### Acceptance checks for each step

```bash
python bminfo/manage.py check
python bminfo/manage.py makemigrations --check --dry-run
python bminfo/manage.py test dashboard.tests        # expect 258 tests, OK
pip-audit -r bminfo/requirements.txt                 # expect no reachable findings
```

Then, after the rollout: `/health/` returns 200, the web log has no
`appears unsupported`, no restart loop, and one PDF and one XLSX export download
and open.

## Not changed by this document

No version was bumped. Production still runs Django 6.1, gunicorn 23.0.0,
reportlab 4.5.1 on Python 3.12.16 — so the reachable advisory in step 1 remains
open until step 1 is applied and deployed.

## Limits of this audit

- Versions were current on 2026-11-13 and will drift; the method in "How this
  was checked" is the durable part.
- `pip-audit` matches version ranges, not reachability. The reachability column
  above is my reading of this codebase and could miss a path I did not think of.
- Only *direct* dependencies were assessed. Transitive packages (`asgiref`,
  `sqlparse`, `urllib3`, `certifi`, `botocore`, `pydantic`) are pulled in by
  ranges and were not individually reviewed; the lock file in step 4 is what
  makes them visible.
- Compatibility was proven by the test suite, a gunicorn boot and a rendered
  report — not by a soak test. Concurrency behaviour under load was not
  re-measured on the new versions; the worker and connection sizing in
  `REVIEW.md` still applies to the current versions until re-measured.
- No linter, formatter or static type checker is configured in this repository,
  so their absence is not listed as a discrepancy; it is a separate decision.
