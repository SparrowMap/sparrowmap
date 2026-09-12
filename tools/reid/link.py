"""Decide which published patrol-car sightings are POSSIBLY the same vehicle.

    python tools/reid/link.py              # writes data/reid/tags.json + a report
    python tools/reid/link.py --show 30    # print the largest groups

Inputs: data/reid/features.json (analyze.py: CLIP embedding index, colours),
data/reid/ocr.json (ocr.py: text read off each crop), data/reid/embeddings.npy.
Output: data/reid/tags.json for tools/reid/apply.py to write on the box.

THE RULES, in the order a reader could check them against the photo:

  U  UNIT NUMBER. Both crops carry the same 2-4 digit number read at >= 0.6,
     both carry an agency word (POLICE / SHERIFF / ...) that agrees, and they
     are in the same region (same state, or the same 1-degree cell when the
     town is unknown). "312" in Austin and "312" in Columbus are two cars.
     911 is never a unit number: it is painted on half the fleet.

  S  SAME CAMERA, SAME CAR, STILL THERE. Consecutive sightings from ONE
     camera under STAY_GAP apart whose CLIP embeddings agree at >= SIM_STAY.
     This is the parked Explorer that one camera reports every poll cycle:
     405 rows that are one vehicle in one bay. Linking them is what turns
     that into "seen here 12:10-14:35" instead of 405 stamps.

Groups are the union of U and S links. A group of one gets no tag - a tag
that links nothing is noise in the panel. The group's name prefers the unit
number (u:<region>:<agency>:<number>) so a later run that reads the same
number lands in the same group; otherwise s:<node>:<first id>.

WHAT THIS DELIBERATELY DOES NOT DO: link two cameras by appearance alone.
A white Explorer with POLICE on the door matches every other white Explorer
with POLICE on the door in that county. Without a number that is an agency,
not a vehicle, and the markings row already says the agency. See the schema
notes on vehicle_tag in db.py: a link must be checkable and inferred.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent.parent
DATA = REPO / "data" / "reid"
REV = "r2-markings"

STAY_GAP_S = 20 * 60
SIM_STAY = 0.92
MIN_TEXT = 0.6
MAX_KM = 80.0

AGENCY = {
    "POLICE": "police", "SHERIFF": "sheriff", "TROOPER": "trooper",
    "STATE TROOPER": "trooper", "HIGHWAY PATROL": "highway patrol",
    "STATE POLICE": "state police", "CONSTABLE": "constable",
    "MARSHAL": "marshal", "PUBLIC SAFETY": "public safety",
}
NOT_UNITS = {"911", "011", "11", "311", "411", "2024", "2025", "2026"}
# Text burned into the FRAME by the camera, or a road sign behind the car, reads
# as digits too: "SH 358 @ FLOUR", "71 at SR-665", "EXIT 422B", "9:06",
# "127/Hamilton Ave". Measured 2026-09-12: 5 of the first 24 "unit numbers" were
# captions. A unit number on a car is a bare number, so anything that looks
# like a caption or a sign is refused before the digits are even looked at.
CAPTION = re.compile(r"[@/:]|(AT|SR|SH|US|HWY|EXIT|BLVD|AVE|ST|RD|MILE|MI|PKWY|DR|LN)", re.I)


def clean(t: str) -> str:
    return re.sub(r"[^A-Z ]", "", t.upper()).strip()


def agency_of(items: list) -> tuple[str | None, float]:
    best, score = None, 0.0
    for txt, sc, _ in items:
        if sc < MIN_TEXT:
            continue
        c = clean(txt)
        for word, kind in AGENCY.items():
            # A leading logo letter ("OSHERIFF") or a dropped one ("POLCE")
            # is still the word; ask for 80% agreement, not equality.
            for tok in [c] + c.split():
                if len(tok) >= 4 and difflib.SequenceMatcher(None, tok, word).ratio() >= 0.8:
                    if sc > score:
                        best, score = kind, sc
    return best, score


def city_of(items: list) -> str | None:
    for txt, sc, _ in items:
        c = clean(txt)
        m = re.search(r"(?:CITY|TOWN|VILLAGE|COUNTY) OF ([A-Z ]{3,})", c)
        if m and sc >= 0.5:
            return m.group(0).title()
    return None


def units_of(items: list, size=None) -> list[tuple[str, float]]:
    out = []
    w, h = (size or (0, 0))
    for txt, sc, box in items:
        if sc < MIN_TEXT or len(re.findall(r"\d", txt)) > 5:
            continue
        if CAPTION.search(txt) or len(txt.strip()) > 6:
            continue
        # Captions are burned along the frame's top or bottom edge, and a road
        # sign sits above the car; a number painted ON the car is in the middle.
        if h and (box[1] < 0.12 * h or box[3] > 0.88 * h):
            continue
        for m in re.finditer(r"(?<!\d)(\d{2,4})(?!\d)", txt):
            n = m.group(1)
            if n in NOT_UNITS:
                continue
            # Two digits are a unit number only when that is ALL the text says
            # and the recogniser is sure; "28" inside a longer read is a fragment.
            if len(n) == 2 and not (txt.strip() == n and sc >= 0.9):
                continue
            out.append((n, sc))
    return out


def km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def region_of(f: dict) -> str:
    place = f.get("place") or ""
    if "," in place:
        return place.rsplit(",", 1)[1].strip().lower().replace(" ", "-")
    return f"cell{int(f['lat'])}_{int(f['lon'])}"


class DSU:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()

    feats = json.load(open(DATA / "features.json"))
    ocr = json.load(open(DATA / "ocr.json"))
    embs = np.load(DATA / "embeddings.npy")
    by_id = {f["id"]: f for f in feats}

    # ---- per-sighting markings -------------------------------------------
    marks = {}
    for f in feats:
        items = ocr.get(str(f["id"]), [])
        agency, asc = agency_of(items)
        city = city_of(items)
        units = units_of(items, f.get("size"))
        unit = max(units, key=lambda u: u[1]) if units else None
        parts = []
        if agency: parts.append(agency.upper())
        if city: parts.append(city)
        # A number is only called a unit number when the same crop also says
        # what it is a unit OF. Without the agency word, "4228" is as likely a
        # road sign behind the car (it was: EXIT 422B) - precision over recall,
        # because the row exists to be checked against the photo.
        if unit and agency: parts.append(f"unit {unit[0]}")
        elif unit: unit = None
        # Colours only ride along with something READ. On their own they are a
        # k-means guess on a night crop ("black/brown" for a white Explorer) and
        # a markings row that says only that would teach readers to ignore it.
        # No colours: measured 2026-09-12, k-means called a black-and-white
        # Austin unit "green/silver" (windshield sunshade) - a wrong colour in
        # the panel teaches readers to ignore the row that carries the number.
        marks[f["id"]] = {"agency": agency, "agency_sc": asc, "city": city,
                          "unit": unit, "text": " · ".join(parts) if parts else None}

    dsu = DSU()
    why = defaultdict(list)          # id -> reasons
    conf = defaultdict(float)

    # ---- rule U: unit number -----------------------------------------------
    by_key = defaultdict(list)
    for f in feats:
        m = marks[f["id"]]
        if m["unit"] and m["agency"]:
            by_key[(region_of(f), m["agency"], m["unit"][0])].append(f)
    u_links = 0
    for (region, agency, n), group in by_key.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda f: f["ts"])
        anchor = group[0]
        for f in group:
            if km((anchor["lat"], anchor["lon"]), (f["lat"], f["lon"])) > MAX_KM:
                continue
            dsu.union(anchor["id"], f["id"])
            m = marks[f["id"]]
            why[f["id"]].append(f"unit number {n} read on the photo ({m['unit'][1]:.2f}), "
                                f"{agency.upper()} livery, {region.replace('-', ' ').title()}")
            conf[f["id"]] = max(conf[f["id"]], min(0.9, 0.5 + 0.4 * m["unit"][1]))
            u_links += 1

    # ---- rule S: same camera, still there ----------------------------------
    by_node = defaultdict(list)
    for f in feats:
        by_node[f["node_id"]].append(f)
    s_links = 0
    for node, rows in by_node.items():
        rows.sort(key=lambda f: f["ts"])
        for a, b in zip(rows, rows[1:]):
            if b["ts"] - a["ts"] >= STAY_GAP_S:
                continue
            sim = float(embs[a["emb"]] @ embs[b["emb"]])
            if sim < SIM_STAY:
                continue
            dsu.union(a["id"], b["id"])
            mins = (b["ts"] - a["ts"]) / 60
            why[b["id"]].append(f"the same camera saw a matching vehicle {mins:.0f} min earlier "
                                f"(image similarity {sim:.2f}), so it had not left")
            conf[b["id"]] = max(conf[b["id"]], min(0.85, 0.4 + 0.5 * (sim - SIM_STAY) / (1 - SIM_STAY)))
            if not why[a["id"]]:
                why[a["id"]].append("first of a run of sightings of one vehicle at this camera")
                conf[a["id"]] = max(conf[a["id"]], 0.6)
            s_links += 1

    # ---- groups -> tags ------------------------------------------------------
    groups = defaultdict(list)
    for f in feats:
        if f["id"] in dsu.p:
            groups[dsu.find(f["id"])].append(f)
    tags = []
    named = {}
    for root, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda f: f["ts"])
        unit_keys = [(region_of(f), marks[f["id"]]["agency"], marks[f["id"]]["unit"][0])
                     for f in members if marks[f["id"]]["unit"] and marks[f["id"]]["agency"]]
        if unit_keys:
            r, a, n = max(set(unit_keys), key=unit_keys.count)
            name = f"u:{r}:{a.replace(' ', '-')}:{n}"
        else:
            name = f"s:{members[0]['node_id']}:{members[0]['id']}"
        named[name] = members
        for f in members:
            tags.append({"id": f["id"], "tag": name, "conf": round(conf[f["id"]] or 0.6, 2),
                         "why": "; ".join(why[f["id"]]) or "part of a run of matching sightings"})

    markings = [{"id": i, "text": m["text"]} for i, m in marks.items() if m["text"]]
    json.dump({"rev": REV, "markings": markings, "tags": tags},
              open(DATA / "tags.json", "w"), indent=0)

    n_ag = sum(1 for m in marks.values() if m["agency"])
    n_un = sum(1 for m in marks.values() if m["unit"])
    cams = {f["node_id"] for f in feats}
    multi = [(k, v) for k, v in named.items() if len({f['node_id'] for f in v}) > 1]
    print(f"{len(feats)} sightings from {len(cams)} cameras")
    print(f"agency word read on {n_ag} ({100 * n_ag / len(feats):.0f}%), "
          f"unit number on {n_un} ({100 * n_un / len(feats):.0f}%), markings written for {len(markings)}")
    print(f"links: {u_links} by unit number, {s_links} same-camera stays")
    print(f"groups: {len(named)} (covering {len(tags)} sightings); "
          f"{len(multi)} span more than one camera - those are the real trails")
    for k, v in sorted(named.items(), key=lambda kv: -len(kv[1]))[:args.show]:
        cams_ = len({f['node_id'] for f in v})
        span = (v[-1]["ts"] - v[0]["ts"]) / 3600
        print(f"  {len(v):4d} sightings  {cams_} cam  {span:6.1f} h  {k}  "
              f"[{marks[v[0]['id']]['text']}]")


if __name__ == "__main__":
    main()
