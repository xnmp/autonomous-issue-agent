"""Wake signal — a file mtime used as an edge between two processes.

The webhook forwarder (`app/webhook.py`) and the role daemons are separate
processes with no shared memory, so a delivery has to cross a process boundary
to shorten a daemon's sleep. The cheapest correct carrier is a file's mtime:
the writer touches it, the sleeper stats it. Deliberately carries NO payload —
a wake means only "reality may have moved", and the daemon answers that by
doing what it does every poll (a full world build). Losing a wake costs
latency, never correctness: the poll loop is still a reconciler.

Contract:
  * `touch_wake(path)` — never raises; a wake that cannot be delivered degrades
    to the reconcile cadence, which is exactly the fallback when the forwarder
    is dead entirely.
  * `WakeSignal.pending()` — CONSUMING. True iff the file's mtime advanced
    since this object last observed it (construction counts as an observation,
    so a stale wake file from a previous run never fires a spurious tick).
    Missing/unreadable file = not pending.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


def wake_path(state_dir: str) -> str:
    """The wake file for a role's STATE_DIR. One function so the forwarder and
    the daemon cannot disagree on the filename (a silent no-push otherwise)."""
    return os.path.join(state_dir, "wake")


def _mtime_ns(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def touch_wake(path: str) -> None:
    """Create/refresh the wake file so any WakeSignal on it reports pending.

    The bump is forced to be STRICTLY increasing rather than "now": two
    deliveries inside one filesystem clock granule would otherwise share an
    mtime and the second would be swallowed. Never raises — a failed touch is
    a lost edge, not a dead forwarder."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a"):
            pass
        now = time.time_ns()
        prev = _mtime_ns(path)
        stamp = now if now > prev else prev + 1000
        os.utime(path, ns=(stamp, stamp))
    except OSError:
        return


@dataclass(frozen=True)
class WakeSignal:
    path: str
    # One-slot mutable cell: the last mtime this signal consumed. Frozen
    # dataclass + a list is the stdlib way to keep value semantics on `path`
    # while the consume position still moves.
    _seen: list[int] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._seen.append(_mtime_ns(self.path))

    def pending(self) -> bool:
        cur = _mtime_ns(self.path)
        if cur > self._seen[0]:
            self._seen[0] = cur
            return True
        return False
