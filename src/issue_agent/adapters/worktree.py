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

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

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

    def path(self, issue: int) -> str:
        return os.path.join(self.worktree_root, f"issue-{issue}")

    def ensure(self, issue: int, branch: str) -> str:
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
        listing = self._git(["worktree", "list", "--porcelain"], self.repo_dir)
        branch = _issue_worktree_branch(listing.out, issue)
        self._git(["worktree", "remove", self.path(issue), "--force"], self.repo_dir)
        self._git(["worktree", "prune"], self.repo_dir)
        if branch:
            self._git(["branch", "-D", branch], self.repo_dir)

    def reset(self, issue: int) -> None:
        """Discard all uncommitted work in the issue's worktree (tracked + untracked),
        leaving only the committed branch state. Used before contract adjudication so
        the adjudicating author sees the committed contract, never the implementer's
        in-progress code (argument-only blindness)."""
        wt = os.path.join(self.repo_dir, self.path(issue))
        self._git(["checkout", "--", "."], wt)
        self._git(["clean", "-fd"], wt)


# ── GateContext builder ───────────────────────────────────────────────────────
_DEFAULT_TEST_TIMEOUT_S = 600.0


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
    changed: tuple[str, ...] = ()
    if baseref:
        log = git(["log", f"{baseref}..HEAD", "--reverse", "--format=%s"], cwd)
        subjects = tuple(s for s in log.out.splitlines() if s)
        diff = git(["diff", "--name-only", f"{baseref}...HEAD"], cwd)
        changed = tuple(f for f in diff.out.splitlines() if f)

    markers = HarnessMarkers(cwd)

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

    def revert_and_run(impl_files: tuple[str, ...]) -> bool | None:
        """§10: drop new impl / revert modified impl to base, run the test cmd,
        restore HEAD. Returns whether the suite PASSED with impl reverted (True
        ⇒ tautological); None if the run was REFUSED (gate re-entry, #63) —
        the domain fails CLOSED on None. Fail-open (return False ⇒ 'genuine
        red') on any git error, matching prc_check_red_fails. A timed-out run
        (see `_default_run_tests`) also surfaces as this same False/'did not
        pass' signal — the existing fail-open contract for §10."""
        if not baseref:
            return False
        # Same precedence as every other gate rule — operator TEST_CMD first
        # (P1R-18). This site used to inline its own `markers.test_cmd() or
        # env_test_cmd` order, so §10 would have kept running the agent-written
        # command even after the domain's `resolve_test_cmd` stopped preferring
        # it. One resolver, one order.
        test_cmd = resolve_test_cmd(markers.test_cmd(), env_test_cmd)
        if not test_cmd:
            return False
        try:
            for f in impl_files:
                if exists_at_base(f):
                    git(["checkout", "-q", baseref, "--", f], cwd)
                else:
                    try:
                        os.remove(os.path.join(cwd, f))
                    except OSError:
                        pass
            passed = rt(test_cmd)
        finally:
            git(["checkout", "-q", "HEAD", "--", *impl_files], cwd)
        return passed

    return GateContext(
        base_branch=base_branch,
        branch=branch,
        branch_subjects=subjects,
        base_resolvable=base_resolvable,
        changed_files=changed,
        open_pr=open_pr,
        open_pr_retry_sleep=retry_sleep,
        allow_untested=allow_untested,
        ac_posted=markers.ac_posted(),
        test_plan_posted=markers.test_plan_posted(),
        adversarial_verdict=markers.adversarial_verdict(),
        visual_ac=markers.visual_ac(),
        screenshots_read=markers.screenshots_read(),
        harness_test_cmd=markers.test_cmd(),
        env_test_cmd=env_test_cmd,
        exists_at_base=exists_at_base,
        run_tests=rt,
        revert_and_run=revert_and_run,
        red_must_fail=red_must_fail,
        verify_red_at_commit=verify_red_at_commit,
    )
