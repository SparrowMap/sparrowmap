# `hub.py` architectural analysis

Status: Stage 0 characterization only. No runtime behavior is intentionally
changed by this document or its accompanying test.

## 1. Responsibilities currently in `hub.py`

`hub.py` is both the process entry point and the implementation of the HTTP
application. Its responsibilities are:

- Process configuration and startup: argument parsing, bind validation,
  `db.init()`, HTTP/TLS listener creation, simulator opt-in, and daemon-thread
  startup.
- HTTP transport: `BaseHTTPRequestHandler` adapter, HTTP/1.1 keep-alive,
  request-body limits/deadlines/draining, JSON/error/file responses, HEAD,
  content types, security headers, CORS, and cache headers.
- Capacity protection: request/route admission semaphores, heavy/ingest pools,
  tile-fetch limits, in-memory rate buckets, inflight accounting, slow-holder
  metrics, micro-cache and single-flight coordination.
- Static/public delivery: HTML shells, JavaScript/vendor/model assets, service
  worker, snapshots, installer redirect/probe, and basemap tile proxy/cache.
- Public map reads: sightings, individual sightings, tracks, nodes, stats,
  policy, leaderboard, audit, health, places, heat, aircraft, geocoding, and
  SSE live feed.
- Node lifecycle: enrollment, placement readback, token/key checks and
  rotation, signing-key registration, span consent, setup progress, heartbeat,
  bulk heartbeat, node identity, parked items, and node labels.
- Ingest orchestration: request authentication, rate accounting, signed-event
  verification, classification, timestamp clamping, privacy tiering, mirror
  stripping, snapshot/crop generation, review-pen/evidence handling, database
  insertion, heartbeat, and live-feed publication.
- Review/operator workflows: operator login/logout and authorization,
  reviewer login/logout/identity, queues, contributed items, held/retracted
  photos, verdicts, fixes, bulk actions, token issue/revoke, reporting, and
  purge.
- Compatibility/community workflows: help task votes, bug reports/admin,
  drive report/vote compatibility, signal observations/tokens, and disabled
  patrol reporting.
- Cross-cutting privacy/security policy: public redaction and anonymous plate
  aliases, true-versus-jittered node position handling, mirror route/storage
  restrictions, audit IP hashing, CSRF content-type enforcement, operator and
  reviewer credential boundaries, and open-proxy/static path guards.

## 2. Route contract inventory

The following is the Stage 0 contract inventory. “Auth” describes the check
performed by the current handler, not a proposed future policy. “Mirror” is
whether `mirror.route_allowed()` permits the route when `public_mirror` is on.
Successful JSON routes use `application/json`; HTML uses `text/html`; files use
their guessed or explicit MIME type; images use the image type; SSE uses
`text/event-stream`.

### Public/static and compatibility pages

| Verb | Route | Auth | Mirror | Status/content | Major side effect |
|---|---|---|---|---|---|
| GET/HEAD | `/`, `/about`, `/transparency`, `/status`, `/checksums`, `/hardware`, `/build16`, `/help`, `/aim`, `/app`, `/node`, `/key`, `/contribute`, `/signin`, `/login/camera`, `/drive`, `/planes`, `/review`, `/login`, `/rv`, `/rv/mine`, `/rv/pool`, `/rv/admin`, `/rv/photos` | none | yes, subject to mirror route policy | 200 HTML when asset exists; 404 otherwise | file read; nonce substituted in inline scripts |
| GET/HEAD | `/support`, `/donate` | none | yes | 200 HTML or 503 HTML if generated page absent | file read |
| GET/HEAD | `/business`, `/ipcamera` | none | yes | 301, `Location: /IPCamera`, `Cache-Control: public, max-age=3600` | none |
| GET/HEAD | `/IPCamera` | none | yes | 200 HTML/404 | file read |
| GET/HEAD | `/relay.py` | none | yes | 200 Python source/404 | file read |
| GET/HEAD | `/download` | none | yes | 302 to configured GitHub URL, or 404 JSON | outbound HEAD probe cached for 600s |
| GET/HEAD | `/sw.js` | none | yes | 200 JavaScript/404 | file read |
| GET/HEAD | `/vendor/<asset>`, `/static/<name>` | none | yes | 200 file/404; long cache for vendor, 60s for static | basename-constrained file read |
| GET/HEAD | `/snap/<name>` | none at transport layer | mirror-dependent | 200 image/404 JSON | basename-constrained snapshot read |

### Public map and discovery APIs

| Verb | Route | Auth | Mirror | Status/content | Major side effect |
|---|---|---|---|---|---|
| GET/HEAD | `/api/stats`, `/api/health`, `/api/policy`, `/api/places`, `/api/heat`, `/api/leaderboard`, `/api/whoami` | none | yes | 200 JSON; malformed/DB errors follow handler error behavior | DB reads; health updates high-water metrics |
| GET/HEAD | `/api/nodes` | none | yes | 200 JSON, heavy/cached | DB read; public node projection/jitter |
| GET/HEAD | `/api/sightings` | none | yes | 200 JSON, heavy/cached | DB read; anonymous redaction/alias mapping |
| GET/HEAD | `/api/sighting/<id>` | none | yes | 200 JSON or 404 | DB read; redaction |
| GET/HEAD | `/api/pending` | none | yes | 200 JSON | compatibility DB read for pending review items |
| GET/HEAD | `/api/track/<plate_hash>` | none | yes | 200 JSON or 404/empty | DB read; alias resolution and redaction |
| GET/HEAD | `/api/plate` | none | no-store path | 200 JSON or validation/error status | DB plate search; search is not audit logged |
| GET/HEAD | `/api/audit` | operator-gated | no on mirror | 200 JSON or 401/404 | DB read |
| GET/HEAD | `/api/live` | none | yes | 200 SSE | subscribes to in-memory feed until disconnect |
| GET/HEAD | `/api/aircraft`, `/planes` | none | yes | 200 JSON or HTML | aircraft read / file read |
| GET/HEAD | `/api/geocode` | none | yes | 200 JSON, 429 on budget exhaustion | outbound Nominatim reverse-geocode, in-memory 24h cache |
| GET/HEAD | `/api/scanner` | none | yes | 200 JSON | scanner/configuration read |
| GET/HEAD | `/api/download` | none | yes | 200 JSON | outbound release HEAD probe via shared cache |
| GET/HEAD | `/api/tile/<z>/<x>/<y>.png` | none | yes | 200 image, 404 on invalid/upstream failure, 429 on budget | bounded outbound Carto fetch and disk cache/prune |

### Node and camera APIs

| Verb | Route | Auth | Mirror | Status/content | Major side effect |
|---|---|---|---|---|---|
| POST | `/api/enroll` | bearer for existing node; new enrollment rate-limited | yes | 200 JSON; 400/403/429 | create/update node, possibly mint reviewer token |
| POST | `/api/sightings` | node token/signature or phone bearer | yes | 200 JSON; 400/401/403/429 | classify, redact/store, insert sighting, review/mirror writes, heartbeat/feed |
| POST | `/api/node/key` | node token | yes | 200 JSON; 400/401/404 | update Ed25519 public key and audit |
| POST | `/api/node/span` | node token | yes | 200 JSON; 401/404 | update publish-span consent and audit |
| GET | `/api/node/me` | node token | no on mirror | 200 JSON; 401/404 | returns true node coordinates and placement |
| POST | `/api/node/whoami`, `/api/node/progress`, `/api/node/label`, `/api/node/confirm`, `/api/node/parked` | node/reviewer/operator checks vary by route | no on mirror where operator/review surface is disabled | 200 JSON; 400/401/403/404 | read/update node setup, labels, confirmations, review queue |
| POST | `/api/key/qr`, `/api/key/rotate` | node token or local/operator check | no on mirror | 200 JSON; 400/401/403/404 | generate QR or replace node bearer token |
| POST | `/api/heartbeat`, `/api/heartbeat/bulk` | node token(s) | yes | 200 JSON; 400/401/413/429 | update node liveness |
| POST | `/api/sighting/fullres` | node token | no on mirror | 200 JSON; 400/401/404 | attach/request full-resolution evidence |
| POST | `/api/signals` | signal token | yes | 200 JSON; 400/401 | write signal observations |

### Review and operator APIs

| Verb | Route | Auth | Mirror | Status/content | Major side effect |
|---|---|---|---|---|---|
| GET | `/api/rv/me`, `/api/rv/queue`, `/api/review/queue`, `/api/rv/contributed`, `/api/rv/retracted`, `/api/rv/held`, `/api/rv/progress`, `/api/rv/tokens` | reviewer token; operator for token listing | no on mirror | 200 JSON; 401/404 | DB reads; reviewer last-used update |
| GET | `/api/rv/crop/<id>`, `/api/rv/retracted/photo/<id>`, `/api/rv/held/photo/<id>` | reviewer scope/trust | no on mirror | 200 image or 401/404 | constrained file read |
| POST | `/api/rv/login`, `/api/rv/logout` | token for login; none for logout | no on mirror | 200 JSON/set-cookie or 401 | reviewer cookie set/cleared |
| POST | `/api/rv/verdict`, `/api/rv/held/fix`, `/api/rv/retracted/delete`, `/api/rv/edit`, `/api/review`, `/api/review/edit`, `/api/review/bulk` | reviewer token; operator/trust varies | no on mirror | 200 JSON; 400/401/403/404 | verdict, crop repair, deletion, edits, bulk review/audit |
| POST | `/api/rv/tokens/new`, `/api/rv/tokens/revoke` | operator | no on mirror | 200 JSON; 401/403/404 | issue/revoke reviewer credentials |
| POST | `/api/rv/my-token` | node token | no on mirror | 200 JSON; 401/404 | mint/retrieve own reviewer token |
| POST | `/api/operator/login`, `/api/operator/logout` | token for login; none for logout | no on mirror | 200 JSON/set-cookie or 401 | operator cookie set/cleared |
| POST | `/api/purge` | operator | no on mirror | 200 JSON or 401 | retention purge and evidence cleanup |

### Help, bugs, drive, and reporting compatibility

| Verb | Route | Auth | Mirror | Status/content | Major side effect |
|---|---|---|---|---|---|
| GET | `/api/help/next`, `/api/help/stats`, `/api/help/img/<id>` | voter/id shape for reads | yes | 200 JSON/image or empty/404 | task/vote DB/file reads |
| POST | `/api/help/vote` | voter identity in body | yes | 200 JSON; 400 | writes separate `label_votes.db` |
| GET | `/admin/bugs`, `/api/bug/list`, `/api/bug/shot/<id>` | admin/operator varies | no for private surface | 200 HTML/JSON/image or 401/404 | bug reads |
| POST | `/api/bug`, `/api/bug/close`, `/api/bug/delete` | operator/admin varies | no for private surface | 200 JSON; 400/401/404 | bug DB/filesystem updates |
| GET | `/api/drive/reports` | none | yes | 200 JSON | DB read |
| POST | `/api/drive/report` | disabled | yes | 410 JSON | deliberately no write |
| POST | `/api/drive/vote` | rate-limited | yes | 200 JSON; 400/429 | DB vote |
| POST | `/api/report` | anonymous rate-limited | no-store | 200 JSON; 400/429 | DB report/audit IP hash |

All POST routes in the handler’s sensitive set require
`Content-Type: application/json`; failures are 415 before route processing.
Successful and failed responses receive the handler’s common security headers
unless the route writes a redirect/error directly.

## 3. HTTP-specific versus application/domain logic

### HTTP/transport-specific

`Handler.handle_one_request`, `_drain_body`, `_too_busy`, `_cache_control`,
`_send`, `do_HEAD`, `_tile`, `_json`, `_err`, `_file`, `_body`, `_is_local`,
`client_ip`, `_gated`, `_micro_key_for`, `_micro_ttl`, `_route_label`, `do_GET`,
`do_POST`, route parsing, content-type checks, cookies, status/header mapping,
SSE framing, and static path normalization are transport concerns.

The module-level tile cache/pruner, rate limiter, `Feed` queue mechanics,
admission semaphores, micro-cache/single-flight, download probe, and TLS
listener are infrastructure concerns even though they live beside route code.

### Application/domain

The route bodies also perform domain work that should eventually be behind
services: node enrollment and movement, node-token and Ed25519 verification,
classification and public-tier gating, timestamp/skew policy, plate aliases and
redaction, snapshot/evidence/crop policy, mirror stripping/quarantine,
review verdict transitions, public map projections, audit decisions, retention,
signal matching, help voting, and bug/report state transitions.

`db.py` currently contains both persistence and domain/policy decisions (for
example public projections, review transitions, retention queries, and driver
report lifecycle). This is intentionally preserved and recorded as technical
debt, not treated as clean layering.

## 4. Direct dependencies

### Database

- `db.init()` and thread-local SQLite connections through `db.connect()`.
- Primary `DATA/sparrow.db` for nodes, sightings, reviews, reports, tokens,
  signals, aircraft/driver data, audits, and migrations.
