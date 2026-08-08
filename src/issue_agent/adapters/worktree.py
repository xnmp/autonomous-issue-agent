"""Git worktree management + the GateContext builder for the in-process governor.

`Worktrees.ensure` / `.remove` port agent/lib.sh:ensure_worktree + cleanup.sh's
`git worktree remove`. `build_gate_context` is the git/fs fact-gatherer the
governor's `make_pretooluse_hook(build_ctx)` needs — it reads the worktree's
branch, the commit subjects since base, the changed-file list, the `.harness/`
markers, and wires the `run_tests` / `revert_and_run` / `exists_at_base` /
`open_pr` callables (§1 + §10 + the push gate) over real git and `gh`.

git/subprocess is injected as `run` so the path/branch logic is testable with a
fake; the production default shells out. The §10 revert simulation mirrors
_pr_common.sh:prc_check_red_fails (revert impl to base, run tests, restore HEAD).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.contracts import (
    DEFAULT_TEST_FAILURE_EXIT_CODES,
    TestExecution,
    TestExecutionStatus,
    classify_test_exit_code,
    parse_test_failure_exit_codes,
)
from ..domain.exemptions import (
    ExemptionDeclaration,
    count_malformed_declarations,
    parse_exemption_declarations,
)
from ..domain.gate import OpenPrLookup, resolve_test_cmd
from .governor import GateContext, run_target_command
from .store import HarnessMarkers


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str


GitRunner = Callable[[list[str], str], GitResult]  # (args, cwd) -> result


# Hard ceiling on any single `git` subprocess (review M20 — this runner had NO
# timeout at all, unlike `_GH_TIMEOUT_S` in github.py, added after a recorded
# `gh pr view` hang that blocked ~14 minutes and froze the whole review daemon
# because the poll loop is synchronous). This same runner is used on BOTH the
# dispatch path (`Worktrees.ensure`'s `git fetch origin`, a real network call)
# AND inside `build_gate_context`'s local ops, which run on EVERY Bash call the
# agent makes via the PreToolUse hook — an unbounded hang on either path wedges
# the daemon or the agent's whole session, the identical failure class as the
# gh hang above, just via a different command. 60s is far above a healthy call
# (local git plumbing is sub-second; a `fetch` against a live remote is at most
# a few seconds) yet bounds a pathological one (a stale `index.lock`, a dead
# proxy holding a fetch open — see CLAUDE.md's proxy note).
_GIT_TIMEOUT_S = 60.0

# All target-command execution defenses (env scrub, HARNESS_GATE_DEPTH
# re-entrancy bound, process-group containment — the #63 fork-bomb class) live
# in governor.run_target_command; this module only wires it into the gate.


def _subprocess_git(args: list[str], cwd: str) -> GitResult:
    """Run `git` in `cwd`, bounded by `_GIT_TIMEOUT_S`.

    NEVER raises: a timeout, a missing `git`, or a spawn failure all come back
    as a non-zero `GitResult` — the same "never raise across this seam"
    contract `_subprocess_gh` below already uses, so every existing `if
    result.code != 0` caller keeps working unchanged. Unlike `_subprocess_gh`
    (whose failures are routine and expected — "no branch yet" is not an
    error), a git failure here is surfaced on OUR stderr with the captured
    stderr text, because a silently-swallowed `worktree add` failure is
    exactly how `ensure` used to hand back a path to a worktree that was never
    created (M20's other half — see `Worktrees.ensure`)."""
    try:
        res = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[worktree] git timed out after {_GIT_TIMEOUT_S:g}s and was killed: "
            f"git {' '.join(args)} (cwd={cwd})",
            file=sys.stderr,
        )
        return GitResult(1, "")
    except OSError as exc:
        print(
            f"[worktree] git failed to start: git {' '.join(args)} (cwd={cwd}) — {exc}",
            file=sys.stderr,
        )
        return GitResult(1, "")
    if res.returncode != 0:
        print(
            f"[worktree] git {' '.join(args)} (cwd={cwd}) failed "
            f"(exit {res.returncode}): {(res.stderr or '').strip()[:500]}",
            file=sys.stderr,
        )
    return GitResult(res.returncode, res.stdout)


def worktree_list_contains_issue(listing: str, issue: int) -> bool:
    expected = f"issue-{issue}"
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line.removeprefix("worktree ").strip()
        if os.path.basename(path) == expected:
            return True
    return False


def _issue_worktree_branch(listing: str, issue: int) -> str:
    """Harness-owned issue branch checked out by issue-N, or "" otherwise."""
    expected = f"issue-{issue}"
    current = False
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ").strip()
            current = os.path.basename(path) == expected
        elif current and line.startswith("branch refs/heads/"):
            branch = line.removeprefix("branch refs/heads/").strip()
            if re.fullmatch(rf"(?:fix|feat|chore|refactor)/{issue}(?:-.+)?", branch):
                return branch
            return ""
        elif current and not line.strip():
            current = False
    return ""


