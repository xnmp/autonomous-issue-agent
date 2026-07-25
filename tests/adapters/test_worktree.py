"""Worktree management + GateContext builder, driven by a fake git runner."""

from issue_agent.adapters.worktree import (
    GitResult,
    Worktrees,
    build_gate_context,
    worktree_list_contains_issue,
)


class FakeGit:
    """Records git argv and answers from a scripted table keyed by a prefix of
    the joined args. Default: rc=0, empty out."""

    def __init__(self, table=None):
        self.calls = []
        self.table = table or {}

    def __call__(self, args, cwd):
        self.calls.append((args, cwd))
        key = " ".join(args)
        for prefix, result in self.table.items():
            if key.startswith(prefix):
                return result
        # `cat-file -e` on an unscripted path means "absent" → non-zero, like
        # real git (an unmatched probe must NOT read as "file exists at base").
        if key.startswith("cat-file -e"):
            return GitResult(1, "")
        return GitResult(0, "")


def test_ensure_reuses_existing_worktree():
    fg = FakeGit({"worktree list": GitResult(0, "worktree /repo/.claude/worktrees/issue-5\n")})
    wt = Worktrees("/repo", run=fg)
    path = wt.ensure(5, "fix/5-x")
    assert path.endswith("issue-5")
    # existing → no `worktree add`
    assert not any(a[0][:2] == ["worktree", "add"] for a in fg.calls)


def test_worktree_list_matches_exact_issue_directory_not_prefix():
    listing = "\n".join([
        "worktree /repo/.claude/worktrees/issue-50",
        "HEAD abc123",
        "branch refs/heads/fix/50-x",
    ])

    assert worktree_list_contains_issue(listing, 50) is True
    assert worktree_list_contains_issue(listing, 5) is False


def test_ensure_does_not_reuse_issue_with_prefix_collision():
    fg = FakeGit({
        "worktree list": GitResult(0, "worktree /repo/.claude/worktrees/issue-50\n"),
        "show-ref": GitResult(1, ""),
    })
    Worktrees("/repo", base_branch="dev", run=fg).ensure(5, "fix/5-x")

    assert any(a[0][:2] == ["worktree", "add"] for a in fg.calls)


def test_ensure_creates_from_base_when_no_branch():
    fg = FakeGit({
        "worktree list": GitResult(0, ""),                 # none yet
        "show-ref": GitResult(1, ""),                       # branch doesn't exist
    })
    wt = Worktrees("/repo", base_branch="dev", run=fg)
    wt.ensure(5, "fix/5-x")
    add = [a for a in fg.calls if a[0][:2] == ["worktree", "add"]]
    assert add and "-b" in add[0][0] and "origin/dev" in add[0][0]


def test_remove_calls_remove_and_prune():
    fg = FakeGit()
    Worktrees("/repo", run=fg).remove(5)
    kinds = [a[0][:2] for a in fg.calls]
    assert ["worktree", "remove"] in kinds and ["worktree", "prune"] in kinds


def test_reset_discards_uncommitted_in_the_issue_worktree():
    fg = FakeGit()
    Worktrees("/repo", worktree_root=".claude/worktrees", run=fg).reset(5)
    kinds = [a[0] for a in fg.calls]
    assert ["checkout", "--", "."] in kinds
    assert ["clean", "-fd"] in kinds
    # both run IN the issue's worktree, not the repo root
    assert all(cwd.endswith("issue-5") for _, cwd in fg.calls)


# ── GateContext builder ──
def _builder_git(branch="fix/5-x", subjects="red: t\ngreen: impl", changed="src/x.py\ntests/test_x.py"):
    return FakeGit({
        "rev-parse --abbrev-ref": GitResult(0, branch + "\n"),
        "rev-parse --verify --quiet origin/dev": GitResult(0, "ok"),
        "log origin/dev..HEAD": GitResult(0, subjects + "\n"),
        "diff --name-only": GitResult(0, changed + "\n"),
    })


