"""Gate-rule behaviour (Phase 1) — pure ports of check-*.sh + _pr_common.sh."""

import pytest
from issue_agent.domain import gate

from issue_agent.domain.contracts import (
    TestExecution as ExecutionReceipt,
    TestExecutionStatus as ExecutionStatus,
)
from issue_agent.domain.gate import (
    OpenPrLookup,
    PushGateMode,
    RedFailInputs,
    check_branch_name,
    check_commit_log,
    check_commit_message,
    commit_phase,
    check_commit_order,
    check_no_evidence,
    check_no_harness_state,
    check_no_web,
    check_pr_base,
    check_push_precondition,
    check_push_target,
    check_red_fails,
    check_red_is_red,
    check_tests,
    gate_is_toothless,
    modified_existing_tests_from_diff,
    head_is_detached,
    partition_changed,
    push_gate_mode,
    resolve_open_pr,
    resolve_push_targets,
    resolve_test_cmd,
    unverified_push_denial,
)


def execution(status: ExecutionStatus) -> ExecutionReceipt:
    code = (
        0
        if status is ExecutionStatus.PASSED
        else 1
        if status in {ExecutionStatus.TEST_FAILED, ExecutionStatus.INFRA_ERROR}
        else None
    )
    return ExecutionReceipt(
        status,
        ("pytest",),
        code,
        1,
        "stdout",
        "stderr",
        "verifier",
        "deadbeef",
    )


# ── branch name ─────────────────────────────────────────────────────────────
def test_branch_name_valid():
    assert check_branch_name("fix/42-null-token").ok


def test_branch_name_invalid():
    assert not check_branch_name("bugfix-token").ok


def test_branch_name_empty_allows():
    assert check_branch_name("").ok


# ── commit message + ordering ─────────────────────────────────────────────────
def test_commit_prefix_required():
    assert check_commit_message("red: add failing test").ok
    assert not check_commit_message("misc changes").ok


def test_commit_empty_message_allowed():
    assert check_commit_message("").ok  # -F file; PR gate re-derives


def test_green_requires_prior_red():
    assert not check_commit_order("green: impl", ["plan: types"]).ok
    assert check_commit_order("green: impl", ["plan: types", "red: test"]).ok


def test_non_green_unconstrained():
    assert check_commit_order("red: test", []).ok


def test_green_allowed_when_base_unresolvable():
    assert check_commit_order("green: impl", [], base_resolvable=False).ok


def test_commit_log_rejects_green_before_red():
    assert not check_commit_log(["plan: x", "green: y", "red: z"]).ok


def test_commit_log_accepts_proper_order():
    assert check_commit_log(["plan: x", "red: y", "green: z"]).ok


# ── commitlint-safe scoped phase forms (test(red): / feat(green): …) ──────────
def test_commit_message_accepts_scoped_phase():
    assert check_commit_message("test(red): add failing test").ok
    assert check_commit_message("feat(green): implement").ok
    assert check_commit_message("docs(plan): outline").ok
    # a plain conventional commit (no phase scope) is still fine
    assert check_commit_message("fix(auth): tidy").ok
    # still rejects a non-prefixed subject
    assert not check_commit_message("misc changes").ok


def test_commit_phase_helper():
    assert commit_phase("red: x") == "red"
    assert commit_phase("test(red): x") == "red"
    assert commit_phase("feat(green): x") == "green"
    assert commit_phase("fix(auth): x") == ""       # a scope that isn't a phase
    assert commit_phase("chore: x") == ""


def test_green_requires_prior_red_scoped():
    # scoped green needs a prior red (bare or scoped)
    assert not check_commit_order("feat(green): impl", ["docs(plan): types"]).ok
    assert check_commit_order("feat(green): impl", ["test(red): t"]).ok
    # mixed forms interoperate
    assert check_commit_order("green: impl", ["test(red): t"]).ok


def test_commit_log_scoped_order():
    assert check_commit_log(["docs(plan): x", "test(red): y", "feat(green): z"]).ok
    assert not check_commit_log(["feat(green): y", "test(red): z"]).ok


def test_red_is_red_matches_scoped():
    # a scoped red commit still triggers the red-is-red suite check
    assert (
        check_red_is_red(
            "test(red): t",
            "pytest",
            lambda _c: execution(ExecutionStatus.PASSED),
        ).ok
        is False
    )
    assert (
        check_red_is_red(
            "test(red): t",
            "pytest",
            lambda _c: execution(ExecutionStatus.TEST_FAILED),
        ).ok
        is True
    )


