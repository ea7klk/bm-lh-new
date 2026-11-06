# Other DMR networks: feed assessment and implementation guide

Research date: **2026-11-06 (UTC)**. Repository inspected: `deploy/v1.2.2`
at `d338978`, containing the v1.2.2 application and its production image pin.
This is a planning document. No application, database, deployment, account, or
network-control changes were made for this investigation.

## 1. Findings and recommended direction

BMInfo can collect additional networks, but a working LastHeard display does
not necessarily supply completed transmissions suitable for its statistics.
Keep **activity observations** separate from **completed QSOs**.

The strongest documented route to additional completed-call data is
**FreeSTAR's partner `/v2/events` API**, subject to obtaining access. Its public
`/v2/lastheard` endpoint serves a different purpose. The most useful anonymous
WebSocket candidates verified here are **ADN Chile**, **FreeDMR UK**,
**TGIF**, and **PNWDigital Consolidated Call Watch**. Each still needs schema,
coverage, and production-use agreements before enabling unattended collection.

Recommended implementation sequence:

1. Add network/feed identity and a separate activity-observation path.
2. Arrange FreeSTAR partner access and validate its completed-event contract.
3. Pilot one ADN monitor for structured LastHeard observations; resolve its
   timezone and identity limitations before counting QSOs.
4. Add TGIF activity monitoring with an isolated, compatible protocol client.
5. Add FreeDMR and regional c-Bridge adapters when operators can supply a
   sufficiently stable, complete event format.

This sequence prioritizes data quality and implementation feasibility. The
popularity assessment below is a separate ordering.

## 2. Popularity ranking and its limits

There is no comparable, current public census of active users across these
networks. RadioID registrations are shared across networks; registered
accounts, configured devices, connected peers, and recent transmitters are
different populations. Bridges also make memberships and traffic overlap.

The table is sorted into **descending estimated global-reach/popularity
bands**. Ties are intentional: the evidence does not support an exact order
inside them. Bands after the BrandMeister reference are an inference from
operator-published scale, international footprint, and observed activity,
not measured market shares. Regional networks are listed separately rather
than assigning them a misleading worldwide position.

| Rank band | Network/family | Evidence and interpretation | Confidence |
|---|---|---|---|
| 1, reference | BrandMeister | A live `/v2/device` response contained **32,780 device records**, including 2,952 six-digit IDs. These are catalog records, not a count of currently online repeaters or active users. Already supported by BMInfo. [B1] | High for scale; catalog is not an activity census |
| 2–3, tied | TGIF | Its live homepage widget reported **4,949 active connections**, including 2,947 hotspots and 198 bridges. Categories do not necessarily partition the total. Strong evidence of a substantial connected community; not 4,949 distinct people. [T1] | Medium; instantaneous counter |
| 2–3, tied | DMR+ / IPSC2 ecosystem | International deployments and regional communities; DVSPh/Phoenix alone advertises **over 90 connected repeaters**. No current consolidated global peer/user count was established. FreeSTAR's IPSC2 master belongs here, not to a separate worldwide DMR+ population. [D1, D2] | Medium for broad reach; relative order with TGIF unresolved |
| 4–7, tied | FreeDMR | Official directory lists dozens of international servers. The official Global LastHeard service displayed **35 source servers** in its selection when inspected. A listed or contributing source is not necessarily an online master or a distinct community. [F1, F2] | Medium for international footprint; low for user ranking |
| 4–7, tied | DMR-MARC and associated regional c-Bridge networks | Established repeater-oriented ecosystem. Current PNWDigital and NEDECN operator resources demonstrate continuing activity. The old DMR-MARC homepage's registration figures must not be treated as current exclusive network membership. [C1, C2, C3] | Medium for continuing reach; current global activity unknown |
| 4–7, tied | ADN Systems | International server ecosystem with a working Chilean monitor and public server software. This investigation did not establish a comparable global connected-user count. Spanish server references exist, but their present feed availability needs separate verification. [A1, A2] | Low for relative popularity |
| 4–7, tied | System X / FreeSTAR | FreeSTAR operates System X and an IPSC2 service. Its public API returned **857 DMR LastHeard snapshot rows**, of which its response classified 366 as connected. This combines DMR sources and is not a System-X-only user count. [S1, S2] | Low for relative popularity |
| Regional, unranked | VKDMR / IPSC3 | The operator dashboard displayed 55/59 repeaters and 341 hotspots in a published snapshot. A meaningful Australian network; this does not establish a worldwide rank. Modern IPSC3 dashboards must be investigated separately from old IPSC2 monitors. [V1, V2] | Medium for regional significance |
| Regional/emerging, unranked | AmComm | Official directory exposes ten regional dashboards. No comparable global activity total was established. [M1] | Low |
| Unranked | openDMR.de and independent HBlink networks | A public activity dashboard exists at openDMR.de; network boundaries, scale, and a supported feed contract were not established. HBlink is software, not one worldwide network. [O1] | Insufficient evidence |

