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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

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
from ..domain.git_remote import require_repository_remote, require_safe_fetch_url
from ..domain.refs import issue_from_branch
from ..domain.resolve_split import ContractResetResult, ContractRevision
from ..domain.resolve_workspace import (
    EpisodeWorkspaceHandoff,
    EpisodeWorkspacePrepared,
    NoPriorResolveAdmission,
    ObservedResolveBase,
)
from ..domain.workflow_identity import canonical_workflow_repository
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


def capture_contract_revision(
    cwd: str,
    issue: int,
    capture_id: str,
    expected_branch: str,
    run: GitRunner = _subprocess_git,
) -> ContractRevision:
    """Capture a clean issue checkout's exact HEAD under an immutable pin."""

    revision = _validate_contract_checkout(
        cwd, issue, expected_branch, run, require_clean=True
    )
    ref = _contract_ref("contracts", capture_id)
    _create_or_verify_ref(run, cwd, ref, revision.head_sha)
    pinned = _read_ref(run, cwd, ref)
    verified = _validate_contract_checkout(
        cwd, issue, expected_branch, run, require_clean=True
    )
    if pinned != revision.head_sha or verified != revision:
        raise WorktreeUnavailable(f"contract pin {ref!r} could not be verified")
    return revision


def read_captured_contract_revision(
    cwd: str,
    issue: int,
    capture_id: str,
    expected_branch: str,
    run: GitRunner = _subprocess_git,
) -> ContractRevision:
    """Read an existing contract pin without changing refs or checkout state."""

    _validate_contract_checkout(
        cwd, issue, expected_branch, run, require_clean=False
    )
    ref = _contract_ref("contracts", capture_id)
    pinned = _read_ref(run, cwd, ref)
    if pinned is None:
        raise WorktreeUnavailable(f"contract pin {ref!r} does not exist")
    commit = _contract_git(
        run,
        ["rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd,
        f"contract pin {ref!r} does not name a commit",
    )
    if commit.code != 0 or commit.out.strip() != pinned:
        raise WorktreeUnavailable(f"contract pin {ref!r} does not name a commit")
    try:
        return ContractRevision(expected_branch, pinned)
    except (TypeError, ValueError) as exc:
        raise WorktreeUnavailable(f"contract pin {ref!r} is invalid") from exc


def verify_contract_revision(
    cwd: str,
    issue: int,
    revision: ContractRevision,
    run: GitRunner = _subprocess_git,
) -> ContractRevision:
    """Prove a clean owned checkout is still at an exact captured revision."""

    if not isinstance(revision, ContractRevision):
        raise WorktreeUnavailable("contract verification requires an exact revision")
    observed = _validate_contract_checkout(
        cwd, issue, revision.branch, run, require_clean=True
    )
    if observed != revision:
        raise WorktreeUnavailable(
            f"contract checkout for issue #{issue} changed after capture"
        )
    return observed


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

    def reset_contract(
        self,
        issue: int,
        revision: ContractRevision,
        intent_id: str,
    ) -> ContractResetResult:
        """Restore one managed issue checkout to an exact retained contract.

        The displaced branch state remains reachable through the returned ref.
        Existing refs are replay evidence, never permission to overwrite them.
        """

        with self._mutation_lock:
            return self._reset_contract(issue, revision, intent_id)

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
    additive_only: tuple[str, ...] = ()
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
        # Modified files whose branch diff ADDS lines only (numstat deletions
        # 0). Only check_tests_immutable consults it (pure additions to an
        # existing test belong in a new file — the reviewer refuses those
        # grants on policy, so the gate now says so at commit time). Binary
        # files report "-" and are never counted; a failed numstat leaves the
        # set empty, i.e. the pre-existing (reviewer-caught) behavior.
        numstat = git(
            [
                "diff",
                "--no-renames",
                "--diff-filter=M",
                "--numstat",
                f"{baseref}...HEAD",
            ],
            cwd,
        )
        if numstat.code == 0:
            additive: list[str] = []
            for row in numstat.out.splitlines():
                parts = row.split("\t", 2)
                if len(parts) != 3:
                    continue
                added, deleted, path = parts
                if deleted == "0" and added.isdigit() and added != "0":
                    additive.append(path)
            additive_only = tuple(additive)
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
        additive_only=additive_only,
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
