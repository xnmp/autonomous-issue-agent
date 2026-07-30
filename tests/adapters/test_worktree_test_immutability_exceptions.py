"""The gate authorizes only the manifest blob committed at HEAD."""

from issue_agent.adapters.worktree import GitResult, build_gate_context


class FakeGit:
    def __call__(self, args: list[str], _cwd: str) -> GitResult:
        command = " ".join(args)
        if command == "rev-parse --abbrev-ref HEAD":
            return GitResult(0, "fix/124-exception\n")
        if command.startswith("rev-parse --verify --quiet origin/dev"):
            return GitResult(0, "base\n")
        if command == "show HEAD:.harness/test-immutability-exceptions":
            return GitResult(
                0, "124 | tests/test_x.py | committed expectation was wrong |\n"
            )
        if command.startswith("cat-file -e"):
            return GitResult(1, "")
        return GitResult(0, "")


def test_dirty_local_manifest_cannot_replace_committed_authorization(tmp_path):
    dirty = tmp_path / ".harness" / "test-immutability-exceptions"
    dirty.parent.mkdir()
    dirty.write_text("124 | tests/test_dirty.py | hidden authorization |\n")

    context = build_gate_context(str(tmp_path), git=FakeGit())

    assert "tests/test_x.py" in context.test_immutability_exceptions
    assert "tests/test_dirty.py" not in context.test_immutability_exceptions