def test_commit_log_rejects_bad_prefix():
    assert not check_commit_log(["wip: stuff"]).ok


# ── published history is exempt from the prefix rule (keifu #182) ─────────────
# GitHub's "Update branch" button writes an UNPREFIXED merge commit onto the PR
# branch. The publish broker then requires it as an ancestor, so a gate that
# denied its subject deadlocked the PR against the broker with no move the agent
# could make. A subject already on `base..origin/<branch>` is published history
# outside the agent's unilateral control, so the prefix rule skips it — once per
# published occurrence, and never at the cost of the TDD ordering rule.
UPDATE_BRANCH = "Merge branch 'chong-dev' into fix/130-caps-lock"


def test_commit_log_allows_a_subject_already_published_on_the_remote_branch():
    assert check_commit_log(
        ["red: t", UPDATE_BRANCH, "green: impl"], published=[UPDATE_BRANCH]
    ).ok


def test_commit_log_exempts_each_published_subject_only_once():
    # Two local copies of a subject published once: the second is un-exempt and
    # denies, so re-using a published subject verbatim smuggles at most one.
    assert not check_commit_log(
        ["red: t", UPDATE_BRANCH, UPDATE_BRANCH], published=[UPDATE_BRANCH]
    ).ok
    assert check_commit_log(
        ["red: t", UPDATE_BRANCH, UPDATE_BRANCH],
        published=[UPDATE_BRANCH, UPDATE_BRANCH],
    ).ok


def test_published_red_keeps_a_continuation_branch_green_legal():
    # A continuation branch whose red: is already pushed: the local log may or
    # may not still show it, and the green must be legal either way.
    assert check_commit_log(["green: pass"], published=["red: failing test"]).ok
    assert check_commit_log(
        ["red: failing test", "green: pass"], published=["red: failing test"]
    ).ok


def test_published_green_does_not_unlock_the_ordering_rule():
    # Only a red: relaxes ordering. A published green must not, or a branch that
    # ever pushed a green would be exempt from TDD ordering forever after.
    assert not check_commit_log(["green: new"], published=["green: old"]).ok
    assert not check_commit_log(
        ["green: new"], published=["green: old", UPDATE_BRANCH]
    ).ok


def test_commit_log_with_empty_published_behaves_exactly_as_before():
    assert check_commit_log(["plan: x", "red: y", "green: z"], published=()).ok
    assert not check_commit_log(["plan: x", "green: y", "red: z"], published=()).ok
    assert not check_commit_log(["wip: stuff"], published=()).ok
    assert not check_commit_log([UPDATE_BRANCH]).ok  # default: no exemption


def test_commit_log_tolerates_malformed_and_huge_inputs():
    assert check_commit_log([], published=["", "red: t"]).ok
    assert check_commit_log(["", "red: t", ""], published=["", ""]).ok
    # An empty published subject exempts nothing — it must never match a real one.
    assert not check_commit_log(["wip: stuff"], published=["", "", ""]).ok
    huge = tuple(f"Merge branch 'dev' into fix/130 ({n})" for n in range(1000))
    assert check_commit_log(huge, published=huge).ok
    assert not check_commit_log(("wip: unpublished", *huge), published=huge).ok


# ── PR base + body ──────────────────────────────────────────────────────────
def test_pr_base_must_be_explicit_and_match():
    assert not check_pr_base("", "dev").ok
    assert not check_pr_base("main", "dev").ok
    assert check_pr_base("dev", "dev").ok


def test_no_web_blocks_browser_pr():
    assert not check_no_web(["gh", "pr", "create", "--web"]).ok
    assert check_no_web(["gh", "pr", "create", "--base", "dev"]).ok


# ── evidence ────────────────────────────────────────────────────────────────
def test_evidence_non_image_not_committed():
    assert not check_no_evidence(["evidence/run.log"]).ok   # logs stay gitignored
    assert not check_no_evidence(["evidence/dump.bin"]).ok
    assert check_no_evidence(["src/x.py"]).ok


def test_evidence_image_may_be_committed():
    # screenshots ARE committed (they render in the PR's Files-changed tab).
    assert check_no_evidence(["evidence/shot.png"]).ok
    assert check_no_evidence(["evidence/ui/tab-title.PNG"]).ok
    assert check_no_evidence(["evidence/a.jpg", "src/x.ts"]).ok


