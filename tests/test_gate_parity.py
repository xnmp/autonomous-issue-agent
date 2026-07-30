# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter>=0.22", "tree-sitter-bash>=0.23", "pytest>=8"]
# ///
"""Gate PARITY — one golden case, both gate stacks, identical verdict (review M6).

The harness has two implementations of the same gate:

  * `plugin/hooks/checks/*.sh` — live for the **codex** runner, invoked by
    `plugin/hooks/bash_governor.py`;
  * `src/issue_agent/domain/gate.py` + `adapters/governor.py` — live for the
    **SDK/Claude** runner, the reviewer and the steward.

They shared a spec, not code, and had drifted: `check-pr.sh` used one three-dot
range for both the diff and the `git log`, so the moment the base branch
advanced it blocked every legitimate PR and push; and two rules (the ADR
citation and the commit-time lint/typecheck) existed ONLY in bash while
`plugin/AGENT.md` and `bin/doctor.sh` told operators and agents that the gate
enforced them.  "Keep in sync with …" comments were the only sync mechanism,
and comments do not fail a build.

This module is the replacement mechanism.  Each golden case builds ONE real
scratch git repo, then runs the SAME command through BOTH stacks:

  bash   → `bash plugin/hooks/checks/check-*.sh <argv>` in the repo
  python → the REAL PreToolUse hook (`adapters.governor.make_pretooluse_hook`)
           over a context from the REAL `adapters.worktree.build_gate_context`

and asserts (a) the two agree AND (b) they both match an expectation stated
independently in the case table.  (b) matters: "both stacks wrongly allow" is
the failure mode a pure agreement test cannot see.

Three deliberate extras, from the 2026-07-26 re-review's §5 process finding
("mutation testing proves a guard fires when code changes; it cannot prove a
guard exists for a path nobody wrote a test for"):

  * ABSENCE tests — delete the wiring that makes a ported rule live and assert
    parity breaks loudly (`test_absence_*`).
  * MUTATION tests — textually re-introduce the exact M6 bug into a COPY of the
    checks dir and assert the named case flips (`test_mutation_*`).
  * DIVERGENCE tests — where the two stacks legitimately differ, the difference
    is asserted with the finding that caused it, never dropped
    (`test_divergence_*`).

Run: uv run tests/test_gate_parity.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from issue_agent.adapters import governor  # noqa: E402
from issue_agent.adapters.worktree import GitResult, build_gate_context  # noqa: E402

CHECKS = ROOT / "plugin" / "hooks" / "checks"

# Every environment knob either stack reads. Stripped from the inherited
# environment before each case so a developer's exported TEST_CMD cannot change
# a verdict — and so the two stacks are provably fed the SAME environment.
GATE_ENV_KNOBS = (
    "TEST_CMD", "LINT_CMD", "TYPECHECK_CMD", "ALLOW_UNTESTED", "RED_MUST_FAIL",
    "VERIFY_RED_AT_COMMIT", "BASE_BRANCH", "GATE_TEST_TIMEOUT_S",
    "GATE_GH_RETRY_SLEEP",
)


# ── scratch repositories ─────────────────────────────────────────────────────
def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def fresh_repo(tmp_path: Path) -> Path:
    """A throwaway repo on `dev` with one seed commit — the same shape
    tests/test_hooks.sh's `fresh_repo` builds, so fixtures stay comparable."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "chore: seed")
    return repo


def compliant_pr_repo(tmp_path: Path) -> Path:
    """A fix/ branch that satisfies every rule: red-then-green commits, a new
    regression test, all `.harness/` markers, and a test command that passes
    only WITH the implementation (so §7c's revert genuinely goes red)."""
    repo = fresh_repo(tmp_path)
    (repo / ".harness").mkdir()
    (repo / "tests").mkdir()
    _git(repo, "switch", "-qc", "fix/7-token")
    (repo / "tests" / "test_token_regression.py").write_text("def test_token(): assert True\n")
    _git(repo, "add", "tests/test_token_regression.py")
    _git(repo, "commit", "-qm", "red: regression test for token")
    (repo / "token.py").write_text("def token(): return 1\n")
    _git(repo, "add", "token.py")
    _git(repo, "commit", "-qm", "green: implement token")
    (repo / ".harness" / "ac-posted").touch()
    (repo / ".harness" / "test-plan-posted").touch()
    (repo / ".harness" / "adversarial-verdict").write_text("PASS looks good\n")
    (repo / ".harness" / "test-cmd").write_text("test -f token.py\n")
    return repo


def advance_base(repo: Path) -> None:
    """Move the BASE branch forward past the PR branch with a merge-shaped
    subject that carries no allowed commit prefix, then return to the branch.

    This is the M6 [repro]: `git log <base>...HEAD` (THREE dots) is the
    symmetric difference, so it hands the commit-hygiene check the BASE's own
    commits.  "Merge pull request #7 from x/y" has no allowed prefix, so the
    bash gate denied every PR and every push on the branch — permanently, and
    for a reason the agent could not act on.  Two dots (`..`) lists only what
    the branch adds.  Every real repo reaches this state the first time
    anything merges into `dev`."""
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(repo, "switch", "-q", "dev")
    (repo / "CHANGELOG.md").write_text("# changelog\n")
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-qm", "Merge pull request #7 from someone/other-work")
    _git(repo, "switch", "-q", branch)


def write_adr(repo: Path, filename: str, governs: str) -> None:
    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)
    (adr_dir / filename).write_text(
        f"# {filename}\n\nStatus: accepted\nGoverns: {governs}\n\nDecision: …\n"
    )


def commit_evidence(repo: Path, relpath: str, content: bytes = b"\x89PNG") -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-qm", "evidence: proof")


