"""Per-issue fix clones and per-PR review worktrees pinned at the reviewed
head. `git` is injected as `run` so the path and branch logic
tests with a fake; the default shells out."""

from __future__ import annotations

import os
import re
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

# A leg writes its checkout, so it can plant a submodule whose `.git` names a
# gitdir with its own config; the harness's git never enters one (#413: the
# sandboxed leg cannot touch the repository config, only its checkout).
_NO_RECURSION = ("-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false")


def _subprocess_git(args: list[str], cwd: str) -> GitResult:
    """`git` in `cwd`, bounded. Never raises: a timeout or spawn failure is a
    non-zero result, and every failure is written to stderr (a swallowed
    `worktree add` failure once handed back a path that did not exist)."""
    command = f"git {' '.join(args)} (cwd={cwd})"
    try:
        res = subprocess.run(["git", *_NO_RECURSION, *args], cwd=cwd, capture_output=True, text=True,
                             timeout=_GIT_TIMEOUT_S)
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


class WorktreeUnavailable(RuntimeError):
    """No usable worktree. Raised before the agent runs (never a path that
    does not exist, review M20), so the leg records no receipt: a machine end
    the next poll retries."""


class Worktrees:
    """Per-issue fix clones under `worktree_root`, relative to `repo_dir`,
    borrowing its objects (ADR 0001: tauri #1106, GIT-OBJECTS)."""

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

    def _is_clone(self, abs_path: str) -> bool:
        return self._path_exists(os.path.join(abs_path, ".git"))

    def _branch(self, issue: int, abs_path: str) -> str | None:
        """The branch of the clone on `abs_path`: None when there is none,
        "" when it is detached."""
        if not self._is_clone(abs_path):
            return None
        observed = self._git(["branch", "--show-current"], abs_path)
        if observed.code != 0:
            raise WorktreeUnavailable(f"cannot read the branch of issue #{issue}'s checkout")
        return observed.out.strip()

    def branch_for_issue(self, issue: int) -> str:
        """The branch of the issue's clone, else ""."""
        current = self._branch(issue, os.path.join(self.repo_dir, self.path(issue)))
        if current is None:
            return ""
        if issue_from_branch(current) != issue:
            raise WorktreeUnavailable(f"issue #{issue} checkout has foreign or detached branch {current!r}")
        return current

    def ensure(self, issue: int, branch: str) -> str:
        with self._mutation_lock:
            return self._ensure(issue, branch)

    def _ensure(self, issue: int, branch: str) -> str:
        """The issue's clone on `branch` (reused, else cloned and branched off
        origin/base), verified to exist and to be on that branch."""
        abs_path = os.path.join(self.repo_dir, self.path(issue))
        if issue_from_branch(branch) != issue:
            raise WorktreeUnavailable(f"branch {branch!r} does not belong to issue #{issue}")
        existing = self._branch(issue, abs_path)
        if existing is not None and existing != branch:
            raise WorktreeUnavailable(f"issue #{issue} requires branch {branch!r}, but {abs_path!r} has {existing!r}")
        if existing is None and self._path_exists(abs_path):
            # `.git` is a file: a git worktree from before the clones, which would write into the host's gitdir.
            raise WorktreeUnavailable(f"{abs_path!r} is a legacy git worktree, not a clone; remove it with "
                                      f"`git -C {self.repo_dir} worktree remove --force {abs_path}`")
        if existing is None:
            # Built aside, then renamed into place: a clone killed midway
            # leaves only the scratch path, which the next attempt replaces.
            scratch = abs_path + ".partial"
            shutil.rmtree(scratch, ignore_errors=True)
            url = self._git(["remote", "get-url", "origin"], self.repo_dir).out.strip()
            # The repository itself: git refuses to borrow through a linked checkout (`start --worktree`).
            common = self._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], self.repo_dir).out.strip()
            made = self._git(["clone", "--quiet", "--no-checkout", "--reference", common, url, scratch],
                             self.repo_dir).code == 0
            if made:
                # A branch already pushed (a leg pushed before the PR opened, then the clone was lost) resumes there.
                pushed = self._git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], scratch)
                start = f"origin/{branch}" if pushed.code == 0 else f"origin/{self.base_branch}"
                made = self._git(["checkout", "--quiet", "-b", branch, start], scratch).code == 0
            if made:
                try:  # a leg's launch mounts hooks/, which a bare init.templateDir leaves out
                    os.makedirs(os.path.join(scratch, ".git", "hooks"), exist_ok=True)
                    os.rename(scratch, abs_path)
                except OSError:
                    made = False
            if not made:
                raise WorktreeUnavailable(f"cannot clone issue #{issue}'s checkout on {branch!r} at {abs_path!r} "
                                          "(see the git runner's stderr)")
        observed = self._git(["branch", "--show-current"], abs_path)
        if observed.code != 0 or observed.out.strip() != branch:
            raise WorktreeUnavailable(f"branch identity could not be verified at {abs_path!r}")
        return abs_path

    def sync(self, issue: int, branch: str, head: str) -> str:
        """The issue's clone hard-reset to the pushed `head` a PR-stage leg
        was decided on; unpushed local work is not part of the PR. A remote
        head that moved refuses the leg."""
        with self._mutation_lock:
            path = self._ensure(issue, branch)
            remote = f"refs/remotes/origin/{branch}"
            # Fetched into the repository, which keeps only what its own refs
            # hold: the clone borrows the head from there, and a review fetches
            # heads under no ref, which the repository's next gc deletes.
            fetched = self._git(["fetch", "origin", f"+refs/heads/{branch}:{remote}", "--quiet"], self.repo_dir)
            observed = self._git(["rev-parse", remote], self.repo_dir)
            if fetched.code != 0 or observed.code != 0 or observed.out.strip() != head:
                raise WorktreeUnavailable(f"branch {branch!r} is not at the decided head {head[:12]}")
            # A link a leg left in `.git` would aim these ref and reflog writes at a host file.
            for root, dirs, files in os.walk(os.path.join(path, ".git")):
                if any(os.path.islink(os.path.join(root, name)) for name in (*dirs, *files)):
                    raise WorktreeUnavailable(f"issue #{issue}'s clone holds a link in {root!r}; not writing there")
            # The clone's tracking ref is what `push --force-with-lease` checks.
            for step in (["update-ref", remote, head], ["reset", "--hard", head]):
                if self._git(step, path).code != 0:
                    raise WorktreeUnavailable(f"cannot reset issue #{issue} checkout to {head[:12]}")
            return path

    def remote_head(self, branch: str) -> str:
        """The commit `branch` is at on the remote, read from the harness's own
        checkout, never the agent's report: a leg's push shows here before
        GitHub's PR view has it. Never raises; "" when unreadable."""
        ref = f"refs/heads/{branch}"
        listed = self._git(["ls-remote", "origin", ref], self.repo_dir)
        sha, _, name = listed.out.strip().partition("\t")
        return sha if listed.code == 0 and name == ref and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else ""

    def _discard(self, issue: int, abs_path: str) -> None:
        """Delete the clone on `abs_path`, verified. It is renamed away first,
        so a removal killed midway leaves only the scratch path the next
        clone replaces."""
        scratch = abs_path + ".partial"
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            os.rename(abs_path, scratch)
        except OSError as exc:
            raise WorktreeUnavailable(f"cannot remove issue #{issue}'s checkout ({exc.strerror})") from exc
        shutil.rmtree(scratch, ignore_errors=True)
        if self._path_exists(abs_path):
            raise WorktreeUnavailable(f"removal of issue #{issue}'s checkout could not be verified")

    def remove(self, issue: int) -> None:
        """Remove the issue's clone, verified."""
        with self._mutation_lock:
            abs_path = os.path.join(self.repo_dir, self.path(issue))
            branch = self._branch(issue, abs_path)
            if branch is None and not self._path_exists(abs_path):
                return
            if branch is None or issue_from_branch(branch) != issue:
                raise WorktreeUnavailable(f"refusing to remove unowned issue #{issue} checkout at {abs_path!r}")
            self._discard(issue, abs_path)


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
                # Re-verified after the (up to 60s each) fetches, right before mutating.
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