- `help_api` opens separate `DATA/label_votes.db`; task crops live under
  `DATA/label_task`.
- `db.py` owns schema/migrations and most SQL used by `hub.py`.

### Filesystem

- `core.DATA` and `core.PUBLIC` roots.
- `DATA/snaps` for stored public/evidence snapshots.
- `DATA/tiles/<z>/<x>/<y>.png` for basemap cache.
- `DATA/inbox` for mirror relay quarantine.
- `DATA/operator.token`, node key files under the node key store, generated
  `PUBLIC/support.html`, public/vendor/static assets, certificates under
  `certs/`, and optional model/runtime files.
- Snapshot/review/mirror modules add additional evidence, held, retracted, and
  crop paths; route code applies basename/path guards in several places.

### Network and process

- Inbound stdlib threaded HTTP and optional TLS listener via `dualstack.serve`.
- Outbound Carto tile proxy, Nominatim reverse geocoder, and GitHub release
  availability probe.
- Background daemon threads for janitor, optional simulator, and TLS listener;
  one request thread per accepted connection from the threaded server.

### Configuration/import-time behavior

- `core` resolves repository paths, loads/creates `config.json`, and exposes
  `CONFIG`, `DATA`, `PUBLIC`, and `SNAPS` at import time.
- `hub.py` imports most policy modules at module import and lazily imports
  optional/network/ML dependencies inside routes or workers.
- `db.init()` and all worker startup occur in `main()` after bind validation.

## 5. Global state and background work

- Tile cache count and prune lock.
- Process start time and health peaks.
- Feed subscriber list and per-subscriber bounded queues.
- Global rate buckets and lock.
- Tile-fetch semaphore.
- Handler request/heavy/ingest semaphores, inflight route counters/locks,
  slow-holder metrics, micro-cache, micro-flight events and locks.
- Geocode cache, anonymous hash alias map/day marker, download probe cache.
- `privacy`/`mirror`/`db`/snapshot modules have additional process/global or
  filesystem state not owned directly by the handler.
- `_janitor()` purges retention every 600 seconds.
- `_simulator()` is opt-in (`--sim`) and publishes synthetic events.
- `_tls_listener()` starts a second listener when certificate files exist.
- `_sse()` holds a request thread for the client lifetime and drops slow
  subscribers rather than blocking publishers.

## 6. Security and privacy boundaries to preserve

- Operator authentication must fail closed when enabled; proxy/socket address
  must not grant operator power. Operator cookies are HttpOnly,
  SameSite=Strict, and conditionally Secure.
- Reviewer tokens are separate from operator credentials, hashed in storage,
  scoped, revocable, and audited.
- Node bearer tokens authorize only the owning node; Ed25519 public keys verify
  signed event claims, and private signing keys remain on the camera.
- Private plate text is not stored as readable text; private images are
  redacted/destroyed before persistence; public-tier publication remains
  conservative and classification-gated.
- Public map nodes use jittered/public coordinates; `/api/node/me` is the
  exceptional true-coordinate route and is node-token protected.
- Mirror mode must not receive/store true positions, private identifiers,
  private images, training crops, or operator routes. Storage-time stripping is
  the boundary, not merely response-time redaction.
- Plate searches are not logged as searches; audit IPs use the existing privacy
  helper. Proxy access logging must remain disabled in deployment.
- CSRF-sensitive cookie routes require `application/json`; CORS is open only
  for public surfaces. Common headers include no-referrer, nosniff, DENY
  framing, CSP, and path-dependent cache controls.
- Tile proxy accepts only checked integer coordinates and a fixed upstream
  template; static/snapshot paths are constrained to repository-selected files.
- Request body caps, deadlines, draining, overload responses, and bounded
  queues protect keep-alive correctness and availability.

## 7. Current coupling/dependency graph

```text
hub.py Handler
  ├─ transport state (locks, semaphores, caches, rate buckets, Feed)
  ├─ core.py ──> config.json + DATA/PUBLIC/SNAPS paths
  ├─ db.py ────> sparrow.db schema, SQL, domain transitions
  ├─ privacy.py ──> pepper/config, redaction, retention, audit IP
  ├─ classify.py ──> classifier/model/config policy
  ├─ snapshot.py ──> image decode/redaction/crops + SNAPS
  ├─ nodes.py ──> enrollment, geometry, Ed25519 verification
  ├─ mirror.py ──> mirror policy + inbox/evidence files
  ├─ review_api.py ──> review DB/file transitions
  ├─ review_auth.py/operator_auth.py ──> credential checks/cookies
  ├─ help_api.py/bugs.py/qr.py/node_label.py ──> feature operations
  ├─ dualstack.py ──> listeners
  └─ urllib/ssl/threading ──> external services and runtime

route branches ──> all of the above directly
node_key.py ──> nodes.py (lazy import)
review_api.py ──> db.py, privacy/snapshot/core policy
mirror.py ──> core.py and filesystem
operator_auth.py/review_auth.py ──> core.py and db.py
```

Risks are direct route-to-policy calls, direct route-to-filesystem/database
access, large handler methods with hidden shared state, lazy imports that make
the graph runtime-dependent, and policy cycles such as review/auth/domain code
calling persistence while the handler also performs persistence decisions.

## 8. Proposed target structure and expected Stage 5 graph

```text
hub.py (compatibility launcher)
  └─ sparrowmap.runtime.server
       ├─ sparrowmap.transport.basic
       ├─ sparrowmap.transport.capacity
       ├─ sparrowmap.transport.responses
       ├─ sparrowmap.transport.static
       └─ sparrowmap.routes
            ├─ public_routes
            ├─ node_routes
            ├─ review_routes
            ├─ support_routes
            └─ compatibility_routes
                 └─ sparrowmap.services
                      ├─ ingest
                      ├─ map_reads
                      ├─ nodes
                      ├─ review
                      └─ maintenance
                           └─ existing behavioral modules
                                (db, privacy, classify, snapshot, nodes,
                                 mirror, review_api, auth modules)
```

Expected direction after Stage 5 is transport → routes → service seams →
existing behavioral modules. The target is deliberately not a new repository,
storage, configuration, or infrastructure abstraction.

Intentional remaining violations for the later containerization/distributed
architecture phase:

- SQLite remains process-local and thread-local, with schema/domain logic in
  `db.py`.
- Filesystem paths remain shared local state for snapshots, tiles, mirror inbox,
  tokens, keys, and generated pages.
- In-memory rate limits, caches, feed subscribers, and single-flight events are
  not cluster-wide.
- The threaded stdlib server and daemon workers remain in one process.
- External tile/geocode/GitHub calls remain synchronous from request paths.
- Configuration remains import-time global state.
- ML/classification and image work remain coupled to the hub process and its
  local filesystem.
- Mirror/home replication remains filesystem/SSH-oriented rather than a
  distributed message or object-store boundary.

## 9. Preserved findings (not fixes)

Stage 0 records apparent defects/inconsistencies here rather than changing them:

- Broad exception handling and silent fallbacks exist in several existing
  modules and route branches.
- Some direct route branches write database/filesystem state instead of using a
  service seam.
- Global in-memory coordination is not suitable for multiple hub processes.
- Lazy imports and `sys.path` mutation for the simulator create
  runtime-dependent import behavior.
- Route-specific direct response writes can differ from `_send()`’s common
  headers/caching behavior.
- Existing concurrency, cache, mirror, and status behavior is characterized as
  observed behavior, including any races or dead paths found by tests.

These items are candidates for separate bug-fix or architecture commits; they
are not corrected by the Stage 0 work.

### 9.1 Findings added during behavioral characterization (pass 2)

Observed by running the unmodified hub as a live process
(`tools/test_hub_behavior.py`), not visible from source inspection alone:

- **A newly enrolled node's wrong-token request is masked by the status
  gate, not the token gate.** `/api/sightings` checks `nd["status"] != "active"`
  before `_token_ok(nd)`, and a freshly enrolled node defaults to a non-active
  ("paused") status (`auto_approve_nodes` is `False` by default). So a wrong
  or missing token against a brand-new node currently returns `403 "node is
  paused"`, not `401 "bad node token"` — the 401 path is only reachable once a
  node has been approved. Behaviorally harmless (both are rejections) but the
  status code a caller sees depends on node lifecycle state, which is not
  documented anywhere as intentional.
- **`/api/rv/tokens` is gated by `_is_local()` (operator identity), not by
  `review_auth.identify()` (reviewer identity)**, despite living under the
  `/api/rv/*` prefix alongside genuinely reviewer-scoped routes like
  `/api/rv/progress`. The prefix is not a reliable signal for "this route
  requires a reviewer credential" — each route must be checked individually.
- **`/api/enroll` triggers a real outbound network call (`road.resolve()` via
  Overpass) for any `kind` other than `"mobile"`/`"public_cam"`**, even in a
  fully offline/isolated test environment with no network mocking. This is
  expected/by-design (span resolution needs a live road lookup), but it means
  any characterization or future test of node enrollment must either pin
  `kind` to `"mobile"`/`"public_cam"` or provide network mocking/a local
  Overpass fixture — there is no in-process way to skip it otherwise.

### 9.2 Findings added during Stage 2B extraction

- **`/api/audit` has no authentication check in `hub.py`**, despite §2's
  route table documenting it as `operator-gated`. The route runs
  `db.connect().execute(...)` and returns the audit log to any caller with no
  `self._is_local()` or other auth gate; its only protection is
  `mirror.route_allowed()`'s mirror-exclusion list, which is a no-op on an
  ordinary (non-mirror) deployment. Left unmodified and unextracted pending a
  deliberate decision (see §11.11).

**Stage 2D1 routing note:** the four Stage 2D1 routes explicitly reject a
missing node token before proceeding (`GET /api/node/me`, `POST
/api/node/whoami`, `POST /api/node/parked`, `POST /api/node/span`). They do
not themselves exercise the permissive `_token_ok()` fallback or the
status-before-auth lifecycle disclosure that remain applicable elsewhere. The
security findings remain documented as inherited behavior, not as a Stage 2D1
refactor bug or an acceptance criterion to fix during route extraction.

**Line count reporting:** local pre-stage → post-stage numbers are stage-local and
must not be conflated with the original fork baseline. For the current branch,
`hub.py` is locally `4194 -> 2885` lines from the repository HEAD to the
working tree after Stage 2D1/2D2 extraction, while the original fork baseline
was `4428` lines; the fork baseline-to-current delta is therefore `4428 -> 2885`.

## Stage 2E pre-analysis: reviewer / operator route family

This is an analysis-only pass for the remaining reviewer/operator surface after
Stage 2D. The route family is still a single-`hub.py` boundary; the goal is to
separate the routes that are thin HTTP adapters from the ones that still contain
real review/evidence/domain logic.

### 2E route inventory and auth model

| Route family | Auth actually used | Trust boundary | Mirror | Sensitive data / side effects | Extraction suitability |
|---|---|---|---|---|---|
| `/api/rv/me`, `/api/rv/queue`, `/api/review/queue`, `/api/rv/contributed`, `/api/rv/retracted`, `/api/rv/held`, `/api/rv/progress` | `review_auth.identify(headers)`; cookie or bearer reviewer token | reviewer-scoped, own/pool token scopes, plus direct camera-key fallback for own queue | blocked on mirror | reads review queues, queue counts, contributed/retracted/held work, and reviewer `last_used` state; some reads are scoped to reviewer node membership | Good thin Stage‑2 adapters; domain read logic still in `db.py` and `review_api.py` |
| `/api/rv/crop/<id>`, `/api/rv/retracted/photo/<id>`, `/api/rv/held/photo/<id>` | same reviewer auth as above, plus scope checks inside the route/reader | reviewer-only evidence access | blocked on mirror | reads private evidence/photo files and review-held/retracted crop paths; can leak candidate or private imagery if mis-scoped | Thin adapter for auth + path gating; evidence retrieval remains domain logic |
| `/api/rv/login`, `/api/rv/logout` | token in body for login; cookie is set/cleared; logout is anonymous | reviewer session | blocked on mirror | sets `sparrow_rv` cookie; authenticates reviewer identity | Thin Stage‑2 adapter |
| `/api/rv/verdict`, `/api/rv/held/fix`, `/api/rv/retracted/delete`, `/api/review`, `/api/review/edit`, `/api/review/bulk` | reviewer token or local operator path depending on route; local-only on administrative bulk/confirm routes | reviewer choices and operator-maintained review corrections | blocked on mirror | writes verdicts, queue state, audit log, label notes, crop refinements, bulk review sweeps, retractions, and classifier re-labeling | Some pure adapter pieces; several decisions still in `review_api.py` and `db.py` |
| `/api/rv/my-token` | node token check via `_token_ok(nd)` after `db.node(node_id)`; not reviewer auth | self-service camera ownership | blocked on mirror | mints or reissues a reviewer token for a camera's own or pool scope; writes review-token DB rows | Thin Stage‑2 adapter; keep the self-service node-auth semantics unchanged |
| `/api/rv/tokens`, `/api/rv/tokens/new`, `/api/rv/tokens/revoke` | `self._is_local()` / operator-only, not reviewer token | operator authority | blocked on mirror | token listing, minting, and revocation; direct admin control over reviewer credentials | Stage‑2 operator admin adapters, but keep auth and DB logic exact |
| `/api/operator/login`, `/api/operator/logout` | operator auth secret in body/cookie; logout clears cookie | operator session | blocked on mirror | sets `sparrow_op` cookie | Thin Stage‑2 adapter |
| `/api/purge` | `self._is_local()` (operator only) | local operator | blocked on mirror | retention purge / timestamp cleanup in `privacy.purge_expired()` | Thin Stage‑2 adapter with maintenance/service seam |
| `/api/audit` | no auth gate in current implementation; only mirror exclusion | public-by-default audit read | blocked on mirror | returns raw audit rows from `db.connect()` | Must remain an explicit audit-risk route; not a candidate for route normalization during Stage 2 |
| `/api/review/queue` alias to `/api/rv/queue` | reviewer token | reviewer-scoped | blocked on mirror | same as reviewer queue | adapter family |
| `/api/review/bulk` | local-only operator gate | operator; bulk review sweep of selected IDs | blocked on mirror | bulk verdict, labels, audit, dispatch, queue state | stays in operator family, not node family |

