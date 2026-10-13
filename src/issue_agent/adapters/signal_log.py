"""The one place the harness sends a signal, so every send is attributable.

Incident #270: a Codex leg took an unattributed SIGTERM and the harness could
not rule its own sends in or out. Each send logs exactly one line (signal,
target, reason, run_id) before it is attempted (best-effort: a broken sink never blocks the send); a failed send still shows
what was tried.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable


def _stderr(line: str) -> None:
    # A raw write: safe inside a signal handler (no buffered-writer reentrancy).
    os.write(2, (line + "\n").encode(errors="replace"))


def send_signal(
    signum: int,
    *,
    reason: str,
    pid: int | None = None,
    pgid: int | None = None,
    run_id: str | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Log then send `signum` to exactly one of `pid` / `pgid`.

    Raises what os.kill/os.killpg raise; callers keep their own handling.
    """
    if (pid is None) == (pgid is None):
        raise ValueError("send_signal needs exactly one of pid or pgid")
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = f"SIG{signum}"
    target = f"pid={pid}" if pid is not None else f"pgid={pgid}"
    run = f" run_id={run_id}" if run_id else ""
    try:
        (log or _stderr)(f"signal-send: {name} {target} reason={reason}{run} sender_pid={os.getpid()}")
    except Exception:  # noqa: BLE001 - a dead log sink must never stop the kill
        pass
    if pid is not None:
        os.kill(pid, signum)
    else:
        os.killpg(pgid, signum)  # type: ignore[arg-type]
