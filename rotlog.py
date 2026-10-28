"""A log file that cannot grow without bound: print() into it, it rotates by size.

    from rotlog import redirect
    redirect("logs/health_check.log")          # stdout + stderr from here on

WHY (10-27): the scheduled jobs logged through a shell redirect (`>> health.log`),
which nothing ever trims. health.log reached 16.8 GB and box_puller.log 1.4 GB.

HOW: every write opens the file, appends and closes it. That costs a little per
line and buys the one property that matters on Windows: no process holds the
file open, so the rename that rotates it cannot fail because a watchdog copy or
a second run happens to have it open. Rotation keeps `backups` older files as
NAME.1 (newest) .. NAME.N (oldest) and drops anything older; total disk use is
about (backups + 1) x max_bytes.

`chain(path)` lists the files oldest first, for a reader that wants the whole
history in order (tools/sync_pen_labels.py).
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
BACKUPS = 4


class RotatingFile(io.TextIOBase):
    encoding = "utf-8"   # writers that pick symbols by stdout.encoding see the truth
    def __init__(self, path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def writable(self) -> bool:
        return True

    def _rotate(self) -> None:
        try:
            oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
            oldest.unlink(missing_ok=True)
            for i in range(self.backups - 1, 0, -1):
                src = self.path.with_name(f"{self.path.name}.{i}")
                if src.exists():
                    os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass   # another writer mid-rename: the next write tries again

    def write(self, s: str) -> int:
        if not s:
            return 0
        try:
            if self.path.exists() and self.path.stat().st_size + len(s) > self.max_bytes:
                self._rotate()
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                f.write(s)
        except OSError:
            pass   # a log must never take the job down with it
        return len(s)

    def flush(self) -> None:
        pass


def redirect(path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> RotatingFile:
    """Send this process's stdout and stderr to a rotating log."""
    rf = RotatingFile(path, max_bytes, backups)
    sys.stdout = rf
    sys.stderr = rf
    return rf


def chain(path, backups: int = BACKUPS) -> list[Path]:
    """The rotated files and the live one, OLDEST first, that exist."""
    p = Path(path)
    olds = [p.with_name(f"{p.name}.{i}") for i in range(backups, 0, -1)]
    return [f for f in olds + [p] if f.exists()]
