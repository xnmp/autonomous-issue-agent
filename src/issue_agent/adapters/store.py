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

import json
import os
from dataclasses import dataclass

from ..domain.actions import (
    Action,
    CloseIssue,
    EscalateCiPending,
    PostComment,
    Relabel,
    Target,
)
from ..domain.labels import Label
from ..domain.world import normalize_predicted_paths

# ── suffix universe (review M4/D8) ──────────────────────────────────────────
# Every accessor below writes `issue-N.<suffix>` or `pr-N.<suffix>` via
# issue_path/pr_path. These two tuples are the SAME suffix universe the
# accessors use — clear_issue/clear_pr_review derive from them instead of
# hand-maintaining a parallel list, and
# tests/adapters/test_store.py::test_every_*_accessor_suffix_is_covered_by_*
# introspects the module source to assert every literal suffix an accessor
# passes to issue_path/pr_path appears in exactly one of these — so a new
# accessor with an unlisted suffix fails a test instead of silently leaking a
# STATE_DIR file forever. `session` legitimately appears in BOTH: issue-N.session
# (resolve/resume/address-review) and pr-N.session (review) are different files
# in different namespaces, not a collision.
#
# `automerge` is a PR suffix — this is the M4 fix. It used to live in the issue
# tuple (`issue-N.automerge`), which nothing ever wrote (mark_automerge/
# automerge_enabled always used pr_path), so clear_issue's removal was a no-op
# and the real pr-N.automerge marker was cleared by NEITHER cleaner.
_ISSUE_SUFFIXES = (
    "session", "resumed-at", "resumes", "ci-retries", "resolve-retries",
    "rebase-retries", "infra-deaths", "review-retries", "phase", "jira",
    "jira-phase", "model", "predicted-paths",
)
_PR_SUFFIXES = (
    "session", "last-sha", "round", "automerge", "patch-id", "ci-rerun-head",
    "ci-pending-escalated-head", "resumed-at",
)