Do not add the rows together. In particular, DVSPh/Phoenix, FreeSTAR IPSC2,
QuadNet access, System X bridges, and c-Bridge interconnects can share traffic.
Do not assume that similarly named `opendmr.net` and `opendmr.de` describe the
same service; the former could not be reached during this investigation.

To produce a defensible strict ranking later, obtain an agreed common metric,
such as distinct transmitting radio IDs over the same seven UTC days, with
known coverage for every network. Report observed-feed activity separately
from an estimate of the entire network.

## 3. Feed feasibility matrix

**Observed** means a short anonymous read succeeded here. **Documented** means
the operator/schema/source describes the interface, without an authenticated
end-to-end test. A successful public read establishes technical reachability,
not an ongoing collection or redistribution agreement.

| Target | Interface | Access verified | Can it immediately produce reliable BMInfo QSO statistics? |
|---|---|---|---|
| TGIF | Socket.IO at `https://tgif.network`, default `/socket.io/`, Engine.IO **3** | Observed guest authentication status 200 and 49 `lastheard` messages during a short browser observation | **No.** Completion, duration, stream reuse, and identity changes need confirmation |
| DMR+ IPSC2, sampled DL hotspot master | HTTPS `/_monitor.html`; dashboard refreshes HTML | HTTP 200 observed; page code polls HTML every second | **No.** Bounded display, local-master scope, and no durable event identity |
| FreeSTAR public API | `GET https://api.freestar.network/v2/lastheard?mode=dmr&window=24h` | HTTP 200, JSON snapshot observed | **No.** Recent occupancy/heard rows have no per-call duration or call ID |
| FreeSTAR partner API | `GET https://api.freestar.network/v2/events?mode=dmr` | OpenAPI documented; unauthenticated request returned **401** | **Best candidate after access and contract validation** |
| FreeDMR UK hotspot monitor | Native WebSocket `wss://www.freedmr.uk/wsdash1/` | Observed `conf,log` subscription and eight `l` frames | Conditional; human-readable logs omit identity/date information and can truncate duration |
| FreeDMR Global LastHeard | `https://freedmr-lh.gb7fr.org.uk/` | Server-rendered table with timestamps and durations observed | Conditional; ask for underlying structured feed/history API before building an HTML collector |
| ADN Chile monitor | Native WebSocket `wss://7302.adn.systems/ws` | Observed `i` and `h` JSON messages and `l` text logs | Conditional; row timezone, timestamp meaning, and stable event identity unresolved |
| PNWDigital c-Bridge aggregation | Native WebSocket `wss://pnwdigital.net/ws`, used by `/ccw/` | Twelve `archive` messages observed | Conditional; validate terminal lifecycle, row fields, aliases/IDs, timezone, and replay coverage |
| System X FreeSTAR dashboard | Native WebSocket `wss://dmr.freestar.network/wss/` in deployed JavaScript | Source inspected, connection not tested | Prefer documented partner API; dashboard payloads include HTML fragments |
| VKDMR IPSC3 | Guest overview and `/dashboard/radios` LastHeard page | Both pages reachable; page-specific JavaScript inspected | No verified machine-readable LastHeard subscription contract in this investigation |
| AmComm / openDMR.de | Public dashboards | Directory/page inspection only; sampled AmComm HTTPS node timed out | Not enough evidence to specify a production adapter |

## 4. Adapter plans

### 4.1 TGIF: reachable, with a different Socket.IO generation

Primary evidence: [T2–T4]. The inspected browser library identifies itself as
Socket.IO **2.4.0**; the actual browser connection used `EIO=3`.

