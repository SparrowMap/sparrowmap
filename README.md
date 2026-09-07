# RavenMap

**A privacy-first, community-owned situational-awareness and public-accountability network.**

> “O’er Mithgarth Hugin and Munin both
> Each day set forth to fly.”
>
> — *Grímnismál*, stanza 20

**RavenMap** takes its name from **Huginn** and **Muninn**, the ravens of Óðinn: *Thought* and *Memory*. In *Grímnismál*, they range across Midgard gathering knowledge and return with what they have learned.

That is a useful model for what this project is intended to do: gather observations from many places, make sense of them, preserve what matters, and return useful knowledge to the community that produced it.

But observation without restraint becomes surveillance.

RavenMap is therefore designed around a second principle: **collect only what is justified, expose only what should be public, and make those limits enforceable in code rather than dependent on promises.**

The *Hávamál* repeatedly counsels the traveler to watch, listen, learn, and act with judgment. It also places lasting weight on deeds and reputation and counsels against remaining silent in the presence of wrongdoing. RavenMap borrows those themes deliberately: **observe carefully, remember responsibly, and make public power accountable without turning private lives into public records.**

---

## What RavenMap is

RavenMap is a fork and architectural successor to **SparrowMap**.

The inherited system began as a community-operated licence-plate camera network: volunteers place cameras overlooking public roads, recognition occurs locally, and observations are sent to a shared map rather than raw video.

RavenMap retains that capability but is being developed into a broader platform for **distributed, privacy-conscious observation and situational awareness**.

A RavenMap deployment may eventually combine:

* privately operated edge cameras;
* OpenIPC, RTSP, browser, and other camera sources;
* public traffic and transportation cameras;
* public government and geographic data sources;
* manually submitted observations;
* local CPU/GPU image processing;
* trusted distributed processing workers;
* geographic and temporal correlation;
* public-accountability information;
* operator-only investigative or review workflows;
* regional RavenMap instances operated by different communities.

The network should not require every sensor, processor, database, or user to belong to one central organization.

**Regional ownership and distributed operation are features, not deployment inconveniences.**

---

## What RavenMap is not

RavenMap is not intended to become:

* a warehouse of everyone's raw video;
* a searchable history of private citizens' movements;
* a centralized commercial surveillance service;
* an indiscriminate licence-plate database;
* a system in which every volunteer worker automatically receives sensitive imagery;
* a black box whose public claims cannot be independently examined;
* a platform where adding more data is automatically considered an improvement.

The ability to collect information is not, by itself, justification to retain or publish it.

---

# The design decision everything else follows from

A distributed observation network can provide legitimate public value while also creating an extraordinary surveillance capability.

If every readable plate, photograph, location, and timestamp is placed into a permanent searchable database, anyone with access can reconstruct another person's life.

That is unacceptable.

RavenMap therefore inherits SparrowMap's fundamental distinction between **public-accountability information** and **private-person information**, and the rearchitecture treats that distinction as a system boundary rather than merely a UI feature.

The currently inherited vehicle policy is:

|                  | Public-accountability tier                                           | Private tier                                                                |
| ---------------- | -------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Primary subjects | Government/public vehicles meeting publication criteria              | Everyone else                                                               |
| Plate text       | May be retained when publication requirements are satisfied          | **Never intentionally persisted as readable text**                          |
| Snapshot         | Plate may remain legible when justified as part of the public record | Plate information is destroyed/redacted before retained publication imagery |
| Retention        | Determined by public-record/accountability policy                    | Short, bounded retention                                                    |
| Search by plate  | Permitted for qualifying public records                              | **No public lookup path**                                                   |
| Location         | May support public accountability                                    | Privacy protections and bounded correlation apply                           |

The exact policy must remain inspectable and configurable where appropriate, but **a deployment must not silently weaken the private tier merely because doing so is operationally convenient.**

---

# Privacy is architecture, not documentation

RavenMap's privacy properties must continue to survive implementation changes.

Several inherited safeguards are therefore treated as architectural invariants.

## 1. Public classification is conservative

A vehicle does not become public merely because OCR thinks it recognizes a government plate.

Publication requires sufficiently strong classification confidence and corroborating evidence.

A single OCR error must not be capable of turning an ordinary person's observation into a public government-vehicle record.