# Closed-unmerged requeue cleanup (review M4/D8, handover §5.7): only the
# resume-blocking markers are cleared — resolve-retries and infra-deaths are
# DELIBERATELY kept (fewer retries after a human closed the PR). This is a
# strict subset of _ISSUE_SUFFIXES, not a third partition member, so it does
# not participate in the "exactly one cleaner" coverage test above.
_RESUME_STATE_SUFFIXES = ("session", "phase", "resumed-at")


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
    runtime: str = "claude"

    # ── pure path helpers ──
    def issue_path(self, issue: int, suffix: str) -> str:
        return os.path.join(self.state_dir, f"issue-{issue}.{suffix}")

    def pr_path(self, pr: int, suffix: str) -> str:
        return os.path.join(self.state_dir, f"pr-{pr}.{suffix}")

    def triage_at_path(self, issue: int) -> str:
        return os.path.join(self.state_dir, f"triage-{issue}.at")

    def pending_actions_path(self) -> str:
        """Role-wide lifecycle queue.

        This is deliberately not an issue/PR suffix: one daemon owns the queue,
        and issue cleanup must never erase an unacknowledged GitHub write.
        """
        return os.path.join(self.state_dir, "pending-actions.json")

    def quarantined_actions_path(self) -> str:
        """Lifecycle writes that remained unverifiable after queued replay."""
        return os.path.join(self.state_dir, "quarantined-actions.json")

    # ── generic thin IO ──
    def _read(self, path: str) -> str:
        try:
            with open(path) as f:
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
        # power loss / kill between write() and replace() — see _read_int's
        # comment for which counter that silently resets.
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _remove(self, path: str) -> None:
        try:
            os.remove(path)
        except (FileNotFoundError, OSError):
            pass

    def _read_int(self, path: str, default: int = 0) -> int:
        # review M3: an empty/zero-length file reads back as `default` here —
        # the one counter this bites is resolve-retries, whose entire purpose
        # is stopping an infinite loop. Before the fsync above, a power loss
        # mid-write could leave exactly this (a torn, zero-length file) and
        # silently reset a tripped breaker to a full budget; fsync narrows the
        # crash window to "before os.replace", where the OLD value still reads
        # back intact instead.
        raw = self._read(path)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    # ── durable terminal lifecycle writes (#64) ──
    @staticmethod
    def _encode_pending_action(action: Action) -> dict[str, object]:
        if isinstance(action, Relabel):
            return {
                "type": "relabel",
                "target": action.target.name,
                "number": action.number,
                "remove": action.remove.value if action.remove else None,
                "add": action.add.value if action.add else None,
            }
        if isinstance(action, PostComment):
            return {
                "type": "comment",
                "target": action.target.name,
                "number": action.number,
                "body": action.body,
            }
        if isinstance(action, CloseIssue):
            return {
                "type": "close",
                "number": action.number,
                "reason": action.reason,
                "comment": action.comment,
            }
        if isinstance(action, EscalateCiPending):
            return {
                "type": "escalate_ci_pending",
                "pr": action.pr,
                "head": action.head,
                "body": action.body,
            }
        raise TypeError(f"action is not a durable lifecycle write: {action!r}")

    @staticmethod
    def _decode_pending_action(raw: object) -> Action:
        if not isinstance(raw, dict):
            raise ValueError("pending lifecycle action must be an object")
        kind = raw.get("type")
        try:
            if kind == "relabel":
                remove = raw.get("remove")
                add = raw.get("add")
                return Relabel(
                    Target[str(raw["target"])],
                    int(raw["number"]),
                    Label(str(remove)) if remove is not None else None,
                    Label(str(add)) if add is not None else None,
                )
            if kind == "comment":
                return PostComment(
                    Target[str(raw["target"])],
                    int(raw["number"]),
                    str(raw["body"]),
                )
            if kind == "close":
                return CloseIssue(
                    int(raw["number"]),
                    str(raw.get("reason") or "completed"),
                    str(raw.get("comment") or ""),
                )
            if kind == "escalate_ci_pending":
                return EscalateCiPending(
                    int(raw["pr"]), str(raw["head"]), str(raw["body"])
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid pending lifecycle action: {raw!r}") from exc
        raise ValueError(f"unknown pending lifecycle action type: {kind!r}")

    def _load_action_queue(
        self, path: str, *, description: str
    ) -> tuple[Action, ...]:
        try:
            with open(path) as queue:
                raw = queue.read()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise OSError(f"{description} is unreadable: {path}") from exc
        if not raw.strip():
            raise ValueError(f"{description} is empty")
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{description} is malformed") from exc
        if not isinstance(values, list):
            raise ValueError(f"{description} must be a JSON list")
        return tuple(self._decode_pending_action(value) for value in values)

    def _save_action_queue(self, path: str, actions: tuple[Action, ...]) -> None:
        if not actions:
            self._remove(path)
            return
        encoded = [self._encode_pending_action(action) for action in actions]
        self._write(path, json.dumps(encoded, separators=(",", ":"), sort_keys=True))

    def load_pending_actions(self) -> tuple[Action, ...]:
        return self._load_action_queue(
            self.pending_actions_path(),
            description="pending lifecycle queue",
        )

    def save_pending_actions(self, actions: tuple[Action, ...]) -> None:
        self._save_action_queue(self.pending_actions_path(), actions)

    def load_quarantined_actions(self) -> tuple[Action, ...]:
        return self._load_action_queue(
            self.quarantined_actions_path(),
            description="quarantined lifecycle queue",
        )

    def quarantine_pending_action(self, action: Action) -> None:
        quarantined = self.load_quarantined_actions()
        if action not in quarantined:
            self._save_action_queue(
                self.quarantined_actions_path(),
                (*quarantined, action),
            )

    # ── sessions (issue: resolve/resume/address-review; pr: review) ──
    def _session_for_runtime(self, raw: str) -> str:
        """The stored id iff the ACTIVE runtime created it, else "" (cold
        start). Bare legacy ids are claude's."""
        if not raw:
            return ""
        tag, _, sid = raw.partition(":")
        if not sid:  # no ":" — legacy bare id, written before tagging existed
            tag, sid = "claude", raw
        return sid if tag == self.runtime else ""

    def get_session(self, issue: int) -> str:
        return self._session_for_runtime(self._read(self.issue_path(issue, "session")))

    def save_session(self, issue: int, session_id: str) -> None:
        if session_id:
            self._write(
                self.issue_path(issue, "session"), f"{self.runtime}:{session_id}"
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
            self._write(self.issue_path(issue, "model"), f"{self.runtime}:{model}")

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

    def get_pr_session(self, pr: int) -> str:
        return self._session_for_runtime(self._read(self.pr_path(pr, "session")))

    def save_pr_session(self, pr: int, session_id: str) -> None:
        if session_id:
            self._write(self.pr_path(pr, "session"), f"{self.runtime}:{session_id}")

    # ── resume reply mark + counter ──
    def get_resumed_at(self, issue: int) -> str:
        return self._read(self.issue_path(issue, "resumed-at"))

    def set_resumed_at(self, issue: int, at: str) -> None:
        self._write(self.issue_path(issue, "resumed-at"), at)

    def get_pr_resumed_at(self, pr: int) -> str:
        return self._read(self.pr_path(pr, "resumed-at"))

    def set_pr_resumed_at(self, pr: int, at: str) -> None:
        self._write(self.pr_path(pr, "resumed-at"), at)

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

    def set_infra_deaths(self, key: int, n: int) -> None:
        """Write the counter directly, for a death that is worth more than one
        life. A DETERMINISTIC death (#13) reproduces on every retry, so charging
        it 1 buys the identical unwinnable dispatch cap-1 more times; the
        dispatcher saturates the counter instead and the role's existing ceiling
        escalates on the next poll. Monotonic at the call site (max of current
        and the cap) — a saturate must never lower a tally."""
        self._write(self.issue_path(key, "infra-deaths"), str(max(0, int(n))))

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

    # ── reviewed-patch identity (harness #2) ──
    # `domain.review.patch_signature` of the diff the last RECORDED review saw.
    # The head SHA alone cannot tell a real revision from a mechanical rebase, so
    # `last-sha` says WHICH commit was reviewed and this says WHAT was reviewed.
    # Absent ("" — a PR last reviewed before this existed) means "unknown", which
    # the reader must treat as "must review", never as "unchanged".
    def get_last_patch_id(self, pr: int) -> str:
        return self._read(self.pr_path(pr, "patch-id"))

    def set_last_patch_id(self, pr: int, patch_id: str) -> None:
        self._write(self.pr_path(pr, "patch-id"), patch_id)

    # ── free CI re-run marker (#38) ──
    # The head SHA whose ONE free `gh run rerun --failed` has been spent, so a
    # second FAILURE on the same commit buys the paid FIX_CI leg instead of
    # re-running forever. Keyed on the SHA rather than a bare flag: a push is a
    # new head and gets a fresh allowance with no extra bookkeeping, and a
    # re-armed marker can never suppress a genuinely new failure. Written by the
    # executor BEFORE the rerun call (see the RerunFailedChecks arm) — a marker
    # that is set-but-not-rerun costs one paid leg on the next poll, exactly what
    # would have happened anyway, whereas rerun-but-not-marked is an unbounded
    # loop. "" (absent) = never re-run, the sole reading a fresh PR must have.
    def get_ci_rerun_head(self, pr: int) -> str:
        return self._read(self.pr_path(pr, "ci-rerun-head"))

    def set_ci_rerun_head(self, pr: int, sha: str) -> None:
        if sha:
            self._write(self.pr_path(pr, "ci-rerun-head"), sha)

    def get_ci_pending_escalated_head(self, pr: int) -> str:
        return self._read(self.pr_path(pr, "ci-pending-escalated-head"))

    def set_ci_pending_escalated_head(self, pr: int, sha: str) -> None:
        if sha:
            self._write(self.pr_path(pr, "ci-pending-escalated-head"), sha)

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

    def clear_pr_review(self, pr: int) -> None:
        """Full PR-state retirement, incl. the automerge marker (review M4:
        `automerge` is a PR suffix — it used to live in `clear_issue`, which
        cleared a file nothing wrote, while the real pr-N.automerge marker was
        cleared by neither cleaner)."""
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
                self.clear_pr_review(pr)
            self.clear_issue(issue_key)
        else:
            self.clear_issue_resume_state(issue_key)


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