Observed browser sequence:

```text
Connect: https://tgif.network, path /socket.io/, WebSocket transport
On connect: emit handshake with the guest value used by the public page
Wait for: status == 200
Then emit: cli-state {scope: "*", page: "lastheard", action: "lh-backlog"}
Receive: lastheard and, where delivered, lastheard_backlog
```

The anonymous page's handshake value was empty. A backlog request was seen,
but a backlog response/replay guarantee was not established. Revalidate guest
access with TGIF before production; do not substitute an account's dashboard
cookie or self-care token.

Observed record fields included `timestamp`, `talkgroup`, `radio_id`,
`repeater_id`, `streamid`, `callsign`, `shortname`, `talkgroup_name`, `rssi`,
and `ber`. No explicit start/end pair or duration appeared in that schema.
The UI treats `repeater_id == 0` as an ending/display-clear signal and handles
speaker changes within a stream. Confirm the server's semantics rather than
turning those UI conventions into a duration calculation.

BMInfo's current Python Socket.IO 5.x/Engine.IO 4.x client is not a drop-in
client for this deployed protocol. Use an isolated adapter process/runtime
with an appropriate supported client or an operator-provided modern endpoint;
do not downgrade the shared application dependencies. Avoid implementing the
Engine.IO heartbeat protocol from scratch. Store activity observations until
completed-transmission semantics are proven.

### 4.2 DMR+ / IPSC2: select a master and establish its coverage

The inspected DL hotspot dashboard loads `/_monitor.html` through jQuery on
a one-second timer. Its table includes time, seconds, repeater, caller ID,
timeslot, TG, BER, and RSSI. This is an observed HTML interface, not a
documented network-wide WebSocket API. [D3]

Implementation route: ask the chosen operator for a supported event export.
If only HTML polling is available, agree a polling interval, establish the
history depth and timezone, and retain overlapping snapshots. Do not assume
the page's one-second browser refresh is permission for an additional
continuous scraper or that it guarantees complete history.

DVSPh/Phoenix explicitly limits the talkgroups available through other IPSC2
servers. Select DVSPh feeds for DVSPh coverage rather than expecting one
arbitrary IPSC2 master to provide all DMR+ calls. [D1]

FreeSTAR's talkgroup-directory API is useful metadata, but a talkgroup list is
not a LastHeard feed. Its newer partner call-event API is a separate interface.

### 4.3 FreeSTAR / System X: distinguish public snapshots from partner events

The operator documents public JSON, a **60-request/hour/IP** budget, ETag
handling for `/directory`, and `Retry-After` on rate limits. Public snapshots
normally cover 24 hours and are removed from public views after 48 hours.
The schema defines a separate authenticated transmission interface. [S1–S3]

| Interface | Fields/contract | Intended BMInfo use |
|---|---|---|
| `/v2/lastheard?mode=dmr&window=24h` | `records`, `generated_at`; rows include `radio_id`, `callsign`, `status`, `talkgroup`, `source_host`, `source_id`, `last_seen` | Activity/presence only |
| `/v2/directory` | Network catalog with an ETag | Names and source-to-network classification |
| `/v2/events?mode=dmr&after=<cursor>&limit=1000` | Partner Bearer or `X-API-Key`; `event_id`, `call_id`, `started_at`, `ended_at`, `duration_seconds`, `status`, `timeslot`, `ingress_id`, `source_host` | Candidate completed-QSO ingestion |

The partner schema specifies terminal statuses `completed`, `interrupted`,
and `timed_out`; `include=started` adds key-up events. It describes a stable
`call_id` across start/end, distinct event IDs, integer replay cursors, and
**24-hour event retention**. These partner behaviors are documented, not
verified with a credential here. A `401` confirmed access is gated.

Implementation steps:

1. Request partner access and approved polling/rate limits; keep the credential
   in environment-backed configuration and deployment secrets.
2. Fetch a terminal-event page, validate it, and atomically persist its events,
   QSO updates, and cursor. Use `call_id` for transmission identity and
   `event_id` for event identity.
3. Drain additional pages without exceeding the approved budget. Reuse the
   returned cursor on empty pages. Make replay after a crash idempotent.
