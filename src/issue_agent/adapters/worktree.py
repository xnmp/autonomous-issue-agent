"""Git worktree management for fix episodes and pinned review checkouts.

`Worktrees.ensure` / `.remove` port agent/lib.sh:ensure_worktree + cleanup.sh's
`git worktree remove`. git/subprocess is injected as `run` so the path/branch
logic is testable with a fake; the production default shells out.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from ..domain.git_remote import require_repository_remote, require_safe_fetch_url
from ..domain.refs import issue_from_branch
from ..domain.resolve_workspace import (
    ContractResetResult,
    ContractRevision,
    EpisodeWorkspaceHandoff,
    EpisodeWorkspacePrepared,
    NoPriorResolveAdmission,
    ObservedResolveBase,
)
from ..domain.workflow_identity import canonical_workflow_repository


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


def _contract_ref(namespace: str, identity: str) -> str:
    if not isinstance(identity, str) or not identity or len(identity) > 4096:
        raise WorktreeUnavailable("contract reference identity is invalid")
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"refs/issue-agent/{namespace}/{digest}"


def _contract_git(
    run: GitRunner, args: list[str], cwd: str, failure: str
) -> GitResult:
    try:
        result = run(args, cwd)
    except Exception as exc:
        raise WorktreeUnavailable(failure) from exc
    if not isinstance(result, GitResult):
        raise WorktreeUnavailable(failure)
    return result


def _resolved_git_path(cwd: str, value: str) -> str:
    path = value.strip()
    if not path:
        return ""
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _validate_contract_checkout(
    cwd: str,
    issue: int,
    expected_branch: str,
    run: GitRunner,
    *,
    require_clean: bool,
) -> ContractRevision:
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise WorktreeUnavailable("contract checkout requires a positive issue number")
    if not isinstance(cwd, str) or not cwd:
        raise WorktreeUnavailable("contract checkout path is invalid")
    if issue_from_branch(expected_branch) != issue:
        raise WorktreeUnavailable(
            f"branch {expected_branch!r} does not belong to issue #{issue}"
        )

    top = _contract_git(
        run,
        ["rev-parse", "--show-toplevel"],
        cwd,
        f"cannot verify the contract checkout for issue #{issue}",
    )
    if top.code != 0 or os.path.realpath(top.out.strip()) != os.path.realpath(cwd):
        raise WorktreeUnavailable(
            f"contract checkout for issue #{issue} is not its exact Git toplevel"
        )
    listing = _contract_git(
        run,
        ["worktree", "list", "--porcelain"],
        cwd,
        f"cannot inspect worktrees for issue #{issue}",
    )
    registered = (
        _registered_branch(listing.out, cwd) if listing.code == 0 else None
    )
    if registered != expected_branch:
        raise WorktreeUnavailable(
            f"contract checkout for issue #{issue} is not registered on "
            f"{expected_branch!r}"
        )
    branch = _contract_git(
        run,
        ["branch", "--show-current"],
        cwd,
        f"cannot verify the contract branch for issue #{issue}",
    )
    if branch.code != 0 or branch.out.strip() != expected_branch:
        raise WorktreeUnavailable(
            f"contract checkout for issue #{issue} is detached or on another branch"
        )
    head = _contract_git(
        run,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        cwd,
        f"cannot resolve the contract HEAD for issue #{issue}",
    )
    if head.code != 0:
        raise WorktreeUnavailable(f"contract HEAD is unavailable for issue #{issue}")
    try:
        revision = ContractRevision(expected_branch, head.out.strip())
    except (TypeError, ValueError) as exc:
        raise WorktreeUnavailable(
            f"contract HEAD is invalid for issue #{issue}"
        ) from exc
    if require_clean:
        status = _contract_git(
            run,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd,
            f"cannot inspect contract checkout state for issue #{issue}",
        )
        if status.code != 0 or status.out:
            raise WorktreeUnavailable(
                f"contract checkout for issue #{issue} is not clean"
            )
    return revision


def _read_ref(run: GitRunner, cwd: str, ref: str) -> str | None:
    result = _contract_git(
        run,
        ["rev-parse", "--verify", "--quiet", ref],
        cwd,
        f"cannot inspect retained Git reference {ref!r}",
    )
    if result.code == 1:
        return None
    if result.code != 0:
        raise WorktreeUnavailable(f"cannot inspect retained Git reference {ref!r}")
    value = result.out.strip()
    if len(value) not in (40, 64) or any(c not in "0123456789abcdef" for c in value):
        raise WorktreeUnavailable(f"retained Git reference {ref!r} is invalid")
    return value


def _create_or_verify_ref(
    run: GitRunner, cwd: str, ref: str, target: str
) -> None:
    existing = _read_ref(run, cwd, ref)
    if existing is not None:
        if existing != target:
            raise WorktreeUnavailable(
                f"retained Git reference {ref!r} conflicts with this operation"
            )
        return
    _contract_git(
        run,
        ["update-ref", ref, target, "0" * len(target)],
        cwd,
        f"cannot create retained Git reference {ref!r}",
    )
    # A concurrent creator of the identical pin is harmless; a different pin
    # is never overwritten.
    observed = _read_ref(run, cwd, ref)
    if observed != target:
        raise WorktreeUnavailable(f"cannot create retained Git reference {ref!r}")


def _checkout_status(
    run: GitRunner,
    cwd: str,
    issue: int,
    *,
    include_ignored: bool = False,
) -> str:
    args = ["status", "--porcelain=v1", "--untracked-files=all"]
    if include_ignored:
        args.append("--ignored=matching")
    status = _contract_git(
        run,
        args,
        cwd,
        f"cannot inspect reset state for issue #{issue}",
    )
    if status.code != 0:
        raise WorktreeUnavailable(f"cannot inspect reset state for issue #{issue}")
    return status.out


def _archive_marker(
    archive_ref: str, revision: ContractRevision
) -> str:
    digest = archive_ref.rsplit("/", 1)[-1]
    return (
        f"issue-agent contract reset {digest} "
        f"{revision.branch} {revision.head_sha}"
    )


def _archive_original_head(
    run: GitRunner,
    cwd: str,
    archive_ref: str,
    marker: str,
    branch: str,
) -> str:
    row = _contract_git(
        run,
        ["rev-list", "--parents", "-n", "1", archive_ref],
        cwd,
        f"cannot verify reset archive {archive_ref!r}",
    )
    subject = _contract_git(
        run,
        ["show", "-s", "--format=%s", archive_ref],
        cwd,
        f"cannot verify reset archive {archive_ref!r}",
    )
    if row.code != 0 or subject.code != 0:
        raise WorktreeUnavailable(f"cannot verify reset archive {archive_ref!r}")
    parts = row.out.split()
    actual_subject = subject.out.rstrip("\n")
    clean_marker = actual_subject == marker and len(parts) == 2
    stash_marker = actual_subject == f"On {branch}: {marker}" and len(parts) in (3, 4)
    if not (clean_marker or stash_marker):
        raise WorktreeUnavailable(
            f"reset archive {archive_ref!r} conflicts with this operation"
        )
    original = parts[1]
    if len(original) not in (40, 64):
        raise WorktreeUnavailable(f"reset archive {archive_ref!r} is invalid")
    return original


def _recover_interrupted_stash(
    run: GitRunner,
    cwd: str,
    archive_ref: str,
    marker: str,
    branch: str,
    current_head: str,
    clean: bool,
) -> str | None:
    stashes = _contract_git(
        run,
        ["stash", "list", "--format=%H%x09%gs"],
        cwd,
        f"cannot inspect retained reset work for {archive_ref!r}",
    )
    if stashes.code != 0:
        raise WorktreeUnavailable(
            f"cannot inspect retained reset work for {archive_ref!r}"
        )
    expected_subject = f"On {branch}: {marker}"
    candidates = [
        row.split("\t", 1)[0]
        for row in stashes.out.splitlines()
        if "\t" in row and row.split("\t", 1)[1] == expected_subject
    ]
    if not candidates:
        return None
    if len(candidates) != 1 or not clean:
        raise WorktreeUnavailable(
            f"interrupted reset archive {archive_ref!r} is ambiguous or stale"
        )
    candidate = candidates[0]
    original = _archive_original_head(
        run, cwd, candidate, marker, branch
    )
    if original != current_head:
        raise WorktreeUnavailable(
            f"interrupted reset archive {archive_ref!r} is stale"
        )
    _create_or_verify_ref(run, cwd, archive_ref, candidate)
    return candidate


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

    def _managed_issue_checkout(self, issue: int) -> str:
        repo_root = os.path.realpath(self.repo_dir)
        managed_root = os.path.realpath(
            os.path.join(self.repo_dir, self.worktree_root)
        )
        cwd = os.path.join(self.repo_dir, self.path(issue))
        checkout = os.path.realpath(cwd)
        exact_child = os.path.normpath(
            os.path.join(managed_root, f"issue-{issue}")
        )
        try:
            checkout_inside_managed = (
                os.path.commonpath((checkout, managed_root)) == managed_root
            )
        except ValueError:
            checkout_inside_managed = False
        if (
            checkout == repo_root
            or checkout == managed_root
            or checkout != exact_child
            or not checkout_inside_managed
        ):
            raise WorktreeUnavailable(
                f"workspace path for issue #{issue} is outside its managed root"
            )
        return cwd

    def _repository_remote(
        self, repository: str, approved_host: str
    ) -> str:
        top = _contract_git(
            self._git,
            ["rev-parse", "--show-toplevel"],
            self.repo_dir,
            "cannot verify the configured repository root",
        )
        if (
            top.code != 0
            or os.path.realpath(top.out.strip())
            != os.path.realpath(self.repo_dir)
        ):
            raise WorktreeUnavailable(
                "workspace repository is not its exact Git toplevel"
            )
        remote = _contract_git(
            self._git,
            ["remote", "get-url", "origin"],
            self.repo_dir,
            "configured origin URL is unavailable",
        )
        remote_url = remote.out.strip()
        if remote.code != 0 or not remote_url or "\n" in remote_url:
            raise WorktreeUnavailable("configured origin URL is unavailable")
        try:
            require_repository_remote(remote_url, repository, approved_host)
            require_safe_fetch_url(remote_url)
        except (TypeError, ValueError) as exc:
            raise WorktreeUnavailable(
                "configured origin is not a safe matching workspace repository; "
                "use a credential helper instead of URL credentials"
            ) from exc
        return remote_url

    def _check_branch_ref(self, ref: str) -> None:
        checked = _contract_git(
            self._git,
            ["check-ref-format", ref],
            self.repo_dir,
            f"Git branch reference {ref!r} is invalid",
        )
        if checked.code != 0:
            raise WorktreeUnavailable(f"Git branch reference {ref!r} is invalid")

    def _verify_episode_pin(self, base: ObservedResolveBase) -> None:
        pinned = _read_ref(self._git, self.repo_dir, base.pin_ref)
        if pinned != base.commit_sha:
            raise WorktreeUnavailable(
                f"episode base pin {base.pin_ref!r} is missing or conflicts"
            )
        commit = _contract_git(
            self._git,
            ["rev-parse", "--verify", f"{base.pin_ref}^{{commit}}"],
            self.repo_dir,
            f"episode base pin {base.pin_ref!r} is not a commit",
        )
        if commit.code != 0 or commit.out.strip() != base.commit_sha:
            raise WorktreeUnavailable(
                f"episode base pin {base.pin_ref!r} is not the observed commit"
            )

    def _verify_workspace_common_dir(self, cwd: str) -> None:
        root_common = _contract_git(
            self._git,
            ["rev-parse", "--git-common-dir"],
            self.repo_dir,
            "cannot verify repository common directory",
        )
        checkout_common = _contract_git(
            self._git,
            ["rev-parse", "--git-common-dir"],
            cwd,
            "cannot verify workspace common directory",
        )
        if (
            root_common.code != 0
            or checkout_common.code != 0
            or _resolved_git_path(self.repo_dir, root_common.out)
            != _resolved_git_path(cwd, checkout_common.out)
        ):
            raise WorktreeUnavailable(
                "episode workspace belongs to another Git repository"
            )

    def observe_and_pin_episode_base(
        self,
        repository: str,
        *,
        observed_at: datetime,
        approved_host: str = "github.com",
    ) -> ObservedResolveBase:
        """Fetch and pin one exact configured upstream branch observation.

        The caller must hold exclusive deployment authority and prove the
        repository quiescent. This local Git operation grants no workflow
        authority; its stable scratch ref is safe only under that barrier.
        """

        with self._mutation_lock:
            remote_url = self._repository_remote(repository, approved_host)
            base_ref = f"refs/heads/{self.base_branch}"
            self._check_branch_ref(base_ref)
            if (
                not isinstance(observed_at, datetime)
                or observed_at.tzinfo is None
                or observed_at.utcoffset() is None
            ):
                raise WorktreeUnavailable(
                    "workspace observation requires an aware datetime"
                )
            canonical_repository = canonical_workflow_repository(repository)
            observation_ref = (
                "refs/issue-agent/episode-observations/"
                + hashlib.sha256(
                    f"{canonical_repository}\0{base_ref}".encode()
                ).hexdigest()
            )
            fetched = _contract_git(
                self._git,
                [
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--no-write-fetch-head",
                    "--force",
                    remote_url,
                    f"+{base_ref}:{observation_ref}",
                ],
                self.repo_dir,
                "cannot fetch the configured upstream base",
            )
            if fetched.code != 0:
                # Never consult the observation ref after a failed fetch: it
                # may contain a prior same-clock retry, which is not freshness.
                raise WorktreeUnavailable(
                    "cannot fetch the configured upstream base"
                )
            commit = _contract_git(
                self._git,
                ["rev-parse", "--verify", f"{observation_ref}^{{commit}}"],
                self.repo_dir,
                "fetched upstream base is not a commit",
            )
            if commit.code != 0:
                raise WorktreeUnavailable("fetched upstream base is not a commit")
            try:
                base = ObservedResolveBase(
                    repository, base_ref, commit.out.strip(), observed_at
                )
            except (TypeError, ValueError) as exc:
                raise WorktreeUnavailable(
                    "fetched upstream base identity is invalid"
                ) from exc
            _create_or_verify_ref(
                self._git, self.repo_dir, base.pin_ref, base.commit_sha
            )
            self._verify_episode_pin(base)
            return base

    def _prepare_episode_checkout(
        self, handoff: EpisodeWorkspaceHandoff
    ) -> str:
        issue = handoff.source.fence.key.target.number
        branch = handoff.branch
        branch_ref = f"refs/heads/{branch}"
        self._check_branch_ref(branch_ref)
        cwd = self._managed_issue_checkout(issue)
        listing = _contract_git(
            self._git,
            ["worktree", "list", "--porcelain"],
            self.repo_dir,
            f"cannot inspect worktrees for issue #{issue}",
        )
        if listing.code != 0:
            raise WorktreeUnavailable(f"cannot inspect worktrees for issue #{issue}")
        registered = _registered_branch(listing.out, cwd)
        if registered is not None:
            if registered != branch:
                raise WorktreeUnavailable(
                    f"episode workspace for issue #{issue} is registered on "
                    f"unexpected branch {registered!r}"
                )
        else:
            if self._path_exists(cwd):
                raise WorktreeUnavailable(
                    f"episode workspace path for issue #{issue} is not registered"
                )
            refs = _contract_git(
                self._git,
                ["for-each-ref", "--format=%(refname:short)", "refs/heads"],
                self.repo_dir,
                f"cannot inspect retained branches for issue #{issue}",
            )
            if refs.code != 0:
                raise WorktreeUnavailable(
                    f"cannot inspect retained branches for issue #{issue}"
                )
            retained = [
                value
                for value in refs.out.splitlines()
                if issue_from_branch(value) == issue
            ]
            if retained == [branch]:
                added = _contract_git(
                    self._git,
                    ["worktree", "add", self.path(issue), branch],
                    self.repo_dir,
                    f"cannot restore retained workspace for issue #{issue}",
                )
            elif retained:
                raise WorktreeUnavailable(
                    f"retained workspace branches for issue #{issue} are ambiguous: {retained!r}"
                )
            elif not isinstance(
                handoff.predecessor, NoPriorResolveAdmission
            ):
                raise WorktreeUnavailable(
                    f"prior admitted workspace for issue #{issue} is unavailable"
                )
            else:
                added = _contract_git(
                    self._git,
                    [
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        self.path(issue),
                        handoff.base.pin_ref,
                    ],
                    self.repo_dir,
                    f"cannot create episode workspace for issue #{issue}",
                )
            if added.code != 0 or not self._path_exists(cwd):
                raise WorktreeUnavailable(
                    f"cannot prepare episode workspace for issue #{issue}"
                )
        self._verify_workspace_common_dir(cwd)
        _validate_contract_checkout(
            cwd, issue, branch, self._git, require_clean=False
        )
        return cwd

    def prepare_episode_workspace(
        self,
        handoff: EpisodeWorkspaceHandoff,
        *,
        approved_host: str = "github.com",
    ) -> ContractResetResult:
        """Archive displaced work and restore the handoff's pinned base.

        The caller must hold exclusive deployment authority, prove quiescence,
        and supply its durable predecessor/absence proof. This method performs
        local Git effects only; the handoff is not admission authority.
        """

        if not isinstance(handoff, EpisodeWorkspaceHandoff):
            raise WorktreeUnavailable("episode workspace requires an exact handoff")
        with self._mutation_lock:
            self._repository_remote(handoff.base.repository, approved_host)
            expected_base_ref = f"refs/heads/{self.base_branch}"
            if handoff.base.base_ref != expected_base_ref:
                raise WorktreeUnavailable(
                    "episode base disagrees with the configured base branch"
                )
            self._check_branch_ref(handoff.base.base_ref)
            self._verify_episode_pin(handoff.base)
            cwd = self._prepare_episode_checkout(handoff)
            result = self._reset_contract(
                handoff.source.fence.key.target.number,
                handoff.revision,
                handoff.handoff_id,
            )
            # Preservation must not displace or rewrite the upstream pin it is
            # resetting to. Verify both retained identities before returning.
            self._verify_episode_pin(handoff.base)
            if result.archive_ref != handoff.archive_ref:
                raise WorktreeUnavailable(
                    "episode workspace reset returned a foreign archive"
                )
            archived = _read_ref(self._git, cwd, result.archive_ref)
            if archived is None:
                raise WorktreeUnavailable(
                    "episode workspace reset archive is unavailable"
                )
            _archive_original_head(
                self._git,
                cwd,
                result.archive_ref,
                _archive_marker(result.archive_ref, handoff.revision),
                handoff.branch,
            )
            return result

    def verify_episode_workspace(
        self,
        completed: EpisodeWorkspacePrepared,
        *,
        approved_host: str = "github.com",
    ) -> None:
        """Read-only verification of a completed workspace acknowledgement.

        The acknowledgement and its caller-owned workflow authority remain the
        permission boundary; this method only checks retained local evidence.
        """

        if not isinstance(completed, EpisodeWorkspacePrepared):
            raise WorktreeUnavailable(
                "episode workspace verification requires its acknowledgement"
            )
        handoff = completed.handoff
        with self._mutation_lock:
            self._repository_remote(handoff.base.repository, approved_host)
            expected_base_ref = f"refs/heads/{self.base_branch}"
            if handoff.base.base_ref != expected_base_ref:
                raise WorktreeUnavailable(
                    "episode base disagrees with the configured base branch"
                )
            self._check_branch_ref(handoff.base.base_ref)
            self._verify_episode_pin(handoff.base)
            issue = handoff.source.fence.key.target.number
            cwd = self._managed_issue_checkout(issue)
            self._verify_workspace_common_dir(cwd)
            observed = _validate_contract_checkout(
                cwd, issue, handoff.branch, self._git, require_clean=True
            )
            if observed != handoff.revision or _checkout_status(
                self._git, cwd, issue, include_ignored=True
            ):
                raise WorktreeUnavailable(
                    f"episode workspace for issue #{issue} is not at its exact base"
                )
            archived = _read_ref(
                self._git, cwd, completed.result.archive_ref
            )
            if archived is None:
                raise WorktreeUnavailable(
                    f"episode workspace archive for issue #{issue} is unavailable"
                )
            marker = _archive_marker(
                completed.result.archive_ref, handoff.revision
            )
            _archive_original_head(
                self._git,
                cwd,
                completed.result.archive_ref,
                marker,
                handoff.branch,
            )

    @contextmanager
    def verified_episode_entry(
        self,
        completed: EpisodeWorkspacePrepared,
        *,
        approved_host: str = "github.com",
    ):
        """Hold local Git serialization from pristine verification through admission.

        The caller supplies SQLite workflow authority separately. The yielded
        path is the exact managed issue checkout verified by this lock holder.
        """

        with self._mutation_lock:
            self.verify_episode_workspace(
                completed, approved_host=approved_host,
            )
            yield self._managed_issue_checkout(
                completed.handoff.source.fence.key.target.number
            )

    def episode_workspace_path(
        self, completed: EpisodeWorkspacePrepared,
    ) -> str:
        """Return the exact managed destination path without creating or verifying it."""

        if not isinstance(completed, EpisodeWorkspacePrepared):
            raise WorktreeUnavailable(
                "episode workspace path requires its acknowledgement"
            )
        return self._managed_issue_checkout(
            completed.handoff.destination.fence.key.target.number
        )

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

    def _reset_contract(
        self,
        issue: int,
        revision: ContractRevision,
        intent_id: str,
    ) -> ContractResetResult:
        if not isinstance(revision, ContractRevision):
            raise WorktreeUnavailable("contract reset requires an exact revision")
        if issue_from_branch(revision.branch) != issue:
            raise WorktreeUnavailable(
                f"contract revision does not belong to issue #{issue}"
            )
        archive_ref = _contract_ref("contract-reset", intent_id)
        marker = _archive_marker(archive_ref, revision)
        cwd = self._managed_issue_checkout(issue)
        if not self._path_exists(cwd):
            raise WorktreeUnavailable(
                f"managed contract checkout for issue #{issue} is unavailable"
            )

        current = _validate_contract_checkout(
            cwd, issue, revision.branch, self._git, require_clean=False
        )
        baseline = _contract_git(
            self._git,
            ["rev-parse", "--verify", f"{revision.head_sha}^{{commit}}"],
            cwd,
            f"contract baseline is unavailable for issue #{issue}",
        )
        if baseline.code != 0 or baseline.out.strip() != revision.head_sha:
            raise WorktreeUnavailable(
                f"contract baseline is unavailable for issue #{issue}"
            )
        # Reset is an argument-blind boundary: ignored files can contain just
        # as much implementer work as tracked files. Capture deliberately does
        # not impose this rule because pre-contract build caches are harmless;
        # reset archives and removes every file class before adjudication.
        status = _checkout_status(
            self._git, cwd, issue, include_ignored=True
        )

        archived = _read_ref(self._git, cwd, archive_ref)
        if archived is not None:
            original_head = _archive_original_head(
                self._git, cwd, archive_ref, marker, revision.branch
            )
            if status:
                raise WorktreeUnavailable(
                    f"reset archive {archive_ref!r} is stale: checkout changed after preservation"
                )
            if current.head_sha == revision.head_sha:
                return ContractResetResult(revision, archive_ref)
            if current.head_sha != original_head:
                raise WorktreeUnavailable(
                    f"reset archive {archive_ref!r} conflicts with current branch history"
                )
        else:
            recovered = _recover_interrupted_stash(
                self._git,
                cwd,
                archive_ref,
                marker,
                revision.branch,
                current.head_sha,
                not status,
            )
            if recovered is not None:
                archived = recovered
                original_head = _archive_original_head(
                    self._git, cwd, archive_ref, marker, revision.branch
                )
            elif status:
                stashed = _contract_git(
                    self._git,
                    ["stash", "push", "--all", "--message", marker],
                    cwd,
                    f"cannot preserve displaced work for issue #{issue}",
                )
                if stashed.code != 0:
                    raise WorktreeUnavailable(
                        f"cannot preserve displaced work for issue #{issue}"
                    )
                preserved_head = _validate_contract_checkout(
                    cwd,
                    issue,
                    revision.branch,
                    self._git,
                    require_clean=True,
                )
                if preserved_head.head_sha != current.head_sha:
                    raise WorktreeUnavailable(
                        f"preserving issue #{issue} unexpectedly changed its branch"
                    )
                if _checkout_status(
                    self._git, cwd, issue, include_ignored=True
                ):
                    raise WorktreeUnavailable(
                        f"preserving issue #{issue} left displaced files in its checkout"
                    )
                stash = _contract_git(
                    self._git,
                    ["rev-parse", "--verify", "refs/stash^{commit}"],
                    cwd,
                    f"cannot verify displaced work for issue #{issue}",
                )
                if stash.code != 0:
                    raise WorktreeUnavailable(
                        f"cannot verify displaced work for issue #{issue}"
                    )
                archived = stash.out.strip()
                if _archive_original_head(
                    self._git, cwd, archived, marker, revision.branch
                ) != current.head_sha:
                    raise WorktreeUnavailable(
                        f"displaced work archive is stale for issue #{issue}"
                    )
                _create_or_verify_ref(self._git, cwd, archive_ref, archived)
                original_head = current.head_sha
            else:
                tree = _contract_git(
                    self._git,
                    ["rev-parse", "--verify", f"{current.head_sha}^{{tree}}"],
                    cwd,
                    f"cannot preserve branch history for issue #{issue}",
                )
                if tree.code != 0:
                    raise WorktreeUnavailable(
                        f"cannot preserve branch history for issue #{issue}"
                    )
                marker_commit = _contract_git(
                    self._git,
                    [
                        "commit-tree",
                        tree.out.strip(),
                        "-p",
                        current.head_sha,
                        "-m",
                        marker,
                    ],
                    cwd,
                    f"cannot preserve branch history for issue #{issue}",
                )
                if marker_commit.code != 0:
                    raise WorktreeUnavailable(
                        f"cannot preserve branch history for issue #{issue}"
                    )
                archived = marker_commit.out.strip()
                _create_or_verify_ref(self._git, cwd, archive_ref, archived)
                original_head = _archive_original_head(
                    self._git, cwd, archive_ref, marker, revision.branch
                )
                if original_head != current.head_sha:
                    raise WorktreeUnavailable(
                        f"branch archive is stale for issue #{issue}"
                    )

        reset = _contract_git(
            self._git,
            ["reset", "--hard", revision.head_sha],
            cwd,
            f"cannot restore contract baseline for issue #{issue}",
        )
        if reset.code != 0:
            raise WorktreeUnavailable(
                f"cannot restore contract baseline for issue #{issue}"
            )
        verified = _validate_contract_checkout(
            cwd, issue, revision.branch, self._git, require_clean=True
        )
        if (
            verified != revision
            or _checkout_status(
                self._git, cwd, issue, include_ignored=True
            )
        ):
            raise WorktreeUnavailable(
                f"contract reset postconditions failed for issue #{issue}"
            )
        if _read_ref(self._git, cwd, archive_ref) != archived:
            raise WorktreeUnavailable(
                f"reset archive could not be verified for issue #{issue}"
            )
        return ContractResetResult(revision, archive_ref)

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
