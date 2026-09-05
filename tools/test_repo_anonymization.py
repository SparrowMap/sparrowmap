"""Generic repository anonymization guard.

This test intentionally checks for common developer-environment leakage patterns
without containing any real personal identifiers. It is a generic guardrail for
future merges, not a repository-specific personal-data list.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUSPICIOUS = (
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
    re.compile(r"/(?:Users|home)/[^/\s]+"),
    re.compile(r"(?i)ssh\s+-i\s+[^\s]+\.ssh/[^\s]+"),
    re.compile(r"(?i)BEGIN (?:OPENSSH|RSA|PGP) PRIVATE KEY"),
    re.compile(r"(?i)(?:ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z\-_]{35})"),
)

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
SKIP_PATH_PARTS = {"vendor"}


def main() -> int:
    hits: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if any(part in SKIP_PATH_PARTS for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".md", ".txt", ".sh", ".yml", ".yaml", ".json", ".js", ".html", ".css", ".mjs"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for idx, line in enumerate(text.splitlines(), 1):
            for pat in SUSPICIOUS:
                if pat.search(line):
                    rel = path.relative_to(ROOT)
                    hits.append(f"{rel}:{idx}: {line.strip()[:180]}")
                    break
    if hits:
        print("Suspicious environment leakage patterns found:")
        for item in hits[:50]:
            print("  ", item)
        print(f"Total matches: {len(hits)}")
        return 1
    print("No suspicious developer-environment absolute path or secret patterns detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
