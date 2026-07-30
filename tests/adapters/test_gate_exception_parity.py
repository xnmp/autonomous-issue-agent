"""Cross-stack parity for the audited existing-test exception path."""

from pathlib import Path

import pytest

from test_gate_parity import Case, _git, _pr_argv, _run_case, fresh_repo


def _base_repo(tmp_path: Path) -> Path:
    repo = fresh_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_keep.py").write_text("def test_keep(): assert True\n")
    _git(repo, "add", "tests/test_keep.py")
    _git(repo, "commit", "-qm", "test: baseline")
    (repo / ".harness").mkdir()
    _git(repo, "switch", "-qc", "fix/13-correct-test")
    (repo / "tests" / "test_regression.py").write_text("def test_new(): assert True\n")
    _git(repo, "add", "tests/test_regression.py")
    _git(repo, "commit", "-qm", "red: regression")
    (repo / "tests" / "test_keep.py").write_text("def test_keep(): assert False\n")
    (repo / "impl.py").write_text("fixed = True\n")
    _git(repo, "add", "tests/test_keep.py", "impl.py")
    _git(repo, "commit", "-qm", "green: correct behavior and expectation")
    (repo / ".harness" / "ac-posted").touch()
    (repo / ".harness" / "test-plan-posted").touch()
    (repo / ".harness" / "adversarial-verdict").write_text("PASS\n")
    return repo


def _with_manifest(tmp_path: Path, manifest: str) -> Path:
    repo = _base_repo(tmp_path)
    (repo / ".harness" / "test-immutability-exceptions").write_text(manifest)
    _git(repo, "add", "-f", ".harness/test-immutability-exceptions")
    _git(repo, "commit", "-qm", "chore: declare corrected test")
    return repo


def _valid(tmp_path: Path) -> Path:
    return _with_manifest(
        tmp_path, "13 | tests/test_keep.py | baseline encoded the defect |\n"
    )


def _dirty_override(tmp_path: Path) -> Path:
    repo = _with_manifest(
        tmp_path, "13 | tests/test_other.py | unrelated expectation |\n"
    )
    (repo / ".harness" / "test-immutability-exceptions").write_text(
        "13 | tests/test_keep.py | hidden local authorization |\n"
    )
    return repo


def _dirty_recreated_deletion(tmp_path: Path) -> Path:
    repo = _base_repo(tmp_path)
    _git(repo, "rm", "tests/test_keep.py")
    _git(repo, "commit", "-qm", "test: remove obsolete expectation")
    manifest = repo / ".harness" / "test-immutability-exceptions"
    manifest.write_text("13 | tests/test_keep.py | deletion requires replacement |\n")
    _git(repo, "add", "-f", ".harness/test-immutability-exceptions")
    _git(repo, "commit", "-qm", "chore: declare deleted test")
    (repo / "tests" / "test_keep.py").write_text("def test_dirty(): assert True\n")
    return repo


def _inherited_manifest(tmp_path: Path) -> Path:
    repo = fresh_repo(tmp_path)
    (repo / ".harness").mkdir()
    manifest = repo / ".harness" / "test-immutability-exceptions"
    manifest.write_text(
        "13 | tests/test_old_issue.py | issue 13 corrected this expectation |\n"
    )
    _git(repo, "add", "-f", ".harness/test-immutability-exceptions")
    _git(repo, "commit", "-qm", "chore: retain prior issue audit record")
    _git(repo, "switch", "-qc", "fix/14-next-issue")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_regression.py").write_text(
        "def test_regression(): assert True\n"
    )
    _git(repo, "add", "tests/test_regression.py")
    _git(repo, "commit", "-qm", "red: specify next issue regression")
    (repo / "impl.py").write_text("fixed = True\n")
    _git(repo, "add", "impl.py")
    _git(repo, "commit", "-qm", "green: implement next issue")
    (repo / ".harness" / "ac-posted").touch()
    (repo / ".harness" / "test-plan-posted").touch()
    (repo / ".harness" / "adversarial-verdict").write_text("PASS\n")
    return repo


def _non_head_source(tmp_path: Path) -> Path:
    repo = fresh_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_keep.py").write_text(
        "def test_keep(): assert True\n"
    )
    _git(repo, "add", "tests/test_keep.py")
    _git(repo, "commit", "-qm", "test: baseline")
    _git(repo, "switch", "-qc", "bad-local-ref")
    (repo / "tests" / "test_keep.py").write_text(
        "def test_keep(): assert False\n"
    )
    _git(repo, "add", "tests/test_keep.py")
    _git(repo, "commit", "-qm", "test: weaken existing coverage")
    _git(repo, "switch", "-q", "dev")
    _git(repo, "switch", "-qc", "fix/13-open-pr")
    (repo / "tests" / "test_regression.py").write_text(
        "def test_regression(): assert True\n"
    )
    _git(repo, "add", "tests/test_regression.py")
    _git(repo, "commit", "-qm", "red: specify regression")
    (repo / "impl.py").write_text("fixed = True\n")
    _git(repo, "add", "impl.py")
    _git(repo, "commit", "-qm", "green: implement fix")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "ac-posted").touch()
    (repo / ".harness" / "test-plan-posted").touch()
    (repo / ".harness" / "adversarial-verdict").write_text("PASS\n")
    return repo


