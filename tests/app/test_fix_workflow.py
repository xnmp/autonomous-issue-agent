"""FixWorkflow wiring tests.

These cover the extracted fix-side workflow directly so AgentDispatcher can keep
shrinking into a router without losing behavior coverage.
"""

import asyncio

from issue_agent.adapters.journal import _legacy_event_record
from issue_agent.adapters.store import Store
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.workflows.fix import FixWorkflow, FixWorkflowConfig
from issue_agent.domain.events import Correlation, TelemetryWarning
from issue_agent.domain.outcome import AdmissionBudgetBlock, AgentResult, ExitReason
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.domain.world import Check, Comment, IssueView, PRView, Review, WorldState
from issue_agent.domain.actions import Target
from issue_agent.domain.self_clear import BudgetBlock

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees, SeqRunner
from issue_agent.adapters.github import GhUnavailable


class RetryRecordingStore(FakeStore):
    """FakeStore swallows the CI counter (set_ci_retries is a no-op) and has no
    rebase counter at all, so the breaker-advance decision is unobservable through
    it. Record both here — these tests are about WHETHER a counter moves."""

    def __init__(self):
        super().__init__()
        self.ci_retries: dict[int, int] = {}
        self.rebase_retries: dict[int, int] = {}

    def set_ci_retries(self, i, n):
        self.ci_retries[i] = n

    def get_ci_retries(self, i):
        return self.ci_retries.get(i, 0)

    def set_rebase_retries(self, i, retries):
        self.rebase_retries[i] = retries

    def get_rebase_retries(self, issue):
        return self.rebase_retries.get(issue, 0)


def _workflow(gw, store, runner, wt=None, **cfg_overrides):
    cfg_kwargs = dict(
        base_branch="dev",
        # Pinned model: these tests predate difficulty routing and assert leg
        # mechanics, not model choice — a pin keeps the router leg out of their
        # scripted run sequences. The routing tests below unpin explicitly.
        fix=FixTaskConfig("o/r", agent_md="AM", model="m"),
        fork_threshold=5,
        author_tests_split=False,
        max_contract_rounds=2,
        fix_mcp_config="",
        harness_root="",
        log=lambda _m: None,
    )
    cfg_kwargs.update(cfg_overrides)
    cfg = FixWorkflowConfig(**cfg_kwargs)
    worktrees = wt or FakeWorktrees()
    executor = Executor(
        gw,
        remove_worktree=worktrees.remove,
        clear_issue_state=getattr(store, "cleanup_pr", None),
    )
    return FixWorkflow(cfg, gw, store, worktrees, runner, executor)


def test_resolve_saves_session_and_uses_ground_truth_pr():
    gw = FakeGateway(
        issues={5: IssueView(5, title="Add token", body="body")},
        prs={100: PRView(100)},
        pr_for_issue={5: 100},
    )
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", needs="ignored", session_id="s5"))

    asyncio.run(_workflow(gw, store, runner).resolve(5))

    assert store.sessions[5] == "s5"
    assert runner.tasks[0].cwd == "/wt/issue-5"
    assert ("relabel", "issue", 5, "ai-working", "ai-pr-open") in gw.calls
    assert ("relabel", "pr", 100, None, "ai-pr-open") in gw.calls


def test_resolve_blocked_comments_and_blocks_issue():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    runner = FakeRunner(AgentResult(
        is_error=False,
        status="blocked",
        needs="which API?",
        session_id="s5",
    ))

    asyncio.run(_workflow(gw, FakeStore(), runner).resolve(5))

    assert any(c[0] == "comment" and "which API?" in c[3] for c in gw.calls)
    assert ("relabel", "issue", 5, "ai-working", "ai-blocked") in gw.calls


def test_resolve_closed_without_a_pr_retires_the_issue_worktree_and_state():
    gw = FakeGateway(
        issues={5: IssueView(5, title="t", body="b", state="closed")},
        pr_for_issue={},
    )
    store = FakeStore()
    store.cleanup_calls = []
    store.cleanup_pr = lambda pr, issue, merged: store.cleanup_calls.append(
        (pr, issue, merged)
    )
    worktrees = FakeWorktrees()
    worktrees.removed = []
    worktrees.remove = worktrees.removed.append
    runner = FakeRunner(
        AgentResult(is_error=False, status="pushed", session_id="s5")
    )

    asyncio.run(_workflow(gw, store, runner, wt=worktrees).resolve(5))

    assert worktrees.removed == [5]
    assert store.cleanup_calls == [(None, 5, True)]
    assert not any(c[0] == "relabel" for c in gw.calls)


def test_fix_ci_records_retry_and_writes_no_label_on_push():
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_name="fix/5-token", closing_issue=5,
                         checks=(Check("unit", bucket="fail"),))},
    )
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))

    asyncio.run(_workflow(gw, store, runner).fix_ci(100))

    assert store.sessions[5] == "c5"
    assert "unit" in runner.tasks[0].prompt
    # review M22/D9: a pushed leg makes NO label write. The clear-ai-working
    # relabel this used to assert removed a label nothing ever added — a `gh pr
    # edit` per leg, and a busy marker a future parallel dispatcher would have
    # trusted. Progress is recorded in the counter, not on the PR.
    assert not any(c[0] == "relabel" for c in gw.calls)


def test_fix_ci_attaches_mcp_when_check_matches(tmp_path):
    cfgfile = tmp_path / "sonar.json"
    cfgfile.write_text('{"mcpServers": {"sonarqube": {"command": "npx"}}}')
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_name="fix/5-token", closing_issue=5,
                         checks=(Check("SonarQube Code Analysis", bucket="fail"),))},
    )
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))
    fmc = '{"SonarQube": {"config": "%s", "tools": "mcp__sonarqube__*"}}' % cfgfile

    asyncio.run(_workflow(
        gw,
        FakeStore(),
        runner,
        fix_mcp_config=fmc,
        harness_root="/tmp/none",
    ).fix_ci(100))

    assert len(runner.tasks[0].mcp_servers) == 1
    assert runner.tasks[0].mcp_servers[0].name == "sonarqube"


def test_fix_ci_no_mcp_when_no_check_matches():
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_name="fix/5-token", closing_issue=5,
                         checks=(Check("SonarQube Code Analysis", bucket="fail"),))},
    )
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))

    asyncio.run(_workflow(
        gw,
        FakeStore(),
        runner,
        fix_mcp_config='{"Wiz": {"config": "/nope.json", "tools": "mcp__wiz__*"}}',
        harness_root="/tmp/none",
    ).fix_ci(100))

    assert runner.tasks[0].mcp_servers == ()


