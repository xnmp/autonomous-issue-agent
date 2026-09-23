"""Real-Git contracts for exact split-contract capture and recovery."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_agent.adapters import worktree as worktree_module
from issue_agent.adapters.worktree import (
    GitResult,
    WorktreeUnavailable,
    Worktrees,
    capture_contract_revision,
    read_captured_contract_revision,
    verify_contract_revision,
)
from issue_agent.domain.resolve_split import ContractRevision


ISSUE = 17
BRANCH = "fix/17-contract"


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
    return result.stdout


@dataclass(frozen=True)
class ContractRepo:
    root: Path
    checkout: Path
    baseline: ContractRevision


@pytest.fixture
def contract_repo(tmp_path: Path) -> ContractRepo:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    _git(root, "config", "user.email", "contract-test@example.invalid")
    _git(root, "config", "user.name", "Contract Test")
    (root / "contract.txt").write_text("accepted contract\n")
    (root / ".gitignore").write_text(
        "implementer-secret.txt\nignored-nested/\n"
    )
    _git(root, "add", "contract.txt", ".gitignore")
    _git(root, "commit", "-q", "-m", "accepted contract")

    checkout = root / ".claude" / "worktrees" / f"issue-{ISSUE}"
    _git(root, "worktree", "add", "-q", "-b", BRANCH, str(checkout), "HEAD")
    baseline = capture_contract_revision(
        str(checkout), ISSUE, "capture-one", BRANCH
    )
    return ContractRepo(root, checkout, baseline)


def test_capture_is_exact_immutable_and_read_does_not_require_current_head(
    contract_repo: ContractRepo,
) -> None:
    checkout = contract_repo.checkout
    pinned = contract_repo.baseline
    assert capture_contract_revision(
        str(checkout), ISSUE, "capture-one", BRANCH
    ) == pinned

    (checkout / "implementation.py").write_text("value = 1\n")
    _git(checkout, "add", "implementation.py")
    _git(checkout, "commit", "-q", "-m", "implementation")

    assert read_captured_contract_revision(
        str(checkout), ISSUE, "capture-one", BRANCH
    ) == pinned
    with pytest.raises(WorktreeUnavailable, match="changed after capture"):
        verify_contract_revision(str(checkout), ISSUE, pinned)
    with pytest.raises(WorktreeUnavailable, match="conflicts"):
        capture_contract_revision(
            str(checkout), ISSUE, "capture-one", BRANCH
        )


def test_capture_rejects_dirty_wrong_path_branch_and_missing_pin(
    contract_repo: ContractRepo,
) -> None:
    checkout = contract_repo.checkout
    nested = checkout / "nested"
    nested.mkdir()

    with pytest.raises(WorktreeUnavailable, match="toplevel"):
        capture_contract_revision(str(nested), ISSUE, "nested", BRANCH)
    with pytest.raises(WorktreeUnavailable, match="not registered"):
        capture_contract_revision(
            str(checkout), ISSUE, "wrong-branch", "feat/17-other"
        )
    with pytest.raises(WorktreeUnavailable, match="does not exist"):
        read_captured_contract_revision(
            str(checkout), ISSUE, "missing", BRANCH
        )

    # Capture concerns the authored contract. Existing ignored build products
    # are permitted here; reset later treats every ignored file as displaced
    # implementer work and archives it before adjudication.
    (checkout / "implementer-secret.txt").write_text("existing cache\n")
    assert capture_contract_revision(
        str(checkout), ISSUE, "ignored-cache", BRANCH
    ) == contract_repo.baseline
    (checkout / "implementer-secret.txt").unlink()

    (checkout / "untracked.txt").write_text("not admitted\n")
    with pytest.raises(WorktreeUnavailable, match="not clean"):
        capture_contract_revision(str(checkout), ISSUE, "dirty", BRANCH)


def test_reset_archives_committed_dirty_and_untracked_work_then_replays(
    contract_repo: ContractRepo,
) -> None:
    checkout = contract_repo.checkout
    (checkout / "implementation.py").write_text("committed implementation\n")
    _git(checkout, "add", "implementation.py")
    _git(checkout, "commit", "-q", "-m", "implementation")
    implementation_head = _git(checkout, "rev-parse", "HEAD").strip()
    (checkout / "implementation.py").write_text("dirty implementation\n")
    (checkout / "untracked.txt").write_text("recover me\n")
    (checkout / "implementer-secret.txt").write_text("ignored secret\n")
    ignored_nested = checkout / "ignored-nested"
    ignored_nested.mkdir()
    (ignored_nested / "generated.py").write_text("ignored nested work\n")

    worktrees = Worktrees(str(contract_repo.root))
    result = worktrees.reset_contract(
        ISSUE, contract_repo.baseline, "reset-intent-one"
    )

    assert result.revision == contract_repo.baseline
    assert result.archive_ref.startswith("refs/issue-agent/contract-reset/")
    assert _git(checkout, "rev-parse", "HEAD").strip() == contract_repo.baseline.head_sha
    assert _git(checkout, "branch", "--show-current").strip() == BRANCH
    assert _git(checkout, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert not (checkout / "implementer-secret.txt").exists()
    assert not ignored_nested.exists()
    assert worktrees.reset_contract(
        ISSUE, contract_repo.baseline, "reset-intent-one"
    ) == result

    # The archive retains the displaced commit as its first parent and both
    # working-tree layers are recoverable using ordinary stash semantics.
    assert _git(checkout, "rev-parse", f"{result.archive_ref}^1").strip() == implementation_head
    _git(checkout, "reset", "--hard", implementation_head)
    _git(checkout, "stash", "apply", "--index", result.archive_ref)
    assert (checkout / "implementation.py").read_text() == "dirty implementation\n"
    assert (checkout / "untracked.txt").read_text() == "recover me\n"
    assert (checkout / "implementer-secret.txt").read_text() == "ignored secret\n"
    assert (ignored_nested / "generated.py").read_text() == "ignored nested work\n"


def test_reset_archives_an_ignored_only_file_before_exact_baseline_checkout(
    contract_repo: ContractRepo,
) -> None:
    checkout = contract_repo.checkout
    secret = checkout / "implementer-secret.txt"
    secret.write_text("implementation hidden by baseline ignore rules\n")
    assert _git(
        checkout, "status", "--porcelain=v1", "--untracked-files=all"
    ) == ""

    result = Worktrees(str(contract_repo.root)).reset_contract(
        ISSUE, contract_repo.baseline, "ignored-only-reset"
    )

    assert not secret.exists()
    assert _git(
        checkout,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    ) == ""
    _git(checkout, "stash", "apply", result.archive_ref)
    assert secret.read_text() == "implementation hidden by baseline ignore rules\n"


def test_reset_rejects_missing_baseline_foreign_branch_and_conflicting_archive(
    contract_repo: ContractRepo,
) -> None:
    worktrees = Worktrees(str(contract_repo.root))
    missing = ContractRevision(BRANCH, "0" * 40)
    with pytest.raises(WorktreeUnavailable, match="baseline is unavailable"):
        worktrees.reset_contract(ISSUE, missing, "missing-baseline")
    foreign = ContractRevision("fix/18-foreign", contract_repo.baseline.head_sha)
    with pytest.raises(WorktreeUnavailable, match="does not belong"):
        worktrees.reset_contract(ISSUE, foreign, "foreign-branch")

    (contract_repo.checkout / "implementation.py").write_text("must remain\n")
    _git(contract_repo.checkout, "add", "implementation.py")
    _git(contract_repo.checkout, "commit", "-q", "-m", "implementation")
    current = _git(contract_repo.checkout, "rev-parse", "HEAD").strip()
    intent = "conflicting-archive"
    archive_ref = (
        "refs/issue-agent/contract-reset/"
        + hashlib.sha256(intent.encode()).hexdigest()
    )
    _git(contract_repo.root, "update-ref", archive_ref, contract_repo.baseline.head_sha)

    with pytest.raises(WorktreeUnavailable, match="conflicts"):
        worktrees.reset_contract(ISSUE, contract_repo.baseline, intent)
    assert _git(contract_repo.checkout, "rev-parse", "HEAD").strip() == current


def test_reset_recovers_interruption_after_stash_before_archive_pin(
    contract_repo: ContractRepo,
) -> None:
    checkout = contract_repo.checkout
    (checkout / "implementation.py").write_text("committed\n")
    _git(checkout, "add", "implementation.py")
    _git(checkout, "commit", "-q", "-m", "implementation")
    implementation_head = _git(checkout, "rev-parse", "HEAD").strip()
    (checkout / "implementation.py").write_text("dirty\n")
    (checkout / "untracked.txt").write_text("also dirty\n")
    ignored_nested = checkout / "ignored-nested"
    ignored_nested.mkdir()
    (ignored_nested / "generated.py").write_text("ignored interrupted work\n")

    failed_once = False

    def fail_archive_create_once(args: list[str], cwd: str) -> GitResult:
        nonlocal failed_once
        if (
            not failed_once
            and args[:1] == ["update-ref"]
            and args[1].startswith("refs/issue-agent/contract-reset/")
        ):
            failed_once = True
            return GitResult(1, "")
        return worktree_module._subprocess_git(args, cwd)

    with pytest.raises(WorktreeUnavailable, match="cannot create"):
        Worktrees(
            str(contract_repo.root), run=fail_archive_create_once
        ).reset_contract(ISSUE, contract_repo.baseline, "interrupted-pin")
    assert _git(checkout, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert not ignored_nested.exists()

    result = Worktrees(str(contract_repo.root)).reset_contract(
        ISSUE, contract_repo.baseline, "interrupted-pin"
    )
    archived_names = set(_git(
        checkout,
        "stash",
        "show",
        "--include-untracked",
        "--name-only",
        result.archive_ref,
    ).splitlines())
    assert archived_names == {
        "ignored-nested/generated.py",
        "implementation.py",
        "untracked.txt",
    }
    assert _git(checkout, "rev-parse", "HEAD").strip() == contract_repo.baseline.head_sha
    _git(checkout, "reset", "--hard", implementation_head)
    _git(checkout, "stash", "apply", "--index", result.archive_ref)
    assert (ignored_nested / "generated.py").read_text() == "ignored interrupted work\n"


def test_reset_failure_is_raised_and_safe_replay_completes(
    contract_repo: ContractRepo,
) -> None:
    checkout = contract_repo.checkout
    (checkout / "implementation.py").write_text("committed\n")
    _git(checkout, "add", "implementation.py")
    _git(checkout, "commit", "-q", "-m", "implementation")
    failed_once = False

    def fail_reset_once(args: list[str], cwd: str) -> GitResult:
        nonlocal failed_once
        if not failed_once and args[:2] == ["reset", "--hard"]:
            failed_once = True
            return GitResult(1, "")
        return worktree_module._subprocess_git(args, cwd)

    with pytest.raises(WorktreeUnavailable, match="cannot restore"):
        Worktrees(str(contract_repo.root), run=fail_reset_once).reset_contract(
            ISSUE, contract_repo.baseline, "interrupted-reset"
        )

    result = Worktrees(str(contract_repo.root)).reset_contract(
        ISSUE, contract_repo.baseline, "interrupted-reset"
    )
    assert _git(checkout, "rev-parse", "HEAD").strip() == contract_repo.baseline.head_sha
    assert _git(contract_repo.root, "show-ref", "--verify", result.archive_ref)
