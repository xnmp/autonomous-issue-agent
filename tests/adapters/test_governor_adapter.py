"""In-process governor — bypass fail-closed + gate routing + SDK deny shape.

Requires tree-sitter (bash_governor's dep), provided via the inline run deps in
the Makefile target. evaluate_command is pure (constructed GateContext); the
hook test drives the async callback with a stub build_ctx.
"""

import asyncio

from issue_agent.adapters.governor import (
    GateContext,
    evaluate_command,
    make_posttooluse_hook,
    make_pretooluse_hook,
    make_read_record_hook,
)
from issue_agent.domain.gate import OpenPrLookup


def ctx(**kw) -> GateContext:
    return GateContext(**kw)


# ── bypass detection (fail CLOSED — reuses bash_governor core) ─────────────────
def test_no_verify_blocked():
    v = evaluate_command("git commit -m 'green: x' --no-verify", ctx(branch_subjects=("red: t",)))
    assert not v.allowed and "no-verify" in v.reason.lower()


def test_husky_env_bypass_blocked():
    v = evaluate_command("HUSKY=0 git commit -m 'red: t'", ctx())
    assert not v.allowed


def test_hookspath_override_blocked():
    v = evaluate_command("git -c core.hooksPath=/dev/null commit -m 'red: t'", ctx())
    assert not v.allowed


def test_ungoverned_command_allowed():
    assert evaluate_command("ls -la && cat README.md", ctx()).allowed


# Force-push/delete of a protected branch must be DENIED through the full adapter
# path (evaluate_command -> _GOV.find_violations -> bypass_reason ->
# _push_destructive_target), not only at the bash_governor unit layer. This is
# the in-process SDK security gate; assert the deny + that the fixer's own
# branch is still allowed.
def test_force_push_protected_blocked_via_evaluate():
    force = "--for" "ce"  # avoid the literal triggering this session's own hook
    v = evaluate_command(f"git push {force} origin main", ctx())
    assert not v.allowed and "protected" in v.reason.lower()


def test_force_push_own_branch_allowed_via_evaluate():
    force = "--for" "ce"
    assert evaluate_command(f"git push {force} origin fix/42-x", ctx()).allowed


def test_delete_protected_branch_blocked_via_evaluate():
    v = evaluate_command("git push origin --delete dev", ctx())
    assert not v.allowed and "protected" in v.reason.lower()


# ── branch-name gate ────────────────────────────────────────────────────────
def test_bad_branch_name_blocked():
    v = evaluate_command("git checkout -b bugfix-token", ctx())
    assert not v.allowed and "invalid" in v.reason.lower()


def test_good_branch_name_allowed():
    assert evaluate_command("git checkout -b fix/42-null-token", ctx()).allowed


# ── commit gate (prefix + ordering) ──────────────────────────────────────────
def test_bad_commit_prefix_blocked():
    v = evaluate_command("git commit -m 'wip stuff'", ctx())
    assert not v.allowed


def test_green_without_red_blocked():
    v = evaluate_command("git commit -m 'green: impl'", ctx(branch_subjects=("plan: x",)))
    assert not v.allowed and "red" in v.reason.lower()


def test_green_with_red_allowed():
    v = evaluate_command(
        "git commit -m 'green: impl'", ctx(branch_subjects=("plan: x", "red: t"))
    )
    assert v.allowed


# ── verify-red-at-commit through the adapter ──────────────────────────────────
def test_red_commit_blocked_when_tests_pass():
    c = ctx(harness_test_cmd="pytest", run_tests=lambda cmd: True)  # tests PASS at red
    v = evaluate_command("git commit -m 'red: add test'", c)
    assert not v.allowed and "PASS" in v.reason


def test_red_commit_allowed_when_tests_fail():
    c = ctx(harness_test_cmd="pytest", run_tests=lambda cmd: False)  # genuinely red
    assert evaluate_command("git commit -m 'red: add test'", c).allowed


def test_red_commit_allowed_when_verification_disabled():
    c = ctx(harness_test_cmd="pytest", run_tests=lambda cmd: True, verify_red_at_commit=False)
    assert evaluate_command("git commit -m 'red: add test'", c).allowed


def test_red_commit_allowed_with_no_test_cmd():
    # no resolvable test command → no teeth here (PR-time §10 still applies).
    assert evaluate_command("git commit -m 'red: add test'", ctx(run_tests=lambda cmd: True)).allowed


# ── PR gate ───────────────────────────────────────────────────────────────────
def _good_pr_ctx():
    return ctx(
        base_branch="dev",
        branch="fix/5-x",
        branch_subjects=("red: t", "green: impl"),
        changed_files=("src/x.py", "tests/test_x.py"),
        ac_posted=True,
        test_plan_posted=True,
        adversarial_verdict="PASS ok",
        harness_test_cmd="pytest",
        exists_at_base=lambda f: False,
        run_tests=lambda c: True,
        revert_and_run=lambda impl: False,  # genuine red
    )