class RecordingJournalRaw:
    """Captures raw TelemetryEvent objects (not the lossy legacy view) so a
    deferral's `code` and `Correlation` can be asserted directly."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_fix_ci_defers_and_journals_dispatch_deferred_when_claim_disagrees():
    """ADR 0008: `_dispatch_pr` defers (no paid run) when the fresh breaker_key
    disagrees with the claimed issue_hint, AND surfaces a
    TelemetryWarning('dispatch_deferred', ...) with Correlation(pr=N) so a
    persistent linkage flap is visible in the dashboard per-issue event view."""
    pr = PRView(100, head_ref_name="fix/6-moved", closing_issue=6,
                checks=(Check("unit", bucket="fail"),))
    gw = FakeGateway(prs={100: pr})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))
    journal = RecordingJournalRaw()

    asyncio.run(
        _workflow(gw, FakeStore(), runner, journal=journal).fix_ci(
            100, issue_hint=5
        )
    )

    assert runner.tasks == []
    warnings = [e for e in journal.events if isinstance(e.payload, TelemetryWarning)]
    assert len(warnings) == 1
    event = warnings[0]
    assert event.payload.code == "dispatch_deferred"
    assert event.correlation == Correlation(pr=100)
    assert event.payload.details == {"observed_target": 5, "fresh_target": 6}


def test_fix_ci_defers_even_when_journalling_the_deferral_raises():
    """Telemetry must never block the deferral it is reporting on: a raising
    journal returns None from fix_ci without an exception propagating."""
    class ExplodingJournal:
        def emit(self, event):
            raise OSError("journal disk full")

    pr = PRView(100, head_ref_name="fix/6-moved", closing_issue=6,
                checks=(Check("unit", bucket="fail"),))
    gw = FakeGateway(prs={100: pr})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))

    result = asyncio.run(
        _workflow(gw, FakeStore(), runner, journal=ExplodingJournal()).fix_ci(
            100, issue_hint=5
        )
    )

    assert result is None
    assert runner.tasks == []


def test_resolve_error_bumps_resolve_counter():
    # T3 finding: an erroring resolve requeues ai-ready; the requeue must COUNT
    # or the loop is unbounded at full session cost.
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=True))

    asyncio.run(_workflow(gw, store, runner).resolve(5))

    assert store.get_resolve_retries(5) == 1


def test_resolve_blocked_does_not_bump_resolve_counter():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", needs="x", session_id="s"))

    asyncio.run(_workflow(gw, store, runner).resolve(5))

    assert store.get_resolve_retries(5) == 0


def test_resolve_infra_death_does_not_bump_resolve_counter():
    # An externally-killed session made no attempt (same rule as fix_ci/rebase).
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=True, exit_reason=ExitReason.TRANSPORT_DEATH))

    asyncio.run(_workflow(gw, store, runner).resolve(5))

    assert store.get_resolve_retries(5) == 0


def test_resume_empty_body_reply_returns_issue_to_blocked():
    # T3 probe finding: the old early-return left the issue ai-working, which
    # the reaper turned into ai-ready → an unbounded RESOLVE loop. An empty
    # reply must be consumed AND the issue put back to awaiting-human.
    iss = IssueView(5, title="t", body="b", comments=(
        Comment("fix-bot", "asking", "t1"),
        Comment("alice", "   ", "t2"),
    ))
    gw = FakeGateway(issues={5: iss}, pr_for_issue={})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="s"))

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume(5))

    assert runner.tasks == []
    assert store.get_resumed_at(5) == "t2"
    assert ("relabel", "issue", 5, "ai-working", "ai-blocked") in gw.calls


def test_resume_writes_resumed_at_mark():
    # V1/#13 regression: a dispatched resume must record WHICH reply it
    # consumed. Without the write, this blocked-again issue re-fires a paid
    # RESUME on the same unchanged thread every poll tick.
    iss = IssueView(5, title="t", body="b", comments=(
        Comment("fix-bot", "asking", "t1"),
        Comment("alice", "the answer", "t2"),
    ))
    gw = FakeGateway(issues={5: iss}, pr_for_issue={})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="s5"))

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume(5))

    assert store.get_resumed_at(5) == "t2"
    assert "the answer" in runner.tasks[0].prompt


def test_blocked_resume_forfeits_its_session_so_the_next_reply_goes_fresh():
    # tauri #604 (2026-08-09): four consecutive resumes of one session each
    # re-blocked faster and cheaper than the last (the fourth in 7s, asking
    # for "additional execution time" with its wall budget unspent) — a
    # session whose history is its own block turns amplifies the pattern on
    # every resume. A resume that settles blocked must drop the stored
    # session so the NEXT reply rebuilds from the thread and worktree
    # (fresh-recovery) instead of re-entering the groove.
    iss = IssueView(5, title="t", body="b", comments=(
        Comment("fix-bot", "**Agent needs input:** x\n\n_Reply to unblock._", "t1"),
        Comment("alice", "answered", "t2"),
    ))
    gw = FakeGateway(issues={5: iss}, pr_for_issue={})
    store = FakeStore()
    store.save_session(5, "poisoned")
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="poisoned"))

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume(5))

    assert runner.tasks[0].resume == "poisoned"
    assert store.get_session(5) == ""


def test_productive_resume_keeps_its_session():
    # Anti-vacuity twin: forfeiture is for blocked resumes only — a resume
    # that pushes keeps its context for the next round.
    iss = IssueView(5, title="t", body="b", comments=(
        Comment("fix-bot", "**Agent needs input:** x\n\n_Reply to unblock._", "t1"),
        Comment("alice", "answered", "t2"),
    ))
    gw = FakeGateway(issues={5: iss}, pr_for_issue={})
    store = FakeStore()
    store.save_session(5, "healthy")
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="healthy"))

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume(5))

    assert store.get_session(5) == "healthy"


def test_pr_resume_uses_pr_reply_and_linked_issue_session_once():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    iss = IssueView(5, title="t", body="b")
    p = PRView(
        100, head_ref_name="fix/5-token", closing_issue=5,
        comments=(Comment("fix-bot", prompt, "t1"), Comment("alice", "Use A.", "t2")),
    )
    gw = FakeGateway(issues={5: iss}, prs={100: p})
    store = FakeStore()
    store.save_session(5, "prior-session")
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume_pr(100))

    assert store.get_pr_resumed_at(100) == "t2"
    assert "Use A." in runner.tasks[0].prompt
    assert ("relabel", "pr", 100, "ai-blocked", "ai-pr-open") in gw.calls


def test_resume_without_human_reply_does_not_run():
    # Only fixer/bot comments on the thread → nothing to resume on; no session
    # spend, no resumed-at mark.
    iss = IssueView(5, title="t", body="b", comments=(
        Comment("fix-bot", "asking", "t1"),
        Comment("ci[bot]", "noise", "t2"),
    ))
    gw = FakeGateway(issues={5: iss}, pr_for_issue={})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="s5"))

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume(5))

    assert runner.tasks == []
    assert store.get_resumed_at(5) == ""


def test_address_review_attempt_rekeys_counter_to_current_review():
    # The counter follows the review being addressed: a count accumulated
    # against an OLDER review restarts at 1 for the new one, and a successful
    # push still counts (no clear-on-success — V2/#64).
    p = PRView(100, head_ref_name="fix/5-x", closing_issue=5,
               reviews=(Review("CHANGES_REQUESTED", "rev-bot", "t9", body="fix it"),))
    gw = FakeGateway(prs={100: p})
    store = FakeStore()
    store.set_review_retry(5, "t1", 3)  # stale: keyed to a superseded review
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s"))

    asyncio.run(_workflow(gw, store, runner).address_review(100))

    assert store.get_review_retry(5) == ("t9", 1)


def test_address_review_blocked_does_not_bump_counter():
    p = PRView(100, head_ref_name="fix/5-x", closing_issue=5,
               reviews=(Review("CHANGES_REQUESTED", "rev-bot", "t9", body="fix it"),))
    gw = FakeGateway(prs={100: p})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="blocked",
                                    summary="unclear", session_id="s"))

    asyncio.run(_workflow(gw, store, runner).address_review(100))

    assert store.get_review_retry(5) == ("", 0)


# ── infra death must not advance a breaker (FAILURE-MODES #58) ───────────────
def _ci_pr():
    return PRView(100, head_ref_name="fix/5-token", closing_issue=5,
                  checks=(Check("unit", bucket="fail"),))


# ── CI attempt history (tauri PR #620) ──────────────────────────────────────
def _ci_pr_at(sha, *check_names):
    return PRView(
        100, head_ref_name="fix/5-token", head_ref_oid=sha, closing_issue=5,
        checks=tuple(Check(n, bucket="fail") for n in check_names),
    )


def _pushed_runner():
    return FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))


def test_fix_ci_records_what_each_attempt_was_pointed_at(tmp_path):
    gw = FakeGateway(prs={100: _ci_pr_at("a" * 40, "e2e tests")})
    store = Store(state_dir=str(tmp_path))

    asyncio.run(_workflow(gw, store, _pushed_runner()).fix_ci(100))

    assert [(a.head_sha, a.failed_checks) for a in store.get_ci_history(5)] == [
        ("a" * 40, ("e2e tests",))
    ]


def test_fix_ci_history_is_bounded_to_the_last_five_attempts(tmp_path):
    store = Store(state_dir=str(tmp_path))
    for n in range(7):
        gw = FakeGateway(prs={100: _ci_pr_at(f"sha{n}", "e2e tests")})
        asyncio.run(_workflow(gw, store, _pushed_runner()).fix_ci(100))

    assert [a.head_sha for a in store.get_ci_history(5)] == [
        f"sha{n}" for n in range(2, 7)
    ]


def test_the_second_fix_ci_leg_sees_what_the_first_one_faced(tmp_path):
    """The incident in one test: leg two must be able to see that the SAME check
    already failed on a different head, which is the pattern nobody had."""
    store = Store(state_dir=str(tmp_path))
    first = _pushed_runner()
    asyncio.run(_workflow(
        FakeGateway(prs={100: _ci_pr_at("a" * 40, "e2e tests")}), store, first
    ).fix_ci(100))
    assert "previous FIX_CI attempts" not in first.tasks[0].prompt

    second = _pushed_runner()
    asyncio.run(_workflow(
        FakeGateway(prs={100: _ci_pr_at("b" * 40, "e2e tests")}), store, second
    ).fix_ci(100))

    prompt = second.tasks[0].prompt
    assert "attempt 1 @ aaaaaaa: e2e tests" in prompt
    assert "bbbbbbb" not in prompt  # the CURRENT head is the prompt's subject


def test_a_budget_capped_tick_is_not_recorded_as_an_attempt(tmp_path):
    """#155's rule, applied to the history: an admission rejection is a $0
    non-attempt that does not advance the breaker, so decide_fix re-dispatches
    it every poll. Recording them would let capped ticks evict the whole real
    record and leave identical zero-information rows behind."""
    store = Store(state_dir=str(tmp_path))
    asyncio.run(_workflow(
        FakeGateway(prs={100: _ci_pr_at("a" * 40, "e2e tests")}), store, _pushed_runner()
    ).fix_ci(100))

    for _ in range(6):
        capped = FakeRunner(AgentResult(
            is_error=True, exit_reason=ExitReason.BUDGET_EXHAUSTED))
        asyncio.run(_workflow(
            FakeGateway(prs={100: _ci_pr_at("b" * 40, "e2e tests")}), store, capped
        ).fix_ci(100))

    assert [a.head_sha for a in store.get_ci_history(5)] == ["a" * 40]


def test_fix_ci_records_the_attempt_even_when_the_leg_dies(tmp_path):
    # Write-before-execute: a killed leg still consumed a round, and its failure
    # is exactly the evidence the next leg needs.
    gw = FakeGateway(prs={100: _ci_pr_at("a" * 40, "e2e tests")})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, status="timeout",
                                    exit_reason=ExitReason.TRANSPORT_DEATH))

    asyncio.run(_workflow(gw, store, runner).fix_ci(100))

    assert len(store.get_ci_history(5)) == 1


def test_a_store_that_cannot_serve_the_history_still_dispatches_the_leg(tmp_path):
    """History is context, not authority. A corrupt file (and a store that
    raises outright) must cost the leg its extra section, never the repair."""
    store = Store(state_dir=str(tmp_path))
    (tmp_path / "issue-5.ci-history").write_text("}{ torn")
    gw = FakeGateway(prs={100: _ci_pr_at("a" * 40, "e2e tests")})
    runner = _pushed_runner()

    asyncio.run(_workflow(gw, store, runner).fix_ci(100))

    assert runner.tasks and "previous FIX_CI attempts" not in runner.tasks[0].prompt

    class ExplodingHistoryStore(Store):
        def get_ci_history(self, issue):
            raise OSError("state dir unreadable")

        def append_ci_attempt(self, issue, attempt):
            raise OSError("state dir unwritable")

    runner2 = _pushed_runner()
    asyncio.run(_workflow(
        FakeGateway(prs={100: _ci_pr_at("b" * 40, "e2e tests")}),
        ExplodingHistoryStore(state_dir=str(tmp_path)),
        runner2,
    ).fix_ci(100))

    assert "previous FIX_CI attempts" not in runner2.tasks[0].prompt
    assert runner2.tasks[0].prompt  # still dispatched


def test_a_merged_pr_leaves_no_history_for_the_next_one(tmp_path):
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: _ci_pr_at("a" * 40, "e2e tests")})
    asyncio.run(_workflow(gw, store, _pushed_runner()).fix_ci(100))

    store.cleanup_pr(100, 5, merged=True)

    runner = _pushed_runner()
    asyncio.run(_workflow(
        FakeGateway(prs={101: PRView(101, head_ref_name="fix/5-token",
                                     head_ref_oid="c" * 40, closing_issue=5,
                                     checks=(Check("e2e tests", bucket="fail"),))}),
        store, runner,
    ).fix_ci(101))

    assert "previous FIX_CI attempts" not in runner.tasks[0].prompt


def test_fix_ci_error_advances_the_breaker():
    # Baseline for the exemption below: a genuine failed attempt DOES count.
    gw = FakeGateway(prs={100: _ci_pr()})
    store = RetryRecordingStore()
    runner = FakeRunner(AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR))

    asyncio.run(_workflow(gw, store, runner).fix_ci(100))

    assert store.get_ci_retries(5) == 1


def test_fix_ci_infra_death_does_not_advance_the_breaker():
    # #58: six SIGKILLs on one PR walked the CI breaker to needs-human-review on
    # work that never ran. The PR is left exactly as it was found (M22/D9: there
    # is no PR busy marker to clear).
    gw = FakeGateway(prs={100: _ci_pr()})
    store = RetryRecordingStore()
    store.set_ci_retries(5, 2)
    runner = FakeRunner(
        AgentResult(is_error=True, status="timeout",
                    exit_reason=ExitReason.KILLED_EXTERNALLY)
    )

    asyncio.run(_workflow(gw, store, runner).fix_ci(100))

    assert store.get_ci_retries(5) == 2
    assert not any(c[0] == "relabel" for c in gw.calls)  # M22/D9: no PR label writes


def test_rebase_error_advances_the_breaker():
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="fix/5-token", closing_issue=5)})
    store = RetryRecordingStore()
    runner = FakeRunner(AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR))

    asyncio.run(_workflow(gw, store, runner).rebase(100))

    assert store.get_rebase_retries(5) == 1


def test_rebase_infra_death_does_not_advance_the_breaker():
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="fix/5-token", closing_issue=5)})
    store = RetryRecordingStore()
    store.set_rebase_retries(5, 1)
    runner = FakeRunner(
        AgentResult(is_error=True, status="timeout",
                    exit_reason=ExitReason.TRANSPORT_DEATH)
    )

    asyncio.run(_workflow(gw, store, runner).rebase(100))

    assert store.get_rebase_retries(5) == 1


def test_fix_ci_infra_death_counts_against_the_infra_ceiling(tmp_path):
    """review C3/D2c, the RETURNED-result half. The exemption above (an infra
    death is not a CI attempt) left the deaths themselves unbounded: the tail
    moves no label, the next poll sees the same red CI, and pays again. The
    separate counter is what bounds it — under the SAME key the CI counter uses,
    so the decide side reads one key per PR leg."""
    gw = FakeGateway(prs={100: _ci_pr()})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, status="timeout",
                                    exit_reason=ExitReason.TRANSPORT_DEATH))

    asyncio.run(_workflow(gw, store, runner).fix_ci(100))

    assert store.get_infra_deaths(5) == 1
    assert store.get_ci_retries(5) == 0  # still not an attempt


def test_fix_ci_error_does_not_touch_the_infra_ceiling(tmp_path):
    """Anti-vacuity twin: a genuine failed attempt marches the CI breaker, NOT
    the infra ceiling. Counting it twice would escalate a normally-failing PR
    through whichever breaker tripped first, for the wrong stated reason."""
    gw = FakeGateway(prs={100: _ci_pr()})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR))

    asyncio.run(_workflow(gw, store, runner).fix_ci(100))

    assert store.get_infra_deaths(5) == 0
    assert store.get_ci_retries(5) == 1


def test_rebase_infra_death_counts_against_the_infra_ceiling(tmp_path):
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="fix/5-token", closing_issue=5)})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, status="timeout",
                                    exit_reason=ExitReason.KILLED_EXTERNALLY))

    asyncio.run(_workflow(gw, store, runner).rebase(100))

    assert store.get_infra_deaths(5) == 1
    assert store.get_rebase_retries(5) == 0


def test_resolve_infra_death_counts_against_the_infra_ceiling(tmp_path):
    """The resolve leg is the expensive one, and its infra deaths leave the
    issue ai-working for the reaper → ai-ready → dispatched again. Without this
    counter that cycle is unbounded (review C3)."""
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, exit_reason=ExitReason.TRANSPORT_DEATH))

    asyncio.run(_workflow(gw, store, runner).resolve(5))

    assert store.get_infra_deaths(5) == 1
    assert store.get_resolve_retries(5) == 0


# ── review P1R-3: the infra ceiling decays on a leg that RETURNED ─────────────
# The counter bounds CONSECUTIVE deaths. Nothing ever reset it, so it was a
# lifetime tally: deaths accumulated across weeks of otherwise healthy work
# until a perfectly serviceable issue was handed to a human. The only cleaner
# that removed it (`cleanup_pr(merged=True)`) runs on work that already
# SUCCEEDED — never on the issue that is still trying.
def _death(reason=ExitReason.TRANSPORT_DEATH):
    return AgentResult(is_error=True, status="timeout", exit_reason=reason)


def test_a_returned_result_resets_the_infra_ceiling(tmp_path):
    """Two deaths, then one leg that came back: the ceiling starts over."""
    gw = FakeGateway(prs={100: _ci_pr()})
    store = Store(state_dir=str(tmp_path))
    seq = SeqRunner([_death(), _death(),
                     AgentResult(is_error=False, status="pushed", session_id="s")])
    wf = _workflow(gw, store, seq)

    asyncio.run(wf.fix_ci(100))
    asyncio.run(wf.fix_ci(100))
    assert store.get_infra_deaths(5) == 2, "anti-vacuity: the deaths were counted"

    asyncio.run(wf.fix_ci(100))

    assert store.get_infra_deaths(5) == 0


def test_an_errored_result_also_resets_it(tmp_path):
    """The ceiling asks "did the session survive to deliver anything?", not "was
    the answer good" — an honest failure is bounded by the CI breaker, which
    this same leg advanced."""
    gw = FakeGateway(prs={100: _ci_pr()})
    store = Store(state_dir=str(tmp_path))
    seq = SeqRunner([_death(),
                     AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR)])
    wf = _workflow(gw, store, seq)

    asyncio.run(wf.fix_ci(100))
    asyncio.run(wf.fix_ci(100))

    assert store.get_infra_deaths(5) == 0
    assert store.get_ci_retries(5) == 1


def test_deaths_that_never_return_still_reach_the_ceiling(tmp_path):
    """Anti-vacuity twin: a decay that fired on the death path itself would
    disarm the ceiling completely. Five consecutive deaths must still count
    five — that unbounded paid loop is why the counter exists."""
    gw = FakeGateway(prs={100: _ci_pr()})
    store = Store(state_dir=str(tmp_path))
    wf = _workflow(gw, store, FakeRunner(_death()))

    for _ in range(5):
        asyncio.run(wf.fix_ci(100))

    assert store.get_infra_deaths(5) == 5


def test_consecutive_resolve_deaths_still_reach_the_ceiling(tmp_path):
    """The same anti-vacuity check on the leg whose decay is NOT inside an
    `if not infra_death` branch: `resolve` calls the decay for every returned
    result, so a decay that forgot to exempt the death path would reset the
    counter it had just bumped and disarm the ceiling on the most expensive
    leg in the harness."""
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = Store(state_dir=str(tmp_path))
    wf = _workflow(gw, store, FakeRunner(_death()))

    for _ in range(5):
        asyncio.run(wf.resolve(5))

    assert store.get_infra_deaths(5) == 5
    assert store.get_resolve_retries(5) == 0, "a death is never an attempt"


def test_the_resolve_leg_decays_it_too(tmp_path):
    """The expensive leg, and the one whose deaths leave the issue ai-working
    for the reaper — so it is the one that accumulates a stale tally fastest."""
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = Store(state_dir=str(tmp_path))
    seq = SeqRunner([_death(ExitReason.KILLED_EXTERNALLY),
                     AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR)])
    wf = _workflow(gw, store, seq)

    asyncio.run(wf.resolve(5))
    assert store.get_infra_deaths(5) == 1

    asyncio.run(wf.resolve(5))

    assert store.get_infra_deaths(5) == 0
    assert store.get_resolve_retries(5) == 1  # the honest failure is bounded elsewhere


def test_the_rebase_and_address_review_legs_decay_it_too(tmp_path):
    gw = FakeGateway(prs={100: PRView(
        100, head_ref_name="fix/5-x", closing_issue=5,
        reviews=(Review("CHANGES_REQUESTED", "rev-bot", "t9", body="fix it"),),
    )})
    store = Store(state_dir=str(tmp_path))
    ok = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s"))

    store.bump_infra_deaths(5)
    asyncio.run(_workflow(gw, store, ok).rebase(100))
    assert store.get_infra_deaths(5) == 0

    store.bump_infra_deaths(5)
    asyncio.run(_workflow(gw, store, ok).address_review(100))
    assert store.get_infra_deaths(5) == 0


def test_address_review_infra_death_does_not_burn_a_review_retry():
    p = PRView(100, head_ref_name="fix/5-x", closing_issue=5,
               reviews=(Review("CHANGES_REQUESTED", "rev-bot", "t9", body="fix it"),))
    gw = FakeGateway(prs={100: p})
    store = RetryRecordingStore()
    store.set_review_retry(5, "t9", 2)
    runner = FakeRunner(
        AgentResult(is_error=True, status="timeout",
                    exit_reason=ExitReason.KILLED_EXTERNALLY, session_id="s")
    )

    asyncio.run(_workflow(gw, store, runner).address_review(100))

    assert store.get_review_retry(5) == ("t9", 2)


def test_split_majority_case_author_then_implement_opens_pr():
    gw = FakeGateway(
        issues={5: IssueView(5, title="Null token", body="x")},
        pr_for_issue={5: 100},
    )
    store, wt = FakeStore(), FakeWorktrees()
    runner = SeqRunner([
        AgentResult(is_error=False, status="tests_authored", session_id="a5"),
        AgentResult(is_error=False, status="pr_opened", session_id="i5"),
    ])

    asyncio.run(_workflow(gw, store, runner, wt=wt, author_tests_split=True).resolve(5))

    prompts = [t.prompt for t in runner.tasks]
    assert "AUTHOR" in prompts[0]
    assert "IMPLEMENTER" in prompts[1]
    assert runner.tasks[1].resume is None
    assert wt.resets == []
    assert ("relabel", "issue", 5, "ai-working", "ai-pr-open") in gw.calls
    assert ("relabel", "pr", 100, None, "ai-pr-open") in gw.calls


def test_split_author_blocked_stops_before_implement():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    runner = SeqRunner([
        AgentResult(is_error=False, status="blocked", needs="which schema?", session_id="a5")
    ])

    asyncio.run(_workflow(gw, FakeStore(), runner, author_tests_split=True).resolve(5))

    assert len(runner.tasks) == 1
    assert ("relabel", "issue", 5, "ai-working", "ai-blocked") in gw.calls


def test_split_contract_change_revised_then_pr():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={5: 100})
    store, wt = FakeStore(), FakeWorktrees()
    runner = SeqRunner([
        AgentResult(is_error=False, status="tests_authored", session_id="a"),
        AgentResult(is_error=False, status="contract_change", needs="test asserts wrong layer", session_id="i1"),
        AgentResult(is_error=False, session_id="adj", structured={"verdict": "revised", "summary": "fixed AC"}),
        AgentResult(is_error=False, status="pr_opened", session_id="i2"),
    ])
    seq_pr = [None, 100]
    gw.pr_for_issue = lambda i: seq_pr.pop(0) if seq_pr else 100

    asyncio.run(_workflow(gw, store, runner, wt=wt, author_tests_split=True).resolve(5))

    prompts = [t.prompt for t in runner.tasks]
    assert "AUTHOR" in prompts[0] and "IMPLEMENTER" in prompts[1]
    assert "adjudicating" in prompts[2]
    assert "test asserts wrong layer" in prompts[2]
    assert "IMPLEMENTER" in prompts[3]
    assert wt.resets == [5]
    assert ("relabel", "issue", 5, "ai-working", "ai-pr-open") in gw.calls


def test_split_contract_change_upheld_then_escalates():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store, wt = FakeStore(), FakeWorktrees()
    runner = SeqRunner([
        AgentResult(is_error=False, status="tests_authored", session_id="a"),
        AgentResult(is_error=False, status="contract_change", needs="objection 1", session_id="i1"),
        AgentResult(is_error=False, session_id="adj", structured={"verdict": "upheld", "rationale": "AC is right"}),
        AgentResult(is_error=False, status="contract_change", needs="objection 2", session_id="i2"),
    ])
    gw.pr_for_issue = lambda i: None

    asyncio.run(_workflow(
        gw,
        store,
        runner,
        wt=wt,
        author_tests_split=True,
        max_contract_rounds=1,
    ).resolve(5))

    assert "UPHELD" in runner.tasks[3].prompt
    assert ("relabel", "issue", 5, "ai-working", "needs-human-review") in gw.calls
    assert any(c[0] == "comment" and "objection 2" in c[3] for c in gw.calls)
    assert not any(c[0] == "relabel" and c[-1] == "ai-pr-open" for c in gw.calls)


def test_split_contract_change_resets_before_adjudication_and_escalates_at_cap():
    gw = FakeGateway(
        issues={5: IssueView(5, title="Hard thing", body="body")},
    )
    store = FakeStore()
    wt = FakeWorktrees()
    runner = SeqRunner([
        AgentResult(is_error=False, status="tests_authored", session_id="a"),
        AgentResult(is_error=False, status="contract_change", needs="bad contract", session_id="i1"),
    ])

    asyncio.run(
        _workflow(
            gw, store, runner, wt=wt,
            author_tests_split=True,
            max_contract_rounds=0,
        ).resolve(5)
    )

    assert wt.resets == []
    assert store.phases[5] == "authored"
    assert ("relabel", "issue", 5, "ai-working", "needs-human-review") in gw.calls


# ── review M13: the two repair prompts must carry their primary inputs ───────
class PromptInputGateway(FakeGateway):
    """FakeGateway + the two M13 prompt-input methods.

    These tests assert at the WORKFLOW level on purpose: the prompt builders
    take `failed_log` / `inline` as parameters, so a builder test supplying its
    own fixture text passes happily while the workflow hands the builder "" —
    which is exactly the bug M13 found sitting in production code."""

    def __init__(self, *a, run_log="", inline="", boom=False, **kw):
        super().__init__(*a, **kw)
        self._run_log = run_log
        self._inline = inline
        self._boom = boom
        self.log_args = []

    def failed_run_log(self, pr, *, branch="", lines=150):
        if self._boom:
            raise RuntimeError("gh run list exploded")
        self.log_args.append((pr, branch))
        return self._run_log

    def review_comments(self, pr, *, limit=20):
        if self._boom:
            raise RuntimeError("gh api exploded")
        return self._inline


class LegacyGateway(FakeGateway):
    """A gateway from before D6: neither prompt-input method exists on it.

    The absence is pinned here rather than inherited from the shared FakeGateway,
    so that adding those methods to the shared fake cannot silently retire this
    coverage — the point is that the workflow tolerates a gateway without them."""

    def __getattribute__(self, name):
        if name in ("failed_run_log", "review_comments"):
            raise AttributeError(name)
        return super().__getattribute__(name)


def _review_pr():
    return PRView(100, head_ref_name="fix/5-x", closing_issue=5,
                  reviews=(Review("CHANGES_REQUESTED", "rev-bot", "t9", body="fix it"),))


_PUSHED = AgentResult(is_error=False, status="pushed", session_id="s")


def test_fix_ci_prompt_carries_the_failed_run_log():
    # M13: the prompt rendered "Log (last 150 lines):" over nothing, so the
    # fixer had to rediscover the failure with `gh api` denied by the governor.
    gw = PromptInputGateway(prs={100: _ci_pr()}, run_log="E   assert 1 == 2  # SENTINEL")
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, FakeStore(), runner).fix_ci(100))

    assert "E   assert 1 == 2  # SENTINEL" in runner.tasks[0].prompt
    # keyed to the REAL head branch, and handed over so the gateway need not
    # re-fetch the PR the workflow already holds
    assert gw.log_args == [(100, "fix/5-token")]


def test_fix_ci_dispatches_when_the_failed_run_log_is_empty():
    # Anti-vacuity twin of the above: a log fetch that finds nothing must not
    # cost the repair leg — the agent can still diagnose from the check names.
    gw = PromptInputGateway(prs={100: _ci_pr()}, run_log="")
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, FakeStore(), runner).fix_ci(100))

    assert len(runner.tasks) == 1
    assert "unit" in runner.tasks[0].prompt
    assert not any(c[0] == "relabel" for c in gw.calls)  # M22/D9: no PR label writes


def test_fix_ci_dispatches_when_the_failed_run_log_raises():
    # M13's contract at the workflow level: the gateway fails soft, but a
    # gateway that does NOT (a raising fake, a legacy adapter, a gh binary that
    # dies mid-call) must still not turn a cosmetic gap into a dead repair leg.
    gw = PromptInputGateway(prs={100: _ci_pr()}, boom=True)
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, FakeStore(), runner).fix_ci(100))

    assert len(runner.tasks) == 1
    assert not any(c[0] == "relabel" for c in gw.calls)  # M22/D9: no PR label writes


def test_fix_ci_dispatches_when_the_gateway_has_no_failed_run_log():
    # An injected gateway predating D6 has no such method at all.
    gw = LegacyGateway(prs={100: _ci_pr()})
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, FakeStore(), runner).fix_ci(100))

    assert len(runner.tasks) == 1
    assert not any(c[0] == "relabel" for c in gw.calls)  # M22/D9: no PR label writes


def test_address_review_prompt_carries_the_inline_comments():
    # M13: the file:line asks reviewers actually make live in the inline
    # comments; the workflow passed "" and the fixer never saw them, burning
    # rounds off the 4-rejection ceiling guessing at the review body.
    gw = PromptInputGateway(
        prs={100: _review_pr()},
        inline="src/a.py:12 (rev-bot): SENTINEL null check missing",
    )
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, FakeStore(), runner).address_review(100))

    assert "SENTINEL null check missing" in runner.tasks[0].prompt
    assert "fix it" in runner.tasks[0].prompt  # review body still carried


def test_address_review_dispatches_when_there_are_no_inline_comments():
    # Anti-vacuity twin: a review with only a body is the common case.
    gw = PromptInputGateway(prs={100: _review_pr()}, inline="")
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, FakeStore(), runner).address_review(100))

    assert len(runner.tasks) == 1
    assert "fix it" in runner.tasks[0].prompt
    assert not any(c[0] == "relabel" for c in gw.calls)  # M22/D9: no PR label writes


def test_address_review_dispatches_when_review_comments_raises():
    gw = PromptInputGateway(prs={100: _review_pr()}, boom=True)
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, FakeStore(), runner).address_review(100))

    assert len(runner.tasks) == 1
    assert not any(c[0] == "relabel" for c in gw.calls)  # M22/D9: no PR label writes


def test_address_review_dispatches_when_the_gateway_has_no_review_comments():
    gw = LegacyGateway(prs={100: _review_pr()})
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, FakeStore(), runner).address_review(100))

    assert len(runner.tasks) == 1
    assert not any(c[0] == "relabel" for c in gw.calls)  # M22/D9: no PR label writes


# ── review C3/D2c: the two legs B1 left uncounted ────────────────────────────
def test_address_review_infra_death_counts_against_the_infra_ceiling(tmp_path):
    """The review-retry exemption (an external kill is not an attempt) left the
    deaths unbounded: the tail moves no label, the next poll sees the same
    CHANGES_REQUESTED review and pays again. Same key the review counter uses."""
    gw = FakeGateway(prs={100: _review_pr()})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, status="timeout",
                                    exit_reason=ExitReason.TRANSPORT_DEATH, session_id="s"))

    asyncio.run(_workflow(gw, store, runner).address_review(100))

    assert store.get_infra_deaths(5) == 1
    assert store.get_review_retry(5) == ("", 0)  # still not an attempt


def test_address_review_error_does_not_touch_the_infra_ceiling(tmp_path):
    """Anti-vacuity twin: a genuine failed attempt marches the review breaker,
    not the infra ceiling — counting both escalates for the wrong reason."""
    gw = FakeGateway(prs={100: _review_pr()})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR,
                                    session_id="s"))

    asyncio.run(_workflow(gw, store, runner).address_review(100))

    assert store.get_infra_deaths(5) == 0
    assert store.get_review_retry(5) == ("t9", 1)


def _resume_issue():
    return IssueView(5, title="t", body="b", comments=(
        Comment("fix-bot", "asking", "t1"),
        Comment("alice", "the answer", "t2"),
    ))


def test_resume_infra_death_counts_against_the_infra_ceiling(tmp_path):
    """resume_actions deliberately does NOTHING on an infra death (the human's
    reply must not be consumed by a session that never ran), so the issue stays
    ai-working → reaper → ai-ready → paid again. Nothing else bounds that."""
    gw = FakeGateway(issues={5: _resume_issue()}, pr_for_issue={})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, status="timeout",
                                    exit_reason=ExitReason.KILLED_EXTERNALLY, session_id="s"))

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume(5))

    assert store.get_infra_deaths(5) == 1
    assert store.get_resolve_retries(5) == 0  # no attempt was made


def test_resume_error_does_not_touch_the_infra_ceiling(tmp_path):
    """Anti-vacuity twin: an agent that ran and failed is not an infra death;
    the resume leg parks the issue back on ai-blocked and no counter moves."""
    gw = FakeGateway(issues={5: _resume_issue()}, pr_for_issue={})
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR,
                                    session_id="s"))

    asyncio.run(_workflow(gw, store, runner, fix_bot="fix-bot").resume(5))

    assert store.get_infra_deaths(5) == 0
    assert ("relabel", "issue", 5, "ai-working", "ai-blocked") in gw.calls


# ── decay must clear the whole key set the read unions (verification sweep) ────
def test_a_clean_pr_leg_clears_a_death_stranded_under_the_orphan_key(tmp_path):
    """The composition gap between P1R-1 (read unions {breaker_key, -pr}) and
    P1R-3 (decay cleared ONE key): a transient `issue_for_pr` failure that
    coincides with a session death records under `-pr`, and the next clean run
    only cleared the resolved key — so the phantom death read as real forever
    and walked a healthy PR toward needs-human-review. `cleanup_pr` never
    reached it either: it clears whichever key is current at cleanup time."""
    store = Store(str(tmp_path))
    store.bump_infra_deaths(-100)  # death recorded while the lookup was down
    store.bump_infra_deaths(5)     # and one under the resolved issue

    gw = FakeGateway(prs={100: _review_pr()})
    runner = FakeRunner(_PUSHED)

    asyncio.run(_workflow(gw, store, runner).address_review(100))

    assert store.get_infra_deaths(5) == 0
    assert store.get_infra_deaths(-100) == 0, "the orphaned half must clear too"


def test_an_infra_death_still_leaves_both_halves_standing(tmp_path):
    """Anti-vacuity twin: decay must not fire on the very deaths the ceiling
    counts, in either half."""
    store = Store(str(tmp_path))
    store.bump_infra_deaths(-100)

    gw = FakeGateway(prs={100: _review_pr()})
    runner = FakeRunner(_death())

    asyncio.run(_workflow(gw, store, runner).address_review(100))

    assert store.get_infra_deaths(-100) == 1, "a death must not clear itself"
    assert store.get_infra_deaths(5) == 1, "and the leg's own death is counted"


# ── difficulty routing (#8 / P4.1) ────────────────────────────────────────────
# The resolve family classifies the issue on a cheap router run and picks the
# tier's model from the ACTIVE runtime's family — but only when no FIX_MODEL is
# pinned, and never at the price of the dispatch itself.

from issue_agent.app.planners.schemas import ROUTER_SCHEMA


def _router_result(tier):
    return AgentResult(is_error=False, session_id="rt", structured={"tier": tier})


class PredictionRecordingStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.predictions = {}

    def save_predicted_paths(self, issue, paths):
        self.predictions[issue] = paths


def test_resolve_routes_standard_issue_to_sonnet_when_model_unpinned():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    router_task, resolve_task = runner.tasks
    assert router_task.schema is ROUTER_SCHEMA
    assert router_task.cwd == "/wt/issue-5"
    assert resolve_task.model == "sonnet"


def test_resolve_persists_router_path_predictions_for_later_overlap_checks():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = PredictionRecordingStore()
    runner = SeqRunner([
        AgentResult(is_error=False, session_id="rt", structured={
            "tier": "standard", "predicted_paths": ["src/action.rs"],
        }),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, store, runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert store.predictions == {5: frozenset({"src/action.rs"})}


def test_router_defers_the_selected_issue_before_resolve_when_prediction_overlaps():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    gw.build_world = lambda: WorldState(prs=(PRView(
        100, state="OPEN", changed_files=frozenset({"src/action.rs"}),
    ),))
    store = PredictionRecordingStore()
    runner = SeqRunner([
        AgentResult(is_error=False, session_id="rt", structured={
            "tier": "standard", "predicted_paths": ["src/action.rs"],
        }),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, store, runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert len(runner.tasks) == 1, "the existing router run must prevent the resolve"
    assert ("relabel", "issue", 5, "ai-working", "ai-ready") in gw.calls


def test_router_defers_when_atomic_active_reservation_rejects_expanded_paths():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = PredictionRecordingStore()
    runner = SeqRunner([
        AgentResult(is_error=False, session_id="rt", structured={
            "tier": "standard", "predicted_paths": ["src/shared.py"],
        }),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])
    workflow = _workflow(
        gw, store, runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    )
    observed = []
    workflow.reserve_paths = (
        lambda issue, paths: observed.append((issue, paths)) or False
    )

    asyncio.run(workflow.resolve(5))

    assert observed == [(5, frozenset({"src/shared.py"}))]
    assert len(runner.tasks) == 1
    assert ("relabel", "issue", 5, "ai-working", "ai-ready") in gw.calls


def test_resolve_skips_the_router_when_a_model_is_pinned():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    runner = FakeRunner(AgentResult(is_error=False, status="pr_opened", session_id="s5"))

    asyncio.run(_workflow(
        gw, FakeStore(), runner,
        fix=FixTaskConfig("o/r", agent_md="AM", model="opus"),
    ).resolve(5))

    assert len(runner.tasks) == 1, "a pinned FIX_MODEL must not pay a router run"
    assert runner.tasks[0].model == "opus"


def test_resolve_routes_within_the_codex_family():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    runner = SeqRunner([
        _router_result("complex"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fixer_runtime="codex",
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[0].model == "gpt-5.6-luna", "router runs on the family's cheapest tier"
    assert runner.tasks[1].model == "gpt-5.6-sol"


def test_garbage_router_output_degrades_to_standard():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    runner = SeqRunner([
        AgentResult(is_error=True, session_id="rt", structured=None),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].model == "sonnet"


def test_router_raise_degrades_to_the_unrouted_default():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})

    class RouterThenFake:
        """First run() raises (the router leg); later runs return normally."""

        def __init__(self):
            self.tasks = []

        async def run(self, task):
            self.tasks.append(task)
            if len(self.tasks) == 1:
                raise RuntimeError("router transport died")
            return AgentResult(is_error=False, status="pr_opened", session_id="s5")

    runner = RouterThenFake()
    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert len(runner.tasks) == 2, "the resolve dispatch must survive a dead router"
    assert runner.tasks[1].model is None


def test_split_mode_routes_once_and_shares_the_model_across_legs():
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    runner = SeqRunner([
        _router_result("trivial"),
        AgentResult(is_error=False, status="tests_authored", session_id="a5"),
        AgentResult(is_error=False, status="pr_opened", session_id="i5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, author_tests_split=True,
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert [t.schema is ROUTER_SCHEMA for t in runner.tasks] == [True, False, False]
    assert runner.tasks[1].model == "haiku"
    assert runner.tasks[2].model == "haiku", "one classification covers author + implement"


# ── escalation on evidence (#16) ──────────────────────────────────────────────
# When the harness already knows the previous attempt under-delivered — the issue
# came back (#10), a resolve failed, review keeps asking for changes — the retry
# must not run the identical configuration. Effort climbs first, then tier.

from issue_agent.domain.effort import Effort
from issue_agent.domain.labels import Label


class RecordingJournal:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        if not isinstance(event, str):
            event, fields = _legacy_event_record(event)
        self.events.append((event, fields))


def _issue(**kw):
    return IssueView(5, title="t", body="b", **kw)


def test_no_evidence_emits_no_effort_at_all():
    """The common path must construct the SAME task it did before the ladder
    existed, so each runtime keeps its own default rather than being pinned to
    the harness's idea of medium."""
    gw = FakeGateway(issues={5: _issue()}, pr_for_issue={})
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].effort is None
    assert runner.tasks[1].model == "sonnet"


