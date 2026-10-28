"""SparrowMap - node identity and enrollment.

A node is somebody's camera on somebody's porch. The hub needs to know that a
sighting claiming to come from "Maple St" really did, because a network that
accepts unsigned reports can be flooded with invented ones - and an invented
sighting placing a named vehicle at a named place at a named time is a very
effective way to hurt someone.

So each node holds an ed25519 private key that never leaves the device, and
signs every event. The hub stores only the public half. This also means the hub
operator cannot forge a node's reports, which matters for a network whose whole
premise is not trusting whoever holds the database.
"""

from __future__ import annotations

import re

import base64
import json
import math
import random
import secrets
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

import db
from core import CONFIG, now


def new_keypair() -> tuple[str, str]:
    """Generate a node identity. Returns (private_b64, public_b64).

    Runs on the NODE, not the hub. The hub never sees the private half.
    """
    from cryptography.hazmat.primitives import serialization
    sk = Ed25519PrivateKey.generate()
    priv = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())
    pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def canonical(event: dict) -> bytes:
    """Byte form that gets signed. Must match exactly on both sides.

    Only the fields that carry meaning are signed, so a hub may add derived
    fields (tier, class confidence) without invalidating the node's signature.
    """
    keep = ("node_id", "ts", "lat", "lon", "plate_hash", "plate_text",
            "vclass", "heading", "speed_mph", "snap_sha256")
    return json.dumps({k: event.get(k) for k in keep},
                      sort_keys=True, separators=(",", ":")).encode()


def sign_event(event: dict, priv_b64: str) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))
    return base64.b64encode(sk.sign(canonical(event))).decode()


def verify_event(event: dict, sig_b64: str, pub_b64: str) -> bool:
    if not (sig_b64 and pub_b64):
        return False
    try:
        pk = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        pk.verify(base64.b64decode(sig_b64), canonical(event))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Placement privacy
# --------------------------------------------------------------------------

def jitter_position(lat: float, lon: float, metres: float) -> tuple[float, float]:
    """Offset a node's published position by a random vector under `metres`.

    Publishing the camera layer is important - people are entitled to know
    where they are being recorded - but publishing it to the metre also
    publishes which specific house volunteered, and volunteers should not have
    to accept a target on their door to take part. The jitter is regenerated
    once at enrollment and then fixed, so it cannot be averaged away by
    watching the map over time.
    """
    if metres <= 0:
        return lat, lon
    r = metres * math.sqrt(random.random())
    theta = random.random() * 2 * math.pi
    dlat = (r * math.cos(theta)) / 111_320.0
    dlon = (r * math.sin(theta)) / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return round(lat + dlat, 6), round(lon + dlon, 6)


# How far a camera must move before it counts as a DIFFERENT camera.
# Past GPS noise (5-20 m), past nudging a phone on a windowsill, inside one
# block, and equal to the published position jitter - so a move big enough to
# split a node is a move the map could already have shown.
MOVED_THRESHOLD_M = 60.0


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


class NodeAuthError(Exception):
    """Re-enrolling an existing node without proving you own it."""


def _clean_name(name: str) -> str:
    """Strip markup and control characters from an operator-supplied name."""
    s = str(name or "")[:80]
    s = re.sub(r"[<>]", "", s)                      # no tags, anywhere
    s = "".join(ch for ch in s if ch == "	" or ord(ch) >= 32)
    return s.strip() or "Camera node"


