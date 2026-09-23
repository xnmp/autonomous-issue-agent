"""Real-Git episode workspace observation, preservation, and replay."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path
import subprocess

import pytest

from issue_agent.adapters import worktree as worktree_module
from issue_agent.adapters.worktree import (
    GitResult,
    WorktreeUnavailable,
    Worktrees,
)
from issue_agent.domain.resolve_workspace import (
    EpisodeWorkspaceHandoff,
    EpisodeWorkspacePrepared,
    NoPriorResolveAdmission,
    PriorResolveAdmission,
)
from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from tests.adapters.test_workflow_control import NOW
from tests.fakes.workflow_authority import resolve_snapshot


ISSUE = 17
REPOSITORY = "o/r"
REMOTE_URL = "https://github.com/o/r.git"
BRANCH = "fix/17-episode-workspace"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
        )
    return result.stdout.strip()


class MappedRemoteGit:
    """Map only the already validated GitHub URL to an isolated local origin."""

    def __init__(self, origin: Path, *, fail_fetch: bool = False):
        self.origin = str(origin)
        self.fail_fetch = fail_fetch
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, args: list[str], cwd: str) -> GitResult:
        self.calls.append((tuple(args), cwd))
        if args[:1] == ["fetch"] and self.fail_fetch:
            return GitResult(1, "")
        mapped = [self.origin if value == REMOTE_URL else value for value in args]
        return worktree_module._subprocess_git(mapped, cwd)


class FailOnceGit(MappedRemoteGit):
    def __init__(self, origin: Path, boundary: str):
        super().__init__(origin)
        self.boundary = boundary
        self.failed = False
        self.reset_completed = False

    def __call__(self, args: list[str], cwd: str) -> GitResult:
        is_pin = any(
            value.startswith("refs/issue-agent/episode-bases/")
            for value in args
        )
        if (
            not self.failed
            and self.boundary == "before-pin"
            and args[:1] == ["update-ref"]
            and is_pin
        ):
            self.failed = True
            return GitResult(1, "")
        if (
            not self.failed
            and self.boundary == "after-pin"
            and args[:2] == ["rev-parse", "--verify"]
            and is_pin
            and any(value.endswith("^{commit}") for value in args)
        ):
            self.failed = True
            return GitResult(1, "")
        if (
            not self.failed
            and self.boundary == "after-reset"
            and self.reset_completed
            and args[:3] == ["rev-parse", "--verify", "--quiet"]
            and is_pin
        ):
            self.failed = True
            return GitResult(2, "")
        result = super().__call__(args, cwd)
        if args[:2] == ["reset", "--hard"] and result.code == 0:
            self.reset_completed = True
        return result


@dataclasses.dataclass(frozen=True)
class EpisodeRepo:
    root: Path
    upstream: Path
    origin: Path
    runner: MappedRemoteGit
    worktrees: Worktrees


@pytest.fixture
def episode_repo(tmp_path: Path) -> EpisodeRepo:
    origin = tmp_path / "origin.git"
    root = tmp_path / "repo"
    upstream = tmp_path / "upstream"
    _git(tmp_path, "init", "--bare", str(origin))
    root.mkdir()
    _git(root, "init", "-b", "dev")
    _git(root, "config", "user.email", "workspace-test@example.invalid")
    _git(root, "config", "user.name", "Workspace Test")
    (root / "baseline.txt").write_text("base one\n")
    (root / ".gitignore").write_text("ignored-work/\n")
    _git(root, "add", "baseline.txt", ".gitignore")
    _git(root, "commit", "-m", "base one")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-u", "origin", "dev")
    _git(root, "remote", "set-url", "origin", REMOTE_URL)

    _git(tmp_path, "clone", str(origin), str(upstream))
    _git(upstream, "checkout", "dev")
    _git(upstream, "config", "user.email", "upstream@example.invalid")
    _git(upstream, "config", "user.name", "Upstream Test")
    runner = MappedRemoteGit(origin)
    return EpisodeRepo(
        root,
        upstream,
        origin,
        runner,
        Worktrees(str(root), run=runner),
    )


def _handoff(
    base, *, branch: str = BRANCH, prior: bool = False
) -> EpisodeWorkspaceHandoff:
    source = resolve_snapshot(ISSUE).binding
    destination = dataclasses.replace(
        source,
        fence=dataclasses.replace(
            source.fence, generation=source.fence.generation + 1
        ),
        episode_id="episode-workspace-destination",
    )
    predecessor = (
        PriorResolveAdmission(
            WorkflowAttemptIdentity(
                source, RunId("prior-run"), "prior-leg"
            ),
            1,
        )
        if prior
        else NoPriorResolveAdmission(source)
    )
    return EpisodeWorkspaceHandoff(
        "workspace-grant",
        source,
        destination,
        predecessor,
        branch,
        base,
    )


def _observe(repo: EpisodeRepo, *, offset: int = 0):
    return repo.worktrees.observe_and_pin_episode_base(
        REPOSITORY, observed_at=NOW + timedelta(seconds=offset)
    )


def _advance_upstream(repo: EpisodeRepo) -> str:
    (repo.upstream / "baseline.txt").write_text("base two\n")
    _git(repo.upstream, "add", "baseline.txt")
    _git(repo.upstream, "commit", "-m", "base two")
    _git(repo.upstream, "push", str(repo.origin), "dev")
    return _git(repo.upstream, "rev-parse", "HEAD")


def test_observation_pins_exact_commit_across_later_upstream_movement(
    episode_repo: EpisodeRepo,
) -> None:
    first = _observe(episode_repo)
    moved = _advance_upstream(episode_repo)

    assert _git(episode_repo.root, "rev-parse", first.pin_ref) == first.commit_sha
    second = _observe(episode_repo, offset=1)

    assert second.commit_sha == moved
    assert second.commit_sha != first.commit_sha
    assert _git(episode_repo.root, "rev-parse", first.pin_ref) == first.commit_sha
    scratch = _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/issue-agent/episode-observations",
    ).splitlines()
    assert len(scratch) == 1
    fetches = [args for args, _cwd in episode_repo.runner.calls if args[:1] == ("fetch",)]
    assert fetches
    assert all(REMOTE_URL in args and "origin" not in args for args in fetches)
    assert all(
        "--no-recurse-submodules" in args and "--no-write-fetch-head" in args
        for args in fetches
    )


@pytest.mark.parametrize("boundary", ["before-pin", "after-pin"])
def test_observation_retries_after_crash_around_immutable_pin(
    episode_repo: EpisodeRepo,
    boundary: str,
) -> None:
    failing_runner = FailOnceGit(episode_repo.origin, boundary)
    failing = Worktrees(str(episode_repo.root), run=failing_runner)
    with pytest.raises(WorktreeUnavailable):
        failing.observe_and_pin_episode_base(REPOSITORY, observed_at=NOW)
    assert failing_runner.failed

    pin_refs = _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/issue-agent/episode-bases",
    ).splitlines()
    assert len(pin_refs) == (0 if boundary == "before-pin" else 1)
    recovered = _observe(episode_repo)
    assert _git(episode_repo.root, "rev-parse", recovered.pin_ref) == recovered.commit_sha
    scratch = _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/issue-agent/episode-observations",
    ).splitlines()
    assert len(scratch) == 1


@pytest.mark.parametrize("base_branch", ["bad..ref", "bad ref", "bad@{ref"])
def test_malformed_configured_base_ref_is_rejected_before_fetch(
    episode_repo: EpisodeRepo,
    base_branch: str,
) -> None:
    runner = MappedRemoteGit(episode_repo.origin)
    worktrees = Worktrees(
        str(episode_repo.root), base_branch=base_branch, run=runner
    )

    with pytest.raises(WorktreeUnavailable, match="invalid"):
        worktrees.observe_and_pin_episode_base(REPOSITORY, observed_at=NOW)

    assert not any(args[:1] == ("fetch",) for args, _cwd in runner.calls)


def test_fetch_failure_and_invalid_or_missing_upstream_never_use_local_fallback(
    episode_repo: EpisodeRepo,
) -> None:
    _observe(episode_repo)
    failing = Worktrees(
        str(episode_repo.root),
        run=MappedRemoteGit(episode_repo.origin, fail_fetch=True),
    )
    with pytest.raises(WorktreeUnavailable, match="cannot fetch"):
        failing.observe_and_pin_episode_base(
            REPOSITORY, observed_at=NOW + timedelta(seconds=1)
        )

    missing = Worktrees(
        str(episode_repo.root),
        base_branch="missing-upstream",
        run=episode_repo.runner,
    )
    with pytest.raises(WorktreeUnavailable, match="cannot fetch"):
        missing.observe_and_pin_episode_base(
            REPOSITORY, observed_at=NOW + timedelta(seconds=2)
        )

    _git(
        episode_repo.root,
        "remote",
        "set-url",
        "origin",
        "https://example.invalid/o/r.git",
    )
    with pytest.raises(WorktreeUnavailable, match="matching workspace"):
        episode_repo.worktrees.observe_and_pin_episode_base(
            REPOSITORY, observed_at=NOW + timedelta(seconds=3)
        )

    _git(
        episode_repo.root,
        "remote",
        "set-url",
        "origin",
        "https://token@github.com/o/r.git",
    )
    with pytest.raises(WorktreeUnavailable, match="credential helper"):
        episode_repo.worktrees.observe_and_pin_episode_base(
            REPOSITORY, observed_at=NOW + timedelta(seconds=4)
        )


def test_new_workspace_uses_pin_and_acknowledgement_replays_read_only(
    episode_repo: EpisodeRepo,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base)
    _advance_upstream(episode_repo)

    result = episode_repo.worktrees.prepare_episode_workspace(handoff)
    checkout = episode_repo.root / episode_repo.worktrees.path(ISSUE)
    assert _git(checkout, "rev-parse", "HEAD") == base.commit_sha
    assert _git(checkout, "branch", "--show-current") == BRANCH
    assert result.revision == handoff.revision
    assert result.archive_ref == handoff.archive_ref

    completed = EpisodeWorkspacePrepared(
        handoff,
        handoff.destination,
        result,
        NOW + timedelta(seconds=5),
    )
    episode_repo.worktrees.verify_episode_workspace(completed)
    assert episode_repo.worktrees.prepare_episode_workspace(handoff) == result
    episode_repo.worktrees.verify_episode_workspace(completed)


def test_explicit_external_worktree_root_remains_supported(
    episode_repo: EpisodeRepo,
) -> None:
    external_root = episode_repo.root.parent / "authorized-worktrees"
    worktrees = Worktrees(
        str(episode_repo.root),
        worktree_root=str(external_root),
        run=episode_repo.runner,
    )
    base = worktrees.observe_and_pin_episode_base(
        REPOSITORY, observed_at=NOW
    )
    handoff = _handoff(base)

    result = worktrees.prepare_episode_workspace(handoff)
    checkout = external_root / f"issue-{ISSUE}"
    assert _git(checkout, "rev-parse", "HEAD") == base.commit_sha
    completed = EpisodeWorkspacePrepared(
        handoff,
        handoff.destination,
        result,
        NOW + timedelta(seconds=1),
    )
    worktrees.verify_episode_workspace(completed)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_pin_refuses_prepare_without_workspace_mutation(
    episode_repo: EpisodeRepo,
    damage: str,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base)
    if damage == "missing":
        _git(episode_repo.root, "update-ref", "-d", base.pin_ref)
    else:
        (episode_repo.root / "alternate.txt").write_text("alternate\n")
        _git(episode_repo.root, "add", "alternate.txt")
        _git(episode_repo.root, "commit", "-m", "alternate local commit")
        alternate = _git(episode_repo.root, "rev-parse", "HEAD")
        _git(episode_repo.root, "update-ref", base.pin_ref, alternate)

    with pytest.raises(WorktreeUnavailable, match="pin"):
        episode_repo.worktrees.prepare_episode_workspace(handoff)

    checkout = episode_repo.root / episode_repo.worktrees.path(ISSUE)
    assert not checkout.exists()
    assert _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/heads/{BRANCH}",
    ) == ""
    assert _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        handoff.archive_ref,
    ) == ""


def test_prepare_archives_every_file_class_and_replays_after_lost_ack(
    episode_repo: EpisodeRepo,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base, prior=True)
    checkout = episode_repo.root / episode_repo.worktrees.path(ISSUE)
    _git(
        episode_repo.root,
        "worktree",
        "add",
        "-b",
        BRANCH,
        str(checkout),
        base.pin_ref,
    )
    (checkout / "implementation.py").write_text("committed\n")
    _git(checkout, "add", "implementation.py")
    _git(checkout, "commit", "-m", "prior implementation")
    implementation_head = _git(checkout, "rev-parse", "HEAD")
    (checkout / "implementation.py").write_text("tracked dirty\n")
    (checkout / "untracked.txt").write_text("untracked\n")
    ignored = checkout / "ignored-work"
    ignored.mkdir()
    (ignored / "secret.py").write_text("ignored\n")

    result = episode_repo.worktrees.prepare_episode_workspace(handoff)
    assert _git(checkout, "rev-parse", "HEAD") == base.commit_sha
    assert not (checkout / "implementation.py").exists()
    assert not (checkout / "untracked.txt").exists()
    assert not ignored.exists()
    assert episode_repo.worktrees.prepare_episode_workspace(handoff) == result

    completed = EpisodeWorkspacePrepared(
        handoff,
        handoff.destination,
        result,
        NOW + timedelta(seconds=1),
    )
    episode_repo.worktrees.verify_episode_workspace(completed)
    assert _git(checkout, "rev-parse", f"{result.archive_ref}^1") == implementation_head
    _git(checkout, "reset", "--hard", f"{result.archive_ref}^1")
    _git(checkout, "stash", "apply", "--index", result.archive_ref)
    assert (checkout / "implementation.py").read_text() == "tracked dirty\n"
    assert (checkout / "untracked.txt").read_text() == "untracked\n"
    assert (ignored / "secret.py").read_text() == "ignored\n"


def test_prepare_retries_after_crash_between_reset_and_returned_result(
    episode_repo: EpisodeRepo,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base, prior=True)
    checkout = episode_repo.root / episode_repo.worktrees.path(ISSUE)
    _git(
        episode_repo.root,
        "worktree",
        "add",
        "-b",
        BRANCH,
        str(checkout),
        base.pin_ref,
    )
    (checkout / "prior.py").write_text("prior\n")
    _git(checkout, "add", "prior.py")
    _git(checkout, "commit", "-m", "prior implementation")

    failing_runner = FailOnceGit(episode_repo.origin, "after-reset")
    failing = Worktrees(str(episode_repo.root), run=failing_runner)
    with pytest.raises(WorktreeUnavailable, match="pin|retained Git reference"):
        failing.prepare_episode_workspace(handoff)
    assert failing_runner.failed
    assert _git(checkout, "rev-parse", "HEAD") == base.commit_sha
    archived_before = _git(
        episode_repo.root, "rev-parse", handoff.archive_ref
    )

    recovered = episode_repo.worktrees.prepare_episode_workspace(handoff)
    assert recovered.archive_ref == handoff.archive_ref
    assert _git(episode_repo.root, "rev-parse", handoff.archive_ref) == archived_before
    assert _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        handoff.archive_ref,
    ).splitlines() == [handoff.archive_ref]


def test_absent_checkout_restores_only_the_exact_retained_branch(
    episode_repo: EpisodeRepo,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base, prior=True)
    temporary = episode_repo.root / "temporary-worktree"
    _git(
        episode_repo.root,
        "worktree",
        "add",
        "-b",
        BRANCH,
        str(temporary),
        base.pin_ref,
    )
    (temporary / "prior.py").write_text("prior committed work\n")
    _git(temporary, "add", "prior.py")
    _git(temporary, "commit", "-m", "retained branch work")
    prior_head = _git(temporary, "rev-parse", "HEAD")
    _git(episode_repo.root, "worktree", "remove", str(temporary))

    result = episode_repo.worktrees.prepare_episode_workspace(handoff)
    checkout = episode_repo.root / episode_repo.worktrees.path(ISSUE)
    assert _git(checkout, "branch", "--show-current") == BRANCH
    assert _git(checkout, "rev-parse", "HEAD") == base.commit_sha
    assert _git(checkout, "rev-parse", f"{result.archive_ref}^1") == prior_head


def test_prior_admission_requires_present_exact_branch_before_creation(
    episode_repo: EpisodeRepo,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base, prior=True)
    checkout = episode_repo.root / episode_repo.worktrees.path(ISSUE)

    with pytest.raises(WorktreeUnavailable, match="prior admitted workspace"):
        episode_repo.worktrees.prepare_episode_workspace(handoff)
    assert not checkout.exists()
    assert _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/heads/{BRANCH}",
    ) == ""
    assert _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        handoff.archive_ref,
    ) == ""

    _git(episode_repo.root, "branch", BRANCH, base.pin_ref)
    result = episode_repo.worktrees.prepare_episode_workspace(handoff)
    assert result.revision == handoff.revision
    assert _git(checkout, "branch", "--show-current") == BRANCH


def test_prepare_rejects_ambiguous_branch_foreign_path_and_wrong_checkout_branch(
    episode_repo: EpisodeRepo,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base)
    _git(episode_repo.root, "branch", "fix/17-one", base.pin_ref)
    _git(episode_repo.root, "branch", "feat/17-two", base.pin_ref)
    with pytest.raises(WorktreeUnavailable, match="ambiguous"):
        episode_repo.worktrees.prepare_episode_workspace(handoff)

    _git(episode_repo.root, "branch", "-D", "fix/17-one")
    _git(episode_repo.root, "branch", "-D", "feat/17-two")
    checkout = episode_repo.root / episode_repo.worktrees.path(ISSUE)
    _git(episode_repo.root, "branch", "fix/18-foreign", base.pin_ref)
    _git(
        episode_repo.root,
        "worktree",
        "add",
        str(checkout),
        "fix/18-foreign",
    )
    with pytest.raises(WorktreeUnavailable, match="unexpected branch"):
        episode_repo.worktrees.prepare_episode_workspace(handoff)
    _git(episode_repo.root, "worktree", "remove", str(checkout))

    outside = episode_repo.root.parent / "foreign-repository"
    outside.mkdir()
    checkout.parent.mkdir(parents=True, exist_ok=True)
    checkout.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorktreeUnavailable, match="outside its managed root"):
        episode_repo.worktrees.prepare_episode_workspace(handoff)
    checkout.unlink()

    checkout.symlink_to(checkout.parent, target_is_directory=True)
    with pytest.raises(WorktreeUnavailable, match="outside its managed root"):
        episode_repo.worktrees.prepare_episode_workspace(handoff)
    checkout.unlink()
    assert _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        handoff.archive_ref,
    ) == ""

    drifted = Worktrees(
        str(episode_repo.root), base_branch="main", run=episode_repo.runner
    )
    with pytest.raises(WorktreeUnavailable, match="configured base branch"):
        drifted.prepare_episode_workspace(handoff)


@pytest.mark.parametrize(
    "aliased_relative", [Path("nested") / "worktree", Path("issue-18")]
)
def test_prepare_rejects_nested_or_sibling_issue_checkout_alias_without_mutation(
    episode_repo: EpisodeRepo,
    aliased_relative: Path,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base, prior=True)
    managed = episode_repo.root / ".claude" / "worktrees"
    actual = managed / aliased_relative
    actual.parent.mkdir(parents=True, exist_ok=True)
    _git(
        episode_repo.root,
        "worktree",
        "add",
        "-b",
        BRANCH,
        str(actual),
        base.pin_ref,
    )
    (actual / "prior.py").write_text("must remain\n")
    _git(actual, "add", "prior.py")
    _git(actual, "commit", "-m", "aliased prior implementation")
    prior_head = _git(actual, "rev-parse", "HEAD")
    lexical = managed / f"issue-{ISSUE}"
    lexical.symlink_to(actual, target_is_directory=True)

    with pytest.raises(WorktreeUnavailable, match="outside its managed root"):
        episode_repo.worktrees.prepare_episode_workspace(handoff)

    assert _git(actual, "rev-parse", "HEAD") == prior_head
    assert (actual / "prior.py").read_text() == "must remain\n"
    assert _git(
        episode_repo.root,
        "for-each-ref",
        "--format=%(refname)",
        handoff.archive_ref,
    ) == ""


def test_verify_rejects_ignored_residue_and_wrong_repository_identity(
    episode_repo: EpisodeRepo,
) -> None:
    base = _observe(episode_repo)
    handoff = _handoff(base)
    result = episode_repo.worktrees.prepare_episode_workspace(handoff)
    completed = EpisodeWorkspacePrepared(
        handoff,
        handoff.destination,
        result,
        NOW + timedelta(seconds=1),
    )
    checkout = episode_repo.root / episode_repo.worktrees.path(ISSUE)
    ignored = checkout / "ignored-work"
    ignored.mkdir()
    (ignored / "residue.py").write_text("must not reach AUTHOR\n")
    with pytest.raises(WorktreeUnavailable, match="exact base"):
        episode_repo.worktrees.verify_episode_workspace(completed)

    (ignored / "residue.py").unlink()
    ignored.rmdir()
    _git(
        episode_repo.root,
        "remote",
        "set-url",
        "origin",
        "https://github.com/other/repository.git",
    )
    with pytest.raises(WorktreeUnavailable, match="matching workspace"):
        episode_repo.worktrees.verify_episode_workspace(completed)