def test_a_recurrence_label_dispatches_the_capability_ceiling():
    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    resolve_task = runner.tasks[1]
    assert resolve_task.effort is Effort.HIGH
    assert resolve_task.model == "fable", "a recurrence goes directly to the ceiling"


def test_a_recurrence_and_prior_failure_still_dispatch_the_ceiling():
    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )
    store = FakeStore()
    store.set_resolve_retries(5, 1)
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, store, runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].effort is Effort.HIGH
    assert runner.tasks[1].model == "fable", "the ceiling remains stable with more evidence"


def test_below_the_hard_evidence_threshold_keeps_complex_route_unchanged():
    """One completed failure buys effort; it must not replace Claude complex's
    normal Opus route or attach the two-failure history notice."""
    gw = FakeGateway(issues={5: _issue()}, pr_for_issue={})
    store = FakeStore()
    store.set_resolve_retries(5, 1)
    runner = SeqRunner([
        _router_result("complex"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, store, runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    resolve_task = runner.tasks[1]
    assert resolve_task.model == "opus"
    assert resolve_task.effort is Effort.HIGH
    assert "difficult problem" not in resolve_task.append_system_prompt


def test_two_completed_failures_add_a_difficult_history_warning():
    gw = FakeGateway(issues={5: _issue()}, pr_for_issue={})
    store = FakeStore()
    store.set_resolve_retries(5, 2)
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, store, runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    resolve_task = runner.tasks[1]
    assert resolve_task.model == "fable"
    assert resolve_task.effort is Effort.HIGH
    assert "difficult problem" in resolve_task.append_system_prompt
    assert "failed in the past" in resolve_task.append_system_prompt
    assert "extra care" in resolve_task.append_system_prompt


def test_escalation_respects_the_fixer_runtime_family():
    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )
    store = FakeStore()
    store.set_resolve_retries(5, 1)
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, store, runner, fixer_runtime="codex",
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].model == "gpt-5.6-sol"


def test_a_pinned_model_still_thinks_harder_but_keeps_its_model():
    """The operator chose the model, not how hard it thinks — and a pin must
    still not pay for a router run."""
    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )
    store = FakeStore()
    store.set_resolve_retries(5, 2)
    runner = FakeRunner(AgentResult(is_error=False, status="pr_opened", session_id="s5"))

    asyncio.run(_workflow(
        gw, store, runner, fix=FixTaskConfig("o/r", agent_md="AM", model="opus"),
    ).resolve(5))

    assert len(runner.tasks) == 1, "a pinned FIX_MODEL must not pay a router run"
    assert runner.tasks[0].model == "opus"
    assert runner.tasks[0].effort is Effort.HIGH