def test_harness_control_files_are_never_committed():
    """Keifu #167→#156 (2026-08-01): a committed .harness/test-cmd merged into
    the base and retargeted the trusted red-fail gate for EVERY downstream
    worktree to another issue's single test — the gate then rejected an
    unrelated PR as tautological. Harness-local control state must never be
    repo content, in any diff."""
    assert not check_no_harness_state([".harness/test-cmd"]).ok
    assert not check_no_harness_state(["src/x.py", ".harness/visual-ac"]).ok
    assert not check_no_harness_state([".harness"]).ok
    assert check_no_harness_state(["src/x.py", "tests/x_test.py"]).ok
    # A target repo may legitimately have a similarly named NON-dot path.
    assert check_no_harness_state(["harness/README.md"]).ok
    assert check_no_harness_state(["docs/.harness-notes.md"]).ok


def _record(header: str, *body: str) -> str:
    """One `diff --git` record, as git actually renders it."""
    return "\n".join((f"diff --git {header}",) + body) + "\n"


# ── test command resolution + §1 ──────────────────────────────────────────────
# P1R-18: `check_tests` is the push gate's only real teeth and the GATED AGENT
# writes `.harness/test-cmd` (under bypassPermissions, with a governor that only
# inspects git/gh shapes). Preferring that file over the operator's TEST_CMD made
# `echo true > .harness/test-cmd` a one-line disarm of §1 AND §10.
def test_p1r_18_operator_test_cmd_wins_over_the_agent_written_marker():
    assert resolve_test_cmd("true", "make test") == "make test"
    assert resolve_test_cmd("pytest tests/test_x.py", "pytest") == "pytest"


def test_p1r_18_agent_marker_is_still_used_when_the_operator_set_none():
    """Anti-vacuity twin: the marker is not ignored — it is the whole answer for
    a repo where the operator has no TEST_CMD (what the discovery path writes it
    for), and a blank/whitespace operator value does not count as set."""
    assert resolve_test_cmd("pytest tests/test_x.py", None) == "pytest tests/test_x.py"
    assert resolve_test_cmd("pytest tests/test_x.py", "") == "pytest tests/test_x.py"
    assert resolve_test_cmd("pytest tests/test_x.py", "  \n ") == "pytest tests/test_x.py"


def test_resolve_falls_back_to_env():
    assert resolve_test_cmd(None, "make test") == "make test"
    assert resolve_test_cmd("  \n ", "make test") == "make test"


def test_resolve_empty_when_neither():
    assert resolve_test_cmd(None, None) == ""
    assert gate_is_toothless("") is True
    assert gate_is_toothless("pytest") is False


def test_check_tests_runs_command():
    assert check_tests("pytest", lambda c: True).ok
    assert not check_tests("pytest", lambda c: False).ok


def test_check_tests_denies_when_no_test_command():
    # "no test command" must NOT look like "tests passed": with nothing to run,
    # §1 and §10 are both toothless, so the gate fails CLOSED and says how to fix.
    res = check_tests("", lambda c: True)
    assert not res.ok
    assert ".harness/test-cmd" in res.reason and "ALLOW_UNTESTED" in res.reason


def test_check_tests_no_command_allowed_with_explicit_opt_out():
    # A genuinely test-less repo opts out deliberately (operator-set ALLOW_UNTESTED=1).
    assert check_tests("", lambda c: True, allow_untested=True).ok


def test_check_tests_opt_out_does_not_excuse_failing_tests():
    # The opt-out only covers the ABSENCE of a command — a command that runs and
    # fails is still a hard deny.
    assert not check_tests("pytest", lambda c: False, allow_untested=True).ok


# ── §10 red-fail (the anti-tautological-test gate) ─────────────────────────────
def test_partition_preserves_new_and_existing_tests():
    new_tests, impl = partition_changed(
        ["src/app.py", "tests/test_new.py", "tests/test_existing.py", ""]
    )
    assert new_tests == ("tests/test_new.py", "tests/test_existing.py")
    assert impl == ("src/app.py",)


