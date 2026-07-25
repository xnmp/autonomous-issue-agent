"""Gate-rule behaviour (Phase 1) — pure ports of check-*.sh + _pr_common.sh."""

from issue_agent.domain.gate import (
    OpenPrLookup,
    RedFailInputs,
    check_adversarial_verdict,
    check_branch_name,
    check_commit_log,
    check_commit_message,
    commit_phase,
    check_commit_order,
    check_markers,
    check_no_evidence,
    check_no_web,
    check_visual_evidence,
    check_pr_base,
    check_pr_body,
    check_push_precondition,
    check_red_fails,
    check_red_is_red,
    check_required_test,
    check_tests,
    check_tests_immutable,
    gate_is_toothless,
    partition_changed,
    push_gate_applies,
    resolve_open_pr,
    resolve_test_cmd,
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
    assert check_red_is_red("test(red): t", "pytest", lambda _c: True).ok is False
    assert check_red_is_red("test(red): t", "pytest", lambda _c: False).ok is True


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


def test_existing_test_must_not_change():
    existed = {"tests/test_app.py"}
    res = check_tests_immutable(["tests/test_app.py"], lambda f: f in existed)
    assert not res.ok
    # a NEW test file (not at base) is fine
    assert check_tests_immutable(["tests/test_new.py"], lambda f: f in existed).ok


# ── markers + adversarial ─────────────────────────────────────────────────────
def test_markers_required():
    assert not check_markers(False, True).ok
    assert not check_markers(True, False).ok
    assert check_markers(True, True).ok


def test_adversarial_verdict_must_pass():
    assert check_adversarial_verdict("PASS looks good").ok
    assert not check_adversarial_verdict("FAIL tautological").ok
    assert not check_adversarial_verdict("").ok


# ── test command resolution + §1 ──────────────────────────────────────────────
def test_resolve_prefers_harness_cmd():
    assert resolve_test_cmd("pytest tests/test_x.py", "pytest") == "pytest tests/test_x.py"


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
    res = check_red_fails(inp, revert_and_run=lambda impl: True)
    assert not res.ok and "tautological" in res.reason


def test_red_fail_allows_genuine_red():
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",))
    assert check_red_fails(inp, revert_and_run=lambda impl: False).ok


def test_red_fail_skips_without_new_tests():
    inp = RedFailInputs(test_cmd="pytest", new_test_files=(), impl_files=("src/x.py",))
    # revert_and_run must NOT even be consulted → pass a poisoned callable.
    def boom(impl):
        raise AssertionError("should not run")
    assert check_red_fails(inp, revert_and_run=boom).ok


def test_red_fail_skips_pure_test_diff():
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",), impl_files=())
    assert check_red_fails(inp, revert_and_run=lambda impl: True).ok


def test_red_fail_skips_when_explicitly_disabled():
    base = dict(new_test_files=("tests/test_x.py",), impl_files=("src/x.py",))
    assert check_red_fails(
        RedFailInputs(test_cmd="pytest", red_must_fail=False, **base), lambda i: True
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
        RedFailInputs(test_cmd="", allow_untested=True, **base), lambda i: True
    ).ok


def test_red_fail_opt_out_does_not_excuse_a_tautological_test():
    # ALLOW_UNTESTED only covers a MISSING command; with one present the revert
    # simulation still runs and still blocks a test that passes without the impl.
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",), allow_untested=True)
    assert not check_red_fails(inp, revert_and_run=lambda impl: True).ok


def test_red_fail_fails_open_on_unresolvable_base():
    inp = RedFailInputs(test_cmd="pytest", new_test_files=("tests/test_x.py",),
                        impl_files=("src/x.py",), base_resolvable=False)
    assert check_red_fails(inp, revert_and_run=lambda impl: True).ok


# ── verify-red-at-commit (fail-fast on the red commit) ────────────────────────
def test_red_is_red_only_fires_on_red_commit():
    # a green:/other commit is never subject to this check, even if tests pass.
    assert check_red_is_red("green: impl", "pytest", lambda c: True).ok
    assert check_red_is_red("plan: types", "pytest", lambda c: True).ok


def test_red_is_red_blocks_passing_red_commit():
    res = check_red_is_red("red: add failing test", "pytest", lambda c: True)
    assert not res.ok and "not" in res.reason.lower()


def test_red_is_red_allows_failing_red_commit():
    assert check_red_is_red("red: add failing test", "pytest", lambda c: False).ok


def test_red_is_red_skips_when_no_cmd_or_disabled():
    assert check_red_is_red("red: t", "", lambda c: True).ok            # no test cmd
    assert check_red_is_red("red: t", "pytest", lambda c: True, enabled=False).ok


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


def test_push_denied_when_pr_state_unknown():
    # The fail-open hole: a failed gh lookup used to read as "no PR" and skip the
    # ENTIRE push gate. An unknown state must block instead.
    res = check_push_precondition("fix/7-token", OpenPrLookup.ERROR)
    assert not res.ok
    assert "retry" in res.reason.lower()          # tells the agent what to do next
    assert "fix/7-token" in res.reason


def test_push_allowed_when_lookup_succeeds():
    assert check_push_precondition("fix/7-token", OpenPrLookup.OPEN).ok
    assert check_push_precondition("fix/7-token", OpenPrLookup.NONE).ok


def test_push_lookup_error_ignored_on_ungoverned_branch():
    # An ungoverned branch is never gated (the bash exits before it even calls
    # gh), so an unknown PR state there is irrelevant — don't wedge it.
    assert check_push_precondition("scratch-branch", OpenPrLookup.ERROR).ok
    assert check_push_precondition("", OpenPrLookup.ERROR).ok


def test_push_gate_enforced_only_for_governed_branch_with_open_pr():
    assert push_gate_applies("fix/7-token", OpenPrLookup.OPEN) is True
    assert push_gate_applies("fix/7-token", OpenPrLookup.NONE) is False   # pre-PR push
    assert push_gate_applies("main", OpenPrLookup.OPEN) is False
    # ERROR never enforces the shared checks — check_push_precondition denies first.
    assert push_gate_applies("fix/7-token", OpenPrLookup.ERROR) is False
