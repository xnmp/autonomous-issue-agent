"""Git worktrees: per-issue fix checkouts and per-PR review checkouts pinned
at the reviewed head. `git` is injected as `run` so the path and branch logic
tests with a fake; the default shells out."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.refs import issue_from_branch


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str


GitRunner = Callable[[list[str], str], GitResult]  # (args, cwd) -> result

# A `git fetch` held open by a dead proxy or a stale lock must not wedge the
# synchronous daemon (review M20, the same class as the 14-minute `gh` hang).
_GIT_TIMEOUT_S = 60.0


def _subprocess_git(args: list[str], cwd: str) -> GitResult:
    """`git` in `cwd`, bounded. Never raises: a timeout or spawn failure is a
    non-zero result, and every failure is written to stderr (a swallowed
    `worktree add` failure once handed back a path that did not exist)."""
    command = f"git {' '.join(args)} (cwd={cwd})"
    try:
        res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"[worktree] git timed out after {_GIT_TIMEOUT_S:g}s and was killed: {command}", file=sys.stderr)
        return GitResult(1, "")
    except OSError as exc:
        print(f"[worktree] git failed to start: {command} — {exc}", file=sys.stderr)
        return GitResult(1, "")
    if res.returncode != 0:
        print(f"[worktree] {command} failed (exit {res.returncode}): {(res.stderr or '').strip()[:500]}",
              file=sys.stderr)
    return GitResult(res.returncode, res.stdout)


def _registered_branch(listing: str, expected_path: str) -> str | None:
    """None means absent; an empty branch means a registered detached checkout."""
    expected = os.path.realpath(expected_path)
    current = False
    branch: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = os.path.realpath(line.removeprefix("worktree ").strip()) == expected
            if current:
                branch = ""
        elif current and line.startswith("branch refs/heads/"):
            return line.removeprefix("branch refs/heads/").strip()
        elif current and not line.strip():
            current = False
    return branch


class WorktreeUnavailable(RuntimeError):
    """No usable worktree. Raised before the agent runs (never a path that
    does not exist, review M20), so the leg records no receipt: a machine end
    the next poll retries."""


class WorktreeBranchAmbiguous(RuntimeError):
    """Several retained branches for one issue: guessing could strand work."""


class Worktrees:
    """Per-issue worktrees under `worktree_root`, relative to `repo_dir`."""

    def __init__(self, repo_dir: str, worktree_root: str = ".claude/worktrees", base_branch: str = "dev",
                 run: GitRunner = _subprocess_git, path_exists: Callable[[str], bool] = os.path.isdir):
        self.repo_dir = repo_dir
        self.worktree_root = worktree_root
        self.base_branch = base_branch
        self._git = run
        self._path_exists = path_exists
        # `worktree add/remove/prune` mutate shared repository metadata and must
        # not race each other. Agent-owned git commands run in other processes
        # and cannot take this lock (residual: remote-ref lock contention).
        self._mutation_lock = threading.RLock()

    def path(self, issue: int) -> str:
        return os.path.join(self.worktree_root, f"issue-{issue}")

    def _listing(self, cwd: str, why: str) -> str:
        listing = self._git(["worktree", "list", "--porcelain"], cwd)
        if listing.code != 0:
            raise WorktreeUnavailable(why)
        return listing.out

    def prune_validate(self) -> int:
        """Prune stale metadata and validate the remaining registry (recovery)."""
        with self._mutation_lock:
            if self._git(["worktree", "prune"], self.repo_dir).code != 0:
                raise WorktreeUnavailable("git worktree prune failed during recovery")
            listing = self._listing(self.repo_dir, "git worktree list failed during recovery")
            paths = [line.removeprefix("worktree ").strip() for line in listing.splitlines()
                     if line.startswith("worktree ")]
            if missing := [path for path in paths if not self._path_exists(path)]:
                raise WorktreeUnavailable(f"worktree registry contains missing paths: {missing!r}")
            return len(paths)

    def branch_for_issue(self, issue: int, fallback: str) -> str:
        """The issue's retained branch: the checkout's own, else the sole
        local branch for the issue (a non-synthetic one wins over the
        fallback), else "". Genuine ambiguity raises rather than guessing."""
        listing = self._listing(self.repo_dir, f"cannot inspect worktrees for issue #{issue}")
        current = _registered_branch(listing, os.path.join(self.repo_dir, self.path(issue)))
        if current is not None:
            if issue_from_branch(current) != issue:
                raise WorktreeUnavailable(f"issue #{issue} checkout has foreign or detached branch {current!r}")
            return current
        refs = self._git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], self.repo_dir)
        if refs.code != 0:
            raise WorktreeUnavailable(f"cannot inspect retained branches for issue #{issue}")
        matches = [ref for ref in refs.out.splitlines() if issue_from_branch(ref) == issue]
        if len(matches) <= 1:
            return matches[0] if matches else ""
        retained = [ref for ref in matches if ref not in {fallback, f"fix/{issue}-issue"}]
        if len(retained) == 1:
            return retained[0]
        raise WorktreeBranchAmbiguous(f"cannot recover issue #{issue}: multiple retained branches {matches!r}")

    def ensure(self, issue: int, branch: str) -> str:
        with self._mutation_lock:
            return self._ensure(issue, branch)

    def _ensure(self, issue: int, branch: str) -> str:
        """The issue's worktree on `branch` (reused, else added from the branch
        or off origin/base), verified to exist and to be on that branch."""
        wt = self.path(issue)
        abs_path = os.path.join(self.repo_dir, wt)
        if issue_from_branch(branch) != issue:
            raise WorktreeUnavailable(f"branch {branch!r} does not belong to issue #{issue}")
        existing = _registered_branch(self._listing(self.repo_dir, f"cannot inspect worktrees for issue #{issue}"),
                                      abs_path)
        if existing is not None and existing != branch:
            raise WorktreeUnavailable(f"issue #{issue} requires branch {branch!r}, but {abs_path!r} has {existing!r}")
        if existing is None:
            self._git(["fetch", "origin", self.base_branch, "--quiet"], self.repo_dir)
            if self._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], self.repo_dir).code == 0:
                self._git(["worktree", "add", wt, branch], self.repo_dir)
            elif self._git(["worktree", "add", "-b", branch, wt, f"origin/{self.base_branch}"], self.repo_dir).code:
                self._git(["worktree", "add", "-b", branch, wt, self.base_branch], self.repo_dir)
        if not self._path_exists(abs_path):
            raise WorktreeUnavailable(f"worktree for issue #{issue} on branch '{branch}' is not ready at "
                                      f"{abs_path!r} — every 'git worktree add' attempt failed (see the git "
                                      "runner's stderr surfacing for why).")
        observed = self._git(["branch", "--show-current"], abs_path)
        if observed.code != 0 or observed.out.strip() != branch:
            raise WorktreeUnavailable(f"branch identity could not be verified at {abs_path!r}")
        return abs_path

    def sync(self, issue: int, branch: str, head: str) -> str:
        """The issue's worktree hard-reset to the pushed `head` a PR-stage leg
        was decided on; unpushed local work is not part of the PR. A remote
        head that moved refuses the leg."""
        with self._mutation_lock:
            path = self._ensure(issue, branch)
            remote = f"refs/remotes/origin/{branch}"
            fetched = self._git(["fetch", "origin", f"+refs/heads/{branch}:{remote}", "--quiet"], path)
            observed = self._git(["rev-parse", remote], path)
            if fetched.code != 0 or observed.code != 0 or observed.out.strip() != head:
                raise WorktreeUnavailable(f"branch {branch!r} is not at the decided head {head[:12]}")
            if self._git(["reset", "--hard", head], path).code != 0:
                raise WorktreeUnavailable(f"cannot reset issue #{issue} checkout to {head[:12]}")
            return path

    def remove(self, issue: int) -> None:
        """Remove the issue's worktree and its branch, each step verified."""
        with self._mutation_lock:
            listing = self._listing(self.repo_dir, f"cannot inspect worktrees before removing issue #{issue}")
            abs_path = os.path.join(self.repo_dir, self.path(issue))
            branch = _registered_branch(listing, abs_path)
            if branch is None and not self._path_exists(abs_path):
                return
            if branch is None or issue_from_branch(branch) != issue:
                raise WorktreeUnavailable(f"refusing to remove unowned issue #{issue} checkout at {abs_path!r}")
            if self._git(["worktree", "remove", self.path(issue), "--force"], self.repo_dir).code != 0:
                raise WorktreeUnavailable(f"git refused to remove the worktree for issue #{issue}")
            if self._git(["worktree", "prune"], self.repo_dir).code != 0:
                raise WorktreeUnavailable(f"git refused to prune worktrees after removing issue #{issue}")
            verified = self._git(["worktree", "list", "--porcelain"], self.repo_dir)
            if (verified.code != 0 or _registered_branch(verified.out, abs_path) is not None
                    or self._path_exists(abs_path)):
                raise WorktreeUnavailable(f"worktree removal for issue #{issue} could not be verified")
            if branch:
                if self._git(["branch", "-D", branch], self.repo_dir).code != 0:
                    raise WorktreeUnavailable(f"git refused to delete retained branch {branch!r}")
                state = self._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], self.repo_dir)
                if state.code == 0 or state.code > 1:
                    raise WorktreeUnavailable(f"branch deletion for {branch!r} could not be verified")