def write_gh_stub(repo: Path, pr_number: str) -> Path:
    """A `gh` on PATH that answers check-push.sh's open-PR probe.  Non-empty
    `pr_number` ⇒ the branch has an open PR.  Returns the dir to prepend."""
    stub_dir = repo / ".stub"
    stub_dir.mkdir(exist_ok=True)
    gh = stub_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in\n'
        f'  *" pr list "*) printf \'%s\' "{pr_number}" ;;\n'
        "  *) : ;;\n"
        "esac\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub_dir


def fake_gh_runner(pr_number: str) -> Callable[[list[str], str], GitResult]:
    """The Python-side adapter of the SAME world fact the PATH stub encodes.
    The two shapes differ because the two stacks ask differently (bash uses
    `--jq`, the Python probe parses `--json number`); the FACT is identical,
    which is what the parity assertion is about."""
    payload = f'[{{"number": {pr_number}}}]' if pr_number else "[]"

    def run(_args: list[str], _cwd: str) -> GitResult:
        return GitResult(0, payload)

    return run


# ── the two stacks ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Verdict:
    allowed: bool
    output: str

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        state = "ALLOW" if self.allowed else "DENY"
        return f"<{state}: {self.output.strip()[:200]}>"


# Which checker script(s) a governed command kind routes to, in the order
# plugin/hooks/bash_governor.py runs them (a `git commit` runs BOTH).
BASH_CHECKERS = {
    "pr": ("check-pr.sh",),
    "push": ("check-push.sh",),
    "commit": ("check-commit.sh", "check-commit-order.sh"),
}