---

## 2. Private plate information is destroyed in retained imagery

Removing plate text from a database is insufficient if the JPEG still contains a readable plate.

For private-tier observations, identifying plate pixels must be irreversibly obscured before imagery intended for retention or publication is stored.

This is a **data-minimization boundary**, not a cosmetic blur applied by the web interface.

---

## 3. Retained images should contain only what is needed

Vehicle observations should normally use tight crops rather than full camera frames.

The pedestrian on the sidewalk, a house number, someone standing in a yard, or an unrelated passing vehicle generally has no reason to become part of an observation about a particular vehicle.

Raw frames should therefore have a substantially higher privacy classification than sanitized observation media.

---

## 4. Private identifiers have bounded correlation

Private vehicles may require temporary pseudonymous correlation so that repeated observations can be recognized without retaining readable plate numbers.

Those identifiers must be keyed rather than trivially reversible hashes, and their keys must rotate so correlation has a defined time boundary.

The objective is not merely to prevent the public from building permanent private trails.

It is to prevent **RavenMap itself** from casually acquiring that capability.

---

## 5. Raw video stays at the edge by default

The normal RavenMap data boundary is:

```text
camera
  │
  ├── raw video
  │      │
  │      └── local processing
  │
  └── sanitized observation ──────────► RavenMap
```

—not:

```text
camera ─── raw video ───► central server
```

This reduces bandwidth, storage, attack surface, disclosure risk, and the consequences of compromise.

A still image or sanitized crop may accompany an observation when policy permits it.

Continuous raw video should not become the central network's default input merely because bandwidth is available.

---

## 6. Distributed processing is trust-aware

RavenMap is being designed so expensive processing can occur away from the camera.

That does **not** mean every worker is trusted with every observation.

Workers will ultimately be described by both capability and trust:

```text
worker
  capabilities:
    - object-detection
    - plate-ocr
    - vehicle-classification
    - embedding
    - cuda

  trust:
    - public
    - sanitized
```

A separately controlled worker might instead be authorized for:

```text
trust:
  - operator-private
  - candidate-evidence
```

A job must satisfy both its computational requirements and its data-classification requirements before it can be assigned.

A volunteer GPU may be perfectly appropriate for processing public traffic-camera imagery while being completely inappropriate for private candidate evidence.

---

# Architecture

RavenMap is currently in an active architectural transition.

The fork intentionally retains working SparrowMap behavior while the inherited monolithic implementation is decomposed behind stable interfaces.

## Current inherited architecture

The SparrowMap baseline is approximately:

```text
Browser / PWA
      │
      ▼
   hub.py
      │
      ├── HTTP/API
      ├── authentication
      ├── node ingest
      ├── map queries
      ├── review
      ├── privacy orchestration
      ├── background work
      └── static content
      │
      ▼
    db.py
      │
      ▼
 SQLite + local data/
```

Camera and public-source processes already operate separately, but coordination between components is still largely built around HTTP, SQLite, local filesystem state, directories used as queues, and several deployment-specific scripts.

That architecture remains the **behavioral reference implementation** while the fork is refactored.

---

## Target architecture

RavenMap is moving toward explicit **control, data, storage, and presentation boundaries**:

```text
                              ┌──────────────────┐
                              │   RavenMap Web   │
                              │    Browser/PWA   │
                              └────────┬─────────┘
                                       │
                                       ▼
                    ┌─────────────────────────────────┐
                    │        RavenMap APIs            │
                    │                                 │
                    │  public read │ control/ingress  │
                    └──────────────┬──────────────────┘
                                   │
                     ┌─────────────┼─────────────┐
                     │             │             │
                     ▼             ▼             ▼
                PostgreSQL      Object        Job /
                + PostGIS       Storage       Event State
                     ▲                           │
                     │                           │
              observations                     │
                     │                           ▼
        ┌────────────┴─────────────┐     ┌───────────────┐
        │                          │     │    Workers    │
        ▼                          ▼     │               │
   Edge collectors          Source adapters  CPU / GPU
   RTSP / OpenIPC /         public APIs,     processing
   browser / native         public cameras
```

This is **not** a plan to turn every Python module into a microservice.

The immediate goal is a **modular monolith with explicit interfaces**.