def test_escalation_survives_a_router_that_died():
    """Routing is advisory; the evidence is not. A dead router must not also
    swallow the escalation the harness already had grounds for."""
    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )

    class RouterDies:
        def __init__(self):
            self.tasks = []

        async def run(self, task):
            self.tasks.append(task)
            if len(self.tasks) == 1:
                raise RuntimeError("router transport died")
            return AgentResult(is_error=False, status="pr_opened", session_id="s5")

    runner = RouterDies()
    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].model == "fable", "evidence must survive router failure"
    assert runner.tasks[1].effort is Effort.HIGH


def test_a_router_failure_still_records_the_ceiling_model():
    """Hard evidence is enough to select the fresh-resolve ceiling even when
    the advisory router is unavailable, so telemetry records the model run."""
    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )
    journal = RecordingJournal()

    class RouterDies:
        def __init__(self):
            self.tasks = []

        async def run(self, task):
            self.tasks.append(task)
            if len(self.tasks) == 1:
                raise RuntimeError("router transport died")
            return AgentResult(is_error=False, status="pr_opened", session_id="s5")

    runner = RouterDies()
    asyncio.run(_workflow(
        gw, FakeStore(), runner, journal=journal,
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    (_event, fields), = journal.events
    assert fields["model"] == "fable"
    assert runner.tasks[1].model == "fable"


def test_the_kill_switch_restores_the_pre_ladder_dispatch():
    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )
    store = FakeStore()
    store.set_resolve_retries(5, 3)
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, store, runner, escalate_on_evidence=False,
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].effort is None
    assert runner.tasks[1].model == "sonnet"