@pytest.mark.parametrize("documentation", ["README.md", "guide.MD", "docs/tests.rst", "guide.adoc"])
def test_red_fail_allows_test_maintenance_with_documentation(documentation):
    tests, impl = partition_changed(["tests/test_existing.py", documentation])
    assert check_red_fails(
        RedFailInputs(test_cmd="pytest", test_files=tests, impl_files=impl),
        lambda _impl: execution(ExecutionStatus.PASSED),
    ).ok


@pytest.mark.parametrize("implementation", ["docs/example.py", "src/component.mdx"])
def test_red_fail_still_checks_executable_files_under_docs(implementation):
    tests, impl = partition_changed(["test_existing.py", implementation, "README.md"])
    assert not check_red_fails(
        RedFailInputs(test_cmd="pytest", test_files=tests, impl_files=impl),
        lambda _impl: execution(ExecutionStatus.PASSED),
    ).ok


def test_red_fail_blocks_tautological_test():
    # impl reverted but suite STILL passes → the test doesn't exercise the change.
    inp = RedFailInputs(test_cmd="pytest", test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",))
    res = check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.PASSED)
    )
    assert not res.ok and "tautological" in res.reason


def test_red_fail_allows_genuine_red():
    inp = RedFailInputs(test_cmd="pytest", test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",))
    assert check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.TEST_FAILED)
    ).ok


def test_red_fail_skips_without_new_tests():
    inp = RedFailInputs(test_cmd="pytest", test_files=(), impl_files=("src/x.py",))
    # revert_and_run must NOT even be consulted → pass a poisoned callable.
    def boom(impl):
        raise AssertionError("should not run")
    assert check_red_fails(inp, revert_and_run=boom).ok


def test_red_fail_skips_pure_test_diff():
    inp = RedFailInputs(test_cmd="pytest", test_files=("tests/test_x.py",), impl_files=())
    assert check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.PASSED)
    ).ok


def test_red_fail_skips_when_explicitly_disabled():
    base = dict(test_files=("tests/test_x.py",), impl_files=("src/x.py",))
    assert check_red_fails(
        RedFailInputs(test_cmd="pytest", red_must_fail=False, **base),
        lambda i: execution(ExecutionStatus.PASSED),
    ).ok


def test_red_fail_denies_when_no_test_command():
    # No command ⇒ the anti-tautology check never ran ⇒ deny (the #39 hole),
    # rather than silently allowing an unproven "red".
    base = dict(test_files=("tests/test_x.py",), impl_files=("src/x.py",))

    def boom(impl):
        raise AssertionError("must not attempt a revert with no test command")

    res = check_red_fails(RedFailInputs(test_cmd="", **base), boom)
    assert not res.ok and "no test command" in res.reason


def test_red_fail_no_command_allowed_with_explicit_opt_out():
    base = dict(test_files=("tests/test_x.py",), impl_files=("src/x.py",))
    assert check_red_fails(
        RedFailInputs(test_cmd="", allow_untested=True, **base),
        lambda i: execution(ExecutionStatus.PASSED),
    ).ok


def test_red_fail_opt_out_does_not_excuse_a_tautological_test():
    # ALLOW_UNTESTED only covers a MISSING command; with one present the revert
    # simulation still runs and still blocks a test that passes without the impl.
    inp = RedFailInputs(test_cmd="pytest", test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",), allow_untested=True)
    assert not check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.PASSED)
    ).ok


def test_red_fail_denies_on_unresolvable_base():
    inp = RedFailInputs(test_cmd="pytest", test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",), base_resolvable=False)
    assert not check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.PASSED)
    ).ok


def test_red_fail_rejects_timeout_and_infrastructure_failure_as_evidence():
    inp = RedFailInputs(
        test_cmd="pytest",
        test_files=("tests/test_x.py",),
        impl_files=("src/x.py",),
    )
    timeout = check_red_fails(
        inp, lambda _impl: execution(ExecutionStatus.TIMED_OUT)
    )
    infra = check_red_fails(
        inp, lambda _impl: execution(ExecutionStatus.INFRA_ERROR)
    )
    assert not timeout.ok and "timed out" in timeout.reason
    assert not infra.ok and "infrastructure" in infra.reason


# ── verify-red-at-commit (fail-fast on the red commit) ────────────────────────
def test_red_is_red_only_fires_on_red_commit():
    # a green:/other commit is never subject to this check, even if tests pass.
    passed = lambda _c: execution(ExecutionStatus.PASSED)
    assert check_red_is_red("green: impl", "pytest", passed).ok
    assert check_red_is_red("plan: types", "pytest", passed).ok