### Key findings for Stage 2E

- `SEC-01` remains applicable: `/api/audit` is documented as operator-gated in the route inventory but currently reads the audit DB with no `self._is_local()` or reviewer/operator credential check in `hub.py`.
- `/api/rv/tokens` is operator-gated despite the `/api/rv/*` prefix; the route family is not uniformly reviewer-only.
- Operator authority is still modeled as either loopback-local (`self._is_local()`) or bearer/cookie auth depending on config; `operator_auth.required()` makes the auth requirement strict when deployed behind TLS or when `operator_requires_auth` is explicitly true.
- Reviewer tokens are separate from operator tokens and are hashed before storage, scoped to `own` and `pool`, and revocable via operator-only admin routes.
- Evidence/photo access is particularly sensitive; routes like `/api/rv/crop/<id>`, `/api/rv/retracted/photo/<id>`, and `/api/rv/held/photo/<id>` are review-only and must remain behind reviewer authentication and scope checks.
- Review verdict mutations, bulk review actions, and deletion paths still make domain transitions in `db.py` / `review_api.py` rather than in a thin route shim alone.

### Proposed Stage 2E extraction sequence

1. `2E1` reviewer read/session routes: `/api/rv/me`, `/api/rv/queue`, `/api/review/queue`, `/api/rv/contributed`, `/api/rv/retracted`, `/api/rv/held`, `/api/rv/progress`, `/api/rv/login`, `/api/rv/logout`, and the photo/crop GET routes.
   - Pre/post characterization: login cookie behavior, reviewer identification, queue scoping, access control, and photo file gating.
2. `2E2` reviewer decision/evidence mutations: `/api/rv/verdict`, `/api/rv/held/fix`, `/api/rv/retracted/delete`, `/api/review`, `/api/review/edit`, `/api/review/bulk`.
   - Pre/post characterization: verdict state transitions, audit writes, label side effects, and bulk-review safety.
3. `2E3` operator/token administration: `/api/operator/login`, `/api/operator/logout`, `/api/rv/my-token`, `/api/rv/tokens`, `/api/rv/tokens/new`, `/api/rv/tokens/revoke`, `/api/purge`.
   - Pre/post characterization: operator auth, loopback/local bypass, token issuance/revocation, and retention purge semantics.
4. `2E4` Stage‑3 deferrals: any route that requires substantial review-domain or evidence-service logic beyond HTTP parsing and response mapping; these should stay behind the `hub.py` compatibility boundary until Stage 3.

### Remaining route sweep after Stage 2E

The remaining ungrouped GET/POST routes outside completed Stage‑2D families are the reviewer/operator/admin set above plus the public/private audit/reporting surfaces (`/api/audit`, `/api/report`, `/api/bug/*`, `/admin/bugs`, `/api/help/*`, `/api/drive/*`) that are already documented as separate families. After Stage 2E, the route sweep should verify that no GET/POST paths remain outside the known Stage‑2/Stage‑3 boundaries.

This stage remains analysis-only; no production code is changed.

## 10. Stage 0 review gate

Before Stage 1 begins, review this inventory and run:

```powershell
python tools\test_hub_contract.py
python tools\test_hub_behavior.py
```

The first test checks that the documented route inventory remains
synchronized with the route literals and dispatch verbs in `hub.py`, and that
key non-negotiable security markers remain present. The second launches the
unmodified `hub.py` as an isolated subprocess (temp working directory, fresh
`data/`/`config.json`, `SPARROW_BIND=127.0.0.1`, an ephemeral port, no
`--sim`) and exercises real HTTP/socket behavior for the transport and
security mechanisms Stage 1A/1B will move: ordinary GET/HEAD/POST, unknown
routes, malformed bodies, public/operator/reviewer/node auth boundaries,
public-mirror route filtering, security headers, cache-control policy,
CORS/CSRF, static file serving and path-traversal rejection, tile-proxy
allow-listing, rate limiting, and POST-body draining/keep-alive correctness.
It does not attempt exhaustive per-route coverage, real outbound network
calls, Ed25519 signature verification, or `MAX_REQUESTS` admission overload —
see the file's trailing comment block for the full list of what is
deliberately out of scope for this pass.

## 11. Stage 1B pre-analysis: stateful transport infrastructure

Stage 1A moved only the stateless body/serialization primitives (`_body`,
`_drain_body`, `_json`, `_err`, `_route_label`, `do_HEAD`) into `transport.py`.
Everything below is still 100% in `hub.py` and is analyzed here, before any
code moves, so Stage 1B can be sequenced into small, independently testable
commits rather than one large cut.

### 11.1 `Handler._send` (response serialization + policy, lines ~781-885)

- **Current state/ownership**: one method that does five unrelated things in
  sequence: (a) mint a per-response CSP nonce and substitute it into the body,
  (b) fill/evict the micro-cache (`Handler._MICRO`) if a `_micro_key` was
  staged, (c) write status/Content-Type/Content-Length, (d) decide
  Cache-Control via `_cache_control()` unless the caller supplied its own, (e)
  write the fixed set of security headers (Referrer-Policy,
  X-Content-Type-Options, X-Frame-Options, CSP, conditional CORS) and any
  caller-supplied `extra` headers, then write (or, for HEAD, suppress) the
  body.
- **Mutable globals/locks/lifecycle**: `Handler._MICRO` (dict) +
  `Handler._MICRO_LOCK`, both class-level and shared for the process
  lifetime; `self._status`/`self._nonce`/`self._head_only` are per-request
  instance attributes set here and read by `_cache_control` and `do_HEAD`.
  `self.__dict__.pop("_micro_key", None)` is taken (and thus cleared)
  unconditionally on every call, whether or not caching happens — this is the
  single point that prevents a micro-cache key leaking onto the next
  keep-alive request (see finding below on `_too_busy`).
- **Inputs/outputs**: inputs are `code`, `body: bytes`, `ctype`, `extra`
  headers, plus handler state (`self.path`, `self._micro_key`); output is the
  full wire response, plus a possible write into `Handler._MICRO`.
- **Coupling**: to `Handler._cache_control` (path-based policy), to
  `Handler._MICRO`/`_MICRO_LOCK` (cache), to `self.path` (CORS
  allow/deny-list by prefix), to nothing in `db`/`privacy`/`classify`/etc. —
  it is pure HTTP-and-cache-policy, but the cache and CORS/security-header
  pieces are inseparable from the wire-write mechanics as currently written.
- **Covered by Stage 0 tests**: security headers present (Referrer-Policy,
  X-Content-Type-Options, X-Frame-Options, CSP with nonce), Cache-Control
  values for a handful of representative paths (`/api/stats`, `/api/nodes`,
  a 403, a 404), CORS `*` present/absent by path prefix, HEAD body
  suppression (indirectly, via `do_HEAD`).
- **NOT yet characterized**: micro-cache hit/fill behavior itself (a repeat
  GET actually being served from `Handler._MICRO` rather than recomputed);
  the micro-cache TTL boundary/eviction-at-200-entries behavior; the
  `_micro_key` clear-on-every-call invariant that guards against key leakage
  across a keep-alive connection when a request bypasses `_send` (`_too_busy`
  writes raw, see §11.6); the exact interaction between a non-extra
  Cache-Control caller and a caller-supplied one; nonce uniqueness across
  responses on the same connection.
- **Proposed extraction seam**: `_send` should not move as a single method.
  Split it into layers with the wire-write mechanics (nonce substitution,
  Content-Length, HEAD-suppression, header-write, exception-swallowed
  `wfile.write`) as one small function, and treat `_cache_control`,
  micro-cache read/write, and the header table (security headers + CORS
  decision) as separate, independently named responsibilities that the
  wire-write function receives as already-decided values, not as fields it
  computes. This lets each piece be extracted and tested (given a path and a
  status, what Cache-Control comes out; given a path, is CORS present) without
  touching the socket-writing code at all.
- **Module placement**: wire-write mechanics belong in `transport.py`
  (genuinely stateless given already-decided headers). `_cache_control`,
  the micro-cache, and the CORS/security-header decision are policy with
  process-lifetime state and belong in a separate module (tentatively
  `response_policy.py` or `cache.py` for the micro-cache specifically) so
  `transport.py` does not become a second dumping ground for "everything
  HTTP-shaped."
- **Dependency direction after extraction**: `hub.py` → `transport.py` (wire
  mechanics) and `hub.py` → `cache.py`/`response_policy.py` (policy/cache);
  `transport.py` should not need to import the policy module, but the
  wire-write function needs the *decided* Cache-Control/security headers
  passed in, so `Handler._send`'s thin remaining wrapper in `hub.py` is what
  composes policy decisions before calling into `transport.py` — no cycle.
- **Defects/races (documented, not fixed)**: none newly found beyond what
  Stage 0 already lists; the `_micro_key` pop-on-every-call is a *fix* for a
  prior bug (leakage via `_too_busy`), not a remaining defect, but it is
  fragile: any future response path that writes to `wfile` directly (as
  `_too_busy` does) without going through `_send` reintroduces the same class
  of leak, and nothing currently guards against a new such path being added.

### 11.2 `_cache_control` (lines ~697-779)

- **Current state/ownership**: pure function of `self.path` and
  `self._status`, no mutable state of its own; reads `Handler._CACHEABLE_API`
  (a frozenset, effectively a constant).
- **Inputs/outputs**: `self.path`, `self._status` (must already be set) →
  a Cache-Control string.
- **Coupling**: only to `Handler._CACHEABLE_API`. No DB/filesystem/network.
- **Covered by Stage 0 tests**: yes, for `/api/stats`, `/api/nodes`, a 403,
  and a 404 (see §11.1). Not covered: `/vendor/`, `/api/tile/`, `/static/`,
  page-shell paths (`/`, `.html`, `/about`, etc.), `/api/places`,
  `/api/sightings`, the generic `/api/*` 15s fallback, or the >=400 override
  combined with a normally-cacheable path.
- **NOT yet characterized**: most of the path-prefix branches above.
- **Proposed extraction seam**: trivial — this is already a pure function of
  two inputs and a constant; move as-is into whichever module ends up owning
  cache policy (see §11.1), with `_CACHEABLE_API` moving alongside it.
- **Module placement**: separate policy module, not `transport.py` (it is
  cache/privacy policy, explicitly out of Stage 1A/1B's core-transport scope
  per the user's original constraints, but it is Stage-1B territory as
  "cache-control/privacy policy" listed in this analysis request).
- **Dependency direction after extraction**: `hub.py` → policy module only.
- **Defects/races**: none found; it is stateless and side-effect-free.

### 11.3 Micro-cache + single-flight (`Handler._MICRO*`, `do_GET`, lines
~1174-1325)

- **Current state/ownership**: `Handler._MICRO` (dict: key → (timestamp,
  body bytes)), `Handler._MICRO_LOCK`, `Handler._MICRO_FLIGHT` (dict: key →
  `threading.Event`, marking an in-progress build), `Handler._MICRO_PARAMS`
  (constant: which query params key which cacheable route). All are
  class-level, shared for the process lifetime, unbounded except for the
  200-entry eviction in `_send` and the fact that `_MICRO_FLIGHT` entries are
  always removed in `do_GET`'s `finally`.
- **Inputs/outputs**: `self.path`/query string → a cache key
  (`_micro_key_for`) and a TTL (`_micro_ttl`, itself derived from
  `_cache_control`); a hit returns cached bytes directly; a miss makes exactly
  one thread ("leader") run `_do_GET_inner` while others ("followers") wait on
  an `Event` up to `min(ttl+5, 20)` seconds, then re-check the cache, then fall
  through to compute themselves if still stale — so a wedged/slow leader
  cannot hang a follower, only delay it.