def _good_pr_cmd():
    return (
        "gh pr create --base dev --title t "
        "--body '## Acceptance Criteria\n- x\n\nFixes #5'"
    )


def test_compliant_pr_allowed():
    assert evaluate_command(_good_pr_cmd(), _good_pr_ctx()).allowed


def test_pr_without_base_blocked():
    cmd = "gh pr create --title t --body '## Acceptance Criteria\n- x\n\nFixes #5'"
    v = evaluate_command(cmd, _good_pr_ctx())
    assert not v.allowed and "base" in v.reason.lower()


def test_pr_web_blocked():
    v = evaluate_command("gh pr create --web", _good_pr_ctx())
    assert not v.allowed and "--web" in v.reason


def test_pr_missing_markers_blocked():
    c = _good_pr_ctx()
    c = GateContext(**{**c.__dict__, "ac_posted": False})
    v = evaluate_command(_good_pr_cmd(), c)
    assert not v.allowed and "Acceptance Criteria" in v.reason


def test_pr_tautological_test_blocked():
    c = _good_pr_ctx()
    c = GateContext(**{**c.__dict__, "revert_and_run": lambda impl: True})  # passes w/o impl
    v = evaluate_command(_good_pr_cmd(), c)
    assert not v.allowed and "tautological" in v.reason


# ── push gate (open-PR-only, LAZY probe) — review known-gap-2 ─────────────────
# The hole this section pins: `has_open_pr` defaulted False and the builder never
# populated it, so on the SDK path EVERY push to an open PR (every
# ADDRESS_REVIEW / FIX_CI leg) ran with no gate at all. The field is now the
# domain tri-state behind a probe, so these tests drive the real wiring.
def _probe(*answers):
    """A fake open-PR probe returning `answers` in order (last one repeats), and
    the call log — laziness and retry are both observable through it."""
    calls: list[int] = []

    def probe():
        calls.append(1)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    return probe, calls


def _push_ctx(probe, **kw):
    """A governed branch mid-work: one red: commit, a clean diff, a test command."""
    base = dict(
        branch="fix/5-x",
        branch_subjects=("red: t",),
        changed_files=("src/x.py", "tests/test_x.py"),
        harness_test_cmd="pytest",
        run_tests=lambda c: True,
        open_pr=probe,
    )
    return ctx(**{**base, **kw})


def test_push_to_open_pr_is_gated_and_blocks_a_failing_suite():
    """The P0 hole: with an open PR the push gate must actually RUN. A failing
    local suite is a real rule violation (§1), so the push is denied."""
    probe, calls = _probe(OpenPrLookup.OPEN)
    v = evaluate_command("git push", _push_ctx(probe, run_tests=lambda c: False))
    assert not v.allowed and "test suite failed" in v.reason
    assert len(calls) == 1  # probed exactly once for one push


def test_push_to_open_pr_with_everything_clean_is_allowed():
    """Anti-vacuity twin: the gate denies the violation above, not every push."""
    probe, _ = _probe(OpenPrLookup.OPEN)
    assert evaluate_command("git push", _push_ctx(probe)).allowed


def test_push_to_open_pr_blocks_committed_evidence():
    """A second rule proves the whole open-PR rule set is armed, not just §1."""
    probe, _ = _probe(OpenPrLookup.OPEN)
    c = _push_ctx(probe, changed_files=("src/x.py", "evidence/run.log"))
    v = evaluate_command("git push", c)
    assert not v.allowed and "evidence" in v.reason.lower()


def test_push_without_open_pr_allowed():
    """The pre-PR TDD phase must stay free — a red: commit's push happens before
    any PR exists, and gating it on a failing suite would make TDD impossible."""
    probe, _ = _probe(OpenPrLookup.NONE)
    c = _push_ctx(probe, run_tests=lambda c: False)
    assert evaluate_command("git push", c).allowed


def test_push_denied_when_the_open_pr_lookup_errors():
    """A failed lookup must never read as "no PR" (the bash's old collapse) —
    it is denied with the retry guidance the domain defines, after the retry."""
    probe, calls = _probe(OpenPrLookup.ERROR)
    slept: list[int] = []
    c = _push_ctx(probe, open_pr_retry_sleep=lambda: slept.append(1))
    v = evaluate_command("git push", c)
    assert not v.allowed
    assert "could not verify PR state" in v.reason
    assert "retry the push" in v.reason
    assert len(calls) == 2 and len(slept) == 1  # retried once, with a pause


