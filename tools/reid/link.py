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
     911 is never a unit NUMBER - it is painted on half the fleet - but a
     read of 911 on the car IS an agency signal, so "4715 + 911" trails
     exactly as "4715 + POLICE" does (his call, 2026-09-23).

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

import places

REPO = Path(__file__).resolve().parent.parent.parent
DATA = REPO / "data" / "reid"
# A new rev whenever the rules or the recogniser change, so apply.py
# --clear-rev retires what the old ones believed instead of leaving two
# vocabularies mixed in one column.
#   r3-numbers   a number is kept without the agency word (written "number N"
#                rather than "unit N"); POLIISI/POLIS are agency words.
#   r4-serverocr the OCR underneath is the PaddleOCR SERVER model, which reads
#                a roof number the mobile model returns as noise (see ocr.py).
#   r5-911       a read of "911" on the car counts as an agency signal, so a
#                number beside it forms a trail; markings print the word that
#                was read, not the fleet it implies.
#   r6-seattle   agency matching is CONTAINMENT-first: difflib's whole-string
#                ratio scored "SEATTLEPOLICE" 0.632 against POLICE but 0.800
#                against STATE POLICE, publishing Seattle cars as STATE POLICE.
#                A multi-word agency now needs every one of its words. The town
#                painted beside the agency word is published when the camera's
#                own town corroborates it.
#   r7-livery    markings report the whole phrase that was read
#                ("HICAGO POLICE"), not just the agency word.
#   r8-place     the livery town is corroborated against the SIGHTING'S OWN
#                coordinates, so a dashcam (whose node has no town) can have
#                one. State-level agencies name their state.
#   r9-digits    five-digit fleet numbers are kept; the guard and the
#                extractor now share MAX_UNIT_DIGITS.
#   r10-roof     roof numbers survive the caption test; an exact agency
#                word beats a fuzzy one.
REV = "r10-roof"

STAY_GAP_S = 20 * 60
SIM_STAY = 0.92
MIN_TEXT = 0.6
MAX_KM = 80.0

AGENCY = {
    "POLICE": "police", "SHERIFF": "sheriff", "TROOPER": "trooper",
    "STATE TROOPER": "trooper", "HIGHWAY PATROL": "highway patrol",
    "STATE POLICE": "state police", "CONSTABLE": "constable",
    "MARSHAL": "marshal", "PUBLIC SAFETY": "public safety",
    # 🚨 SparrowMap HAS CAMERAS IN FINLAND and the word on those cars is
    # POLIISI. Measured 2026-09-23: 4 of the 25 numbers this file discarded for
    # "no agency word" had POLIISI or POLIS read in the very same crop. An
    # English-only vocabulary is not a precision rule, it is a blind spot.
    "POLIISI": "poliisi", "POLIS": "poliisi",
}
NOT_UNITS = {"911", "011", "11", "311", "411", "2024", "2025", "2026"}
# Text burned into the FRAME by the camera, or a road sign behind the car, reads
# as digits too: "SH 358 @ FLOUR", "71 at SR-665", "EXIT 422B", "9:06",
# "127/Hamilton Ave". Measured 2026-09-12: 5 of the first 24 "unit numbers" were
# captions. A unit number on a car is a bare number, so anything that looks
# like a caption or a sign is refused before the digits are even looked at.
# 🚨 THE WORD-BOUNDARY ESCAPES HERE WERE LITERAL BACKSPACE BYTES UNTIL 2026-09-23, so the whole
# word list never matched anything and only [@/:] was doing any work. It went
# unnoticed because units_of has a second guard - text longer than 6 characters
# is refused - and every caption this was written for ("911 at SR-665",
# "EXIT 422B") is longer than 6. The rule only became load-bearing when
# agency_of started reading 911 off the car, which has no length guard.
CAPTION = re.compile(r"[@/:]|\b(AT|SR|SH|US|HWY|EXIT|BLVD|AVE|ST|RD|MILE|MI|PKWY|DR|LN)\b", re.I)


def clean(t: str) -> str:
    return re.sub(r"[^A-Z ]", "", t.upper()).strip()