def test_red_is_red_blocks_passing_red_commit():
    res = check_red_is_red(
        "red: add failing test",
        "pytest",
        lambda _c: execution(ExecutionStatus.PASSED),
    )
    assert not res.ok and "not" in res.reason.lower()


def test_red_is_red_allows_failing_red_commit():
    assert check_red_is_red(
        "red: add failing test",
        "pytest",
        lambda _c: execution(ExecutionStatus.TEST_FAILED),
    ).ok


def test_red_is_red_rejects_non_test_failures():
    timeout = check_red_is_red(
        "red: add failing test",
        "pytest",
        lambda _c: execution(ExecutionStatus.TIMED_OUT),
    )
    infra = check_red_is_red(
        "red: add failing test",
        "pytest",
        lambda _c: execution(ExecutionStatus.INFRA_ERROR),
    )

    assert not timeout.ok and "timed out" in timeout.reason
    assert not infra.ok and "infrastructure" in infra.reason


def test_red_is_red_skips_when_no_cmd_or_disabled():
    passed = lambda _c: execution(ExecutionStatus.PASSED)
    assert check_red_is_red("red: t", "", passed).ok
    assert check_red_is_red(
        "red: t", "pytest", passed, enabled=False
    ).ok


# ── push-gate precondition: the open-PR lookup (check-push.sh) ────────────────
def _probe(*answers):
    """A probe returning the given answers in order (then repeating the last),
    recording how many times it was called."""
    calls = []

    def probe():
        calls.append(len(calls))
        return answers[min(len(calls) - 1, len(answers) - 1)]

    return probe, calls


def test_resolve_open_pr_returns_first_answer_without_retrying():
    probe, calls = _probe(OpenPrLookup.OPEN, OpenPrLookup.NONE)
    assert resolve_open_pr(probe) is OpenPrLookup.OPEN
    assert len(calls) == 1  # a good answer is never second-guessed


def test_resolve_open_pr_retries_a_transient_error():
    probe, calls = _probe(OpenPrLookup.ERROR, OpenPrLookup.OPEN)
    slept = []
    assert resolve_open_pr(probe, sleep=lambda: slept.append(1)) is OpenPrLookup.OPEN
    assert len(calls) == 2
    assert len(slept) == 1  # backoff happens BETWEEN attempts, not before the first


def test_resolve_open_pr_reports_error_when_retry_also_fails():
    probe, calls = _probe(OpenPrLookup.ERROR)
    assert resolve_open_pr(probe) is OpenPrLookup.ERROR
    assert len(calls) == 2  # exactly one retry, then give up


def test_resolve_open_pr_single_attempt_does_not_sleep():
    probe, calls = _probe(OpenPrLookup.ERROR)
    slept = []
    assert resolve_open_pr(probe, attempts=1, sleep=lambda: slept.append(1)) is OpenPrLookup.ERROR
    assert len(calls) == 1 and slept == []


def test_push_denied_when_pr_state_unknown_and_nothing_is_checkable():
    # The fail-open hole: a failed gh lookup used to read as "no PR" and skip the
    # ENTIRE push gate. When the state-derived checks cannot run either, there is
    # nothing left to verify, so an unknown state must block.
    res = check_push_precondition("fix/7-token", OpenPrLookup.ERROR, checks_runnable=False)
    assert not res.ok
    assert "retry" in res.reason.lower()          # tells the agent what to do next
    assert "fix/7-token" in res.reason


def test_push_allowed_when_lookup_succeeds():
    for runnable in (True, False):
        assert check_push_precondition("fix/7-token", OpenPrLookup.OPEN, runnable).ok
        assert check_push_precondition("fix/7-token", OpenPrLookup.NONE, runnable).ok


def test_push_lookup_error_ignored_on_ungoverned_branch():
    # An ungoverned branch is never gated (the bash exits before it even calls
    # gh), so an unknown PR state there is irrelevant — don't wedge it.
    assert check_push_precondition("scratch-branch", OpenPrLookup.ERROR, False).ok
    assert check_push_precondition("", OpenPrLookup.ERROR, False).ok


# ── P1R-12: the ERROR arm ran the agent's whole leg into the ground ───────────
# Denying EVERY push on a failed lookup also denied the first, pre-PR push, so
# `gh pr create` never happened and max_resolve_retries walked a healthy issue to
# a human. Unknown state now costs a test run, not the leg — while an
# unknown-state push that FAILS a rule is still denied.
def test_p1r_12_unknown_pr_state_still_runs_the_state_derived_checks():
    assert push_gate_mode("fix/7-token", OpenPrLookup.ERROR) is PushGateMode.UNVERIFIED


