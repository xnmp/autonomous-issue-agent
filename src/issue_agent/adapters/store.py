"""ADR 0009 workflow continuity and low-authority worktree claims.

Replaces the scattered `cat`/`echo`/`rm -f "$STATE_DIR/issue-N.…"` in the bash
with one typed surface. Two state roots:

  * STATE_DIR (per-issue / per-PR daemon state): session ids, resume / ci /
    review retry counters, the resumed-at reply mark, the automerge marker.
    These feed FixState / ReviewState / TriageState in the domain.
  * worktree `.harness/` (non-authoritative agent declarations): visual-ac,
    visual-ac and test-cmd. Claims can schedule verification or select
    a command, but never supply an authorization verdict.

Path construction is pure (testable without touching disk); the read/write
helpers are thin. Mirrors cleanup.sh's state-file set so teardown stays in sync.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from ..domain.agent_task import RuntimeName
from ..domain.ci import parse_timestamp
from ..domain.ci_history import (
    CiAttempt,
    append_attempt,
    ci_history_payload,
    parse_ci_history,
)
from ..domain.world import normalize_predicted_paths
from ..domain.actions import Target
from ..domain.ports import WorkflowControlError

# ── suffix universe (review M4/D8) ──────────────────────────────────────────
# Every accessor below writes `issue-N.<suffix>` or `pr-N.<suffix>` via
# issue_path/pr_path. These two tuples are the SAME suffix universe the
# accessors use — clear_issue/clear_pr_state derive from them instead of
# hand-maintaining a parallel list, and
# tests/adapters/test_store.py::test_every_*_accessor_suffix_is_covered_by_*
# introspects the module source to assert every literal suffix an accessor
# passes to issue_path/pr_path appears in exactly one of these — so a new
# accessor with an unlisted suffix fails a test instead of silently leaking a
# STATE_DIR file forever. Retired review session/round files remain stopped
# import evidence; runtime cleanup cannot alter them.
#
# `automerge` is a PR suffix — this is the M4 fix. It used to live in the issue
# tuple (`issue-N.automerge`), which nothing ever wrote (mark_automerge/
# automerge_enabled always used pr_path), so clear_issue's removal was a no-op
# and the real pr-N.automerge marker was cleared by NEITHER cleaner.
#
# The retired resolve files (`resolve-retries`, `last-error`) have no accessor
# and no cleaner: resolve episodes live in SQLite (ADR 0015), and any stopped
# file is retained as import evidence read only by adapters/resolve_activation.
_ISSUE_SUFFIXES = (
    "session", "resumed-at", "resumes", "ci-retries", "ci-history",
    "machine-block", "budget-block",
    "self-clears", "self-clear-ceiling",
    "rebase-retries", "infra-deaths", "review-retries", "phase", "jira",
    "jira-phase", "model", "predicted-paths", "branch", "stall-sig",
    "steward-stalls-triage",
)
_PR_SUFFIXES = (
    "automerge", "ci-rerun-head",
    "ci-cancel-head",
    "update-branch-head", "ci-pending-escalated-head", "resumed-at",
    "stall-sig",
)

# Closed-unmerged requeue cleanup (review M4/D8, handover §5.7): only the
# resume-blocking markers are cleared — infra-deaths (and the SQL resolve
# episode) are DELIBERATELY kept (fewer retries after a human closed the PR). This is a
# strict subset of _ISSUE_SUFFIXES, not a third partition member, so it does
# not participate in the "exactly one cleaner" coverage test above.
#
# `ci-history` joins them for the same reason the session does: the requeue's
# next lifecycle opens a NEW PR, and an attempt list describing a dead PR's head
# SHAs would tell that PR's first repair leg it is on attempt four of a loop it
# has never been in. Unlike resolve-retries it is context, not budget, so
# carrying it forward has no deterrent value — only a wrong diagnosis.
_RESUME_STATE_SUFFIXES = ("session", "phase", "resumed-at", "ci-history")

# Operator unblock (`clear_retries`): the per-attempt ceilings only. Also a
# strict subset of _ISSUE_SUFFIXES — infra-deaths is deliberately absent, see
# clear_retries.
_RETRY_SUFFIXES = ("ci-retries", "review-retries", "rebase-retries")


@dataclass(frozen=True)
class Store:
    """STATE_DIR-rooted per-issue/PR daemon state. `state_dir` is the bash
    STATE_DIR (fixer) or REVIEW_STATE_DIR (reviewer) or SDIR (steward)."""

    state_dir: str
    # The agent runtime whose sessions this store may hand out. A session id is
    # only resumable by the runtime that created it — `codex exec resume
    # <claude-uuid>` dies in 0.3s with "no rollout found" (observed live on
    # REBASE #541, 2026-07-27, the first dispatch after FIXER_RUNTIME flipped
    # claude→codex). Sessions are therefore saved tagged (`<runtime>:<id>`) and
    # a read under a DIFFERENT runtime returns "" — the leg cold-starts, which
    # every resume-shaped leg supports. Legacy untagged files (bare uuid) read
    # as "claude", the only runtime that existed when they were written.
    runtime: str | RuntimeName = RuntimeName.CLAUDE.value

    def _runtime_tag(self) -> str:
        return (
            self.runtime.value
            if isinstance(self.runtime, RuntimeName)
            else self.runtime
        )

    # ── pure path helpers ──
    def issue_path(self, issue: int, suffix: str) -> str:
        return os.path.join(self.state_dir, f"issue-{issue}.{suffix}")

    def pr_path(self, pr: int, suffix: str) -> str:
        return os.path.join(self.state_dir, f"pr-{pr}.{suffix}")

    def triage_at_path(self, issue: int) -> str:
        return os.path.join(self.state_dir, f"triage-{issue}.at")

    def groom_at_path(self, issue: int) -> str:
        return os.path.join(self.state_dir, f"groom-{issue}.at")

    def groom_content_path(self, issue: int) -> str:
        return os.path.join(self.state_dir, f"groom-{issue}.content")

    # ── generic thin IO ──
    # Encoding is PINNED, never the process locale: most values here are ASCII
    # ids and counters, but advisory history may contain Unicode,
    # and a daemon started under a C/POSIX locale would otherwise fail to write
    # (or mis-read) the same file a UTF-8 shell wrote. `errors="replace"` on the
    # advisory reads preserve byte-damaged notes. Control records use strict
    # decoding so a damaged marker cannot grant a fresh attempt.
    def _read(self, path: str) -> str:
        advisory = path.rsplit(".", 1)[-1] in {
            "ci-history", "jira", "jira-phase",
            "predicted-paths", "stall-sig",
        }
        if not advisory:
            raw = self._read_control(path)
            if raw == "" and path.rsplit(".", 1)[-1] in {
                "session", "resumed-at", "at", "content",
                "ci-rerun-head", "ci-cancel-head", "update-branch-head",
                "ci-pending-escalated-head", "self-clear-ceiling",
            }:
                raise WorkflowControlError(path, "empty control record; repair or explicitly remove it")
            return raw or ""
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

    def _read_timestamp(self, path: str) -> str:
        raw = self._read_control(path)
        if raw is None:
            return ""
        if parse_timestamp(raw) is None:
            raise WorkflowControlError(path, "malformed consumed-reply timestamp")
        return raw

    @staticmethod
    def _integer(value: object, path: str, *, signed: bool = False) -> int:
        if type(value) is not int or not (-(2**63) if signed else 0) <= value < 2**63:
            raise WorkflowControlError(path, "malformed integer in structured control record")
        return value

    def get_branch(self, issue: int) -> str:
        return self._read(self.issue_path(issue, "branch"))

    def save_branch(self, issue: int, branch: str) -> None:
        self._write(self.issue_path(issue, "branch"), branch)

    # ── sessions (issue: resolve/resume/address-review; pr: review) ──
    def _session_for_runtime(self, raw: str) -> str:
        """The stored id iff the ACTIVE runtime created it, else "" (cold
        start). Bare legacy ids are claude's."""
        if not raw:
            return ""
        tag, _, sid = raw.partition(":")
        if not sid:  # no ":" — legacy bare id, written before tagging existed
            tag, sid = "claude", raw
        runtime = self._runtime_tag()
        # Older composition roots interpolated RuntimeName directly and wrote
        # `RuntimeName.CODEX:<value>`. Accept that deployed shape while every
        # new write uses the stable enum value (`codex:<value>`).
        legacy_enum_tag = f"RuntimeName.{runtime.upper()}"
        return sid if tag in {runtime, legacy_enum_tag} else ""

    def get_session(self, issue: int) -> str:
        return self._session_for_runtime(self._read(self.issue_path(issue, "session")))

    def save_session(self, issue: int, session_id: str) -> None:
        if session_id:
            self._write(
                self.issue_path(issue, "session"),
                f"{self._runtime_tag()}:{session_id}",
            )

    def has_session(self, issue: int) -> bool:
        return bool(self.get_session(issue))

    # ── routed model (issue: the model the resolve family actually ran) ──
    # Repair legs resume the resolve's session; without this marker they fell
    # back to the configured default model (#21), silently swapping the
    # reasoner under the routed transcript. Same runtime-tagging discipline as
    # sessions: a model routed for one runtime must never leak into another.
    def get_model(self, issue: int) -> str:
        return self._session_for_runtime(self._read(self.issue_path(issue, "model")))

    def save_model(self, issue: int, model: str | None) -> None:
        if model:
            self._write(
                self.issue_path(issue, "model"),
                f"{self._runtime_tag()}:{model}",
            )

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

    # ── resume reply mark + counter ──
    def get_resumed_at(self, issue: int) -> str:
        return self._read_timestamp(self.issue_path(issue, "resumed-at"))

    def set_resumed_at(self, issue: int, at: str) -> None:
        self._write(self.issue_path(issue, "resumed-at"), at)

    def bump_resumes(self, issue: int) -> int:
        path = self.issue_path(issue, "resumes")
        n = self._read_int(path) + 1
        self._write(path, str(n))
        return n

    # ── retry counters ──
    def clear_ci_history(self, issue: int) -> None:
        """Discard advisory context only after SQL closes the selected CI episode."""
        self._remove(self.issue_path(issue, "ci-history"))

    # ── CI attempt history (tauri PR #620) ──────────────────────────────────
    # The counter above says HOW MANY repair legs ran; this says what each of
    # them was looking at. Provenance and bounding: see domain/ci_history.py.
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

    # ── infra-death ceiling (review C3/D2c) ──
    # NOT a retry counter: a session that dies before returning a result made no
    # attempt, so it must never march ci-/review-/rebase-/resolve-retries (that
    # exemption is FAILURE-MODES #58). It gets its own, higher ceiling instead,
    # because "the session keeps dying" is itself a paid loop. `key` is the same
    # key the leg's retry counter uses: the issue number, or the NEGATED PR
    # number for a PR with no resolvable issue (domain.fix.breaker_key).
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

    def clear_infra_deaths(self, key: int) -> None:
        """Decay: this key just produced a RETURNED result, so the transport and
        the process are demonstrably alive (review P1R-3/P0R-3 residual).

        The counter bounds CONSECUTIVE deaths — "the session keeps dying" — and
        without a reset it is a lifetime tally instead: five unrelated deaths
        spread over a month of otherwise healthy work eventually walk a healthy
        issue to needs-human-review, and the cleanup path only clears it on a
        MERGED PR, which is precisely the issue that never got there. A key that
        never returns a result never reaches this, so the loop it was tuned for
        still escalates.

        Uses `_remove` rather than writing "0": absent and zero already read
        identically (`_read_int` defaults to 0), and not creating a file is the
        only form that cannot leave a torn one behind."""
        self._remove(self.issue_path(key, "infra-deaths"))




    def clear_retries(self, key: int) -> int:
        """Operator unblock: give the lifecycle its per-attempt budget back.
        Returns how many counters were actually retired, so a caller can report
        what it did rather than what it attempted.

        Deliberately partial, on the same conservatism as
        `clear_issue_resume_state` (handover §5.7): resolve-retries and
        infra-deaths survive. A human deciding to retry does not make a
        deterministically dying session or an unresolvable issue cheaper, and
        those two counters are the only ceilings that bound those loops.

        `key` is `domain.fix.breaker_key`'s key — the issue number, or the
        NEGATED PR number for a PR with no resolvable issue.
        """
        removed = 0
        for suffix in _RETRY_SUFFIXES:
            path = self.issue_path(key, suffix)
            removed += os.path.exists(path)
            self._remove(path)
        # The CI attempt history annotates the ci-retries counter, so it is
        # retired with it — an operator handing back the budget is saying the
        # previous attempts should not steer the next one either. Deliberately
        # NOT counted in `removed`: that number reports retired ceilings, and
        # inflating it would misreport how much budget the unblock handed back.
        self._remove(self.issue_path(key, "ci-history"))
        return removed

    # PR → head a branch-refresh (advisory-UNSTABLE arm) was last dispatched
    # for. Same head-keyed shape and same write-before-execute ordering as the
    # ci-rerun marker above: refresh-but-not-marked is an unbounded push loop,
    # marked-but-not-refreshed costs one retry next poll.
    def get_update_branch_head(self, pr: int) -> str:
        return self._read(self.pr_path(pr, "update-branch-head"))

    def set_update_branch_head(self, pr: int, sha: str) -> None:
        if sha:
            self._write(self.pr_path(pr, "update-branch-head"), sha)

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
        return (str(data.get("sig", "")), str(data.get("since", "")))

    def set_stall_marker(
        self, kind: str, number: int, sig: str, since: str
    ) -> None:
        path = (
            self.pr_path(number, "stall-sig")
            if kind == "pr"
            else self.issue_path(number, "stall-sig")
        )
        self._write(path, json.dumps({"sig": sig, "since": since}))

    def get_ci_pending_escalated_head(self, pr: int) -> str:
        return self._read(self.pr_path(pr, "ci-pending-escalated-head"))

    def set_ci_pending_escalated_head(self, pr: int, sha: str) -> None:
        if sha:
            self._write(self.pr_path(pr, "ci-pending-escalated-head"), sha)

    # ── automerge marker ──
    def automerge_enabled(self, pr: int) -> bool:
        return self._read_control(self.pr_path(pr, "automerge")) is not None

    def mark_automerge(self, pr: int) -> None:
        self._write(self.pr_path(pr, "automerge"), "")

    # ── triage updatedAt baseline ──
    def get_triage_at(self, issue: int) -> str:
        """Stopped legacy-source observation; runtime continuity lives in SQLite."""
        return self._read(self.triage_at_path(issue))

    def get_groom_at(self, issue: int) -> str:
        """Stopped legacy-source observation; runtime continuity lives in SQLite."""
        return self._read(self.groom_at_path(issue))

    def get_groom_content(self, issue: int) -> str:
        """Stopped legacy-source observation; runtime continuity lives in SQLite."""
        return self._read(self.groom_content_path(issue))

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
        """Full issue-state retirement. Derives from `_ISSUE_SUFFIXES` (review
        M4) so a suffix added anywhere above and forgotten here fails
        test_store.py's coverage test instead of leaking a file forever.

        Also retires the steward's triage baseline (`triage-N.at`, via
        `triage_at_path`; review P1R-16) — a suffix-less namespace that
        `_ISSUE_SUFFIXES` can't carry (triage_at_path takes no suffix
        argument), and that consequently had NO cleaner at all before this."""
        for suffix in _ISSUE_SUFFIXES:
            self._remove(self.issue_path(issue, suffix))
        self._remove(self.triage_at_path(issue))

    def clear_pr_state(self, pr: int) -> None:
        """Retire current file-backed PR state, preserving stopped review evidence.

        `automerge` is a PR suffix — it used to live in `clear_issue`, which
        cleared a file nothing wrote, while the real pr-N.automerge marker was
        cleared by neither cleaner.
        """
        for suffix in _PR_SUFFIXES:
            self._remove(self.pr_path(pr, suffix))

    def clear_issue_resume_state(self, issue: int) -> None:
        """Closed-unmerged requeue's conservative partial clear (review M4/D8):
        drop only the resume-blocking markers (session/phase/resumed-at) so
        the next lifecycle doesn't try to resume a dead PR's session — but
        DELIBERATELY keep resolve-retries and infra-deaths (handover §5.7:
        fewer retries after a human closed the PR is intentional, not a bug)."""
        for suffix in _RESUME_STATE_SUFFIXES:
            self._remove(self.issue_path(issue, suffix))

    def cleanup_pr(self, pr: int | None, issue: int | None, merged: bool) -> None:
        """Single entry point the Executor's `ClearIssueState` action wiring
        calls (review M4: `clear_issue`/`clear_pr_review` had ZERO production
        call sites — a closed-unmerged PR requeued its issue while
        issue-N.session/.phase survived, so the next lifecycle resumed a dead
        PR's session with a preloaded breaker).

        merged=True  → retire everything: full issue state (incl.
                       infra-deaths) AND the PR's own state (incl. the
                       pr-N.automerge marker).
        merged=False → closed-unmerged requeue: only the resume-blocking
                       markers on the issue are cleared; resolve-retries/
                       infra-deaths persist on purpose (handover §5.7). PR-side
                       state is left alone — the PR is dead, not reused.

        `issue=None` (review P1R-16): `domain.fix.breaker_key` falls back to
        the NEGATED pr number for a PR with no resolvable issue, so counters
        such as infra-deaths for that PR accumulate at `issue_path(-pr, …)` —
        e.g. `issue--112.infra-deaths` — a namespace this method used to leave
        untouched forever (it only ever cleared `pr-N.*`, and the "exactly one
        cleaner" coverage test was blind to the gap because it introspects
        SUFFIXES, not key namespaces: `issue-9.infra-deaths` and
        `issue--112.infra-deaths` share the same suffix). Route the negated
        key through the SAME clear_issue/clear_issue_resume_state helpers a
        real issue gets, instead of a bespoke clear, so any suffix later added
        to `_ISSUE_SUFFIXES` stays covered here automatically too.

        `pr=None` (#28) is the mirror: the issue-driven cleanup arm repairs an
        issue whose PR has vanished from the world, so there is no `pr-N.*`
        namespace to retire — the issue's half runs alone. `issue` is then
        necessarily set (the arm is keyed on an issue), so no `-pr` fallback key
        is needed either."""
        if pr is None and issue is None:
            return  # nothing addressable — the caller has no key at all
        issue_key = issue if issue is not None else -pr  # type: ignore[operator]
        if merged:
            if pr is not None:
                self.clear_pr_state(pr)
            self.clear_issue(issue_key)
        else:
            self.clear_issue_resume_state(issue_key)


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