- **Coupling**: to `Handler._cache_control`/`_micro_ttl` (policy), to
  `Handler._gated`/`_route_label` (admission, see §11.4), to
  `Handler._do_GET_inner` (all of application/domain logic, out of scope) —
  this is the crux of why `do_GET` cannot be extracted as a unit: the
  single-flight/cache decision must wrap the *call* to `_do_GET_inner`, not
  replace it, and `_do_GET_inner` is explicitly staying in `hub.py` until
  Stage 2.
- **Covered by Stage 0 tests**: none directly. The behavioral suite checks
  Cache-Control *header values* (a policy question) but never issues two
  overlapping requests to observe an actual cache hit, nor forces two
  concurrent misses to observe single-flight collapse.
- **NOT yet characterized**: (a) that a second GET within the TTL window
  returns byte-identical cached content without recomputation, observable
  indirectly (e.g., by having `_do_GET_inner` produce a value that changes
  between calls, such as `/api/stats`' uptime-derived fields, and asserting
  the second response is stale/frozen rather than fresh); (b) that the
  200-entry eviction in `_send` actually bounds `_MICRO`'s size; (c)
  single-flight collapse under concurrency — the two-thread case (one leader,
  one follower) can be characterized **deterministically** without a flaky
  flood: start one request, block `_do_GET_inner` on a controllable gate
  (impossible without a code seam, since the real `_do_GET_inner` cannot be
  paused) — see the sizing note below for a testable alternative; (d) the
  follower fallback-to-self-compute path when the leader is slow past
  `ttl+5`/20s (impractical to test without shortening the TTL/timeout, which
  changes behavior — likely NOT safely characterizable without a config knob
  that does not currently exist, so this should be recorded as an
  intentionally uncharacterized case rather than forced).
- **On deterministic characterization of the cache/single-flight without
  flakiness**: the *TTL-hit* case (a) is fully deterministic — issue GET,
  immediately issue GET again, assert same bytes/timestamp field, no
  concurrency needed. The *single-flight* case is harder because
  `_do_GET_inner` runs to completion in microseconds for cheap routes like
  `/api/stats`, giving no reliable window for a second thread to observe
  "leader in flight." A reasonably deterministic approach: use a route whose
  `_do_GET_inner` cost is nontrivial and comparatively stable (`/api/nodes`
  with a non-trivial row count in the harness's temp DB), fire N concurrent
  requests via a thread pool, and assert (not on exact interleaving, but on
  an *invariant*) that all N responses are byte-identical and that this is
  at least directionally consistent with one computation — this is weaker
  than proving single-flight occurred, and should be documented as such
  rather than oversold as a strong concurrency proof.
- **Proposed extraction seam**: extract `_micro_key_for`, `_micro_ttl`, and
  the cache-read/single-flight-wait/cache-write choreography as a single
  cohesive "micro-cache" unit that takes a zero-argument builder callable
  (i.e., what `_do_GET_inner` would be bound to) and a key/TTL, and returns
  bytes — mirroring the shape already implicit in `do_GET`. `do_GET` in
  `hub.py` would shrink to: compute path/ttl, delegate to
  `microcache.get_or_build(key, ttl, builder)`, still call through `_gated`.
- **Module placement**: separate module (`microcache.py`), not
  `transport.py` — it owns nontrivial process-lifetime state (`_MICRO`,
  `_MICRO_FLIGHT`) and a locking/waiting protocol, which is qualitatively
  different from `transport.py`'s per-call stateless helpers. Keeping it
  separate is exactly what avoids `transport.py` becoming a second monolith.
- **Dependency direction after extraction**: `hub.py` → `microcache.py`;
  `microcache.py` has no dependency on `hub.py`, `_do_GET_inner`, or any
  domain module — it only knows about a key, a TTL, and a builder callable.
- **Defects/races (documented, not fixed)**: the follower's fallback check
  (`hit and time.time() - hit[0] < ttl + 5.0`) uses a *different*, looser
  deadline (`ttl+5`) than the leader's own write, which is intentional
  slack, not a bug — but it does mean a follower can serve an entry that is
  up to 5s staler than what a fresh TTL check would allow, which is an
  existing, intentional behavior worth naming explicitly so Stage 1B doesn't
  "tighten" it as an accidental fix.

### 11.4 Admission/semaphore accounting (`Handler._INFLIGHT`/`_HEAVY`/
`_INGEST`, `_gated`, `_too_busy`, lines ~610-649, 673-685, 1095-1152)

- **Current state/ownership**: three `threading.Semaphore`s
  (`_INFLIGHT` at `MAX_REQUESTS`=200, `_HEAVY` at `MAX_HEAVY`=48, `_INGEST` at
  `MAX_INGEST`=40), all class-level, process-lifetime, non-reentrant; plus
  `_INFLIGHT_PATHS` (dict: `id(self)` → `(label, started)`) and
  `_INFLIGHT_LOCK`, and `_SLOW_HELD` (dict: label → worst-hold-seconds,
  capped at 40 entries) — all mutated inside `_gated`'s try/finally.
- **Inputs/outputs**: `_gated(inner, label)` takes a zero-arg callable and a
  route label; decides whether `label` is heavy/ingest and, if so, blocks
  on that pool with a bounded wait (`HEAVY_WAIT_S`/`INGEST_WAIT_S`); then
  takes `_INFLIGHT` non-blockingly; runs `inner()`; releases both pools in
  reverse order; records timing into `_INFLIGHT_PATHS`/`_SLOW_HELD`. Output
  is either `inner()`'s result or a `_too_busy()` 503.
- **Coupling**: to `Handler._HEAVY_ROUTES`/`_INGEST_ROUTES` (constants
  derived from `HEAVY_ROUTES`/`INGEST_ROUTES` module constants), to
  `Handler._too_busy` (which itself calls `_drain_body`, already in
  `transport.py`), and — critically — to whatever `inner` is, which for
  `do_GET`/`do_POST` is `_do_GET_inner`/`_do_POST_inner`, i.e. all of
  application/domain logic. `_gated` itself has zero domain knowledge; it is
  a pure "run this under these permits, record timing" wrapper.
- **Covered by Stage 0 tests**: only the `[skip]`ped admission/overload case
  — explicitly not reliably triggerable in the existing suite. Rate limiting
  (a related but distinct mechanism, §11.5) is covered.
- **NOT yet characterized**: `_INFLIGHT` exhaustion producing 503 with
  `Retry-After: 1`; `_HEAVY`/`_INGEST` sub-pool wait-then-503 behavior;
  `_INFLIGHT_PATHS`/`_SLOW_HELD` bookkeeping being visible via `/api/health`
  (the doc says these are published there); the exact non-blocking-vs-
  blocking-with-timeout distinction between `_INFLIGHT` (non-blocking) and
  `_HEAVY`/`_INGEST` (bounded wait).
- **On deterministic characterization of MAX_REQUESTS overload without
  flakiness**: exhausting a real semaphore of 200 by firing 200 real
  concurrent slow requests is inherently timing-sensitive and expensive.
  A materially more deterministic alternative that stays black-box: since
  `_INFLIGHT`/`_HEAVY`/`_INGEST` are published as *named, class-level*
  semaphores (the comment at line 636 says as much — "Named, module-level, so
  tools/test_overload.py can size its flood off the real number"), a
  characterization test can reach into the running subprocess only via HTTP,
  so the deterministic option is really: lower the effective caps for a
  dedicated test run via a config/env override (none currently exists) or
  accept that this remains a "best-effort, may be occasionally flaky"
  integration test category — consistent with `tools/test_overload.py`,
  which already exists as a separate, admittedly-heavier tool rather than
  part of the fast contract/behavior suites. Recommendation: leave
  `MAX_REQUESTS` overload as an existing-tool concern (`test_overload.py`)
  rather than duplicating it in `test_hub_behavior.py`, and characterize only
  the *shape* of the 503 (`Retry-After`, `Cache-Control: no-store`, JSON
  error body) by directly calling `_too_busy` in-process against a live
  `Handler` instance if a lower-level harness is introduced in Stage 1B,
  which sidesteps concurrency entirely.
- **Proposed extraction seam**: extract `_gated` and its three semaphores,
  the constants (`MAX_REQUESTS`/`MAX_HEAVY`/`MAX_INGEST`/`HEAVY_ROUTES`/
  `INGEST_ROUTES`/`*_WAIT_S`), `_INFLIGHT_PATHS`/`_SLOW_HELD`/`_INFLIGHT_LOCK`,
  and `_too_busy`, as a single "admission" unit exposing something like
  `admission.run_gated(label, inner)` and `admission.too_busy(handler)`. This
  is a clean seam: `_gated` already has no domain knowledge.
- **Module placement**: separate module (`admission.py`), not
  `transport.py` — same reasoning as the micro-cache: distinct
  process-lifetime state and a distinct concern (concurrency/backpressure
  vs. per-call body/response mechanics).
- **Dependency direction after extraction**: `hub.py` → `admission.py`;
  `admission.py` → `transport.py` only for `_too_busy`'s call into
  `drain_body` (a legitimate, already-established Stage 1A dependency
  direction); no cycle.
- **Defects/races (documented, not fixed)**: `_INFLIGHT_PATHS` is keyed by
  `id(self)`, which is reused once a `Handler` instance is garbage collected
  — with `ThreadingHTTPServer` creating one `Handler` per *connection* (not
  per request) and ids being process-unique only while alive, this is
  almost certainly fine in practice but is an aliasing hazard worth flagging:
  two connections whose `Handler` objects do not overlap in lifetime could
  reuse the same `id()`, which is only benign because the dict entry is
  always popped in the same `finally` that would also end that id's
  validity window. No observed bug, just a fragility worth naming.

### 11.5 Rate limiting (`rate_ok`, `_HITS`, `_HIT_LOCK`, `RATE`, lines
~378-503)

- **Current state/ownership**: `_HITS` (dict: `(path, who_or_ip, bucket)` →
  count), `_HIT_LOCK`, both module-level (not `Handler`-scoped), process-
  lifetime, with an age-based eviction when the table exceeds 20000 entries.
  `RATE` (dict: path → `(limit, window_seconds)`) is a module-level constant.
- **Inputs/outputs**: `rate_ok(path, ip, who="")` → bool; pure function of
  its args plus the shared `_HITS` table; the caller (route-dispatch code
  inside `_do_*_inner`, and `_tile`) decides what to do with a `False`
  (usually `_err(429, ...)`).
- **Coupling**: to nothing but `now()` (from `core`) and the shared dict —
  no `Handler` coupling at all; it is a free function already. Its callers,
  however, are scattered through `_do_GET_inner`/`_do_POST_inner`/`_tile`
  (domain/route-dispatch code, out of scope for Stage 1B/1A) — this is a
  case where the *mechanism* is cleanly separable but its *call sites* are
  not, mirroring `_route_label`'s Stage 1A situation.
- **Covered by Stage 0 tests**: yes — `/api/drive/vote` tripping 429 after
  ~121 requests is characterized (see Stage 0 pass-2 notes).
- **NOT yet characterized**: per-node (`who`) keying distinct from per-IP;
  the 20000-entry eviction; multiple routes' distinct limits beyond
  `/api/drive/vote`; the fact that `client_ip` is always `127.0.0.1` behind
  Caddy (a documented, intentional current-deployment fact, not a defect).
- **Proposed extraction seam**: `rate_ok` and its state are already a clean,
  free-function unit with no `Handler` coupling — nearly a no-op move.
- **Module placement**: separate module (`ratelimit.py`) — not
  `transport.py` (it is policy/accounting state with its own table and
  constants, not per-request body/response mechanics), and not merged into
  `admission.py` either, since rate limiting is per-caller/per-route policy
  while admission is whole-process concurrency/backpressure — different
  axes, worth keeping distinct so neither module absorbs the other's
  concerns.
- **Dependency direction after extraction**: `hub.py` → `ratelimit.py`; zero
  dependencies the other way. `RATE`'s route-specific limits stay as data
  next to the function that reads them.
- **Defects/races**: none newly found; the existing eviction-by-age (never
  `clear()`) is already documented in the file's own comments as a
  deliberate fix for a prior bug, not a remaining defect.

### 11.6 Static-file serving and tile proxying (`_file`, `_tile`, lines
~906-1016, 1002-1016, plus the `/static/`, `/vendor/`, `/snap/` dispatch
inside `_do_GET_inner`)

- **Current state/ownership**: `_file` is stateless (reads a `Path`, guesses
  a content type, optionally rewrites `<script>` tags for nonce injection,
  calls `_send`). `_tile` touches the filesystem (`TILES` cache directory),
  a module-level prune lock (`_tile_prune_lock`)/counter (`_tile_count`), and
  a concurrency-bounding semaphore (`_TILE_FETCH`, at 12), plus a real
  outbound network call to the CartoDB CDN on a cache miss.