def test_a_store_that_cannot_answer_still_dispatches():
    """The counters decide WHICH configuration to run, never WHETHER to run.
    An unreadable counter must cost a mis-tiering, not the pipeline."""

    class BrokenStore(FakeStore):
        def get_resolve_retries(self, i):
            raise OSError("state dir vanished")

        def get_review_retry(self, i):
            raise OSError("state dir vanished")

    gw = FakeGateway(
        issues={5: _issue()}, prs={100: PRView(100)}, pr_for_issue={5: 100}
    )
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, BrokenStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].model == "sonnet"
    assert runner.tasks[1].effort is None


def _pr_with_rounds(n, extra=()):
    """A PR the reviewer has requested changes on `n` times."""
    reviews = tuple(
        Review("CHANGES_REQUESTED", "rev-bot", f"t{i}", body="fix it")
        for i in range(1, n + 1)
    ) + tuple(extra)
    return PRView(100, head_ref_name="fix/5-x", closing_issue=5, reviews=reviews)


def test_repair_legs_keep_their_model_while_thinking_harder():
    """A repair leg resumes a live transcript — handing that context to a
    different reasoner is a different bet than asking this one to think harder.
    The hard ceiling is a fresh-resolve routing decision, so it cannot replace a
    model attached to a resumed transcript."""
    gw = FakeGateway(prs={100: _pr_with_rounds(2)}, issues={5: IssueView(5)})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM", model="sonnet"),
    ).address_review(100))

    assert runner.tasks[0].effort is Effort.HIGH
    assert runner.tasks[0].model == "sonnet", "a resumed session keeps its reasoner"


