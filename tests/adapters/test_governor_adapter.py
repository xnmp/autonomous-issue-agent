"""In-process governor — bypass fail-closed + gate routing + SDK deny shape.

Requires tree-sitter (bash_governor's dep), provided via the inline run deps in
the Makefile target. evaluate_command is pure (constructed GateContext); the
hook test drives the async callback with a stub build_ctx.
"""

import asyncio

import pytest

from issue_agent.domain import governor_policy
from issue_agent.adapters.governor import (
    _GATE_HANDLERS,
    GateContext,
    evaluate_command,
    make_posttooluse_hook,
    make_pretooluse_hook,
)


def test_gate_registry_covers_every_classified_kind():
    assert frozenset(_GATE_HANDLERS) == governor_policy.GOVERNED_KINDS
from issue_agent.domain.contracts import (
    TestExecution as ExecutionReceipt,
    TestExecutionStatus as ExecutionStatus,
)
from issue_agent.domain.gate import OpenPrLookup


def ctx(**kw) -> GateContext:
    return GateContext(**kw)


def execution(passed: bool) -> ExecutionReceipt:
    return ExecutionReceipt(
        ExecutionStatus.PASSED if passed else ExecutionStatus.TEST_FAILED,
        ("pytest",),
        0 if passed else 1,
        1,
        "stdout",
        "stderr",
        "verifier",
        "deadbeef",
    )


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


def test_policy_exception_denies_instead_of_failing_open(monkeypatch):
    def fail(_command):
        raise RuntimeError("injected parser failure")

    monkeypatch.setattr(governor_policy, "resolve", fail)

    verdict = evaluate_command("git push origin dev", ctx())

    assert not verdict.allowed
    assert "failed safely" in verdict.reason


def test_gh_api_input_mutation_is_blocked():
    verdict = evaluate_command(
        "gh api repos/o/r/issues/1 --input payload.json",
        ctx(),
    )

    assert not verdict.allowed
    assert "allowlist" in verdict.reason


def test_durable_publication_mode_routes_remote_writes_through_broker():
    push = evaluate_command(
        "git push origin HEAD:issue-42",
        ctx(
            durable_publish=True,
            branch="issue-42",
            open_pr=lambda _branch: OpenPrLookup.NO,
        ),
    )
    assert not push.allowed
    assert "issue-agent-publish git push" in push.reason
    assert evaluate_command(
        "issue-agent-publish git push origin HEAD:issue-42",
        ctx(
            durable_publish=True,
            branch="issue-42",
            open_pr=lambda _branch: OpenPrLookup.NO,
        ),
    ).allowed
    protected = evaluate_command(
        "issue-agent-publish git push --force origin HEAD:main",
        ctx(durable_publish=True),
    )
    assert not protected.allowed
    assert "protected branch" in protected.reason


@pytest.mark.parametrize(
    "command",
    (
        "ISSUE_AGENT_CONTROL_DB=/tmp/fake.db "
        "issue-agent-publish git push origin HEAD:issue-42",
        "env ISSUE_AGENT_REPOSITORY=attacker/other "
        "issue-agent-publish gh pr create --title Fix",
        "env ISSUE_AGENT_PUBLICATION_OWNER=attacker "
        "issue-agent-publish git push origin HEAD:issue-42",
    ),
)
def test_paid_child_cannot_override_publication_authority(command):
    verdict = evaluate_command(command, ctx(durable_publish=True))

    assert not verdict.allowed
    assert "publication authority cannot be overridden" in verdict.reason


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
    """The bypass layer must not block the legitimate rebase-and-repush of the
    fixer's own branch. Driven through a realistic push context (a governed
    destination now reaches the push gate — P1R-10 — so a bare GateContext would
    be denied by the gate's own rules, which is a different thing than the
    protected-branch bypass firing)."""
    force = "--for" "ce"
    probe, _ = _probe(OpenPrLookup.NONE)
    assert evaluate_command(f"git push {force} origin fix/42-x", _push_ctx(probe)).allowed


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
    c = ctx(
        harness_test_cmd="pytest",
        run_red_tests=lambda _cmd: execution(True),
    )
    v = evaluate_command("git commit -m 'red: add test'", c)
    assert not v.allowed and "PASS" in v.reason