class ReviewCheckouts:
    """Detached per-PR worktrees pinned at the exact reviewed head. A review
    once read the operator's live checkout and quoted stale code while
    stamping the fresh head (keifu #165): the verdict binds a SHA, so the
    reviewed code must be that SHA, out of reach of the operator's work."""

    def __init__(self, repo_dir: str, worktree_root: str = ".claude/worktrees", run: GitRunner = _subprocess_git,
                 path_exists: Callable[[str], bool] = os.path.isdir,
                 remove_tree: Callable[[str], None] | None = None):
        self.repo_dir = repo_dir
        self.worktree_root = worktree_root
        self._git = run
        self._path_exists = path_exists
        self._remove_tree = remove_tree if remove_tree is not None else (
            lambda p: shutil.rmtree(p, ignore_errors=False))
        self._mutation_lock = threading.RLock()
        self._ensured: dict[int, str] = {}  # pr -> last ensured head

    def path(self, pr: int) -> str:
        return os.path.join(self.worktree_root, f"review-pr-{pr}")

    def ensure(self, pr: int, head_sha: str, base_ref: str, base_sha: str) -> str:
        """An absolute path whose checkout IS `head_sha`, with `base_sha` (the
        tip of `base_ref`) present so the reviewer can diff base...head, or
        `WorktreeUnavailable`: never a stale or dirty view."""
        if not head_sha or not base_ref or not base_sha:
            raise WorktreeUnavailable("review checkout needs the head SHA and the base ref and SHA")
        with self._mutation_lock:
            abs_path = os.path.join(self.repo_dir, self.path(pr))
            is_worktree = False
            if self._path_exists(abs_path):
                # A plain unregistered directory here makes git find the PARENT
                # repo, so a reset would destroy the operator's live checkout:
                # only a toplevel that IS this path may be mutated.
                is_worktree = self._is_this_worktree(pr, abs_path)
                if not is_worktree:
                    self._remove_stale_dir(pr, abs_path)
            if (is_worktree and self._ensured.get(pr) == head_sha and self._has_commit(base_sha)
                    and self._git(["rev-parse", "HEAD"], abs_path).out.strip() == head_sha):
                return abs_path
            # GitHub always serves refs/pull/<n>/head; a bare SHA fetch may be refused.
            # Two fetches: one missing ref (a renamed base) fails a joint fetch whole.
            for ref in (f"refs/pull/{pr}/head", f"refs/heads/{base_ref}"):
                self._git(["fetch", "origin", ref, "--quiet"], self.repo_dir)
            for name, sha in (("reviewed head", head_sha), ("base", base_sha)):
                if not self._has_commit(sha):
                    raise WorktreeUnavailable(f"{name} {sha} for PR #{pr} is not reachable after fetching its "
                                              "ref (force-pushed away, or the base renamed?) — defer and re-observe")
            if is_worktree:
                # Re-verified after the (up to 60s) fetch, right before mutating.
                if not self._is_this_worktree(pr, abs_path):
                    raise WorktreeUnavailable(f"review worktree for PR #{pr} stopped being a worktree during "
                                              "fetch — defer")
                # -fd, not -fdx: ignored build caches cannot alter tracked content.
                for refresh in (["checkout", "--detach", head_sha], ["reset", "--hard", head_sha], ["clean", "-fd"]):
                    if self._git(refresh, abs_path).code != 0:
                        raise WorktreeUnavailable(f"review worktree for PR #{pr} could not be refreshed "
                                                  f"(git {refresh[0]} failed) — defer")
            else:
                # Prune first: a registration without its directory fails `add` forever.
                self._git(["worktree", "prune"], self.repo_dir)
                made = self._git(["worktree", "add", "--detach", self.path(pr), head_sha], self.repo_dir)
                if made.code != 0 or not self._path_exists(abs_path):
                    raise WorktreeUnavailable(f"review worktree for PR #{pr} could not be created at {abs_path!r}")
            confirmed = self._git(["rev-parse", "HEAD"], abs_path).out.strip()
            if confirmed != head_sha:
                raise WorktreeUnavailable(f"review worktree for PR #{pr} is at {confirmed[:12]!r}, not the "
                                          f"reviewed head {head_sha[:12]!r}")
            self._ensured[pr] = head_sha
            return abs_path

    def has_changes(self, base_sha: str, head_sha: str) -> bool:
        """Whether base...head (GitHub's PR diff) changes anything. A git
        failure is not an empty diff: it raises, so the round defers."""
        # --name-only, not --quiet: --quiet's "differs" exit is the same 1 a
        # timeout or a missing object returns.
        result = self._git(["diff", "--name-only", f"{base_sha}...{head_sha}"], self.repo_dir)
        if result.code != 0:
            raise WorktreeUnavailable(f"git diff {base_sha[:12]}...{head_sha[:12]} failed — defer")
        return bool(result.out.strip())

    def _has_commit(self, sha: str) -> bool:
        return self._git(["cat-file", "-e", f"{sha}^{{commit}}"], self.repo_dir).code == 0

    def _is_this_worktree(self, pr: int, abs_path: str) -> bool:
        """Whether git says the directory's toplevel is the directory. A link
        never is (a link to the parent would pass the comparison). A failed
        rev-parse cannot classify, and an unclassified directory defers,
        never deleted as garbage (#63)."""
        if os.path.islink(abs_path):
            return False
        top = self._git(["rev-parse", "--show-toplevel"], abs_path)
        if top.code != 0 or not top.out.strip():
            raise WorktreeUnavailable(f"review checkout for PR #{pr} at {abs_path!r} could not be classified "
                                      "(rev-parse failed) — defer")
        return os.path.realpath(top.out.strip()) == os.path.realpath(abs_path)

    def _remove_stale_dir(self, pr: int, abs_path: str) -> None:
        """Delete a plain directory (or link) squatting on the worktree path,
        only strictly inside the worktree root; a link is unlinked, never
        followed."""
        try:
            if os.path.islink(abs_path):
                os.unlink(abs_path)
            else:
                root = os.path.realpath(os.path.join(self.repo_dir, self.worktree_root))
                if not os.path.realpath(abs_path).startswith(root + os.sep):
                    raise WorktreeUnavailable(f"stale review checkout for PR #{pr} at {abs_path!r} is outside the "
                                              "worktree root — refusing to delete")
                self._remove_tree(abs_path)
        except OSError as exc:
            raise WorktreeUnavailable(f"stale review checkout for PR #{pr} could not be removed ({exc}) — "
                                      "defer") from exc
        self._git(["worktree", "prune"], self.repo_dir)

    def remove(self, pr: int) -> None:
        """Best-effort removal after a settled round; the next round recreates."""
        with self._mutation_lock:
            self._ensured.pop(pr, None)
            self._git(["worktree", "remove", "--force", self.path(pr)], self.repo_dir)
            self._git(["worktree", "prune"], self.repo_dir)