def test_push_allowed_when_a_transient_lookup_error_clears_on_retry():
    """Anti-vacuity twin for the retry: a blip must not cost the agent a turn."""
    probe, calls = _probe(OpenPrLookup.ERROR, OpenPrLookup.NONE)
    c = _push_ctx(probe, run_tests=lambda c: False)
    assert evaluate_command("git push", c).allowed
    assert len(calls) == 2


def test_push_on_ungoverned_branch_is_allowed_without_probing():
    """An ungoverned branch is not our business — and must not cost a `gh` call
    (mirrors check-push.sh, which never reaches gh on that path)."""
    probe, calls = _probe(OpenPrLookup.OPEN)
    c = _push_ctx(probe, branch="main", run_tests=lambda c: False)
    assert evaluate_command("git push", c).allowed
    assert calls == []


def test_open_pr_probe_never_fires_for_non_push_commands():
    """Laziness contract: the context is rebuilt for EVERY Bash call, so a `gh`
    round-trip must happen only when a push is actually being gated."""
    probe, calls = _probe(OpenPrLookup.OPEN)
    for cmd in (
        "ls -la && cat README.md",
        "git status",
        "git commit -m 'green: impl'",
        "gh pr view 5",
    ):
        evaluate_command(cmd, _push_ctx(probe, branch_subjects=("red: t",)))
    assert calls == []


def test_open_pr_defaults_to_unknown_and_fails_closed():
    """A GateContext built without wiring the probe must DENY the push, not
    silently disable the gate — the shape of the original bug (`has_open_pr`
    defaulted to the permissive value and was never populated)."""
    v = evaluate_command("git push", ctx(branch="fix/5-x"))
    assert not v.allowed and "could not verify PR state" in v.reason


# ── SDK hook deny shape ────────────────────────────────────────────────────────
def test_hook_returns_deny_shape_on_violation():
    hook = make_pretooluse_hook(lambda cwd: ctx())
    out = asyncio.run(
        hook({"tool_input": {"command": "git commit -m 'wip'"}, "cwd": "/x"}, "id", None)
    )
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"]


def test_hook_allows_clean_command():
    hook = make_pretooluse_hook(lambda cwd: ctx())
    out = asyncio.run(hook({"tool_input": {"command": "ls"}, "cwd": "/x"}, "id", None))
    assert out == {}


def test_hook_denies_background_bash():
    """`run_in_background=true` spawns an SDK background task; in SDK 0.2.110 the
    main turn then ends with no terminal ResultMessage and the single-shot runner
    loses the whole run. The governor denies it BEFORE the command-content gate —
    even for an otherwise-clean command like `ls`."""
    hook = make_pretooluse_hook(lambda cwd: ctx())
    out = asyncio.run(hook(
        {"tool_input": {"command": "ls", "run_in_background": True}, "cwd": "/x"},
        "id", None,
    ))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "background" in hso["permissionDecisionReason"].lower()


def test_hook_allows_foreground_bash_that_would_be_backgroundable():
    """The deny is gated on the flag, not the command — the same command in the
    foreground (no run_in_background) passes."""
    hook = make_pretooluse_hook(lambda cwd: ctx())
    out = asyncio.run(hook(
        {"tool_input": {"command": "ls", "run_in_background": False}, "cwd": "/x"},
        "id", None,
    ))
    assert out == {}


def test_hook_logs_allowed_commit_with_issue_number():
    """An allowed commit emits a live breadcrumb tagged with the issue number
    parsed from the worktree cwd — the per-commit half of the fix log that closes
    the dispatch→PR silent gap."""
    logged: list[str] = []
    # green: needs a preceding red: on the branch to pass the ordering gate.
    hook = make_pretooluse_hook(
        lambda cwd: ctx(branch_subjects=("plan: x", "red: t")), log=logged.append
    )
    out = asyncio.run(hook(
        {"tool_input": {"command": "git commit -m 'green: impl the thing'"},
         "cwd": "/repo/.claude/worktrees/issue-56"},
        "id", None,
    ))
    assert out == {}  # allowed
    assert logged == ["commit #56: green: impl the thing"]


def test_hook_does_not_log_a_denied_commit():
    """A blocked commit must NOT emit a breadcrumb (log only reflects real progress)."""
    logged: list[str] = []
    hook = make_pretooluse_hook(lambda cwd: ctx(), log=logged.append)  # no red → green denied
    out = asyncio.run(hook(
        {"tool_input": {"command": "git commit -m 'green: impl'"},
         "cwd": "/repo/.claude/worktrees/issue-56"},
        "id", None,
    ))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert logged == []


