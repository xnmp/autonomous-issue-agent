"""Advisory files, worktree claims, and the explicitly retained Jira path.

Selected lifecycle continuity, allowances, holds, and completion receipts live
in SQLite. Historical control files are stopped-import evidence. Jira retains
its existing infrastructure counter; its ticket mirrors remain advisory.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from ..domain.agent_task import RuntimeName
from ..domain.ci_history import (
    CiAttempt,
    append_attempt,
    ci_history_payload,
    parse_ci_history,
)
from ..domain.world import normalize_predicted_paths
from ..domain.actions import Target
from ..domain.ports import WorkflowControlError

# Historical control files remain stopped-import evidence. Runtime cleanup
# removes only the explicit advisory subsets; SQLite owns lifecycle state.
_ISSUE_ADVISORY_SUFFIXES = ("jira", "jira-phase", "predicted-paths", "ci-history")
_PR_ADVISORY_SUFFIXES = ("automerge",)

@dataclass(frozen=True)
class Store:
    """Filesystem advisory context and the retained Jira counter exception."""

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
    # advisory reads preserve byte-damaged notes. Control records use strict
    # decoding so a damaged marker cannot grant a fresh attempt.
    def _read(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        except (FileNotFoundError, OSError):
            return ""

    def _read_control(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8") as stream:
                # Control files hold scalar markers or small typed records;
                # advisory prose has a separate read contract.
                raw = stream.read(65537)
                if len(raw) > 65536:
                    raise WorkflowControlError(path, "control record exceeds 65536-character limit")
                return raw.strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise WorkflowControlError(path, f"unreadable control record: {exc}") from exc

    def _write(self, path: str, value: str) -> None:
        # review M3: pid-suffix the tmp name — two daemons sharing a STATE_DIR
        # (or a restarted process racing its predecessor's in-flight write)
        # must not collide on one shared "<path>.tmp" and truncate each
        # other's write mid-flight. flush()+fsync() before os.replace closes
        # the crash window where a torn/zero-length tmp file (data still in
        # the page cache, not on disk) could get installed at `path` by a
        # power loss / kill between write() and replace() — see _read_int's
        # comment for which counter that silently resets.
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

    def _read_int(self, path: str, default: int = 0) -> int:
        raw = self._read_control(path)
        if raw is None:
            return default
        return self._counter(raw, path)

    @staticmethod
    def _counter(raw: str, path: str) -> int:
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
            raise WorkflowControlError(path, "malformed nonnegative counter; repair the record")
        value = int(raw)
        if value > 2**63 - 1:
            raise WorkflowControlError(path, "counter exceeds the supported integer range")
        return value

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

    def clear_ci_history(self, issue: int) -> None:
        """Discard advisory context only after SQL closes the selected CI episode."""
        self._remove(self.issue_path(issue, "ci-history"))

    # ── CI attempt history (tauri PR #620) ──────────────────────────────────
    # Bounded prompt context, separate from SQL eligibility and debt.
    def get_ci_history(self, issue: int) -> tuple[CiAttempt, ...]:
        """Recorded FIX_CI attempts, oldest first — () when absent or corrupt."""
        try:
            raw = json.loads(self._read(self.issue_path(issue, "ci-history")))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        return parse_ci_history(raw)

    def append_ci_attempt(
        self, issue: int, attempt: CiAttempt
    ) -> tuple[CiAttempt, ...]:
        """Record one dispatched attempt, keeping the last CI_HISTORY_LIMIT.

        Returns the persisted history so a caller can report what it wrote
        rather than re-read it."""
        history = append_attempt(self.get_ci_history(issue), attempt)
        self._write(
            self.issue_path(issue, "ci-history"),
            json.dumps(ci_history_payload(history)),
        )
        return history

    # Jira retains its existing counter and ceiling policy. The stopped review
    # activation reader also observes it once; selected fix/steward episodes
    # neither consume nor update this counter.
    def get_infra_deaths(self, key: int) -> int:
        return self._read_int(self.issue_path(key, "infra-deaths"))

    def bump_infra_deaths(self, key: int) -> int:
        path = self.issue_path(key, "infra-deaths")
        n = self._read_int(path) + 1
        self._write(path, str(n))
        return n

    def set_infra_deaths(self, key: int, count: int) -> None:
        """Write the counter directly, for a death that is worth more than one
        life. A DETERMINISTIC death (#13) reproduces on every retry, so charging
        it 1 buys the identical unwinnable dispatch cap-1 more times; the
        dispatcher saturates the counter instead and the role's existing ceiling
        escalates on the next poll. Monotonic at the call site (max of current
        and the cap) — a saturate must never lower a tally."""
        self._write(self.issue_path(key, "infra-deaths"), str(max(0, int(count))))

    # ── stall watchdog markers (no-silent-forever) ──
    # (signature, since_iso) per watched target: the observable state last
    # seen and when it first appeared. Written only when the signature
    # changes, so a stable target costs zero writes per poll.
    def get_stall_marker(self, kind: str, number: int) -> tuple[str, str]:
        path = (
            self.pr_path(number, "stall-sig")
            if kind == "pr"
            else self.issue_path(number, "stall-sig")
        )
        raw = self._read(path)
        if not raw:
            return ("", "")
        try:
            data = json.loads(raw)
        except ValueError:
            return ("", "")
        if not isinstance(data, dict):
            return ("", "")
        sig, since = data.get("sig", ""), data.get("since", "")
        return (sig, since) if isinstance(sig, str) and isinstance(since, str) else ("", "")

    def set_stall_marker(
        self, kind: str, number: int, sig: str, since: str
    ) -> None:
        path = (
            self.pr_path(number, "stall-sig")
            if kind == "pr"
            else self.issue_path(number, "stall-sig")
        )
        self._write(path, json.dumps({"sig": sig, "since": since}))

    # ── Jira mirror (JIRA_SYNC): the ticket key + the last phase we pushed ──
    def get_jira_key(self, issue: int) -> str:
        return self._read(self.issue_path(issue, "jira"))

    def set_jira_key(self, issue: int, key: str) -> None:
        if key:
            self._write(self.issue_path(issue, "jira"), key)

    def get_jira_phase(self, issue: int) -> str:
        return self._read(self.issue_path(issue, "jira-phase"))

    def set_jira_phase(self, issue: int, phase: str) -> None:
        self._write(self.issue_path(issue, "jira-phase"), phase)

    # ── advisory cleanup ──
    def _clear_issue_advisory(self, issue: int) -> None:
        """Selected SQL retirement cannot erase stopped control/import evidence."""
        for suffix in _ISSUE_ADVISORY_SUFFIXES:
            self._remove(self.issue_path(issue, suffix))

    def _clear_pr_advisory(self, pr: int) -> None:
        """Retire the obsolete merge hint; durable commands own completion."""
        for suffix in _PR_ADVISORY_SUFFIXES:
            self._remove(self.pr_path(pr, suffix))

    def cleanup_pr(self, pr: int | None, issue: int | None, merged: bool) -> None:
        """Retire advisory files after the selected SQL cleanup.

        Stopped control files remain import evidence. Closed-unmerged requeue
        changes continuity in SQL and clears only dead-PR diagnostic context. An issueless PR
        addresses advisory issue metadata through its original negative key.
        """
        if pr is None and issue is None:
            return  # nothing addressable — the caller has no key at all
        issue_key = issue if issue is not None else -pr  # type: ignore[operator]
        if merged:
            if pr is not None:
                self._clear_pr_advisory(pr)
            self._clear_issue_advisory(issue_key)
        else:
            self.clear_ci_history(issue_key)

# ── worktree .harness/ claims and local test command ──────────────────────────
class HarnessMarkers:
    """Reads non-authoritative `.harness/` claims and the local test command.

    Claims may schedule stricter verification but never prove a gate condition.
    `root` is the worktree path (cwd).
    """

    def __init__(self, root: str):
        self.root = root
        self.dir = os.path.join(root, ".harness")
        # One tracked-check subprocess per marker per instance, not per read:
        # this runs on the PreToolUse hot path (every agent Bash call).
        self._tracked_cache: dict[str, bool] = {}

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _read(self, name: str) -> str:
        if self._tracked(name):
            return ""
        try:
            with open(self._path(name)) as f:
                return f.read()
        except (FileNotFoundError, OSError):
            return ""

    def _tracked(self, name: str) -> bool:
        """A marker TRACKED by git is repo history, not this session's claim —
        and repo history is agent/merge-writable input to the gate's own teeth.
        Keifu #167→#156 (2026-08-01): a committed .harness/test-cmd merged into
        the base and every downstream worktree's trusted red-fail gate silently
        ran another issue's single test. Tracked markers are therefore ignored
        outright (the diff-level deny rule stops NEW commits; this closes the
        read against history that predates it). Unknowable (git failed) reads
        as untracked — the marker is non-authoritative either way."""
        cached = self._tracked_cache.get(name)
        if cached is not None:
            return cached
        try:
            tracked = (
                subprocess.run(
                    [
                        "git", "-C", self.root, "ls-files",
                        "--error-unmatch", f".harness/{name}",
                    ],
                    capture_output=True,
                    timeout=10,
                ).returncode
                == 0
            )
        except Exception:  # noqa: BLE001 — unknowable ⇒ preserve old behaviour
            tracked = False
        self._tracked_cache[name] = tracked
        return tracked

    def visual_ac(self) -> bool:
        """The agent declared a visual acceptance criterion (touched .harness/visual-ac),
        so the PR gate requires a committed + read screenshot."""
        return os.path.exists(self._path("visual-ac")) and not self._tracked(
            "visual-ac"
        )

    def test_cmd(self) -> str | None:
        """First non-blank line of .harness/test-cmd (prc_resolve_test_cmd's
        marker side). None when absent so the resolver falls back to env."""
        raw = self._read("test-cmd")
        if not raw:
            return None
        for line in raw.replace("\r", "").splitlines():
            if line.strip():
                return line.strip()
        return None