class WorktreeUnavailable(RuntimeError):
    """`Worktrees.ensure` could not produce a usable worktree — every `git
    worktree add` attempt failed (a locked index, a stale lockfile, a race
    with a concurrent worktree op, disk pressure…) and the path still doesn't
    exist as a directory. Before this existed, `ensure` returned that (missing)
    path regardless, and the caller logged "worktree #N: ready on '<branch>' →
    <path>" and dispatched the agent straight into a nonexistent cwd (review
    M20).

    Raising joins the SAME "session died before it started" contract
    `AgentDispatcher.dispatch` already has for a leg that raises before
    `runner.run` returns (review C3): the whole workflow tail (save_session,
    retry-counter bump, follow-up actions) is skipped, the infra-death counter
    is bumped, and the exception is re-raised so `Daemon.tick`'s crash
    isolation puts the issue back for the next poll. Nothing new to add at any
    call site — `_ensure_cwd` (app/workflows/fix.py) has no try/except around
    `self.worktrees.ensure(...)`, so this propagates straight into that
    existing handler."""


class WorktreeBranchAmbiguous(RuntimeError):
    """Recovery found several non-synthetic branches for the same issue.

    A recovery that guesses here can create a fresh worktree on the wrong
    branch and strand the blocked work. Callers must surface the ambiguity
    rather than silently falling back to a title-derived branch name.
    """


class Worktrees:
    """Create/remove per-issue worktrees under WORKTREE_ROOT, relative to the
    target repo (`repo_dir`). Branch shape is the harness's
    <type>/<issue>-<slug>; the agent may pick a different prefix, so callers
    resolve the real branch from GitHub, not this name."""

    def __init__(
        self,
        repo_dir: str,
        worktree_root: str = ".claude/worktrees",
        base_branch: str = "dev",
        run: GitRunner = _subprocess_git,
        path_exists: Callable[[str], bool] = os.path.isdir,
    ):
        self.repo_dir = repo_dir
        self.worktree_root = worktree_root
        self.base_branch = base_branch
        self._git = run
        self._path_exists = path_exists
        # `git worktree add/remove/prune` mutate shared repository metadata.
        # Paid work may overlap, but those short setup/cleanup transactions may
        # not race each other on `.git/worktrees` and branch refs.
        # Residual: agent-owned git commands run in separate processes after
        # setup and cannot acquire this in-process lock. Concurrent `git fetch`
        # may still contend on shared remote-ref lockfiles; rollout metrics must
        # treat that signature as cross-leg interference, not a code failure.
        self._mutation_lock = threading.RLock()

    def path(self, issue: int) -> str:
        return os.path.join(self.worktree_root, f"issue-{issue}")

    def prune_validate(self) -> int:
        """Prune stale metadata and validate the remaining worktree registry."""

        with self._mutation_lock:
            pruned = self._git(["worktree", "prune"], self.repo_dir)
            if pruned.code != 0:
                raise WorktreeUnavailable("git worktree prune failed during recovery")
            listing = self._git(
                ["worktree", "list", "--porcelain"], self.repo_dir
            )
            if listing.code != 0:
                raise WorktreeUnavailable(
                    "git worktree list failed during recovery"
                )
            paths = [
                line.removeprefix("worktree ").strip()
                for line in listing.out.splitlines()
                if line.startswith("worktree ")
            ]
            missing = [path for path in paths if not self._path_exists(path)]
            if missing:
                raise WorktreeUnavailable(
                    f"worktree registry contains missing paths: {missing!r}"
                )
            return len(paths)

    def branch_for_issue(self, issue: int, fallback: str) -> str:
        """Return a retained harness branch for an older issue leg.

        New legs persist their branch in Store. This local-ref lookup recovers
        pre-persistence blocked legs after their worktree was reaped. A sole
        non-synthetic branch wins over any stale fallback. An absent branch
        returns ""; genuine ambiguity raises rather than guessing.
        """
        refs = self._git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads"], self.repo_dir
        )
        matches = [
            ref for ref in refs.out.splitlines()
            if re.fullmatch(rf"(?:fix|feat|chore|refactor)/{issue}-.+", ref)
        ]
        if len(matches) <= 1:
            return matches[0] if matches else ""
        synthetic = {fallback, f"fix/{issue}-issue"}
        retained = [ref for ref in matches if ref not in synthetic]
        if len(retained) == 1:
            return retained[0]
        raise WorktreeBranchAmbiguous(
            f"cannot recover issue #{issue}: multiple retained branches {matches!r}"
        )

    def ensure(self, issue: int, branch: str) -> str:
        with self._mutation_lock:
            return self._ensure(issue, branch)

    def _ensure(self, issue: int, branch: str) -> str:
        """Ensure a worktree for the issue exists on `branch`; return its abs
        path. Port of ensure_worktree: reuse existing, else create from the
        branch ref or off origin/base.

        Raises `WorktreeUnavailable` if the path still isn't a real directory
        afterward (M20): every `git worktree add` attempt can fail, and
        returning a path that doesn't exist is a lie the caller has no way to
        catch before dispatching the agent into it."""
        wt = self.path(issue)
        listing = self._git(["worktree", "list", "--porcelain"], self.repo_dir)
        if not worktree_list_contains_issue(listing.out, issue):
            self._git(["fetch", "origin", self.base_branch, "--quiet"], self.repo_dir)
            has_branch = self._git(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], self.repo_dir
            )
            if has_branch.code == 0:
                self._git(["worktree", "add", wt, branch], self.repo_dir)
            else:
                made = self._git(
                    ["worktree", "add", "-b", branch, wt, f"origin/{self.base_branch}"],
                    self.repo_dir,
                )
                if made.code != 0:
                    self._git(
                        ["worktree", "add", "-b", branch, wt, self.base_branch],
                        self.repo_dir,
                    )
        abs_path = os.path.join(self.repo_dir, wt)
        if not self._path_exists(abs_path):
            raise WorktreeUnavailable(
                f"worktree for issue #{issue} on branch '{branch}' is not ready at "
                f"{abs_path!r} — every 'git worktree add' attempt failed (see the git "
                "runner's stderr surfacing for why)."
            )
        return abs_path

    def remove(self, issue: int) -> None:
        with self._mutation_lock:
            self._remove(issue)

    def _remove(self, issue: int) -> None:
        listing = self._git(["worktree", "list", "--porcelain"], self.repo_dir)
        if listing.code != 0:
            raise WorktreeUnavailable(
                f"cannot inspect worktrees before removing issue #{issue}"
            )
        branch = _issue_worktree_branch(listing.out, issue)
        abs_path = os.path.join(self.repo_dir, self.path(issue))
        if (
            not worktree_list_contains_issue(listing.out, issue)
            and not self._path_exists(abs_path)
        ):
            return
        removed = self._git(
            ["worktree", "remove", self.path(issue), "--force"],
            self.repo_dir,
        )
        if removed.code != 0:
            raise WorktreeUnavailable(
                f"git refused to remove the worktree for issue #{issue}"
            )
        pruned = self._git(["worktree", "prune"], self.repo_dir)
        if pruned.code != 0:
            raise WorktreeUnavailable(
                f"git refused to prune worktrees after removing issue #{issue}"
            )
        verified = self._git(
            ["worktree", "list", "--porcelain"], self.repo_dir
        )
        if (
            verified.code != 0
            or worktree_list_contains_issue(verified.out, issue)
            or self._path_exists(abs_path)
        ):
            raise WorktreeUnavailable(
                f"worktree removal for issue #{issue} could not be verified"
            )
        if branch:
            deleted = self._git(["branch", "-D", branch], self.repo_dir)
            if deleted.code != 0:
                raise WorktreeUnavailable(
                    f"git refused to delete retained branch {branch!r}"
                )
            branch_state = self._git(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                self.repo_dir,
            )
            if branch_state.code == 0 or branch_state.code > 1:
                raise WorktreeUnavailable(
                    f"branch deletion for {branch!r} could not be verified"
                )

    def reset(self, issue: int) -> None:
        """Discard all uncommitted work in the issue's worktree (tracked + untracked),
        leaving only the committed branch state. Used before contract adjudication so
        the adjudicating author sees the committed contract, never the implementer's
        in-progress code (argument-only blindness)."""
        wt = os.path.join(self.repo_dir, self.path(issue))
        self._git(["checkout", "--", "."], wt)
        self._git(["clean", "-fd"], wt)


