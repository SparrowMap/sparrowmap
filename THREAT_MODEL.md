# Anonymity and attack preparation

Written for the launch of sparrowmap.com on a public box. Two questions:
how do we protect the people who use this, and what happens when somebody
attacks it.

---

## 1. Who needs protecting, and from whom

Three groups, and they need different things. Conflating them is how privacy
work goes wrong.

| | who they are | what they fear | what protects them |
|---|---|---|---|
| **Operators** | people who put a camera in a window | being identified as the person watching police | node aliasing, jitter, no contact field served |
| **Viewers** | anyone opening the map | a record that they looked | no viewer logging, keyed audit hashes, no-referrer |
| **The photographed** | every driver on the road | their movements becoming a searchable history | two-tier: private plates destroyed in pixels at the camera |

The third group is already the whole design. The first two were thinner than
they should have been and are what this pass fixed.

### The one place the third group is knowingly exposed

A government CANDIDATE keeps a full-resolution, unredacted crop of the vehicle
(`core.EVIDENCE`) from the moment it is classified until a human answers. The
classifier runs near 95% precision, so roughly **one in twenty vehicles held
there is an ordinary car**, and for as long as it waits its plate is legible in
that file.

This is a deliberate trade, not an oversight. The alternative is the behaviour
it replaced: the plate regions were painted out and the crop shrunk to 200px
*before* anyone was asked, which destroyed the door livery a reviewer judges by
and the plate the public tier exists to publish - so the system degraded exactly
the evidence needed to make the decision, and then published the degraded copy.
A reviewer cannot judge a picture that has already been destroyed.

What bounds it:

* **no route serves that directory** - it is not `SNAPS`, so `/snap/<name>`
  cannot reach it even if a filename leaks;
* it is **never written to `sightings.snap`**, so it is on no public read path,
  and `privacy.redact` already withholds a private row's photo from anon;
* a reviewer reaches it only through the authenticated pen (`may_touch`);
* it is **deleted on every verdict** - confirmed, gov, or rejected;
* it is **swept after 72 hours** regardless (`core.EVIDENCE_TTL_S`), on its own
  clock, so a queue nobody works cannot hold originals indefinitely under a rule
  written to protect the decision rather than the picture;
* **home only.** `mirror.evidence_write` refuses on a mirror, so the public box
  never holds one.

Nothing changes for a vehicle the classifier does not call government: its plate
is still destroyed in the pixels at the camera, exactly as the row above says.

### Operators

**What was leaking:** every public sighting carried `node_id`. Anyone could
group a volunteer's entire output by camera and, with the road span published
accurately, build a profile of one identifiable installation - its hours, its
quiet days, the week it went dark.

**Now:** anonymous viewers get a **per-day rolling alias** (`n:d0a1bb3a`), the
same treatment plate hashes already had. A camera's sightings still group
together on screen, which the map needs, but cannot be joined across days into
a history of one household.

**What still cannot be hidden, and should be said plainly:** a fixed camera
watching a named road is not anonymous from anyone who walks that road. The
span is published *deliberately* - it is the honest part of the bargain, the
thing that makes the map a public record rather than a secret one. Position
jitter (measured 44 m off true) hides the house, not the street.

➡️ **The strongest operator protection is not technical.** It is that the map
publishes government vehicles and destroys everything else, so there is
nothing here worth compelling out of an operator.

### Viewers

**What was leaking:** the audit table stored raw IP addresses. The table exists
for a good reason - *"we are building a surveillance network; the least we can
do is be the first thing it watches"* - but a column of addresses turns an
accountability record into the exact dossier the project refuses to keep about
vehicles. Somebody checking whether police are near a protest should not be
identifiable from this file a year later.

**Now:** addresses are stored as a **per-day keyed hash** (`ip:551ee966...`),
keyed with the rotating pepper. Repeated actions from one person still group
within a day, which is all abuse triage needs. Nothing survives the day.
Keyed, not plain - an unkeyed hash of an IPv4 is reversible in seconds.

**Also:** `Referrer-Policy: no-referrer`. Without it every outbound click - the
OpenStreetMap attribution link at the bottom of the map, for one - tells the
destination that the visitor came from sparrowmap.com, and carries the URL
including any plate they searched.

**Still to decide:** the reverse proxy will keep its own access log. **Turn it
off, or truncate it.** Everything above is undone by one nginx default.

---

## 2. Reducing what a breach can take

Assume the box is compromised. The question is what they get.

