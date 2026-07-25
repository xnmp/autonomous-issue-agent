"""Filesystem state — the STATE_DIR files + the worktree `.harness/` markers.

Replaces the scattered `cat`/`echo`/`rm -f "$STATE_DIR/issue-N.…"` in the bash
with one typed surface. Two state roots:

  * STATE_DIR (per-issue / per-PR daemon state): session ids, resume / ci /
    review retry counters, the resumed-at reply mark, the automerge marker.
    These feed FixState / ReviewState / TriageState in the domain.
  * worktree `.harness/` (the agent's own on-disk declaration the gate reads):
    ac-posted, test-plan-posted, test-cmd, adversarial-verdict. The agent writes
    these via Bash; the gate (and the GateContext builder) reads them.

Path construction is pure (testable without touching disk); the read/write
helpers are thin. Mirrors cleanup.sh's state-file set so teardown stays in sync.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Store:
    """STATE_DIR-rooted per-issue/PR daemon state. `state_dir` is the bash
    STATE_DIR (fixer) or REVIEW_STATE_DIR (reviewer) or SDIR (steward)."""

    state_dir: str

    # ── pure path helpers ──
    def issue_path(self, issue: int, suffix: str) -> str:
        return os.path.join(self.state_dir, f"issue-{issue}.{suffix}")

    def pr_path(self, pr: int, suffix: str) -> str:
        return os.path.join(self.state_dir, f"pr-{pr}.{suffix}")

    def triage_at_path(self, issue: int) -> str:
        return os.path.join(self.state_dir, f"triage-{issue}.at")

    # ── generic thin IO ──
    def _read(self, path: str) -> str:
        try:
            with open(path) as f:
                return f.read().strip()
        except (FileNotFoundError, OSError):
            return ""

    def _write(self, path: str, value: str) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(value)
        os.replace(tmp, path)

    def _remove(self, path: str) -> None:
        try:
            os.remove(path)
        except (FileNotFoundError, OSError):
            pass

    def _read_int(self, path: str, default: int = 0) -> int:
        raw = self._read(path)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    # ── sessions (issue: resolve/resume/address-review; pr: review) ──
    def get_session(self, issue: int) -> str:
        return self._read(self.issue_path(issue, "session"))

    def save_session(self, issue: int, session_id: str) -> None:
        if session_id:
            self._write(self.issue_path(issue, "session"), session_id)

    def has_session(self, issue: int) -> bool:
        return bool(self.get_session(issue))

    def get_pr_session(self, pr: int) -> str:
        return self._read(self.pr_path(pr, "session"))

    def save_pr_session(self, pr: int, session_id: str) -> None:
        if session_id:
            self._write(self.pr_path(pr, "session"), session_id)

    # ── resume reply mark + counter ──
    def get_resumed_at(self, issue: int) -> str:
        return self._read(self.issue_path(issue, "resumed-at"))

    def set_resumed_at(self, issue: int, at: str) -> None:
        self._write(self.issue_path(issue, "resumed-at"), at)

    def bump_resumes(self, issue: int) -> int:
        path = self.issue_path(issue, "resumes")
        n = self._read_int(path) + 1
        self._write(path, str(n))
        return n

    # ── retry counters ──
    def get_ci_retries(self, issue: int) -> int:
        return self._read_int(self.issue_path(issue, "ci-retries"))

    def set_ci_retries(self, issue: int, n: int) -> None:
        self._write(self.issue_path(issue, "ci-retries"), str(n))

    def get_resolve_retries(self, issue: int) -> int:
        return self._read_int(self.issue_path(issue, "resolve-retries"))

    def set_resolve_retries(self, issue: int, n: int) -> None:
        self._write(self.issue_path(issue, "resolve-retries"), str(n))

    def get_rebase_retries(self, issue: int) -> int:
        return self._read_int(self.issue_path(issue, "rebase-retries"))

    def set_rebase_retries(self, issue: int, n: int) -> None:
        self._write(self.issue_path(issue, "rebase-retries"), str(n))

    def get_review_retry(self, issue: int) -> tuple[str, int]:
        """(review_at, count). Bash stored "<at> <n>" in issue-N.review-retries."""
        raw = self._read(self.issue_path(issue, "review-retries"))
        if not raw:
            return "", 0
        at, _, n = raw.rpartition(" ")
        try:
            return at, int(n or "0")
        except ValueError:
            return "", 0

    def set_review_retry(self, issue: int, at: str, n: int) -> None:
        self._write(self.issue_path(issue, "review-retries"), f"{at} {n}")

    def clear_review_retry(self, issue: int) -> None:
        self._remove(self.issue_path(issue, "review-retries"))

    # ── pr review head-sha + round ──
    def get_last_sha(self, pr: int) -> str:
        return self._read(self.pr_path(pr, "last-sha"))

    def set_last_sha(self, pr: int, sha: str) -> None:
        self._write(self.pr_path(pr, "last-sha"), sha)

    def get_round(self, pr: int) -> int:
        return self._read_int(self.pr_path(pr, "round"), default=1)

    def set_round(self, pr: int, n: int) -> None:
        self._write(self.pr_path(pr, "round"), str(n))

    # ── automerge marker ──
    def automerge_enabled(self, pr: int) -> bool:
        return os.path.exists(self.pr_path(pr, "automerge"))

    def mark_automerge(self, pr: int) -> None:
        self._write(self.pr_path(pr, "automerge"), "")

    # ── triage updatedAt baseline ──
    def get_triage_at(self, issue: int) -> str:
        return self._read(self.triage_at_path(issue))

    def set_triage_at(self, issue: int, at: str) -> None:
        self._write(self.triage_at_path(issue), at)

    # ── author/implementer phase (AUTHOR_TESTS_SPLIT) ──
    # "" (no file) = not started / single-pass; "authoring" = author leg ran and is
    # blocked awaiting a human; "authored" = tests written, implementer leg is next.
    def get_phase(self, issue: int) -> str:
        return self._read(self.issue_path(issue, "phase"))

    def set_phase(self, issue: int, phase: str) -> None:
        self._write(self.issue_path(issue, "phase"), phase)

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

    # ── cleanup (mirror cleanup.sh's rm -f set) ──
    def clear_issue(self, issue: int) -> None:
        for suffix in ("session", "ci-retries", "resumes", "resumed-at",
                       "automerge", "review-retries", "resolve-retries",
                       "rebase-retries", "phase", "jira", "jira-phase"):
            self._remove(self.issue_path(issue, suffix))

    def clear_pr_review(self, pr: int) -> None:
        for suffix in ("session", "last-sha", "round"):
            self._remove(self.pr_path(pr, suffix))


# ── worktree .harness/ markers (the agent's on-disk declaration) ──────────────
class HarnessMarkers:
    """Reads the `.harness/` markers the agent writes in its worktree. Used by
    the GateContext builder. `root` is the worktree path (cwd)."""

    def __init__(self, root: str):
        self.dir = os.path.join(root, ".harness")

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _read(self, name: str) -> str:
        try:
            with open(self._path(name)) as f:
                return f.read()
        except (FileNotFoundError, OSError):
            return ""

    def ac_posted(self) -> bool:
        return os.path.exists(self._path("ac-posted"))

    def test_plan_posted(self) -> bool:
        return os.path.exists(self._path("test-plan-posted"))

    def adversarial_verdict(self) -> str:
        return self._read("adversarial-verdict").strip()

    def visual_ac(self) -> bool:
        """The agent declared a visual acceptance criterion (touched .harness/visual-ac),
        so the PR gate requires a committed + read screenshot."""
        return os.path.exists(self._path("visual-ac"))

    def screenshots_read(self) -> tuple[str, ...]:
        """Basenames of evidence screenshots the agent actually READ (recorded by the
        PostToolUse(Read) hook, one per line in .harness/screenshots-read)."""
        return tuple(
            ln.strip() for ln in self._read("screenshots-read").splitlines() if ln.strip()
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
