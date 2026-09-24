"""The case classifier, as cases a reader can check against a docket.

    python tools/test_classify.py

No framework: this runs next to the data it describes. Every row is a caption
and party list the live database actually contains, or a near miss that has
already cost a day.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from oversight import classify_case_police  # noqa: E402

#: (caption, party names, a party carried a rank, expected verdict)
CASES = [
    # --- federal: Bivens / FTCA territory, added 2026-09-24 ---------------
    ("Doe v. United States", ["Special Agent John Smith, FBI"], False, "federal"),
    ("Reyes v. Mayorkas", ["Department of Homeland Security"], False, "federal"),
    ("Lopez v. USA", ["Border Patrol Agent Ruiz"], False, "federal"),
    ("Nolan v. USA", ["Drug Enforcement Administration"], False, "federal"),
    ("Tran v. USA", ["Deportation Officer Blake, ICE"], False, "federal"),
    # 🚨 "Deputy U.S. Marshal" contains "deputy" AND "marshal", both of which
    # the state pattern reads as local law enforcement. Tested naively, every
    # federal marshal files as a county deputy.
    ("Ward v. USA", ["Deputy U.S. Marshal Boyd"], True, "federal"),
    ("Vega v. USA", ["Deputy United States Marshal Cruz"], True, "federal"),

    # --- joint task forces MUST stay police -------------------------------
    # An FBI agent beside three city detectives is the commonest way a federal
    # name appears here. Calling it 'federal' removes a police department from
    # the queue a reviewer actually works.
    ("Brooks v. City of Flint",
     ["Detroit Police Officer Carter", "Special Agent Diaz, FBI"], True, "police"),
    ("Hale v. County", ["Deputy Sheriff Ames", "F.B.I."], True, "police"),
    ("Reed v. Twp", ["Trooper Vance", "Task Force Officer Hall"], True, "police"),

    # --- unchanged behaviour ----------------------------------------------
    ("Smith v. Jones", ["Police Officer Nunez"], True, "police"),
    ("Ali v. MDOC", ["Warden Thompson"], False, "corrections"),
    ("Clark v. Saginaw",
     ["Saginaw County Prosecuting Attorney's Office"], False, "other"),
    # 🚨 None means "no evidence yet", never "no". Two surnames say nothing
    # about anyone's job, and calling that 'other' would hide real police
    # cases the moment enrichment names the parties.
    ("Smith v. Jones", [], False, None),
]


def main() -> int:
    bad = 0
    for name, parties, rank, want in CASES:
        got, why = classify_case_police(name, "", "", parties, has_rank=rank)
        ok = got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {str(got):11s} want {str(want):11s} "
              f"{name:26s} {why}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} pass")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
