"""Deterministic module-level characterization of privacy.py's alias/redaction
helpers moved from hub.py during Stage 2B (ALIAS/ALIAS_DAY, alias_map(),
resolve_hash(), public_rows()).

This is a CHARACTERIZATION-ONLY hardening pass requested before Stage 2C: it
demonstrates the CURRENT behavior of these helpers, including per-day
state-sharing/rotation, without changing production behavior. No timing
assertions or real sleeps - day rotation is exercised by monkeypatching
`privacy.now` to fixed values, exactly like the microcache TTL tests use fake
clocks instead of real ones.

Run directly:  python tools\\test_privacy_alias_unit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import privacy

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name + (f": {detail}" if detail else ""))


def reset(day_seconds: float = 1_000_000 * 86400) -> None:
    """Clear alias state and pin the clock to a known day boundary."""
    privacy.ALIAS.clear()
    privacy.ALIAS_DAY[0] = int(day_seconds // 86400)
    privacy.now = lambda: day_seconds


def t_public_rows_redacts_and_returns_new_dicts() -> None:
    print("\n== public_rows(): redacts and does not mutate the input row ==")
    reset()
    row = {"id": 1, "tier": "private", "plate_hash": "deadbeef", "node_id": "n1",
           "plate_conf": 0.9}
    out = privacy.public_rows([row])
    check("public_rows returns a list of the same length", len(out) == 1)
    check("private-tier row's plate_hash is aliased (a: prefix)",
          out[0]["plate_hash"].startswith("a:"), out[0]["plate_hash"])
    check("internal-only field stripped from output", "plate_conf" not in out[0])
    check("original row dict is untouched (redact copies)",
          row["plate_hash"] == "deadbeef" and "plate_conf" in row)


def t_alias_map_records_reversible_mapping() -> None:
    print("\n== alias_map()/resolve_hash(): alias is reversible within a day ==")
    reset()
    row = {"id": 2, "tier": "private", "plate_hash": "cafef00d"}
    privacy.alias_map([row])
    aliased = [k for k in privacy.ALIAS if privacy.ALIAS[k] == "cafef00d"]
    check("alias_map recorded exactly one alias for the real hash",
          len(aliased) == 1, str(privacy.ALIAS))
    alias_token = aliased[0]
    check("resolve_hash maps the alias back to the real hash",
          privacy.resolve_hash(alias_token) == "cafef00d")
    check("resolve_hash passes through an unknown token unchanged",
          privacy.resolve_hash("not-an-alias") == "not-an-alias")


def t_public_rows_populates_alias_as_a_side_effect() -> None:
    print("\n== public_rows() populates ALIAS as a side effect (used by /api/track) ==")
    reset()
    row = {"id": 3, "tier": "private", "plate_hash": "0ff1ce00"}
    out = privacy.public_rows([row])
    token = out[0]["plate_hash"]
    check("the alias returned by public_rows resolves back to the real hash",
          privacy.resolve_hash(token) == "0ff1ce00", token)


def t_no_alias_for_public_tier_rows() -> None:
    print("\n== public tier rows are not aliased (real plate_hash retained) ==")
    reset()
    row = {"id": 4, "tier": "public", "plate_hash": "abc12345",
           "reviewed": "confirmed"}
    out = privacy.public_rows([row])
    check("public-tier confirmed row keeps its real plate_hash",
          out[0]["plate_hash"] == "abc12345", out[0]["plate_hash"])
    check("no alias was recorded for a public-tier row",
          "abc12345" not in privacy.ALIAS.values())


def t_empty_plate_hash_not_aliased() -> None:
    print("\n== a row with no readable plate is never aliased ==")
    reset()
    row = {"id": 5, "tier": "private", "plate_hash": None}
    privacy.alias_map([row])
    check("ALIAS stays empty for a NULL plate_hash", privacy.ALIAS == {})
    row2 = {"id": 6, "tier": "private", "plate_hash": ""}
    privacy.alias_map([row2])
    check("ALIAS stays empty for an empty-string plate_hash", privacy.ALIAS == {})


def t_alias_rotates_across_a_day_boundary() -> None:
    print("\n== ALIAS is cleared when alias_map() observes a new day ==")
    day0 = 2_000_000 * 86400.0
    reset(day0)
    row = {"id": 7, "tier": "private", "plate_hash": "11112222"}
    privacy.alias_map([row])
    check("alias recorded on day 0", len(privacy.ALIAS) == 1, str(privacy.ALIAS))
    token_day0 = next(iter(privacy.ALIAS))

    # Advance the clock by exactly one day; do not touch ALIAS/ALIAS_DAY
    # directly - only alias_map()'s own day-rollover check should clear it.
    privacy.now = lambda: day0 + 86400.0
    row2 = {"id": 8, "tier": "private", "plate_hash": "33334444"}
    privacy.alias_map([row2])
    check("ALIAS was cleared on crossing a day boundary (old token gone)",
          token_day0 not in privacy.ALIAS, str(privacy.ALIAS))
    check("ALIAS_DAY advanced by exactly one day",
          privacy.ALIAS_DAY[0] == int(day0 // 86400) + 1, str(privacy.ALIAS_DAY))

    new_token = next(iter(privacy.ALIAS))
    check("the new day's row was re-aliased and its real hash resolves",
          privacy.resolve_hash(new_token) == "33334444")
    check("the previous day's alias token no longer resolves to anything real"
          " (falls through unchanged, since it is now an unrecognized token)",
          privacy.resolve_hash(token_day0) == token_day0)


def t_same_hash_same_day_yields_same_alias() -> None:
    print("\n== the SAME real hash on the SAME day always yields the SAME alias ==")
    reset()
    row_a = {"id": 9, "tier": "private", "plate_hash": "55556666"}
    row_b = {"id": 10, "tier": "private", "plate_hash": "55556666"}
    out = privacy.public_rows([row_a, row_b])
    check("both rows for the same underlying plate share one alias token",
          out[0]["plate_hash"] == out[1]["plate_hash"], str(out))
    check("exactly one alias entry recorded despite two rows",
          len(privacy.ALIAS) == 1, str(privacy.ALIAS))


def main() -> int:
    t_public_rows_redacts_and_returns_new_dicts()
    t_alias_map_records_reversible_mapping()
    t_public_rows_populates_alias_as_a_side_effect()
    t_no_alias_for_public_tier_rows()
    t_empty_plate_hash_not_aliased()
    t_alias_rotates_across_a_day_boundary()
    t_same_hash_same_day_yields_same_alias()

    print(f"\n{len(FAILURES)} failure(s) out of the checks above.")
    if FAILURES:
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("privacy.py alias/redaction unit characterization passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