def test_build_gate_context_reads_branch_subjects_changed():
    ctx = build_gate_context("/wt", base_branch="dev", git=_builder_git())
    assert ctx.branch == "fix/5-x"
    assert ctx.base_resolvable is True
    assert ctx.branch_subjects == ("red: t", "green: impl")
    assert ctx.changed_files == ("src/x.py", "tests/test_x.py")


def test_build_gate_context_unresolvable_base():
    fg = FakeGit({
        "rev-parse --abbrev-ref": GitResult(0, "fix/5-x\n"),
        "rev-parse --verify --quiet origin/dev": GitResult(1, ""),
        "rev-parse --verify --quiet dev": GitResult(1, ""),
    })
    ctx = build_gate_context("/wt", base_branch="dev", git=fg)
    assert ctx.base_resolvable is False
    assert ctx.branch_subjects == ()


def test_build_gate_context_exists_at_base_uses_cat_file():
    fg = _builder_git()
    fg.table["cat-file -e origin/dev:src/x.py"] = GitResult(0, "")
    ctx = build_gate_context("/wt", base_branch="dev", git=fg)
    assert ctx.exists_at_base("src/x.py") is True
    assert ctx.exists_at_base("tests/test_new.py") is False  # default rc≠0 path


def test_build_gate_context_wires_run_tests():
    ctx = build_gate_context("/wt", base_branch="dev", git=_builder_git(),
                             run_tests=lambda cmd: cmd == "pytest")
    assert ctx.run_tests("pytest") is True
    assert ctx.run_tests("false") is False


# ── default run_tests: timeout (P0.5 — a hung TEST_CMD must not wedge the gate) ──
def test_default_run_tests_denies_on_timeout(tmp_path, monkeypatch, capsys):
    """A hung TEST_CMD must resolve to FAILURE (False), not hang forever and not
    be silently swallowed as a pass — same failure class as FAILURE-MODES #24.
    No `run_tests` override is passed, so this exercises the real subprocess
    default, bounded by a tiny GATE_TEST_TIMEOUT_S."""
    monkeypatch.setenv("GATE_TEST_TIMEOUT_S", "1")
    ctx = build_gate_context(str(tmp_path), base_branch="dev", git=_builder_git())

    passed = ctx.run_tests("sleep 5")

    assert passed is False
    err = capsys.readouterr().err
    assert "timed out" in err.lower()
    assert "1" in err  # names the configured timeout


def test_default_run_tests_passes_fast_command_unaffected(tmp_path, monkeypatch):
    """The timeout must not disturb the ordinary (fast, well-behaved) case."""
    monkeypatch.setenv("GATE_TEST_TIMEOUT_S", "5")
    ctx = build_gate_context(str(tmp_path), base_branch="dev", git=_builder_git())

    assert ctx.run_tests("true") is True
    assert ctx.run_tests("false") is False


def test_default_run_tests_malformed_timeout_env_falls_back_to_default(tmp_path, monkeypatch):
    """A garbled or empty GATE_TEST_TIMEOUT_S must fall back to the default
    rather than disabling the timeout (or crashing) — don't actually wait out
    the 600s default, just confirm a fast command still runs and passes under
    it."""
    monkeypatch.setenv("GATE_TEST_TIMEOUT_S", "not-a-number")
    ctx = build_gate_context(str(tmp_path), base_branch="dev", git=_builder_git())
    assert ctx.run_tests("true") is True

    monkeypatch.setenv("GATE_TEST_TIMEOUT_S", "")
    ctx = build_gate_context(str(tmp_path), base_branch="dev", git=_builder_git())
    assert ctx.run_tests("true") is True

    monkeypatch.setenv("GATE_TEST_TIMEOUT_S", "-5")
    ctx = build_gate_context(str(tmp_path), base_branch="dev", git=_builder_git())
    assert ctx.run_tests("true") is True