def test_review_rounds_climb_with_the_reviewer_not_with_retries_at_one_review():
    """The regression the store's `review-retries` counter cannot catch.

    That counter is re-keyed to 0 by every NEW review (address_review's
    `base = n if at == review_at else 0`), so it reads 1 forever no matter how
    many times the reviewer comes back — reading it made this whole evidence
    class dead on arrival. The honest source is the PR's own review list.
    """
    seen = []
    for rounds in (1, 2, 3):
        gw = FakeGateway(prs={100: _pr_with_rounds(rounds)}, issues={5: IssueView(5)})
        runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))
        asyncio.run(_workflow(
            gw, FakeStore(), runner,
            fix=FixTaskConfig("o/r", agent_md="AM", model="sonnet"),
        ).address_review(100))
        seen.append(runner.tasks[0].effort)

    assert seen == [None, Effort.HIGH, Effort.HIGH], (
        "one REQUEST_CHANGES is the system working; the second is evidence"
    )


def test_an_approve_resets_the_round_count():
    """Prior dissatisfaction that was SATISFIED is not evidence — the same rule
    the reviewer's own escalation ceiling applies."""
    pr = _pr_with_rounds(3, extra=(Review("APPROVED", "rev-bot", "t9", body=""),))
    gw = FakeGateway(prs={100: pr}, issues={5: IssueView(5)})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM", model="sonnet"),
    ).address_review(100))

    assert runner.tasks[0].effort is None


