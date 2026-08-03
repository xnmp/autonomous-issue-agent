"""Gate-rule behaviour (Phase 1) — pure ports of check-*.sh + _pr_common.sh."""

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
    check_visual_evidence,
    count_ac_items,
    referenced_evidence_images,
    check_pr_base,
    check_pr_body,
    check_push_precondition,
    check_push_target,
    check_red_fails,
    check_red_is_red,
    check_required_test,
    check_tests,
    check_tests_immutable,
    gate_is_toothless,
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


# ── PR base + body ──────────────────────────────────────────────────────────
def test_pr_base_must_be_explicit_and_match():
    assert not check_pr_base("", "dev").ok
    assert not check_pr_base("main", "dev").ok
    assert check_pr_base("dev", "dev").ok


def test_pr_body_needs_ac_and_link():
    assert not check_pr_body("no sections here").ok
    assert not check_pr_body("## Acceptance Criteria\n- x").ok  # no closing kw
    assert check_pr_body("## Acceptance Criteria\n- x\n\nFixes #12").ok


def test_no_web_blocks_browser_pr():
    assert not check_no_web(["gh", "pr", "create", "--web"]).ok
    assert check_no_web(["gh", "pr", "create", "--base", "dev"]).ok


# ── required test in diff ─────────────────────────────────────────────────────
def test_fix_branch_requires_regression_test():
    assert not check_required_test("fix/1-x", ["src/app.py"]).ok
    assert check_required_test("fix/1-x", ["src/app.py", "tests/test_app.py"]).ok


def test_feat_branch_requires_test():
    assert not check_required_test("feat/1-x", ["src/app.ts"]).ok
    assert check_required_test("feat/1-x", ["src/app.ts", "test_app.py"]).ok


def test_chore_branch_needs_no_test():
    assert check_required_test("chore/1-x", ["package.json"]).ok


# ── evidence + test immutability ──────────────────────────────────────────────
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


# ── visual evidence (screenshot captured + read) ──────────────────────────────
def test_visual_evidence_skipped_when_not_visual():
    # non-visual issue (the common case): no requirement at all.
    assert check_visual_evidence(False, ["src/x.py"], []).ok


def test_visual_evidence_requires_a_committed_screenshot():
    res = check_visual_evidence(True, ["src/x.ts"], [])
    assert not res.ok and "no screenshot" in res.reason


def test_visual_evidence_requires_the_screenshot_be_read():
    res = check_visual_evidence(True, ["evidence/tab.png"], [])  # committed but unread
    assert not res.ok and "never read" in res.reason


def test_visual_evidence_passes_when_committed_and_read():
    assert check_visual_evidence(True, ["evidence/tab.png"], ["tab.png"]).ok


def test_visual_evidence_all_images_must_be_read():
    # one read, one not → blocked, names the unread one.
    res = check_visual_evidence(True, ["evidence/a.png", "evidence/b.png"], ["a.png"])
    assert not res.ok and "b.png" in res.reason


# ── receipt-less runtimes (codex, #153): reviewer is the sole verifier ────────
def test_visual_evidence_receiptless_runtime_skips_the_read_check():
    # No receipts minted → committed-but-unread evidence PASSES the fixer gate;
    # the independent review leg is the content authority (keifu #142/#156 —
    # denying here manufactured deterministic dead-ends).
    assert check_visual_evidence(
        True, ["evidence/tab.png"], [], receipt_available=False
    ).ok


def test_visual_evidence_receiptless_runtime_keeps_the_committed_floor():
    # Loosening the READ check must not loosen the COMMIT floor.
    res = check_visual_evidence(True, ["src/x.ts"], [], receipt_available=False)
    assert not res.ok and "no screenshot" in res.reason


def test_visual_evidence_unreadable_receipt_only_matters_when_minted():
    # receipt_unreadable describes a failed read of a receipt the runtime DOES
    # mint; on a receipt-less runtime the flag is meaningless and must not deny.
    denied = check_visual_evidence(
        True, ["evidence/tab.png"], [],
        receipt_available=True, receipt_unreadable=True,
    )
    assert not denied.ok and "unavailable" in denied.reason
    assert check_visual_evidence(
        True, ["evidence/tab.png"], [],
        receipt_available=False, receipt_unreadable=True,
    ).ok


