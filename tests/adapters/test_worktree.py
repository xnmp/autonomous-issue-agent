"""Worktree management + GateContext builder, driven by a fake git runner."""

import os
import subprocess

import pytest

from issue_agent.adapters import worktree as wt_mod
from issue_agent.adapters.governor import evaluate_command
from issue_agent.adapters.worktree import (
    GitResult,
    WorktreeUnavailable,
    Worktrees,
    build_gate_context,
    worktree_list_contains_issue,
)
from issue_agent.domain.gate import OpenPrLookup


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
    # /repo isn't a real directory in the test environment — the honesty check
    # (M20, below) is exercised separately; these git-wiring tests fake it away.
    wt = Worktrees("/repo", run=fg, path_exists=lambda p: True)
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
    Worktrees("/repo", base_branch="dev", run=fg, path_exists=lambda p: True).ensure(5, "fix/5-x")

    assert any(a[0][:2] == ["worktree", "add"] for a in fg.calls)


def test_ensure_creates_from_base_when_no_branch():
    fg = FakeGit({
        "worktree list": GitResult(0, ""),                 # none yet
        "show-ref": GitResult(1, ""),                       # branch doesn't exist
    })
    wt = Worktrees("/repo", base_branch="dev", run=fg, path_exists=lambda p: True)
    wt.ensure(5, "fix/5-x")
    add = [a for a in fg.calls if a[0][:2] == ["worktree", "add"]]
    assert add and "-b" in add[0][0] and "origin/dev" in add[0][0]


# ── M20: `ensure` must be HONEST about readiness ──────────────────────────────
def test_ensure_raises_when_every_worktree_add_attempt_fails():
    """Before this, `ensure` returned the path regardless of whether any
    `worktree add` actually succeeded — the caller logged "ready" and
    dispatched the agent into a directory that was never created. With the
    REAL default `path_exists` (os.path.isdir) and a repo dir that plainly
    doesn't exist on disk, `ensure` must raise rather than lie."""
    fg = FakeGit({
        "worktree list": GitResult(0, ""),                       # none yet
        "show-ref": GitResult(1, ""),                             # branch doesn't exist
        "worktree add": GitResult(1, "fatal: boom"),              # every add attempt fails
    })
    wt = Worktrees("/definitely/not/a/real/repo-dir", base_branch="dev", run=fg)

    with pytest.raises(WorktreeUnavailable):
        wt.ensure(5, "fix/5-x")


def test_ensure_returns_real_path_when_worktree_add_actually_creates_it(tmp_path):
    """Anti-vacuity twin: when a 'worktree add' genuinely produces the
    directory, `ensure` must still succeed — the honesty check is a real
    verification, not a trap that also denies the success path. Uses the REAL
    default `path_exists` against a real tmp_path directory."""
    repo_dir = str(tmp_path)
    expected_rel = Worktrees(repo_dir).path(7)
    expected_abs = os.path.join(repo_dir, expected_rel)

    def git(args, cwd):
        if args[:2] == ["worktree", "add"]:
            os.makedirs(expected_abs, exist_ok=True)
            return GitResult(0, "")
        if args[0] == "show-ref":
            return GitResult(1, "")
        return GitResult(0, "")

    wt = Worktrees(repo_dir, base_branch="dev", run=git)

    path = wt.ensure(7, "fix/7-x")

    assert path == expected_abs
    assert os.path.isdir(path)


# ── M20: the git runner must be bounded and must surface failures ────────────
def test_default_git_runner_times_out_and_reports_rather_than_hanging(tmp_path, monkeypatch, capsys):
    """A hanging `git` subprocess must be killed and reported as a failure, not
    hang the daemon or the agent's session — same failure class as the 14-min
    `gh pr view` hang (github.py's `_GH_TIMEOUT_S`). Uses a REAL subprocess: a
    fake `git` on PATH that sleeps, with `_GIT_TIMEOUT_S` monkeypatched tiny."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nsleep 5\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(wt_mod, "_GIT_TIMEOUT_S", 0.2)

    result = wt_mod._subprocess_git(["status"], str(tmp_path))

    assert result.code != 0            # reported as failure, never a silent pass
    err = capsys.readouterr().err
    assert "timed out" in err.lower()


def test_default_git_runner_surfaces_stderr_on_failure(tmp_path, monkeypatch, capsys):
    """A non-zero `git` exit must be surfaced (not silently swallowed) so a
    wedged worktree op is visible in the daemon log — the other half of M20."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\necho 'fatal: kaboom' >&2\nexit 1\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    result = wt_mod._subprocess_git(["worktree", "add", "x", "y"], str(tmp_path))

    assert result.code == 1
    err = capsys.readouterr().err
    assert "kaboom" in err


def test_default_git_runner_real_success_path_unaffected(tmp_path):
    """Anti-vacuity twin: the timeout/stderr-surfacing changes must not disturb
    the ordinary (fast, well-behaved) case — a real `git` invocation still
    returns its stdout untouched."""
    result = wt_mod._subprocess_git(["--version"], str(tmp_path))

    assert result.code == 0
    assert "git version" in result.out


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