def test_a_repair_leg_sees_the_recurrence_label_too():
    """An issue the harness already failed once is exactly the one whose REPAIR
    legs should also try harder — the signal must not be resolve-only."""
    gw = FakeGateway(
        prs={100: _pr_with_rounds(1)},
        issues={5: IssueView(5, labels=frozenset({Label.RECURRENCE}))},
    )
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM", model="sonnet"),
    ).address_review(100))

    assert runner.tasks[0].effort is Effort.HIGH


def test_a_repair_leg_dispatches_even_when_the_issue_cannot_be_fetched():
    class NoIssues(FakeGateway):
        def get_issue(self, number):
            raise GhUnavailable("network", "get_issue timed out (test-injected)")

    gw = NoIssues(prs={100: _pr_with_rounds(1)})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM", model="sonnet"),
    ).address_review(100))

    assert len(runner.tasks) == 1 and runner.tasks[0].effort is None


def test_escalations_are_journalled_so_the_tier_split_stays_measurable():
    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )
    journal = RecordingJournal()
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, journal=journal,
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert len(journal.events) == 1
    event, fields = journal.events[0]
    assert event == "route_escalation", (
        "must NOT reuse 'escalation' — that name means handed-to-a-human and "
        "feeds the escalation-rate guardrail"
    )
    assert fields["issue"] == 5
    assert (fields["from_effort"], fields["to_effort"]) == ("medium", "high")
    assert (fields["from_tier"], fields["to_tier"]) == ("standard", "complex")
    assert fields["model"] == "fable", "the split must be derivable from what RAN"
    assert fields["ceiling"] is True
    assert fields["recurrence"] is True


def test_a_repair_legs_event_reports_no_tier_because_it_chose_none():
    """The event feeds `group_by(.to_tier)` — CLAUDE.md's load-bearing routing
    guardrail. A repair leg keeps its resumed session's model, so reporting the
    placeholder tier `_escalated` climbs from internally would attribute a
    sonnet leg to `complex` in exactly that split."""
    gw = FakeGateway(prs={100: _pr_with_rounds(4)}, issues={5: IssueView(5)})
    journal = RecordingJournal()
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(
        gw, FakeStore(), runner, journal=journal,
        fix=FixTaskConfig("o/r", agent_md="AM", model="sonnet"),
    ).address_review(100))

    assert runner.tasks[0].model == "sonnet"
    (event, fields), = journal.events
    assert event == "route_escalation"
    assert fields["notches"] >= 2, "enough evidence to have climbed a tier, had it one"
    assert (fields["from_tier"], fields["to_tier"]) == (None, None)
    assert fields["model"] == "sonnet", "what actually ran"
    assert (fields["from_effort"], fields["to_effort"]) == ("medium", "high")


def test_hard_history_on_an_unpinned_repair_preserves_its_stored_model_and_journal():
    """A Codex Terra session must not silently become Sol, even after two
    failures: the ceiling selects a fresh resolve model, never a resumed one."""
    gw = FakeGateway(prs={100: _pr_with_rounds(1)}, issues={5: IssueView(5)})
    store = FakeStore()
    store.models[5] = "gpt-5.6-terra"
    store.set_resolve_retries(5, 2)
    journal = RecordingJournal()
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(
        gw, store, runner, fixer_runtime="codex", journal=journal,
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).address_review(100))

    assert runner.tasks[0].model == "gpt-5.6-terra"
    (event, fields), = journal.events
    assert event == "route_escalation"
    assert (fields["from_tier"], fields["to_tier"]) == (None, None)
    assert fields["model"] == "gpt-5.6-terra"
    assert fields["ceiling"] is False


def test_the_kill_switch_costs_no_gateway_read_on_a_repair_leg():
    """With escalation off nothing reads the labels, so the recurrence fetch is
    pure waste — one `gh issue view` per repair dispatch, forever."""

    class CountingGateway(FakeGateway):
        issue_reads = 0

        def get_issue(self, number):
            type(self).issue_reads += 1
            return super().get_issue(number)

    gw = CountingGateway(prs={100: _pr_with_rounds(4)}, issues={5: IssueView(5)})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(
        gw, FakeStore(), runner, escalate_on_evidence=False,
        fix=FixTaskConfig("o/r", agent_md="AM", model="sonnet"),
    ).address_review(100))

    assert CountingGateway.issue_reads == 0
    assert runner.tasks[0].effort is None


def test_the_resume_leg_does_not_refetch_the_issue_it_already_holds():
    class CountingGateway(FakeGateway):
        issue_reads = 0

        def get_issue(self, number):
            type(self).issue_reads += 1
            return super().get_issue(number)

    iss = IssueView(
        5, title="t", body="b",
        comments=(Comment("a-human", "please continue", "2026-01-02T00:00:00Z"),),
    )
    gw = CountingGateway(issues={5: iss}, pr_for_issue={})
    runner = FakeRunner(AgentResult(is_error=False, status="pr_opened", session_id="s5"))

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM", model="sonnet"),
    ).resume(5))

    assert CountingGateway.issue_reads == 1, "the resume leg already holds the IssueView"