def bash_verdict(
    kind: str, repo: Path, argv: list[str], env: dict[str, str],
    checks_dir: Path | None = None,
) -> Verdict:
    """Run the bash stack exactly as `bash_governor.py` would: the checker(s)
    for this kind, in order, in the repo, with a sanitised environment.  First
    non-zero exit is the verdict."""
    checks = checks_dir or CHECKS
    full_env = {k: v for k, v in os.environ.items() if k not in GATE_ENV_KNOBS}
    full_env.update(env)
    for script in BASH_CHECKERS[kind]:
        proc = subprocess.run(
            ["bash", str(checks / script), *argv],
            cwd=repo, env=full_env, capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            return Verdict(False, proc.stdout + proc.stderr)
    return Verdict(True, "")


@contextlib.contextmanager
def _environment(env: dict[str, str]):
    """Put the case's knobs in `os.environ` for the duration of the Python run.

    Not a convenience: `complete_gate_context` reads LINT_CMD/TYPECHECK_CMD
    from the process environment because that is the very same source the bash
    checker reads (it is spawned by the daemon and inherits its env).  Feeding
    the two stacks from one dict is what makes the comparison honest."""
    saved = {k: os.environ.get(k) for k in set(env) | set(GATE_ENV_KNOBS)}
    try:
        for key in GATE_ENV_KNOBS:
            os.environ.pop(key, None)
        os.environ.update(env)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def python_verdict(
    kind: str, repo: Path, argv: list[str], env: dict[str, str],
    open_pr: str = "", complete: bool = True,
) -> Verdict:
    """Run the Python stack through the REAL PreToolUse hook.

    Going through the hook (rather than calling `evaluate_command` directly) is
    the point: the hook is where `complete_gate_context` wires the two ported
    rules, and an absence test can only be meaningful if the wiring is inside
    the code under test.  `complete=False` simulates that wiring being gone."""
    tool = {"pr": "gh", "push": "git", "commit": "git"}[kind]
    # A real shell command string, because the Python stack's first step is a
    # tree-sitter PARSE of one — feeding it a pre-split argv would skip the layer
    # the bash stack's dispatcher also runs.
    command = shlex.join([tool, *argv])

    def build_ctx(cwd: str):
        return build_gate_context(
            cwd,
            base_branch=env.get("BASE_BRANCH", "dev"),
            repo="owner/repo",
            env_test_cmd=env.get("TEST_CMD"),
            red_must_fail=env.get("RED_MUST_FAIL", "1") != "0",
            verify_red_at_commit=env.get("VERIFY_RED_AT_COMMIT", "1") != "0",
            allow_untested=env.get("ALLOW_UNTESTED", "") == "1",
            gh=fake_gh_runner(open_pr),
        )

    if not complete:
        # The ABSENCE shape: the hook's `complete_gate_context(...)` call
        # removed, i.e. exactly the state review M6 found the SDK path in.
        with _environment(env):
            verdict = governor.evaluate_command(command, build_ctx(str(repo)))
        return Verdict(verdict.allowed, verdict.reason)

    hook = governor.make_pretooluse_hook(build_ctx)
    with _environment(env):
        result = asyncio.run(
            hook({"tool_input": {"command": command}, "cwd": str(repo)}, None, None)
        )
    specific = (result or {}).get("hookSpecificOutput") or {}
    if specific.get("permissionDecision") == "deny":
        return Verdict(False, specific.get("permissionDecisionReason", ""))
    return Verdict(True, "")


# ── golden cases ─────────────────────────────────────────────────────────────
BODY_OK = "## Acceptance Criteria\n- token works\n\nFixes #7"
BODY_NO_AC = "Fixes #7\nsome text"
BODY_NO_LINK = "## Acceptance Criteria\n- token works"
BODY_ADR = "## Acceptance Criteria\n- token works\n\nPer ADR 0003.\n\nFixes #7"


def _pr_argv(body: str = BODY_OK, base: str = "dev", extra: list[str] | None = None):
    argv = ["pr", "create", "--base", base, "--title", "t", "--body", body]
    return argv + (extra or [])


@dataclass(frozen=True)
class Case:
    name: str
    kind: str                                   # pr | push | commit
    setup: Callable[[Path], Path]
    argv: list[str]
    expect_allowed: bool
    env: dict[str, str] = field(default_factory=dict)
    open_pr: str = ""                           # push cases: the PR number, or ""
    # Substring the DENY reason must contain on both stacks. Empty ⇒ only the
    # allow/deny verdict is compared (used where the two stacks phrase the same
    # rule differently, which is noted per-case).
    reason_contains: str = ""


def _visual_repo(tmp_path: Path) -> Path:
    repo = compliant_pr_repo(tmp_path)
    (repo / ".harness" / "visual-ac").touch()
    return repo


def _adr_repo(governs: str = "token.py"):
    def build(tmp_path: Path) -> Path:
        repo = compliant_pr_repo(tmp_path)
        write_adr(repo, "0003-token-layer.md", governs)
        return repo

    return build


def _commit_repo(tmp_path: Path) -> Path:
    """A branch with a `red:` already on it, so a `green:` commit is legal and
    the ordering rule never masks the rule a case is actually about."""
    repo = fresh_repo(tmp_path)
    _git(repo, "switch", "-qc", "fix/7-token")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_token_regression.py").write_text("def test_token(): assert True\n")
    _git(repo, "add", "tests/test_token_regression.py")
    _git(repo, "commit", "-qm", "red: regression test")
    return repo


def _no_red_repo(tmp_path: Path) -> Path:
    repo = fresh_repo(tmp_path)
    _git(repo, "switch", "-qc", "fix/7-token")
    return repo


def _base_advanced_repo(tmp_path: Path) -> Path:
    repo = compliant_pr_repo(tmp_path)
    advance_base(repo)
    return repo


def _base_advanced_bad_branch_commit_repo(tmp_path: Path) -> Path:
    """The anti-vacuity twin of the M6 fix: the base has advanced AND the branch
    itself carries a badly-prefixed commit.  Narrowing the log range must stop
    reading the base's commits WITHOUT stopping reading the branch's — an
    "always empty" range would pass `base_advanced_pr` while silently disabling
    commit hygiene, which is the absence-shaped regression §5 warns about."""
    repo = compliant_pr_repo(tmp_path)
    (repo / "extra.py").write_text("x = 1\n")
    _git(repo, "add", "extra.py")
    _git(repo, "commit", "-qm", "wip whatever")
    advance_base(repo)
    return repo


def _non_image_evidence_repo(tmp_path: Path) -> Path:
    repo = compliant_pr_repo(tmp_path)
    (repo / "evidence").mkdir()
    (repo / "evidence" / "run.log").write_text("noise\n")
    _git(repo, "add", "evidence/run.log")
    _git(repo, "commit", "-qm", "chore: oops evidence")
    return repo


def _read_screenshot_repo(tmp_path: Path) -> Path:
    repo = _visual_repo(tmp_path)
    commit_evidence(repo, "evidence/ac-1.png")
    (repo / ".harness" / "screenshots-read").write_text("ac-1.png\n")
    return repo


def _weakened_test_repo(tmp_path: Path) -> Path:
    repo = fresh_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_keep.py").write_text("def test_keep(): assert True\n")
    _git(repo, "add", "tests/test_keep.py")
    _git(repo, "commit", "-qm", "test: baseline")
    (repo / ".harness").mkdir()
    _git(repo, "switch", "-qc", "fix/13-modtest")
    (repo / "tests" / "test_new_regression.py").write_text("def test_new(): assert True\n")
    _git(repo, "add", "tests/test_new_regression.py")
    _git(repo, "commit", "-qm", "red: new")
    (repo / "tests" / "test_keep.py").write_text("def test_keep(): assert False\n")
    (repo / "impl.py").write_text("x = 1\n")
    _git(repo, "add", "tests/test_keep.py", "impl.py")
    _git(repo, "commit", "-qm", "green: impl + weaken existing test")
    (repo / ".harness" / "ac-posted").touch()
    (repo / ".harness" / "test-plan-posted").touch()
    (repo / ".harness" / "adversarial-verdict").write_text("PASS\n")
    return repo


def _test_exception_repo(tmp_path: Path, manifest: str) -> Path:
    repo = _weakened_test_repo(tmp_path)
    path = repo / ".harness" / "test-immutability-exceptions"
    path.write_text(manifest)
    _git(repo, "add", "-f", ".harness/test-immutability-exceptions")
    _git(repo, "commit", "-qm", "chore: declare test exception")
    return repo


def _valid_test_exception_repo(tmp_path: Path) -> Path:
    return _test_exception_repo(
        tmp_path,
        "13 | tests/test_keep.py | baseline assertion encoded the defect |\n",
    )


def _mismatched_test_exception_repo(tmp_path: Path) -> Path:
    return _test_exception_repo(
        tmp_path,
        "13 | tests/test_other.py | unrelated expectation is wrong |\n",
    )


def _malformed_test_exception_repo(tmp_path: Path) -> Path:
    return _test_exception_repo(
        tmp_path,
        "13 | tests/test_keep.py | missing final field\n",
    )


def _dirty_manifest_override_repo(tmp_path: Path) -> Path:
    repo = _mismatched_test_exception_repo(tmp_path)
    (repo / ".harness" / "test-immutability-exceptions").write_text(
        "13 | tests/test_keep.py | dirty local authorization must be ignored |\n"
    )
    return repo


GOLDEN: tuple[Case, ...] = (
    # ── the happy path: everything the harness asks for, allowed by both ─────
    Case("good_pr", "pr", compliant_pr_repo, _pr_argv(), True),

    # ── PR shape (§0b/§2/§3/§4/§8) ───────────────────────────────────────────
    Case("bad_base", "pr", compliant_pr_repo, _pr_argv(base="main"), False,
         reason_contains="must be 'dev'"),
    Case("no_base", "pr", compliant_pr_repo,
         ["pr", "create", "--title", "t", "--body", BODY_OK], False,
         reason_contains="no --base"),
    Case("missing_ac", "pr", compliant_pr_repo, _pr_argv(body=BODY_NO_AC), False,
         reason_contains="Acceptance Criteria"),
    Case("missing_issue_link", "pr", compliant_pr_repo, _pr_argv(body=BODY_NO_LINK), False,
         reason_contains="Fixes #"),
    Case("web_flag", "pr", compliant_pr_repo, _pr_argv(extra=["--web"]), False,
         reason_contains="--web"),
    Case("missing_ac_marker", "pr",
         lambda p: _rm(compliant_pr_repo(p), ".harness/ac-posted"), _pr_argv(), False,
         reason_contains="ac-posted"),
    Case("failed_adversarial_verdict", "pr",
         lambda p: _write(compliant_pr_repo(p), ".harness/adversarial-verdict", "FAIL nope\n"),
         _pr_argv(), False, reason_contains="adversarial"),

    # ── M6 [repro]: the base branch moved on. The ONLY difference from
    #    `good_pr` is a commit on `dev`; the branch is untouched. ─────────────
    Case("base_advanced_pr", "pr", _base_advanced_repo, _pr_argv(), True),
    Case("base_advanced_push", "push", _base_advanced_repo, ["push"], True,
         open_pr="7"),
    Case("base_advanced_branch_still_checked", "pr",
         _base_advanced_bad_branch_commit_repo, _pr_argv(), False,
         reason_contains="allowed prefix"),

    # ── evidence rules (§7/§7d) ──────────────────────────────────────────────
    Case("non_image_evidence", "pr", _non_image_evidence_repo, _pr_argv(), False,
         reason_contains="evidence/"),
    Case("visual_ac_without_screenshot", "pr", _visual_repo, _pr_argv(), False,
         reason_contains="no screenshot is committed"),
    Case("visual_ac_with_read_screenshot", "pr", _read_screenshot_repo, _pr_argv(), True),
    Case("existing_test_weakened", "pr", _weakened_test_repo, _pr_argv(), False,
         env={"TEST_CMD": "true"}, reason_contains="existing test file"),
    Case("existing_test_declared", "pr", _valid_test_exception_repo, _pr_argv(), True,
         env={"TEST_CMD": "test -f impl.py"}),
    Case("existing_test_declaration_mismatch", "pr", _mismatched_test_exception_repo,
         _pr_argv(), False, env={"TEST_CMD": "true"},
         reason_contains="unchanged or new file"),
    Case("existing_test_declaration_malformed", "pr", _malformed_test_exception_repo,
         _pr_argv(), False, env={"TEST_CMD": "true"}, reason_contains="malformed"),
    Case("existing_test_dirty_declaration_ignored", "pr", _dirty_manifest_override_repo,
         _pr_argv(), False, env={"TEST_CMD": "true"},
         reason_contains="unchanged or new file"),

    # ── ADR citation (§9) — PORTED to Python in wave P1-2. Before the port the
    #    Python column of these two rows was ALLOW/ALLOW: the rule was dead on
    #    the SDK path while AGENT.md claimed the gate enforced it. ────────────
    Case("adr_governed_path_uncited", "pr", _adr_repo("token.py"), _pr_argv(), False,
         reason_contains="ADR 0003"),
    Case("adr_governed_path_cited", "pr", _adr_repo("token.py"),
         _pr_argv(body=BODY_ADR), True),
    Case("adr_governs_untouched_path", "pr", _adr_repo("src/billing"), _pr_argv(), True),

    # ── commit-time lint / typecheck — PORTED to Python in wave P1-2. Same
    #    story: doctor printed "✓ LINT_CMD set" about a command the SDK path
    #    never ran. `docs:` keeps the ordering + red-at-commit rules inert so
    #    each row isolates the rule it names. ─────────────────────────────────
    Case("lint_fails", "commit", _commit_repo, ["commit", "-m", "docs: tidy"], False,
         env={"LINT_CMD": "false"}, reason_contains="Lint failed"),
    Case("lint_passes", "commit", _commit_repo, ["commit", "-m", "docs: tidy"], True,
         env={"LINT_CMD": "true"}),
    Case("typecheck_fails", "commit", _commit_repo, ["commit", "-m", "docs: tidy"], False,
         env={"LINT_CMD": "true", "TYPECHECK_CMD": "false"},
         reason_contains="Typecheck failed"),
    Case("no_project_commands_configured", "commit", _commit_repo,
         ["commit", "-m", "docs: tidy"], True),

    # ── commit hygiene (both stacks, unchanged by this wave — regression net) ─
    Case("commit_bad_prefix", "commit", _commit_repo, ["commit", "-m", "wip: stuff"], False,
         reason_contains="allowed prefix"),
    Case("green_without_red", "commit", _no_red_repo, ["commit", "-m", "green: impl"], False,
         reason_contains="TDD ordering"),

    # ── test-command precedence (P1R-18). The operator's TEST_CMD must win, on
    #    BOTH stacks: the agent writes `.harness/test-cmd` under
    #    bypassPermissions, so a marker that wins is a one-line disarm of §1 and
    #    §7c. The bash preferred the marker for one wave after the Python
    #    flipped — this pair is what catches that. ─────────────────────────────
    Case("operator_test_cmd_wins", "pr", compliant_pr_repo,
         _pr_argv(), True,
         # marker (below) is genuine; TEST_CMD is genuine too. Both green.
         env={"TEST_CMD": "test -f token.py"}),
    Case("operator_tautological_test_cmd_blocks", "pr", compliant_pr_repo,
         _pr_argv(), False,
         # The marker is a GENUINE command; the operator's is tautological.
         # Marker-wins would ALLOW; operator-wins denies. The verdict names the
         # precedence.
         env={"TEST_CMD": "true"}, reason_contains="still PASS"),
    Case("marker_used_when_operator_has_none", "pr",
         lambda p: _write(compliant_pr_repo(p), ".harness/test-cmd", "true\n"),
         _pr_argv(), False,
         # No TEST_CMD ⇒ the marker IS the resolved command, and §7c still
         # polices it: 'true' stays green on revert ⇒ tautological ⇒ deny.
         reason_contains="still PASS"),

    # ── no test command at all → fail CLOSED, and the operator opt-out ───────
    Case("no_test_command", "pr",
         lambda p: _rm(compliant_pr_repo(p), ".harness/test-cmd"), _pr_argv(), False,
         reason_contains="no test command resolved"),
    Case("no_test_command_with_opt_out", "pr",
         lambda p: _rm(compliant_pr_repo(p), ".harness/test-cmd"), _pr_argv(), True,
         env={"ALLOW_UNTESTED": "1"}),

    # ── push gate ────────────────────────────────────────────────────────────
    Case("push_without_open_pr", "push", compliant_pr_repo, ["push"], True),
    Case("push_with_open_pr_failing_tests", "push", compliant_pr_repo, ["push"], False,
         env={"TEST_CMD": "false"}, open_pr="7", reason_contains="local test suite failed"),
    Case("push_with_open_pr_green", "push", compliant_pr_repo, ["push"], True,
         env={"TEST_CMD": "true"}, open_pr="7"),
    Case("push_on_ungoverned_branch", "push",
         lambda p: _switch(fresh_repo(p), "scratch-branch"), ["push"], True,
         env={"TEST_CMD": "false"}, open_pr="7"),

    # ── --body-file. Both stacks must read the file: the Python side used to
    #    see an EMPTY body and deny "missing '## Acceptance Criteria'" on a PR
    #    whose body has one — fail-closed, but with a reason the agent could
    #    not act on. ────────────────────────────────────────────────────────
    Case("body_file", "pr",
         lambda p: _write(compliant_pr_repo(p), "pr-body.md", BODY_OK),
         ["pr", "create", "--base", "dev", "--title", "t", "--body-file", "pr-body.md"],
         True),
    Case("body_file_missing_ac", "pr",
         lambda p: _write(compliant_pr_repo(p), "pr-body.md", BODY_NO_AC),
         ["pr", "create", "--base", "dev", "--title", "t", "--body-file", "pr-body.md"],
         False, reason_contains="Acceptance Criteria"),
)


def _rm(repo: Path, relpath: str) -> Path:
    (repo / relpath).unlink()
    return repo


def _write(repo: Path, relpath: str, content: str) -> Path:
    (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
    (repo / relpath).write_text(content)
    return repo


def _switch(repo: Path, branch: str) -> Path:
    _git(repo, "switch", "-qc", branch)
    return repo


def _run_case(case: Case, tmp_path: Path, checks_dir: Path | None = None,
              complete: bool = True) -> tuple[Verdict, Verdict]:
    repo = case.setup(tmp_path)
    env = dict(case.env)
    env.setdefault("BASE_BRANCH", "dev")
    env.setdefault("GATE_GH_RETRY_SLEEP", "0")
    bash_env = dict(env)
    if case.kind == "push":
        stub_dir = write_gh_stub(repo, case.open_pr)
        bash_env["PATH"] = f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    return (
        bash_verdict(case.kind, repo, case.argv, bash_env, checks_dir=checks_dir),
        python_verdict(case.kind, repo, case.argv, env,
                       open_pr=case.open_pr, complete=complete),
    )


# ── the parity assertion ─────────────────────────────────────────────────────
@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c.name)
def test_gate_parity(case: Case, tmp_path: Path) -> None:
    """The bash gate and the Python gate reach the SAME verdict, and it is the
    verdict the case table declares.

    Both halves matter. Agreement alone would be satisfied by two stacks that
    are wrong in the same direction (the state M6 found: two rules dead on one
    side and unenforced on the other would have "agreed" if the bash had been
    deleted instead of ported). The independent `expect_allowed` column is the
    anti-vacuity twin."""
    bash, python = _run_case(case, tmp_path)
    assert bash.allowed == python.allowed, (
        f"GATE DRIFT on '{case.name}': bash={bash!r} python={python!r}"
    )
    assert bash.allowed is case.expect_allowed, (
        f"'{case.name}' expected {'ALLOW' if case.expect_allowed else 'DENY'} "
        f"but both stacks said {'ALLOW' if bash.allowed else 'DENY'} — either the "
        f"fixture or the rule changed. bash={bash!r}"
    )
    if case.reason_contains:
        assert case.reason_contains in bash.output, (
            f"bash reason for '{case.name}' lost its explanation: {bash!r}"
        )
        assert case.reason_contains in python.output, (
            f"python reason for '{case.name}' lost its explanation: {python!r}"
        )


def test_golden_covers_every_rule_family() -> None:
    """Set equality against an independently written expectation, not
    membership of whatever happens to be in the table (re-review §5).  A rule
    family that loses its last golden case must fail here, loudly, rather than
    quietly stop being compared."""
    expected_families = {
        "pr-shape", "base-range", "evidence", "adr", "project-commands",
        "commit-hygiene", "test-immutability", "test-cmd-precedence",
        "untested", "push", "body-file",
    }
    family_of = {
        "good_pr": "pr-shape", "bad_base": "pr-shape", "no_base": "pr-shape",
        "missing_ac": "pr-shape", "missing_issue_link": "pr-shape",
        "web_flag": "pr-shape", "missing_ac_marker": "pr-shape",
        "failed_adversarial_verdict": "pr-shape",
        "base_advanced_pr": "base-range", "base_advanced_push": "base-range",
        "base_advanced_branch_still_checked": "base-range",
        "non_image_evidence": "evidence",
        "visual_ac_without_screenshot": "evidence",
        "visual_ac_with_read_screenshot": "evidence",
        "existing_test_weakened": "evidence",
        "existing_test_declared": "test-immutability",
        "existing_test_declaration_mismatch": "test-immutability",
        "existing_test_declaration_malformed": "test-immutability",
        "existing_test_dirty_declaration_ignored": "test-immutability",
        "adr_governed_path_uncited": "adr", "adr_governed_path_cited": "adr",
        "adr_governs_untouched_path": "adr",
        "lint_fails": "project-commands", "lint_passes": "project-commands",
        "typecheck_fails": "project-commands",
        "no_project_commands_configured": "project-commands",
        "commit_bad_prefix": "commit-hygiene", "green_without_red": "commit-hygiene",
        "operator_test_cmd_wins": "test-cmd-precedence",
        "operator_tautological_test_cmd_blocks": "test-cmd-precedence",
        "marker_used_when_operator_has_none": "test-cmd-precedence",
        "no_test_command": "untested", "no_test_command_with_opt_out": "untested",
        "push_without_open_pr": "push", "push_with_open_pr_failing_tests": "push",
        "push_with_open_pr_green": "push", "push_on_ungoverned_branch": "push",
        "body_file": "body-file", "body_file_missing_ac": "body-file",
    }
    assert {c.name for c in GOLDEN} == set(family_of), (
        "a golden case was added or removed without updating the coverage map — "
        "add it here so the family accounting stays honest"
    )
    assert set(family_of.values()) == expected_families
    # Every family carries at least one ALLOW and one DENY, so no family is
    # only ever exercised in the direction that cannot fail open.
    by_family: dict[str, set[bool]] = {}
    for case in GOLDEN:
        by_family.setdefault(family_of[case.name], set()).add(case.expect_allowed)
    single_sided = {f for f, verdicts in by_family.items() if len(verdicts) < 2}
    assert single_sided == {"commit-hygiene"}, (
        "every rule family needs an ALLOW case and a DENY case (the anti-vacuity "
        f"twin). Single-sided: {sorted(single_sided)}. 'commit-hygiene' is the one "
        "accepted exception — its ALLOW direction is covered by every other "
        "commit/pr case, all of which have clean commit logs."
    )


# ── ABSENCE tests (re-review §5): remove the handling, assert a loud failure ──
ABSENCE_CASES = ("adr_governed_path_uncited", "lint_fails", "typecheck_fails")


@pytest.mark.parametrize("name", ABSENCE_CASES)
def test_absence_of_ported_rule_wiring_breaks_parity(name: str, tmp_path: Path) -> None:
    """Delete the wiring that makes a ported rule live on the Python path and
    the parity assertion must FAIL.

    This is the test the P0 wave's mutation checks could not be: both P0
    regressions were ABSENCE bugs (a var no longer emitted, a failure no longer
    counted), invisible to "change the code, see a test fail". Here the code is
    not changed — the wiring is removed — and the suite must notice.

    Concretely `complete=False` reproduces `make_pretooluse_hook` without its
    `complete_gate_context(...)` call, which is exactly the state review M6
    found the SDK path in: the ADR rule and the lint/typecheck arm inert, while
    AGENT.md and doctor.sh advertised them."""
    case = next(c for c in GOLDEN if c.name == name)
    bash, python = _run_case(case, tmp_path, complete=False)
    assert bash.allowed is False, "fixture no longer exercises the rule"
    assert python.allowed is True, (
        f"'{name}' still denies on the Python path without the context "
        "completion — the wiring this test guards has moved; point the absence "
        "test at its new home rather than deleting it"
    )
    # …and that is precisely a parity failure.
    assert bash.allowed != python.allowed


# ── MUTATION tests: re-introduce the exact bug, assert the case flips ────────
def _mutated_checks(tmp_path: Path, replacements: dict[str, str]) -> Path:
    """A copy of plugin/hooks/checks/ with textual mutations applied. Each
    replacement must actually match — a mutation that silently no-ops would
    make the test pass for the wrong reason."""
    dest = tmp_path / "mutated-checks"
    shutil.copytree(CHECKS, dest)
    for path in sorted(dest.glob("*.sh")):
        text = path.read_text()
        for old, new in replacements.items():
            if old in text:
                text = text.replace(old, new)
                replacements = {k: v for k, v in replacements.items() if k != old}
        path.write_text(text)
    assert not replacements, f"mutation did not apply: {sorted(replacements)}"
    return dest


def test_mutation_three_dot_log_range_reintroduces_the_m6_bug(tmp_path: Path) -> None:
    """Put the three-dot `git log` range back and the base-advanced case must
    start FAILING on the bash side — the M6 [repro], reproduced mechanically.

    The mutation is one character (`..` → `...`) in the one place the review
    named, and it flips a PR that every other rule accepts from ALLOW to DENY
    with "lacks an allowed prefix" — over a commit the branch does not even
    contain."""
    mutated = _mutated_checks(tmp_path, {
        'log_range="$baseref..HEAD"': 'log_range="$baseref...HEAD"',
    })
    case = next(c for c in GOLDEN if c.name == "base_advanced_pr")
    bash, python = _run_case(case, tmp_path / "case", checks_dir=mutated)
    assert python.allowed is True, "the Python side was never affected by M6"
    assert bash.allowed is False, (
        "re-introducing the three-dot log range no longer breaks anything — the "
        "base-advanced fixture has stopped advancing the base"
    )
    assert "allowed prefix" in bash.output
    assert "Merge pull request" in bash.output


def test_mutation_marker_precedence_reintroduces_the_p1r18_drift(tmp_path: Path) -> None:
    """Restore the old `.harness/test-cmd`-wins precedence in the bash and the
    two stacks disagree — the drift this wave closed (P1R-18)."""
    mutated = _mutated_checks(tmp_path, {
        'local m=".harness/test-cmd" c="${TEST_CMD:-}"':
            'local m=".harness/test-cmd" c=""',
    })
    case = next(c for c in GOLDEN if c.name == "operator_tautological_test_cmd_blocks")
    bash, python = _run_case(case, tmp_path / "case", checks_dir=mutated)
    assert python.allowed is False, "the Python side must still prefer TEST_CMD"
    assert bash.allowed is True, (
        "the mutated bash no longer prefers the marker — the mutation missed"
    )


def test_mutation_dropping_the_bash_adr_rule_breaks_parity(tmp_path: Path) -> None:
    """The bash half of the ported rule is load-bearing too: deleting the §9
    guard makes the bash allow a PR the Python gate denies.  Porting a rule is
    only half the job — both halves stay tested."""
    mutated = _mutated_checks(tmp_path, {
        'if [ -d docs/adr ]; then': 'if false; then',
    })
    case = next(c for c in GOLDEN if c.name == "adr_governed_path_uncited")
    bash, python = _run_case(case, tmp_path / "case", checks_dir=mutated)
    assert bash.allowed is True
    assert python.allowed is False


# ── DIVERGENCE: differences that are real, deliberate, and therefore asserted ─
def test_divergence_push_gate_on_an_unreadable_pr_state(tmp_path: Path) -> None:
    """The one push-gate case where the stacks legitimately differ.

    When `gh pr list` FAILS (rate limit, auth blip), check-push.sh denies the
    push outright, while the Python gate (review P1R-12) runs the state-derived
    checks anyway and ALLOWS a clean push.  P1R-12's reasoning: denying on ERROR
    also denied the FIRST, pre-PR push, so `gh pr create` never happened and
    `max_resolve_retries` walked a healthy issue to a human — a worse failure
    than the one the fail-closed rule prevents.  Running the checks costs a test
    run; denying costs the issue.

    The decision was made for the Python path only.  Porting it to bash changes
    the codex path's fail-closed behaviour and is a separate call, so the
    difference is recorded here rather than papered over: if someone ports it,
    this test fails and tells them to move the row into GOLDEN."""
    repo = compliant_pr_repo(tmp_path)
    env = {"BASE_BRANCH": "dev", "TEST_CMD": "true", "GATE_GH_RETRY_SLEEP": "0"}

    failing_stub = repo / ".stub"
    failing_stub.mkdir(exist_ok=True)
    gh = failing_stub / "gh"
    gh.write_text("#!/usr/bin/env bash\nexit 1\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    bash_env = dict(env, PATH=f"{failing_stub}{os.pathsep}{os.environ.get('PATH', '')}")
    bash = bash_verdict("push", repo, ["push"], bash_env)

    def failing_gh(_args: list[str], _cwd: str) -> GitResult:
        return GitResult(1, "")

    def build_ctx(cwd: str):
        return build_gate_context(cwd, base_branch="dev", repo="owner/repo",
                                 env_test_cmd="true", gh=failing_gh)

    hook = governor.make_pretooluse_hook(build_ctx)
    with _environment(env):
        result = asyncio.run(
            hook({"tool_input": {"command": "git push"}, "cwd": str(repo)}, None, None)
        )

    assert bash.allowed is False and "could not verify PR state" in bash.output
    assert result == {}, "the Python push gate should allow a clean unverified push"


def test_divergence_commit_rule_order(tmp_path: Path) -> None:
    """Both stacks DENY a commit that violates ordering AND lint, but they name
    different rules, because the bash runs its two checkers as whole scripts.

    `bash_governor.py` runs check-commit.sh (prefix → lint → typecheck →
    red-at-commit) to completion and only then check-commit-order.sh, so lint is
    reported first.  The Python gate interleaves: prefix → ordering → lint →
    red-at-commit, which is the M21 order (the pure check before the subprocess
    one).  The VERDICT is identical, which is what the gate promises; the
    ordering cannot be unified without moving the ordering rule into
    check-commit.sh, and that file is the codex path's contract."""
    repo = _no_red_repo(tmp_path)
    env = {"BASE_BRANCH": "dev", "LINT_CMD": "false"}
    argv = ["commit", "-m", "green: impl"]
    bash = bash_verdict("commit", repo, argv, env)
    python = python_verdict("commit", repo, argv, env)
    assert bash.allowed is python.allowed is False
    assert "Lint failed" in bash.output
    assert "TDD ordering" in python.output


def test_bound_sh_dash_c_skips_both_gates(tmp_path: Path) -> None:
    """Every parity claim above is bounded by this: `sh -c 'git push …'`
    classifies as nothing, so NEITHER stack gates it (M9 / re-review R3-F8).

    The classification layer is shared code (`adapters/governor_policy`, which
    `plugin/hooks/bash_governor.py` imports), so this is not drift between the
    two gates — it is one hole under both.  Asserted, not assumed: if the
    classifier ever learns to see through `sh -c`, this test fails and the
    bound can be lifted from the module docstring.  The permanent record lives
    in tests/test_governor.py's strict-xfail block (wave P1-2 / C2d)."""
    repo = compliant_pr_repo(tmp_path)
    env = {"BASE_BRANCH": "dev", "TEST_CMD": "false"}

    direct = python_verdict("push", repo, ["push"], env, open_pr="7")
    assert direct.allowed is False, "the direct push must be gated (control)"

    def build_ctx(cwd: str):
        return build_gate_context(cwd, base_branch="dev", repo="owner/repo",
                                 env_test_cmd="false", gh=fake_gh_runner("7"))

    hook = governor.make_pretooluse_hook(build_ctx)
    with _environment(env):
        result = asyncio.run(hook(
            {"tool_input": {"command": "sh -c 'git push origin fix/7-token'"},
             "cwd": str(repo)},
            None, None,
        ))
    assert result == {}, (
        "sh -c is now classified — good news; update the module docstring's "
        "bound and this test"
    )


# ── doctor: the operator-facing claims this wave had to make true ────────────
DOCTOR_SH = ROOT / "bin" / "doctor.sh"
SNAPSHOT_SECTION = "== config snapshot (config/.active.<name>.env) =="

ACTIVE_HEADER = (
    "# AUTO-GENERATED by bin/compose-env.sh from shared.env + repos/demo.env "
    "— do not edit.\n"
)


def _fake_tools(tmp_path: Path) -> Path:
    """Stand-ins for the external tools doctor shells out to, so these tests are
    hermetic (no gh auth, no network, no tree-sitter resolve) and fast. Same
    technique tests/adapters/test_compose_env.py already uses on doctor."""
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    scripts = {
        "gh": ('#!/usr/bin/env bash\n'
               'if [ "$1" = "--version" ]; then echo "gh 0-fake"; exit 0; fi\n'
               'if [ "$1" = "api" ]; then echo "fake-user"; exit 0; fi\n'
               'exit 0\n'),
        "claude": '#!/usr/bin/env bash\necho "claude 0-fake"\n',
        "jq": '#!/usr/bin/env bash\necho "jq 0-fake"\n',
        # doctor's governor self-test asserts rc=2 (blocked).
        "uv": ('#!/usr/bin/env bash\n'
               'if [ "$1" = "--version" ]; then echo "uv 0-fake"; exit 0; fi\n'
               'exit 2\n'),
    }
    for name, body in scripts.items():
        path = fake_bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin


def _doctor_snapshot_section(tmp_path: Path, shared: str, overlay: str, active: str) -> str:
    """Run the REAL bin/doctor.sh against a scratch harness tree and return just
    its config-snapshot section (doctor reports many unrelated things; asserting
    on the whole output would couple these tests to all of them)."""
    root = tmp_path / "harness"
    (root / "config" / "repos").mkdir(parents=True)
    (root / "config" / "shared.env").write_text(shared)
    (root / "config" / "repos" / "demo.env").write_text(overlay)
    (root / "config" / ".active.demo.env").write_text(active)

    env = {k: v for k, v in os.environ.items() if k not in GATE_ENV_KNOBS}
    env["HARNESS_ROOT"] = str(root)
    env["AGENT_ENV"] = str(root / "config" / ".active.demo.env")
    env["PATH"] = f"{_fake_tools(tmp_path)}{os.pathsep}{env.get('PATH', '')}"
    proc = subprocess.run(
        ["bash", str(DOCTOR_SH)], env=env, capture_output=True, text=True, timeout=60,
    )
    out = proc.stdout
    assert SNAPSHOT_SECTION in out, f"doctor lost its snapshot section:\n{out}"
    tail = out.split(SNAPSHOT_SECTION, 1)[1]
    return tail.split("\n== ", 1)[0]


def test_doctor_flags_a_stale_active_env(tmp_path: Path) -> None:
    """A machine whose `.active.env` was composed by the pre-P0R-1 compose-env
    keeps a BROKEN snapshot until someone re-runs point-at, and nothing told the
    operator.  P0R-1's symptom: `TEST_CMD` silently omitted ⇒ the gate resolves
    no test command ⇒ every push and every `gh pr create` denied, forever,
    across reboots (install-systemd.sh copies the file).

    doctor now recomposes the two source files in a subshell and compares the
    variable NAMES against the snapshot — an independently-derived expectation,
    not a hand-maintained list. It is an ERROR (✗), not a warning: the failure it
    predicts is total and silent, and the fix is one command."""
    section = _doctor_snapshot_section(
        tmp_path,
        shared='REPO="o/r"\nFIX_BOT="a"\n',
        overlay='TEST_CMD="make test"\nCI_IGNORE_CHECKS="lint"\n',
        active=ACTIVE_HEADER + "REPO=o/r\nFIX_BOT=a\nCI_IGNORE_CHECKS=lint\n",
    )
    assert "✗" in section, f"staleness must be an error, not a warning:\n{section}"
    assert "TEST_CMD" in section, "the message must name the omitted variable"
    assert "point-at" in section, "the message must say how to fix it"


def test_doctor_accepts_a_fresh_active_env(tmp_path: Path) -> None:
    """Anti-vacuity twin: a snapshot carrying every assigned name passes, so the
    check above detects staleness rather than always complaining."""
    section = _doctor_snapshot_section(
        tmp_path,
        shared='REPO="o/r"\nFIX_BOT="a"\n',
        overlay='TEST_CMD="make test"\nCI_IGNORE_CHECKS="lint"\n',
        active=ACTIVE_HEADER
        + "REPO=o/r\nFIX_BOT=a\nTEST_CMD='make test'\nCI_IGNORE_CHECKS=lint\n",
    )
    assert "✗" not in section, section
    assert "matches config/shared.env + repos/demo.env" in section


def test_doctor_ignores_a_name_the_overlay_unsets(tmp_path: Path) -> None:
    """An overlay that ASSIGNS then `unset`s a name legitimately leaves it out
    of the snapshot (re-review R2-11).  The expectation is derived by SOURCING
    the files, so it sees the unset and must not cry stale — the first false
    positive is what teaches operators to ignore a check."""
    section = _doctor_snapshot_section(
        tmp_path,
        shared='REPO="o/r"\nFIX_BOT="a"\n',
        overlay='TEST_CMD="make test"\nunset TEST_CMD\n',
        active=ACTIVE_HEADER + "REPO=o/r\nFIX_BOT=a\n",
    )
    assert "✗" not in section, section
    assert "matches config/shared.env + repos/demo.env" in section


def test_doctor_survives_a_hand_edited_active_env(tmp_path: Path) -> None:
    """No AUTO-GENERATED header ⇒ the check cannot know which overlay composed
    the file. It must WARN and move on, never guess an overlay and then report a
    confident, wrong staleness verdict."""
    section = _doctor_snapshot_section(
        tmp_path,
        shared='REPO="o/r"\n',
        overlay='TEST_CMD="make test"\n',
        active="REPO=o/r\n",
    )
    assert "✗" not in section, section
    assert "⚠" in section and "skipping" in section


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