# ── byte-identical evidence (runtime-independent, #153 reuse gaming) ──────────
def test_visual_evidence_rejects_byte_identical_images():
    res = check_visual_evidence(
        True, ["evidence/a.png", "evidence/b.png"], ["a.png", "b.png"],
        image_digests={"evidence/a.png": "d1", "evidence/b.png": "d1"},
    )
    assert not res.ok and "byte-identical" in res.reason
    assert "evidence/a.png" in res.reason and "evidence/b.png" in res.reason


def test_visual_evidence_distinct_digests_pass():
    assert check_visual_evidence(
        True, ["evidence/a.png", "evidence/b.png"], ["a.png", "b.png"],
        image_digests={"evidence/a.png": "d1", "evidence/b.png": "d2"},
    ).ok


def test_visual_evidence_digest_rule_skipped_when_caller_could_not_digest():
    # None (I/O failure) and missing entries both fail-soft: the rule never
    # guesses, it only fires on two POSITIVELY matching digests.
    dupe = ["evidence/a.png", "evidence/b.png"]
    assert check_visual_evidence(True, dupe, ["a.png", "b.png"], image_digests=None).ok
    assert check_visual_evidence(
        True, dupe, ["a.png", "b.png"], image_digests={}
    ).ok
    assert check_visual_evidence(
        True, dupe, ["a.png", "b.png"],
        image_digests={"evidence/a.png": "", "evidence/b.png": ""},
    ).ok


def test_visual_evidence_byte_identical_applies_on_receiptless_runtimes_too():
    res = check_visual_evidence(
        True, ["evidence/a.png", "evidence/b.png"], [],
        receipt_available=False,
        image_digests={"evidence/a.png": "d1", "evidence/b.png": "d1"},
    )
    assert not res.ok and "byte-identical" in res.reason


# ── AC-item counting + one screenshot PER AC item (P3.6-lite) ─────────────────
def test_count_ac_items_counts_checklist_items_inside_the_section():
    body = (
        "- [ ] decoy above the heading\n"
        "## Acceptance Criteria\n"
        "- [ ] the tab title reads 'My App'\n"
        "* [x] the empty state shows the CTA\n"
        "  + [ ] indented items count too\n"
        "- a prose bullet is not a criterion checkbox\n"
        "### Test Plan\n"
        "- [ ] decoy after the section\n"
    )
    assert count_ac_items(body) == 3


def test_count_ac_items_is_zero_when_unparseable():
    # prose AC section (today's common body), no section at all, empty body.
    assert count_ac_items("## Acceptance Criteria\n- token works\n\nFixes #7") == 0
    assert count_ac_items("no sections here") == 0
    assert count_ac_items("") == 0


def test_count_ac_items_accepts_any_heading_level_and_case():
    assert count_ac_items("#### acceptance criteria (v2)\n- [ ] a\n- [x] b") == 2


def test_referenced_evidence_images_are_distinct_paths():
    body = (
        "![AC 1](evidence/ac-1-title.png)\n"
        "see evidence/ac-2-empty.PNG and [again](evidence/ac-1-title.png)\n"
        "evidence/notes.txt is not an image\n"
    )
    assert referenced_evidence_images(body) == (
        "evidence/ac-1-title.png",
        "evidence/ac-2-empty.PNG",
    )


_AC3 = "## Acceptance Criteria\n- [ ] one\n- [ ] two\n- [ ] three\n\nFixes #7\n"
_AC3_LINKED = _AC3 + (
    "![AC 1](evidence/ac-1.png)\n![AC 2](evidence/ac-2.png)\n![AC 3](evidence/ac-3.png)\n"
)


