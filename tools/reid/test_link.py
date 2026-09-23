"""The trail rules, as cases a reader can check against a photograph.

    python tools/reid/test_link.py

No test framework on purpose: this runs in either venv, next to the pipeline,
and the point is that the rules stay checkable after somebody changes them.
Every case is a crop the recogniser could plausibly produce.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import link  # noqa: E402

H = 200


def mid(t, s=0.95):
    """Text painted on the car: in the middle of the frame."""
    return [t, s, [40, 80, 90, 100]]


def edge(t, s=0.95):
    """Text burned along the frame edge: the camera talking, not the car."""
    return [t, s, [0, 2, 199, 14]]


CASES = [
    # name,                items,                                       trails?
    ("number + POLICE",    [mid("4715"), mid("POLICE")],                True),
    # 911 is an agency signal, never a unit number (his call 2026-09-23)
    ("number + 911",       [mid("4715"), mid("911")],                   True),
    ("number + 911 + word", [mid("4715"), mid("911"), mid("POLICE")],   True),
    ("911 alone",          [mid("911")],                                False),
    ("number alone",       [mid("4715")],                               False),
    # a caption that happens to contain 911 is not a car marking
    ("911 in a caption",   [mid("4715"), edge("911 at SR-665")],        False),
    ("road sign",          [mid("4715"), mid("EXIT 422B")],             False),
    ("real word beats 911", [mid("4715"), mid("911"), mid("SHERIFF")],  True),
    ("911 read too weakly", [mid("4715"), mid("911", 0.40)],            False),
    ("Finnish livery",     [mid("522"), mid("POLIISI")],                True),
]

#: The agency a livery is published as, and the town beside it. His catch
#: 2026-09-23: a Seattle patrol car was published as STATE POLICE.
#: (items, camera's place, expected agency, expected town)
AGENCY_CASES = [
    ([mid("SEATTLEPOLICE")], "Seattle, Washington",  "police",       "Seattle"),
    ([mid("SEATILEPOLICE")], "Seattle, Washington",  "police",       "Seattle"),
    # a mangled prefix must not invent the town of Ttle
    ([mid("TTLEPOLICE")],    "Seattle, Washington",  "police",       None),
    # the town is corroborated by the camera, never taken on the read alone
    ([mid("SEATTLEPOLICE")], "Columbus, Ohio",       "police",       None),
    ([mid("SEATTLEPOLICE")], None,                   "police",       None),
    # a real state car still reads as one
    ([mid("STATEPOLICE")],   "Lansing, Michigan",    "state police", None),
    ([mid("HIGHWAYPATROL")], None,                   "highway patrol", None),
    # damage the fuzz exists for
    ([mid("POLCE")],         None,                   "police",       None),
    ([mid("OSHERIFF")],      None,                   "sheriff",      None),
    ([mid("CITY OF LINDEN")], "Linden, Michigan",    None,           "City Of Linden"),
]


def main() -> int:
    bad = 0
    for name, items, want in CASES:
        agency, _, word = link.agency_of(items)
        units = link.units_of(items, (H, H))
        unit = max(units, key=lambda u: u[1]) if units else None
        parts = []
        if agency:
            parts.append((word or agency).upper())
        if unit:
            parts.append(f"unit {unit[0]}" if agency else f"number {unit[0]}")
        got = bool(unit and agency)
        ok = got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:22s} {' / '.join(parts) or '-':24s} "
              f"trail={'yes' if got else 'no':3s} want={'yes' if want else 'no'}")
    for items, place, want_agency, want_town in AGENCY_CASES:
        agency, _, _ = link.agency_of(items)
        town = link.city_of(items, place)
        ok = agency == want_agency and town == want_town
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {items[0][0]:16s} @ {str(place):20s} "
              f"agency={str(agency):14s} town={str(town):15s} "
              f"want {want_agency} / {want_town}")

    # 911 in the markings must never read as a unit number
    assert "911" not in [u[0] for u in link.units_of([mid("911")], (H, H))], \
        "911 must never be a unit number"
    total = len(CASES) + len(AGENCY_CASES)
    print(f"\n{total - bad}/{total} pass")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