- **Inputs/outputs**: `_file(path)` → writes a response from a `Path` already
  resolved and traversal-guarded by its caller (`.name`-flattening happens at
  the call sites in `_do_GET_inner`, not inside `_file` itself — worth noting,
  since the traversal defense is NOT co-located with the function that serves
  the bytes). `_tile(path)` → parses z/x/y from the URL, validates range,
  serves from `TILES/` cache or fetches from upstream, writes to cache,
  triggers `_tile_prune()`.
  the traversal-guard placement (guard at the call site) means `_file` is
  not itself trustworthy in isolation.
- **Covered by Stage 0 tests**: `/static/app.js` 200 + content-type;
  `/static/..%2f..%2fhub.py` and `/static/../hub.py` traversal → 404;
  `/api/tile/{z,x,y}` allow-list rejection paths (bad z/x/non-integer/wrong
  extension) → 404 before any network call.
- **NOT yet characterized**: an actual tile cache *hit* (would require a
  pre-seeded `TILES/` directory in the harness, deterministic and doable);
  an actual tile cache *miss* going to the real network (explicitly avoided
  per Stage 0's "mocked external network dependencies" instruction — not
  characterized and should stay that way without a mock upstream);
  `_tile_prune`'s pruning-at-cap behavior (deterministic in principle: seed
  more than `TILE_CACHE_MAX` files, trigger one write, assert prune to the
  80% low-water mark — expensive at 20000 files but the constant could be
  monkeypatched for a test-only build, which would be a test change, not a
  production one, so it's a legitimate option for Stage 1B's own test
  additions); `_TILE_FETCH`'s 12-concurrent-fetch bound (hard to characterize
  without controlling upstream timing — likely stays uncharacterized;
  document as such rather than forcing a flaky test); `/vendor/images/`'s
  one-level-subdirectory allow-list; `/snap/` serving.
- **Proposed extraction seam**: `_file` can move to `transport.py` (or a
  `static.py`) largely as-is — it is nearly stateless — **provided the
  traversal guard is moved with its call sites, not left behind**; since the
  guard is currently inline in `_do_GET_inner` (route-dispatch, staying in
  `hub.py` until Stage 2), the cleanest Stage 1B seam is: extract `_file`
  alone (safe, self-contained) and leave the `.name`-flattening exactly where
  it is in `_do_GET_inner` for now, documenting that `_file`'s safety is
  contingent on callers pre-sanitizing the path — do not "helpfully" add a
  second guard inside `_file` itself, since that would be a behavior
  change (defense-in-depth is desirable but out of scope; note it as a
  possible future hardening, not a Stage 1B action). `_tile` is far more
  entangled (rate limiting, a semaphore, disk cache, network, pruning) and
  should NOT be extracted as a single unit; if attempted at all in Stage 1B,
  split into: URL validation (pure, easy), cache lookup/write (filesystem,
  moderate), and the upstream fetch + prune trigger (network + the semaphore
  + `_tile_prune`, hardest) — each independently testable, but this entire
  area is large enough that it may be better deferred to its own dedicated
  Stage 1B sub-stage rather than bundled with the smaller mechanisms above.
- **Module placement**: `_file` → `transport.py` is defensible (it is a
  generic "serve this file with the nonce/mimetype dance" helper with no
  cache/CORS/rate-limit entanglement of its own) or a small `static.py` if
  the team prefers keeping `transport.py` limited to request/response
  mechanics rather than "anything file-shaped." Given the stated goal of
  not turning `transport.py` into a second monolith, recommend `static.py`.
  `_tile` and its cache/semaphore/prune state should be their own module
  (`tiles.py`), not merged into `static.py` or `transport.py`.
- **Dependency direction after extraction**: `hub.py` → `static.py` (for
  `_file`) and `hub.py` → `tiles.py` (for `_tile`/`_tile_prune`); `tiles.py`
  → `ratelimit.py` (it calls `rate_ok`) — a new, legitimate one-directional
  edge; neither depends on `hub.py`.
- **Defects/races (documented, not fixed)**: the traversal guard living at
  the call site rather than inside `_file` is a latent footgun for any
  *future* caller of `_file` that forgets to pre-sanitize — not a bug today
  (every current call site already guards), but worth flagging as an
  extraction-time risk: moving `_file` without also moving/duplicating the
  guard, or without a loud comment, would let a future contributor call
  `_file` on an unguarded path and reintroduce traversal.

### 11.7 CORS/CSRF (lines ~866-872, 2452-2483, plus `_sse`'s separate
CORS header at line 4163)

- **Current state/ownership**: no dedicated module; CORS is a single
  conditional line inside `_send` (deny for `/api/review`, `/api/operator`,
  `/api/purge`, `/api/rv` prefixes, allow `*` otherwise) plus a second,
  independent CORS header hardcoded inside `_sse` (which writes directly to
  `wfile`, bypassing `_send` entirely — worth flagging as a second place
  CORS policy is expressed and could drift from the first). CSRF is a
  `Handler._CSRF_SENSITIVE` frozenset checked at the top of
  `_do_POST_inner`, requiring `Content-Type: application/json` on cookie-
  authenticated, state-changing routes.
- **Inputs/outputs**: `self.path` → CORS allow/deny; `p in
  _CSRF_SENSITIVE` + `Content-Type` header → 415 or pass-through.
- **Coupling**: CORS to `self.path` only; CSRF to `self.path` and
  `self.headers`. Neither touches DB/filesystem/network. `_sse`'s CORS
  header is coupled to nothing but being hand-copied from `_send`'s logic.
- **Covered by Stage 0 tests**: CORS `*` presence/absence by path prefix
  (`/api/stats` vs. `/api/review/queue`); CSRF 415 on `text/plain` vs.
  pass-through on `application/json` for `/api/review`.
- **NOT yet characterized**: the full `_CSRF_SENSITIVE` set (only one route
  tested); `_sse`'s independent CORS header (the SSE route itself,
  `/api/live` per the "RETIRED" comment at line 2440-2445, currently answers
  410 — so `_sse` may be dead code reachable only internally; worth
  confirming before Stage 1B touches it, since extracting or duplicating
  policy for genuinely dead code is wasted effort).
- **Proposed extraction seam**: fold the CORS decision into whatever module
  ends up owning `_send`'s header composition (§11.1's `response_policy.py`)
  as a pure `def cors_header(path) -> str | None` function; fold CSRF
  Content-Type checking into a pure `def csrf_check(path, headers) -> bool`
  usable from `_do_POST_inner` without moving `_do_POST_inner` itself (same
  shape as the `rate_ok`/route-label situation: mechanism separable, call
  site stays in `hub.py`). Leave `_sse`'s inline header alone for now (or,
  if it is confirmed dead, flag it for removal as a documented finding
  rather than folding dead code into the new policy module).
- **Module placement**: `response_policy.py` (with `_cache_control` and the
  security-header table) for CORS; CSRF's `csrf_check` can live in the same
  module since both are "is this request's shape acceptable" policy
  functions of comparable size, or in a small `csrf.py` if the team prefers
  one-concern-per-file — either is defensible; recommend co-locating with
  `response_policy.py` to avoid a proliferation of one-function modules.
- **Dependency direction after extraction**: `hub.py` → `response_policy.py`
  only.
- **Defects/races**: `_sse` expressing CORS policy independently of `_send`
  is a duplication risk (not a bug today, since both currently say `"*"`,
  but a future edit to one could silently diverge from the other) —
  documented here as a finding, not fixed.

### 11.8 Summary: proposed Stage 1B implementation sequence