class ReviewCheckouts:
    """Detached per-PR worktrees pinned at the exact reviewed head SHA.

    Review legs used to execute in the operator's live target checkout, so
    every on-disk read saw whatever branch (and dirty state) the operator
    left there — on keifu #165 (2026-08-01) the reviewer quoted `chong-dev`'s
    stale code while stamping the fresh head SHA, and three phantom
    REQUEST_CHANGES rounds nearly escalated a correct PR. The verdict binds a
    SHA, so the code the reviewer reads MUST be that SHA: a detached worktree
    per PR, refreshed to the head each round, unreachable by the operator's
    branch switches and invisible to their uncommitted work.
    """

    def __init__(
        self,
        repo_dir: str,
        worktree_root: str = ".claude/worktrees",
        run: GitRunner = _subprocess_git,
        path_exists: Callable[[str], bool] = os.path.isdir,
        remove_tree: Callable[[str], None] | None = None,
    ):
        self.repo_dir = repo_dir
        self.worktree_root = worktree_root
        self._git = run
        self._path_exists = path_exists
        self._remove_tree = remove_tree if remove_tree is not None else (
            lambda p: shutil.rmtree(p, ignore_errors=False)
        )
        self._mutation_lock = threading.RLock()
        # (pr → last ensured sha): a multi-mode round runs several legs against
        # one head; only the first pays the fetch/reset.
        self._ensured: dict[int, str] = {}

    def path(self, pr: int) -> str:
        return os.path.join(self.worktree_root, f"review-pr-{pr}")

    def ensure(self, pr: int, head_sha: str) -> str:
        """Return an absolute path whose checkout IS `head_sha`, or raise
        `WorktreeUnavailable`. Never returns a stale or dirty view: a caller
        that cannot get the pinned head must defer the round, not review
        whatever happens to be on disk."""
        if not head_sha:
            raise WorktreeUnavailable("review checkout needs a head SHA")
        with self._mutation_lock:
            abs_path = os.path.join(self.repo_dir, self.path(pr))
            is_worktree = False
            if self._path_exists(abs_path):
                # A directory here is NOT proof of a usable worktree. If it is
                # a plain unregistered dir (partial remove; a prune racing an
                # add), git commands run inside it discover the PARENT repo —
                # so a reset/clean "refresh" would destroy the operator's live
                # checkout, and the rev-parse confirmation would pass because
                # the parent's HEAD had just been moved. Only a toplevel that
                # IS this path may be mutated.
                is_worktree = self._is_this_worktree(pr, abs_path)
                if not is_worktree:
                    self._remove_stale_dir(pr, abs_path)
            if (
                is_worktree
                and self._ensured.get(pr) == head_sha
                and self._git(["rev-parse", "HEAD"], abs_path).out.strip()
                == head_sha
            ):
                return abs_path
            # GitHub always serves refs/pull/<n>/head; fetching the bare SHA
            # is not universally allowed, the PR ref is.
            self._git(
                ["fetch", "origin", f"refs/pull/{pr}/head", "--quiet"],
                self.repo_dir,
            )
            present = self._git(
                ["cat-file", "-e", f"{head_sha}^{{commit}}"], self.repo_dir
            )
            if present.code != 0:
                raise WorktreeUnavailable(
                    f"reviewed head {head_sha} for PR #{pr} is not reachable "
                    "after fetching its PR ref (force-pushed away?) — defer "
                    "and re-observe"
                )
            if is_worktree:
                # Re-verify immediately before mutating: the classification
                # above is separated from this point by a network fetch (up to
                # 60s), and the original H1 mechanism reappears if the .git
                # link vanishes in that window. Between this check and the
                # refresh there are only local sub-second ops.
                if not self._is_this_worktree(pr, abs_path):
                    raise WorktreeUnavailable(
                        f"review worktree for PR #{pr} stopped being a "
                        "worktree during fetch — defer"
                    )
                for refresh in (
                    ["checkout", "--detach", head_sha],
                    ["reset", "--hard", head_sha],
                    # -fd, not -fdx: ignored build caches (target/,
                    # node_modules) are expensive and cannot alter tracked
                    # review content.
                    ["clean", "-fd"],
                ):
                    step = self._git(refresh, abs_path)
                    if step.code != 0:
                        raise WorktreeUnavailable(
                            f"review worktree for PR #{pr} could not be "
                            f"refreshed (git {refresh[0]} failed) — defer"
                        )
            else:
                # A registration whose directory is gone makes `worktree add`
                # fail forever; prune first so this path self-heals instead of
                # deferring every round.
                self._git(["worktree", "prune"], self.repo_dir)
                made = self._git(
                    ["worktree", "add", "--detach", self.path(pr), head_sha],
                    self.repo_dir,
                )
                if made.code != 0 or not self._path_exists(abs_path):
                    raise WorktreeUnavailable(
                        f"review worktree for PR #{pr} could not be created at "
                        f"{abs_path!r}"
                    )
            confirmed = self._git(["rev-parse", "HEAD"], abs_path)
            if confirmed.out.strip() != head_sha:
                raise WorktreeUnavailable(
                    f"review worktree for PR #{pr} is at "
                    f"{confirmed.out.strip()[:12]!r}, not the reviewed head "
                    f"{head_sha[:12]!r}"
                )
            self._ensured[pr] = head_sha
            return abs_path

    def _is_this_worktree(self, pr: int, abs_path: str) -> bool:
        """True iff git agrees the directory's toplevel IS the directory.

        Fail-safe tri-state collapsed the right way: a rev-parse that FAILS
        (60s timeout, spawn failure under process pressure — #63's class) means
        "could not classify", and an unclassifiable directory must DEFER, never
        be deleted as garbage. Only a successful answer naming a DIFFERENT
        toplevel proves a plain dir."""
        if os.path.islink(abs_path):
            # A link is never this worktree. Without this, a link to the
            # PARENT repo makes both realpaths resolve to the parent, the
            # comparison below passes, and the refresh loop reset --hards the
            # operator's live checkout — H1 routed around the stale-dir guard.
            return False
        top = self._git(["rev-parse", "--show-toplevel"], abs_path)
        if top.code != 0 or not top.out.strip():
            raise WorktreeUnavailable(
                f"review checkout for PR #{pr} at {abs_path!r} could not be "
                "classified (rev-parse failed) — defer"
            )
        return os.path.realpath(top.out.strip()) == os.path.realpath(abs_path)

    def _remove_stale_dir(self, pr: int, abs_path: str) -> None:
        """Delete a plain (unregistered) directory squatting on the worktree
        path, so `worktree add` can recreate it. Refuses anything that is not
        strictly inside this repo's worktree root — the one thing this class
        must never delete is the parent checkout."""
        if os.path.islink(abs_path):
            # Remove the LINK, never its pointee: rmtree(realpath) would
            # destroy whatever the link names (possibly a sibling PR's
            # checkout) while leaving the link squatting forever.
            try:
                os.unlink(abs_path)
            except OSError as exc:
                raise WorktreeUnavailable(
                    f"stale review checkout link for PR #{pr} could not be "
                    f"removed ({exc}) — defer"
                ) from exc
            self._git(["worktree", "prune"], self.repo_dir)
            return
        root = os.path.realpath(os.path.join(self.repo_dir, self.worktree_root))
        target = os.path.realpath(abs_path)
        if not target.startswith(root + os.sep):
            raise WorktreeUnavailable(
                f"stale review checkout for PR #{pr} at {abs_path!r} is "
                "outside the worktree root — refusing to delete"
            )
        try:
            self._remove_tree(abs_path)
        except OSError as exc:
            raise WorktreeUnavailable(
                f"stale review checkout for PR #{pr} could not be removed "
                f"({exc}) — defer"
            ) from exc
        self._git(["worktree", "prune"], self.repo_dir)

    def remove(self, pr: int) -> None:
        """Best-effort removal after a settled round; the next round recreates."""
        with self._mutation_lock:
            self._ensured.pop(pr, None)
            self._git(
                ["worktree", "remove", "--force", self.path(pr)], self.repo_dir
            )
            self._git(["worktree", "prune"], self.repo_dir)