def test_p1r_12_unknown_pr_state_does_not_deny_on_the_precondition_alone():
    """Anti-vacuity twin: with the checks runnable, ERROR no longer denies by
    itself — the checks decide, so a CLEAN push during a rate limit goes through."""
    assert check_push_precondition("fix/7-token", OpenPrLookup.ERROR, checks_runnable=True).ok


def test_push_gate_mode_maps_the_tri_state_and_governedness():
    assert push_gate_mode("fix/7-token", OpenPrLookup.OPEN) is PushGateMode.ENFORCE
    assert push_gate_mode("fix/7-token", OpenPrLookup.NONE) is PushGateMode.SKIP  # pre-PR
    assert push_gate_mode("main", OpenPrLookup.OPEN) is PushGateMode.SKIP
    assert push_gate_mode("main", OpenPrLookup.ERROR) is PushGateMode.SKIP


def test_unverified_denial_names_the_rule_and_the_unknown_state():
    """An unverified denial must not read as 'the gate is offline' — the rule is
    real and fixing it unblocks the push even while the lookup stays down."""
    msg = unverified_push_denial("local test suite failed: pytest", "fix/7-token")
    assert "local test suite failed: pytest" in msg
    assert "fix/7-token" in msg and "lookup" in msg


# ── P1R-10: the gate must key on the ref being WRITTEN, not the checked-out one ──
def test_p1r_10_no_refspec_falls_back_to_the_checked_out_branch():
    assert resolve_push_targets((), "fix/42-x") == ("fix/42-x",)


def test_p1r_10_explicit_destination_wins_over_the_checked_out_branch():
    """`git push origin HEAD:other` writes `other`; checking `fix/42-x` was the bug."""
    assert resolve_push_targets(("other/thing",), "fix/42-x") == ("other/thing",)


def test_p1r_10_head_alias_destination_resolves_to_the_checked_out_branch():
    assert resolve_push_targets(("HEAD",), "fix/42-x") == ("fix/42-x",)
    assert resolve_push_targets(("@",), "fix/42-x") == ("fix/42-x",)


def test_p1r_10_detached_head_with_no_refspec_is_undeterminable():
    """A conflicted rebase leaves `rev-parse --abbrev-ref HEAD` == 'HEAD'; a
    failed rev-parse leaves ''. Neither is a branch, so the destination of a
    refspec-less push is unknown — and unknown denies, it does not skip."""
    for head in ("HEAD", "", "  "):
        assert resolve_push_targets((), head) is None
        assert resolve_push_targets(("HEAD",), head) is None
        assert not check_push_target(resolve_push_targets((), head)).ok


def test_p1r_10_detached_head_with_an_explicit_destination_is_determinable():
    """Anti-vacuity twin: the deny is about the UNKNOWN destination, not about
    being detached — naming the ref (what git demands anyway) is the way out."""
    targets = resolve_push_targets(("fix/42-x",), "HEAD")
    assert targets == ("fix/42-x",)
    assert check_push_target(targets).ok


def test_p1r_10_detached_head_denial_tells_the_agent_the_way_out():
    reason = check_push_target(None).reason
    assert "detached" in reason.lower()
    assert "HEAD:" in reason  # the exact refspec form that unblocks it


def test_p1r_10_multiple_destinations_are_deduped_in_order():
    assert resolve_push_targets(("a", "b", "a"), "fix/42-x") == ("a", "b")


def test_head_is_detached_only_for_non_branch_head_values():
    assert head_is_detached("HEAD") and head_is_detached("") and head_is_detached(" ")
    assert not head_is_detached("fix/42-x")
    assert not head_is_detached("dev")


def test_rust_test_files_are_recognized_as_tests():
    """Rust integration tests are bare .rs files under tests/ (each compiled
    as its own crate by cargo) — keifu #155's tests/help_popup_scroll_test.rs
    was rejected by the .py-only tests/ arm (2026-08-01)."""
    from issue_agent.domain.gate import looks_like_test

    assert looks_like_test("tests/help_popup_scroll_test.rs")
    assert looks_like_test("tests/scroll.rs")
    assert looks_like_test("src/app/mouse_test.rs")
    assert not looks_like_test("src/app/mouse.rs")
    assert not looks_like_test("tests/fixtures/data.json")