4. Detect outages beyond the retention window and expose a coverage gap.
5. Obtain the exact duplicate-collapse and terminal-status policy. The schema
   documents additional cross-master collapse behavior for `include=started`;
   do not assume every query mode returns the same event population.

Classify `source_host` using verified source metadata: `mode=dmr` alone does
not distinguish System X from FreeSTAR IPSC2. Public polling should leave
quota headroom, for example one LastHeard request every two minutes plus
infrequent catalog reads. Partner polling frequency requires its own agreement.

### 4.4 FreeDMR: global aggregator and individual master feeds

The official Global LastHeard page links to
`https://freedmr-lh.gb7fr.org.uk/`. It exposes server-rendered rows with ISO UTC
timestamps, decimal durations, source-server identity, and subscriber/TG IDs.
Only 100 calls were loaded on the sampled page. No supported subscription or
machine-readable history endpoint was established. Request that interface
from the operator before using the display as an ingestion contract. [F2]

For the UK hotspot monitor, the deployed `hbmon.js` uses:

```text
wss://www.freedmr.uk/wsdash1/
Send: conf,log
Receive: l<text log line>
Other subscriptions: conf,main,lsthrd_log  (payload format depends on fork)
```

The deployed path overrides the upstream examples' port 9000; discover each
master's actual published URL rather than guessing ports. [F3–F5]

The inspected FDMR-Monitor source produces START/END human-readable logs. Its
END log formats duration with `int(float(...))` and omits the upstream stream
ID; displayed time can omit the calendar date. Consequently, log parsing can
lose precision and reliable identity. Prefer an agreed structured export of
the server's bridge events. Otherwise keep source-specific fingerprints,
mark precision/time uncertainty, and restrict the feed to observations until
those limitations are resolved.

A master's OpenBridge traffic can include mirrored calls. “Observed on this
master” must not become “all FreeDMR calls.” Even the global aggregator's
participating sources and routing/filtering need confirmation.

### 4.5 ADN Systems: a verified structured WebSocket candidate

The Chilean monitor accepted:

```text
wss://7302.adn.systems/ws
Send: conf,main,lsthrd_log,log
i + JSON: {lastheard: [...], ctable: ...}
h + JSON: {rows: [...]}
l + text: monitor log line
```

Observed rows contained `date`, `qso_time`, `qso_type`, `system`, `tg_num`,
`tg_callsign`, `dmr_id`, and `subscriber`; `qso_time` was a string. [A1–A4]

The inspected code reads decimal-second durations but converts stored UTC
dates into the server's display timezone without an offset in the resulting
string. The public config response did not disclose that timezone. Obtain
the IANA zone and date semantics, or preferably an offset-aware UTC export.
Handle DST ambiguity explicitly; do not assume UTC because the backing
database uses it.

These are bounded history snapshots, not an identified event log. Confirm
whether `date` identifies start or completion and obtain a stable call ID.
Until then, repeated rows can be deduplicated only heuristically and cannot
guarantee exact QSO counts. The root `adn.systems/ws` returned HTML rather
than an upgrade; use a specific monitor. The Spanish `2141.adn.systems`
HTTPS endpoint was unreachable from this environment; that is not evidence
that ADN Spain has ceased operating.

Upstream source also supports optional compressed WebSocket frames (`z=1`).
The observed plain `/ws` path avoids that extra codec. Verify deployed
versions before sharing an adapter between ADN servers.

### 4.6 DMR-MARC / regional c-Bridges: PNWDigital is a concrete candidate

PNWDigital's documented Consolidated Call Watch combines four regional
c-Bridges, including imported traffic. Its page connects to
`wss://pnwdigital.net/ws`. An anonymous probe received `archive` envelopes with
`type`, `site`, `hash`, and a ten-element `data` array. [C1, C4]

The page source additionally handles `create`, `update`, `delete`, `ping`, and
`seq`; application-level `ping` requires JSON `{"type":"pong"}`. Column
meanings include start time, duration, source peer/radio aliases, destination
bridge group, RSSI, and loss. Array positions must be taken from the inspected
version; do not infer them from visual column order, which users can change.