A component becomes a separate process or container only when there is a reason for it to have an independent lifecycle, dependency set, scaling requirement, hardware requirement, or trust boundary.

---

# Smart containerization

RavenMap will containerize **workloads**, not merely repository directories.

Expected deployment roles include:

| Role                  | Purpose                                                             |
| --------------------- | ------------------------------------------------------------------- |
| `ravenmap-api`        | Trusted control, ingestion, authentication, and application API     |
| `ravenmap-public-api` | Public read plane with no private/operator data authority           |
| `ravenmap-web`        | Static browser/PWA application                                      |
| `ravenmap-worker-cpu` | CPU/ONNX image-processing workloads                                 |
| `ravenmap-worker-gpu` | CUDA/GPU-dependent classification and vision workloads              |
| `ravenmap-collector`  | Public/source-adapter ingestion                                     |
| `ravenmap-scheduler`  | Retention, maintenance, job coordination                            |
| `ravenmap-edge`       | Linux/RTSP/OpenIPC edge collection where containerization is useful |

Native Windows, macOS, browser, USB-camera, and hardware-specific edge applications do not need to be forced into containers merely for architectural symmetry.

---

# Edge reliability

A distributed sensing network must assume that networks fail.

RavenMap's target edge design therefore uses a **durable local outbox**:

```text
capture / detection
        │
        ▼
 local durable queue
        │
        ├── pending
        ├── retry
        └── delivered
        │
        ▼
    RavenMap ingress
```

Observations receive immutable event identifiers and central ingestion is designed to be idempotent.

A temporary outage should delay an observation.

It should not silently destroy it.

---

# Storage

Different kinds of RavenMap data have fundamentally different lifetimes and trust requirements.

They should not all be anonymous files beneath one application directory.

The target architecture distinguishes:

```text
structured application state
    └── PostgreSQL / PostGIS

durable media objects
    └── S3-compatible object storage

ephemeral cache
    └── disposable local/container state

secrets
    └── injected secret storage

edge buffering
    └── local durable storage

models
    └── versioned model artifacts
```

Media storage will likewise distinguish classes such as:

```text
public
candidate/private
quarantine
training
edge-only
```

with appropriate access and retention policies.

---

# Public and private planes

The inherited SparrowMap mirror design minimizes private data before making a deployment public.

RavenMap intends to strengthen that idea.

Rather than relying exclusively on runtime checks such as:

```python
if public_mirror:
    ...
```

the eventual public service should simply **lack private capabilities**.

A public-facing process should receive:

* only public routes;
* only public projections of data;
* only database permissions required for public access;
* no operator credentials;
* no private evidence store;
* no administrative functionality.

The security model should progress from:

> The service promises not to reveal private information.

to:

> **The service does not possess the authority required to retrieve private information.**

---

# Sources and processors

RavenMap treats observations and processing as separate concerns.

## Sources

Potential source adapters include:

```text
RTSP / ONVIF cameras
OpenIPC cameras
browser cameras
native desktop nodes
public DOT cameras
public geographic/APIs
synthetic test sources
future community integrations
```

A source produces observations.

It should not need to understand RavenMap's database.

## Processors

Processing capabilities may include:

```text
object detection
vehicle localization
plate localization
plate OCR
vehicle classification
embedding generation
image sanitization
future re-identification/correlation
```

A processor consumes an authorized observation or media object and produces a result.

It should not need to know which HTTP request caused that work to exist.

---

# Synthetic town

The inherited codebase includes a simulated town in:

```text
sources/synthetic.py
```

This provides known ground truth without requiring physical cameras.

It remains an important architectural feature.

A privacy-sensitive observation system should be testable without surveilling real people in order to develop it.

In the inherited runtime it is disabled by default and may be enabled with:

```bash
python hub.py --sim
```

Commands and launchers will evolve as the RavenMap package architecture replaces the inherited SparrowMap entry point.

---

# Current development status

RavenMap is presently being refactored from the SparrowMap baseline.

The initial architectural work intentionally follows a conservative rule:

> **Structural changes first; behavioral changes separately.**

The inherited implementation is being characterized with regression tests before major components are moved.

During this phase:

* existing API behavior is preserved;
* privacy/security behavior is preserved;
* database schemas are not silently redesigned;
* apparent bugs discovered during refactoring are documented rather than mixed into structural commits;
* major refactoring stages remain independently reviewable and revertible.

Only after those seams exist will storage, distributed workers, PostgreSQL/PostGIS, object storage, and new deployment boundaries replace their inherited counterparts.

---

# Repository layout

During the transition, much of the inherited SparrowMap structure remains:

```text
core.py          shared configuration and paths
privacy.py       hashing, key rotation, retention and privacy policy
classify.py      vehicle classification and publication gate
snapshot.py      crops, redaction and provenance
nodes.py         node identity, enrollment and camera geometry
db.py            inherited SQLite persistence and some legacy domain logic
hub.py           inherited HTTP server and current compatibility entry point

sources/
  synthetic.py   deterministic simulated source

public/
  browser application and vendored runtime assets

detect/
  full edge recognition pipeline

desktop/
  native contributor/node application

tools/
  development, diagnostics and migration utilities
```

This layout is **not** the final RavenMap architecture.

The intended direction is approximately:

```text
src/ravenmap/
  domain/
  application/
  ports/
  adapters/
  api/
  workers/
  edge/
  runtime/

frontend/
models/
ops/
tests/
```

Dependencies should progressively flow:

```text
transport / workers / CLI
           │
           ▼
      application
           │
           ▼
         domain
           │
           ▼
         ports
           ▲
           │
  infrastructure adapters
```

Domain code should not need to know whether production happens to use HTTP, PostgreSQL, SQLite, S3, Docker, or a particular deployment topology.

---

# API

The inherited HTTP API remains available during the compatibility/refactoring phase.

Representative endpoints include:

| Route                                            | Purpose                                                          |
| ------------------------------------------------ | ---------------------------------------------------------------- |
| `GET /api/sightings?since=&vclass=&bbox=&limit=` | Recent sanitized observations                                    |
| `GET /api/sighting/<id>`                         | Retrieve one observation                                         |
| `GET /api/track/<plate_hash>`                    | Temporarily correlated vehicle observations where policy permits |
| `GET /api/nodes`                                 | Camera nodes using privacy-protected public positions            |
| `GET /api/leaderboard?hours=`                    | Public-accountability observation statistics                     |
| `GET /api/policy`                                | Machine-readable privacy policy                                  |
| `GET /api/audit`                                 | Public-accountability publication decisions                      |
| `POST /api/enroll`                               | Enroll a camera/node                                             |
| `POST /api/sightings`                            | Submit a signed/supported observation                            |

The API is being characterized before it is decomposed so that structural refactoring cannot silently change authentication, payloads, status codes, privacy behavior, or public exposure.

---

# Verifiability

`/api/policy` exists for an important reason:

**A privacy promise that outsiders cannot verify is weaker than an enforceable and observable technical boundary.**

RavenMap should increasingly expose enough information about its own operation for researchers, contributors, operators, and communities to determine whether a deployment behaves as claimed—without granting them access to the private information those safeguards exist to protect.

Transparency should apply to the observer too.

---

# Known limitations

RavenMap inherits several limitations that must be addressed rather than hidden.

* Classification confidence values require calibration against appropriately labeled data. A numerical confidence score is not meaningful merely because a model emits one.
* Conservative publication rules necessarily reduce public-accountability coverage. That is preferable to publishing a private citizen because a classifier was optimistic.
* Human-submitted observations carry different provenance from automated observations and should remain distinguishable.
* Temporary pseudonymous correlation depends on protection and rotation of its secret key material.
* Automated redaction can fail and therefore requires defense in depth, conservative retention, and review controls.
* Distributed processing introduces additional trust boundaries; remote compute capacity does not automatically imply authorization to receive sensitive media.
* Public-source integrations depend on external services whose availability, licensing, formats, and policies can change.
* The current inherited architecture still contains filesystem coupling, SQLite coordination, process-local state, and several responsibilities concentrated in the hub. These are active rearchitecture targets, not desired end-state properties.

---

# Security and privacy invariants

Changes to RavenMap should be treated with particular suspicion if they weaken any of these properties:

