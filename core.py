"""SparrowMap - shared paths, config and small helpers.

SparrowMap is a citizen-run camera network. Volunteers point a camera at a public
road from their own property. The camera does all recognition locally and sends
the hub a *detection event*, never video.

Name: Matthew 10:26-29. "There is nothing covered, that shall not be revealed...
what ye hear in the ear, that preach ye upon the housetops... one sparrow shall
not fall on the ground without your Father." Spoken to people about to be hauled
in front of governors and kings.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SNAPS = DATA / "snaps"

# Photographs pulled off the map but not yet destroyed.
#
# 🚨 THE POINT OF THIS DIRECTORY IS THAT NO ROUTE SERVES IT. `/snap/<name>`
# hands out any file in SNAPS by name, so clearing a row's `snap` column takes
# the picture off the map and leaves it fetchable by direct URL - which is the
# exact mistake the retracted-photo shelf exists to clean up after. A photograph
# somebody has flagged as showing them is not taken down until the FILE stops
# being reachable, so holding one means MOVING it, not un-referencing it.
#
# It is a hold, not a delete, because the whole reason a photo is here is that a
# human has not looked yet: the flag may be wrong, and the original is the only
# thing a full-quality crop can be cut from. review_api.fix_photo either crops
# it back onto the map, restores it untouched, or deletes it for good.
HELD = DATA / "held"

# The full-resolution, UNREDACTED crop of a government CANDIDATE, kept only
# until a human answers.
#
# 🚨 WHY THIS HAD TO EXIST. Ingest classifies a vehicle, then forces the tier
# back to private so a person decides. Everything downstream reads `tier`, so a
# patrol car awaiting review was handled as an ordinary civilian car and the
# evidence was destroyed BEFORE the human was asked: its door livery was painted
# out as a plate-shaped region, its photo was discarded outright when no plate
# box was found, and the only surviving copy was the 200px pen crop - which is
# what got published on confirm. The system was degrading exactly the thing it
# needed in order to make, and then to support, the decision.
#
# Degradation is irreversible and the decision is deferred, so the original has
# to outlive the wait. The rails that make that acceptable:
#
#   * NO ROUTE SERVES THIS DIRECTORY. Same rule as HELD. It is not SNAPS, so
#     `/snap/<name>` cannot reach it even if a filename leaks.
#   * It is never written to `sightings.snap`, so it is on no public read path.
#   * A reviewer reaches it only through the authenticated pen (`may_touch`).
#   * It is deleted the moment a verdict lands - confirmed or rejected - and
#     swept after EVIDENCE_TTL_S if nobody ever answers.
#   * HOME ONLY. A mirror carries claims, not un-degraded photographs of the
#     street (mirror.evidence_write refuses).
#
# ⚠️ AND THE HONEST COST, WHICH IS REAL. The classifier runs near 95% precision,
# so roughly one in twenty vehicles held here is NOT a government vehicle - an
# ordinary car whose plate is legible in this file until somebody rejects it.
# That is a deliberate trade the operator made knowingly: a reviewer cannot
# judge a picture that has already been destroyed, and the harder the call the
# better the picture needs to be. It is the reason for every rail above, and it
# is why the sweep is aggressive rather than tidy.
EVIDENCE = DATA / "evidence"

# Bug reports from the site: a description, and usually a screenshot.
#
# 🚨 NO ROUTE SERVES THIS DIRECTORY, and the reason is specific rather than
# habitual. A screenshot of SparrowMap taken by somebody having trouble with it
# very often contains the thing they were having trouble WITH - their camera
# key, the QR that is their camera key, a reviewer token, or the street outside
# their window. They are handing it over to get help, not to publish it.
#
# It follows that a bug report must never reach the alert channel either. The
# GitHub issue that notifies the operator carries an id and nothing else,
# because ALERT_REPO defaults to the PUBLIC code repo - see tools/bug_alert.py.
BUGS = DATA / "bugs"

# How long a report is kept if nobody deals with it. Longer than EVIDENCE
# because a bug report is not imagery of the street and the person chose to
# send it - but bounded, because "we still have your screenshot from March" is
# not an answer anybody wants.
BUGS_TTL_S = 30 * 24 * 3600

# How long an unanswered candidate's original may sit there. Deliberately far
# shorter than the 14-day private-tier retention: this is un-degraded imagery,
# and the only thing justifying it is that somebody is about to look.
EVIDENCE_TTL_S = 72 * 3600

# 🚨 EVERY CLIENT THAT TALKS TO THE HUB MUST SAY WHO IT IS.
#
# Not politeness - it is load-bearing. The node clients sent no User-Agent at
# all, so urllib defaulted to "Python-urllib/3.x", and the moment Cloudflare was
# put in front of map.sparrowmap.com its bot protection banned exactly that
# signature: every camera POST came back 403 "error code: 1010" and never
# reached the origin, while browser-based phone nodes carried on fine. Ingest
# stopped network-wide and nothing said so, because a camera that cannot reach
# the hub has nowhere to report that it cannot reach the hub.
#
# The server side of this project has always identified itself to third parties
# (Overpass, Nominatim, the tile CDN, aircraft). The clients pointed at our OWN
# hub were the ones that did not, which is backwards: those are the requests we
# most need to be able to recognise, allow-list and debug.
NODE_UA = "SparrowMap-Node/1.0 (+https://sparrowmap.com)"
PUBLIC = ROOT / "public"
DB_PATH = DATA / "sparrow.db"
CONFIG_PATH = ROOT / "config.json"

for _d in (DATA, SNAPS):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
# Every value here is a *policy* decision, not a tuning knob. They are in one
# place on purpose: a person auditing this project should be able to read the
# entire privacy posture of a deployment in one screen.

DEFAULTS = {
    "site_name": "RavenMap",

    # Optional CARTO basemap API key.
    # Deployment-specific; never commit a real key.
    "carto_api_key": "",

    # 🧪 STAGED, OFF BY DEFAULT. The aircraft page (/planes) reads live ADS-B
    # and the FAA registry to show government aircraft and ones that are
    # circling. It is off in every deployment that does not explicitly enable
    # it, which is how a feature can live in the repo - so local, repo and box
    # all match - without appearing on a public map before it has earned it.
    # Turn on locally with "aircraft_preview": true in config.json.
    "aircraft_preview": False,
    "http_port": 8150,
    "https_port": 8151,

    # Map opens here. Region-level and deliberately rounded: a default that
    # ships in the source must not be street-level over anyone's camera.
    "map_center": [42.7, -84.5],
    "map_zoom": 8,

    # ---- privacy policy -------------------------------------------------
    # Civilian plates are hashed at the edge and the readable text is never
    # stored. Government / police / fleet plates are stored readable, because a
    # publicly owned vehicle doing public work on a public road is a public
    # record. This is the single most important setting in the project.
    # 🚨 POLICE ONLY. His call, 2026-08-13: "just police type vehicles - no
    # regular city work trucks, no ambulance, no firefighters."
    #
    # A council pickup on a public road is still a publicly owned vehicle, so
    # publishing it was defensible - but it is not what anyone comes to this
    # map for, and every bin lorry on it dilutes the one claim the project
    # exists to make. gov_dot publishing bin lorries is already recorded
    # history here. Narrowing the claim makes it easier to defend, not harder.
    #
    # Nothing is lost: the classification still happens and the row is still
    # kept privately, so a decision to widen this later has the data to do it.
    "public_tiers": ["police"],

    # ⚠️ MAY THIS DEPLOYMENT ASSERT "police" ABOUT A VEHICLE IN PUBLIC?
    #
    # OFF until a vehicle classifier has been measured against locally
    # labelled footage. Not caution for its own sake - measured: the colour
    # heuristic was wrong 5/5 on the reference camera's street, and CLIP zero-shot calls
    # POLICE on 40% of ordinary traffic, confidently. Precision on this camera
    # is zero and there is no confirmed police vehicle anywhere in the data to
    # calibrate against.
    #
    # While this is off the network still watches, still counts every pass as
    # traffic, and still banks crops for training. It simply does not make a
    # claim it cannot support. Turn it on when detect/calibrate.py reports a
    # real precision AND a real recall on held-out local data.
    "publish_public_tier": False,

    # How long a civilian sighting survives before it is deleted, in days.
    # Flock Safety's default is 30. Shorter is better; a trail you cannot
    # assemble is a trail that cannot be abused.
    "civilian_retention_days": 14,

    # Government-tier sightings are kept indefinitely by default (0 = forever),
    # for the same reason council minutes are kept.
    "public_retention_days": 0,

    # The plate pepper is rotated on this cadence. After a rotation the same
    # car hashes to a different value, so trails cannot be joined across the
    # boundary. This puts a hard ceiling on how long anyone - including the
    # operator of this hub - can follow a private citizen.
    "pepper_rotation_days": 30,

    # Node positions shown on the public map are jittered by up to this many
    # metres, so publishing the camera layer does not publish which specific
    # house is watching. Set 0 to show exact positions.
    "node_position_jitter_m": 60,

    # Blur detected faces in stored snapshots. The subject of this system is
    # vehicles, not people.
    "blur_faces": True,

    # Refuse to store a sighting whose plate read is weaker than this.
    "min_plate_confidence": 0.55,

    # ---- contribution ---------------------------------------------------
    # When true, a brand new node may post sightings before a human approves
    # it. Useful for a demo, wrong for a real deployment - so the DEFAULT is
    # now false (fail closed). A fresh box auto-generates its config.json from
    # these defaults, and an auto-approving public node is how a stranger puts
    # self-asserted evidence on the map. Flip it to true explicitly for a demo.
    "auto_approve_nodes": False,

    # The operator's DECISIONS on public-tier claims - every confirm, retract
    # and public flag - are written to the audit log and shown publicly. Watching
    # the watchers means the map's own decisions are auditable. Note what is NOT
    # here: a search against public data is never logged or counted. Tracking who
    # reads a public record is the surveillance this project exists to refuse.
    "public_audit_log": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:  # a broken config must not silently loosen policy
            raise SystemExit(f"config.json is not valid JSON: {exc}")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


CONFIG = load_config()
if not CONFIG_PATH.exists():
    save_config(CONFIG)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now() -> float:
    return time.time()


def is_operator_addr(addr: str) -> bool:
    """May a caller from this address use the operator-only routes?

    🚨 THIS WAS IPv4-ONLY AND IT SILENTLY BROKE BOTH OPERATOR PAGES.
    `example-host` resolves to an IPv6 unique-local address
    (fd00:2e67:...), so reaching the labelling or review pages by HOSTNAME -
    which is how every link in the Pages hub is written, and the only way the
    phone can reach them - produced 403 "local only". The labelling buttons did
    nothing and the review page showed an empty state, both without an error a
    user could see. Reaching them as `localhost` worked, which is exactly why
    it passed every test I ran.

    Dual-stack keeps biting this project (see the IPv4-only bind that cost
    2-4s per request). The lesson is the same each time: on this box a
    hostname is IPv6 first, so any address logic has to handle both families.

    `ipaddress` already knows about loopback, link-local, RFC1918 and IPv6
    unique-local (fc00::/7, which covers Tailscale's fd7a::/48). The one gap is
    IPv4 CGNAT 100.64.0.0/10, which Tailscale also uses and which the stdlib
    does not count as private.

    ⚠️ ONE IMPLEMENTATION, ON PURPOSE. The hub and camctl both need this, and a
    security check copied into two files is a check that will be fixed in one
    of them.
    """
    import ipaddress
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in ipaddress.ip_network("100.64.0.0/10"):     # Tailscale IPv4
        return True
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing from point 1 to point 2, degrees clockwise from north."""
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings, 0-180."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)