Plan an adapter state machine keyed by `(site, hash)`. Preserve archive rows,
merge field updates, and validate whether `delete` is the definitive end of a
transmission. Its UI retains ended calls for display. Confirm numeric radio
and talkgroup identity, timestamps, precision, and replay depth with the
operator. Aliases alone are insufficient for BMInfo's normal ID semantics.

This feed covers PNWDigital's participating bridges, not the entire
DMR-MARC ecosystem. NEDECN and other c-Bridge operators need separate coverage
agreements or feeds; do not assume the same consolidated API exists there.

### 4.7 Other candidates and boundaries

| Candidate | What was established | Required next step |
|---|---|---|
| System X FreeSTAR monitor | Deployed JavaScript uses `/wss/`, `conf,` subscriptions, and HTML dashboard panes. [S4] | Prefer the partner events API; validate any master-specific monitor feed separately |
| VKDMR/IPSC3 | Guest overview and actual LastHeard navigation route `/dashboard/radios` were reachable. [V1, V2] | Ask the operator for a supported read-only API/subscription and current guest limitations |
| AmComm | Official regional dashboard directory; sampled HTTPS master timed out. [M1] | Choose a reachable operator-approved master and inspect its actual client/protocol |
| openDMR.de | Public LastHeard/activity display. [O1] | Establish operator, network scope, protocol, and schema before recommending ingestion |
| Independent HBlink/RYSEN networks | Shared software ancestry does not guarantee a shared protocol or network identity | Register one feed per approved deployment; pin its schema/version |
| XLX and mixed-mode bridges | May carry DMR observations alongside other modes | Treat them as separately scoped bridge observations; filter mode and avoid counting mirrored traffic twice |

Do not join a voice talkgroup, register a hotspot, open a radio bridge, capture
audio, or enable network-control commands merely to collect metadata.

## 5. Normalize observations before using `store_event`

Introduce a provider-neutral contract. The following is a design example,
not an implemented payload or a captured operator record:

```json
{
  "network": "systemx",
  "feed": "freestar-partner-events",
  "coverage": "operator-declared participating masters",
  "event_id": "upstream-event-id",
  "call_id": "upstream-transmission-id",
  "event_type": "completed",
  "radio_id": 1234567,
  "callsign": "EXAMPLE",
  "talkgroup_id": 123,
  "timeslot": 1,
  "started_at": "2026-11-06T12:00:00Z",
  "ended_at": "2026-11-06T12:00:03Z",
  "duration_resolution_ms": 1000,
  "identity_quality": "native",
  "time_quality": "upstream-utc"
}
```

Distinguish four record classes: live activity, completed transmission,
interrupted/timed-out transmission, and presence snapshot. Expiry of a socket
or presence record is not proof of call completion. Persist incomplete or
uncertain observations separately from statistically eligible QSOs.

For eligible completed transmissions:

- Keep `duration_ms = round((Stop - Start) * 1000)` and inclusive three-second
  boundary behavior. If reconstructing start from an end and duration, label
  it as reconstructed and retain the source's actual precision.
- Preserve the separate raw-event threshold and idempotent session semantics.
  An upstream filter may already discard short events: BMInfo cannot recover
  those or promise identical kerchunk counts across feeds.
- Validate finite numbers, units, timezone, ordering, and plausible bounds.
  Never invent exact times from browser receipt intervals.
- Apply TG 9 exclusion by default. Review other service destinations per
  network; the BrandMeister list is not automatically universal.
- Treat a DMR stream ID as potentially short-lived and reused. Scope it by
  network, feed/system, boot/session epoch where available, and transmission
  identity. Handle a new speaker within a continuing stream explicitly.
- Deduplicate retransmissions and repeated snapshots within a feed. Preserve
  provenance for sightings on different masters. Cross-network bridge
  matches should be an explicit correlation layer, not automatic deletion of
  similarly timed calls.
- Allowlist metadata fields. Do not retain authentication data, dashboard
  cookies, private/admin objects, or unnecessary IP/location fields in raw
  audit payloads. Keep logs at operational levels, not per-packet payload dumps.

## 6. Required BMInfo changes

### Storage and compatibility

The current schema assumes one network. Simply starting another collector
against it would mix unrelated talkgroups and can overwrite sessions.