**Today, if the box ran the full hub, they would get:**

* 2,027 private-tier sightings - fourteen days of a street's traffic
* 1,978 stored images
* **3 node true positions** - which defeats the jitter entirely and hands over
  exactly where each volunteer's camera is

That last one is the reason to change the architecture rather than only harden
the box.

### The recommendation: the box is a MIRROR, not the hub

Keep the hub at home, where it already is. The box serves the public map from
a **push-only replica** that contains:

* public-tier sightings in full - they are the published record;
* private-tier rows reduced to `(ts, lat, lon, vclass)` for the live traffic
  view, with **no hash, no image, no bank reference**;
* node **spans and jittered positions only** - never `lat`/`lon`/`contact`.

Then a full compromise of the internet-facing machine leaks the public record,
which is public, plus some anonymous dots. No true camera positions, no
private images, no plate hashes, no operator tokens.

This also removes the proxy problem: the mirror has no operator routes to
protect, because reviewing happens at home.

**Cost:** a sync job and no live review from the public site. Worth it.

---

## 3. Hardening already applied in the code

| risk | before | now |
|---|---|---|
| operator gate behind a proxy | every visitor is an operator | token auth, socket address ignored |
| camera takeover | `enroll` returned any node's token | re-enrol requires the node token |
| open state change | `/api/purge` unauthenticated | operator only |
| flooding | no limit | 5 enrol/h, 600 sightings/h per address |
| version disclosure | `SparrowMap/0.1.0 Python/3.12.10` | `SparrowMap` |
| injection | no CSP | strict CSP with per-response nonce |
| clickjacking | none | `X-Frame-Options: DENY`, `frame-ancestors 'none'` |
| MIME confusion | none | `nosniff` |
| referrer leak | default | `no-referrer` |
| operator JSON CORS | `*` on everything | wildcard dropped on operator routes |

Verified sound and recorded so nobody re-audits them: path traversal 404s on
`../` and `%2e%2e%2f` across `/static`, `/snap`, `/vendor`; node positions
served from the jittered field; `contact` and `token` never serialised; error
responses are generic while the traceback stays in the log.

---

## 4. The box itself

The application is now the harder half. The usual way in is the machine.

**Before the domain points anywhere:**

1. **SSH: keys only.** `PasswordAuthentication no`, `PermitRootLogin no`.
   Password auth on a public IP is scanned within minutes.
2. **Firewall default-deny.** Inbound 22, 80, 443 only. The hub's 8150/8151
   must NOT be reachable from the internet - the proxy talks to it on
   localhost. If 8150 is exposed directly, every header and auth control here
   can be bypassed by going round the proxy.
3. **Unattended security upgrades.** Most real compromises are a known CVE in
   something nobody patched.
4. **fail2ban** on sshd and on the proxy's 401/429 responses.
5. **Run the hub as its own unprivileged user.** Not root. It needs its data
   directory and nothing else.
6. **TLS via Let's Encrypt**, HSTS once you are sure, and set `behind_tls: true`
   so the operator cookie gets `Secure`.
7. **Back up `data/` off the box**, encrypted, and *test a restore*. An
   untested backup is a belief, not a backup.
8. **Keep `data/operator.token` out of the backup you carry around**, and know
   how to rotate it: delete the file, restart, sign in again.

**Expect specifically:**

* automated scanners within hours of DNS resolving - they are looking for
  `/wp-admin`, `/.env`, `/.git`. Make sure `.git` is not served;
* people trying `/api/enroll` to plant fake cameras - rate limited. The live
  box runs `auto_approve_nodes: true` (Matthew's call 2026-10-28), so a new node
  posts at once and a fake one is removed by revoking it; `false` would hold
  every new node until approved;
* attempts to POST false police sightings - every submission is attributed to
  a node token, so the fix is revoking one node, not rebuilding the map;
* someone framing the site to trick a click - blocked;
* **and the one nobody plans for: a lawful request for your logs.** The best
  answer is not having them. That is what the hashing above is for.

---

## 5. What is still open

* **`publish_public_tier: false` on the box to start.** Let the head run there
  for a few days and read the review queue before the map asserts anything.
* **No CAPTCHA or account system**, deliberately. Attribution by node token is
  the abuse control, and it is enough while enrolment is manual.
* **The mirror in section 2 is not built yet.** Until it is, running the full
  hub on the box means a breach exposes true camera positions. That is the
  largest single risk remaining, and it is architectural rather than a
  vulnerability to patch.
