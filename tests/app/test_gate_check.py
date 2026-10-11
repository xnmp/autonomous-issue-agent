"""Actual trusted shell transports with independently expected outcomes."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_gate_parity import CHECKS, ROOT, _git, compliant_pr_repo, fresh_repo, python_verdict


def invoke(repo, operation, *args, env=None, checks=CHECKS):
    values = dict(os.environ, BASE_BRANCH="dev", VERIFY_RED_AT_COMMIT="0", GATE_GH_RETRY_SLEEP="0")
    values.update(env or {})
    return subprocess.run(["bash", str(checks / f"check-{operation}.sh"), *args], cwd=repo,
        env=values, capture_output=True, text=True, timeout=30)


def test_missing_harness_policy_fails_closed(tmp_path):
    checks = tmp_path / "checks"
    shutil.copytree(CHECKS, checks)
    result = invoke(fresh_repo(tmp_path / "target"), "commit", "commit", "-m", "docs: safe", checks=checks)
    assert result.returncode == 1
    assert "policy is unavailable" in result.stderr


def test_target_pythonpath_cannot_replace_harness_policy(tmp_path):
    repo = fresh_repo(tmp_path / "target")
    hostile = tmp_path / "hostile" / "issue_agent"
    hostile.mkdir(parents=True)
    (hostile / "__init__.py").write_text("raise RuntimeError('target package imported')")
    result = invoke(repo, "commit", "commit", "-m", "wip: invalid", env={"PYTHONPATH": str(hostile.parent)})
    assert result.returncode == 1
    assert "allowed prefix" in result.stdout
    assert "target package imported" not in result.stderr


@pytest.mark.parametrize("name,value", [("RED_MUST_FAIL", "false"), ("ALLOW_UNTESTED", "yes"), ("EVIDENCE_MAX_IMAGE_KB", "-1"), ("GATE_GH_RETRY_SLEEP", "nan")])
def test_malformed_gate_configuration_denies_before_target_execution(tmp_path, name, value):
    repo = fresh_repo(tmp_path)
    marker = repo / "lint-executed"
    result = invoke(repo, "commit", "commit", "-m", "docs: safe", env={name: value, "LINT_CMD": "touch lint-executed"})
    assert result.returncode == 1
    assert "malformed" in result.stderr
    assert not marker.exists()


def test_bad_commit_prefix_never_runs_configured_lint_in_either_transport(tmp_path):
    repo = fresh_repo(tmp_path)
    values = {"LINT_CMD": "touch lint-executed", "BASE_BRANCH": "dev"}
    result = invoke(repo, "commit", "commit", "-m", "wip: invalid", env=values)
    sdk = python_verdict("commit", repo, ["commit", "-m", "wip: invalid"], values)
    assert result.returncode == 1 and not sdk.allowed
    assert not (repo / "lint-executed").exists()


@pytest.mark.parametrize("generated", [False, True])
def test_staged_target_instructions_preserve_ownership_boundary(tmp_path, generated):
    repo = fresh_repo(tmp_path)
    text = "<!-- issue-agent generated instructions: start -->" if generated else "Target maintainer's instructions"
    (repo / "AGENTS.md").write_text(text)
    _git(repo, "add", "AGENTS.md")
    shell = invoke(repo, "commit", "commit", "-m", "docs: instructions")
    sdk = python_verdict("commit", repo, ["commit", "-m", "docs: instructions"], {})
    assert (shell.returncode == 0) is (not generated)
    assert sdk.allowed is (not generated)


def test_committed_generated_instructions_deny_pr_before_suite(tmp_path):
    repo = compliant_pr_repo(tmp_path)
    (repo / "AGENTS.override.md").write_text("<!-- issue-agent generated instructions: start -->")
    _git(repo, "add", "AGENTS.override.md")
    _git(repo, "commit", "-qm", "docs: instructions")
    body = "## Acceptance Criteria\n- token works\n\nFixes #7"
    values = {"TEST_CMD": "touch suite-executed"}
    shell = invoke(repo, "pr", "pr", "create", "--base", "dev", "--body", body, env=values)
    sdk = python_verdict("pr", repo, ["pr", "create", "--base", "dev", "--body", body], values)
    assert shell.returncode == 1 and not sdk.allowed
    assert "generated instructions" in shell.stdout and "generated instructions" in sdk.output
    assert not (repo / "suite-executed").exists()


@pytest.mark.parametrize("staged", [False, True])
def test_oversized_new_image_denies_commit_before_lint(tmp_path, staged):
    repo = fresh_repo(tmp_path)
    (repo / "evidence").mkdir()
    (repo / "evidence" / "new.png").write_bytes(b"x" * 1025)
    if staged:
        _git(repo, "add", "evidence/new.png")
    values = {"EVIDENCE_MAX_IMAGE_KB": "1", "LINT_CMD": "touch lint-executed"}
    shell = invoke(repo, "commit", "commit", "-m", "docs: capture", env=values)
    sdk = python_verdict("commit", repo, ["commit", "-m", "docs: capture"], values)
    assert shell.returncode == 1 and not sdk.allowed
    assert "size limit" in shell.stdout and "size limit" in sdk.output
    assert not (repo / "lint-executed").exists()


def test_missing_own_package_cannot_fall_back_to_installed_editable_code(tmp_path):
    harness = tmp_path / "incomplete-harness"
    entry = harness / "src" / "issue_agent" / "app" / "gate_check.py"
    entry.parent.mkdir(parents=True)
    shutil.copy(ROOT / "src" / "issue_agent" / "app" / "gate_check.py", entry)
    checks = harness / "plugin" / "hooks" / "checks"
    shutil.copytree(CHECKS, checks)
    (harness / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
    result = invoke(fresh_repo(tmp_path / "target"), "commit", "commit", "-m", "docs: valid", checks=checks)
    assert result.returncode != 0
    assert "package is unavailable" in result.stderr


@pytest.mark.parametrize("failure", ["diff", "log"])
def test_required_git_read_failure_denies_real_pr_transport(tmp_path, failure):
    repo = compliant_pr_repo(tmp_path / "target")
    shim = tmp_path / "shim"
    shim.mkdir()
    real_git = shutil.which("git")
    (shim / "git").write_text(f'#!/bin/sh\nif [ "$1" = "{failure}" ]; then exit 128; fi\nexec "{real_git}" "$@"\n')
    (shim / "git").chmod(0o755)
    args = ["pr", "create", "--base", "dev", "--body", "## Acceptance Criteria\n- token works\n\nFixes #7"]
    result = invoke(repo, "pr", *args, env={"PATH": f"{shim}:{os.environ['PATH']}", "TEST_CMD": "touch suite-executed"})
    assert result.returncode == 1
    assert "Git history/diff facts are unavailable" in result.stdout
    assert not (repo / "suite-executed").exists()


def test_committed_deleted_target_instructions_are_proven_absent(tmp_path):
    repo = fresh_repo(tmp_path)
    (repo / "AGENTS.md").write_text("target-owned instructions")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-qm", "docs: instructions")
    _git(repo, "switch", "-qc", "chore/7-clean")
    _git(repo, "rm", "AGENTS.md")
    _git(repo, "commit", "-qm", "chore: remove obsolete instructions")
    result = invoke(repo, "pr", "pr", "create", "--base", "dev", "--body", "## Acceptance Criteria\n- obsolete instructions removed\n\nFixes #7", env={"TEST_CMD": "true"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_restore_failure_blocks_red_authorization_in_real_shell_transport(tmp_path):
    repo = compliant_pr_repo(tmp_path / "target")
    shim = tmp_path / "shim"
    shim.mkdir()
    real_git = shutil.which("git")
    (shim / "git").write_text(f'#!/bin/sh\nif [ "$1" = checkout ] && [ "$3" = HEAD ]; then exit 128; fi\nexec "{real_git}" "$@"\n')
    (shim / "git").chmod(0o755)
    result = invoke(repo, "pr", "pr", "create", "--base", "dev", "--body", "## Acceptance Criteria\n- token works\n\nFixes #7", env={"PATH": f"{shim}:{os.environ['PATH']}"})
    assert result.returncode == 1
    assert "infrastructure" in result.stdout
