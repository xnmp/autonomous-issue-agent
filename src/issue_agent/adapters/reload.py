"""ADR 0007 hot reload — restart when coherent harness state changes.

The nodemon/uvicorn-reload pattern, with two properties those don't need:

* A restart may only complete BETWEEN ticks after admission closes and active
  dispatches drain. Observation continues during the drain, but a reload never
  kills paid work — the constraint learned from manual restarts in the
  2026-07-27 deployment.

* The trigger is the harness repo's HEAD commit, not file mtimes. An mtime
  watch reloads on every editor save, so a tick boundary landing between two
  saves of one logical change would boot the daemon on a half-edited tree.
  Committing is the operator's (and the assistant's) "this state is coherent
  and the suite ran" signal — partial changes structurally cannot make it in.
  The one mtime-watched exception is the composed env file (`AGENT_ENV`): it
  is gitignored by design, and an operator edit to it is a single atomic save.
  With a `fingerprint` a file's identity is what that function makes of it,
  recomputed only when its bytes change: the daemons fingerprint only the
  part that needs a restart, so a live knob (ADR 0007 amendment) never
  reloads.

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

import subprocess
import hashlib
from urllib.parse import urlparse
from collections.abc import Callable

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


def remote_repo(root: str) -> str:
    """The GitHub origin as owner/name, or "": the dashboard's "is self"
    badge, and where the fix role files harness gaps (`fix.harness_gap_repo`)."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode:
        return ""
    url = result.stdout.strip().removesuffix(".git")
    # Only a GitHub origin: the fix role files harness gaps into it.
    if url.startswith("git@github.com:"):
        return url.split(":", 1)[1]
    parsed = urlparse(url)
    return parsed.path.strip("/") if parsed.hostname == "github.com" and parsed.path.count("/") == 2 else ""


class ReloadWatch:
    """Baselines the harness repo's HEAD (plus explicit file contents) at
    construction; `changed()` answers against that baseline."""

    def __init__(
        self,
        repo_root: str,
        files: tuple[str, ...] = (),
        validate: Callable[[], None] | None = None,
        fingerprint: Callable[[str], str] | None = None,
    ):
        self.repo_root = repo_root
        self.files = files
        self.validate = validate
        self.fingerprint = fingerprint
        self._fingerprints: dict[str, tuple[str, str]] = {}
        self._base = self._fingerprint()

    def _digests(self) -> tuple[str, ...]:
        out = []
        for path in self.files:
            try:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                out.append("")
                continue
            out.append(self._identity(path, digest))
        return tuple(out)

    def _identity(self, path: str, digest: str) -> str:
        if self.fingerprint is None:
            return digest
        cached = self._fingerprints.get(path)
        if cached is None or cached[0] != digest:
            try:
                identity = self.fingerprint(path)
            except Exception:  # noqa: BLE001 - unreadable reads as changed; validate refuses it
                identity = f"unfingerprinted:{digest}"
            cached = self._fingerprints[path] = (digest, identity)
        return cached[1]

    def _fingerprint(self) -> tuple[str, tuple[str, ...]]:
        return (_head_commit(self.repo_root), self._digests())

    def changed(self) -> bool:
        try:
            head, digests = self._fingerprint()
            base_head, base_digests = self._base
            # "" means git couldn't answer (transiently or at all): compare only
            # what we CAN see rather than flapping between "" and a real sha.
            changed = (
                bool(head and base_head and head != base_head)
                or digests != base_digests
            )
            if changed and self.validate is not None:
                self.validate()
            return changed
        except Exception:  # noqa: BLE001 — a broken watch must not kill the daemon
            return False