| Existing assumption | Required design |
|---|---|
| `QSO.session_id` is the primary key | Add explicit upstream identity and network/feed provenance. Preserve existing BrandMeister IDs; generate namespaced IDs for new feeds and enforce appropriate source-scoped uniqueness |
| `Talkgroup.talkgroup_id` is globally unique | Use uniqueness on `(network, on_air_talkgroup_id)` and a surrogate relation for QSO references |
| QSO FK targets the numeric talkgroup ID | Backfill a relation to the correct network-specific talkgroup; distinguish ORM internal IDs from on-air IDs in every serializer/query |
| `RawEvent.payload_hash` is globally unique | Scope raw-event identity to its feed/network, or namespace the hash input; retain existing BM deduplication behavior |
| One `collector` heartbeat | Track each feed's connection, last event, last durable write/checkpoint, and coverage gaps. Preserve existing BM status compatibility |
| Every completed event comes from BM | Add an observation type/quality gate; only approved completed-event adapters can write statistical QSOs |

Use new Django migrations, staged backfills, and measured index creation on
the populated production tables. Seed existing rows as BrandMeister. Measure
row counts, index size, lock time, and available disk before planning the
migration; do not combine a bulk rewrite with activation of new feeds.

`QSO.talkgroup_id` currently appears as a numeric destination in serializers
and report grouping. Changing the FK without updating those call sites would
leak internal database IDs as radio talkgroup IDs. Add regression fixtures
where TG 91 has different names and meanings on two networks.

### Code and UI work map

| Area | Files to inspect/update in a future implementation |
|---|---|
| Existing BM adapter and normalization | `bminfo/dashboard/collector.py`, `lastheard_client.py`, `qso_rules.py` |
| Feed supervisor, checkpoints, and commands | New focused modules under `bminfo/dashboard/`; `management/commands/collect_brandmeister.py` remains compatible |
| Network/feed models and migrations | `bminfo/dashboard/models.py`, new files in `dashboard/migrations/` |
| Metadata ownership | `dashboard/talkgroups.py`; do not apply BM-specific pinned names/MCC mapping indiscriminately to other networks |
| Public queries and response compatibility | `dashboard/core_views.py`, `live_views.py`, `public_views.py`, `report_views.py`, `utils.py` |
| Authenticated live stream | `dashboard/live_consumers.py`; preserve the existing Channels routing and server-side authentication/filter enforcement |
| Selectable network and visible coverage | Dashboard, reports, live-QSO templates, and export queries; update English, Spanish, German, and French catalogs |
| Runtime configuration | `config/settings.py`, `.env.example`, both Compose files, README, and Fleet source as needed |

Keep existing URLs, `sessionId`, numeric `destinationId`, and BrandMeister
default behavior compatible. Add explicit network selection rather than
silently changing existing totals to an all-network aggregate. Carry network
selection through reports, exports, saved filters, and live subscriptions.
Label partial-master coverage and unavailable duration instead of displaying
unknown values as zero.

### Runtime, recovery, and limits

- Use one supervised adapter per feed and a bounded writer queue; avoid
  unbounded database work in socket callbacks. Close Django thread-local
  connections outside request processing, as the current collector does.
- Retain the existing three web workers and 120-connection database target
  until measurements justify changes. Account for writers, metadata tasks,
  maintenance, and pgAdmin before increasing parallelism.
- Use exponential reconnect backoff with jitter, resubscribe after reconnect,
  and make shutdown interruptible. Let protocol libraries handle their own
  transport heartbeat rules; TGIF EIO3 and BM EIO4 differ.
- For replayable feeds, commit data and cursor together. On reconnect, overlap
  replay and rely on durable deduplication. Track cursor retention/expiry.
- For display snapshots/log streams, record gaps explicitly. A reconnect
  backlog or finite history is not a guarantee of lossless delivery.
- Respect published limits and `Retry-After`. Do not copy a browser's refresh
  rate blindly. Do not disable TLS verification or bypass access controls.
- Do not expose legacy HBlink report ports on the public Internet. The
  inspected legacy monitors unpickle some backend report messages; consume
  only an operator-controlled trusted interface or a safe JSON bridge.
- Make each feed independently disableable. Roll back new adapters without
  removing observations or altering the existing BrandMeister collector.

## 7. Delivery plan and acceptance criteria

### Phase A: confirm the upstream contract