def test_nothing_is_journalled_when_nothing_escalated():
    gw = FakeGateway(issues={5: _issue()}, pr_for_issue={})
    journal = RecordingJournal()
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, journal=journal,
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert journal.events == []


def test_a_broken_journal_never_fails_a_dispatch():
    class ExplodingJournal:
        def emit(self, *a, **k):
            raise OSError("journal disk full")

    gw = FakeGateway(
        issues={5: _issue(labels=frozenset({Label.RECURRENCE}))}, pr_for_issue={}
    )
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, journal=ExplodingJournal(),
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].effort is Effort.HIGH


# ── #21: repair legs inherit the resolve's routed model ──


def _failing_ci_pr():
    return FakeGateway(
        prs={100: PRView(100, head_ref_name="fix/5-token", closing_issue=5,
                         checks=(Check("unit", bucket="fail"),))},
    )


def test_fix_ci_inherits_the_resolves_routed_model():
    """The resolve routed to a tiered model; the repair leg resuming that
    session must run the SAME reasoner, not the configured default (#21)."""
    store = FakeStore()
    store.models[5] = "gpt-routed"
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))

    asyncio.run(_workflow(
        _failing_ci_pr(), store, runner,
        fix=FixTaskConfig("o/r", agent_md="AM"),  # unpinned — routing owns model
    ).fix_ci(100))

    assert runner.tasks[0].model == "gpt-routed"


def test_fix_ci_pinned_model_beats_the_stored_marker():
    store = FakeStore()
    store.models[5] = "gpt-routed"
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))

    asyncio.run(_workflow(_failing_ci_pr(), store, runner).fix_ci(100))  # model="m"

    assert runner.tasks[0].model == "m"


def test_fix_ci_without_marker_keeps_the_runtime_default():
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))

    asyncio.run(_workflow(
        _failing_ci_pr(), FakeStore(), runner,
        fix=FixTaskConfig("o/r", agent_md="AM"),
    ).fix_ci(100))

    assert runner.tasks[0].model is None


def test_resolve_persists_its_model_for_later_repair_legs():
    gw = FakeGateway(
        issues={5: IssueView(5, title="t", body="b")},
        prs={100: PRView(100)},
        pr_for_issue={5: 100},
    )
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="s5"))

    asyncio.run(_workflow(gw, store, runner).resolve(5))

    assert store.models[5] == "m"


# ── #40: the classify probe is its own leg, not a resolve ────────────────────
# The router run inherits the RESOLVE dispatch's journal context, so every routed
# issue used to write two indistinguishable `kind=RESOLVE` lines — one a 4-second
# ~$0.07 classify, one the real resolve. That inflated the RESOLVE bucket of any
# `group_by(.kind)` cost mix by one entry per dispatch and was misread as a
# completed resolve during live babysitting.


def _journalled_resolve(tmp_path, runner):
    """Run a routed resolve through the real JournalingRuntime and return
    attributed agent-run telemetry."""
    from tests.fakes.legacy_journal import Journal
    from issue_agent.adapters.journal import JournalingRuntime, read_events

    journal = Journal(str(tmp_path / "journal.jsonl"))
    rt = JournalingRuntime(runner, journal, repo="o/r", role="fix")
    rt.set_context("fix", "RESOLVE", 5, 5)

    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    asyncio.run(_workflow(
        gw, FakeStore(), rt, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    runs = [e for e in read_events(journal.path) if e["event"] == "agent_run"]
    return runs


def test_the_router_leg_journals_as_route_and_the_resolve_as_resolve(tmp_path):
    runs = _journalled_resolve(tmp_path, SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5", cost_usd=3.0),
    ]))

    assert [r["kind"] for r in runs] == ["ROUTE", "RESOLVE"], (
        "the classify probe must not be counted as a resolve"
    )


def test_a_route_leg_keeps_its_cost_in_run_telemetry(tmp_path):
    """Renaming the bucket must not erase its cost attribution."""
    runs = _journalled_resolve(tmp_path, SeqRunner([
        AgentResult(is_error=False, session_id="rt", cost_usd=0.07,
                    structured={"tier": "standard"}),
        AgentResult(is_error=False, status="pr_opened", session_id="s5", cost_usd=3.0),
    ]))

    assert [(run["kind"], run["cost_usd"]) for run in runs] == [
        ("ROUTE", 0.07), ("RESOLVE", 3.0),
    ]
    assert all(run["issue"] == 5 for run in runs)


def test_the_route_leg_stays_inside_the_dispatch_that_ran_it(tmp_path):
    """One dispatch, one `dispatch` id — `unsettled_starts` counts one death per
    dispatch, so a classify probe that minted its own id would charge a daemon
    restart twice."""
    runs = _journalled_resolve(tmp_path, SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5", cost_usd=3.0),
    ]))

    assert len({r["dispatch"] for r in runs}) == 1


def test_the_resolve_after_a_dead_router_is_still_journalled_as_resolve(tmp_path):
    """The kind override must unwind on the failure path too — routing is
    advisory, and a raised classify must not leave the dispatch renamed."""
    class DeadRouter:
        def __init__(self):
            self.calls = 0

        async def run(self, task):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("router transport died")
            return AgentResult(is_error=False, status="pr_opened",
                               session_id="s5", cost_usd=3.0)

    runs = _journalled_resolve(tmp_path, DeadRouter())

    assert [r["kind"] for r in runs] == ["ROUTE", "RESOLVE"]
    assert [r["kind"] for r in runs if r["cost_usd"] > 0] == ["RESOLVE"]


def test_a_runner_that_cannot_name_its_legs_still_resolves():
    """An undecorated runner has no `leg_kind`; a journal nicety must never be
    able to kill a dispatch."""
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    runner = SeqRunner([
        _router_result("standard"),
        AgentResult(is_error=False, status="pr_opened", session_id="s5"),
    ])

    asyncio.run(_workflow(
        gw, FakeStore(), runner, fix=FixTaskConfig("o/r", agent_md="AM"),
    ).resolve(5))

    assert runner.tasks[1].model == "sonnet"


def test_fix_ci_persists_structured_publication_block_for_reconciliation(tmp_path):
    pr = PRView(
        100,
        head_ref_name="fix/5-token",
        head_ref_oid="blocked-head",
        closing_issue=5,
        checks=(Check("unit", bucket="fail"),),
    )
    store = Store(str(tmp_path))
    result = AgentResult(
        is_error=False,
        status="blocked",
        needs="publication denied",
        structured={"blocker": "publication"},
    )

    asyncio.run(
        _workflow(FakeGateway(prs={100: pr}), store, FakeRunner(result)).fix_ci(100)
    )

    marker = store.get_machine_block(5)
    assert marker is not None
    assert marker.blocker.value == "publication"
    assert marker.blocked_head == "blocked-head"


def test_fix_ci_persists_harness_budget_facts_for_re_evaluation(tmp_path):
    pr = PRView(
        100,
        head_ref_name="fix/5-token",
        head_ref_oid="head",
        closing_issue=5,
        checks=(Check("unit", bucket="fail"),),
    )
    store = Store(str(tmp_path))
    result = AgentResult(
        is_error=True,
        status="error_max_budget_usd",
        exit_reason=ExitReason.BUDGET_EXHAUSTED,
        structured={
            "budget_block": {
                "scope": "issue:5",
                "reservation_micros": 2_000_000,
                "old_headroom_micros": 250_000,
                "currency": "USD",
            }
        },
        admission_budget_block=AdmissionBudgetBlock(
            "issue:5", 2_000_000, 5_000_000, 4_750_000, "USD"
        ),
    )

    asyncio.run(
        _workflow(FakeGateway(prs={100: pr}), store, FakeRunner(result)).fix_ci(100)
    )

    assert store.get_budget_block(5) == BudgetBlock(
        5, Target.PR, 100, 2_000_000, "issue:5", 5_000_000, 250_000, "USD"
    )


def test_in_run_budget_kill_does_not_create_a_re_evaluable_marker(tmp_path):
    pr = PRView(
        100,
        head_ref_name="fix/5-token",
        head_ref_oid="head",
        closing_issue=5,
        checks=(Check("unit", bucket="fail"),),
    )
    store = Store(str(tmp_path))
    result = AgentResult(
        is_error=True,
        status="error_max_budget_usd",
        exit_reason=ExitReason.BUDGET_EXHAUSTED,
        # Even a forged agent payload is not admission authority.
        structured={
            "budget_block": {
                "scope": "issue:5",
                "reservation_micros": 2_000_000,
                "cap_micros": 5_000_000,
            }
        },
    )

    asyncio.run(
        _workflow(FakeGateway(prs={100: pr}), store, FakeRunner(result)).fix_ci(100)
    )

    assert store.get_budget_block(5) is None