Each stage below is a separate, independently testable/committable step,
run against the same two gates (`tools/test_hub_behavior.py`,
`tools/test_hub_contract.py`), in dependency order (later stages may depend
on earlier ones' modules but not vice versa):

1. **`ratelimit.py`** — move `rate_ok`, `_HITS`, `_HIT_LOCK`, `RATE` verbatim;
   update call sites in `_do_GET_inner`/`_do_POST_inner`/`_tile` to
   `ratelimit.rate_ok(...)`. Lowest risk: already a free function, zero
   `Handler` coupling.
2. **`admission.py`** — move `_gated`, `_too_busy`, the three semaphores and
   their route-set constants, `_INFLIGHT_PATHS`/`_SLOW_HELD`/`_INFLIGHT_LOCK`;
   `Handler` keeps thin wrappers (`_gated`, `_too_busy` delegate in) for the
   same external-tooling-compatibility reason as Stage 1A (`test_overload.py`
   reaches into `hub.Handler._INFLIGHT` directly and must keep working).
3. **`microcache.py`** — move `_MICRO`/`_MICRO_LOCK`/`_MICRO_FLIGHT`/
   `_MICRO_PARAMS`, `_micro_key_for`, `_micro_ttl`, and the single-flight
   choreography out of `do_GET`, exposing a `get_or_build(key, ttl, builder)`
   entry point; `do_GET` shrinks accordingly. Depends on `_cache_control`
   already existing somewhere callable (either still in `hub.py` at this
   point, or already moved in step 4 — sequence 4 before 3 if that ordering
   is preferred to avoid a temporary back-reference).
4. **`response_policy.py`** — move `_cache_control`, `_CACHEABLE_API`, the
   CORS decision, and CSRF's `csrf_check`, plus the security-header table,
   as pure functions; `_send` in `hub.py` calls into this module to get
   decided values, then calls `transport.py` for the wire-write (see §11.1's
   split). This is the highest-risk step since it touches `_send`'s
   internals; do it after the lower-risk state extractions above so the
   test harness and reviewer have already re-validated the mechanism in
   isolation.
5. **`static.py`** — move `_file` (and, if confirmed safe, a documented note
   that the traversal guard remains at the `_do_GET_inner` call sites).
6. **`tiles.py`** (optional/separate sub-stage, larger surface) — move
   `_tile`, `_tile_prune`, `_tile_prune_lock`, `_tile_count`, `_TILE_FETCH`,
   `TILES`/`TILE_UPSTREAM`/`TILE_SUBDOMAINS`/`TILE_MAX_ZOOM`/
   `TILE_CACHE_MAX`; depends on `ratelimit.py` (step 1) for its `rate_ok`
   call. Recommend doing this last and treating it as its own reviewable
   unit given its size and its real-network/filesystem footprint.

Steps 1–2 have no bearing on `_send`/cache/CORS and can be done, tested, and
committed with essentially no risk to observable behavior. Steps 3–4 are
where the cache/CORS/security-header entanglement identified in §11.1 must
actually be resolved, and should be reviewed most carefully. Step 5 is
low-risk given the caveat above. Step 6 is the largest remaining piece of
`hub.py` transport-adjacent code and is deliberately sequenced last.

### 11.9 Stage 1B completion: tile substage (`tiles.py`)

Completed as step 6 above, following pre-extraction characterization
(`tools/test_tiles_unit.py`, 26 deterministic checks against the unmodified
`hub.Handler._tile`, no real network calls — `urllib.request.urlopen` is
monkeypatched at the module level `tiles.serve` calls through). The same
test file was then repointed at the extracted `tiles.serve` and re-run,
proving byte/behavior-identical output.

`tiles.py` moved `TILES`/`TILE_UPSTREAM`/`TILE_SUBDOMAINS`/`TILE_MAX_ZOOM`/
`TILE_CACHE_MAX`/`_tile_count`/`_tile_prune_lock`/`_tile_prune`/`_TILE_FETCH`/
`Handler._tile` (as `tiles.serve`) verbatim. It imports only `ratelimit`
(reusing `rate_ok`/`RATE["/api/tile"]` through the existing interface — no
duplicated hit table) and stdlib; it does not import `hub` and does not
touch `admission.py`, matching the pre-analysis note that tiles are
deliberately ungated and need only their own fetch-concurrency semaphore.
`hub.py` keeps `TILES`/`TILE_UPSTREAM`/`TILE_SUBDOMAINS`/`TILE_MAX_ZOOM`/
`TILE_CACHE_MAX`/`_TILE_FETCH` as aliases to the same objects (not copies)
and `_tile_prune`/`Handler._tile` as one-line delegations, so existing
tooling reaching into `hub.TILES`/`hub._TILE_FETCH` is unaffected.

This closes out the Stage 1B sequence proposed in §11.8 (through the tile
substage). No new findings beyond what was already documented in §9/§11.6;
the `/vendor/images/` 404 quirk and the tile-prune/upstream-fetch incident
history are preserved verbatim in `tiles.py`'s own docstrings rather than
duplicated only in `hub.py`.

### 11.10 Stage 2A: public/static/download/page route adapters (`pages.py`)

Stage 1B (all six steps) is complete and accepted. Stage 2A extracts the
lowest-risk, purely-presentational public GET routes into a new `pages.py`
route-adapter module, per the Stage 2 plan in §8.

Routes moved (all GET, all already covered by `tools/test_hub_contract.py`'s
57-route inventory — no route added, removed, or renamed):
`/`, `/about`, `/transparency`, `/status`, `/checksums`, `/support`,
`/donate`, `/business`, `/ipcamera`, `/IPCamera`, `/relay.py`, `/download`,
`/api/download`, `/hardware`, `/build16`, `/app`, `/node`, `/key`,
`/contribute`, `/signin`, `/login/camera`, `/sw.js`.

`pages.py` also owns the `DOWNLOAD_URL`/`_DL_CACHE`/`_DL_TTL_S`/
`download_url()` cached GitHub-release HEAD-probe shared by `/download` and
`/api/download`. `hub.py` keeps `DOWNLOAD_URL`/`_DL_CACHE`/`_DL_TTL_S` as
aliases to the same objects (not copies) and `_download_url()` as a one-line
delegation, matching the established Stage 1B aliasing pattern.

Each `pages.py` function takes the same duck-typed handler parameter every
other Stage 1B module takes (`handler._file`/`handler._json`/`handler._send`/
`handler._err`/`handler._status`/`handler.send_response`/`handler.send_header`/
`handler.end_headers`); `hub.py`'s `_do_GET_inner` calls straight into
`pages.<fn>(self)` at the exact sequential position each route previously
occupied in the if/elif chain, so first-match-wins ordering across the
hub.py/pages.py boundary is unchanged. The outer `mirror.route_allowed(p)`
gate, and the `do_GET` admission/micro-cache choreography ahead of
`_do_GET_inner`, are untouched.

Deliberately NOT moved in Stage 2A, despite superficially similar shape:
- `/admin/bugs`, `/api/bug/list`, `/api/bug/shot/*` — operator-gated via
  `self._is_local()`, not purely presentational.
- `/help`, `/api/help/*` — `/help` itself is a static shell but sits directly
  adjacent to `/api/help/next`/`/api/help/stats`/`/api/help/img/*`, which
  delegate to `help_api.py`'s real domain logic; left together in `hub.py`
  pending a Stage 2B/3 decision about the help-vote route family as a whole.
- `/planes`, `/api/aircraft`, `/api/geocode`, `/api/scanner`, `/drive`,
  `/api/drive/reports`, `/api/places`, `/api/heat`, `/api/node/me`, `/aim`,
  every `/rv*`/`/api/rv/*` route, `/login`, `/review`, `/api/review/queue`,
  and everything below it — reviewer/operator/authenticated/external-
  integration routes, explicitly out of scope per the Stage 2A instructions.

`tools/test_hub_contract.py` needed no changes: none of its assertions grep
`hub.py`'s source for these specific route strings independent of the
inventory check, and the inventory check already tolerates a route living in
"the handler or a documented grouped/prefix branch" — it was written before
any route left `hub.py`'s literal source. The 57 GET / 36 POST counts are
unchanged.

Files added/modified: `pages.py` (new), `hub.py` (modified — `import pages`
added; `DOWNLOAD_URL`/`_DL_CACHE`/`_DL_TTL_S`/`_download_url()` now alias/
delegate to `pages.py`; the 22 routes above now delegate to `pages.py`
functions in place). `hub.py`: 3404 → 3297 lines (-107).

No new characterization was needed beyond the existing Stage 0 suites: these
routes were already exercised indirectly by `tools/test_hub_behavior.py`
(security headers, cache-control, static-serving checks reuse `/` and
`/static/*`) and by name in `tools/test_hub_contract.py`'s route inventory;
`tools/test_signin_recovery.py` continues to exercise `/signin`'s downstream
flow unchanged (its own pre-existing `UnicodeEncodeError` on an emoji
`print()` under the Windows `cp1252` console codec was reproduced identically
against the pre-Stage-2A code via `git stash` and is unrelated to this
extraction).

No new findings. No behavior changed intentionally; all previously
documented defects/quirks (the `/business`→`/IPCamera` 301 case-sensitivity
note, the `/support`/`/donate` generated-file-may-be-absent 503, the
download-probe 600s cache) are preserved verbatim, including their original
inline comments, now living in `pages.py`.

### 11.11 Stage 2B: public read-only map/data API route adapters (`mapdata.py`)

Stage 2A is complete and accepted. Stage 2B extracts the ordinary, public,
read-only map/data GET routes into a new `mapdata.py` route-adapter module,
per the Stage 2 plan in §8. Unlike Stage 2A (pure presentation, no domain
logic to worry about), this stage's rule is stricter: only HTTP-adapter
responsibility (path/query parsing, invocation of an existing narrow seam,
response mapping) may move — routes with non-trivial inline domain/privacy
logic, or with authentication semantics that do not fit "ordinary public
read", are deliberately left in `hub.py` and reported below rather than
forced into the new module.

**Routes moved** (all GET, all already covered by
`tools/test_hub_contract.py`'s inventory — no route added, removed, or
renamed): `/api/stats`, `/api/policy`, `/api/whoami`, `/api/plate`,
`/api/pending`, `/api/leaderboard`, `/api/heat`, `/api/places`,
`/api/sightings`, `/api/sighting/<id>`, `/api/track/<hash>`.

Each is a thin `mapdata.<fn>(handler, ...)` call taking the same duck-typed
handler parameter as `pages.py`/tiles.py (only `handler._json`/`handler._err`/
`handler._is_local` are used), invoked from `hub.py`'s `_do_GET_inner` at the
exact sequential position each route previously occupied, so first-match-wins
ordering across the hub.py/mapdata.py boundary is unchanged. The outer
`mirror.route_allowed(p)` gate and the `do_GET` admission/micro-cache
choreography (including which paths are in `_CACHEABLE_API`) are untouched —
`mapdata.py` contains no cache-control or admission logic of its own.

**Shared redaction/alias helpers relocated to `privacy.py`.** `/api/plate`,
`/api/sightings`, `/api/sighting/<id>`, and `/api/track/<hash>` all depended
on module-level `_alias_map`/`_resolve_hash`/`_public_rows` helpers that used
to live in `hub.py`, and the still-present (retired-but-present) SSE handler
(`Handler._sse`) also calls `_public_rows`. Since `mapdata.py` must not import
`hub`, and these helpers are privacy/redaction-adjacent rather than transport
plumbing, they were moved to `privacy.py` as `privacy.alias_map()` /
`privacy.resolve_hash()` / `privacy.public_rows()`, with the underlying
`_ALIAS`/`_ALIAS_DAY` dict/list kept as the SAME objects (not copies) —
`hub.py` now aliases `_ALIAS`/`_ALIAS_DAY` to `privacy.ALIAS`/
`privacy.ALIAS_DAY` and keeps `_alias_map()`/`_resolve_hash()`/
`_public_rows()` as one-line delegating wrappers, exactly like the Stage 1B
aliasing pattern, so `Handler._sse` and any other existing caller of
`hub._alias_map`/`hub._resolve_hash`/`hub._public_rows` keeps working
unchanged.

**Deliberately NOT moved in Stage 2B** (left in `hub.py`, reported rather
than forced):

- **`/api/nodes`** — contains genuine domain/privacy logic: the `public_cam`
  true-position exception, the `publish_span` consent gate that decides
  whether `span`/`road_name`/`span_source` are disclosed at all, and viewport
  (`box`) filtering tied to that same consent boundary. This is exactly the
  kind of logic Stage 2B's rules say must not be relocated merely to make a
  route module look cleaner, and there is no existing narrow seam (a `db.py`
  function) that already encapsulates the consent decision — extracting it
  would mean either inventing a new seam (out of scope; that is Stage 3's
  job) or moving the domain logic itself (explicitly forbidden). Left whole
  in `hub.py`, a Stage 3 candidate.
- **`/api/health`** — transport/admission diagnostics (reads
  `Handler._INFLIGHT_LOCK`/`_INFLIGHT_PATHS`/`_SLOW_HELD`, the admission
  semaphores' `._value`, `tiles._TILE_FETCH`, dynamically imports `road.py`,
  and does `/proc/<pid>/fd` introspection on Linux). This is not a map/data
  API in any meaningful sense and is far more coupled to `Handler`/admission
  internals than to `db.py`; left in `hub.py`.
- **`/api/audit`** — see the new finding below; left in `hub.py` unmodified
  because its actual authentication behavior does not match "ordinary public
  read" and needs a human decision, not a Stage 2B extraction, once the gap
  is resolved.

**New finding — `/api/audit` has no authentication check in `hub.py`,
despite being documented as operator-gated.** §2's route table lists
`/api/audit` as `operator-gated`, and `tools/test_hub_behavior.py`/
`test_hub_contract.py` do not exercise it. Reading the actual branch in
`hub.py`'s `_do_GET_inner`, there is no `self._is_local()` (or any other
auth) check on this route at all — it runs `db.connect().execute(...)`
directly and returns the (IP-truncated) audit log to any caller. The only
thing that currently hides it is `mirror.route_allowed()`'s exclusion list
(`/api/audit` is one of the mirror-excluded prefixes alongside `/review`,
`/api/review`, `/api/operator`, `/api/purge`), which only matters when
`public_mirror=true`; on an ordinary (non-mirror) home deployment this route
currently appears reachable by anyone who asks. This is recorded as a
finding, per the Stage 0/1B/2A precedent of documenting rather than silently
fixing defects discovered during characterization; the route is left
unmodified in `hub.py` and NOT extracted this stage.

**New behavioral/privacy characterization added**
(`tools/test_mapdata_characterization.py`, 46 checks, reusing the isolated
subprocess harness from `tools/test_hub_behavior.py`): schema-stability
checks (major JSON keys/types) for `/api/stats`, `/api/policy`,
`/api/whoami`, `/api/plate`, `/api/pending`, `/api/leaderboard`, `/api/heat`,
`/api/places`, `/api/sightings`; a privacy check that `/api/nodes` rows never
carry `pub_lat`/`pub_lon`/`heading`/`fov`/`reach` and that non-`public_cam`
rows have `lat`/`lon` both `None`; an internal-field redaction check on
`/api/sightings` (`plate_conf`/`confirmed_by` absent from anon output); the
`/api/sighting/<missing id>` 404 path and `/api/track/<unknown hash>` empty
list path; and a `public_mirror=true` reachability check across all the
routes moved this stage. Deliberately not over-fit to volatile values
(timestamps, row counts) — only key presence/type and the redaction/coord
invariants are asserted.

`tools/test_hub_contract.py` needed no changes beyond what Stage 2A already
established: it does not grep for these route strings independent of its
existing inventory tolerance for "the handler or a documented grouped/prefix
branch," and every one of these routes still literally occurs in `hub.py`
(as the delegating `return mapdata.<fn>(self, ...)` line). The 57 GET / 36
POST counts are unchanged.

**Files added/modified:** `mapdata.py` (new — 11 route-adapter functions);
`privacy.py` (modified — added `ALIAS`/`ALIAS_DAY`/`alias_map()`/
`resolve_hash()`/`public_rows()`); `hub.py` (modified — `import mapdata`
added; `_ALIAS`/`_ALIAS_DAY`/`_alias_map()`/`_resolve_hash()`/`_public_rows()`
now alias/delegate to `privacy.py`; the 11 routes above now delegate to
`mapdata.py` functions in place; `/api/nodes`, `/api/health`, `/api/audit`
unchanged). `tools/test_mapdata_characterization.py` (new). `hub.py`: 3297 →
3139 lines (-158).

**Test results:** `tools/test_hub_behavior.py` — 48/48 passed.
`tools/test_hub_contract.py` — 57 GET / 36 POST, passed.
`tools/test_mapdata_characterization.py` — 46/46 passed.
`tools/test_microcache_unit.py` and `tools/test_cache_control_characterization.py`
(existing, relevant to the `_CACHEABLE_API`/`no-store` routes touched this
stage) — both passed unchanged.

**Dependency direction:** `hub.py` → `mapdata.py` → {`db`, `classify`,
`privacy`, `operator_auth`, `core`}. `mapdata.py` does not import `hub`.
`privacy.py`'s new alias/redaction helpers depend only on `core`, matching
its existing dependency shape.

No behavior changed intentionally, including the documented latent O(n²)
`classify.patrol_score` recomputation in `/api/track/<hash>` and the
unaudited-by-design read paths, both preserved verbatim in `mapdata.py`.

### 11.12 Characterization-only hardening pass on privacy.py's Stage-2B helpers

Before Stage 2C, `ALIAS`/`ALIAS_DAY`/`alias_map()`/`resolve_hash()`/
`public_rows()` (moved into `privacy.py` during Stage 2B, see §11.11) received
a deterministic, module-level test pass:
`tools/test_privacy_alias_unit.py` (18 checks). No production behavior was
changed. Characterized, with `privacy.now` monkeypatched to fixed values
(no real sleeps, no timing assertions):

- `public_rows()` redacts and returns new dict objects without mutating the
  input rows.
- `alias_map()`/`resolve_hash()` round-trip: a real plate hash gets a stable
  `a:`-prefixed alias within a day, and that alias resolves back to the real
  hash; an unrecognized token passes through `resolve_hash()` unchanged.