# ── GateContext builder ───────────────────────────────────────────────────────
_DEFAULT_TEST_TIMEOUT_S = 600.0


def _test_failure_exit_codes() -> frozenset[int]:
    """Trusted runner exit codes that mean an asserted test failed, as this
    deployment declares them. Parsing lives in the domain so doctor reports on
    exactly the set the gate enforces."""

    return parse_test_failure_exit_codes(
        os.environ.get(
            "GATE_TEST_FAILURE_EXIT_CODES", DEFAULT_TEST_FAILURE_EXIT_CODES
        )
    )


def _test_timeout_s() -> float:
    """GATE_TEST_TIMEOUT_S in seconds, read fresh on every call (not cached at
    import/build time) so a test can override it per-case and an operator's env
    edit takes effect without restarting the daemon. A hung TEST_CMD must never
    wedge the gate hook — same failure class as FAILURE-MODES #24 — so an
    empty or malformed value falls back to the default rather than disabling
    the timeout outright (precedent: governor.py's edit-check uses a fixed
    120s; the test suite gets a generous but finite default since it can
    legitimately run longer)."""
    raw = os.environ.get("GATE_TEST_TIMEOUT_S", "")
    try:
        seconds = float(raw)
    except ValueError:
        return _DEFAULT_TEST_TIMEOUT_S
    return seconds if seconds > 0 else _DEFAULT_TEST_TIMEOUT_S