def test_visual_evidence_requires_one_screenshot_per_ac_item():
    res = check_visual_evidence(
        True,
        ["evidence/ac-1.png", "evidence/ac-2.png"],
        ["ac-1.png", "ac-2.png"],
        _AC3_LINKED,
    )
    assert not res.ok
    assert "3 AC items" in res.reason and "only 2" in res.reason


def test_visual_evidence_passes_with_one_read_screenshot_per_ac_item():
    assert check_visual_evidence(
        True,
        ["evidence/ac-1.png", "evidence/ac-2.png", "evidence/ac-3.png"],
        ["ac-1.png", "ac-2.png", "ac-3.png"],
        _AC3_LINKED,
    ).ok


def test_visual_evidence_requires_each_screenshot_be_referenced_in_the_body():
    # enough images, all read — but the body links only one, so the reviewer
    # cannot tell which shot proves which criterion.
    res = check_visual_evidence(
        True,
        ["evidence/ac-1.png", "evidence/ac-2.png", "evidence/ac-3.png"],
        ["ac-1.png", "ac-2.png", "ac-3.png"],
        _AC3 + "![only one](evidence/ac-1.png)\n",
    )
    assert not res.ok
    assert "references only 1" in res.reason


def test_visual_evidence_falls_back_to_the_floor_when_ac_unparseable():
    # prose AC section → 0 items → exactly today's rule: ONE read+committed shot,
    # no body reference required. A parse miss must never deny on its own.
    prose = "## Acceptance Criteria\n- the tab title is right\n\nFixes #7"
    assert check_visual_evidence(True, ["evidence/tab.png"], ["tab.png"], prose).ok
    res = check_visual_evidence(True, ["src/x.ts"], [], prose)
    assert not res.ok and "no screenshot" in res.reason


def test_visual_evidence_per_ac_rule_ignored_when_not_visual():
    # a non-visual PR with a 3-item checklist and no screenshots is untouched.
    assert check_visual_evidence(False, ["src/x.ts"], [], _AC3).ok


def test_visual_evidence_body_defaults_to_the_floor_rule():
    # caller without the body in hand keeps the pre-P3.6 behaviour.
    assert check_visual_evidence(True, ["evidence/tab.png"], ["tab.png"]).ok


def test_existing_test_must_not_change():
    existed = {"tests/test_app.py"}
    res = check_tests_immutable(["tests/test_app.py"], lambda f: f in existed)
    assert not res.ok
    # a NEW test file (not at base) is fine
    assert check_tests_immutable(["tests/test_new.py"], lambda f: f in existed).ok


# ── #124 exemption REQUEST path: a declared edit is let into the PR ──────────
def test_declared_existing_test_edit_passes_the_commit_gate():
    # The declaration makes the edit explicit; it authorizes nothing — the
    # MERGE gate (exemptions.exemption_permits_merge) withholds until granted.
    existed = {"tests/test_app.py"}
    assert check_tests_immutable(
        ["tests/test_app.py", "src/app.py"],
        lambda f: f in existed,
        declared=("tests/test_app.py",),
    ).ok


def test_undeclared_edit_still_denies_with_the_recovery_script():
    # Anti-regression for the keifu #126 message extension: the deny must keep
    # telling a benign agent HOW to comply (move tests out + restore the file).
    existed = {"tests/test_app.py", "tests/test_other.py"}
    res = check_tests_immutable(
        ["tests/test_other.py"],
        lambda f: f in existed,
        declared=("tests/test_app.py",),  # declares a DIFFERENT file
    )
    assert not res.ok
    assert "tests/test_other.py" in res.reason
    assert "move them to a new test file" in res.reason
    assert "git checkout <base>" in res.reason


def test_more_than_the_bound_declared_denies_regardless():
    existed = {f"tests/test_{i}.py" for i in range(4)}
    res = check_tests_immutable(
        sorted(existed),
        lambda f: f in existed,
        declared=tuple(sorted(existed)),  # 4 > MAX_TEST_IMMUTABILITY_EXCEPTIONS
    )
    assert not res.ok
    assert "exemption" in res.reason