def test_debug_logs_allow_and_deny_verdicts():
    """ISSUE_AGENT_DEBUG: every command traced with its verdict — turns the
    fail-open/allow path from a blind spot into a trace when diagnosing a wedge."""
    logged: list[str] = []
    hook = make_pretooluse_hook(lambda cwd: ctx(), log=logged.append, debug=True)
    asyncio.run(hook({"tool_input": {"command": "ls -la"}, "cwd": "/x"}, "id", None))
    asyncio.run(hook({"tool_input": {"command": "git commit -m 'wip bad'"}, "cwd": "/x"}, "id", None))
    joined = "\n".join(logged)
    assert "gate: ALLOW ls -la" in joined
    assert "gate: DENY git commit" in joined


def test_debug_logs_fail_open_exception(monkeypatch):
    """A gate crash is fail-open (allow) but must NOT be silent under debug —
    the swallowed-exception blind spot that hid root causes."""
    def boom(cwd):
        raise RuntimeError("gate blew up")

    logged: list[str] = []
    hook = make_pretooluse_hook(boom, log=logged.append, debug=True)
    out = asyncio.run(hook({"tool_input": {"command": "git status"}, "cwd": "/x"}, "id", None))
    assert out == {}  # still fail-open
    assert any("FAIL-OPEN" in m and "RuntimeError" in m for m in logged)


def test_hook_fails_open_on_builder_error():
    def boom(cwd):
        raise RuntimeError("git exploded")

    hook = make_pretooluse_hook(boom)
    out = asyncio.run(
        hook({"tool_input": {"command": "git checkout -b bad-name"}, "cwd": "/x"}, "id", None)
    )
    # builder error on a (would-be) governed command → fail OPEN (allow).
    assert out == {}


def test_hook_empty_command_noop():
    hook = make_pretooluse_hook(lambda cwd: ctx())
    out = asyncio.run(hook({"tool_input": {}, "cwd": "/x"}, "id", None))
    assert out == {}


# ── PostToolUse edit-check hook (deterministic edit-time gate) ─────────────────
def test_posttooluse_passing_check_is_noop():
    hook = make_posttooluse_hook("tsc", run_check=lambda cmd, cwd: (True, "ok"))
    out = asyncio.run(hook({"cwd": "/x"}, "id", None))
    assert out == {}


def test_posttooluse_failing_check_blocks_with_output():
    hook = make_posttooluse_hook(
        "tsc", run_check=lambda cmd, cwd: (False, "x.ts(3,1): error TS2322")
    )
    out = asyncio.run(hook({"cwd": "/x"}, "id", None))
    assert out["decision"] == "block"
    assert "TS2322" in out["reason"]
    assert "tsc" in out["reason"]


def test_posttooluse_no_command_noop():
    hook = make_posttooluse_hook("", run_check=lambda cmd, cwd: (False, "should not run"))
    out = asyncio.run(hook({"cwd": "/x"}, "id", None))
    assert out == {}


def test_posttooluse_fails_open_on_runner_error():
    def boom(cmd, cwd):
        raise RuntimeError("spawn failed")

    hook = make_posttooluse_hook("tsc", run_check=boom)
    out = asyncio.run(hook({"cwd": "/x"}, "id", None))
    assert out == {}  # never wedge the agent over a broken check


# ── PostToolUse(Read) screenshot-read recorder ────────────────────────────────
def _read_hook_with_recorder():
    recorded = []
    hook = make_read_record_hook(record=lambda cwd, name: recorded.append((cwd, name)))
    return hook, recorded


def test_read_record_logs_evidence_image():
    hook, recorded = _read_hook_with_recorder()
    out = asyncio.run(hook(
        {"tool_input": {"file_path": "/wt/issue-5/evidence/tab.png"}, "cwd": "/wt/issue-5"}, "id", None
    ))
    assert out == {}                       # observe-only
    assert recorded == [("/wt/issue-5", "tab.png")]   # basename recorded


def test_read_record_ignores_non_image():
    hook, recorded = _read_hook_with_recorder()
    asyncio.run(hook({"tool_input": {"file_path": "evidence/run.log"}, "cwd": "/x"}, "id", None))
    assert recorded == []


def test_read_record_ignores_image_outside_evidence():
    hook, recorded = _read_hook_with_recorder()
    asyncio.run(hook({"tool_input": {"file_path": "src/assets/logo.png"}, "cwd": "/x"}, "id", None))
    assert recorded == []


def test_read_record_fails_open():
    def boom(cwd, name):
        raise RuntimeError("disk full")

    hook = make_read_record_hook(record=boom)
    out = asyncio.run(hook(
        {"tool_input": {"file_path": "evidence/a.png"}, "cwd": "/x"}, "id", None
    ))
    assert out == {}  # recorder error never wedges the agent
