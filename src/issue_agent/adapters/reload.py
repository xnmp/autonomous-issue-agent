"""Hot-reload watch — restart a daemon when the harness's own code changes.

The nodemon/uvicorn-reload pattern, with one property those don't need: a
restart may only happen BETWEEN ticks. Dispatch is synchronous inside
`Daemon.tick`, so between ticks there is never an agent session in flight —
a reload can therefore never kill paid work, which is what made every manual
restart in the 2026-07-27 deployment a wait-for-settle exercise.

Mechanism: `ReloadWatch.changed()` is handed to `Daemon.run_forever`, which
checks it after each tick and exits with RELOAD_EXIT_CODE; `bin/point-at.sh`
wraps each daemon in a relaunch loop that re-sources the composed env file and
starts the new code. In-process `importlib.reload` is deliberately NOT used —
partial reloads of a live object graph are the classic source of
half-old-half-new state; a clean exec through `uv run` also picks up
dependency changes.

The fingerprint is (file count, newest mtime) over the watched trees: the
count catches deletions, the mtime catches edits and additions. A broken watch
returns "unchanged" — losing hot reload must never take the daemon with it.
"""

from __future__ import annotations

import os

# Reserved exit status the point-at relaunch loop recognises as "same daemon,
# new code, go again" — anything else propagates as a real exit.
RELOAD_EXIT_CODE = 87

_WATCHED_SUFFIXES = (".py", ".md", ".sh")
_SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "node_modules"})


class ReloadWatch:
    """Fingerprints `roots` (recursively, watched suffixes only) plus explicit
    `files` at construction; `changed()` answers against that baseline."""

    def __init__(self, roots: tuple[str, ...], files: tuple[str, ...] = ()):
        self.roots = roots
        self.files = files
        self._base = self._fingerprint()

    def _fingerprint(self) -> tuple[int, float]:
        count, newest = 0, 0.0
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for name in filenames:
                    if not name.endswith(_WATCHED_SUFFIXES):
                        continue
                    try:
                        mtime = os.stat(os.path.join(dirpath, name)).st_mtime
                    except OSError:
                        continue
                    count += 1
                    newest = max(newest, mtime)
        for path in self.files:
            try:
                newest = max(newest, os.stat(path).st_mtime)
                count += 1
            except OSError:
                continue
        return (count, newest)

    def changed(self) -> bool:
        try:
            return self._fingerprint() != self._base
        except Exception:  # noqa: BLE001 — a broken watch must not kill the daemon
            return False