# ── open-PR probe: the push gate's precondition (review known-gap-2) ─────────
# Bounded from day one because this runs INLINE in the PreToolUse hook, on the
# agent's own `git push`: an unbounded `gh` (dead proxy, auth prompt, a GitHub
# 5xx holding the socket) would wedge the session mid-push. Same failure class
# as the TEST_CMD timeout above; 20s is far above a healthy call (<2s).
_OPEN_PR_TIMEOUT_S = 20.0

# Pause between the probe's two attempts (`resolve_open_pr`'s retry) — long
# enough for a transient blip to clear, short enough that the agent is not left
# waiting on its own push.
_OPEN_PR_RETRY_SLEEP_S = 1.0

# A `gh … list` argv MUST state its ceiling (review C2) — without `--limit`, gh
# silently answers with the 30 NEWEST items and looks complete. The question
# here is purely existential ("does this head have an open PR?"), so 1 is a
# complete answer by construction rather than a truncation; the flag is still
# passed explicitly so this call can never be mistaken for another instance of
# the C2 omission.
_OPEN_PR_LIMIT = "1"

GhRunner = Callable[[list[str], str], GitResult]  # (args, cwd) -> result


def _subprocess_gh(args: list[str], cwd: str) -> GitResult:
    """Run `gh` in `cwd`, bounded by `_OPEN_PR_TIMEOUT_S`.

    NEVER raises: a timeout, a missing `gh`, an auth failure and a 5xx all come
    back as a non-zero result, which `classify_open_pr` turns into
    `OpenPrLookup.ERROR` — "I could not look", never "there is no PR". That
    collapse is the exact bug `OpenPrLookup` exists to prevent."""
    try:
        res = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True,
            timeout=_OPEN_PR_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return GitResult(1, "")
    return GitResult(res.returncode, res.stdout)


def classify_open_pr(result: GitResult) -> OpenPrLookup:
    """`gh pr list --json number` result → the domain's tri-state. Pure, so the
    error-vs-empty distinction the whole type exists for is unit-testable.

    Non-zero exit, unparseable output, or a non-list payload ⇒ ERROR. Only a
    well-formed EMPTY list means "no open PR"; a blank stdout on a 0 exit is an
    answer we cannot read, so it is an error, not an absence."""
    if result.code != 0:
        return OpenPrLookup.ERROR
    try:
        prs = json.loads(result.out)
    except ValueError:
        return OpenPrLookup.ERROR
    if not isinstance(prs, list):
        return OpenPrLookup.ERROR
    return OpenPrLookup.OPEN if prs else OpenPrLookup.NONE


_repo_unset_warned = False


def _warn_repo_unset_once() -> None:
    """The open-PR probe is the ONE gh call in the harness that can silently
    change meaning when it is not pinned to a repo (P1R-11): unpinned, gh
    resolves a repo from the worktree's remotes, and on a fork/two-remote
    checkout it can pick a DIFFERENT repo where this branch has no PR — which
    answers NONE, and NONE means "no gate". That is precisely the bypass
    `OpenPrLookup` exists to kill, wearing a valid-looking answer. If the
    orchestrator ever stops threading `repo=`, say so loudly rather than
    degrading in silence (§5: an absence bug is invisible by construction)."""
    global _repo_unset_warned
    if _repo_unset_warned:
        return
    _repo_unset_warned = True
    print(
        "[gate] WARNING: the open-PR probe is running WITHOUT --repo (no `repo=` was "
        "passed to build_gate_context). gh will resolve a repo from the worktree's "
        "remotes; on a fork or two-remote checkout that can be the wrong one, and a "
        "wrong repo answers 'no open PR' — which silently disables the push gate. "
        "Wire cfg.repo through to build_gate_context.",
        file=sys.stderr,
    )


