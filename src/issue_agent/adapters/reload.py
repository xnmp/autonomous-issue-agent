"""Hot-reload watch — restart a daemon when the harness's own code changes.

The nodemon/uvicorn-reload pattern, with two properties those don't need:

* A restart may only happen BETWEEN ticks. Dispatch is synchronous inside
  `Daemon.tick`, so between ticks there is never an agent session in flight —
  a reload can therefore never kill paid work, which is what made every manual
  restart in the 2026-07-27 deployment a wait-for-settle exercise.

* The trigger is the harness repo's HEAD commit, not file mtimes. An mtime
  watch reloads on every editor save, so a tick boundary landing between two
  saves of one logical change would boot the daemon on a half-edited tree.
  Committing is the operator's (and the assistant's) "this state is coherent
  and the suite ran" signal — partial changes structurally cannot make it in.
  The one mtime-watched exception is the composed env file (`AGENT_ENV`): it
  is gitignored by design, and an operator edit to it is a single atomic save.

Mechanism: `ReloadWatch.changed()` is handed to `Daemon.run_forever`, which
checks it after each tick and exits with RELOAD_EXIT_CODE; `bin/point-at.sh`
wraps each daemon in a relaunch loop that re-sources the composed env file and
starts the new code. In-process `importlib.reload` is deliberately NOT used —
partial reloads of a live object graph are the classic source of
half-old-half-new state; a clean exec through `uv run` also picks up
dependency changes.

A broken watch (git missing, not a repo, unreadable env file) reads as
"unchanged" — losing hot reload must never take the daemon with it.
"""

from __future__ import annotations

import os
import subprocess

# Reserved exit status the point-at relaunch loop recognises as "same daemon,
# new code, go again" — anything else propagates as a real exit.
RELOAD_EXIT_CODE = 87


def _head_commit(repo_root: str) -> str:
    """HEAD sha of `repo_root`, or "" when unanswerable (not a repo, no git).
    A subprocess per tick (~ms at POLL_SECONDS cadence) buys correct handling
    of every ref layout (branch, detached, packed-refs) over hand-parsing
    .git internals."""
    try:
        res = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — a broken watch must not kill the daemon
        return ""


class ReloadWatch:
    """Baselines the harness repo's HEAD (plus explicit files' mtimes) at
    construction; `changed()` answers against that baseline."""

    def __init__(self, repo_root: str, files: tuple[str, ...] = ()):
        self.repo_root = repo_root
        self.files = files
        self._base = self._fingerprint()

    def _mtimes(self) -> tuple[float, ...]:
        out = []
        for path in self.files:
            try:
                out.append(os.stat(path).st_mtime)
            except OSError:
                out.append(0.0)
        return tuple(out)

    def _fingerprint(self) -> tuple[str, tuple[float, ...]]:
        return (_head_commit(self.repo_root), self._mtimes())

    def changed(self) -> bool:
        try:
            head, mtimes = self._fingerprint()
            base_head, base_mtimes = self._base
            # "" means git couldn't answer (transiently or at all): compare only
            # what we CAN see rather than flapping between "" and a real sha.
            if head and base_head and head != base_head:
                return True
            return mtimes != base_mtimes
        except Exception:  # noqa: BLE001 — a broken watch must not kill the daemon
            return False
