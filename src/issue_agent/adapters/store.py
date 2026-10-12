"""Advisory files and worktree claims.

Selected lifecycle continuity, allowances, holds, and completion receipts live
in SQLite. Historical control files are stopped-import evidence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..domain.agent_task import RuntimeName
from ..domain.world import normalize_predicted_paths
from ..domain.ports import WorkflowControlError


@dataclass(frozen=True)
class Store:
    """Filesystem advisory context and the dispatcher infrastructure counter."""

    state_dir: str
    # SQL-backed session projection still needs the configured receiving runtime.
    runtime: str | RuntimeName = RuntimeName.CLAUDE.value
    # ── pure path helpers ──
    def issue_path(self, issue: int, suffix: str) -> str:
        return os.path.join(self.state_dir, f"issue-{issue}.{suffix}")

    def pr_path(self, pr: int, suffix: str) -> str:
        return os.path.join(self.state_dir, f"pr-{pr}.{suffix}")

    # ── generic thin IO ──
    # Encoding is PINNED, never the process locale: most values here are ASCII
    # ids and counters, but advisory history may contain Unicode,
    # and a daemon started under a C/POSIX locale would otherwise fail to write
    # (or mis-read) the same file a UTF-8 shell wrote. `errors="replace"` on the
    # advisory reads preserve byte-damaged notes.
    def _read(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        except (FileNotFoundError, OSError):
            return ""

    def _write(self, path: str, value: str) -> None:
        # review M3: pid-suffix the tmp name — two daemons sharing a STATE_DIR
        # (or a restarted process racing its predecessor's in-flight write)
        # must not collide on one shared "<path>.tmp" and truncate each
        # other's write mid-flight. flush()+fsync() before os.replace closes
        # the crash window where a torn/zero-length tmp file (data still in
        # the page cache, not on disk) could get installed at `path` by a
        # power loss / kill between write() and replace().
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _remove(self, path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WorkflowControlError(path, f"control record removal failed: {exc}") from exc

    # ── router path predictions ─────────────────────────────────────────────
    def get_predicted_paths(self, issue: int) -> frozenset[str]:
        """The router's last usable path prediction, or no signal on bad state."""
        try:
            raw = json.loads(self._read(self.issue_path(issue, "predicted-paths")))
        except (TypeError, ValueError, json.JSONDecodeError):
            return frozenset()
        return normalize_predicted_paths(raw)

    def save_predicted_paths(self, issue: int, paths: object) -> None:
        """Persist only validated paths; bad router output clears stale guesses."""
        normalized = normalize_predicted_paths(paths)
        path = self.issue_path(issue, "predicted-paths")
        if normalized:
            self._write(path, json.dumps(sorted(normalized)))
        else:
            self._remove(path)