def _word_match(tok: str, word: str) -> bool:
    """Is `word` on the car, allowing for a mangled read but not for a longer
    name that merely resembles it?

    🚨 CONTAINMENT FIRST, FUZZ ONLY FOR DAMAGE, AND ONLY AT COMPARABLE LENGTH.
    This is the whole of the SEATTLE POLICE bug (his catch, 2026-09-23): OCR
    read "SEATTLEPOLICE" at 0.96 and difflib scored it 0.632 against POLICE but
    0.800 against STATE POLICE, so a Seattle patrol car was published as STATE
    POLICE. difflib's whole-string ratio rewards similar LENGTH, so a
    city-plus-POLICE livery always resembles a longer agency name more than the
    short word it literally contains - the matcher was structurally biased
    towards the wrong answer, and the more crops it read the more often it
    would be wrong.

    So an exact containment is the evidence, and difflib is kept only for the
    case it was actually wanted for - a dropped or hallucinated letter
    ("POLCE", "OSHERIFF") - where the two strings are within two characters of
    each other and the comparison is meaningful.
    """
    if word in tok:
        return True
    return (abs(len(tok) - len(word)) <= 2
            and difflib.SequenceMatcher(None, tok, word).ratio() >= 0.8)


def _phrase_match(text: str, phrase: str) -> int:
    """How many words of an agency phrase are on the car (0 = not this agency).

    A multi-word agency is only matched when EVERY word of it is there, so
    "STATE POLICE" needs STATE, and "SEATTLE POLICE" can never satisfy it.
    """
    parts = phrase.split()
    toks = [t for t in [text] + text.split() if len(t) >= 4]
    if not toks:
        return 0
    for p in parts:
        if not any(_word_match(t, p) for t in toks):
            return 0
    return len(parts)


def agency_of(items: list) -> tuple[str | None, float, str | None]:
    """What the livery says this vehicle IS. 911 counts (his call, 2026-09-23).

    🚨 "911" IS AN AGENCY SIGNAL, NEVER A UNIT NUMBER. NOT_UNITS already refuses
    it as a unit because it is painted on half the fleet - but that same
    ubiquity is exactly what makes it evidence of WHAT the car is. A crop that
    reads "4715" and "911" is a numbered emergency vehicle just as plainly as
    one that reads "4715" and "POLICE", and before this it produced no trail at
    all, because rule U needs an agency and 911 gave it none.

    It maps to "police" rather than to a kind of its own so that a car read as
    911 at one camera and POLICE at the next lands in the SAME rule-U key -
    two names for one fleet would silently prevent the very link this is for.
    Lowest precedence: a real agency word always wins. The third return value
    is the word actually READ ("POLICE", "POLIISI", "911"), so the markings row
    can say 911 rather than claim POLICE was on the car.
    """
    # 🚨 REPORT THE WHOLE PHRASE THAT WAS READ, NOT ONLY THE WORD MATCHED.
    #
    # His catch, 2026-09-25: a Chicago patrol car whose door plainly reads
    # CHICAGO POLICE was published with markings of just "POLICE". The OCR had
    # read "HICAGO POLICE" at 0.999 - the C clipped by the crop edge - and this
    # function kept only the token it recognised. The department name is the
    # single most identifying thing written on a patrol car, and this row
    # exists to say what is READ off the photograph.
    #
    # The confidence floor is what keeps it honest. At 0.999 a reader can check
    # the phrase against the picture; the 0.62 reads this file also sees
    # ("POUICG", "Potiae", "331i0d") would turn the row into noise and teach
    # people to ignore the one line that carries the evidence.
    #
    # ⚠️ AS READ, INCLUDING THE CLIPPED LETTER. "HICAGO POLICE" is what the
    # photograph shows - the C really is outside the crop - and quietly
    # correcting it to CHICAGO would be inventing a letter that was never seen.
    # 🚨 AN EXACT WORD BEATS A FUZZY ONE, WHATEVER ORDER THE TABLE IS IN.
    # His van read POLIS at 1.00 and was published as POLIISI. Both are in the
    # table (Finland is bilingual and its cars carry both), and difflib scores
    # POLIS against POLIISI at 0.833 - over the 0.8 fuzz threshold - so the
    # fuzzy entry matched too, tied on every other term, and won purely by
    # sitting earlier in the dict. Printing a word the car does not carry is
    # exactly what this row must never do, so exactness is now part of the
    # ranking rather than an accident of iteration order.
    FULL_PHRASE_MIN = 0.9
    best, score, seen, words, phrase, exact = None, 0.0, None, 0, None, False
    for txt, sc, _ in items:
        if sc < MIN_TEXT:
            continue
        c = clean(txt)
        for ph, kind in AGENCY.items():
            n = _phrase_match(c, ph)
            if not n:
                continue
            ex = all(part in c for part in ph.split())
            if (n, ex, sc) > (words, exact, score):
                best, score, seen, words, exact = kind, sc, ph, n, ex
                tidy = " ".join(c.split())
                phrase = (tidy if (sc >= FULL_PHRASE_MIN
                                   and ph in tidy and len(tidy) > len(ph))
                          else None)
    if best:
        return best, score, (phrase or seen)
    for txt, sc, box in items:
        # Same placement test a unit number gets: a caption burned along the
        # frame edge saying 911 is the camera talking, not the car.
        if sc >= MIN_TEXT and re.search(r"(?<!\d)911(?!\d)", txt) and not CAPTION.search(txt):
            seen = "911"
            score = max(score, sc)
    return ("police" if seen else None), score, seen


