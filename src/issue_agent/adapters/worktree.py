"""Git worktree management + the GateContext builder for the in-process governor.

`Worktrees.ensure` / `.remove` port agent/lib.sh:ensure_worktree + cleanup.sh's
`git worktree remove`. `build_gate_context` is the git/fs fact-gatherer the
governor's `make_pretooluse_hook(build_ctx)` needs — it reads the worktree's
branch, the commit subjects since base, the changed-file list, the `.harness/`
markers, and wires the `run_tests` / `revert_and_run` / `exists_at_base`
callables (§1 + §10 of the gate) over real git.

git/subprocess is injected as `run` so the path/branch logic is testable with a
fake; the production default shells out. The §10 revert simulation mirrors
_pr_common.sh:prc_check_red_fails (revert impl to base, run tests, restore HEAD).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .governor import GateContext
from .store import HarnessMarkers


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str


GitRunner = Callable[[list[str], str], GitResult]  # (args, cwd) -> result


def _subprocess_git(args: list[str], cwd: str) -> GitResult:
    res = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
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
    ):
        self.repo_dir = repo_dir
        self.worktree_root = worktree_root
        self.base_branch = base_branch
        self._git = run

    def path(self, issue: int) -> str:
        return os.path.join(self.worktree_root, f"issue-{issue}")

    def ensure(self, issue: int, branch: str) -> str:
        """Ensure a worktree for the issue exists on `branch`; return its abs
        path. Port of ensure_worktree: reuse existing, else create from the
        branch ref or off origin/base."""
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
        return os.path.join(self.repo_dir, wt)

    def remove(self, issue: int) -> None:
        self._git(["worktree", "remove", self.path(issue), "--force"], self.repo_dir)
        self._git(["worktree", "prune"], self.repo_dir)

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


def _resolve_base_ref(git: GitRunner, cwd: str, base: str) -> str | None:
    for ref in (f"origin/{base}", base):
        if git(["rev-parse", "--verify", "--quiet", ref], cwd).code == 0:
            return ref
    return None


def build_gate_context(
    cwd: str,
    base_branch: str = "dev",
    env_test_cmd: str | None = None,
    red_must_fail: bool = True,
    verify_red_at_commit: bool = True,
    has_open_pr: bool = False,
    allow_untested: bool = False,
    git: GitRunner = _subprocess_git,
    run_tests: Callable[[str], bool] | None = None,
) -> GateContext:
    """Gather the worktree facts the governor's gate rules need. Pure-ish: all
    git access goes through the injected `git`, so a fake exercises the wiring
    without a real repo. `run_tests` defaults to a real shell eval in `cwd`."""
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

    def exists_at_base(path: str) -> bool:
        if not baseref:
            return False
        return git(["cat-file", "-e", f"{baseref}:{path}"], cwd).code == 0

    def _default_run_tests(cmd: str) -> bool:
        """Real shell eval of the test command, bounded by GATE_TEST_TIMEOUT_S
        (default 600s). A hang must resolve to 'did not pass' (False), never
        propagate — `evaluate_command` (which calls this via GateContext) runs
        inside governor.py's make_pretooluse_hook under a broad fail-OPEN
        except; an uncaught TimeoutExpired there would silently ALLOW the very
        command the gate exists to block. subprocess.run's own TimeoutExpired
        handling already kills the direct child (the `sh -c` process spawned by
        shell=True) before raising, so no explicit process-group cleanup is
        needed for the common case — a command that further backgrounds its
        own grandchildren can still outlive us, a pre-existing footgun of
        shell=True this doesn't newly introduce."""
        timeout = _test_timeout_s()
        try:
            return subprocess.run(cmd, shell=True, cwd=cwd, timeout=timeout).returncode == 0
        except subprocess.TimeoutExpired:
            print(
                f"[gate] TEST_CMD timed out after {timeout:g}s and was killed "
                f"(reported as a FAILING run, not a pass): {cmd}",
                file=sys.stderr,
            )
            return False

    rt = run_tests or _default_run_tests

    def revert_and_run(impl_files: tuple[str, ...]) -> bool:
        """§10: drop new impl / revert modified impl to base, run the test cmd,
        restore HEAD. Returns whether the suite PASSED with impl reverted (True
        ⇒ tautological). Fail-open (return False ⇒ 'genuine red') on any git
        error, matching prc_check_red_fails. A timed-out run (see
        `_default_run_tests`) also surfaces as this same False/'did not pass'
        signal — indistinguishable here from a real fail, which is the
        existing fail-open contract for §10, not something introduced by the
        timeout."""
        if not baseref:
            return False
        test_cmd = (markers.test_cmd() or env_test_cmd or "")
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
        has_open_pr=has_open_pr,
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