def probe_open_pr(
    branch: str, cwd: str, gh: GhRunner = _subprocess_gh, repo: str = ""
) -> OpenPrLookup:
    """One `gh pr list --repo <repo> --head <branch> --state open` round-trip →
    tri-state. Port of check-push.sh's precondition. The retry lives in the
    domain (`resolve_open_pr`), so this stays a single honest observation.

    `branch` is the ref the push WRITES (not necessarily the checked-out one) —
    see `governor._push_gate`. `repo` pins the question to the target repo, like
    every other gh call in the harness; empty falls back to gh's own remote
    resolution and warns once."""
    if not repo:
        _warn_repo_unset_once()
    args = ["pr", "list"]
    if repo:
        args += ["--repo", repo]
    args += ["--head", branch, "--state", "open",
             "--limit", _OPEN_PR_LIMIT, "--json", "number"]
    return classify_open_pr(gh(args, cwd))


def _sleep_between_probes() -> None:
    time.sleep(_OPEN_PR_RETRY_SLEEP_S)


def _resolve_base_ref(git: GitRunner, cwd: str, base: str) -> str | None:
    for ref in (f"origin/{base}", base):
        if git(["rev-parse", "--verify", "--quiet", ref], cwd).code == 0:
            return ref
    return None


def build_gate_context(
    cwd: str,
    base_branch: str = "dev",
    repo: str = "",
    env_test_cmd: str | None = None,
    red_must_fail: bool = True,
    verify_red_at_commit: bool = True,
    allow_untested: bool = False,
    git: GitRunner = _subprocess_git,
    gh: GhRunner = _subprocess_gh,
    run_tests: Callable[[str], bool] | None = None,
    retry_sleep: Callable[[], None] = _sleep_between_probes,
    screenshots_read_path: str = "",
    screenshots_receipt_key: bytes | None = None,
    durable_publish: bool = False,
) -> GateContext:
    """Gather the worktree facts the governor's gate rules need. Pure-ish: all
    git/gh access goes through the injected `git`/`gh` runners, so a fake
    exercises the wiring without a real repo. `run_tests` defaults to a real
    shell eval in `cwd`.

    The open-PR lookup is wired as a LAZY probe, never a value: this function
    runs on EVERY Bash call the agent makes, and resolving it eagerly would put
    a `gh` round-trip behind every `ls` (review known-gap-2)."""
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).out.strip()
    baseref = _resolve_base_ref(git, cwd, base_branch)
    base_resolvable = baseref is not None

    subjects: tuple[str, ...] = ()
    published_subjects: tuple[str, ...] = ()
    declared_exemptions: tuple[ExemptionDeclaration, ...] = ()
    malformed_exemption_lines = 0
    changed: tuple[str, ...] = ()
    touched_at_base: tuple[str, ...] = ()
    if baseref:
        log = git(["log", f"{baseref}..HEAD", "--reverse", "--format=%s"], cwd)
        subjects = tuple(s for s in log.out.splitlines() if s)
        # #124 declarations are Gate-Exemption trailers anywhere on the
        # branch's commits: tracked history the agent cannot edit without
        # changing the branch, and nothing left in the repo after merge.
        bodies = git(["log", f"{baseref}..HEAD", "--format=%B"], cwd)
        if bodies.code == 0:
            seen_decl: dict[ExemptionDeclaration, None] = {}
            for declaration in parse_exemption_declarations(bodies.out):
                seen_decl.setdefault(declaration)
            declared_exemptions = tuple(seen_decl)
            malformed_exemption_lines = count_malformed_declarations(
                bodies.out
            )
        diff = git(["diff", "--name-only", f"{baseref}...HEAD"], cwd)
        changed = tuple(f for f in diff.out.splitlines() if f)
        # OLD-side paths of modified/deleted/renamed files, tracked SEPARATELY
        # from `changed`. `--name-only` reports a rename by its NEW path only,
        # so three tests renamed out of the test tree left the suite with the
        # tests-immutable gate seeing nothing test-shaped, while the
        # merge-side and shell derivations (both old-side) saw three subjects
        # — the gate admitted what merge would then withhold, and on the SDK
        # path nothing stopped the removal pre-publish (round-4 adversarial
        # review + parity check). It must NOT be folded into `changed`: a
        # DELETED test path there would read as an ADDED test and satisfy
        # check_required_test. `-M` off so a rename is delete+add.
        old_side = git(
            [
                "diff",
                "--no-renames",
                "--diff-filter=MDRT",
                "--name-only",
                f"{baseref}...HEAD",
            ],
            cwd,
        )
        if old_side.code == 0:
            touched_at_base = tuple(
                f for f in old_side.out.splitlines() if f
            )
        # Commits already on the observed remote PR branch are published
        # history the agent cannot rewrite (the broker requires them as
        # ancestors), so the commit-log gate must not re-police their
        # subjects — GitHub's Update-branch merge commit wedged keifu PR
        # #182 (2026-08-03). Local origin/<branch> only, never a fetch:
        # this context is built on every Bash call. A stale or absent ref
        # fails toward the stricter, pre-exemption behavior.
        if branch and branch != "HEAD":
            plog = git(
                [
                    "log",
                    f"{baseref}..refs/remotes/origin/{branch}",
                    "--reverse",
                    "--format=%s",
                ],
                cwd,
            )
            if plog.code == 0:
                published_subjects = tuple(
                    s for s in plog.out.splitlines() if s
                )

    markers = HarnessMarkers(cwd)
    screenshots_read: tuple[str, ...] = ()
    screenshots_receipt_available = False
    # Distinct from "unavailable": the runtime DOES mint receipts but this
    # read failed (transient I/O). The gate must say "retry/read the images",
    # never the capability dead-end message (adversarial P2-3 — the latter
    # instructs deleting committed evidence over a recoverable condition).
    screenshots_receipt_unreadable = False
    if screenshots_read_path:
        # A CONFIGURED path IS the capability statement: this runtime mints
        # receipts, whatever happens to the read below. Anything less fails
        # OPEN — e.g. a transient `git rev-parse HEAD` failure used to leave
        # this False, which under receipt-less-pass-through gate semantics
        # silently disables the read check on the SDK path (adversarial P1-1).
        screenshots_receipt_available = True
        try:
            with open(screenshots_read_path, encoding="utf-8") as receipt:
                lines = tuple(receipt.read(64 * 1024).splitlines())
            if screenshots_receipt_key is None:
                # Explicit compatibility surface for the shell parity harness.
                # Production callers always supply the daemon-held signing key.
                screenshots_read = tuple(
                    line.strip() for line in lines if line.strip()
                )
            else:
                head_result = git(["rev-parse", "HEAD"], cwd)
                if head_result.code == 0 and head_result.out.strip():
                    current_head = head_result.out.strip()
                    verified: list[str] = []
                    for line in lines:
                        try:
                            record = json.loads(line)
                            payload = {
                                "v": record["v"],
                                "head": record["head"],
                                "path": record["path"],
                                "digest": record["digest"],
                            }
                            encoded = json.dumps(
                                payload,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            valid_signature = hmac.compare_digest(
                                str(record["signature"]),
                                hmac.new(
                                    screenshots_receipt_key,
                                    encoded.encode(),
                                    hashlib.sha256,
                                ).hexdigest(),
                            )
                            relative = str(record["path"])
                            image_path = os.path.realpath(
                                os.path.join(cwd, relative)
                            )
                            evidence_root = os.path.realpath(
                                os.path.join(cwd, "evidence")
                            )
                            valid_path = (
                                os.path.commonpath(
                                    (image_path, evidence_root)
                                )
                                == evidence_root
                            )
                            if (
                                not valid_signature
                                or record["v"] != 1
                                or record["head"] != current_head
                                or not valid_path
                            ):
                                continue
                            with open(image_path, "rb") as image:
                                digest = hashlib.sha256(
                                    image.read()
                                ).hexdigest()
                            if digest != record["digest"]:
                                continue
                            verified.append(os.path.basename(relative))
                        except (
                            KeyError,
                            OSError,
                            TypeError,
                            ValueError,
                            json.JSONDecodeError,
                        ):
                            continue
                    screenshots_read = tuple(verified)
        except OSError:
            screenshots_read = ()
            screenshots_receipt_unreadable = True

    # Harness-side digests of committed evidence images: the byte-identical
    # reuse check (#153) must not depend on receipts, so it reads the worktree
    # directly on EVERY runtime. Per-file fail-soft: `changed` includes paths
    # deleted later on the branch, and killing the whole map on one miss both
    # disabled the rule (agent-triggerable: commit-then-delete a spare image)
    # and diverged from check-pr.sh's `[ -f ] || continue` (adversarial P1-2).
    # Guarded on the visual-ac marker because this runs on EVERY Bash call the
    # agent makes — no I/O tax on the non-visual common case.
    image_digests: dict[str, str] = {}
    if markers.visual_ac():
        for path in changed:
            if not path.startswith("evidence/"):
                continue
            try:
                with open(os.path.join(cwd, path), "rb") as image:
                    image_digests[path] = hashlib.sha256(
                        image.read()
                    ).hexdigest()
            except OSError:
                continue

    def open_pr(target_branch: str) -> OpenPrLookup:
        """Resolved ONLY when the governor actually gates a push (see
        `governor._push_gate`) — building this context must stay gh-free.

        `target_branch` is the ref the push writes, derived from the push argv;
        it is NOT necessarily `branch` (the checked-out one), which is the whole
        of P1R-10 — `git push origin HEAD:x` and any push from a detached
        mid-rebase HEAD used to ask about the wrong branch, or skip the gate."""
        return probe_open_pr(target_branch, cwd, gh, repo=repo)

    def exists_at_base(path: str) -> bool:
        if not baseref:
            return False
        return git(["cat-file", "-e", f"{baseref}:{path}"], cwd).code == 0

    def _default_run_tests(cmd: str) -> bool | None:
        """Run the test command via `governor.run_target_command` (env scrub +
        re-entrancy bound + process-group kill on timeout — the #63 defenses).

        Tri-state: True = passed, False = failed (incl. timeout), None = the
        run was REFUSED because it would re-enter the gate. None must flow to
        the domain unchanged — the rules fail CLOSED on it with a recursion
        message, which is a different verdict than 'your tests are red'. A
        hang still resolves to False here, never an exception —
        `evaluate_command` runs under a broad fail-OPEN except and an escaping
        TimeoutExpired would silently ALLOW the gated command."""
        rc, _ = run_target_command(cmd, cwd, _test_timeout_s())
        if rc is None:
            return None
        return rc == 0

    rt = run_tests or _default_run_tests

    def receipt(
        command: str,
        status: TestExecutionStatus,
        *,
        code: int | None = None,
        output: str = "",
        started: float | None = None,
    ) -> TestExecution:
        revision = git(["rev-parse", "HEAD"], cwd).out.strip() or "unknown"
        duration_ms = (
            max(0, round((time.monotonic() - started) * 1000))
            if started is not None
            else 0
        )
        digest = hashlib.sha256(output.encode()).hexdigest()
        return TestExecution(
            status=status,
            command=("/bin/bash", "-c", command or "<unresolved>"),
            exit_code=code,
            duration_ms=duration_ms,
            stdout_digest=digest,
            stderr_digest=digest,
            executor_identity="issue-agent:trusted-verifier",
            repository_revision=revision,
        )

    def run_red_tests(cmd: str) -> TestExecution:
        """Return typed evidence; only TEST_FAILED authorizes a red commit."""
        started = time.monotonic()
        if run_tests is not None:
            passed = rt(cmd)
            status = (
                TestExecutionStatus.INFRA_ERROR
                if passed is None
                else TestExecutionStatus.PASSED
                if passed
                else TestExecutionStatus.TEST_FAILED
            )
            code = 127 if passed is None else 0 if passed else 1
            return receipt(cmd, status, code=code, started=started)
        rc, output = run_target_command(
            cmd, cwd, _test_timeout_s(), capture=True
        )
        status = classify_test_exit_code(rc, _test_failure_exit_codes())
        return receipt(
            cmd,
            status,
            code=(
                None
                if status
                in {
                    TestExecutionStatus.TIMED_OUT,
                    TestExecutionStatus.CANCELLED,
                }
                else 127 if rc is None else rc
            ),
            output=output,
            started=started,
        )

    def revert_and_run(impl_files: tuple[str, ...]) -> TestExecution:
        """§10: drop new impl / revert modified impl to base, run the test cmd,
        restore HEAD, and return a typed trusted-execution receipt. Only an exit
        code declared by the operator as an assertion-failure code becomes
        TEST_FAILED; timeouts, signals, command-not-found, and runner failures
        remain non-authorizing infrastructure evidence."""
        # Same precedence as every other gate rule — operator TEST_CMD first
        # (P1R-18). This site used to inline its own `markers.test_cmd() or
        # env_test_cmd` order, so §10 would have kept running the agent-written
        # command even after the domain's `resolve_test_cmd` stopped preferring
        # it. One resolver, one order.
        test_cmd = resolve_test_cmd(markers.test_cmd(), env_test_cmd)

        if not baseref or not test_cmd:
            return receipt(
                test_cmd, TestExecutionStatus.INFRA_ERROR, code=127
            )
        started = time.monotonic()
        try:
            for f in impl_files:
                if exists_at_base(f):
                    if git(["checkout", "-q", baseref, "--", f], cwd).code != 0:
                        return receipt(
                            test_cmd,
                            TestExecutionStatus.INFRA_ERROR,
                            code=127,
                            started=started,
                        )
                else:
                    try:
                        os.remove(os.path.join(cwd, f))
                    except OSError:
                        pass
            if run_tests is not None:
                passed = rt(test_cmd)
                status = (
                    TestExecutionStatus.INFRA_ERROR
                    if passed is None
                    else TestExecutionStatus.PASSED
                    if passed
                    else TestExecutionStatus.TEST_FAILED
                )
                code = 127 if passed is None else 0 if passed else 1
                return receipt(
                    test_cmd, status, code=code, started=started
                )
            rc, output = run_target_command(
                test_cmd, cwd, _test_timeout_s(), capture=True
            )
            status = classify_test_exit_code(
                rc, _test_failure_exit_codes()
            )
            return receipt(
                test_cmd,
                status,
                code=(
                    None
                    if status in {
                        TestExecutionStatus.TIMED_OUT,
                        TestExecutionStatus.CANCELLED,
                    }
                    else 127 if rc is None else rc
                ),
                output=output,
                started=started,
            )
        finally:
            git(["checkout", "-q", "HEAD", "--", *impl_files], cwd)

    return GateContext(
        base_branch=base_branch,
        repository=repo,
        branch=branch,
        branch_subjects=subjects,
        published_subjects=published_subjects,
        declared_exemptions=declared_exemptions,
        malformed_exemption_lines=malformed_exemption_lines,
        touched_at_base=touched_at_base,
        base_resolvable=base_resolvable,
        changed_files=changed,
        open_pr=open_pr,
        open_pr_retry_sleep=retry_sleep,
        allow_untested=allow_untested,
        visual_ac=markers.visual_ac(),
        screenshots_read=screenshots_read,
        screenshots_receipt_available=screenshots_receipt_available,
        screenshots_receipt_unreadable=screenshots_receipt_unreadable,
        image_digests=image_digests,
        harness_test_cmd=markers.test_cmd(),
        env_test_cmd=env_test_cmd,
        exists_at_base=exists_at_base,
        run_tests=rt,
        run_red_tests=run_red_tests,
        revert_and_run=revert_and_run,
        red_must_fail=red_must_fail,
        verify_red_at_commit=verify_red_at_commit,
        durable_publish=durable_publish,
    )