def city_of(items: list, place: str | None = None,
            lat=None, lon=None) -> str | None:
    """Whose fleet it is: "City Of Linden", or the town painted beside the
    agency word when the camera's own town CONFIRMS it.

    The second rule is what the SEATTLE POLICE bug exposed: the livery said
    SEATTLE POLICE and the only thing published was the agency, so the most
    identifying words on the car were thrown away. The town is not taken on the
    recogniser's word - "TTLEPOLICE" would otherwise publish a car belonging to
    the town of Ttle. It is only used when it matches the town the camera is
    already known to be in, which makes it corroborated rather than read.
    """
    for txt, sc, _ in items:
        c = clean(txt)
        m = re.search(r"(?:CITY|TOWN|VILLAGE|COUNTY) OF ([A-Z ]{3,})", c)
        if m and sc >= 0.5:
            return m.group(0).title()
    town = (place or "").split(",")[0].strip()
    if len(town) < 4 and lat is not None:
        # 🚨 A MOVING CAMERA HAS NO TOWN, BUT THE SIGHTING HAS COORDINATES.
        # A dashcam node carries no place, so the livery town had nothing to
        # be checked against and went out uncorroborated - measured, a Chicago
        # patrol car published as "HICAGO POLICE". The
        # position was there all along; places.near turns it into a town, from
        # a cache so the pipeline is not paying a second per sighting.
        town = (places.known(lat, lon)[0] or "").strip()
    if len(town) < 4:
        return None
    up = clean(town)
    for txt, sc, _ in items:
        if sc < MIN_TEXT:
            continue
        c = clean(txt)
        for word in AGENCY:
            if not c.endswith(word) or len(c) <= len(word):
                continue
            prefix = c[:-len(word)].strip()
            if len(prefix) >= 4 and _word_match(prefix, up):
                return town
    return None


#: How many digits a fleet number may have. 🚨 ONE CONSTANT, USED BY BOTH THE
#: GUARD AND THE EXTRACTOR, because they disagreed and it cost a perfect read.
#:
#: His catch, 2026-09-25: a patrol car with 12020 plainly painted on the roof
#: published no markings at all. PaddleOCR had read "12020" at confidence 1.00 -
#: the single cleanest read in the whole store - it sat mid-crop so the caption
#: and edge tests both passed it, and then the extractor threw it away, because
#: `\d{2,4}` with a no-digit-either-side lookaround cannot match a FIVE digit
#: number. The guard on the line above allowed up to five. The two bounds were
#: written separately, they drifted by one, and the failure was silent: every
#: five-digit fleet number this network has ever seen was deleted after being
#: read correctly.
#:
#: ⚠️ Roof numbers are the one marking that identifies an individual car rather
#: than a department, so this is exactly the reading the markings row exists to
#: carry - and 12020 is a real number on a real car, not a guess.
MAX_UNIT_DIGITS = 5