- The alias populated by `public_rows()` as a side effect is the same one
  `resolve_hash()` (used by `/api/track/<hash>`) can reverse.
- Public-tier rows are never aliased (their real `plate_hash` is retained).
- A row with a `NULL` or empty-string `plate_hash` is never aliased.
- **Day-boundary rotation**: advancing `privacy.now()` by exactly one day and
  calling `alias_map()` again clears `ALIAS` and advances `ALIAS_DAY`; a
  token minted on the previous day no longer resolves to anything and is
  returned unchanged (this is existing, intentional behavior — the whole
  point of the per-day alias is that yesterday's token cannot be replayed to
  re-identify a vehicle today).
- The same underlying hash, aliased twice within one day (two rows for the
  same vehicle), yields exactly one shared alias token, not two — this is
  what lets the map group a vehicle's sightings on screen within a day.

**🚨 Architectural note for Stage 2C and beyond:** `ALIAS`/`ALIAS_DAY`/
`alias_map()`/`resolve_hash()`/`public_rows()` are, in substance, Stage-3-style
application/domain-service extraction (privacy/redaction policy, not HTTP
routing) that was performed EARLY, during Stage 2B, because the alternative —
leaving them behind a temporary `hub.*` compatibility seam — would have meant
either keeping `mapdata.py` unable to serve `/api/plate`/`/api/sightings`/
`/api/sighting/<id>`/`/api/track/<hash>` at all (since it must not import
`hub`), or duplicating the alias state in two places. Moving them to
`privacy.py` was judged the lesser violation because they already lived next
to `privacy.redact()`, which they only ever wrap, and moving them created no
new seam design — it reused an existing module's existing role.

This is called out explicitly so future Stage 2 substages (2C and later) do
NOT treat this as precedent for moving substantial application/domain logic
out of `hub.py` merely to avoid a temporary compatibility seam. The default
for Stage 2 remains: HTTP-adapter-only extraction, with a temporary
`hub.*`-owned seam (or leaving the route in `hub.py` entirely, as `/api/nodes`
was this stage) when a route's domain logic has no existing narrow seam to
call through. `privacy.py`'s alias helpers were a narrow, pre-existing-module
exception, not a new general license.

### 11.13 Security findings / deployment blockers

The following is promoted from a general "preserved finding" (§9.2) to an
explicit pre-deployment blocker list, because it represents a live
authorization gap rather than a cosmetic or performance quirk:

- **`/api/audit` has no authentication check in `hub.py`**, despite being
  documented (§2) as `operator-gated`. Any caller — including an
  unauthenticated one, on a non-mirror deployment — can currently read the
  full operator decision audit log (`SELECT ts, action, target, ip FROM
  audit ORDER BY ts DESC LIMIT 200`, IP-truncated) simply by requesting
  `/api/audit`. The only thing that currently hides this route at all is
  `mirror.route_allowed()`'s mirror-exclusion list, which is a no-op unless
  `public_mirror=true`. **This must be resolved (either by adding an
  explicit `self._is_local()`/operator-auth check, or by confirming and
  documenting that open read access is actually intended) before any
  deployment that is reachable by untrusted callers.** Deliberately NOT
  fixed as part of this characterization pass, per instructions; recorded
  here so it cannot be missed during any future extraction or deployment
  review.

Dependency direction: `pages.py` imports `core` only (`CONFIG`, `PUBLIC`,
`now`) and stdlib `urllib.request` (deferred, inside `download_url()`, exactly
as it was in `hub.py`). It does not import `hub`, `admission`, `ratelimit`,
`static`, `tiles`, or any application/domain module. `hub.py` imports `pages`
and calls into it; the dependency arrow points the same direction as every
other Stage 1B extraction (`hub.py` → leaf module, never the reverse).

### 11.14 Stage 2C1: help/community/drive route adapters (`community.py`)

Extracted the entire help-labelling and drive-radar route family — the
Stage 2A deferral noted at what was then lines 928-933 (`/help`/`/api/help/*`
and `/drive`/`/api/drive/reports`) is now resolved.

**Routes moved** (verbatim, no behavior change):

- GET `/help` — static page shell (`PUBLIC / "help.html"`).
- GET `/api/help/next` — thin wrapper around `help_api.next_for(voter)`.
- GET `/api/help/stats` — thin wrapper around `help_api.stats()`.
- GET `/api/help/img/<id>` (prefix match) — thin wrapper around
  `help_api.image(id)`, 404 if `None`.
- POST `/api/help/vote` — thin wrapper around
  `help_api.record(item, label, voter)`. Preserved quirk: `record()` reports
  validation failures via an in-body `{"error": ...}` value with HTTP 200,
  not a 4xx status; the new wrapper does not add a status check that was
  not there before.
- GET `/drive` — static page shell (`PUBLIC / "drive.html"`).
- GET `/api/drive/reports` — thin wrapper around `db.active_driver_reports()`.
- POST `/api/drive/report` — unconditional `410` (closed 2026-08-15); the
  full inline security rationale comment was preserved as the function's
  docstring rather than dropped.
- POST `/api/drive/vote` — rate-limited (`ratelimit.rate_ok`, reused not
  duplicated) wrapper around `db.vote_driver_report(rid, still_there)`.

**Routes explicitly left in `hub.py`** (out of Stage 2C1 scope per
instructions, unchanged): `/aim` (page shell — deferred as a node/camera
capability page, not help/community/drive), `/admin/bugs`, `/api/bug/*`,
reviewer/operator routes, node routes, ingest, SSE, external integrations,
`/api/audit`.

**Why no Handler compatibility seam was needed:** unlike Stage 2B's
`/api/nodes` (which had genuine consent-gating domain logic inline in
`hub.py`), every route in this family already delegated to an existing
clean application-service seam (`help_api.py`, which is a small,
well-factored, already-separate module) or to a `db.py` function, or was a
pure static shell / fixed 410 response. There was no substantial
domain/database logic sitting directly in `hub.py`'s dispatch body for this
family, so this stage did not need to invent a temporary callback per the
"leave it behind a narrow Handler seam for Stage 3" allowance — that
allowance simply did not apply here.

**Behavioral characterization added:** new
`tools/test_community_characterization.py` (29 checks): `/help`/`/drive`
page-shell status/content-type; `/api/help/next` and `/api/help/stats`
schema (dict, valid JSON) with and without a `voter` query param;
`/api/help/img/<unknown>` → 404; `/api/help/vote` malformed/unknown-item
input → preserved 200-with-in-body-error quirk (not a new 400); `/drive`
page shell; `/api/drive/reports` schema (`{"reports": [...]}`);
`/api/drive/report` → 410 with the withdrawal message preserved verbatim;
`/api/drive/vote` malformed id → 400, unknown id → `{"ok": false}`, and
flooding past the existing 120/hour cap → 429 (reusing the same
`ratelimit.rate_ok` state as before, run in its own isolated hub instance
to avoid interference with the schema checks); mirror availability
(`public_mirror=true`) for every GET in the family plus the fixed 410.

**Files added/modified:**
- Added `community.py` (new route-adapter module).
- Added `tools/test_community_characterization.py` (new, 29 checks).
- Modified `hub.py`: added `import community`; replaced each of the 9
  route bodies above with a one-line delegating call at its exact original
  position in the ordered dispatch chain.
- Modified `docs/HUB_ARCHITECTURE.md` (this section).

**Ordered dispatch:** every route above kept its exact original position in
`_do_GET_inner`/`_do_POST_inner`'s if/elif chain; only the route body
changed (inline code → `return community.<fn>(self, ...)`), so first-match
ordering across `hub.py` and `community.py` is unaffected.

**Dependency direction:** `community.py` imports `db`, `help_api`, `core`
(`PUBLIC`), and `ratelimit` (`rate_ok`) — reusing existing seams, not
duplicating their state. It does not import `hub` or any other route
module. `hub.py` imports `community` and calls into it; the arrow points
the same direction as every prior Stage 1B/2A/2B extraction.

**hub.py line count:** 3139 → 3099 lines (40 lines removed).

**Test results:** `tools/test_hub_contract.py` — 57 GET / 36 POST, all
passed (no contract-file changes were needed, same as Stage 2A/2B).
`tools/test_hub_behavior.py` — 48/48 passed, including the existing
`/api/drive/vote` flood-to-429 check, unaffected by the extraction.
`tools/test_community_characterization.py` — 29/29 passed.

**New findings:** none. No new defects were discovered in this route
family; the `/api/drive/report` 410 rationale, the `help_api.record()`
200-with-error-body quirk, and the separate `label_votes.db`
privacy boundary were all confirmed unchanged and preserved exactly.

### 11.15 Stage 2C2: bug-report/operator-management route adapters (`operator_bugs.py`)

**Routes moved** (verbatim, no behavior change): GET `/admin/bugs`,
GET `/api/bug/list`, GET `/api/bug/shot/<id>`, POST `/api/bug`,
POST `/api/bug/close`, POST `/api/bug/delete`.

**Actual authorization behavior, characterized from executable code and
new tests (not inferred from naming/docs):**

- `/admin/bugs`, `/api/bug/list`, `/api/bug/shot/<id>`, `/api/bug/close`,
  `/api/bug/delete` all gate on `Handler._is_local()` — the exact same
  `operator_auth.check(headers, socket address)` mechanism every other
  operator route in `hub.py` already uses (e.g. `/api/review/queue`,
  `/api/rv/tokens`). Confirmed via new tests: from loopback with
  `operator_requires_auth` at its default (`False`), all five succeed with
  **no credential at all** (fail-open-by-design, matching §"operator route
  without/with auth" in the Stage 0 suite). With
  `operator_requires_auth=true`, all five correctly return `403` with no
  token, and `200` with a correct bearer token obtained the same way the
  existing `/api/review/queue` test obtains one (via `/login`, which
  lazily creates `data/operator.token`).
- `/api/bug` (report submission) is **deliberately unauthenticated** —
  confirmed to remain reachable with `200` and no credential even when
  `operator_requires_auth=true`, because operator-token gating only
  applies to the operator-facing routes, not this intentionally-open
  intake route. This matches the extensive inline rationale already
  present at this route's original site in `hub.py` (preserved as this
  function's docstring in `operator_bugs.py`).
- Implementation and documentation were found to be **consistent** for
  this family — no mismatch to record (contrast with the `/api/audit`
  finding from Stage 2B, which remains open and untouched).

**Files added/modified:**
- Added `operator_bugs.py` (new route-adapter module).
- Added `tools/test_bugs_characterization.py` (new, 32 checks).
- Modified `hub.py`: removed the now-unused `import bugs`, added
  `import operator_bugs`, replaced each of the 6 route bodies above with a
  one-line delegating call at its exact original position in the ordered
  dispatch chain.
- Modified `docs/HUB_ARCHITECTURE.md` (this section).

**Behavioral characterization added:** new
`tools/test_bugs_characterization.py` (32 checks): `/admin/bugs` and
`/api/bug/list` schema/status from loopback; `/api/bug/shot/<unknown>` →
404; `/api/bug` success (200, `{"ok": true, "id": ...}`) and its side
effect (the new report appears in `/api/bug/list`); `/api/bug` malformed
input (blank desc+shot → 400, matching `bugs.save()`'s existing
validation); `/api/bug/close`/`/api/bug/delete` for both a known id
(ok:true, and the id disappears from the listing) and an unknown id
(ok:false); rate-limiting (flooding `/api/bug` — see finding below);
`operator_requires_auth=true` behavior for all six routes (no
token/bad-implied/correct-token cases); and mirror availability
(`public_mirror=true`) for the GET routes plus the POST intake route,
confirmed reachable because none of `/admin/bugs`/`/api/bug/*` appear in
`mirror.route_allowed()`'s exclusion list (`/review`, `/api/review`,
`/api/operator`, `/api/purge`, `/api/audit`) — so on a mirror deployment
they stay reachable and still trust the loopback socket exactly as before.

**Temporary Handler callbacks:** none. Every route in this family already
delegated to an existing clean seam (`bugs.py`, which is a small,
self-contained, already-separate behavioral-authority module for
report storage/redaction/TTL) or to `handler._is_local()`, so — as with
Stage 2C1 — there was no substantial domain/persistence logic sitting
directly in `hub.py`'s dispatch body for this family, and the "leave it
behind a narrow Handler callback for Stage 3" allowance did not need to
be invoked.

**Domain/persistence logic left for Stage 3:** none.

**Dependency direction:** `operator_bugs.py` imports `bugs`, `core`
(`PUBLIC`, `DATA`), and `ratelimit` (`rate_ok`) — reusing existing seams,
not duplicating their state or `bugs.py`'s persistence/redaction logic. It
does not import `hub` or any other route module. `hub.py` imports
`operator_bugs` and calls into it; the arrow points the same direction as
every prior extraction.

**hub.py line count:** 3099 → 3048 lines (51 lines removed).