def enroll(name: str, lat: float, lon: float, pubkey: Optional[str] = None,
           heading: float = 0, fov: float = 60, reach_m: float = 45,
           kind: str = "fixed", contact: str = "", node_id: str = "",
           snap_road: bool = True, token: Optional[str] = None) -> dict:
    """Register a camera. Returns the stored node record.

    🚨 RE-ENROLLING AN EXISTING NODE REQUIRES ITS TOKEN.

    Without that check this function was a complete camera takeover, reachable
    by anyone: POST /api/enroll with somebody else's `node_id` and it returned
    THEIR token, because the existing token is deliberately preserved so that
    re-saving a placement does not invalidate a running detector. With the
    token you can post sightings as that camera, move it anywhere on the map,
    or rename it.

    And node ids are not secret - they are printed on the public map next to
    every sighting and on the review page (e.g. `n_example1`). There was nothing
    to guess.

    Both clients already sent the token when updating; only the server never
    looked at it.
    """
    # 🚨 A NODE NAME IS PUBLIC AND OPERATOR-SUPPLIED. CLEAN IT AT THE SOURCE.
    # Every current render path escapes it (esc() on the map, textContent on the
    # review and retracted pages), so this is not a live hole - it is defence in
    # depth against the mistake this codebase keeps making: a control applied to
    # one representation and bypassed by another. A name reaches the map, the
    # review app, the retracted shelf, health-check logs and any future export,
    # and only one of those has to forget.
    #
    # Not hypothetical: a node is enrolled right now called "<h1>hello</h1>",
    # so somebody is already testing this field.
    #
    # Markup characters and control characters go; the name itself is left
    # otherwise intact, because a volunteer naming their camera should not have
    # to think about our storage.
    name = _clean_name(name)
    nid = node_id or ("n_" + secrets.token_hex(4))
    moved_from = None
    if node_id:
        _prior = db.node(nid)
        # 🚨 A REVOKED CAMERA STAYS REVOKED (10-27). revoke_node.py sets
        # token=NULL so the seized key dies - and the token check just below
        # only runs when a token EXISTS. So anyone holding nothing but the
        # node id, which the public map prints, could re-enrol it: a fresh
        # token, a new name, status flipped from 'revoked' back to live
        # (auto-approve on). Reproduced on the mirror before this line. The
        # holder of the seized phone is exactly who that let back in.
        if _prior and _prior.get("status") == "revoked":
            raise NodeAuthError("this camera was revoked; it cannot be re-enrolled. "
                                "Add a new camera instead.")
        if _prior and _prior.get("token"):
            if not token or not secrets.compare_digest(str(_prior["token"]),
                                                       str(token)):
                raise NodeAuthError(
                    "this camera already exists; updating it needs its token")

        # 🚨 A CAMERA THAT MOVES IS A DIFFERENT CAMERA.
        #
        # Re-enrolling used to mutate the row in place, so carrying a phone to
        # another street rewrote the watched road on a node that already had
        # thousands of sightings attached. The sightings themselves kept their
        # recorded positions - those are stored per row at ingest - but the
        # SPAN jumped, so the map drew a line on the new street with the old
        # dots stranded on the previous one, and the camera now asserted it had
        # been watching a road it had never seen.
        #
        # He put it exactly right: moving a device should never move the
        # sightings, it should create a new node for the stretch of road it can
        # now see. The old node keeps its span, its history and its claim -
        # true for the period it was there - and simply stops receiving new
        # passes. It goes offline on its own, because it stops beating.
        #
        # ⚠️ THE THRESHOLD IS NOT ZERO ON PURPOSE. A phone re-registering from
        # GPS reports a position that wanders by 5-20 m without anybody
        # touching it, and splitting a node on jitter would litter the map with
        # duplicate cameras - the exact failure the double-tap bug just caused.
        # 60 m is past GPS noise, past nudging a phone on a windowsill, and
        # still inside one block. It also matches the published position
        # jitter, so a move that matters is a move the map could already show.
        if (_prior and _prior.get("lat") is not None
                and lat is not None and lon is not None):
            moved_m = _distance_m(_prior["lat"], _prior["lon"], lat, lon)
            if moved_m > MOVED_THRESHOLD_M:
                moved_from = {"id": nid, "metres": round(moved_m)}
                nid = "n_" + secrets.token_hex(4)   # a new camera, from here on
                node_id = ""                        # ...so mint it a new token
    plat, plon = jitter_position(lat, lon,
                                 float(CONFIG.get("node_position_jitter_m", 60)))

    # Work out the stretch of road this lens covers, from the TRUE position,
    # once, here. See road.py for the privacy reasoning behind all of this.
    #
    # ⚠️ ONLY A FIXED CAMERA HAS A WATCHED SPAN, and the test used to be
    # `kind != "mobile"` - which let a PHONE through, because a phone enrolls as
    # kind='phone'. Two of his did, and each got an 80 m line of imaginary road
    # running due north, because a phone has no aim to snap and `heading`
    # defaults to 0. The map then drew a confident wedge onto a road that does
    # not exist. A phone is carried, points wherever its owner is standing, and
    # its sightings carry their own GPS - so it gets no span at all.
    #
    # Allow-list rather than deny-list: a new kind added later defaults to
    # having no span, which is the safe direction. Publishing nothing is
    # recoverable; publishing a fabricated road is not.
    # 🚨 A SPAN REQUIRES A REAL AIM, NOT A SELF-DECLARED `kind`.
    #
    # This used to trust the client's `kind` alone, and the unified camera app
    # sends kind='fixed' for every device - so four phones enrolled through it
    # each got an 80 m line of imaginary road running DUE NORTH, because a
    # browser reports heading=0 when it has no compass fix. The map then drew a
    # confident wedge onto a road that does not exist. That is the same
    # fabricated-span bug as before, arriving from the other direction: last
    # time the server's test was wrong, this time the client's claim was.
    #
    # So the gate is now the thing a span actually depends on: did anybody
    # AIM this camera. A browser only reports a heading while the device is
    # MOVING, so an enrolment from a window never has one - and a node with no
    # aim gets no span until its owner points it on the placement page.
    #
    # Publishing nothing is recoverable. Publishing a fabricated road is not.
    #
    # 🔁 REVISED: "no span" turned out not to mean "publish nothing" - it meant
    # fall through to pushing a point out along heading 0, i.e. due north of the
    # camera, which put contributors' sightings on buildings. That is a
    # fabricated position with no road attached, which is the worse half of both
    # options. So an unaimed FIXED camera now snaps to the nearest real road
    # (road.span_nearest), which claims less than an aimed span and is true.
    #
    # 🚨 THE LINE IS CARRIED-vs-STATIONARY, NOT PHONE-vs-FIXED.
    #
    # This used to span only kind='fixed', on the reasoning that "a phone is
    # carried, so there is no stretch of road it watches, and its sightings
    # carry their own GPS instead". The first half is true of a phone in a
    # POCKET. The second half is not true of the thing this project actually
    # asks people to do: a phone propped in a window is stationary, watches one
    # street, and sends no per-sighting GPS at all - so with no span its
    # sightings fall back to a single invented point and stack there for ever.
    # That was the "dot in the park with thousands of passes behind it".
    #
    # So everything gets a span EXCEPT kind='mobile', which is the one that is
    # genuinely carried: driving mode, whose sightings do carry their own GPS
    # and for which a fixed watched road is a false claim about a street.
    # 🚨 A PUBLIC TRAFFIC CAMERA GETS NO SPAN EITHER, FOR THE OPPOSITE REASON.
    #
    # A span exists to publish the ROAD a camera watches while withholding the
    # HOUSE it sits in. A government traffic camera has no house to protect:
    # the transport authority publishes its exact coordinates, SparrowMap
    # passes them straight through, and the transparency page says so. There is
    # nothing left for a span to hide.
    #
    # It is also what makes scale possible. road.resolve() is an Overpass query
    # per camera, and these arrive in thousands - Finland alone is 2,160. That
    # is thousands of requests to somebody else's free service to compute a
    # privacy screen for a camera whose position is already public. The
    # sightings carry the camera's own published position, which is the honest
    # thing to draw anyway.
    span = None
    if kind not in ("mobile", "public_cam"):
        import road
        span = road.resolve(lat, lon, heading or 0.0, fov, reach_m,
                            online=snap_road)

    # ⚠️ EVERY node gets a token, not just phones.
    #
    # Originally only kind='phone' got one, on the reasoning that fixed nodes
    # would hold an ed25519 key. In practice the placement page enrols a fixed
    # node with no key, and the ingest path only checks a token IF one exists -
    # so that node could be posted to by anyone on the network with no
    # authentication at all. A node that is unauthenticated by omission is
    # worse than one that is unauthenticated by decision, because nothing in
    # the code says so.
    #
    # A bearer token is weaker than a signature - whoever steals it can post as
    # that node - but it makes every submission ATTRIBUTED, which is the
    # control that matters here: someone flooding the map with false police
    # sightings can be found and revoked.
    #
    # An existing node keeps its token so re-saving a placement does not
    # invalidate a running detector.
    existing = db.node(nid) if node_id else None
    token = (existing or {}).get("token") or secrets.token_urlsafe(24)

    rec = {
        "id": nid, "name": name, "pubkey": pubkey, "token": token,
        "lat": lat, "lon": lon, "pub_lat": plat, "pub_lon": plon,
        "heading": heading, "fov": fov, "reach_m": reach_m,
        "kind": kind, "contact": contact, "created": now(),
        "status": "active" if CONFIG.get("auto_approve_nodes", True) else "paused",
        "span_lat1": None, "span_lon1": None, "span_lat2": None,
        "span_lon2": None, "road_name": None, "span_source": None,
    }
    if span:
        (a_lat, a_lon), (b_lat, b_lon) = span["span"]
        rec.update({"span_lat1": a_lat, "span_lon1": a_lon,
                    "span_lat2": b_lat, "span_lon2": b_lon,
                    "road_name": span["road_name"],
                    "span_source": span["source"]})
    import mirror
    db.upsert_node(mirror.node_fields(rec))
    if moved_from:
        # The caller must tell the owner, and the client must adopt the new id
        # and token - otherwise it keeps posting as a camera that is no longer
        # where it says it is.
        rec["moved_from"] = moved_from
        # 🚨 AND RECORD IT, BECAUSE THE OLD ROW IS NO LONGER A CAMERA.
        # This fact was computed here, handed back, and then forgotten. The
        # retired node keeps beating until the device adopts its new id - and
        # if the device never does, for ever - so it counted as a camera ONLINE
        # the whole time. One physical camera at Ross street appeared three
        # times, all reporting in.
        #
        # Written AFTER the new row is stored, so a failure here leaves a live
        # camera with an extra stale entry rather than a retired pointer to a
        # node that does not exist.
        try:
            db.set_superseded(moved_from["id"], nid)
        except Exception as exc:
            print(f"[nodes] could not mark {moved_from['id']} superseded: {exc}")
    return rec