def test_no_declaration_keeps_todays_behaviour_exactly():
    # Anti-vacuity: the default () must reproduce the pre-#124 results verbatim
    # (the shell twin and the parity goldens know nothing of declarations).
    existed = {"tests/test_app.py"}
    assert not check_tests_immutable(["tests/test_app.py"], lambda f: f in existed).ok
    assert check_tests_immutable(["src/app.py"], lambda f: f in existed).ok


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
def test_partition_splits_new_tests_from_impl():
    existed = {"src/app.py"}
    new_tests, impl = partition_changed(
        ["src/app.py", "tests/test_new.py"], lambda f: f in existed
    )
    assert new_tests == ("tests/test_new.py",)
    assert impl == ("src/app.py",)


def test_red_fail_blocks_tautological_test():
    # impl reverted but suite STILL passes → the test doesn't exercise the change.
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",))
    res = check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.PASSED)
    )
    assert not res.ok and "tautological" in res.reason


def test_red_fail_allows_genuine_red():
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",))
    assert check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.TEST_FAILED)
    ).ok


def test_red_fail_skips_without_new_tests():
    inp = RedFailInputs(test_cmd="pytest", new_test_files=(), impl_files=("src/x.py",))
    # revert_and_run must NOT even be consulted → pass a poisoned callable.
    def boom(impl):
        raise AssertionError("should not run")
    assert check_red_fails(inp, revert_and_run=boom).ok


def test_red_fail_skips_pure_test_diff():
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",), impl_files=())
    assert check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.PASSED)
    ).ok


def test_red_fail_skips_when_explicitly_disabled():
    base = dict(new_test_files=("tests/test_x.py",), impl_files=("src/x.py",))
    assert check_red_fails(
        RedFailInputs(test_cmd="pytest", red_must_fail=False, **base),
        lambda i: execution(ExecutionStatus.PASSED),
    ).ok


def test_red_fail_denies_when_no_test_command():
    # No command ⇒ the anti-tautology check never ran ⇒ deny (the #39 hole),
    # rather than silently allowing an unproven "red".
    base = dict(new_test_files=("tests/test_x.py",), impl_files=("src/x.py",))

    def boom(impl):
        raise AssertionError("must not attempt a revert with no test command")

    res = check_red_fails(RedFailInputs(test_cmd="", **base), boom)
    assert not res.ok and "no test command" in res.reason


def test_red_fail_no_command_allowed_with_explicit_opt_out():
    base = dict(new_test_files=("tests/test_x.py",), impl_files=("src/x.py",))
    assert check_red_fails(
        RedFailInputs(test_cmd="", allow_untested=True, **base),
        lambda i: execution(ExecutionStatus.PASSED),
    ).ok


def test_red_fail_opt_out_does_not_excuse_a_tautological_test():
    # ALLOW_UNTESTED only covers a MISSING command; with one present the revert
    # simulation still runs and still blocks a test that passes without the impl.
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",), allow_untested=True)
    assert not check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.PASSED)
    ).ok


def test_red_fail_denies_on_unresolvable_base():
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",), base_resolvable=False)
    assert not check_red_fails(
        inp, revert_and_run=lambda impl: execution(ExecutionStatus.PASSED)
    ).ok


def test_red_fail_rejects_timeout_and_infrastructure_failure_as_evidence():
    inp = RedFailInputs(
        test_cmd="pytest",
        new_test_files=("tests/test_x.py",),
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


def test_pr_body_denial_teaches_the_full_required_shape():
    """Both 2026-07-27 sessions hit this gate twice: a denial naming only the
    missing header invites a second bounce on the next requirement. The denial
    must carry the whole skeleton so ONE retry can fix the body."""
    missing_ac = check_pr_body("no sections here")
    assert "## Acceptance Criteria" in missing_ac.reason
    assert "Fixes #" in missing_ac.reason
    missing_kw = check_pr_body("## Acceptance Criteria\n- x")
    assert "Fixes #" in missing_kw.reason and "- [ ]" in missing_kw.reason


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