**Test results:** `tools/test_hub_contract.py` — 57 GET / 36 POST, all
passed (no contract-file changes were needed). `tools/test_hub_behavior.py`
— 48/48 passed (one run required a retry after a large, pre-existing,
environment-wide TIME_WAIT backlog on this shared Windows host briefly
exhausted the ephemeral port range — unrelated to this extraction; see
finding below). `tools/test_bugs_characterization.py` — 32/32 passed.

**New findings:**
1. **Rate-limit layering** (characterized, not fixed): `bugs.py`'s own
   per-hour ceiling (`bugs.MAX_PER_HOUR = 60`, enforced inside
   `bugs.save()`) is *stricter than, and fires before,* `ratelimit.py`'s
   per-IP `/api/bug` bucket (120/hour, `RATE["/api/bug"]`). Flooding
   `/api/bug` from a single IP within an hour is observed to return HTTP
   `400` with an in-body `"too many reports"` error well before any HTTP
   `429` from `rate_ok()` is reached. Both caps exist and both remain
   intact; only the tighter one is externally reachable in practice from
   one caller. This is pre-existing behavior, unchanged by this
   extraction — documented here because the new focused test needed to
   characterize the actually-observed status code rather than assume 429.
2. **Environment note, not a code defect:** this shared development host
   accumulates a very large number of TCP `TIME_WAIT` connections to
   unrelated external hosts (observed >15,000, most of the 16384-port
   Windows ephemeral range) unrelated to this test suite's own traffic.
   This occasionally causes a transient `WinError 10048` on the *next*
   `urlopen()` call regardless of which test file is running; a retry (or
   a longer wait) reliably succeeds. This is the same class of flakiness
   already documented for `tools/test_hub_behavior.py` in earlier stages,
   observed here at a larger scale — recorded so future stages are not
   surprised by it, not attributed to any change made in this stage.

### Stage 2D pre-analysis corrections and additions (analysis-only)

This section corrects the earlier node-auth analysis and records the routing
and security findings that should govern Stage 2D without changing production
behavior.

#### 1. `_token_ok` contradiction resolved

The apparent contradiction is not a real contradiction in implementation.
`Handler._token_ok()` is intentionally permissive when the stored node record
has no token:

```
    def _token_ok(self, nd: dict) -> bool:
        """Constant-time bearer check. A node with no token accepts anyone."""
        if not nd.get("token"):
            return True
        ... compare Authorization header to nd["token"] ...
```

This means `_token_ok()` is only a connector for routes that have already
checked token presence or otherwise decided they are willing to accept a
tokenless node. It is not itself a fail-closed auth gate.

What the implementation really does is:

| Route / caller | explicit token-present guard | lifecycle/status guard | order | token is NULL/missing | wrong token | correct token |
|---|---|---|---|---|---|---|
| `GET /api/node/me` | yes: `if not nd.get("token") or not self._token_ok(nd):` | none | token guard before any node data is returned | `401` | `401` | `200` JSON with true lat/lon |
| `POST /api/node/whoami` | yes: `if not nd.get("token"):` then `_token_ok` | none | token check before response | `401` | `401` | `200` |
| `POST /api/node/parked` | yes: `if not nd.get("token"):` then `_token_ok` | none | token check before data | `401` | `401` | `200` |
| `POST /api/node/key` | yes: `if not nd.get("token"):` then `_token_ok` | none | token check before key write | `401` | `401` | `200` |
| `POST /api/node/span` | yes: `if not nd.get("token"):` then `_token_ok` | none | token check before write | `401` | `401` | `200` |
| `POST /api/node/label` | yes: `if not nd.get("token"):` then `_token_ok` | yes: `if nd["status"] != "active": 403` | status before token | `403` if paused; `401` if missing token on active node | `401` | `200` |
| `POST /api/node/confirm` | yes: `if not nd.get("token"):` then `_token_ok` | yes: `if nd["status"] != "active": 403` | status before token | `403` if paused; `401` if missing token on active node | `401` | `200` |
| `POST /api/sightings` | no | yes: `if nd["status"] != "active": 403` | status before token | if status is `active` and token is missing, `_token_ok()` returns `True` and request is accepted; otherwise `403` for non-active | `401` | `200` |
| `POST /api/node/progress` | no | none | direct `_token_ok()` | if token missing, `True` and request proceeds | `401` | `200` |
| `POST /api/heartbeat` | no | yes: `if nd["status"] != "active": ...` after token | token check before status | if token missing, `_token_ok()` returns `True`; route still returns `{"posting": False,...}` for non-active nodes | `401` | `200`, either posting false or true |
| `POST /api/sighting/fullres` | no | none | direct `_token_ok()` | if token missing, `True` and route continues | `401` | `200`/`400`/`404` depending on ownership and evidence |
| `POST /api/rv/my-token` | no | none | direct `_token_ok()` | if token missing, `True` and route may mint/return a reviewer token | `401` | `200` |

The corrected finding is therefore:

- GET `/api/node/me` is not a vulnerable tokenless route; it fails closed by
a direct token-present check before calling `_token_ok()`.
- The security concern is in routes that call `_token_ok()` without a prior
  `nd.get("token")` check, especially when the node is still `active`.
- This is not a route-by-route auth redesign suggestion; it is a preserved
  implementation fact to be characterized and left unmodified during Stage 2.

#### 2. GET `/api/nodes` analysis

`GET /api/nodes` is a public, read-only public-map route and should be treated
as a route-adapter extraction candidate, not an application-service extraction.

- Auth: unauthenticated; no bearer or local-only gate at transport level.
- Mirror availability: reachable on a public mirror because it is not excluded
  by `mirror.route_allowed()`.
- `public_cam` exception: `public_cams=1` is the default and the route filters
  only the `kind == "public_cam"` subset. Volunteer nodes are not filtered by
  viewport in the same way; all volunteer-node span metadata is still present
  if the route is requested.
- Public / jittered coordinates: the route intentionally does not publish a
  jittered point for volunteer nodes. It omits `pub_lat`/`pub_lon` entirely,
  and the comment in `hub.py` explicitly records that the map previously
  showed a weak, jittered point that was later removed because it still
  narrowed the road span too much. For `public_cam` nodes, exact coordinates are
  published (`lat`/`lon` are returned as-is); for volunteer nodes they are not.
- Publish-span consent: a volunteer node only exposes `span`, `road_name`, and
  `span_source` when `publish_span` is true. `publish_span` is the consent gate
  for road/span publication. Without consent, `span` and road metadata are `None`.
- Road/span exposure: the route exposes road-span information as a consented
  public projection, not as exact household coordinates. This is a privacy
  boundary that persists and should be preserved.
- Persistence dependencies: reads the node table via `db.nodes()`, then calls
  `node_mod.span_of(n)` and the node record itself; no writes occur in this
  route.
- Inline domain/projection logic: the route does the public projection itself
  in `hub.py`: it trims, filters, selects which geometry to publish, and gates
  volunteer nodes on `publish_span`. This is still route-adapter concern
  because it is HTTP/public-shape projection logic, but it is not a pure
  “delegate to a helper and forget it” route. It is a safe Stage-2 extraction
  only if the caller remains compatible and the projected JSON shape is preserved.
- Stage-2 vs Stage-3 seam: appropriate for Stage 2 route-adapter extraction as
  a thin route adapter with a compatibility callback or direct call to
  `node_mod.span_of()` and `db.nodes()`, but not a domain-service extraction.
  Substantial application logic remains in the `hub.py` projection itself and
  should not be moved out as part of the route adapter unless the existing seam
  is already clean.

#### 3. `/aim` analysis and extraction placement

`GET /aim` is a public page shell, not a node-auth route.

- Route: `GET /aim`
- Auth: none.
- Behavior: serves `PUBLIC / "aim.html"`; the page itself reads client-side
  state (`fragment` or `localStorage`) and shows nothing without it.
- Mirror availability: allowed by mirror policy because it is not in the
  restricted set.
- Extraction placement: it belongs in the public/static page family alongside
  `/app`, `/node`, `/key`, `/signin`, `/contribute`, etc. It should be
  extracted with public presentation adapters in Stage 2A / low-risk public
  page routes, not with the node-auth family in Stage 2D.

#### 4. Authentication unification should not be done in Stage 2

The implementation is intentionally not uniform across the node family.
These are preserved behavioral differences, not a defect to be cleaned up as a
precondition for extraction.

- `_token_ok()` is a shared helper, but multiple endpoints intentionally
  check or omit the token-present guard depending on their semantics.
- `/api/heartbeat` intentionally does token first and then status; it may
  return `{"posting": False}` for a non-active node without counting it as an
  auth failure.
- `/api/node/confirm` and `/api/node/label` intentionally inspect status before
  token and therefore may disclose lifecycle status while still refusing the
  caller.
- `/api/heartbeat/bulk` intentionally compares body tokens per entry and is a
  separate, distinct credential flow from `Authorization`-header `_token_ok()`.
- `/api/key/qr` and `/api/key/rotate` intentionally use body token
  validation and different HTTP status semantics.

Any auth unification is a later intentional behavior/security change and must
be handled as a behavioral policy change, not as part of Stage 2 route motion.

#### 5. Tokenless vs revoked must remain separate in Stage 2 characterization

The status state and the credential presence are separate dimensions in the
current implementation and may vary independently.

- Tokenless does not necessarily mean `status == "revoked"`.
- A node may be `active`, `paused`, or `revoked` and still have a token or no
  token depending on how the system was set up or how the operational tooling
  mutated it.
- Some routes check token presence explicitly; others call `_token_ok()`
  directly and therefore accept a tokenless node if the route is otherwise
  allowed through.
- The route family should be characterized by `status` and `token_present` as
  independent dimensions, not collapsed into one “unauthenticated = revoked”
  model.

#### 6. Security findings (do not fix in Stage 2)

- SEC-01 — `/api/audit` auth mismatch.
  - The route is documented and historically described as operator-gated, but
    the current route inventory and implementation history show a mismatch: the
    route is treated as a high-sensitivity operational API in some docs and a
    public map-data query in others.
  - This remains a recorded security finding and is not fixed during Stage 2.

- SEC-02 — permissive `_token_ok()` behavior.
  - Exact affected routes where the implementation calls `_token_ok()` without a
    preceding explicit token check:
    - `POST /api/sightings` (for active nodes with no token)
    - `POST /api/node/progress`
    - `POST /api/heartbeat`
    - `POST /api/sighting/fullres`
    - `POST /api/rv/my-token`
  - This is the actual issue to preserve and track during Stage 2 route
    movement; it is not a reason to collapse the family into one auth model.

- SEC-03 — status-before-auth lifecycle disclosure.
  - `POST /api/node/confirm` and `POST /api/node/label` check `nd["status"]`
    before token validity; they can therefore return `403` with a lifecycle
    message (`"node is paused"`) before discovering the caller lacks a valid
    token.
  - This leaks lifecycle state to unauthenticated callers and masks the
    underlying auth failure in a way the rest of the family does not.

#### 7. Corrected Stage 2D extraction sequence

The extraction sequence should be:

- 2D1 — node self-service adapters
  - `GET /api/node/me`
  - `POST /api/node/whoami`
  - `POST /api/node/parked`
  - `POST /api/node/key`
  - `POST /api/node/span`
  - `POST /api/node/progress` (only after preserving current tokenless behavior)
  - `POST /api/rv/my-token` (only after preserving current tokenless behavior)
  - `GET /aim` remains in the public page route family, not in 2D1
  - Keep `/api/signals` out of the node-auth family because it uses the
    separate signal-token trust model

- 2D2 — lifecycle / heartbeat adapters
  - `POST /api/heartbeat`
  - `POST /api/heartbeat/bulk`
  - `GET /api/nodes` (public/read-only map projection) if extracted as a
    route adapter with the same projection semantics

- 2D3 — key / credential / full-resolution evidence adapters
  - `POST /api/key/qr`
  - `POST /api/key/rotate`
  - `POST /api/sighting/fullres`
  - route-level auth and body-token semantics retained exactly as implemented

- 2D4 — enrollment / confirm / label deferred to Stage 3
  - `POST /api/enroll`
  - `POST /api/node/confirm`
  - `POST /api/node/label`
  - `POST /api/sightings` remains outside the node-auth adapter family and is
    not included in Stage 2D because it is ingest-coupled and application-
    service heavy.

Sequence note: the route family is not a single uniform auth boundary. The
correct sequence is based on route semantics and existing seams, not a blanket
“move all node routes together” rule.

#### 8. Architectural note for future Stage 2 route extraction

Stage 2 route extraction should continue to preserve existing behavior and not
“clean up” the auth inconsistencies. The earlier suggestion to unify node auth
mechanisms before route movement is intentionally rejected for this phase.

The correct Stage 2 rule is:

- characterize the actual behavior before movement;
- preserve it while extracting HTTP route adapters;
- leave any intentional behavior change (auth unification, fail-closed token
  checks, status-before-auth reorder, or explicit revocation handling) for a
  later, deliberate security change.

This document intentionally records the current behavior without fixing it.