def units_of(items: list, size=None) -> list[tuple[str, float]]:
    out = []
    w, h = (size or (0, 0))
    for txt, sc, box in items:
        if sc < MIN_TEXT or len(re.findall(r"\d", txt)) > MAX_UNIT_DIGITS:
            continue
        if CAPTION.search(txt) or len(txt.strip()) > 6:
            continue
        # 🚨 A ROOF NUMBER IS AT THE TOP OF THE CROP. THAT IS WHERE ROOFS ARE.
        #
        # This used to refuse the top and bottom 12% outright, on the reasoning
        # that "captions are burned along the FRAME's edge". True of a frame,
        # false of a CROP: what this function reads is a tight box around one
        # vehicle, so the top of it is the roof - and a roof number is the only
        # marking that identifies an individual car rather than a department.
        #
        # His catch, 2026-09-26 (a Finnish patrol van, POLIS on the door, 207 on
        # the roof, no markings row at all): OCR read "-207" at confidence 1.00
        # at y=12 in a 200x173 crop. The cut was 20.8, so a perfect read of the
        # most valuable marking on the car was discarded for being 9 pixels too
        # high. Every overhead traffic camera - which is most of this network -
        # puts roof numbers in exactly that band.
        #
        # ⚠️ WHAT A BURNED-IN CAPTION ACTUALLY LOOKS LIKE IS A BANNER: it spans
        # the picture and hugs the very edge. So both have to be true before the
        # text is refused for its position, and a narrow number near the top is
        # read as what it is. The caption REGEX and the 6-character length guard
        # are untouched and still carry the cases they were measured on
        # ("911 at SR-665", "EXIT 422B", "9:06").
        if h and w:
            wide = (box[2] - box[0]) >= 0.45 * w
            at_edge = box[1] < 0.05 * h or box[3] > 0.95 * h
            if wide and at_edge:
                continue
        for m in re.finditer(r"(?<!\d)(\d{2,%d})(?!\d)" % MAX_UNIT_DIGITS, txt):
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
        agency, asc, agency_word = agency_of(items)
        city = city_of(items, f.get("place"), f.get("lat"), f.get("lon"))
        units = units_of(items, f.get("size"))
        unit = max(units, key=lambda u: u[1]) if units else None
        parts = []
        # The WORD that was read, not the fleet it implies: a crop whose only
        # evidence is "911" must not print "POLICE" as though the word were on
        # the car. The markings row exists to be checked against the photo.
        if agency: parts.append((agency_word or agency).upper())
        # 🚨 "STATE POLICE" WITHOUT THE STATE IS HALF AN ANSWER. Michigan State
        # Police and Illinois State Police are different forces, and the row is
        # supposed to identify a vehicle. The state comes from where the
        # sighting HAPPENED, which is corroboration rather than a guess - and
        # if that is unknown the row simply says STATE POLICE, as before.
        if agency in ("state police", "trooper", "highway patrol") and not city:
            st = (places.known(f.get("lat"), f.get("lon")) or (None, None))[1]
            if st:
                parts.append(st)
        if city: parts.append(city)
        # 🚨 A NUMBER READ OFF A CAR IS KEPT WHETHER OR NOT THE AGENCY WORD WAS
        # READ TOO (his call, 2026-09-23: "numbers on cars even not plates
        # should be searchable").
        #
        # This used to throw the number away unless the same crop also read
        # POLICE or SHERIFF, on the reasoning that "4228" alone might be the
        # road sign behind the car. But the sign case is already handled where
        # it belongs - by WHERE the text sits in the frame (CAPTION, the
        # top/bottom edge test in units_of), which is the actual discriminator.
        # Requiring the agency word on top of that was a second, blunter filter
        # that mostly deleted true readings: 25 numbers discarded against 13
        # kept, and four of the discards had POLIISI in the crop all along.
        #
        # What changes is the WORDING, not the confidence. With an agency word
        # it is a "unit"; without one it is a "number", because that is exactly
        # what is known - a number painted on a published government vehicle.
        # The photograph is on the row either way, so a reader can check it.
        if unit: parts.append(f"unit {unit[0]}" if agency else f"number {unit[0]}")
        # Colours only ride along with something READ. On their own they are a
        # k-means guess on a night crop ("black/brown" for a white Explorer) and
        # a markings row that says only that would teach readers to ignore it.
        # No colours: measured 2026-09-12, k-means called a black-and-white
        # Austin unit "green/silver" (windshield sunshade) - a wrong colour in
        # the panel teaches readers to ignore the row that carries the number.
        marks[f["id"]] = {"agency": agency, "agency_sc": asc, "city": city,
                          "agency_word": agency_word,
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
            _ev = (m.get("agency_word") or agency).upper()
            why[f["id"]].append(f"unit number {n} read on the photo ({m['unit'][1]:.2f}), "
                                f"{_ev} livery, {region.replace('-', ' ').title()}")
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
    # 🚨 SAY WHICH ROWS WERE JUDGED, NOT ONLY WHICH ONES GOT A MARKING.
    # A row that HAD a marking under the old rules and gets none under the new
    # ones is never named in this file, so apply.py used to leave the old value
    # in place for ever and the column quietly held two generations of guess.
    # That is how a Seattle car stayed labelled STATE POLICE after the rules
    # that produced the label were replaced. "considered" lets apply.py clear
    # exactly the rows this run looked at and decided had nothing to say.
    json.dump({"rev": REV, "markings": markings, "tags": tags,
               "considered": [f["id"] for f in feats]},
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