def _bulk_push_with_unvalidated_branch(tmp_path: Path) -> Path:
    repo = fresh_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_keep.py").write_text(
        "def test_keep(): assert True\n"
    )
    _git(repo, "add", "tests/test_keep.py")
    _git(repo, "commit", "-qm", "test: baseline")
    _git(repo, "switch", "-qc", "fix/13-open-pr")
    (repo / "tests" / "test_keep.py").write_text(
        "def test_keep(): assert False\n"
    )
    _git(repo, "add", "tests/test_keep.py")
    _git(repo, "commit", "-qm", "test: weaken existing coverage")
    _git(repo, "switch", "-q", "dev")
    _git(repo, "switch", "-qc", "fix/14-clean-checkout")
    (repo / "tests" / "test_regression.py").write_text(
        "def test_regression(): assert True\n"
    )
    _git(repo, "add", "tests/test_regression.py")
    _git(repo, "commit", "-qm", "red: specify regression")
    (repo / "impl.py").write_text("fixed = True\n")
    _git(repo, "add", "impl.py")
    _git(repo, "commit", "-qm", "green: implement fix")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "ac-posted").touch()
    (repo / ".harness" / "test-plan-posted").touch()
    (repo / ".harness" / "adversarial-verdict").write_text("PASS\n")
    return repo


CASES = (
    Case("declared", "pr", _valid, _pr_argv(), True,
         env={"TEST_CMD": "test -f impl.py"}),
    Case("unicode", "pr",
         lambda path: _with_manifest(
             path, "１３ | tests/test_keep.py | Unicode issue digits |\n"
         ),
         _pr_argv(), False, env={"TEST_CMD": "true"},
         reason_contains="invalid issue"),
    Case("malformed", "pr",
         lambda path: _with_manifest(
             path, "13 | tests/test_keep.py | missing final field\n"
         ),
         _pr_argv(), False, env={"TEST_CMD": "true"},
         reason_contains="malformed"),
    Case("dirty_override", "pr", _dirty_override, _pr_argv(), False,
         env={"TEST_CMD": "true"}, reason_contains="unchanged or new file"),
    Case("dirty_recreated_deletion", "pr", _dirty_recreated_deletion,
         _pr_argv(), False, env={"TEST_CMD": "true"},
         reason_contains="replacement"),
    Case("inherited_manifest", "pr", _inherited_manifest, _pr_argv(), True,
         env={"TEST_CMD": "test -f impl.py"}),
    Case("push_destination_issue", "push",
         lambda path: _with_manifest(
             path, "14 | tests/test_keep.py | destination issue correction |\n"
         ),
         ["push", "origin", "HEAD:refs/heads/fix/14-correct-test"], True,
         env={"TEST_CMD": "true"}, open_pr="14"),
    Case("multi_ref_push_checks_every_destination", "push", _base_repo,
         [
             "push", "origin", "scratch",
             "HEAD:refs/heads/fix/13-correct-test",
         ],
         False, env={"TEST_CMD": "true"}, open_pr="13",
         reason_contains="existing test file"),
    Case("non_head_source_is_not_validated_as_checkout_head", "push",
         _non_head_source,
         ["push", "origin", "bad-local-ref:refs/heads/fix/13-open-pr"],
         False, env={"TEST_CMD": "test -f impl.py"}, open_pr="13",
         reason_contains="source ref"),
    Case("push_all_cannot_hide_an_unvalidated_branch", "push",
         _bulk_push_with_unvalidated_branch, ["push", "--all", "origin"],
         False, env={"TEST_CMD": "test -f impl.py"}, open_pr="13",
         reason_contains="bulk push"),
    Case("push_mirror_cannot_hide_an_unvalidated_branch", "push",
         _bulk_push_with_unvalidated_branch, ["push", "--mirror", "origin"],
         False, env={"TEST_CMD": "test -f impl.py"}, open_pr="13",
         reason_contains="bulk push"),
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_exception_gate_parity(case: Case, tmp_path: Path):
    bash, python = _run_case(case, tmp_path)
    assert bash.allowed == python.allowed == case.expect_allowed
    if case.reason_contains:
        assert case.reason_contains in bash.output
        assert case.reason_contains in python.output