For each chosen operator, record endpoint and version, approved access,
coverage, credentials/rate limits, timestamp and duration semantics, native
call/event identity, replay depth, duplicate/bridge behavior, attribution,
and permitted retention/redistribution. Request test fixtures with start/end,
short and long calls, reconnects, and source changes. No operator messages
were sent during this investigation.

First decision: obtain FreeSTAR partner access, or explicitly choose an
activity-only pilot. Public FreeSTAR LastHeard and TGIF display rows must not
be treated as a substitute for the partner call-event contract.

### Phase B: prepare isolation and migrations

Implement network/feed models, namespace identity, migrations/backfills, and
separate activity storage. Preserve the BM-only response baseline. Validate
the plan against a disposable PostgreSQL copy before scheduling production
DDL. Keep every new feed disabled by default.

### Phase C: validate one adapter in isolation

Acceptance tests must cover:

- The same completed transmission arriving twice, after restart, and through
  a replay page boundary produces one source-scoped QSO.
- Equal stream IDs and talkgroup numbers on different networks remain
  distinct. Reused stream IDs/new speakers are not merged accidentally.
- Exactly 3.000 seconds is retained; lower durations are excluded as specified.
  Source rounding/filtering limitations are visible and documented.
- Invalid, absent, or ambiguous times/durations remain observations and do
  not contribute invented talk time.
- A dropped END event, missing START, out-of-order updates, empty snapshots,
  daylight-saving ambiguity, rollover at midnight, and a replay-expired cursor
  cannot silently corrupt totals.
- A provider disconnect/dead writer degrades that feed's status without
  claiming healthy collection or breaking web liveness.
- Existing BM data/API fixtures, authenticated reports, authorization rules,
  and Channels subscription behavior continue to work.
- All supported locales expose understandable network/coverage/status labels.

Run `manage.py check`, `makemigrations --check --dry-run`, and the full Django
suite with PostgreSQL. Add protocol fixtures and adapter-specific integration
tests instead of depending on live third-party services in ordinary CI.

### Phase D: controlled production activation

Measure event rates, queue age, writes, database connections, storage growth,
reconnects, rate-limit responses, and upstream/ingest lag. Re-estimate retention
from actual raw payload sizes. Enable one feed with visible partial-coverage
labels, compare its output with the operator dashboard for an agreed period,
then expand. Build/release and change the pinned image through Fleet only
after separate implementation/activation authorization.

## 8. Investigation record and sources

Only short public reads/subscriptions were used. Socket connections were
closed after observation. No authentication secrets were obtained, no partner
credential was tested, no voice connection was made, and no long-running
collector was installed. Failure to reach a host reflects this environment
and observation time, not necessarily a network outage.

### Primary web/operator sources