1. Raw video normally remains at the source.
2. Private readable plate text is not intentionally persisted centrally.
3. Private retained imagery is irreversibly redacted.
4. Public classification requires conservative evidence.
5. Private correlation is bounded in time.
6. Public coordinates do not unnecessarily expose exact private camera locations.
7. Node identities and observations remain cryptographically authenticated where required.
8. Reviewer, operator, node, public, and worker trust are separate concepts.
9. Public services do not gain private authority merely for convenience.
10. Sensitive media is processed only by workers authorized for its data classification.
11. Retention is an enforceable system behavior rather than an administrative suggestion.
12. Structural refactoring must not silently weaken any of the above.

---

# Development philosophy

RavenMap favors:

**boring interfaces over clever coupling**

**explicit trust over implied trust**

**local processing over unnecessary central collection**

**data minimization over collecting first and deciding later**

**durable events over best-effort delivery**

**small independently reviewable changes over flag-day rewrites**

**regional/community ownership over mandatory centralization**

**observable policy over unverifiable promises**

**architecture before orchestration**

Docker, Kubernetes, queues, GPU workers, and distributed databases are tools.

They are not the architecture.

---

# Why the raven?

In *Grímnismál* stanza 20, Huginn and Muninn travel across Midgard each day and return with knowledge.

Their names are commonly understood as **Thought** and **Memory**.

Those ideas map naturally onto RavenMap:

```text
Huginn
Thought
    │
    └── observation, analysis, interpretation

Muninn
Memory
    │
    └── provenance, history, accountability
```

But Norse wisdom literature also repeatedly warns that knowledge must be accompanied by judgment.

*Hávamál* stanza 7 praises the knowing guest who listens and watches.

Stanzas 77–78 emphasize that deeds and the reputation they leave behind endure.

Stanza 127 counsels the listener not to remain indifferent when wrongdoing is known.

RavenMap uses those references as themes, not as claims of ancient endorsement for modern technology.

The name is a reminder that **seeing and remembering create responsibilities of their own.**

---

# Mythological references

Primary textual references for the project's name and themes:

* ***Grímnismál*, stanza 20** — Huginn and Muninn range across Midgard and return to Óðinn.
* ***Hávamál*, stanza 7** — wisdom expressed through attentive watching and listening.
* ***Hávamál*, stanzas 77–78** — deeds and reputation outlasting the individual.
* ***Hávamál*, stanza 127** — recognizing and speaking against wrongdoing.

English stanza numbering and wording vary among translations. The project currently uses the numbering found in Henry Adams Bellows' 1923 translation of *The Poetic Edda* as a convenient public-domain reference.

---

# License

RavenMap is derived from SparrowMap and remains licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**. See `LICENSE`.

In practical terms, the AGPL permits people to use, study, modify, and redistribute the covered code under its terms.

It also contains an important network-use provision: if you modify the covered program and allow users to interact with that modified version remotely through a computer network, those users must be offered access to the corresponding source code as required by the license.

For RavenMap, that property is intentional.

Software used to observe public spaces or support public accountability should itself be open to examination.

The codebase also incorporates third-party components with their own licenses and notices. Those dependencies must continue to be reviewed and documented as RavenMap's packaging and model architecture evolves.

The AGPL governs copyright licensing of the covered software. Project names, logos, and other branding may be governed separately from the software license itself.

---

# Fork lineage

RavenMap began as a fork of **SparrowMap**.

The fork preserves SparrowMap's history and the substantial work embodied in its original privacy model, local processing pipeline, simulated environment, node identity system, map interface, review workflow, and public-accountability design.

RavenMap intentionally diverges in architecture.

The long-term objective is not to maintain patch-level compatibility with SparrowMap. Upstream remains valuable as reference material and as a possible source of individual fixes or improvements, but RavenMap's architecture is governed by RavenMap's own requirements.

---

# The long-term model

Running your own regional instance is not an edge case.

It is the point.

A resilient community-owned network should look less like:

```text
everyone
   │
   ▼
one enormous central authority
```

and more like:

```text
regional RavenMap ───── regional RavenMap
       │                       │
       │ trusted/federated     │
       │ exchange where        │
       │ policy permits        │
       ▼                       ▼
 local sources             local sources
 local workers             local workers
 local governance          local governance
```

Huginn and Muninn return with knowledge.

RavenMap's job is to ensure that what we choose to observe and remember remains worthy of the trust required to gather it.
