"""Feed SparrowMap from PUBLIC traffic cameras, at home, one process for many.

    python tools\\public_cams.py survey --limit 400      # find the usable ones
    python tools\\public_cams.py enrol  --limit 8        # register them as nodes
    python tools\\public_cams.py run                     # poll them, post crops

The fleet registered by tools/bulk_enrol_cams.py never passes through the state
file those three share - its credentials are in the DATABASE. Two extra steps
bridge that, and they run on different machines on purpose:

    python tools/public_cams.py tokens --out data/cam_tokens.json   # ON THE HUB
    python tools/public_cams.py run --tokens data/cam_tokens.json \\
        --sources fi,ia,atx --interval 300 --workers 96             # ON THE POLLER

🚨 THIS WAS TRIED, MEASURED, AND DELETED IN AUGUST - READ WHY BEFORE CHANGING IT.
Michigan's 806 cameras gave ~15px vehicles against the ~120px the classifier
needs, DOT cameras were found to be standard definition as a category, and
training at low resolution could not rescue them. All the code was removed.

⚠️ "AS A CATEGORY" WAS THE WRONG WORD AND A NATIONWIDE SWEEP DISPROVED IT.
It is per-AGENCY, and the spread is enormous. Measured across whole fleets:

    Ohio          85% at 1280+          Indiana     86%
    Iowa          50%                   Ontario     24%
    New Mexico    25%                   Michigan    11%
    Minnesota      1.5%                 Wisconsin    1.4%
    Arkansas       0% of 554            Louisiana    0%
    Georgia        0% of 7,083          Caltrans     0% of 2,936

So a camera COUNT tells you nothing at all - Georgia publishes 7,083 and not one
of them can see a vehicle, while Ohio's 1,164 are nearly all usable. There is no
substitute for downloading the images and looking at their size, which is what
`probe` is for and why every source here is gated behind it.

The other half is the same as it ever was: this does not "add thousands of
cameras", it finds the ones that can actually see and ignores the rest.

WHERE IT RUNS, AND WHY THAT MATTERS
At HOME, never on the box. The box has no OpenCV and two vCPUs, and putting
continuous inference on the public server is the wrong shape regardless. This is
the same relationship a business camera has: the pixels are handled here, and
only a crop that cannot carry a plate is uploaded.

⚠️ AND THEY ARE LABELLED AS WHAT THEY ARE. SparrowMap's whole argument is that a
volunteer pointed a camera at their own street. A government traffic camera is
not that, and quietly mixing the two would make the project's own description of
itself untrue. Every node enrolled here is named "Public traffic camera - ..."
and carries kind="public_cam" so the map, the review page and anyone reading the
API can tell them apart.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse as up
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                      # noqa: E402
from PIL import Image                   # noqa: E402

STATE = ROOT / "data" / "public_cams.json"
UA_SURVEY = "Mozilla/5.0 (SparrowMap camera survey)"
UA_NODE = "SparrowMap-node/1.0"
VEHICLE = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

CONF = 0.45              # matches the live node
SEND_EDGE = 200          # the plate-illegible cap, same as detect/relay
MIN_VEHICLE_PX = 120     # the bar: below this the head is guessing
POLL_S = 20.0            # per camera; DOT snapshots refresh slower than this
BEAT_BATCH = 1000        # must not exceed hub.BULK_BEAT_MAX
# A 1920x1080 JPEG from these networks is 150-400 KB. Ten megabytes is not a
# traffic camera, it is a mistake or a trap, and either way it is not worth a
# worker slot on a sweep of thousands.
MAX_IMAGE_BYTES = 10 << 20
POLL_TIMEOUT_S = 8       # per camera during `run` - see fetch()'s deadline
# How many cameras must serve the byte-identical image before it is judged a
# placeholder rather than a coincidence. See the sweep in cmd_probe.
PLACEHOLDER_MIN = 5
MIN_HD_WIDTH = 1280


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------
def nyc_index() -> list:
    """NYC DOT's public camera list. No key, publishes lat/lon and an online flag."""
    req = urllib.request.Request("https://webcams.nyctmc.org/api/cameras/",
                                 headers={"User-Agent": UA_SURVEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        idx = json.loads(r.read())
    out = []
    for c in idx:
        if str(c.get("isOnline")).lower() != "true":
            continue
        if c.get("latitude") in (None, 0) or c.get("longitude") in (None, 0):
            continue
        out.append({"src": "nyc", "ref": c["id"], "name": c.get("name") or c["id"],
                    "lat": float(c["latitude"]), "lon": float(c["longitude"]),
                    "url": f"https://webcams.nyctmc.org/api/cameras/{c['id']}/image"})
    return out


def _get_json(url: str, timeout: int = 60):
    return json.loads(fetch(url, timeout=timeout))


def finland_index() -> list:
    """Fintraffic weathercams. CC BY 4.0, no key.

    🚨 THE ONE NETWORK THAT PUBLISHES ITS OWN RESOLUTIONS. Every other feed has
    to be probed image by image to find out which cameras clear the 120px-of-
    vehicle bar - thousands of downloads against somebody else's free service,
    just to ask a question they could have answered. Fintraffic states it per
    preset, so the HD subset is selected from metadata alone.

    ⚠️ The resolution is in the per-station DETAIL, not the index, so this costs
    809 small metadata requests once. That is still an order of magnitude
    cheaper than fetching 2,272 images to measure them, and it happens at
    survey time rather than on every poll.
    """
    idx = _get_json("https://tie.digitraffic.fi/api/weathercam/v1/stations")
    ids = [(f.get("properties") or {}).get("id")
           for f in (idx.get("features") or [])]
    ids = [i for i in ids if i]

    def one(sid):
        try:
            return _get_json(
                f"https://tie.digitraffic.fi/api/weathercam/v1/stations/{sid}",
                timeout=30)
        except Exception:
            return None

    out = []
    with cf.ThreadPoolExecutor(max_workers=24) as pool:
        for d in pool.map(one, ids):
            if not d:
                continue
            pr = d.get("properties") or {}
            geo = (d.get("geometry") or {}).get("coordinates") or []
            if len(geo) < 2:
                continue
            lon, lat = float(geo[0]), float(geo[1])
            for pre in (pr.get("presets") or []):
                if not pre.get("inCollection"):
                    continue
                try:
                    w = int(str(pre.get("resolution") or "0").split("x")[0])
                except Exception:
                    w = 0
                if w < MIN_HD_WIDTH:
                    continue
                url = pre.get("imageUrl")
                pid = pre.get("id")
                if not url or not pid:
                    continue
                out.append({"src": "fi", "ref": pid,
                            "name": (pre.get("presentationName")
                                     or pr.get("name") or pid)[:60],
                            "lat": lat, "lon": lon, "url": url})
    return out


def austin_index() -> list:
    """City of Austin, explicitly PUBLIC_DOMAIN in its own metadata."""
    rows = _get_json("https://data.austintexas.gov/resource/b4k4-adkb.json?$limit=2000")
    out = []
    for c in rows:
        loc = c.get("location") or {}
        coords = (loc.get("coordinates") or []) if isinstance(loc, dict) else []
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except Exception:
            continue
        url = c.get("screenshot_address") or c.get("image_url")
        if not url:
            continue
        out.append({"src": "atx", "ref": str(c.get("camera_id") or c.get("atd_camera_id") or url)[-24:],
                    "name": (c.get("location_name") or "Austin camera")[:60],
                    "lat": lat, "lon": lon, "url": url})
    return out


def iowa_index() -> list:
    """Iowa DOT via the ArcGIS data portal.

    ⚠️ THE INDEX PATH DECIDES THE LICENCE, NOT THE IMAGE URL. The identical
    JPEG is forbidden if reached by scraping 511ia.org - whose terms ban
    automated retrieval - and CC BY 4.0 when reached through this layer. Same
    bytes, different permission. Never "optimise" this to the 511 site.
    """
    url = ("https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/"
           "Traffic_Cameras_View/FeatureServer/0/query"
           "?where=1%3D1&outFields=*&f=geojson&resultRecordCount=2000")
    idx = _get_json(url)
    out = []
    for f in (idx.get("features") or []):
        pr = f.get("properties") or {}
        geo = (f.get("geometry") or {}).get("coordinates") or []
        if len(geo) < 2:
            continue
        # ⚠️ THE FIELD NAMES ARE CASE-SENSITIVE AND NOT WHAT YOU WOULD GUESS.
        # This asked for IMAGEURL/DESCRIPTION and got zero cameras from a
        # feed that was returning 1,251 of them - a silent empty result, which
        # is the failure mode this project keeps having to design against.
        # The real names, read from the response: ImageURL, Desc_, device_id.
        img = pr.get("ImageURL") or pr.get("IMAGEURL") or pr.get("imageUrl")
        if not img:
            continue
        out.append({"src": "ia", "ref": str(pr.get("device_id") or img)[-24:],
                    "name": (pr.get("Desc_") or pr.get("Route")
                             or "Iowa camera")[:60],
                    "lat": float(geo[1]), "lon": float(geo[0]), "url": img})
    return out


def ontario_index() -> list:
    """Ontario 511 (MTO), under the Open Government Licence – Ontario.

    🚨 THIS SOURCE MUST BE PROBED, AND REFUSES TO BE GUESSED AT.

    Ontario publishes 948 stations carrying 1,670 enabled views and says
    NOTHING about their resolution, and measuring found the network split
    almost three ways: 1920x1080 and 1280x720 alongside 800x450, 752x480 and
    704x480. So roughly a third can see a vehicle at the 120px bar and the rest
    cannot, and there is no way to tell which from the metadata.

    ⚠️ Its neighbour on the SAME 511 software is the argument for measuring
    every time: Alberta 511 publishes 599 views and every single one sampled
    was 840x630 or smaller. Same vendor, same API shape, opposite answer. The
    platform tells you nothing about the optics.

    So this reads `data/cam_probe.json` and returns only what was MEASURED past
    the bar. No cache, no cameras - deliberately. Enrolling 1,670 to find 500
    would put a thousand nodes on the map that can never produce a sighting,
    and duplicate-and-useless nodes are the hardest thing here to take back.
    """
    probe = load_probe()
    d = _get_json("https://511on.ca/api/v2/get/cameras")
    out, unprobed = [], 0
    for st in d:
        lat, lon = st.get("Latitude"), st.get("Longitude")
        if lat in (None, 0) or lon in (None, 0):
            continue
        for v in (st.get("Views") or []):
            if v.get("Status") != "Enabled" or not v.get("Url"):
                continue
            w = probe.get(v["Url"])
            if w is None:
                unprobed += 1
                continue
            if w < MIN_HD_WIDTH:
                continue
            desc = (v.get("Description") or "").strip()
            name = (st.get("Location") or st.get("Roadway") or "Ontario camera")
            if desc:
                name = f"{name} ({desc})"
            out.append({"src": "on", "ref": str(v["Id"]),
                        "name": name[:60], "lat": float(lat), "lon": float(lon),
                        "url": v["Url"]})
    if unprobed:
        print(f"  ⚠ {unprobed} Ontario view(s) never measured - run "
              f"`public_cams.py probe --source on` or they stay out")
    if not out and unprobed:
        raise RuntimeError("no Ontario cameras are measured yet; run `probe` first")
    return out


def ohio_index(measured_only: bool = True) -> list:
    """Ohio DOT (OHGO). Keyless, and it overturns this file's own headline.

    🚨 "DOT CAMERAS ARE STANDARD DEFINITION AS A CATEGORY" IS NO LONGER TRUE,
    AND THIS IS THE SOURCE THAT BREAKS IT. That conclusion was reached in
    August against Michigan's 806 cameras and it was fair there - 11% clear the
    bar. Measured across Ohio's full fleet: 982 of 1,151 are 1280 or wider,
    85%, most of them 1920 or better. It is not a category, it is a per-agency
    procurement decision, and the only way to know is to measure.

    ⚠️ AND 319 OF THE HD ONES ARE FOUR CAMERAS IN A TRENCHCOAT.
    The 2560x1920, 2592 and 2688 frames are 2x2 QUAD MOSAICS: four unrelated
    views tiled into one image. They clear the width bar and they are the most
    dangerous thing in this whole survey - a vehicle detected in the bottom
    right tile would be published at the camera's coordinates, which is a
    different junction entirely. This map's one job is not to say a patrol car
    was somewhere it was not.

    Splitting them needs a per-tile location the API does not publish, so they
    are EXCLUDED. That leaves 663 single-frame HD cameras, and excluding is the
    only honest option until someone can say where each tile points.
    """
    rows = _get_json("https://api.ohgo.com/cameras", timeout=60)
    out = []
    for r in rows:
        lat, lon = r.get("Latitude"), r.get("Longitude")
        if not lat or not lon:
            continue
        for i, cam in enumerate(r.get("Cameras") or []):
            url = cam.get("LargeURL") or cam.get("SmallURL")
            if not url:
                continue
            name = (r.get("Location") or r.get("Description") or "Ohio camera")
            d = (cam.get("Direction") or "").strip()
            if d and d.lower() != "view":
                name = f"{name} ({d})"
            out.append({"src": "oh", "ref": f"{r['Id']}-{i}", "name": name[:60],
                        "lat": float(lat), "lon": float(lon), "url": url})
    return probe_filter(out, "oh") if measured_only else out


def alabama_index(measured_only: bool = True) -> list:
    """ALGO Traffic. The best-optics network measured anywhere - 89% clear the bar.

    ⚠️ NOT the ArcGIS layer that turns up first in a search. That one is titled
    "Old AL DOT roadway cameras" and every image id 404s; it is a stale
    dataset and cost an afternoon. `api.algotraffic.com/v4.0/Cameras` is the
    live system and is fully keyless.

    ⚠️ 14 of its widest images are QUAD MOSAICS of four fisheye views, and ten
    more at 3072px are a "No camera preview available" card. Both are caught by
    SRC_MAX_WIDTH and the placeholder sweep rather than trusted.
    """
    rows = _get_json("https://api.algotraffic.com/v4.0/Cameras", timeout=60)
    out = []
    for c in rows:
        loc = c.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        url = c.get("snapshotImageUrl")
        if not url or not lat or not lon:
            continue
        name = " ".join(x for x in (loc.get("displayRouteDesignator"),
                                    loc.get("displayCrossStreet"),
                                    loc.get("city")) if x)
        out.append({"src": "al", "ref": str(c.get("id")),
                    "name": (name or "Alabama camera")[:60],
                    "lat": float(lat), "lon": float(lon), "url": url})
    return probe_filter(out, "al") if measured_only else out


def _castlerock_index(src: str, host: str) -> list:
    """Castle Rock 511 sites, through their own map XHR rather than the API.

    🚨 THE SITE ID IS NOT THE IMAGE ID, AND CONFUSING THEM COSTS TWO THIRDS OF
    THE FLEET. `/map/mapIcons/Cameras` returns SITE ids; asking for
    `/map/Cctv/<siteId>` returns a 540x330 "camera unavailable" card for most
    of them - 258 of 404 across New England. The real image ids live in
    `images[].id` from the List/GetData POST. Fixing this took New England
    from 68 usable to 172.

    ⚠️ These same hosts answer `/api/v2/get/cameras` with "Invalid Key". The
    keyless door is a different door - see newyork_index for the same lesson.
    """
    # ⚠️ TRUST NEW IDS, NOT THE PAGE COUNTER. This endpoint happily accepts a
    # `start` it then ignores, so a loop that advances until rows run out
    # returns the SAME hundred cameras for ever - measured, 20,004 "cameras"
    # from a 404-camera network. Stopping when a page adds nothing new is the
    # only condition the server cannot lie about.
    return cached_index(src, lambda: _castlerock_fetch(src, host))


def _castlerock_fetch(src: str, host: str) -> list:
    """The live pull. Wrapped by cached_index because several of these hosts
    403 a European address while serving a US one - Utah does, exactly like
    Michigan - and enrolment has to run on the hub, which is in Helsinki."""
    out, start, seen = [], 0, set()
    while True:
        body = (f"start={start}&length=100&draw=1&query=&lang=en").encode()
        req = urllib.request.Request(
            f"https://{host}/List/GetData/Cameras", data=body, method="POST",
            headers={"User-Agent": UA_SURVEY,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        d = json.loads(raw)
        rows = d.get("data") or []
        if not rows:
            break
        fresh = 0
        for c in rows:
            wkt = (((c.get("latLng") or {}).get("geography") or {})
                   .get("wellKnownText") or "")
            m = re.search(r"POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)", wkt)
            if not m:
                continue
            lon, lat = float(m.group(1)), float(m.group(2))
            for im in (c.get("images") or []):
                iid = im.get("id") or im.get("imageId")
                if not iid or str(iid) in seen:
                    continue
                seen.add(str(iid))
                fresh += 1
                out.append({"src": src, "ref": str(iid),
                            "name": (c.get("title") or c.get("name")
                                     or f"{src.upper()} camera")[:60],
                            "lat": lat, "lon": lon,
                            "url": f"https://{host}/map/Cctv/{iid}"})
        if not fresh:
            break                    # the page repeated: see the note above
        start += len(rows)
        if start > 20000:
            break
    return out


def newengland_index(measured_only: bool = True) -> list:
    """Vermont, Maine and New Hampshire share one 511 deployment."""
    out = _castlerock_index("ne_511", "www.newengland511.org")
    return probe_filter(out, "ne_511") if measured_only else out


def northcarolina_index(measured_only: bool = True) -> list:
    """NCDOT. 660 of 1,121 clear the bar."""
    d = _get_json("https://www.drivenc.gov/map/mapIcons/Cameras", timeout=60)
    out = []
    for c in (d.get("item2") or []):
        loc = c.get("location") or []
        iid = c.get("itemId")
        if len(loc) < 2 or not iid:
            continue
        out.append({"src": "nc", "ref": str(iid),
                    "name": (c.get("title") or "North Carolina camera")[:60]
                            or f"NC camera {iid}",
                    "lat": float(loc[0]), "lon": float(loc[1]),
                    "url": f"https://www.drivenc.gov/map/Cctv/{iid}"})
    return probe_filter(out, "nc") if measured_only else out


def michigan_index(measured_only: bool = True) -> list:
    """MDOT Mi Drive.

    ⚠️ ONLY REACHABLE FROM THE US BOX. mdotjboss.state.mi.us answers a European
    address with 403 while serving a US one normally - and confusingly its
    IMAGE host (micamerasimages.net) is happy to serve Helsinki. Index blocked,
    pictures not, which is why this looked like a dead source rather than a
    geographic one.

    ⚠️ AND THE THUMBNAIL IS THE DEFAULT. The list gives `/thumbs/` URLs; strip
    that path segment for the full-resolution frame. Left alone, Michigan
    measures as a 320px network and gets written off - the thumbnails ARE 320px.

    ⚠️ AND IT TAKES TWO ENDPOINTS. `AllForMap` has clean latitude/longitude/id/
    title and NO image at all; `list` has the image and buries its coordinates
    in an HTML anchor. Neither alone is a camera. They are joined on the id,
    which in `list` exists only inside the image blob as id="1129Img".
    """
    def _build():
        coords = _get_json(
            "https://mdotjboss.state.mi.us/MiDrive/camera/AllForMap/", timeout=60)
        rows = _get_json("https://mdotjboss.state.mi.us/MiDrive/camera/list",
                         timeout=60)
        return {"coords": coords, "rows": rows}

    both = cached_index("mi", _build)
    coords, rows = both.get("coords") or [], both.get("rows") or []
    by_id = {}
    for c in (coords if isinstance(coords, list) else []):
        if c.get("id") is not None and c.get("latitude") and c.get("longitude"):
            by_id[int(c["id"])] = c
    out = []
    for c in (rows if isinstance(rows, list) else []):
        blob = c.get("image") or ""
        m = re.search(r'src="([^"?]+)', blob)
        i = re.search(r'id="(\d+)Img"', blob)
        if not m or not i:
            continue
        meta = by_id.get(int(i.group(1)))
        if not meta:
            continue
        url = m.group(1).replace("/thumbs/", "/")   # see the note above
        name = (meta.get("title")
                or f"{c.get('route', '')} {c.get('location', '')}").strip()
        out.append({"src": "mi", "ref": i.group(1),
                    "name": (name or "Michigan camera")[:60],
                    "lat": float(meta["latitude"]), "lon": float(meta["longitude"]),
                    "url": url})
    return probe_filter(out, "mi") if measured_only else out


# 🚨 ONE PLATFORM, EIGHT STATES, AND IT IS A TEMPLATE RATHER THAN A COINCIDENCE.
#
# `https://{ss}tg.carsprogram.org/cameras_v1/api/cameras` is keyless and returns
# a whole state fleet in ONE request with no paging. Probing all fifty
# two-letter codes found CO, IN, IA, KS, MA, MN, NE and SD answering it - so
# five of the states below cost one adapter between them rather than five.
#
# ⚠️ Their own 511 sites answer /api/v2/get/cameras with "Invalid Key". The
# keyless door is a different door, which is now the third time that has been
# true (see newyork_index and _castlerock_index).
#
# ⚠️ MASSACHUSETTS AND IOWA ARE DELIBERATELY ABSENT. Massachusetts measured 0 of
# 308 - it caps at 352px and its `-fullJpeg.jpg` really is 352x198 - and Iowa
# already comes through the ArcGIS layer, which is a better index for it.
CARS = {
    "in": ("intg", "Indiana"),
    "ne": ("netg", "Nebraska"),
    "ks": ("kstg", "Kansas"),
    "mn": ("mntg", "Minnesota"),
    "co": ("cotg", "Colorado"),
}


def cars_index(state: str, measured_only: bool = True) -> list:
    """Any state on the CARS platform.

    ⚠️ ORDERED SAMPLING LIES ABOUT THESE FEEDS, STRUCTURALLY. Minnesota is two
    populations - 720px metro cameras and 1920px outstate ones - and the index
    lists metro first, so a sample taken in order hits nothing but 720 and
    hides all 557 usable cameras. That is the Virginia and Florida lesson for
    the third time: `probe` walks the whole fleet, never a sample.
    """
    host, label = CARS[state]
    rows = _get_json(f"https://{host}.carsprogram.org/cameras_v1/api/cameras",
                     timeout=60)
    out = []
    for c in rows:
        loc = c.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat in (None, 0) or lon in (None, 0):
            continue
        for i, v in enumerate(c.get("views") or []):
            url = v.get("videoPreviewUrl") or v.get("url")
            if not url or not str(url).lower().startswith("http"):
                continue
            # 🚨 THE REF COMES FROM THE URL, NOT THE ID, BECAUSE THE ID MOVES.
            #
            # MnDOT's `id` is an internal key it REGENERATES: the hub enrolled
            # 580 cameras with ids in the 493xxx range and hours later the same
            # feed served 496xxx for the same fleet. Every node name built on
            # those ids became unmatchable - 0 of 580 reached the poller, while
            # Indiana, Nebraska, Kansas and Colorado matched 100% because their
            # ids happened to hold still.
            #
            # "Happened to hold still" is not an identifier. The image URL is
            # what actually names a camera feed, and it was stable across every
            # fetch and both machines: 1,940 of 1,940 in common.
            out.append({"src": state, "ref": _url_ref(url),
                        "name": (v.get("name") or c.get("name")
                                 or f"{label} camera")[:60],
                        "lat": float(lat), "lon": float(lon), "url": url})
    return probe_filter(out, state) if measured_only else out


def illinois_index(measured_only: bool = True) -> list:
    """Travel Midwest (LMIGA) - the largest single haul in the whole survey.

    ⚠️ POST ONLY. A GET returns 405, which reads as a dead endpoint on a site
    that otherwise looks like an abandoned SPA. The body is an empty JSON
    object and that is all it wants.

    ⚠️ One camera carries SEVERAL image URLs in `remUrls`, so the count of
    cameras and the count of pictures are different numbers - 1,945 sites and
    4,331 URLs.
    """
    body = json.dumps({}).encode()
    req = urllib.request.Request(
        "https://travelmidwest.com/lmiga/cameraMap.json", data=body,
        method="POST", headers={"User-Agent": UA_SURVEY,
                                "Content-Type": "application/json",
                                "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    d = json.loads(raw)
    out = []
    for f in (d.get("features") or []):
        pr = f.get("properties") or {}
        geo = (f.get("geometry") or {}).get("coordinates") or []
        if len(geo) < 2:
            continue
        for i, url in enumerate(pr.get("remUrls") or []):
            if not url or not str(url).lower().startswith("http"):
                continue
            out.append({"src": "il", "ref": f"{pr.get('id') or pr.get('name')}-{i}",
                        "name": (pr.get("name") or pr.get("desc")
                                 or "Illinois camera")[:60],
                        "lat": float(geo[1]), "lon": float(geo[0]),
                        "url": url})
    return probe_filter(out, "il") if measured_only else out


def southdakota_index(measured_only: bool = True) -> list:
    """South Dakota, on Iteris. The best OPTICS anywhere - 21 are true 4K.

    ⚠️ 3840px is also exactly the width a quad mosaic arrives at, so these were
    opened and looked at before being believed. They are single views.
    """
    d = _get_json("https://sd.cdn.iteris-atis.com/geojson/icons/metadata/"
                  "icons.cameras.geojson", timeout=60)
    out = []
    for f in (d.get("features") or []):
        pr = f.get("properties") or {}
        geo = (f.get("geometry") or {}).get("coordinates") or []
        if len(geo) < 2:
            continue
        for i, cam in enumerate(pr.get("cameras") or []):
            url = cam.get("image")
            if not url:
                continue
            out.append({"src": "sd", "ref": f"{pr.get('id') or cam.get('id')}-{i}",
                        "name": (cam.get("name") or pr.get("name")
                                 or "South Dakota camera")[:60],
                        "lat": float(geo[1]), "lon": float(geo[0]),
                        "url": url})
    return probe_filter(out, "sd") if measured_only else out


def arizona_index(measured_only: bool = True) -> list:
    """AZ511, through the legacy Castle Rock list rather than the keyed API."""
    out = _castlerock_index("az", "www.az511.gov")
    return probe_filter(out, "az") if measured_only else out


def utah_index(measured_only: bool = True) -> list:
    """UDOT. The largest single state in the survey - 1,720 clear the bar.

    ⚠️ UDOT SERVES ITS OFFLINE CARD AT 1280x720, so it passes the width test
    while being a dead camera. A width histogram alone overcounts Utah by 43.
    The content hash catches it; do not remove that check to "simplify" this.
    """
    out = _castlerock_index("ut", "www.udottraffic.utah.gov")
    return probe_filter(out, "ut") if measured_only else out


def idaho_index(measured_only: bool = True) -> list:
    """Idaho 511. Mostly 800px; 53 real HD cameras is a small but real haul."""
    out = _castlerock_index("id", "511.idaho.gov")
    return probe_filter(out, "id") if measured_only else out


def alaska_index(measured_only: bool = True) -> list:
    """AKDOT road weather cameras.

    🚨 NOT THE 511 LAYER, WHICH IS A ZOMBIE. Alaska's ArcGIS `511_Cameras`
    still answers with 457 features and 431 of them are ONE byte-identical
    "no live feed" card with LastUpdated fields from 2024. The live 511 door
    caps every image at 1000px, so it reads as a network that just misses the
    bar - which is a different and wrong conclusion.

    The real cameras are on a different system. Measured on the same camera in
    the same minute: 511 gave 1000x563, roadweather gave 1280x720. That is a
    genuine downsampling proxy, unlike Arizona and Idaho where the equivalent
    path passes through untouched.

    🚨 NOT WIRED, AND THE REASON IS ARCHITECTURAL RATHER THAN A BUG.
    Alaska's image URLs carry a TIMESTAMP - Vid-000351001-00-00-2026-08-16-
    03-09.jpg - so they rotate every few minutes. This whole pipeline assumes a
    camera has a durable URL, and three separate things break without one:
      * the ref is derived from the URL, so a node's NAME would change every
        cycle and orphan itself (see cars_index for what that costs);
      * the probe cache is keyed on the URL, so nothing would ever be measured
        twice and every sweep would re-measure the fleet;
      * conditional requests are keyed on the URL, so every fetch downloads.

    Wyoming and Montana have the same property. Together that is ~1,081
    measured-HD cameras waiting on ONE change: identify a camera by a stable
    key (site + view) and RESOLVE its URL from the index each cycle. That is
    worth doing and is not a small edit, so it is written down rather than
    half-built.

    ⚠️ Covers AKDOT's own RWIS sites only (79). Roughly 46 other-agency sites
    exist nowhere but behind the 1000px cap, so they are simply out of reach.
    """
    sites = _get_json("https://roadweather.alaska.gov/api/sites", timeout=60)
    rows = sites if isinstance(sites, list) else (sites.get("sites") or [])
    out = []
    for s in rows:
        lat, lon = s.get("latitude"), s.get("longitude")
        sid = s.get("id") or s.get("siteId")
        if not sid or not lat or not lon:
            continue
        try:
            imgs = _get_json(
                f"https://roadweather.alaska.gov/api/images/{sid}", timeout=30)
        except Exception:
            continue
        for i, im in enumerate(imgs if isinstance(imgs, list) else []):
            url = im.get("downloadUrl")
            if not url:
                continue
            out.append({"src": "ak", "ref": _url_ref(url),
                        "name": (s.get("name") or im.get("name")
                                 or "Alaska camera")[:60],
                        "lat": float(lat), "lon": float(lon), "url": url})
    return probe_filter(out, "ak") if measured_only else out


def indiana_index(measured_only: bool = True) -> list:
    """INDOT, via the CARS platform. Only reachable from the US box.

    ⚠️ THE FULL-SIZE MIRROR DOES NOT COVER THIS INDEX. pws.trafficwise.org
    serves 738 full-resolution JPEGs and looks like the obvious better source -
    86% of THOSE clear the bar against 15% of the index's own previews. But the
    two barely overlap: the files are named by route (01-002-073, 01-006-003)
    and the index's cameras are on routes with no files at all - there is not a
    single 01-094-* image for any of its I-94 cameras. Measured: 33 of 740 join.
    The previews, probed, yield about a hundred. Fewer good pictures beats more
    good pictures of somewhere else.

    ⚠️ So this takes videoPreviewUrl and lets `probe` find the HD subset, which
    is the same bargain every other mixed network here makes.
    """
    rows = _get_json("https://intg.carsprogram.org/cameras_v1/api/cameras",
                     timeout=60)
    out = []
    for c in rows:
        loc = c.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat in (None, 0) or lon in (None, 0):
            continue
        for i, v in enumerate(c.get("views") or []):
            url = v.get("videoPreviewUrl")
            if not url or not url.lower().startswith("http"):
                continue
            out.append({"src": "in", "ref": f"{c.get('id')}-{i}",
                        "name": (v.get("name") or c.get("name")
                                 or "Indiana camera")[:60],
                        "lat": float(lat), "lon": float(lon), "url": url})
    return probe_filter(out, "in") if measured_only else out


def newyork_index(measured_only: bool = True) -> list:
    """511NY (NYSDOT).

    ⚠️ THE KEYLESS DOOR IS A DIFFERENT DOOR. `/api/v2/get/cameras` - the Castle
    Rock path every other 511 site answers - returns "Invalid Key" here, and
    that is what got New York written down as key-gated on the first sweep. The
    working endpoint is `/api/getcameras?format=json&key=` with an EMPTY key.
    One state, two APIs, opposite answers; test the state's own before
    concluding a key is needed.

    ⚠️ `Disabled` is not decoration - 1,064 of 2,931 rows are disabled cameras
    that still carry a URL.
    """
    rows = _get_json("https://511ny.org/api/getcameras?format=json&key=",
                     timeout=60)
    out = []
    for c in rows:
        if c.get("Disabled") or c.get("Blocked"):
            continue
        lat, lon, url = c.get("Latitude"), c.get("Longitude"), c.get("Url")
        if not url or not lat or not lon:
            continue
        out.append({"src": "ny", "ref": str(c.get("ID") or url)[-24:],
                    "name": (c.get("Name") or c.get("RoadwayName")
                             or "New York camera")[:60],
                    "lat": float(lat), "lon": float(lon), "url": url})
    return probe_filter(out, "ny") if measured_only else out


def newmexico_index(measured_only: bool = True) -> list:
    """NMRoads.

    ⚠️ TAKE `snapshotFile` AND NEVER THE SITE'S OWN IMAGE PROXY. NMRoads offers
    a `GetCameraImage` endpoint that re-encodes EVERYTHING to 360x240. Routing
    through the obvious, official-looking URL would silently destroy the entire
    source - every one of its 42 HD cameras would measure as unusable and the
    network would be written off as standard definition. The direct file is
    1920 or 1280.
    """
    rows = _get_json("https://servicev5.nmroads.com/RealMapWAR/GetCameraInfo",
                     timeout=60)
    # ⚠️ The list is under `cameraInfo`. Guessing `cameras` returns an empty
    # list from a 183-camera feed and looks like a network that went away.
    if isinstance(rows, dict):
        rows = rows.get("cameraInfo") or []
    out = []
    for c in rows:
        url, lat, lon = c.get("snapshotFile"), c.get("lat"), c.get("lon")
        if not url or not lat or not lon:
            continue
        ref = str(c.get("name") or url)[-24:]
        out.append({"src": "nm", "ref": ref,
                    "name": (c.get("title") or c.get("name")
                             or "New Mexico camera")[:60],
                    "lat": float(lat), "lon": float(lon), "url": url})
    return probe_filter(out, "nm") if measured_only else out


def missouri_index(measured_only: bool = True) -> list:
    """MoDOT. Thirteen cameras, nine of them HD - small, and it is still a state."""
    d = _get_json("https://traveler.modot.org/map/js/snapshot.json", timeout=45)
    rows = d if isinstance(d, list) else (d.get("cameras") or [])
    out = []
    for c in rows:
        loc = c.get("location") or {}
        lat, lon = loc.get("y"), loc.get("x")
        url = c.get("url")
        if not url or lat is None or lon is None:
            continue
        if url.startswith("/"):
            url = "https://traveler.modot.org" + url
        out.append({"src": "mo", "ref": str(c.get("id") or url)[-24:],
                    "name": (c.get("caption") or "Missouri camera")[:60],
                    "lat": float(lat), "lon": float(lon), "url": url})
    return probe_filter(out, "mo") if measured_only else out


# --------------------------------------------------------------------------
# the generic ArcGIS source: ONE adapter, many agencies
# --------------------------------------------------------------------------
# 🚨 THE REASON THIS EXISTS. Dozens of transport agencies publish their camera
# list as an ArcGIS FeatureServer - the same shape Iowa already reaches us
# through. Written one at a time that is forty scrapers to maintain; written
# once it is a table of four strings per agency, and adding a state is a data
# change rather than a code change.
#
# ⚠️ WHAT IS NOT SHARED IS THE FIELD NAMES, and there is no convention
# whatsoever. The image URL is `ImageURL` in Iowa, `URL` in Seattle, `snapshot`
# in Kentucky, `Image_URL` in Alabama, `IMAGEURL` in Toronto, `feed_url` in
# Utah, `IMAGE_` in Kirkland. Guessing one silently returns zero cameras from a
# feed with hundreds - which this project has already been bitten by once, on
# Iowa's own `ImageURL` vs `IMAGEURL`.
#
# ⚠️ EVERY ONE OF THESE WAS MEASURED BEFORE IT WAS ADDED. The counts below are
# what the agency OFFERS; the HD subset is what survives `probe`, and for most
# of these networks that is a minority. Never add a row here from a camera
# count alone - Georgia offers 7,083 and every one measured 450-540px.
ARCGIS = {
    "sea": {"name": "Seattle DOT",
            "url": "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest"
                   "/services/Traffic_Cameras_CDL/FeatureServer/0",
            "img": "URL", "label": ("NAME", "LOCATION"), "ref": "COMPKEY"},
    "wsd": {"name": "WSDOT storm-response cameras",
            "url": "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest"
                   "/services/StormResponse_TrafficCameras/FeatureServer/0",
            "img": "URL", "label": ("NAME", "LOCATION"), "ref": "COMPKEY"},
    "kytc": {"name": "Kentucky TC",
             "url": "https://services2.arcgis.com/CcI36Pduqd0OR4W9/arcgis/rest"
                    "/services/trafficCamerasCur_Prd/FeatureServer/0",
             "img": "snapshot", "label": ("name", "description"), "ref": "id"},
    "aldot": {"name": "Alabama DOT",
              "url": "https://services7.arcgis.com/33Tmvrm3G2UZLFK9/arcgis/rest"
                     "/services/AL_DOT_roadway_cameras/FeatureServer/0",
              "img": "Image_URL", "label": ("Camera_Name", "Primary_Road"),
              "ref": "Device_ID"},
    "tor": {"name": "City of Toronto",
            "url": "https://services1.arcgis.com/DwLTn0u9VBSZvUPe/arcgis/rest"
                   "/services/Traffic_Camera_List/FeatureServer/0",
            "img": "IMAGEURL", "label": ("MAINROAD", "CROSSROAD"),
            "ref": "REC_ID"},
    "kc": {"name": "King County WA",
           "url": "https://services.arcgis.com/Ej0PsM5Aw677QF1W/arcgis/rest"
                  "/services/TRAFFICCAMERA_POINT_2029/FeatureServer/0",
           "img": "ImageURL", "label": ("Location", "Description"),
           "ref": "AssetID"},
    "nl": {"name": "Newfoundland and Labrador",
           "url": "https://services8.arcgis.com/aCyQID5qQcyrJMm2/arcgis/rest"
                  "/services/TI_HighwayCamera/FeatureServer/0",
           "img": "url", "label": ("location",), "ref": "objectid"},
    # 🚨 THE FOUR BELOW WERE REJECTED ON A 12-IMAGE SAMPLE AND ARE HERE TO BE
    # MEASURED PROPERLY. A sample lies: Virginia looked uniformly 320x240 at
    # n=25 and a full 1,648-camera scan found 87 units at 1920x1080. These are
    # the largest networks in the survey, so even a 2% HD tail is hundreds of
    # cameras - which is more than most states contribute in total. `probe`
    # walks all of them; the ones that stay empty stay out, on evidence rather
    # than on a sample.
    "ga": {"name": "Georgia DOT 511",
           "url": "https://services1.arcgis.com/2iUE8l8JKrP2tygQ/arcgis/rest"
                  "/services/GDOT_511_Traffic_Cameras_Updated/FeatureServer/0",
           "img": "Url", "label": ("Description", "Roadway"), "ref": "Id"},
    "fl": {"name": "Florida 511",
           "url": "https://services.arcgis.com/3wFbqsFPLeKqOlIK/arcgis/rest"
                  "/services/FL511_Traffic_Cameras/FeatureServer/0",
           "img": "IMAGE", "label": ("DESCRIPT", "HIGHWAY"), "ref": "ID"},
    "ca": {"name": "Caltrans",
           "url": "https://gisdata.dot.ca.gov/arcgis/rest/services/CHhighway"
                  "/CCTV/FeatureServer/0",
           "img": "currentImageURL", "label": ("locationName", "nearbyPlace"),
           "ref": "index_"},
    "or": {"name": "Oregon TripCheck / WSDOT travel info",
           "url": "https://data.wsdot.wa.gov/arcgis/rest/services"
                  "/TravelInformation/TravelInfoCamerasWeather/FeatureServer/0",
           "img": "ImageURL", "label": ("CameraTitle",), "ref": "OBJECTID"},
    "kirk": {"name": "Kirkland WA",
             "url": "https://services2.arcgis.com/loGMwowmR0OPlOQb/arcgis/rest"
                    "/services/TRN_TrafficCams_10032022/FeatureServer/0",
             "img": "IMAGE_", "label": ("LOCATION", "DESCRIPTIO"),
             "ref": "TRAFCAM_ID"},
}


def arcgis_index(key: str, measured_only: bool = True) -> list:
    """Every camera in one ArcGIS FeatureServer, paged.

    ⚠️ `measured_only` is the same rule Ontario follows and for the same
    reason: none of these agencies publish resolution, most of their cameras
    are below the bar, and enrolling the whole list to find the usable third
    would put thousands of nodes on the map that can never produce a sighting.
    Only `probe` passes False, because it is the thing that does the measuring.

    ⚠️ PAGED, BECAUSE THESE SERVERS LIE BY OMISSION. A FeatureServer caps what
    one query returns (commonly 1,000 or 2,000) and says so only in
    `exceededTransferLimit` - which is easy not to read. A single unpaged query
    against a 5,000-camera layer returns 2,000 and looks completely successful.
    """
    cfg = ARCGIS[key]
    out, offset = [], 0
    while True:
        q = (cfg["url"] + "/query?where=1%3D1&outFields=*&f=geojson"
             f"&resultRecordCount=1000&resultOffset={offset}")
        d = _get_json(q, timeout=60)
        feats = d.get("features") or []
        if not feats:
            break
        for f in feats:
            pr = f.get("properties") or {}
            g = f.get("geometry") or {}
            geo = g.get("coordinates") or []
            # ⚠️ NOT EVERY LAYER IS A Point. Toronto publishes MultiPoint, so
            # its coordinates are [[lon, lat]] and the obvious geo[0] is a LIST
            # rather than a number - which silently produced zero cameras from
            # a 498-camera layer rather than raising anything. Same class of
            # failure as the field names: the shape is agency-specific and
            # assuming one is how a whole network disappears without a message.
            if geo and isinstance(geo[0], (list, tuple)):
                geo = geo[0]
            if len(geo) < 2 or not isinstance(geo[0], (int, float)) \
                    or not isinstance(geo[1], (int, float)):
                continue
            url = pr.get(cfg["img"])
            if not isinstance(url, str) or not url.lower().startswith("http"):
                continue
            ref = str(pr.get(cfg["ref"]) or "").strip()
            if not ref:
                continue
            bits = [str(pr.get(k) or "").strip() for k in cfg["label"]]
            name = " ".join(b for b in bits if b) or cfg["name"]
            out.append({"src": key, "ref": ref, "name": name[:60],
                        "lat": float(geo[1]), "lon": float(geo[0]), "url": url})
        if not d.get("exceededTransferLimit") and len(feats) < 1000:
            break
        offset += len(feats)
        if offset > 60000:               # a runaway guard, not a real limit
            break
    return probe_filter(out, key) if measured_only else out


SOURCES = {"nyc": nyc_index, "fi": finland_index, "atx": austin_index,
           "ia": iowa_index, "on": ontario_index, "oh": ohio_index,
           "nm": newmexico_index, "mo": missouri_index,
           "ny": newyork_index, "mi": michigan_index,
           "in": indiana_index, "al": alabama_index,
           "ne_511": newengland_index, "nc": northcarolina_index,
           "il": illinois_index, "sd": southdakota_index,
           "az": arizona_index, "ut": utah_index, "id": idaho_index}
for _k in ARCGIS:
    SOURCES[_k] = (lambda k: lambda: arcgis_index(k))(_k)

# Networks that publish their own resolution and therefore need no probing.
# Finland is the only one so far, and it is why Finland was worth 2,160 cameras
# for the cost of 809 small metadata requests.
SELF_DESCRIBING = {"fi"}


# --------------------------------------------------------------------------
# the resolution probe
# --------------------------------------------------------------------------
# 🚨 CACHED ON DISK BECAUSE IT IS SOMEBODY ELSE'S BANDWIDTH. Measuring a
# network means downloading one image per camera, which is a thing to do once
# and remember - not on every restart of a service that restarts on failure.
PROBE = ROOT / "data" / "cam_probe.json"


# 🚨 AN UPPER BOUND, WHICH SOUNDS BACKWARDS UNTIL YOU SEE WHY.
# Ohio tiles four unrelated camera views into one 2560x1920 frame. It clears
# the width bar with room to spare and is the most dangerous image in the
# survey: a vehicle found in one tile would be published at the camera's single
# published coordinate, which is a different junction. Excluded until someone
# can say where each tile points. See ohio_index.
SRC_MAX_WIDTH = {"oh": 2559, "al": 2559}


def probe_filter(cams: list, src: str) -> list:
    """Keep only cameras MEASURED to be usable. Shared by every gated source.

    ⚠️ Unmeasured is not the same as unusable. A camera with no probe entry is
    dropped from this run and SAID SO, rather than being quietly treated as too
    small - a probe file that failed to copy would otherwise look exactly like
    a network that went standard definition overnight.
    """
    probe = load_probe()
    hi = SRC_MAX_WIDTH.get(src, 10 ** 9)
    out, unmeasured, small, big = [], 0, 0, 0
    for c in cams:
        w = probe.get(c["url"])
        if w is None:
            unmeasured += 1
        elif w < MIN_HD_WIDTH:
            small += 1
        elif w > hi:
            big += 1
        else:
            out.append(c)
    if unmeasured:
        print(f"  ⚠ {src}: {unmeasured} camera(s) never measured - run "
              f"`public_cams.py probe --source {src}` or they stay out")
    if big:
        print(f"  {src}: {big} camera(s) excluded as multi-view mosaics "
              f"(over {hi}px - see SRC_MAX_WIDTH)")
    if not out and unmeasured:
        raise RuntimeError(f"no {src} cameras are measured yet; run `probe` first")
    return out


PROBE_DIR = ROOT / "data" / "probe"
INDEX_DIR = ROOT / "data" / "index_cache"


def cached_index(src: str, build):
    """Build a source's camera list, falling back to a shipped copy.

    🚨 THE MACHINE THAT CAN READ A CATALOGUE IS NOT ALWAYS THE ONE THAT NEEDS
    IT. Michigan's index answers a US address and 403s a European one, while
    its IMAGE host serves both. But enrolment has to run on the HUB, because
    that is where the database is - and the hub is in Helsinki. So the state
    was unreachable from the only machine allowed to register it.

    Reading the catalogue and reading the pictures are separate problems, so
    they get separate machines: whichever box CAN see the index writes
    data/index_cache/<src>.json, that file is copied to the hub, and enrolment
    reads it. A camera list changes over months; an image changes every minute.

    ⚠️ The cache is a FALLBACK, never the first choice. A stale catalogue that
    silently outranks the live one is how a network quietly stops growing.

    Returns whatever build() returns - a list for most sources, a dict for
    Michigan, which needs two endpoints joined.
    """
    try:
        rows = build()
        if rows:
            INDEX_DIR.mkdir(parents=True, exist_ok=True)
            (INDEX_DIR / f"{src}.json").write_text(json.dumps(rows),
                                                   encoding="utf-8")
            return rows
    except Exception as exc:
        cached = INDEX_DIR / f"{src}.json"
        if not cached.exists():
            raise
        print(f"  ⚠ {src}: index unreachable from here "
              f"({type(exc).__name__}) - using the shipped copy")
        return json.loads(cached.read_text(encoding="utf-8"))
    return []


def _url_ref(url: str) -> str:
    """A short, stable id for a camera derived from its image URL.

    ⚠️ Used where an agency's own id is not durable - see cars_index. It has to
    be SHORT because it goes in the node name, which is capped, and the human
    part of the name is what gets truncated to make room.
    """
    import hashlib
    return hashlib.blake2b(url.encode(), digest_size=5).hexdigest()


def content_hash(raw: bytes) -> str:
    """A fingerprint of what the picture SHOWS, ignoring a burned-in clock.

    🚨 HASHING THE RAW BYTES DOES NOT FIND THESE CARDS, AND THAT WAS THE FIRST
    ATTEMPT. "Temporarily Unavailable" placeholders burn a LIVE TIMESTAMP into
    the image, so every camera serving the identical card produces different
    bytes and a byte hash reports them all as unique. Measured on Bay Area 511:
    275 of 744 cameras serve one card, and a byte hash finds none of them.

    So decode, drop the bottom strip where the clock is printed, and hash the
    pixels above it. Cropping 15% is enough for every timestamp bar seen and
    small enough that two genuinely different views never collide - they differ
    across the whole frame, not in a caption.

    ⚠️ Falls back to a byte hash if the image will not decode. A hash that
    raises is worse than one that occasionally misses.
    """
    import hashlib
    try:
        im = Image.open(io.BytesIO(raw)).convert("L")
        w, h = im.size
        im = im.crop((0, 0, w, max(1, int(h * 0.85))))
        # Downscale first: two frames from ONE camera differ pixel by pixel as
        # traffic moves, and this must fingerprint the CARD, not the moment.
        # A placeholder is flat, so it survives shrinking; a real road does not
        # match another real road even at 64px.
        im = im.resize((64, 64), Image.BILINEAR)
        return hashlib.md5(im.tobytes()).hexdigest()
    except Exception:
        return hashlib.md5(raw).hexdigest()


def load_probe() -> dict:
    """Every measurement we have, from one file per source.

    🚨 ONE FILE PER SOURCE BECAUSE ONE SHARED FILE IS A LOST-UPDATE RACE.
    A probe reads the whole map, adds its own results and writes the whole map
    back. Run two at once - which is the obvious thing to do when there are
    four large networks to measure - and the second finisher writes a map built
    before the first one's results existed, silently deleting them. Nothing
    errors; a network simply appears unmeasured again later.

    The legacy single file is still read, so an older box loses nothing.
    """
    out = {}
    if PROBE.exists():
        try:
            out.update(json.loads(PROBE.read_text(encoding="utf-8")))
        except Exception:
            pass
    if PROBE_DIR.is_dir():
        for f in sorted(PROBE_DIR.glob("*.json")):
            try:
                out.update(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
    return out


def cmd_probe(args) -> int:
    """Measure a network's actual image width, once, and remember it."""
    if args.source in SELF_DESCRIBING:
        print(f"{args.source} publishes its own resolutions - nothing to probe")
        return 0
    # ⚠️ ONLY THIS SOURCE'S OWN FILE IS REWRITTEN. Reading everybody's
    # measurements and writing them all back is exactly the lost-update race
    # that per-source files exist to prevent - see load_probe.
    mine = PROBE_DIR / f"{args.source}.json"
    known = {}
    if mine.exists():
        try:
            known = json.loads(mine.read_text(encoding="utf-8"))
        except Exception:
            known = {}
    everyone = load_probe()
    # 🚨 THE RAW LIST, NOT THE FILTERED ONE, OR THIS CAN NEVER BOOTSTRAP.
    # Every probe-gated index refuses to return unmeasured cameras by design,
    # so asking it what to measure would answer "nothing" for ever.
    if args.source == "on":
        d = _get_json("https://511on.ca/api/v2/get/cameras")
        urls = [v["Url"] for st in d for v in (st.get("Views") or [])
                if v.get("Status") == "Enabled" and v.get("Url")]
    elif args.source in ARCGIS:
        urls = [c["url"] for c in arcgis_index(args.source, measured_only=False)]
    elif (args.source in ("oh", "nm", "mo", "ny", "mi", "in", "tx", "al",
                          "ne_511", "nc", "il", "sd", "az", "ut", "id")
          or args.source in CARS):
        urls = [c["url"] for c in SOURCES[args.source](measured_only=False)]
    else:
        urls = [c["url"] for c in SOURCES[args.source]()]
    todo = [u for u in urls if u not in everyone]
    print(f"{len(urls)} camera(s), {len(known)} already measured, "
          f"{len(todo)} to do")

    def one(u):
        try:
            raw = fetch_image(u, timeout=20)
            # The bytes are kept so identical images can be found afterwards -
            # see the placeholder sweep below.
            return u, Image.open(io.BytesIO(raw)).size[0], content_hash(raw)
        except Exception:
            return u, 0, ""        # 0 = measured and unusable, not unmeasured

    done, digest = 0, {}
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        for u, w, h in pool.map(one, todo):
            if h:
                digest[u] = h
            known[u] = w
            done += 1
            if done % 200 == 0:
                print(f"  ...{done}/{len(todo)}", flush=True)
    # 🚨 A 1920x1080 "STREAM NOT AVAILABLE" CARD PASSES A WIDTH TEST.
    #
    # Several networks answer a dead camera with a full-HD placeholder image
    # rather than an error, and this probe measures WIDTH - so a placeholder is
    # indistinguishable from a working HD camera and gets enrolled as one. The
    # node then beats, posts nothing for ever, and looks like a camera pointed
    # at a very quiet road.
    #
    # Found by the state survey, and it is not rare: Tennessee's ENTIRE 21-strong
    # "HD tail" is one placeholder, and it ate 27 of South Carolina's 35.
    #
    # A placeholder gives itself away by SHOWING the same thing on cameras
    # that are supposed to be looking at different roads. Note "showing", not
    # "byte-identical": these cards print a live clock, so the bytes differ
    # every time and a byte hash finds none of them. See content_hash.
    #
    # Five is the threshold because two or three matching frames can happen
    # honestly - a black night shot, a covered lens - while five separate roads
    # producing one picture cannot.
    if digest:
        counts: dict = {}
        for h in digest.values():
            counts[h] = counts.get(h, 0) + 1
        dupes = {h for h, n in counts.items() if n >= PLACEHOLDER_MIN}
        if dupes:
            hit = [u for u, h in digest.items() if h in dupes]
            for u in hit:
                known[u] = 0        # measured, and measured to be a card
            print(f"  ⚠ {len(hit)} image(s) are byte-identical across "
                  f"{len(dupes)} group(s) - placeholders, not cameras")
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    mine.write_text(json.dumps(known), encoding="utf-8")
    everyone.update(known)
    good = sum(1 for u in urls if everyone.get(u, 0) >= MIN_HD_WIDTH)
    dead = sum(1 for u in urls if everyone.get(u, 0) == 0)
    print(f"\n{good} of {len(urls)} clear the {MIN_HD_WIDTH}px bar "
          f"({dead} would not load at all) -> {mine}")
    return 0


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------
class NotModified(Exception):
    """The camera says the picture has not changed since we last looked."""


# 🚨 THE WHOLE SCALING PROBLEM IS BANDWIDTH, NOT COMPUTE, AND THIS IS THE FIX.
#
# Measured before building anything: a camera costs 0.36s of NETWORK and 0.07s
# of inference. So the ceiling was never the GPU - at ~14 frames a second on one
# core, a 20s cycle covers ~280 cameras before compute matters at all. What does
# not scale is DOWNLOADING: several thousand HD JPEGs at ~200 KB each is
# hundreds of megabytes per sweep, every sweep, for ever, over a home
# connection.
#
# But a traffic camera refreshes every 1-5 minutes and we poll faster than that,
# so MOST FETCHES DOWNLOAD A PICTURE WE ALREADY HAVE. A conditional request
# turns those into a 304 with no body: ~200 bytes instead of ~200 KB, a
# thousandfold saving on the majority of requests, and no inference either
# because there is nothing new to look at.
#
# It is also the polite thing to do. These are somebody else's public services,
# several of them explicitly asking for it, and this is the difference between
# being a good citizen of their infrastructure and being a problem for it.
_COND: dict = {}          # url -> (etag, last_modified)
_COND_MAX = 40_000


def fetch(url: str, ua: str = UA_SURVEY, timeout: int = 15,
          conditional: bool = False) -> bytes:
    headers = {"User-Agent": ua,
               # Finland (Fintraffic) REFUSES an uncompressed request outright:
               # 406 "Use of gzip compression is required". Sending it always is
               # correct anyway - it is their bandwidth too.
               "Accept-Encoding": "gzip",
               "Digitraffic-User": "SparrowMap/public-cameras"}
    # ⚠️ TxDOT's snapshot endpoint refuses a request with no Referer. Sent only
    # to that host: a Referer is a claim about where you came from, and sending
    # a false one everywhere is both untrue and a fingerprint.
    if "its.txdot.gov" in url:
        headers["Referer"] = "https://its.txdot.gov/its/"
    if conditional:
        et, lm = _COND.get(url, (None, None))
        if et:
            headers["If-None-Match"] = et
        if lm:
            headers["If-Modified-Since"] = lm
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # 🚨 A WALL-CLOCK DEADLINE, NOT JUST A PER-READ TIMEOUT. MEASURED:
            # ONE CHUNK OF 192 CAMERAS TOOK 150 SECONDS INSTEAD OF 5.
            #
            # `timeout` is the socket timeout, which applies to each recv
            # SEPARATELY. A camera trickling one packet every few seconds
            # resets it for ever, so `r.read()` can hold a worker for minutes
            # while never once timing out. With 96 workers against thousands of
            # somebody else's cameras, a handful of those drag a whole sweep
            # under - and it looks exactly like being slow rather than being
            # stuck, which is why it survived the first three runs.
            #
            # The hub already learned this about its own request bodies (see
            # _body in hub.py). Same trap, same fix: read in pieces against a
            # total budget, and cap the size while we are here - a traffic
            # camera sending 20 MB is not a traffic camera.
            deadline = time.time() + timeout
            parts, got = [], 0
            while True:
                if time.time() > deadline:
                    raise TimeoutError(f"body exceeded {timeout}s")
                piece = r.read(1 << 16)
                if not piece:
                    break
                got += len(piece)
                if got > MAX_IMAGE_BYTES:
                    raise ValueError(f"body over {MAX_IMAGE_BYTES // 1024} KB")
                parts.append(piece)
            body = b"".join(parts)
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            if conditional:
                et = r.headers.get("ETag")
                lm = r.headers.get("Last-Modified")
                if et or lm:
                    if len(_COND) > _COND_MAX:
                        _COND.clear()
                    _COND[url] = (et, lm)
            return body
    except urllib.error.HTTPError as e:
        if e.code == 304:
            raise NotModified()
        raise


TXDOT_SNAP = "its.txdot.gov/its/DistrictIts/GetCctvSnapshotByIcdId"


def fetch_image(url: str, timeout: int = 15, conditional: bool = False) -> bytes:
    """The JPEG bytes for a camera, whatever hoop its agency puts in the way.

    🚨 NOT EVERY CAMERA IS A URL THAT RETURNS A PICTURE. Texas publishes 4,230
    cameras and none of them has an image address: you ask a JSON endpoint for
    a snapshot and it hands back base64 in a `snippet` field. Every other part
    of this file - probe, survey, the sweep - is written around "fetch this URL,
    get a JPEG", so the choice was to special-case three call sites or to put
    the difference in one function. This is that function.

    ⚠️ The base64 has NO `data:` prefix. It starts /9j/, which is a JPEG's own
    magic bytes in base64, and a decoder expecting a data URI silently gets
    nothing.
    """
    raw = fetch(url, timeout=timeout, conditional=conditional)
    if TXDOT_SNAP not in url:
        return raw
    snip = (json.loads(raw) or {}).get("snippet") or ""
    if not snip:
        raise ValueError("no snapshot in the response")
    # Tolerate a data: prefix in case they ever add one.
    m = re.search(r"base64,\s*([A-Za-z0-9+/=]+)", snip)
    return base64.b64decode(m.group(1) if m else snip)


def texas_index(measured_only: bool = True) -> list:
    """TxDOT ITS - the whole state, and it covers Dallas and San Antonio too.

    ⚠️ `isActive` IS FALSE ON ALL 4,230 AND MEANS NOTHING HERE. Filtering on it
    - the obvious reading - returns an empty fleet from a live network.

    ⚠️ The owner string goes in the query and MUST be URL-encoded: these names
    carry spaces, @ and brackets ("IH-10ML @ Dairy Ashford"). Unencoded, most
    requests fail, which reads as a broken source rather than a quoting bug.
    """
    rows = _get_json("https://its.txdot.gov/its/Map/GetIconModelsOfType?type=camera",
                     timeout=60)
    out = []
    for c in rows:
        lat, lon, owner = c.get("lat"), c.get("lon"), c.get("owner")
        if not lat or not lon or not owner:
            continue
        o = up.quote(str(owner), safe="")
        out.append({"src": "tx", "ref": str(owner)[-24:],
                    "name": (c.get("name") or str(owner))[:60],
                    "lat": float(lat), "lon": float(lon),
                    "url": f"https://{TXDOT_SNAP}?icdId={o}&districtCode={o}"})
    return probe_filter(out, "tx") if measured_only else out


# Registered here rather than in the table above, because Texas needs
# fetch_image and fetch_image needs to sit next to fetch.
SOURCES["tx"] = texas_index
for _st in CARS:
    SOURCES[_st] = (lambda k: lambda measured_only=True:
                    cars_index(k, measured_only))(_st)


def load_model(hub: str):
    import onnxruntime as ort
    # ⚠️ DO NOT go through detect.relay unless you have to. It imports cv2 at
    # module level, so asking it to resolve a FILE PATH drags OpenCV (and its
    # system libGL) onto a machine whose only job is polling JPEGs - this file
    # decodes with PIL and never touches cv2. The weights ship in the repo, so
    # on a checkout the answer is a path lookup with no imports at all.
    local = ROOT / "public" / "vendor" / "yolo11s.onnx"
    if not local.exists():
        from detect import relay          # download + verify path, needs cv2
        local = relay.model_path(hub)
    sess = ort.InferenceSession(str(local), providers=["CPUExecutionProvider"])
    shape = sess.get_inputs()[0].shape
    size = int(shape[2]) if isinstance(shape[2], int) else 320
    return sess, size


def detect(sess, size, img: Image.Image, conf: float = CONF):
    """Vehicle boxes in ORIGINAL image pixels, widest first."""
    w, h = img.size
    s = min(size / w, size / h)
    nw, nh = max(1, int(w * s)), max(1, int(h * s))
    ox, oy = (size - nw) // 2, (size - nh) // 2
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(img.resize((nw, nh), Image.BILINEAR), (ox, oy))
    x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    d = sess.run(None, {sess.get_inputs()[0].name: x})[0][0]
    boxes = []
    for i in range(d.shape[1]):
        best, bs = -1, 0.0
        for c in VEHICLE:
            v = float(d[4 + c, i])
            if v > bs:
                bs, best = v, c
        if bs < conf:
            continue
        cx, cy, bw, bh = (float(d[0, i]), float(d[1, i]),
                          float(d[2, i]), float(d[3, i]))
        x0 = (cx - bw / 2 - ox) / s
        y0 = (cy - bh / 2 - oy) / s
        boxes.append({"cls": VEHICLE[best], "conf": bs,
                      "box": (x0, y0, x0 + bw / s, y0 + bh / s),
                      "w": bw / s})
    boxes.sort(key=lambda b: -b["w"])
    # crude NMS: drop anything mostly inside a bigger keeper
    keep = []
    for b in boxes:
        if not any(_iou(b["box"], k["box"]) > 0.45 for k in keep):
            keep.append(b)
    return keep


def _iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    i = max(0, x1 - x0) * max(0, y1 - y0)
    ua = ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i)
    return i / ua if ua > 0 else 0.0


def crop_b64(img: Image.Image, box) -> str:
    """The vehicle, shrunk below plate legibility BEFORE it leaves this machine.

    Deliberately identical to detect/relay.crop_of. A public camera is not a
    special case: the same cap protects everybody in the frame who is not the
    vehicle being reported.
    """
    x0, y0, x1, y1 = box
    # Asymmetric, matching every other node kind: the roof is where the light
    # bar is, and the detector's box stops at the car.
    bw, bh = (x1 - x0), (y1 - y0)
    pad_x, pad_top, pad_bot = bw * 0.10, bh * 0.28, bh * 0.08
    sx, sy = max(0, int(x0 - pad_x)), max(0, int(y0 - pad_top))
    ex, ey = min(img.width, int(x1 + pad_x)), min(img.height, int(y1 + pad_bot))
    if ex - sx < 8 or ey - sy < 8:
        return ""
    sub = img.crop((sx, sy, ex, ey))
    s = min(1.0, SEND_EDGE / max(sub.size))
    if s < 1.0:
        sub = sub.resize((max(1, int(sub.width * s)), max(1, int(sub.height * s))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    sub.convert("RGB").save(buf, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cams": {}}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------
# the bridge between bulk enrolment and this poller
# --------------------------------------------------------------------------
# 🚨 WHY THIS EXISTS. `enrol` writes node_id + token into the state file above,
# so `run` knows who it is posting as. `tools/bulk_enrol_cams.py` does not go
# near that file - it calls nodes.enroll on the box and the credentials land in
# the DATABASE. So after registering 4,434 cameras the map showed 4,434 cameras
# and the poller could post as NONE of them: it had never heard of them.
#
# The join is the source ref, which bulk enrolment deliberately puts in the node
# name as " [fi:C0150301]" precisely because camera names are not unique.
#
# It is two commands and not one on purpose. The DATABASE is on the hub box; the
# POLLER runs somewhere with bandwidth and CPU to spare. `tokens` reads the DB
# and writes a small file, `run --tokens` fetches the source indexes itself and
# joins. Nothing has to hand thousands of camera credentials out over an API.
NAME_PREFIX = "Public traffic camera - "


def node_name_for(c: dict) -> str:
    """The node name a camera gets. THE SINGLE DEFINITION - do not re-derive.

    🚨 THIS IS THE JOIN KEY BETWEEN A SOURCE'S CAMERA AND OUR DATABASE ROW, and
    two separate programs have to agree on it exactly: bulk_enrol_cams.py to
    decide what is already registered, and cmd_tokens/cams_from_tokens to find
    the credentials again. Written out twice it would drift, and the failure
    would be silent both ways - cameras registered twice, or a fleet that never
    polls.

    ⚠️ AND THE SOURCE REF ALONE IS NOT ENOUGH, which cost 400 cameras.
    The obvious key is `src:ref`, and it is wrong: Iowa publishes several
    DIRECTIONAL VIEWS per `device_id`, each its own image URL and its own
    description. Keyed on the ref, 103 devices collapsed to one entry and 400
    perfectly good views were dropped without a word. The full name carries the
    view's description as well, so it is unique where the ref is not.

    The ref still goes in, last, where truncation cannot reach it: camera names
    are not unique either (Fintraffic has a "Tienpinta" at almost every site).
    """
    ref = str(c.get("ref") or "")
    tail = f" [{c['src']}:{ref}]"
    human = c["name"][:max(8, 80 - len(NAME_PREFIX) - len(tail))]
    return NAME_PREFIX + human + tail


def ref_of(node_name: str) -> str:
    """`...Tienpinta [fi:C0150301]` -> `fi:C0150301`, or "" if not one of ours.

    Only used to tell OUR public-camera nodes from anything else. It is not the
    join key - see node_name_for.
    """
    name = node_name or ""
    if not name.endswith("]"):
        return ""
    i = name.rfind("[")
    if i < 0:
        return ""
    key = name[i + 1:-1].strip()
    if ":" not in key:
        return ""
    # 🚨 THIS ONCE REJECTED ANY REF CONTAINING A SPACE, AND TEXAS IS 4,230
    # CAMERAS WHOSE REF IS A SENTENCE.
    #
    # TxDOT identifies a camera by its owner string - "330 @ Airhart (W)" - so
    # every Texas node name ends "[tx:330 @ Airhart (W)]". The space test was
    # there to avoid mistaking an ordinary name that happens to end in brackets
    # for one of ours, and it silently dropped 765 of 820 registered Texas
    # cameras from the credential export. They were on the map, they had
    # tokens, and the poller was told they did not exist.
    #
    # Checking the PREFIX against the known sources is both stricter and
    # correct: "[tx:...]" is ours, "[see note 4]" is not, and neither answer
    # depends on what the agency chose to put after the colon.
    src = key.split(":", 1)[0]
    return key if src in SOURCES else ""


def cmd_tokens(args) -> int:
    """Export node_id + token per source ref. RUN THIS WHERE THE DATABASE IS."""
    sys.path.insert(0, str(ROOT))
    import db                                             # noqa: E402

    # active_only=False deliberately: if enrolment left cameras pending, the
    # honest answer is a status breakdown, not a silently short file.
    rows = db.nodes(active_only=False)
    out, status, unkeyed, tokenless = {}, {}, 0, 0
    for n in rows:
        if (n.get("kind") or "") != "public_cam":
            continue
        st = n.get("status") or "?"
        status[st] = status.get(st, 0) + 1
        name = n.get("name") or ""
        if not ref_of(name):
            unkeyed += 1
            continue
        if not n.get("token"):
            tokenless += 1
            continue
        # 🚨 A LIST, NOT A VALUE, AND THIS IS THE WHOLE LESSON OF THIS FILE.
        # Even the full name is not unique: Iowa runs three cameras at one rest
        # area (ENTRY, CENTER, EXIT) that share coordinates, device_id AND
        # description - 101 names cover 499 real cameras. Every previous version
        # of this key assumed uniqueness and silently kept the last row it saw,
        # so ~400 working cameras were dropped without appearing in any count.
        out.setdefault(name, []).append(
            {"node_id": n["id"], "token": n["token"],
             "lat": n.get("lat"), "lon": n.get("lon"), "status": st})
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out), encoding="utf-8")
    exported = sum(len(v) for v in out.values())
    print(f"{exported} public camera(s) under {len(out)} name(s) exported to {path}")
    print("  by status: " + ", ".join(f"{k}={v}" for k, v in sorted(status.items())))
    # 🚨 EVERY ROW MUST BE ACCOUNTED FOR. A count that only reports successes
    # is how 400 cameras went missing in silence the first time.
    total = sum(status.values())
    dropped = total - exported - unkeyed - tokenless
    if unkeyed:
        print(f"  {unkeyed} node(s) carry no [src:ref] and are not from bulk "
              f"enrolment - the survey fleet, polled from its own state file")
    if tokenless:
        print(f"  ⚠ {tokenless} node(s) have no token and can never post")
    if dropped:
        print(f"  ⚠ {dropped} node(s) share a name with another and collapsed - "
              f"those are true duplicates and should be merged")
    if any(k != "active" for k in status):
        print("  ⚠ a camera that is not 'active' will be rejected when it posts")
    return 0


def dedupe_index(idx: list) -> list:
    """Drop cameras that are literally the same picture twice.

    ⚠️ Iowa serves the identical RWIS snapshot under `/Public/` and `/public/`.
    Different strings, one JPEG, and the pair registered as two nodes drawing
    two dots on one mast. The URL decides identity, case-insensitively; the
    first spelling offered wins so the choice is stable between runs.
    """
    seen, out = set(), []
    for c in idx:
        k = (c.get("url") or "").lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def cams_from_tokens(tokens_path: str, sources: list) -> list:
    """Join the live source indexes to exported credentials.

    🚨 THE NAME IS NOT A UNIQUE KEY, so this pairs GROUPS, not rows. One name
    can cover several real cameras (Iowa's ENTRY/CENTER/EXIT at a rest area),
    and the export therefore carries a list of nodes per name.

    Within a group the pairing is by NEAREST COORDINATES, then by a stable sort
    for the ones that genuinely share a position - so a camera keeps the same
    node across restarts, and its sightings keep landing on the same dot.
    """
    creds = json.loads(Path(tokens_path).read_text(encoding="utf-8"))
    probe = load_probe()
    cams, missing, ambiguous, dead = [], 0, 0, 0
    for src in sources:
        try:
            idx = dedupe_index(SOURCES[src]())
        except Exception as exc:
            print(f"  ⚠ {src} index unavailable ({type(exc).__name__}: "
                  f"{str(exc)[:60]}) - its cameras are skipped this run")
            continue
        # 🚨 DO NOT SPEND A SWEEP ON A CAMERA MEASURED AS DEAD. Austin's index
        # offers 1,005 cameras and 163 of them do not return an image at all -
        # they are listed, they are gone, and every cycle paid a connection and
        # up to the fetch timeout to rediscover that.
        #
        # ⚠️ ONLY width == 0, which means MEASURED AND UNLOADABLE. A camera with
        # no probe entry is UNMEASURED and is kept: missing data is not negative
        # data, and silently dropping everything unprobed would quietly delete
        # whole networks the moment the probe file went missing.
        before = len(idx)
        idx = [c for c in idx if probe.get(c["url"], 1) != 0]
        dead += before - len(idx)
        groups: dict = {}
        for c in idx:
            groups.setdefault(node_name_for(c), []).append(c)
        hit = 0
        for name, views in groups.items():
            pool = list(creds.get(name) or [])
            if not pool:
                missing += len(views)
                continue
            if len(views) > 1:
                ambiguous += len(views) - 1
            for c in sorted(views, key=lambda v: v.get("url") or ""):
                if not pool:
                    missing += 1
                    continue
                # Nearest first. Where a name covers cameras at genuinely
                # different points this is exact; where they share a position
                # every candidate ties and the sorted order decides, which is
                # arbitrary but at least the SAME arbitrary every restart.
                pool.sort(key=lambda cr: ((cr.get("lat") or 0) - c["lat"]) ** 2
                                         + ((cr.get("lon") or 0) - c["lon"]) ** 2)
                cr = pool.pop(0)
                # Position comes from the DB, which is what the map draws. A
                # source quietly moving a camera must not make the dots
                # disagree with the node.
                cams.append({**c, "node_id": cr["node_id"], "token": cr["token"],
                             "lat": cr.get("lat", c["lat"]),
                             "lon": cr.get("lon", c["lon"])})
                hit += 1
        print(f"  {src}: {hit} of {len(idx)} camera(s) matched to a node")
    if dead:
        print(f"  {dead} camera(s) skipped: measured and never return an image")
    if ambiguous:
        print(f"  {ambiguous} camera(s) share a name with another and were "
              f"paired by position - correct, but not guaranteed correct")
    if missing:
        print(f"  {missing} camera(s) offered by a source are not registered "
              f"(run tools/bulk_enrol_cams.py, then re-export tokens)")
    return cams


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_survey(args) -> int:
    cams = SOURCES[args.source]()
    print(f"{len(cams)} online cameras with coordinates; probing {args.limit}\n")
    sess, size = load_model(args.hub)
    st = load_state()
    good = 0
    for c in cams[:args.limit]:
        try:
            img = Image.open(io.BytesIO(fetch_image(c["url"]))).convert("RGB")
        except Exception:
            continue
        # Resolution is a free filter and 87% fail it, so spend no inference
        # on a camera that cannot possibly clear the bar.
        if img.width < MIN_HD_WIDTH:
            continue
        boxes = detect(sess, size, img)
        widest = boxes[0]["w"] if boxes else 0.0
        ok = widest >= MIN_VEHICLE_PX
        print(f"  {'USABLE ' if ok else 'too small'}  {c['name'][:44]:<46} "
              f"{img.width}x{img.height}  widest {widest:>6.1f}px")
        if ok:
            good += 1
            key = f"{c['src']}:{c['ref']}"
            prev = st["cams"].get(key, {})
            st["cams"][key] = {**prev, **c, "widest_px": round(widest, 1),
                               "surveyed": time.time()}
    save_state(st)
    print(f"\n{good} usable camera(s) recorded in {STATE}")
    return 0


def cmd_enrol(args) -> int:
    st = load_state()
    todo = [(k, c) for k, c in st["cams"].items() if not c.get("node_id")]
    print(f"{len(todo)} camera(s) to enrol; doing {min(len(todo), args.limit)}\n")
    for key, c in todo[:args.limit]:
        # ⚠️ NAMED AND TYPED AS A PUBLIC CAMERA. Anyone reading the map, the
        # review queue or the API must be able to tell this apart from a
        # volunteer's window without asking.
        body = {"name": f"Public traffic camera - {c['name']}"[:80],
                "lat": c["lat"], "lon": c["lon"], "kind": "public_cam",
                "reach_m": 60}
        req = urllib.request.Request(
            args.hub.rstrip("/") + "/api/enroll", method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA_NODE})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                nd = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"  FAILED {c['name'][:40]}: {e.code} {e.read()[:120]}")
            continue
        c["node_id"], c["token"] = nd["id"], nd["token"]
        c["status"] = nd.get("status")
        print(f"  {nd['id']}  {nd.get('status'):<8} {c['name'][:46]}")
        save_state(st)
        time.sleep(0.5)
    print("\n⚠️ new nodes may need approving (auto_approve_nodes is off).")
    return 0


def cmd_run(args) -> int:
    if args.tokens:
        cams = cams_from_tokens(args.tokens, args.sources.split(","))
    else:
        st = load_state()
        cams = [c for c in st["cams"].values() if c.get("node_id")]
    if not cams:
        print("no enrolled cameras - run survey then enrol first, or pass "
              "--tokens for cameras registered by bulk_enrol_cams.py")
        return 1
    sess, size = load_model(args.hub)
    # 🚨 WHY THREADS AND NOT A GPU.
    #
    # Measured before spending anything: a camera costs 0.36s of NETWORK and
    # 0.07s of inference. Fetching them one at a time made a cycle take 55s
    # against a 20s target with only 18 cameras, and it looked exactly like
    # being compute-bound - the machine was busy, the cycle was late, and the
    # obvious read was "we need a GPU". 83% of that time was a socket waiting,
    # and one camera timing out held up all seventeen others.
    #
    # So the fix is concurrency, not hardware. Fetch in parallel, infer in
    # series: inference is the cheap part and keeping it single-threaded means
    # one ONNX session, no locking, and no surprise memory growth.
    #
    # Headroom after this: 0.07s per frame is about 14 cameras a second on one
    # core, so a 20s cycle covers roughly 280 cameras before compute matters
    # at all. The next wall is politeness to the camera operators, not silicon.
    # 🚨 THE DEFAULTS ARE SIZED FOR EIGHT CAMERAS AND BREAK AT FOUR THOUSAND.
    # 0.36s of network each, 32 at a time, is ~50s for a 4,434-camera sweep -
    # already over a 20s interval before a single frame is decoded, and the
    # cycle silently falls behind rather than failing. So both are arguments,
    # and the run announces the arithmetic instead of leaving it to be
    # discovered from a warning at the end of the first cycle.
    interval = float(args.interval or POLL_S)
    workers = int(args.workers or min(32, max(4, len(cams))))
    pool = cf.ThreadPoolExecutor(max_workers=workers)
    est = len(cams) * 0.36 / max(1, workers)
    print(f"polling {len(cams)} camera(s) every {interval:.0f}s, {workers} at a time "
          f"(vehicles must be >= {MIN_VEHICLE_PX}px)")
    print(f"  a cold sweep is roughly {est:.0f}s of fetching; warm sweeps are far "
          f"cheaper because unchanged cameras answer 304"
          + ("\n  ⚠ THAT IS LONGER THAN THE INTERVAL - raise --workers or "
             "--interval" if est > interval else ""))
    print()
    sent = skipped = 0
    seen_last = {}

    def beat(reached: list) -> None:
        """Tell the hub these cameras are awake.

        🚨 WITHOUT THIS THE WHOLE FLEET READS AS OFFLINE, and the map says so on
        its front page. A camera is "online" when it has BEATEN recently, never
        when it last saw a car - deliberately, because a camera pointed at an
        empty street at 4am is working perfectly and has nothing to report.
        Almost every camera here is that camera on almost every sweep: the bar
        is a 120px vehicle, so a sweep that posts a dozen sightings has ~4,400
        cameras working correctly and silently.

        ⚠️ ONLY CAMERAS WE ACTUALLY REACHED. Beating for one that just timed out
        would be inventing liveness for a camera nobody can see - the exact
        thing the separate heartbeat exists to prevent. A 304 counts: the
        server answered, the picture simply had not changed.
        """
        for i in range(0, len(reached), BEAT_BATCH):
            chunk = reached[i:i + BEAT_BATCH]
            body = {"nodes": [{"node_id": c["node_id"], "token": c["token"]}
                              for c in chunk]}
            req = urllib.request.Request(
                args.hub.rstrip("/") + "/api/heartbeat/bulk", method="POST",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": UA_NODE})
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    out = json.loads(r.read() or b"{}")
                if out.get("rejected") or out.get("inactive"):
                    print(f"  -- beat: {out.get('beat')} ok, "
                          f"{out.get('rejected')} rejected, "
                          f"{out.get('inactive')} not active")
            except Exception as exc:
                # A missed heartbeat costs an "offline" badge, not a sighting.
                print(f"  -- beat failed for {len(chunk)}: "
                      f"{type(exc).__name__}: {str(exc)[:60]}")

    # 🚨 SEPARATE THE TWO COSTS OR YOU CANNOT TUNE EITHER. Chunk times swung
    # from 5s to 68s at a constant worker count and a constant 192 images, and
    # "cameras per second" cannot tell you whether that is a slow network or a
    # slow classifier - which is the difference between raising --workers and
    # parallelising inference. Two accumulators, reported per chunk.
    t_fetch = [0.0]
    t_infer = [0.0]

    def post_one(c, b, crop) -> str:
        """Send one sighting. Runs in the pool, never in the sweep loop."""
        body = {"node_id": c["node_id"], "ts": time.time(),
                "source": "phone_node", "body": b["cls"],
                "det_conf": round(b["conf"], 3), "plate_text": "",
                "plate_conf": 0, "evidence": {}, "snap_b64": crop,
                "lat": c["lat"], "lon": c["lon"]}
        req = urllib.request.Request(
            args.hub.rstrip("/") + "/api/sightings", method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + c["token"],
                     "User-Agent": UA_NODE})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                out = json.loads(r.read() or b"{}")
            return (f"  sent  {c['name'][:34]:<36} {b['cls']:<6} "
                    f"{b['w']:>5.0f}px  id={out.get('id')}")
        except urllib.error.HTTPError as e:
            return f"  {e.code}   {c['name'][:34]:<36} {e.read()[:80]}"
        except Exception as exc:
            # 🚨 ONE SLOW CAMERA MUST NOT KILL THE FLEET. This once caught
            # HTTPError only, so an ordinary read timeout escaped the loop and
            # took the whole poller down on its first cycle. A fleet runner
            # treats every per-item failure as data, never as an exit.
            return (f"  ERR   {c['name'][:34]:<36} "
                    f"{type(exc).__name__}: {str(exc)[:50]}")

    def grab(c):
        """Fetch one camera. Returns (cam, image or None, error).

        A camera whose picture has not changed returns (c, None, "304"), which
        the loop skips without spending any inference on it - see fetch().
        """
        try:
            # ⚠️ SHORTER THAN THE SURVEY'S 15s, DELIBERATELY. A survey is
            # patient because it runs once and a slow camera is still worth
            # measuring. A sweep is not: every second spent waiting on one
            # camera is a second the other 4,411 are not being looked at, and
            # the next sweep is along in five minutes anyway.
            _t = time.time()
            raw = fetch_image(c["url"], conditional=True,
                              timeout=POLL_TIMEOUT_S)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            t_fetch[0] += time.time() - _t
            return c, img, None
        except NotModified:
            return c, None, "304"
        except Exception as exc:
            return c, None, f"{type(exc).__name__}: {str(exc)[:40]}"

    try:
        while True:
            cycle_started = time.time()
            unchanged = 0
            reached = []
            posts = []
            # 🚨 CHUNKED, AND THE CHUNK IS THE MEMORY BOUND. MEASURED: 15.8 GB
            # AND CLIMBING BEFORE THIS EXISTED.
            #
            # `pool.map(grab, cams)` submits ALL of them at once and hands back
            # results in order. The fetches are 96-way concurrent but the
            # inference below is serial, so completed frames pile up in the
            # executor's result queue waiting to be consumed - and `grab`
            # returns a DECODED image, about 6 MB for an HD frame. At 4,412
            # cameras that is ~27 GB of pictures nobody is looking at yet, on a
            # 30 GB box.
            #
            # This is the shape of every outage in this project: a bound on the
            # wrong noun. Concurrency WAS limited - it was the queue behind it
            # that was not - and at eighteen cameras the difference did not
            # exist. Chunking makes the ceiling `chunk` frames, ~1.2 GB, and
            # costs nothing: the pool refills while inference runs.
            chunk = max(workers * 2, 32)
            # 🚨 A FLEET SWEEP MUST NARRATE ITSELF. Without this the process
            # printed nothing between "polling 4,412 cameras" and the end of a
            # cycle - eight minutes of a silent log in which a healthy sweep, a
            # stalled one and a hung socket are indistinguishable. The startup
            # estimate only counts the happy path, and the thing that actually
            # dominates a cold sweep is cameras that answer slowly or not at
            # all, each costing up to the fetch timeout.
            _done = 0
            _t0 = time.time()
            for _batch in (cams[i:i + chunk] for i in range(0, len(cams), chunk)):
                _bt = time.time()
                for c, img, err in pool.map(grab, _batch):
                    if err == "304":
                        unchanged += 1          # nothing new; costs ~200 bytes
                        reached.append(c)       # it answered: it is awake
                        continue
                    if err:
                        print(f"  {c['name'][:34]:<36} unreachable ({err})")
                        continue
                    reached.append(c)
                    _t = time.time()
                    boxes = detect(sess, size, img)
                    t_infer[0] += time.time() - _t
                    # 🚨 ONLY VEHICLES BIG ENOUGH TO BE JUDGED. A snapshot of a
                    # motorway holds dozens of 30px blobs; posting them would bury
                    # the review queue in things no human could rule on either.
                    big = [b for b in boxes if b["w"] >= MIN_VEHICLE_PX]
                    skipped += len(boxes) - len(big)
                    if not big:
                        continue
                    # One per poll: these are stills seconds apart, so several
                    # boxes are usually the same traffic seen twice.
                    b = big[0]
                    crop = crop_b64(img, b["box"])
                    if not crop:
                        continue
                    key = c["node_id"]
                    if time.time() - seen_last.get(key, 0) < interval * 0.9:
                        continue
                    seen_last[key] = time.time()
                    # 🚨 POSTING IS THE THIRD COST AND IT WAS THE HIDDEN ONE.
                    #
                    # Measured: one chunk took 63s wall while fetch contributed
                    # 121s of THREAD time (~1.3s wall across 96 threads) and
                    # inference 4s. The missing ~54s was eighteen sighting POSTs
                    # run one after another in this loop, each waiting on the hub
                    # to ingest a base64 JPEG - and the hub does real work per
                    # sighting, so ~3s each is normal, not slow.
                    #
                    # ⚠️ AND IT SCALES WITH SUCCESS. Every extra camera that
                    # actually SEES something makes the sweep slower, so the
                    # fleet would get further behind precisely as it started
                    # working. That is the worst possible shape for this.
                    #
                    # Handed to the same pool the fetches use, and drained at the
                    # end of the cycle so nothing is silently dropped.
                    posts.append(pool.submit(post_one, c, b, crop))
                _done += len(_batch)
                _el = time.time() - _t0
                print(f"  .. {_done}/{len(cams)} swept in {_el:.0f}s "
                      f"({len(_batch) / max(0.01, time.time() - _bt):.0f} cam/s"
                      f" | fetch {t_fetch[0]:.0f}s across {workers} threads,"
                      f" infer {t_infer[0]:.0f}s serial"
                      # ⚠️ QUEUED, NOT SENT. Posts are drained at the end of the
                      # cycle now, so `sent` is 0 for the whole sweep and a line
                      # saying "0 sent" would read as "finding nothing" during
                      # the exact minutes it is finding things.
                      f" | {unchanged} unchanged, {len(posts)} queued)",
                      flush=True)
            # ⚠️ DRAINED, NOT ABANDONED. Submitting and walking away would make
            # "sent" a count of things we hoped for. Every result is collected
            # and printed before the cycle claims a number.
            for f in posts:
                line = f.result()
                if line.startswith("  sent"):
                    sent += 1
                print(line)
            beat(reached)
            took = time.time() - cycle_started
            if unchanged:
                print(f"  -- {unchanged}/{len(cams)} unchanged (304, no download)")
            print(f"  -- cycle done in {took:.1f}s: {len(reached)}/{len(cams)} "
                  f"reached and beating, {sent} sent, {skipped} too "
                  f"small --" + ("  ⚠ SLOWER THAN THE POLL INTERVAL"
                                 if took > interval else ""), flush=True)
            if args.once:
                return 0
            time.sleep(max(0.0, interval - took))
    except KeyboardInterrupt:
        print(f"\nstopped. {sent} sent, {skipped} skipped as too small.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub",
                    default=os.environ.get("RAVEN_HUB") or
                    os.environ.get("SPARROW_HUB") or "")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("survey"); s.add_argument("--source", default="nyc")
    s.add_argument("--limit", type=int, default=200); s.set_defaults(fn=cmd_survey)
    e = sub.add_parser("enrol"); e.add_argument("--limit", type=int, default=5)
    e.set_defaults(fn=cmd_enrol)
    pr = sub.add_parser("probe")
    pr.add_argument("--source", required=True)
    pr.set_defaults(fn=cmd_probe)
    t = sub.add_parser("tokens")
    t.add_argument("--out", default=str(ROOT / "data" / "cam_tokens.json"))
    t.set_defaults(fn=cmd_tokens)
    r = sub.add_parser("run"); r.add_argument("--once", action="store_true")
    r.add_argument("--tokens", default="",
                   help="credentials exported by the `tokens` command; poll the "
                        "bulk-enrolled fleet instead of the local state file")
    r.add_argument("--sources", default="fi,ia,atx",
                   help="comma-separated source indexes to join against --tokens")
    r.add_argument("--interval", type=float, default=0,
                   help=f"seconds between sweeps (default {POLL_S:.0f}); a "
                        f"fleet of thousands needs far more")
    r.add_argument("--workers", type=int, default=0,
                   help="concurrent fetches (default min(32, fleet size))")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args()
    if not args.hub:
        ap.error("No RavenMap hub configured. Set RAVEN_HUB; "
                 "legacy SPARROW_HUB is also accepted.")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