- **B1:** [BrandMeister device catalog](https://api.brandmeister.network/v2/device), live JSON counted locally; current collector also inspected in this checkout.
- **T1:** [TGIF homepage](https://tgif.network/) and its [live widget JSON](https://tgif.network/index.php?live-widget=1), point-in-time connection counts.
- **T2:** [TGIF Last Heard](https://tgif.network/lastheard.php), inline connection/subscription logic and short browser observation.
- **T3:** [Deployed TGIF Socket.IO library](https://tgif.network/assets/js/socket.io.js), inspected version header; [LastHeard client](https://tgif.network/assets/js/lastheard.js) and [session cache](https://tgif.network/assets/js/lh_session_cache.js).
- **T4:** [TGIF API helper](https://tgif.network/api_helper.php). Its self-care/static-talkgroup API should not be assumed to be the LastHeard subscription API.
- **D1:** [DVSPh operator overview](https://www.dvsph.net/), reach and routing limitations.
- **D2:** [DMR-MARC's DMR+ partnership description](https://www.dmr-marc.net/FAQ/dmrplus-america.html), historical background rather than a current user census.
- **D3:** [Sample IPSC2 master](https://ipsc2-dl-hotspot.xreflector.net/) and its [monitor HTML](https://ipsc2-dl-hotspot.xreflector.net/_monitor.html), live source inspection.
- **F1:** [FreeDMR worldwide server directory](https://www.freedmr.uk/index.php/freedmr-servers/). Generated directory dates and cached copies differ; it is not a live online census.
- **F2:** [Official Global LastHeard link](https://www.freedmr.uk/index.php/dashboard/global-last-heard/) and [aggregator](https://freedmr-lh.gb7fr.org.uk/).
- **F3:** [FreeDMR UK hotspot monitor](https://www.freedmr.uk/dash1/moni.php) and its [deployed JavaScript](https://www.freedmr.uk/dash1/scripts/hbmon.js).
- **S1:** [FreeSTAR services/API description](https://freestar.network/services/), public access and fair-use terms.
- **S2:** [Public DMR LastHeard](https://api.freestar.network/v2/lastheard?mode=dmr&window=24h), sampled JSON; counts describe returned records, not exclusive network users.
- **S3:** [FreeSTAR OpenAPI schema](https://api.freestar.network/v2/schema) and [interactive docs](https://api.freestar.network/v2/docs). Schema was retrieved directly as YAML; partner `/events` was tested only without credentials and returned 401.
- **S4:** [System X dashboard](https://dmr.freestar.network/dashboard/) and [deployed monitor JavaScript](https://dmr.freestar.network/dashboard/scripts/monitor.js).
- **A1:** [ADN](https://adn.systems/) and [Chilean monitor](https://7302.adn.systems/); only the latter's `/ws` was a confirmed monitor subscription.
- **A2:** [Spanish operator's ADN migration announcement](https://ea5gvk-dmr.zigor.es/2024/05/02/adios-a-freedmr-espana-y-bienvenido-a-adn-systems-espana/), dated 2024; not proof of current Spanish endpoint availability.
- **C1:** [PNWDigital Callwatch documentation](https://pnwdigital.net/callwatch/), regional coverage and imported traffic.
- **C2:** [NEDECN operator downloads](https://nedecn.org/downloads/) and [repeater report](https://reports.nedecn.org/NEDECN/NEDECN-RADIOID-REPEATER-TABLE.html), evidence of current regional infrastructure.
- **C3:** [DMR-MARC homepage](https://www.dmr-marc.net/), historical registration figures excluded from current popularity comparisons.
- **C4:** [PNWDigital Consolidated Call Watch](https://pnwdigital.net/ccw/), inspected inline WebSocket client and anonymous archive-message probe.
- **V1:** [VKDMR IPSC3 overview](https://ipsc3.vkdmr.com/dashboard), snapshot counters and public-view labeling; [actual LastHeard route](https://ipsc3.vkdmr.com/dashboard/radios), HTTP 200 without a login redirect.
- **V2:** [Current VKDMR operator FAQ](https://www.vkdmr.com/dashboard/content/faq), IPSC3 behavior and operator contact routes.
- **M1:** [AmComm](https://amcomm.network/) and [regional dashboard directory](https://amcomm.network/dashboards).
- **O1:** [openDMR.de](https://opendmr.de/), public activity page only; no verified production feed contract.

### Pinned upstream source inspected

These are implementation references, not guarantees that every server runs
the same revision. The two older monitor repositories are archived.

- **F4:** [HBMonv2 at `8e085f9`](https://github.com/sp2ong/HBMonv2/tree/8e085f9f86cf8435a5baf5d51e5c862986243017): `monitor.py`, `html/scripts/hbmon.js`.
- **F5:** [FDMR-Monitor at `cd57a18`](https://github.com/yuvelq/FDMR-Monitor/tree/cd57a18f351bbd370688c902efda4ae7efaf8136): log formatting, subscriptions, and upstream report handling.
- **A3:** [ADN-Monitor at `b64b9e7`](https://github.com/Amateur-Digital-Network/ADN-Monitor/tree/b64b9e7247b4b4a83e5f0f6e55708c8f8ea3b8ff): `monitor/src/adn_monitor/application/dashboard_ws.py`, `dashboard_rows.py`, and `time_utils.py`.
- **A4:** [ADN WebSocket hub at that revision](https://github.com/Amateur-Digital-Network/ADN-Monitor/blob/b64b9e7247b4b4a83e5f0f6e55708c8f8ea3b8ff/monitor/src/adn_monitor/infrastructure/fastapi/ws_hub.py): framing and initial-history behavior.

Before implementation, repeat endpoint/schema checks and replace inferred
behavior with operator-confirmed contracts wherever possible.