def test_red_commit_allowed_when_tests_fail():
    c = ctx(
        harness_test_cmd="pytest",
        run_red_tests=lambda _cmd: execution(False),
    )
    assert evaluate_command("git commit -m 'red: add test'", c).allowed


def test_red_commit_allowed_when_verification_disabled():
    c = ctx(
        harness_test_cmd="pytest",
        run_red_tests=lambda _cmd: execution(True),
        verify_red_at_commit=False,
    )
    assert evaluate_command("git commit -m 'red: add test'", c).allowed


def test_red_commit_allowed_with_no_test_cmd():
    # no resolvable test command → no teeth here (PR-time §10 still applies).
    assert evaluate_command(
        "git commit -m 'red: add test'",
        ctx(run_red_tests=lambda _cmd: execution(True)),
    ).allowed


def test_red_commit_denied_when_test_execution_times_out():
    timeout = ExecutionReceipt(
        ExecutionStatus.TIMED_OUT,
        ("pytest",),
        None,
        1,
        "stdout",
        "stderr",
        "verifier",
        "deadbeef",
    )
    verdict = evaluate_command(
        "git commit -m 'red: add test'",
        ctx(
            harness_test_cmd="pytest",
            run_red_tests=lambda _cmd: timeout,
        ),
    )

    assert not verdict.allowed
    assert "timed out" in verdict.reason