@pytest.mark.parametrize("blobs", [None, [], (("AGENTS.md", None),), (("AGENTS.md", "x" * 2_000_001),), (([], "text"),), (("AGENTS.md",),)])
def test_generated_instruction_missing_or_malformed_evidence_denies(blobs):
    assert not gate.check_generated_instructions(blobs).ok


@pytest.mark.parametrize("images,limit", [(None, 1), ((), True), ((("x.png", True),), 1), ((("x.png", None),), 1), ((("x.png", 2**64),), 1), (((),), 1)])
def test_invalid_image_facts_cannot_authorize_a_commit(images, limit):
    assert not gate.check_image_sizes(images, limit).ok


@pytest.mark.parametrize("record,path", [
    ("diff --git a/tests/test_old.py b/tests/test_old.py\n--- a/tests/test_old.py\n+++ b/tests/test_old.py\n", "tests/test_old.py"),
    ("diff --git a/tests/test_gone.py b/tests/test_gone.py\ndeleted file mode 100644\n", "tests/test_gone.py"),
    ("diff --git a/tests/test_old.py b/docs/moved.py\nrename from tests/test_old.py\nrename to docs/moved.py\n", "tests/test_old.py"),
    ("diff --git a/tests/test_binary.py b/tests/test_binary.py\nBinary files differ\n", "tests/test_binary.py"),
    ("diff --git a/tests/test_mode.py b/tests/test_mode.py\nold mode 100644\nnew mode 100755\n", "tests/test_mode.py"),
    ('diff --git "a/tests/test_caf\\303\\251.py" "b/tests/test_caf\\303\\251.py"\n', "tests/test_café.py"),
    ('diff --git "a/tests/test_caf\\303\\251.py" b/docs/moved.py\nrename from "tests/test_caf\\303\\251.py"\n', "tests/test_café.py"),
    ("diff --git a/tests/my test.py b/tests/my test.py\nBinary files differ\n", "tests/my test.py"),
    ("diff --git a/tests/@scope/test_old.py b/tests/@scope/test_old.py\n", "tests/@scope/test_old.py"),
])
def test_existing_test_listing_covers_git_record_shapes(record, path):
    assert modified_existing_tests_from_diff(record) == (path,)


@pytest.mark.parametrize("diff", [None, 42, b"diff --git a/tests/test_x.py b/tests/test_x.py", [], "", "not a diff", 'diff --git "unterminated\n'])
def test_existing_test_listing_tolerates_absent_and_malformed_diff(diff):
    assert modified_existing_tests_from_diff(diff) == ()


def test_existing_test_listing_excludes_new_files_and_source_and_deduplicates():
    diff = (
        "diff --git a/tests/test_new.py b/tests/test_new.py\nnew file mode 100644\n"
        "diff --git a/src/main.py b/src/main.py\n"
        "diff --git a/tests/test_b.py b/tests/test_b.py\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "diff --git a/tests/test_b.py b/tests/test_b.py\n"
    )
    assert modified_existing_tests_from_diff(diff) == ("tests/test_b.py", "tests/test_a.py")


def test_existing_test_listing_has_no_grant_scope_or_path_limits():
    paths = tuple(f"tests/@scope/{'x' * 250}/test_{n}.py" for n in range(1000))
    diff = "".join(f"diff --git a/{p} b/{p}\n" for p in paths)
    assert modified_existing_tests_from_diff(diff) == paths


@pytest.mark.parametrize("suffix", ["test.js", "spec.js", "test.jsx", "spec.ts", "test.tsx", "spec.mjs", "test.cjs", "spec.mts", "test.cts"])
def test_colocated_javascript_test_edits_are_prominent(suffix):
    path = f"src/widget.{suffix}"
    diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            "@@ -1 +1 @@\n-expect(render()).toEqual(42)\n+expect(true).toBe(true)\n")
    assert modified_existing_tests_from_diff(diff) == (path,)


@pytest.mark.parametrize("path", ["src/widget.ts", "src/widget.test.ts.map", "src/widget.spec.json"])
def test_colocated_test_detection_excludes_source_and_artifacts(path):
    assert modified_existing_tests_from_diff(f"diff --git a/{path} b/{path}\n") == ()