def span_of(nd: dict) -> Optional[list]:
    """The node's published watched span, or None if it has none."""
    if nd.get("span_lat1") is None or nd.get("span_lat2") is None:
        return None
    return [[nd["span_lat1"], nd["span_lon1"]],
            [nd["span_lat2"], nd["span_lon2"]]]


def sighting_position(nd: dict, lat: Optional[float] = None,
                      lon: Optional[float] = None,
                      seed: str = "") -> tuple[float, float]:
    """Where a sighting should be PLOTTED. Never the node's true position.

    🚨 THIS EXISTS BECAUSE THE JITTER WAS BEING DEFEATED.
    Node positions are published jittered so the map can show where people are
    recorded without publishing which house is watching. But sightings were
    being stored at the node's TRUE lat/lon, and /api/sightings serves those
    coordinates to anybody. Reading one sighting gave you the exact camera
    location, so the jitter on /api/nodes protected nothing at all.

    It also looked wrong, which is how it was caught: the camera marker and its
    own sightings sat 57 m apart on the map.

    🚨 AND THEN THE FIX FOR THAT WAS ALSO WRONG.
    The first repair pushed a sighting out from the PUBLISHED point along the
    heading by 0.6 x reach. That kept the camera safe and put the dot nowhere
    near the road, because the published point is displaced by up to 60 m while
    his node's reach is 28 m: the privacy noise was more than twice the depth
    of the cone it was being added to. Every dot sat in whatever back garden
    the jitter picked, and they all sat on the SAME invented point.

    The model now is a published WATCHED SPAN - the real stretch of road the
    lens covers, snapped to the road centreline from the camera's true position
    and extended to a minimum length so its midpoint cannot be back-projected
    onto a house. A sighting lands at a stable point along that span. The road
    is published accurately because the road is public; the camera's own point
    stays jittered because the house is not. road.py carries the full argument.
    """
    import math

    span = span_of(nd)

    # 🚨 A FIXED CAMERA USES ITS SPAN, EVEN WHEN THE EVENT CARRIES GPS.
    #
    # The per-event GPS branch below used to run FIRST, unconditionally, so a
    # single sighting that happened to carry a lat/lon jumped off the watched
    # span and was re-placed on whatever road was nearest that point.
    #
    # Measured on his Bridge Street camera: 93 of 95 sightings sat on its North
    # Bridge Street span, and TWO landed 25 m west on HAMRICK STREET - the side
    # road its own house sits near. Reported as "the two on hamrick street
    # should be on bridge street". The camera had not moved; it reported where
    # it IS, and that is precisely the position the span exists to avoid
    # publishing. GPS from a fixed node is worse than useless here: it is the
    # one coordinate the whole span model was built to keep off the map.
    #
    # ⚠️ THE TEST IS `kind`, NOT "does it have a span". A PHONE node also gets a
    # span at enrolment - from wherever it was standing when it enrolled - and
    # keying on the span's existence would pin every dashcam sighting to the
    # street the phone was first switched on in, undoing the fix directly above.
    # What matters is whether the camera MOVES.
    MOBILE_KINDS = {"phone", "mobile", "drive"}
    if span and (nd.get("kind") or "") not in MOBILE_KINDS:
        import road
        return road.point_on_span(span, seed or f"{nd.get('id','')}:{now()}")

    if lat is not None and lon is not None:
        # 🚨 SNAP FROM THE TRUE POINT. JITTERING FIRST PUT DOTS ON THE WRONG
        # STREET, WHICH IS A FALSE CLAIM RATHER THAN A VAGUE ONE.
        #
        # This used to jitter by 60 m and snap the DISPLACED point. Snapping
        # after the jitter was a deliberate choice and it was wrong, for a
        # reason only the map showed: 60 m in a random direction is further
        # than the gap between streets, so near a junction the nearest road to
        # the jittered point is a DIFFERENT road. snap_point then places the
        # dot at a seeded position along an 80 m stretch of that wrong road.
        #
        # Measured on eight of his dashcam sightings in Linden: five sat within
        # 2 m of a road and were still wrong, because they were spread across
        # Hickory Street, East Broad Street and South Main Street when the car
        # had been on one of them. Reported as "there are 8 sightings that
        # aren't on bridge street... they all should be on the road", with a
        # screenshot marking where the car actually was.
        #
        # ⚠️ THE PRIVACY BUDGET IS KEPT, IT JUST MOVES ALONG THE ROAD.
        # snap_point does not return the nearest point; it returns a stable
        # position along an 80 m stretch of the matched road. So the reader
        # still cannot tell WHERE ALONG the street the contributor was, which
        # is the protection that matters for somebody driving past. What they
        # no longer get is a confident dot on a street nobody drove down.
        #
        # "A vehicle passed on this street" is only the more honest shape while
        # it is the RIGHT street. Vague is fine; wrong is not.
        # 🚨 CACHE ONLY. A SIGHTING MUST NEVER WAIT ON A NETWORK LOOKUP.
        #
        # This defaulted to online=True, so any sighting in a cell we had not
        # seen before fired an Overpass query INSIDE the POST, holding an
        # ingest permit while it ran. Overpass refuses this box, so that is a
        # full timeout every time - measured, POST /api/sightings held for 14
        # seconds, and with only 40 ingest permits a publish burst could not
        # drain: 48.3% of the camera fleet's posts were refused with "busy",
        # in clusters of ~700 within a single minute.
        #
        # The lookup has a better home now. tools/road_fill.py resolves cells
        # out of band, from the machine Overpass will actually talk to, so a
        # cache miss here is temporary rather than permanent: this sighting is
        # placed by jitter, the cell is filled within minutes, and every later
        # sighting on that street snaps properly.
        #
        # ⚠️ ENROLMENT IS NOT CHANGED. A person registering a camera is waiting
        # on purpose and wants the road found now; that path still goes online
        # via snap_road. This is only the automated, high-volume one.
        try:
            import road
            snapped = road.snap_point(float(lat), float(lon),
                                      seed or f"{nd.get('id','')}:{now()}",
                                      online=False)
            if snapped:
                return snapped
        except Exception:
            pass          # no cached road: fall through to the jitter below
        # No road matched, or the lookup is down. NOW jitter - an unsnapped
        # true position is the one thing that must never be published, and a
        # dot slightly off the road beats no dot at all.
        return jitter_position(float(lat), float(lon),
                               float(CONFIG.get("node_position_jitter_m", 60)))

    # A MOBILE node that reported no GPS for this sighting. Its span is where it
    # enrolled, which is the best guess available: better a dot on the street it
    # was switched on in than the invented point the fallback below produces.
    # (A fixed node with a span already returned at the top.)
    if span:
        import road
        return road.point_on_span(span, seed or f"{nd['id']}:{now()}")

    # No span: an old node, or one enrolled while the road lookup was
    # unavailable. Fall back to the previous behaviour rather than refusing to
    # place the sighting - a dot in roughly the right place beats no dot.
    base_lat = nd.get("pub_lat") if nd.get("pub_lat") is not None else nd["lat"]
    base_lon = nd.get("pub_lon") if nd.get("pub_lon") is not None else nd["lon"]
    # 🚨 NO HEADING MEANS NO PUSH, NOT A PUSH DUE NORTH.
    #
    # `nd.get("heading") or 0` collapsed "this camera never reported an aim"
    # into "this camera points north", and then moved the dot 0.6 x reach in
    # that invented direction. A browser enrolment reports no heading at all, so
    # for most nodes this was pure fabrication: a real displacement, in a
    # direction nobody supplied, on top of a base already jittered by up to 60 m.
    # Reported as sightings appearing about 80 m north of the camera - which is
    # exactly 0.6 x an 80 m reach plus the jitter.
    #
    # A camera that genuinely points north has heading 0.0 STORED, which is not
    # the same value as None, and it still gets its push. The distinction is the
    # whole fix: guessing a direction adds error without adding information, and
    # the snap below can only work with what it is given.
    hdg_raw = nd.get("heading")
    if hdg_raw is None:
        fb_lat, fb_lon = round(base_lat, 6), round(base_lon, 6)
    else:
        reach = float(nd.get("reach_m") or 40) * 0.6
        hdg = math.radians(float(hdg_raw))
        dlat = (reach * math.cos(hdg)) / 111_320.0
        dlon = (reach * math.sin(hdg)) / (111_320.0 *
                                          max(math.cos(math.radians(base_lat)), 1e-6))
        fb_lat, fb_lon = round(base_lat + dlat, 6), round(base_lon + dlon, 6)

    # 🚨 THIS BRANCH IS WHERE THE OFF-ROAD DOTS ACTUALLY CAME FROM, and it is
    # worse than it looks. A browser enrolment reports NO heading, so `hdg` is
    # 0 and every sighting from the node is pushed the same distance DUE NORTH
    # of the same jittered point - so they do not merely land off the road,
    # they all land on ONE invented point, stacked, forever. On the live map
    # that is a single dot sitting in a park with dozens of passes behind it.
    #
    # road.py already named this exact failure in span_nearest's docstring and
    # fixed it for nodes that HAVE a span. Most nodes do not: they enrolled
    # from a window with no heading, so the span was never computed and this
    # branch runs every time.
    #
    # Snapping here is the safety net. The real repair is giving the node a
    # span (tools/resnap_nodes.py), which also re-places its stored history;
    # this makes the fallback tolerable in the meantime and for any node that
    # enrols while the road lookup is down.
    # Cache only, for the same reason as above: this runs on the ingest path.
    try:
        import road
        snapped = road.snap_point(fb_lat, fb_lon,
                                  seed or f"{nd.get('id','')}:{now()}",
                                  online=False)
        if snapped:
            return snapped
    except Exception:
        pass
    return fb_lat, fb_lon


def in_view(nd: dict, lat: float, lon: float) -> bool:
    """Is a point inside this camera's cone? Range and bearing, no occlusion."""
    from core import angle_diff, bearing_deg, haversine_m
    d = haversine_m(nd["lat"], nd["lon"], lat, lon)
    if d > (nd.get("reach_m") or 45):
        return False
    b = bearing_deg(nd["lat"], nd["lon"], lat, lon)
    return angle_diff(b, nd.get("heading") or 0) <= (nd.get("fov") or 60) / 2.0