# ── PR gate ───────────────────────────────────────────────────────────────────
def _good_pr_ctx():
    return ctx(
        base_branch="dev",
        branch="fix/5-x",
        branch_subjects=("red: t", "green: impl"),
        changed_files=("src/x.py", "tests/test_x.py"),
        harness_test_cmd="pytest",
        exists_at_base=lambda f: False,
        run_tests=lambda c: True,
        revert_and_run=lambda impl: execution(False),  # genuine red
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


def test_pr_gate_exempts_a_commit_already_published_on_the_remote_branch():
    """keifu #182: GitHub's Update-branch merge commit carries no allowed
    prefix, yet the publish broker requires it as an ancestor — the PR wedged
    between the two. Both halves are asserted: without the published fact the
    same branch is still denied, so the exemption is the wiring under test and
    not a rule that stopped firing."""
    update = "Merge branch 'dev' into fix/5-x"
    base = GateContext(**{
        **_good_pr_ctx().__dict__,
        "branch_subjects": ("red: t", update, "green: impl"),
    })

    denied = evaluate_command(_good_pr_cmd(), base)
    assert not denied.allowed and "allowed prefix" in denied.reason

    exempt = GateContext(**{**base.__dict__, "published_subjects": (update,)})
    assert evaluate_command(_good_pr_cmd(), exempt).allowed


def test_push_gate_exempts_a_commit_already_published_on_the_remote_branch():
    """The same fact must reach the PUSH gate: the agent has to push the
    continuation commit before the broker can merge anything."""
    update = "Merge branch 'dev' into fix/5-x"
    probe, _ = _probe(OpenPrLookup.OPEN)
    subjects = ("red: t", update)

    denied = evaluate_command("git push", _push_ctx(probe, branch_subjects=subjects))
    assert not denied.allowed and "allowed prefix" in denied.reason

    probe, _ = _probe(OpenPrLookup.OPEN)
    allowed = evaluate_command(
        "git push",
        _push_ctx(probe, branch_subjects=subjects, published_subjects=(update,)),
    )
    assert allowed.allowed


def test_self_authored_marker_fields_cannot_enter_gate_context():
    names = set(GateContext.__dataclass_fields__)
    assert names.isdisjoint(
        {"ac_posted", "test_plan_posted", "adversarial_verdict"}
    )


def test_pr_tautological_test_blocked():
    c = _good_pr_ctx()
    c = GateContext(**{
        **c.__dict__, "revert_and_run": lambda impl: execution(True)
    })  # passes w/o impl
    v = evaluate_command(_good_pr_cmd(), c)
    assert not v.allowed and "tautological" in v.reason


# ── PR gate ORDERING (review M21) ─────────────────────────────────────────────
# `_pr_gate` used to evaluate every rule eagerly and only ever report
# `reasons[0]` — so a trivial, cheap denial (e.g. the wrong --base) still paid
# for check_tests AND check_red_fails (both run the full TEST_CMD; the latter
# also mutates the worktree via revert_and_run) before the agent ever saw it,
# up to 2×GATE_TEST_TIMEOUT_S (600s default each). These pin the reorder:
# cheap structural checks first, short-circuiting on the first denial, so the
# suite-running checks are never even invoked once a cheap one has failed.
def _counting(return_value):
    """A fake callable that records how many times it was invoked — the
    "assert on the money" pattern: proving a check did NOT run is stronger
    than asserting on the verdict alone."""
    calls = {"n": 0}

    def fn(*_args, **_kwargs):
        calls["n"] += 1
        return return_value

    return fn, calls


def test_pr_gate_short_circuits_cheap_denial_before_running_expensive_checks():
    """A cheap, wrong-base denial must arrive WITHOUT ever running
    check_tests or check_red_fails."""
    run_tests, run_calls = _counting(True)
    revert_and_run, revert_calls = _counting(execution(False))
    c = GateContext(**{
        **_good_pr_ctx().__dict__,
        "run_tests": run_tests,
        "revert_and_run": revert_and_run,
    })
    cmd = (
        "gh pr create --base wrong-branch --title t "
        "--body '## Acceptance Criteria\n- x\n\nFixes #5'"
    )

    v = evaluate_command(cmd, c)

    assert not v.allowed and "base" in v.reason.lower()
    assert run_calls["n"] == 0      # check_tests: never invoked
    assert revert_calls["n"] == 0   # check_red_fails: never invoked (worktree untouched)


def test_pr_gate_runs_expensive_checks_when_nothing_cheap_denies():
    """Anti-vacuity twin: the short-circuit must not accidentally skip the
    suite-running checks on a genuinely compliant PR — both must actually run
    exactly once."""
    run_tests, run_calls = _counting(True)
    revert_and_run, revert_calls = _counting(execution(False))
    c = GateContext(**{
        **_good_pr_ctx().__dict__,
        "run_tests": run_tests,
        "revert_and_run": revert_and_run,
    })

    v = evaluate_command(_good_pr_cmd(), c)

    assert v.allowed
    assert run_calls["n"] == 1
    assert revert_calls["n"] == 1


# ── push gate (open-PR-only, LAZY probe) — review known-gap-2 ─────────────────
# The hole this section pins: `has_open_pr` defaulted False and the builder never
# populated it, so on the SDK path EVERY push to an open PR (every
# ADDRESS_REVIEW / FIX_CI leg) ran with no gate at all. The field is now the
# domain tri-state behind a probe, so these tests drive the real wiring.
def _probe(*answers):
    """A fake open-PR probe returning `answers` in order (last one repeats), and
    the call log — laziness, retry AND *which branch was asked about* are all
    observable through it (the last one is P1R-10's contract: the probe must be
    asked about the ref being written, not the one that happens to be checked
    out)."""
    calls: list[str] = []

    def probe(branch: str):
        calls.append(branch)
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


# ── P1R-12: an unknown PR state costs a test run, not the whole leg ───────────
# The ERROR arm used to deny EVERY push, including the very first pre-PR one, so
# during a rate limit `gh pr create` never happened and max_resolve_retries
# escalated a healthy issue to a human. The checks now run regardless of PR
# state — fail-closed where it matters (a rule violation is still denied),
# fail-workable where it does not (a clean push goes through).
def test_p1r_12_unknown_pr_state_denies_a_push_that_breaks_a_rule():
    probe, calls = _probe(OpenPrLookup.ERROR)
    slept: list[int] = []
    c = _push_ctx(probe, run_tests=lambda _c: False, open_pr_retry_sleep=lambda: slept.append(1))
    v = evaluate_command("git push", c)
    assert not v.allowed
    assert "local test suite failed" in v.reason      # the REAL rule it broke…
    assert "lookup for 'fix/5-x' failed" in v.reason  # …annotated as unverified
    assert len(calls) == 2 and len(slept) == 1        # retried once, with a pause


def test_p1r_12_unknown_pr_state_allows_a_clean_push():
    """The finding itself: a healthy pre-PR push during a GitHub rate limit must
    NOT be denied — that is what stalled `gh pr create` and burned the retries."""
    probe, calls = _probe(OpenPrLookup.ERROR)
    assert evaluate_command("git push", _push_ctx(probe)).allowed
    assert len(calls) == 2  # it still paid for the retry before giving up


def test_p1r_12_unknown_pr_state_denies_when_nothing_at_all_is_checkable():
    """The residual fail-closed arm: with the base unresolvable the state-derived
    checks are vacuous, so an unknown PR state leaves NOTHING verified → deny."""
    probe, _ = _probe(OpenPrLookup.ERROR)
    v = evaluate_command("git push", _push_ctx(probe, base_resolvable=False))
    assert not v.allowed and "could not verify PR state" in v.reason


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
    defaulted to the permissive value and was never populated). Post-P1R-12 the
    unknown state runs the checks instead of denying outright, and an unwired
    context has no test command either, so it is `check_tests` that denies —
    still loudly, still annotated as unverified."""
    v = evaluate_command("git push", ctx(branch="fix/5-x"))
    assert not v.allowed
    assert "no test command resolved" in v.reason
    assert "lookup for 'fix/5-x' failed" in v.reason


# ── P1R-10: the gate keys on the ref being WRITTEN, not the checked-out one ───
# The gate read `ctx.branch` (`git rev-parse --abbrev-ref HEAD`) for every push.
# Three consequences: a conflicted rebase makes that the literal string "HEAD"
# → ungoverned → the gate returned BEFORE probing, on exactly the leg whose
# prompt says "force-with-lease if you rebased"; `git push origin HEAD:other`
# was checked against the wrong branch; and the probe asked GitHub about a
# branch the push was not writing.
_REAL_PUSH_ARGVS = (
    "git push",
    "git push origin HEAD:refs/heads/fix/5-x",
    "git push --force-with-lease origin fix/5-x",
    "git push -u origin fix/5-x",
    "git push origin HEAD",
    "git push origin fix/5-x",
)


def test_p1r_10_every_real_push_shape_reaches_the_gate_on_its_destination():
    """All the argv shapes the fixer actually emits must gate, and must ask
    GitHub about the branch they WRITE (fix/5-x), not about `HEAD`."""
    for cmd in _REAL_PUSH_ARGVS:
        probe, calls = _probe(OpenPrLookup.OPEN)
        v = evaluate_command(cmd, _push_ctx(probe, run_tests=lambda _c: False))
        assert not v.allowed, cmd
        assert "test suite failed" in v.reason, cmd
        assert calls == ["fix/5-x"], (cmd, calls)


def test_p1r_10_every_real_push_shape_is_allowed_when_clean():
    """Anti-vacuity twin: the shapes above are gated, not blanket-denied."""
    for cmd in _REAL_PUSH_ARGVS:
        probe, _ = _probe(OpenPrLookup.OPEN)
        assert evaluate_command(cmd, _push_ctx(probe)).allowed, cmd


def test_p1r_10_conflicted_rebase_force_push_is_gated_not_skipped():
    """THE repro: mid-rebase `rev-parse --abbrev-ref HEAD` is the literal string
    'HEAD'. That read as an ungoverned branch and skipped the gate entirely on
    the one leg that force-pushes. The refspec names the destination, so the
    gate now applies — and probes the destination."""
    force = "--force-with-lease"
    probe, calls = _probe(OpenPrLookup.OPEN)
    c = _push_ctx(probe, branch="HEAD", run_tests=lambda _c: False)
    v = evaluate_command(f"git push {force} origin fix/5-x", c)
    assert not v.allowed and "test suite failed" in v.reason
    assert calls == ["fix/5-x"]


def test_p1r_10_conflicted_rebase_force_push_allowed_when_clean():
    """Anti-vacuity twin: gated, not blocked — a clean rebase-and-repush works."""
    force = "--force-with-lease"
    probe, _ = _probe(OpenPrLookup.OPEN)
    c = _push_ctx(probe, branch="HEAD")
    assert evaluate_command(f"git push {force} origin fix/5-x", c).allowed


def test_p1r_10_detached_head_without_a_refspec_is_denied_not_skipped():
    """Detached HEAD and no refspec: the gate cannot tell whether the destination
    is governed, so it cannot tell whether to enforce. Unknown denies (and git
    itself would reject this push anyway); the message names the way out."""
    probe, calls = _probe(OpenPrLookup.OPEN)
    for head in ("HEAD", ""):
        v = evaluate_command("git push", _push_ctx(probe, branch=head))
        assert not v.allowed
        assert "detached" in v.reason.lower() and "HEAD:" in v.reason
    assert calls == []  # undeterminable ⇒ never worth a gh round-trip


def test_p1r_10_push_to_a_non_pr_ref_is_not_this_gates_business():
    """The scope the destination rule buys: a push to a ref no PR points at
    cannot update a PR, so it is not gated — and costs no gh call. (Opening a PR
    from it still goes through the create gate.)"""
    probe, calls = _probe(OpenPrLookup.OPEN)
    c = _push_ctx(probe, run_tests=lambda _c: False)
    assert evaluate_command("git push origin HEAD:refs/heads/scratch", c).allowed
    assert calls == []


def test_p1r_10_governed_destination_from_an_ungoverned_head_is_gated():
    """The mirror image of the rebase case: HEAD sits on an ungoverned branch but
    the push writes a governed one. Keying on HEAD skipped it entirely."""
    probe, calls = _probe(OpenPrLookup.OPEN)
    c = _push_ctx(probe, branch="dev", run_tests=lambda _c: False)
    v = evaluate_command("git push origin HEAD:refs/heads/fix/42-x", c)
    assert not v.allowed and "test suite failed" in v.reason
    assert calls == ["fix/42-x"]


def test_absence_p1r_10_destination_derivation_is_load_bearing(monkeypatch):
    """§5's counter-measure: REMOVE the destination derivation and assert the
    bypass reopens, loudly. Without it the gate keys on the checked-out branch
    again — ungoverned `dev` — and a push that writes a governed branch with an
    open PR and a red suite sails through without even a probe."""
    argv = "git push origin HEAD:refs/heads/fix/42-x"

    probe, calls = _probe(OpenPrLookup.OPEN)
    c = _push_ctx(probe, branch="dev", run_tests=lambda _c: False)
    assert not evaluate_command(argv, c).allowed          # guard present: gated
    assert calls == ["fix/42-x"]

    monkeypatch.setattr(governor_policy, "push_destination_refs", lambda _argv: ())
    probe2, calls2 = _probe(OpenPrLookup.OPEN)
    c2 = _push_ctx(probe2, branch="dev", run_tests=lambda _c: False)
    assert evaluate_command(argv, c2).allowed             # guard gone: bypass back
    assert calls2 == []


def test_absence_p1r_10_derivation_removal_also_reopens_the_rebase_leg(monkeypatch):
    """Second arm of the same absence check, on the detached-HEAD repro: with the
    derivation removed the force-push mid-rebase is no longer checked against the
    branch it writes."""
    force = "--force-with-lease"
    argv = f"git push {force} origin fix/5-x"

    probe, calls = _probe(OpenPrLookup.OPEN)
    c = _push_ctx(probe, branch="HEAD", run_tests=lambda _c: False)
    v = evaluate_command(argv, c)
    assert not v.allowed and "test suite failed" in v.reason
    assert calls == ["fix/5-x"]

    monkeypatch.setattr(governor_policy, "push_destination_refs", lambda _argv: ())
    probe2, calls2 = _probe(OpenPrLookup.OPEN)
    c2 = _push_ctx(probe2, branch="HEAD", run_tests=lambda _c: False)
    v2 = evaluate_command(argv, c2)
    # no longer the real rule violation — it degrades to "I have no idea"
    assert "test suite failed" not in v2.reason
    assert calls2 == []


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


# ── P1R-17: a missing cwd must DENY, never fall back to os.getcwd() ───────────
# The daemon's own cwd is TARGET_DIR, checked out on dev/main — ungoverned — so
# the fallback silently deleted the push gate instead of failing.
def test_p1r_17_missing_cwd_denies_instead_of_using_the_daemons_own_cwd():
    built: list[str] = []

    def build(cwd):
        built.append(cwd)
        return ctx()

    hook = make_pretooluse_hook(build)
    for payload in ({"tool_input": {"command": "git push"}},
                    {"tool_input": {"command": "git push"}, "cwd": ""},
                    {"tool_input": {"command": "git push"}, "cwd": None}):
        out = asyncio.run(hook(payload, "id", None))
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "working directory" in hso["permissionDecisionReason"]
    assert built == []  # never built a context against the wrong directory


def test_p1r_17_present_cwd_is_used_and_the_command_still_flows():
    """Anti-vacuity twin: the deny is about the MISSING cwd, not about pushes —
    a normal call still builds the context from the cwd it was handed."""
    built: list[str] = []

    def build(cwd):
        built.append(cwd)
        return ctx(branch="dev")   # ungoverned ⇒ the push gate skips

    hook = make_pretooluse_hook(build)
    out = asyncio.run(hook(
        {"tool_input": {"command": "git push"}, "cwd": "/repo/.claude/worktrees/issue-5"},
        "id", None,
    ))
    assert out == {}
    assert built == ["/repo/.claude/worktrees/issue-5"]


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


# ── malformed Gate-Exemption hint (ADR 0014) ─────────────────────────────────
# The declaration parser is silent about lines it rejects, which is correct
# (malformed ⇒ not a declaration, fail closed) but unteachable: an agent that
# typo'd the rule or used an out-of-charset subject sees the same denial as one
# that never declared, and loops on "how do I comply" (keifu #126). The gate
# surfaces the count on the denial of the one rule a trailer still governs —
# required-test. tests-immutable is auto-declared from the diff since
# 2026-08-03, so it has no trailer to typo and carries no hint.
def _notest_ctx(**overrides):
    """A fix/ branch whose diff carries no test at all — the required-test
    denial, the rule a `Gate-Exemption:` trailer still applies to."""
    return GateContext(**{
        **_good_pr_ctx().__dict__,
        "changed_files": ("src/x.py",),
        **overrides,
    })


def test_a_required_test_denial_names_unparsed_gate_exemption_lines():
    v = evaluate_command(_good_pr_cmd(), _notest_ctx(malformed_exemption_lines=2))
    assert not v.allowed
    assert "regression test" in v.reason        # the rule still speaks first
    assert "Gate-Exemption" in v.reason
    assert "did not parse" in v.reason
    assert "2" in v.reason


def test_the_same_denial_without_malformed_lines_is_left_alone():
    # Anti-vacuity twin: the hint is driven by the observed count, not bolted
    # onto every denial.
    v = evaluate_command(_good_pr_cmd(), _notest_ctx(malformed_exemption_lines=0))
    assert not v.allowed
    assert "did not parse" not in v.reason
    assert v.reason == evaluate_command(_good_pr_cmd(), _notest_ctx()).reason


def test_malformed_lines_never_turn_an_allow_into_a_denial():
    # A hint is a hint. A compliant change with sloppy trailers still passes.
    ctx_with_junk = GateContext(**{
        **_good_pr_ctx().__dict__, "malformed_exemption_lines": 3
    })
    assert evaluate_command(_good_pr_cmd(), ctx_with_junk).allowed


# ── tests-immutable is auto-declared from the diff (ADR 0014, 2026-08-03) ────
def test_editing_an_existing_test_no_longer_denies_at_the_pr_gate():
    # The edit is admitted so the reviewer can judge it; the MERGE rule
    # withholds until REVIEW_BOT grants it. Denying here was the #126/#163
    # comply-how loop.
    edited = GateContext(**{
        **_good_pr_ctx().__dict__,
        "exists_at_base": lambda f: f.startswith("tests/"),
    })
    assert evaluate_command(_good_pr_cmd(), edited).allowed


def test_tests_renamed_out_of_the_tree_count_toward_the_scope_bound():
    # `--name-only` reports a rename by its NEW path only, so without the
    # old-side list three tests renamed out of the test tree left the suite
    # with the gate seeing nothing test-shaped (round-4 adversarial review).
    renamed = GateContext(**{
        **_good_pr_ctx().__dict__,
        # A NEW test keeps required-test satisfied; the point here is the
        # nine renamed-away OLD paths. (That the old paths alone do NOT
        # satisfy required-test is the separation this field exists for.)
        "changed_files": tuple(f"docs/old_{i}.txt" for i in range(9))
        + ("tests/test_new.py",),
        "touched_at_base": tuple(f"tests/test_{i}.py" for i in range(9)),
        "exists_at_base": lambda f: f.startswith("tests/"),
    })
    v = evaluate_command(_good_pr_cmd(), renamed)
    assert not v.allowed
    assert "existing test files modified/deleted" in v.reason


def test_a_few_renamed_tests_are_admitted_like_any_other_edit():
    # Anti-vacuity twin: "counted" must not mean "everything denies".
    renamed = GateContext(**{
        **_good_pr_ctx().__dict__,
        "changed_files": ("docs/old_0.txt", "docs/old_1.txt", "tests/test_new.py"),
        "touched_at_base": ("tests/test_0.py", "tests/test_1.py"),
        "exists_at_base": lambda f: f.startswith("tests/"),
    })
    assert evaluate_command(_good_pr_cmd(), renamed).allowed


def test_a_mass_test_rewrite_still_denies_and_teaches_no_trailer():
    # Beyond the scope bound the gate refuses — and since the rule has no
    # trailer any more, the denial must not mention one even when the branch
    # carries unparsed Gate-Exemption lines.
    many = GateContext(**{
        **_good_pr_ctx().__dict__,
        "changed_files": tuple(f"tests/test_{i}.py" for i in range(9)) + ("src/x.py",),
        "exists_at_base": lambda f: f.startswith("tests/"),
        "malformed_exemption_lines": 2,
    })
    v = evaluate_command(_good_pr_cmd(), many)
    assert not v.allowed
    assert "existing test files modified/deleted" in v.reason
    assert "Gate-Exemption" not in v.reason


@pytest.mark.parametrize("value", ["malformed", "9" * 5000])
def test_sdk_hook_malformed_policy_denies_governed_command_without_target_execution(monkeypatch, value):
    monkeypatch.setenv("EVIDENCE_MAX_IMAGE_KB", value)
    monkeypatch.setenv("LINT_CMD", "must-not-run")
    calls = []
    hook = make_pretooluse_hook(lambda cwd: ctx(run_check=lambda cmd: calls.append(cmd) or True))
    result = asyncio.run(hook({"tool_input": {"command": "git commit -m 'green: test'"}, "cwd": "/x"}, "id", None))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "malformed EVIDENCE_MAX_IMAGE_KB" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert not calls
    ordinary = asyncio.run(hook({"tool_input": {"command": "pwd"}, "cwd": "/x"}, "id", None))
    assert ordinary == {}
