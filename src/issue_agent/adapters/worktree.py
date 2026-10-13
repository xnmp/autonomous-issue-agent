"""Git worktree management for fix episodes and pinned review checkouts.

`Worktrees.ensure` / `.remove` port agent/lib.sh:ensure_worktree + cleanup.sh's
`git worktree remove`. git/subprocess is injected as `run` so the path/branch
logic is testable with a fake; the production default shells out.
"""

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


# Hard ceiling on any single `git` subprocess (review M20 — this runner had NO
# timeout at all, unlike `_GH_TIMEOUT_S` in github.py, added after a recorded
# `gh pr view` hang that blocked ~14 minutes and froze the whole review daemon
# because the poll loop is synchronous). This same runner is used on BOTH the
# dispatch path (`Worktrees.ensure`'s `git fetch origin`, a real network call)
# — an unbounded hang wedges the daemon, the identical failure class as the gh
# hang above, just via a different command. 60s is far above a healthy call
# (local git plumbing is sub-second; a `fetch` against a live remote is at most
# a few seconds) yet bounds a pathological one (a stale `index.lock`, a dead
# proxy holding a fetch open — see CLAUDE.md's proxy note).
_GIT_TIMEOUT_S = 60.0


def _subprocess_git(args: list[str], cwd: str) -> GitResult:
    """Run `git` in `cwd`, bounded by `_GIT_TIMEOUT_S`.

    NEVER raises: a timeout, a missing `git`, or a spawn failure all come back
    as a non-zero `GitResult`, so every `if result.code != 0` caller keeps
    working unchanged. A git failure is surfaced on OUR stderr with the captured
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


def _registered_branch(listing: str, expected_path: str) -> str | None:
    """None means absent; an empty branch means a registered detached checkout."""
    expected = os.path.realpath(expected_path)
    current = False
    branch: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ").strip()
            current = os.path.realpath(path) == expected
            if current:
                branch = ""
        elif current and line.startswith("branch refs/heads/"):
            return line.removeprefix("branch refs/heads/").strip()
        elif current and not line.strip():
            current = False
    return branch


class WorktreeUnavailable(RuntimeError):
    """`Worktrees.ensure` could not produce a usable worktree — every `git
    worktree add` attempt failed (a locked index, a stale lockfile, a race
    with a concurrent worktree op, disk pressure…) and the path still doesn't
    exist as a directory. Before this existed, `ensure` returned that (missing)
    path regardless, and the caller logged "worktree #N: ready on '<branch>' →
    <path>" and dispatched the agent straight into a nonexistent cwd (review
    M20).

    Raising before `runner.run` means the leg never returned: it records no
    leg receipt (a machine end), and `Daemon.tick`'s crash isolation puts the
    item back for the next poll."""


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

    def branch_for_issue(self, issue: int, fallback: str, *, preferred: str = "") -> str:
        """Return a retained harness branch for an older issue leg.

        New legs persist their branch in Store. This local-ref lookup recovers
        pre-persistence blocked legs after their worktree was reaped. A sole
        non-synthetic branch wins over any stale fallback. An absent branch
        returns ""; genuine ambiguity raises rather than guessing.
        """
        listing = self._git(["worktree", "list", "--porcelain"], self.repo_dir)
        if listing.code != 0:
            raise WorktreeUnavailable(f"cannot inspect worktrees for issue #{issue}")
        current = _registered_branch(listing.out, os.path.join(self.repo_dir, self.path(issue)))
        if current is not None:
            if issue_from_branch(current) != issue:
                raise WorktreeUnavailable(f"issue #{issue} checkout has foreign or detached branch {current!r}")
            return current
        refs = self._git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads"], self.repo_dir
        )
        if refs.code != 0:
            raise WorktreeUnavailable(f"cannot inspect retained branches for issue #{issue}")
        matches = [
            ref for ref in refs.out.splitlines()
            if issue_from_branch(ref) == issue
        ]
        if preferred in matches:
            return preferred
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
        abs_path = os.path.join(self.repo_dir, wt)
        if issue_from_branch(branch) != issue:
            raise WorktreeUnavailable(f"branch {branch!r} does not belong to issue #{issue}")
        listing = self._git(["worktree", "list", "--porcelain"], self.repo_dir)
        if listing.code != 0:
            raise WorktreeUnavailable(f"cannot inspect worktrees for issue #{issue}")
        existing = _registered_branch(listing.out, abs_path)
        if existing is not None and existing != branch:
            raise WorktreeUnavailable(
                f"issue #{issue} requires branch {branch!r}, but {abs_path!r} has {existing!r}"
            )
        if existing is None:
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
        if not self._path_exists(abs_path):
            raise WorktreeUnavailable(
                f"worktree for issue #{issue} on branch '{branch}' is not ready at "
                f"{abs_path!r} — every 'git worktree add' attempt failed (see the git "
                "runner's stderr surfacing for why)."
            )
        observed = self._git(["branch", "--show-current"], abs_path)
        if observed.code != 0 or observed.out.strip() != branch:
            raise WorktreeUnavailable(f"branch identity could not be verified at {abs_path!r}")
        return abs_path

    def sync(self, issue: int, branch: str, head: str) -> str:
        """The issue's worktree on `branch`, hard-reset to the pushed `head` a
        PR-stage leg was decided on. Unpushed local work is not part of the PR,
        so it is discarded; a remote head that moved refuses the leg."""
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
        with self._mutation_lock:
            self._remove(issue)

    def _remove(self, issue: int) -> None:
        listing = self._git(["worktree", "list", "--porcelain"], self.repo_dir)
        if listing.code != 0:
            raise WorktreeUnavailable(
                f"cannot inspect worktrees before removing issue #{issue}"
            )
        abs_path = os.path.join(self.repo_dir, self.path(issue))
        branch = _registered_branch(listing.out, abs_path)
        if (
            branch is None
            and not self._path_exists(abs_path)
        ):
            return
        if branch is None or issue_from_branch(branch) != issue:
            raise WorktreeUnavailable(f"refusing to remove unowned issue #{issue} checkout at {abs_path!r}")
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
            or _registered_branch(verified.out, abs_path) is not None
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