# ── open-PR probe: the push gate's precondition (review known-gap-2) ──────────
class FakeGh:
    """Records gh argv/cwd and answers with a scripted GitResult."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else GitResult(0, "[]")

    def __call__(self, args, cwd):
        self.calls.append((args, cwd))
        return self.result


def _ctx_with_gh(gh, branch="fix/5-x"):
    return build_gate_context(
        "/wt", base_branch="dev", git=_builder_git(branch=branch), gh=gh,
        retry_sleep=lambda: None,
    )


def test_build_gate_context_does_not_call_gh_until_a_push_is_gated():
    """Laziness: this builder runs on EVERY Bash call the agent makes, so the
    open-PR lookup must cost nothing until the governor actually gates a push."""
    gh = FakeGh(GitResult(0, '[{"number": 7}]'))

    ctx = _ctx_with_gh(gh)

    assert gh.calls == []                       # built without touching GitHub
    assert ctx.open_pr() is OpenPrLookup.OPEN   # …and answers when asked
    assert len(gh.calls) == 1


def test_open_pr_probe_asks_about_the_current_branch_with_an_explicit_limit():
    gh = FakeGh(GitResult(0, "[]"))
    _ctx_with_gh(gh, branch="fix/9-thing").open_pr()

    (argv, cwd), = gh.calls
    assert argv[:2] == ["pr", "list"]
    assert "--head" in argv and argv[argv.index("--head") + 1] == "fix/9-thing"
    assert "--state" in argv and argv[argv.index("--state") + 1] == "open"
    assert "--limit" in argv          # review C2: a list call always states its ceiling
    assert cwd == "/wt"               # asked from inside the worktree


def test_open_pr_probe_reports_open_none_and_error_distinctly():
    """The tri-state's whole point: a failed lookup must NOT look like 'no PR'."""
    assert _ctx_with_gh(FakeGh(GitResult(0, '[{"number": 7}]'))).open_pr() is OpenPrLookup.OPEN
    assert _ctx_with_gh(FakeGh(GitResult(0, "[]"))).open_pr() is OpenPrLookup.NONE
    assert _ctx_with_gh(FakeGh(GitResult(1, ""))).open_pr() is OpenPrLookup.ERROR


def test_open_pr_probe_reports_error_on_unreadable_output():
    """Blank / malformed / non-list output on a 0 exit is an answer we cannot
    read — an error, never an absence."""
    for out in ("", "not json", "null", '{"number": 7}'):
        assert _ctx_with_gh(FakeGh(GitResult(0, out))).open_pr() is OpenPrLookup.ERROR


def test_default_gh_runner_is_bounded_and_reports_error_on_timeout(monkeypatch):
    """The probe runs inline in the PreToolUse hook: a hung `gh` must be killed
    and reported as 'could not look', never hang the agent's push."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)
    ctx = build_gate_context("/wt", base_branch="dev", git=_builder_git())

    assert ctx.open_pr() is OpenPrLookup.ERROR
    assert seen["argv"][0] == "gh"
    assert seen["kwargs"]["timeout"] > 0        # bounded from day one


def test_default_gh_runner_reports_error_when_gh_is_missing(monkeypatch):
    """No `gh` on PATH is an infrastructure failure, not an empty world."""
    def boom(argv, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(wt_mod.subprocess, "run", boom)
    ctx = build_gate_context("/wt", base_branch="dev", git=_builder_git())

    assert ctx.open_pr() is OpenPrLookup.ERROR


def test_default_gh_runner_returns_open_for_a_listed_pr(monkeypatch):
    """Anti-vacuity twin for the two error tests: the real runner's happy path
    still resolves through the same seam."""
    class Done:
        returncode = 0
        stdout = '[{"number": 7}]'

    monkeypatch.setattr(wt_mod.subprocess, "run", lambda argv, **kw: Done())
    ctx = build_gate_context("/wt", base_branch="dev", git=_builder_git())

    assert ctx.open_pr() is OpenPrLookup.OPEN


def _context_for_push(gh):
    return build_gate_context(
        "/wt", base_branch="dev", git=_builder_git(), gh=gh,
        env_test_cmd="pytest", run_tests=lambda cmd: False,   # suite is RED
        retry_sleep=lambda: None,
    )


def test_a_built_context_actually_arms_the_push_gate():
    """review known-gap-2, across the seam that was the hole: both halves were
    correct in isolation (the domain's tri-state was tested, the builder built a
    context) — nothing CONNECTED them, so every push to an open PR ran ungated.
    Drive the real builder output through the real gate: an open PR + a failing
    suite is denied, the identical context with no open PR is allowed."""
    denied = evaluate_command("git push", _context_for_push(FakeGh(GitResult(0, '[{"number": 7}]'))))
    assert not denied.allowed and "test suite failed" in denied.reason

    allowed = evaluate_command("git push", _context_for_push(FakeGh(GitResult(0, "[]"))))
    assert allowed.allowed


def test_a_built_context_fails_a_push_closed_when_gh_cannot_answer():
    """…and a `gh` that cannot answer denies rather than waving the push through
    (the collapse of "could not look" into "no PR" that the bash used to make)."""
    gh = FakeGh(GitResult(1, ""))
    v = evaluate_command("git push", _context_for_push(gh))

    assert not v.allowed and "could not verify PR state" in v.reason
    assert len(gh.calls) == 2  # the domain's retry actually reached gh twice
