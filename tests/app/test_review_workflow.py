"""ReviewWorkflow wiring tests."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.journal import (
    JournalingRuntime,
    _legacy_event_record,
    read_events,
)
from issue_agent.domain.events import Correlation, TelemetryWarning
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.planners.schemas import (
    REVIEW_SCHEMA,
    review_schema_with_exemptions,
)
from issue_agent.domain.exemptions import (
    ExemptionDeclaration,
    ExemptionFacts,
    exemptions_permit_merge,
    parse_exemption_grants,
    render_grant_line,
)
from issue_agent.domain.review import IdentityMode
from issue_agent.app.workflows.review import ReviewWorkflow, ReviewWorkflowConfig
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.review import (
    ReviewMode,
    ReviewState,
    needs_re_review,
    patch_signature,
)
from issue_agent.app.planners.tasks import ReviewTaskConfig
from issue_agent.app.planners.tasks import REVIEW_DIMENSIONS
from issue_agent.domain.world import PRView, PullRequestDiff, Review

from test_dispatch import FakeGateway, FakeRunner, FakeStore, SchemaRunner, _dim, _gate, _val


class FakeJournal:
    """Records what the write funnel emitted. `escalation` is the event an
    operator is paged on (Executor emits it for any move to needs-human-review)."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        if not isinstance(event, str):
            event, fields = _legacy_event_record(event)
        self.events.append((event, fields))


def _workflow(gw, store, runner, journal=None, **cfg_overrides):
    cfg_kwargs = dict(
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER"),
        review_mode=ReviewMode.SINGLE,
        max_review_rejections=4,
        review_bot="review-bot",
        review_trigger_label="ai-pr-open",
        log=lambda _m: None,
        journal=journal,
    )
    cfg_kwargs.update(cfg_overrides)
    if cfg_kwargs.get("reviewer_runtime") == "codex":
        cfg_kwargs.setdefault("execution_cwd", "/configured-review-checkout")
        cfg_kwargs.setdefault("prepare_cwd", lambda _cwd: None)
    cfg = ReviewWorkflowConfig(**cfg_kwargs)
    # The REAL Executor, not a spy: M10's whole point is that escalation must
    # travel the same funnel every other write does (journal event + ALERT_CMD
    # come from the funnel, not from the workflow).
    return ReviewWorkflow(cfg, gw, store, runner, Executor(gw, journal=journal))


def test_initial_review_skips_when_our_verdict_already_targets_head():
    review = Review(
        "APPROVED", "review-bot[bot]", "t1", commit_id="abc",
    )
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc", reviews=(review,))})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False))
    journal = FakeJournal()

    asyncio.run(_workflow(gw, store, runner, journal=journal).review(100))

    assert runner.tasks == []
    assert gw.calls == []
    assert store.get_last_sha(100) == "abc"
    assert store.get_pr_session(100) == "github-review"
    assert ("review_skip", {
        "pr": 100,
        "reviewer": "review-bot",
        "head_sha": "abc",
        "reason": "reviewer-verdict-on-head",
    }) in journal.events


def test_re_review_skips_same_head_but_reviews_a_genuinely_new_head():
    prior = Review("CHANGES_REQUESTED", "review-bot", "t1", commit_id="old")
    gw = FakeGateway(prs={
        100: PRView(100, head_ref_oid="new", reviews=(prior,)),
    })
    store = FakeStore()
    store.save_pr_session(100, "rs")
    store.set_last_sha(100, "old")
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={"decision": "APPROVE", "body": "fixed"},
    ))

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert len(runner.tasks) == 1
    assert any(call[0] == "review" for call in gw.calls)


def test_external_review_marker_never_becomes_a_resume_session():
    prior = Review(
        "APPROVED", "review-bot", "t1",
        body="check the null-token contract", commit_id="old",
    )
    gw = FakeGateway(prs={
        100: PRView(
            100, head_ref_oid="old", title="Null token", body="Fixes #7",
            reviews=(prior,),
        ),
    })
    store = FakeStore()
    first_runner = FakeRunner(AgentResult(is_error=False))
    asyncio.run(_workflow(gw, store, first_runner).review(100))

    gw._prs[100] = PRView(
        100, head_ref_oid="new", title="Null token", body="Fixes #7",
        reviews=(prior,),
    )
    next_runner = FakeRunner(AgentResult(
        is_error=False,
        session_id="real-session",
        structured={"decision": "APPROVE", "body": "new head reviewed"},
    ))
    asyncio.run(_workflow(gw, store, next_runner).re_review(100))

    assert len(next_runner.tasks) == 1
    assert not next_runner.tasks[0].resume
    assert "Null token" in next_runner.tasks[0].prompt
    assert "Fixes #7" in next_runner.tasks[0].prompt
    assert "check the null-token contract" in next_runner.tasks[0].prompt
    assert store.get_pr_session(100) == "real-session"


def test_codex_reviewer_re_reviews_in_a_fresh_read_only_session():
    """`codex exec resume` has no sandbox flag, so retaining its session id
    would silently restore danger-full-access on a re-review."""
    prior = Review("CHANGES_REQUESTED", "review-bot", "t1", body="fix it", commit_id="old")
    old = PRView(100, head_ref_oid="old", title="t", body="b")
    gw = FakeGateway(prs={100: old})
    store = FakeStore()
    first = FakeRunner(AgentResult(
        is_error=False, session_id="codex-session",
        structured={"decision": "REQUEST_CHANGES", "body": "fix it"},
    ))

    asyncio.run(_workflow(gw, store, first, reviewer_runtime="codex").review(100))
    assert store.get_pr_session(100) == "codex-review"

    gw._prs[100] = PRView(100, head_ref_oid="new", title="t", body="b", reviews=(prior,))
    setattr(
        gw,
        "pr_diff",
        lambda _pr, base, head: PullRequestDiff(
            base, head, "new diff"
        ),
    )
    second = FakeRunner(AgentResult(
        is_error=False, session_id="new-codex-session",
        structured={"decision": "APPROVE", "body": "fixed"},
    ))
    asyncio.run(_workflow(gw, store, second, reviewer_runtime="codex").re_review(100))

    assert second.tasks[0].resume is None
    assert store.get_pr_session(100) == "codex-review"


def test_codex_review_installs_its_hook_in_the_explicit_checkout_before_running():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={"decision": "APPROVE", "body": "looks good"},
    ))
    prepared: list[str] = []

    asyncio.run(_workflow(
        gw, store, runner, reviewer_runtime="codex",
        execution_cwd="/explicit/review-checkout", prepare_cwd=prepared.append,
    ).review(100))

    assert prepared == ["/explicit/review-checkout"]
    assert runner.tasks[0].cwd == "/explicit/review-checkout"


def test_codex_review_refuses_an_implicit_process_cwd():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False))
    logs: list[str] = []

    asyncio.run(_workflow(
        gw, store, runner, reviewer_runtime="codex", execution_cwd="", log=logs.append,
    ).review(100))

    assert runner.tasks == []
    assert any("set TARGET_DIR" in line for line in logs)


def test_single_review_submits_before_persisting_state():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        session_id="rs",
        structured={"decision": "APPROVE", "body": "looks good"},
    ))

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert any(c[0] == "review" and c[2] == "APPROVE" for c in gw.calls)
    assert store.pr_sessions[100] == "rs"
    assert store.last_sha[100] == "abc"
    assert store.rounds[100] == 1


def test_codex_shaped_review_verdict_submits_with_attributed_usage(tmp_path):
    """The Codex runner's schema-normalised result must travel through the
    ordinary REVIEW_SCHEMA submission seam and leave a queryable review ledger
    receipt; neither assertion is a constructor-level proxy."""
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    journal = Journal(str(tmp_path / "review-journal.jsonl"))
    runner = JournalingRuntime(
        FakeRunner(AgentResult(
            is_error=False,
            session_id="codex-session",
            model="gpt-5.6-terra",
            cost_usd=1.25,
            usage={"input_tokens": 10, "output_tokens": 20},
            structured={"decision": "APPROVE", "body": "looks good"},
        )),
        journal,
        repo="o/r",
        role="review",
        runtime="codex",
    )
    runner.set_context("review", "REVIEW", 100, None)

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert runner.inner.tasks[0].schema == REVIEW_SCHEMA
    assert any(call[:3] == ("review", 100, "APPROVE") for call in gw.calls)
    run = [
        event
        for event in read_events(journal.path)
        if event["event"] == "agent_run"
    ][0]
    assert run["role"] == "review"
    assert run["runtime"] == "codex"
    assert run["model"] == "gpt-5.6-terra"
    assert run["usage"] == {
        "input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }


def test_successful_submit_logs_verdict():
    """A clean review must announce its OUTCOME on stdout — not go silent between
    `dispatch` and the next poll. Regression for the observability gap where only
    the error path logged, so an APPROVE was invisible except on GitHub."""
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False, session_id="rs",
        structured={"decision": "APPROVE", "body": "looks good"},
    ))
    logs: list[str] = []

    asyncio.run(_workflow(gw, store, runner, log=logs.append).review(100))

    assert any("review #100 r1: submitted APPROVE" in m for m in logs)


def test_single_review_failed_submit_does_not_persist_state():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr}, submit_ok=False)
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        session_id="rs",
        structured={"decision": "APPROVE", "body": "looks good"},
    ))

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert any(c[0] == "review" for c in gw.calls)
    assert 100 not in store.pr_sessions
    assert 100 not in store.last_sha


def test_single_review_never_submits_stale_verdict_to_new_head():
    original = PRView(100, head_ref_oid="head-1", title="t", body="b")
    gw = FakeGateway(prs={100: original})
    store = FakeStore()

    class AdvancingRunner(FakeRunner):
        async def run(self, task):
            self.tasks.append(task)
            gw._prs[100] = PRView(
                100, head_ref_oid="head-2", title="t", body="b"
            )
            return self.result

    runner = AdvancingRunner(
        AgentResult(
            is_error=False,
            session_id="stale-session",
            structured={
                "decision": "APPROVE",
                "body": "verdict for head-1",
            },
        )
    )

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert not any(call[0] == "review" for call in gw.calls)
    assert 100 not in store.pr_sessions
    assert 100 not in store.last_sha
    assert 100 not in store.rounds


def test_single_review_error_submits_nothing():
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=True))

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert gw.calls == []
    assert store.pr_sessions == {}


def test_delegated_review_uses_scout_backed_task_and_submits():
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="sha1")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={"decision": "APPROVE", "body": "lgtm"},
        session_id="rs",
    ))

    asyncio.run(_workflow(
        gw,
        store,
        runner,
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER", scout_model="sonnet"),
        review_mode=ReviewMode.DELEGATED,
    ).review(100))

    task = runner.tasks[-1]
    assert task.subagents and task.subagents[0].name == "code-scout"
    assert "Task" in task.all_allowed_tools()
    assert any(c[0] == "review" for c in gw.calls)
    assert store.pr_sessions.get(100) == "rs"


def test_delegated_re_review_uses_prior_review_without_session_resume():
    pr = PRView(
        100,
        head_ref_oid="new",
        title="t",
        body="b",
        reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1", body="fix this"),),
    )
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.set_round(100, 1)
    store.set_last_sha(100, "old")
    store.save_pr_session(100, "old-session")
    runner = FakeRunner(AgentResult(
        is_error=False,
        session_id="new-session",
        structured={"decision": "APPROVE", "body": "fixed"},
    ))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.DELEGATED).re_review(100))

    assert runner.tasks[0].resume is None
    assert "fix this" in runner.tasks[0].prompt
    assert store.last_sha[100] == "new"
    assert store.rounds[100] == 2


def test_re_review_failed_submit_does_not_advance_round():
    pr = PRView(100, head_ref_oid="new", title="t", body="b")
    gw = FakeGateway(prs={100: pr}, submit_ok=False)
    store = FakeStore()
    store.set_round(100, 2)
    store.save_pr_session(100, "rs")
    store.set_last_sha(100, "old")
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={"decision": "REQUEST_CHANGES", "body": "x"},
    ))

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert store.last_sha[100] == "old"
    assert store.rounds[100] == 2


def test_re_review_escalation_uses_configured_trigger_label():
    # review M10: the removed label follows REVIEW_TRIGGER_LABEL (ai-review on a
    # repo where another bot owns ai-pr-open) — removing the wrong one leaves the
    # PR in the reviewer's pickable set forever.
    pr = PRView(815, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={815: pr})
    store = FakeStore()
    store.set_round(815, 5)
    store.save_pr_session(815, "rs")
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={"decision": "ESCALATE", "body": "stuck"},
    ))

    asyncio.run(_workflow(
        gw,
        store,
        runner,
        review=ReviewTaskConfig("o/ext", review_prompt="round=$ROUND_NUMBER"),
        review_trigger_label="ai-review",
    ).re_review(815))

    assert any(c[0] == "review" and c[2] == "COMMENT" for c in gw.calls)
    assert ("relabel", "pr", 815, "ai-review", "needs-human-review") in gw.calls


# ── review M10: escalations travel the write funnel ───────────────────────────
def test_review_escalation_emits_an_escalation_event_through_the_funnel():
    """M10: the reviewer giving up is the state change an operator most needs to
    hear about, and it was the ONE escalation that bypassed the funnel — a raw
    `gateway.relabel(..., "needs-human-review")`, so no `escalation` journal
    event and no ALERT_CMD, while the spend-cap escalation on the same daemon
    alerted. Emitted by Executor, so asserting on the journal proves the write
    actually went through it."""
    prior = tuple(
        Review("CHANGES_REQUESTED", "review-bot", f"t{i}", body="fix") for i in range(3)
    )
    pr = PRView(100, head_ref_oid="new", title="t", body="b", reviews=prior)
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.set_round(100, 4)
    store.save_pr_session(100, "rs")
    journal = FakeJournal()
    runner = FakeRunner(AgentResult(
        is_error=False, structured={"decision": "REQUEST_CHANGES", "body": "still broken"},
    ))

    asyncio.run(_workflow(gw, store, runner, journal=journal).re_review(100))

    assert ("relabel", "pr", 100, "ai-pr-open", "needs-human-review") in gw.calls
    assert ("escalation", {"target": "pr", "number": 100}) in journal.events
    # …and the funnel's comment explains WHY, so the human arrives informed.
    assert any(c[0] == "comment" and "Circuit breaker" in c[3] for c in gw.calls)
    # A ceiling trip names the ceiling, in the breaker comment itself.
    breaker = [c[3] for c in gw.calls if c[0] == "comment" and "Circuit breaker" in c[3]]
    assert breaker and "requested changes" in breaker[0] and "ceiling 4" in breaker[0]


def test_verdict_escalation_reason_names_the_verdict_not_the_ceiling():
    """keifu PR #177 (2026-08-02): sol returned ESCALATE on round 3; the breaker
    comment said "requested changes 3 time(s) ... (ceiling 4)", which reads as a
    fencepost bug and hands the dashboard's escalation_reason the wrong WHY."""
    pr = PRView(100, head_ref_oid="new", title="t", body="b",
                reviews=(Review("CHANGES_REQUESTED", "review-bot", "t0", body="fix"),))
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.set_round(100, 2)
    store.save_pr_session(100, "rs")
    runner = FakeRunner(AgentResult(
        is_error=False, structured={"decision": "ESCALATE", "body": "deadlocked"},
    ))

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert ("relabel", "pr", 100, "ai-pr-open", "needs-human-review") in gw.calls
    breaker = [c[3] for c in gw.calls if c[0] == "comment" and "Circuit breaker" in c[3]]
    assert breaker and "returned ESCALATE" in breaker[0]
    assert "requested changes" not in breaker[0]


def test_ordinary_rejection_does_not_escalate():
    """Anti-vacuity twin: below the ceiling the reviewer bounces the PR back to
    the fixer — no label move, no escalation event, nobody paged."""
    pr = PRView(100, head_ref_oid="new", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.set_round(100, 2)
    store.save_pr_session(100, "rs")
    journal = FakeJournal()
    runner = FakeRunner(AgentResult(
        is_error=False, structured={"decision": "REQUEST_CHANGES", "body": "fix this"},
    ))

    asyncio.run(_workflow(gw, store, runner, journal=journal).re_review(100))

    assert any(c[0] == "review" and c[2] == "REQUEST_CHANGES" for c in gw.calls)
    assert not any(c[0] == "relabel" for c in gw.calls)
    assert [e for e, _ in journal.events if e == "escalation"] == []


def test_multi_review_escalation_also_goes_through_the_funnel():
    """The multi-agent path had its own copy of the raw relabel (review.py:240)
    — both had to move, or the leak survives behind REVIEW_MODE."""
    prior = tuple(
        Review("CHANGES_REQUESTED", "review-bot", f"t{i}", body="fix") for i in range(3)
    )
    pr = PRView(100, head_ref_oid="new", title="t", body="b", reviews=prior)
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.set_round(100, 4)
    store.save_pr_session(100, "multi")
    journal = FakeJournal()
    runner = SchemaRunner(
        gate=_gate(),
        dimension=lambda t: _dim([{"location": "a.py:1", "claim": "bug",
                                   "severity": "blocking"}]),
        validation=lambda t: _val(True),
    )

    asyncio.run(_workflow(gw, store, runner, journal=journal,
                          review_mode=ReviewMode.MULTI).re_review(100))

    assert ("relabel", "pr", 100, "ai-pr-open", "needs-human-review") in gw.calls
    assert ("escalation", {"target": "pr", "number": 100}) in journal.events


def test_unknown_trigger_label_falls_back_instead_of_stranding_the_pr():
    """A misconfigured REVIEW_TRIGGER_LABEL must not raise AFTER the verdict has
    been POSTed — that would strand the PR mid-escalation (submitted, unlabelled,
    and re-picked next poll). Fall back loudly to ai-pr-open."""
    pr = PRView(815, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={815: pr})
    store = FakeStore()
    store.set_round(815, 5)
    store.save_pr_session(815, "rs")
    logs: list[str] = []
    runner = FakeRunner(AgentResult(
        is_error=False, structured={"decision": "ESCALATE", "body": "stuck"},
    ))

    asyncio.run(_workflow(gw, store, runner, review_trigger_label="not-a-label",
                          log=logs.append).re_review(815))

    assert ("relabel", "pr", 815, "ai-pr-open", "needs-human-review") in gw.calls
    assert any("not a known lifecycle label" in m for m in logs)


def test_multi_review_confirmed_blocking_finding_requests_changes_and_persists():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = SchemaRunner(
        gate=_gate(),
        dimension=lambda t: _dim([{"location": "a.py:1", "claim": "bug", "severity": "blocking"}]),
        validation=lambda t: _val(True),
    )

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert any(c[0] == "review" and c[2] == "REQUEST_CHANGES" for c in gw.calls)
    assert store.pr_sessions[100] == "multi"
    assert store.last_sha[100] == "abc"


def test_multi_review_gate_short_circuits_non_reviewable_diff_to_approve():
    pr = PRView(100, head_ref_oid="abc", title="Docs", body="typo fix")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = SchemaRunner(gate=_gate(reviewable=False, reason="docs only"))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert any(c[0] == "review" and c[2] == "APPROVE" for c in gw.calls)
    assert len(runner.tasks) == 1
    assert store.last_sha[100] == "abc"


def test_multi_review_degenerate_gate_runs_full_review_not_approve():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = SchemaRunner(
        gate=AgentResult(is_error=False, structured={}),
        dimension=lambda t: _dim([]),
        validation=lambda t: _val(True),
    )

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert len(runner.tasks) == 1 + len(REVIEW_DIMENSIONS)
    assert any(c[0] == "review" and c[2] == "APPROVE" for c in gw.calls)


def test_multi_review_all_findings_refuted_approves():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = SchemaRunner(
        gate=_gate(),
        dimension=lambda t: _dim([{
            "location": "a.py:1",
            "claim": "maybe",
            "severity": "blocking",
        }]),
        validation=lambda t: _val(False, "not reachable"),
    )

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert any(c[0] == "review" and c[2] == "APPROVE" for c in gw.calls)


def test_multi_review_no_findings_approves_without_validation():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = SchemaRunner(
        gate=_gate(),
        dimension=lambda t: _dim([]),
        validation=lambda t: _val(True),
    )

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert any(c[0] == "review" and c[2] == "APPROVE" for c in gw.calls)
    assert len(runner.tasks) == 1 + len(REVIEW_DIMENSIONS)


def test_multi_review_gate_error_persists_nothing():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = SchemaRunner(gate=AgentResult(is_error=True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert gw.calls == []
    assert 100 not in store.last_sha


def test_multi_review_failed_submit_does_not_persist():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr}, submit_ok=False)
    store = FakeStore()
    runner = SchemaRunner(
        gate=_gate(),
        dimension=lambda t: _dim([]),
        validation=lambda t: _val(True),
    )

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert any(c[0] == "review" for c in gw.calls)
    assert 100 not in store.last_sha


def test_multi_review_incomplete_no_finding_submits_nothing():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    calls = {"n": 0}

    def dim(_task):
        calls["n"] += 1
        if calls["n"] == 1:
            return AgentResult(is_error=True)
        return _dim([])

    runner = SchemaRunner(gate=_gate(), dimension=dim, validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert gw.calls == []
    assert 100 not in store.last_sha


def test_multi_review_incomplete_but_blocking_still_requests_changes():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    calls = {"n": 0}

    def dim(_task):
        calls["n"] += 1
        if calls["n"] == 1:
            return AgentResult(is_error=True)
        return _dim([{"location": "a.py:1", "claim": "bug", "severity": "blocking"}])

    runner = SchemaRunner(gate=_gate(), dimension=dim, validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert any(c[0] == "review" and c[2] == "REQUEST_CHANGES" for c in gw.calls)


def test_multi_review_validator_error_marks_incomplete():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    seen = []

    def dim(_task):
        if seen:
            return _dim([])
        seen.append(True)
        return _dim([{"location": "a.py:1", "claim": "maybe", "severity": "blocking"}])

    runner = SchemaRunner(
        gate=_gate(),
        dimension=dim,
        validation=lambda t: AgentResult(is_error=True),
    )

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert gw.calls == []
    assert 100 not in store.last_sha


def test_multi_review_leg_that_raises_does_not_abort_review():
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    calls = {"n": 0}

    def dim(_task):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("stream closed")
        return _dim([])

    runner = SchemaRunner(gate=_gate(), dimension=dim, validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert gw.calls == []
    assert 100 not in store.last_sha


def test_multi_re_review_at_ceiling_escalates():
    # Ceiling is driven by the reviewer's OWN prior rejections, not the round
    # counter: 3 prior CHANGES_REQUESTED verdicts → this 4th blocking review (cap 4)
    # can no longer bounce back, it escalates.
    prior = tuple(
        Review("CHANGES_REQUESTED", "review-bot", f"t{i}", body="fix") for i in range(3)
    )
    pr = PRView(100, head_ref_oid="new", title="t", body="b", reviews=prior)
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.set_round(100, 4)
    store.save_pr_session(100, "multi")
    runner = SchemaRunner(
        gate=_gate(),
        dimension=lambda t: _dim([{
            "location": "a.py:1",
            "claim": "bug",
            "severity": "blocking",
        }]),
        validation=lambda t: _val(True),
    )

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).re_review(100))

    assert any(c[0] == "review" and c[2] == "COMMENT" for c in gw.calls)
    assert ("relabel", "pr", 100, "ai-pr-open", "needs-human-review") in gw.calls
    assert store.rounds[100] == 5


# ── the no-progress counter (review P0R-3) ────────────────────────────────────
# Every `return` above that skips save_pr_session/set_last_sha leaves
# `needs_initial_review`/`needs_re_review` True, so the IDENTICAL dispatch is
# bought again next poll — under MULTI that is a gate + 5 dimensions + N
# validators re-run on a RESULT the workflow chose not to use. None of them
# raise, so the dispatcher's `except BaseException` bump never sees them. These
# pin the write half of the ceiling: the read half lives in
# tests/adapters/test_roles.py, which drives it through decide_review.
def _counter(store, pr_number=100):
    """The stable PR-keyed review convergence counter."""
    return store.get_infra_deaths(-pr_number)


def _bumps(store) -> int:
    return sum(store.infra_deaths.values())


def test_a_healthy_review_counts_no_unproductive_round():
    """Anti-vacuity, and the one that matters most: a counter that ticks on the
    happy path escalates every PR the reviewer ever succeeds on."""
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc", title="t", body="b")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, session_id="rs",
                                    structured={"decision": "APPROVE", "body": "lgtm"}))

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert _bumps(store) == 0


def test_a_healthy_multi_review_counts_no_unproductive_round():
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc", title="t", body="b")})
    store = FakeStore()
    runner = SchemaRunner(gate=_gate(), dimension=lambda t: _dim([]),
                          validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert any(c[0] == "review" and c[2] == "APPROVE" for c in gw.calls)
    assert _bumps(store) == 0


def test_an_errored_review_counts_one_unproductive_round():
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc")})
    store = FakeStore()

    asyncio.run(_workflow(gw, store, FakeRunner(AgentResult(is_error=True))).review(100))

    assert _counter(store) == 1
    assert _bumps(store) == 1  # exactly one — no second key was written


def test_a_failed_review_post_counts_one_unproductive_round():
    """Money-identical to an agent error: the verdict exists but nothing
    persists, so the whole session is bought again next poll."""
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc")}, submit_ok=False)
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, session_id="rs",
                                    structured={"decision": "APPROVE", "body": "lgtm"}))

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert 100 not in store.pr_sessions and _counter(store) == 1


def test_an_errored_re_review_counts_one_unproductive_round():
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="def")})
    store = FakeStore()
    store.save_pr_session(100, "rs")
    store.set_last_sha(100, "abc")

    asyncio.run(_workflow(gw, store, FakeRunner(AgentResult(is_error=True))).re_review(100))

    assert store.last_sha[100] == "abc" and _counter(store) == 1


def test_a_multi_review_gate_error_counts_one_unproductive_round():
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc", title="t", body="b")})
    store = FakeStore()
    runner = SchemaRunner(gate=AgentResult(is_error=True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert _counter(store) == 1


def test_a_multi_fanout_that_synthesises_nothing_counts_one_unproductive_round():
    """The headline P0R-3 shape: the gate SUCCEEDS, the dimension legs die,
    `synthesize_review` returns None and the workflow returns — a RETURNED
    result, so nothing on the raise path ever sees it, and the entire fan-out
    re-runs every poll."""
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc", title="t", body="b")})
    store = FakeStore()
    runner = SchemaRunner(gate=_gate(), dimension=lambda t: AgentResult(is_error=True),
                          validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert gw.calls == [], "nothing was submitted — the round bought nothing"
    assert _counter(store) == 1


def test_a_multi_leg_that_raises_is_counted_by_the_workflow_not_the_dispatcher():
    """`_gather_legs` coerces a raised leg to an errored result, so the raise
    NEVER reaches `AgentDispatcher.dispatch`'s counter — the workflow returns
    normally. Without this bump that death is bounded by nothing at all."""
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc", title="t", body="b")})
    store = FakeStore()

    def dim(_task):
        raise RuntimeError("stream closed before any result")

    runner = SchemaRunner(gate=_gate(), dimension=dim, validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert _counter(store) == 1


def test_the_counter_key_stays_with_the_pr_when_it_has_a_linked_issue():
    """Review retry identity is independent of mutable issue linkage.

    The dispatcher's raised-session path and the workflow's returned-error path
    must both charge the stable PR key or relinking can reset the ceiling.
    """
    pr = PRView(100, head_ref_oid="abc", head_ref_name="fix/42-x", closing_issue=42)
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()

    asyncio.run(_workflow(gw, store, FakeRunner(AgentResult(is_error=True))).review(100))

    assert store.infra_deaths == {-100: 1}
    assert gw.issue_for_pr(100) == 42  # the key the dispatcher would have used


def test_rounds_accumulate_across_polls():
    """One counter, many polls: the ceiling is only a ceiling if the count
    survives the poll that produced it."""
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc")})
    store = FakeStore()
    wf = _workflow(gw, store, FakeRunner(AgentResult(is_error=True)))

    for _ in range(3):
        asyncio.run(wf.review(100))

    assert _counter(store) == 3


# ── unproductive-round decay (reviewer twin of review P1R-3) ───────────────────
def test_a_confirmed_submit_clears_the_unproductive_round_tally():
    """Without decay the P0R-3 ceiling is a LIFETIME tally: a long-lived PR that
    accumulates flaky rounds over weeks of healthy reviewing escalates on its
    next needed review, having never looped. A confirmed submit proves the PR is
    not stuck, so it resets the counter."""
    pr = PRView(100, head_ref_oid="abc", title="t", body="b", closing_issue=7)
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.infra_deaths[-100] = 4  # four earlier rounds bought nothing
    runner = FakeRunner(AgentResult(
        is_error=False, session_id="rs",
        structured={"decision": "APPROVE", "body": "looks good"},
    ))

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert store.get_infra_deaths(-100) == 0, "a productive round must reset the tally"


def test_a_round_that_submits_nothing_still_counts():
    """Anti-vacuity twin: decay must not swallow the very rounds the ceiling
    exists to count — an errored round leaves the tally standing and growing."""
    pr = PRView(100, head_ref_oid="abc", title="t", body="b", closing_issue=7)
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.infra_deaths[-100] = 4
    runner = FakeRunner(AgentResult(is_error=True, status="error"))

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert store.get_infra_deaths(-100) == 5, "an unproductive round still counts up"


def test_a_productive_round_clears_the_orphaned_half_too():
    """A productive round clears both the stable key and legacy issue key.

    Deployments before ADR 0008 could write review deaths under either key.
    Migration reads and decay must therefore include both until those counters
    have naturally drained.
    """
    pr = PRView(100, head_ref_oid="abc", title="t", body="b", closing_issue=7)
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    store.infra_deaths[7] = 2
    store.infra_deaths[-100] = 3  # recorded while issue_for_pr was down
    runner = FakeRunner(AgentResult(
        is_error=False, session_id="rs",
        structured={"decision": "APPROVE", "body": "looks good"},
    ))

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert store.get_infra_deaths(7) == 0
    assert store.get_infra_deaths(-100) == 0, "the orphaned half must clear too"


# ── carrying an approval across a mechanical rebase (harness #2) ──────────────
# `needs_re_review` fires on any head-SHA advance, so a force-push that only
# rebased the branch bought a full paid re-review (~$5, 5-10 min) for a diff the
# reviewer had already approved. These tests pin the discriminator: the PATCH,
# not the SHA, decides whether a review agent runs.


class PatchStore(FakeStore):
    """FakeStore + the reviewed-patch accessors the real `Store` grew for #2."""

    def __init__(self):
        super().__init__()
        self.patch_ids = {}

    def get_last_patch_id(self, p):
        return self.patch_ids.get(p, "")

    def set_last_patch_id(self, p, s):
        self.patch_ids[p] = s


class DiffGateway(FakeGateway):
    """FakeGateway whose `pr_diff` is scriptable (the base fake returns a
    constant, which cannot express 'the diff changed')."""

    def __init__(self, diff, **kw):
        super().__init__(**kw)
        self.diff = diff
        self.diffs_fetched = 0

    def pr_diff(self, pr, base_oid, head_oid):
        self.diffs_fetched += 1
        return PullRequestDiff(base_oid, head_oid, self.diff)


_REVIEWED = (
    "diff --git a/app.py b/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -10,6 +10,7 @@ def handler(req):\n"
    "     ctx = build(req)\n"
    "+    guard(ctx)\n"
    "     return ctx.render()\n"
)
# Same change, rebased: new blob hashes, shifted offsets, re-derived heading.
_REBASED = (
    "diff --git a/app.py b/app.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -87,6 +87,7 @@ def handler(req, opts):\n"
    "     ctx = build(req)\n"
    "+    guard(ctx)\n"
    "     return ctx.render()\n"
)
_REVISED = _REBASED.replace("guard(ctx)", "guard(ctx, strict=True)")


def _approved(**kw):
    return AgentResult(
        is_error=False, session_id="rs",
        structured={"decision": "APPROVE", "body": "lgtm"}, **kw
    )


def _reviewed_pr(store, diff, head="old-sha"):
    """A PR that has ALREADY been reviewed once at `diff` — the state an initial
    review leaves behind."""
    store.save_pr_session(100, "rs")
    store.set_last_sha(100, head)
    store.set_round(100, 1)
    store.set_last_patch_id(100, patch_signature(diff))


def _rebased_pr(head: str = "new-sha", prior_head: str = "old-sha"):
    return PRView(
        100,
        head_ref_oid=head,
        base_ref_oid="base-sha",
        reviews=(
            Review(
                "APPROVED",
                "review-bot",
                "t1",
                body="lgtm",
                commit_id=prior_head,
            ),
        ),
    )


def test_a_rebase_that_does_not_change_the_patch_runs_no_review_agent():
    """THE acceptance criterion: a force-push whose diff is semantically
    identical must dispatch no paid review run at all."""
    store = PatchStore()
    _reviewed_pr(store, _REVIEWED)
    gw = DiffGateway(_REBASED, prs={100: _rebased_pr()})
    runner = FakeRunner(_approved())

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert runner.tasks == [], "no review agent may run for an unchanged patch"
    assert any(
        call[0] == "review" for call in gw.calls
    ), "the prior verdict is rebound to the new head"


def test_revision_mismatched_diff_never_runs_or_submits_review():
    class MovingDiffGateway(DiffGateway):
        def pr_diff(self, pr, base_oid, head_oid):
            self.diffs_fetched += 1
            # Models the h1→h2→h1 ABA: a PR-number read observed h2 even
            # though the workflow snapshot and later POST both see h1.
            return PullRequestDiff(base_oid, "h2", _REVISED)

    store = PatchStore()
    gw = MovingDiffGateway(
        _REVIEWED,
        prs={
            100: PRView(
                100,
                head_ref_oid="h1",
                base_ref_oid="base-sha",
            )
        },
    )
    runner = FakeRunner(_approved())

    asyncio.run(_workflow(gw, store, runner).review(100))

    assert runner.tasks == []
    assert not any(call[0] == "review" for call in gw.calls)
    assert store.get_last_sha(100) == ""


def test_the_carried_forward_skip_happens_once_not_every_poll():
    """`decide_review` re-emits RE_REVIEW for as long as `needs_re_review` is
    true, and that predicate reads the same `last-sha` the skip advances. Without
    this write the daemon would re-enter the skip on every single poll forever."""
    store = PatchStore()
    _reviewed_pr(store, _REVIEWED)
    gw = DiffGateway(_REBASED, prs={100: _rebased_pr()})

    asyncio.run(_workflow(gw, store, FakeRunner(_approved())).re_review(100))

    assert store.get_last_sha(100) == "new-sha"
    # needs_re_review's exact predicate: head == last_sha → nothing dispatched.
    assert needs_re_review(PRView(100, head_ref_oid="new-sha"),
                           ReviewState(sessions=frozenset({100}),
                                       last_sha={100: store.get_last_sha(100)})) is False


def test_the_skip_does_not_consume_a_review_round():
    # No round was reviewed, so the round counter (and the prior-review body the
    # next real re-review is given) must be untouched.
    store = PatchStore()
    _reviewed_pr(store, _REVIEWED)
    gw = DiffGateway(_REBASED, prs={100: _rebased_pr()})

    asyncio.run(_workflow(gw, store, FakeRunner(_approved())).re_review(100))

    assert store.get_round(100) == 1


def test_a_rebase_that_changes_the_patch_still_runs_the_review_agent():
    """The anti-vacuity twin: real work pushed on top of a rebase is reviewed."""
    store = PatchStore()
    _reviewed_pr(store, _REVIEWED)
    gw = DiffGateway(_REVISED, prs={100: PRView(100, head_ref_oid="new-sha")})
    runner = FakeRunner(_approved())

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert len(runner.tasks) == 1
    assert any(c[0] == "review" for c in gw.calls)
    assert store.get_last_sha(100) == "new-sha"
    assert store.get_round(100) == 2
    assert store.get_last_patch_id(100) == patch_signature(_REVISED), \
        "the newly reviewed patch becomes the baseline for the NEXT rebase"


def test_unchanged_patch_does_not_copy_verdict_from_unrelated_revision():
    store = PatchStore()
    _reviewed_pr(store, _REVIEWED, head="reviewed-sha")
    gw = DiffGateway(
        _REBASED,
        prs={100: _rebased_pr("current-sha", "unrelated-sha")},
    )
    runner = FakeRunner(_approved())

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert len(runner.tasks) == 1


def test_a_pr_with_no_recorded_patch_is_always_reviewed():
    """Pre-existing sessions (reviewed before this existed) have no baseline —
    unknown must mean 'review it', never 'unchanged'."""
    store = PatchStore()
    store.save_pr_session(100, "rs")
    store.set_last_sha(100, "old-sha")
    store.set_round(100, 1)
    gw = DiffGateway(_REBASED, prs={100: PRView(100, head_ref_oid="new-sha")})
    runner = FakeRunner(_approved())

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert len(runner.tasks) == 1
    assert store.get_last_patch_id(100) == patch_signature(_REBASED)


def test_an_initial_review_records_the_patch_it_reviewed():
    """So the FIRST rebase after the initial review is already skippable — the
    exact shape that cost three paid rounds on the live deployment."""
    store = PatchStore()
    gw = DiffGateway(_REVIEWED, prs={100: PRView(100, head_ref_oid="sha1")})

    asyncio.run(_workflow(gw, store, FakeRunner(_approved())).review(100))

    assert store.get_last_patch_id(100) == patch_signature(_REVIEWED)

    # …and the rebase that follows costs nothing.
    gw2 = DiffGateway(
        _REBASED,
        prs={100: _rebased_pr("sha2", "sha1")},
    )
    runner2 = FakeRunner(_approved())
    asyncio.run(_workflow(gw2, store, runner2).re_review(100))
    assert runner2.tasks == []


def test_a_review_that_never_submitted_records_no_patch_baseline():
    """State advances only on a confirmed submit — otherwise a failed POST would
    make the un-reviewed patch look reviewed and skip forever."""
    store = PatchStore()
    gw = DiffGateway(_REVIEWED, prs={100: PRView(100, head_ref_oid="sha1")},
                     submit_ok=False)

    asyncio.run(_workflow(gw, store, FakeRunner(_approved())).review(100))

    assert store.get_last_patch_id(100) == ""


def test_a_multi_agent_re_review_also_skips_an_unchanged_patch():
    """MULTI is the expensive path (gate + 5 dimensions + N validators), so the
    short-circuit must precede the fan-out there too."""
    store = PatchStore()
    _reviewed_pr(store, _REVIEWED)
    store.save_pr_session(100, "multi")
    gw = DiffGateway(_REBASED, prs={100: _rebased_pr()})
    runner = SchemaRunner(gate=_gate(), dimension=lambda t: _dim([]),
                          validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).re_review(100))

    assert runner.tasks == []
    assert any(call[0] == "review" for call in gw.calls)
    assert store.get_last_sha(100) == "new-sha"


def test_a_multi_agent_review_records_the_patch_it_reviewed():
    store = PatchStore()
    gw = DiffGateway(_REVIEWED, prs={100: PRView(100, head_ref_oid="abc")})
    runner = SchemaRunner(gate=_gate(), dimension=lambda t: _dim([]),
                          validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).review(100))

    assert store.get_last_patch_id(100) == patch_signature(_REVIEWED)


def test_a_delegated_re_review_also_skips_an_unchanged_patch():
    store = PatchStore()
    _reviewed_pr(store, _REVIEWED)
    gw = DiffGateway(_REBASED, prs={100: _rebased_pr()})
    runner = FakeRunner(_approved())

    asyncio.run(_workflow(gw, store, runner,
                          review_mode=ReviewMode.DELEGATED).re_review(100))

    assert runner.tasks == []


def test_the_skip_costs_exactly_one_diff_fetch_no_matter_the_mode():
    # The whole point is that the discriminator is cheap: one `gh pr diff`
    # replaces a paid round, and delegating to MULTI must not re-fetch it.
    store = PatchStore()
    _reviewed_pr(store, _REVISED)  # baseline differs → the fan-out DOES run
    gw = DiffGateway(_REBASED, prs={100: _rebased_pr()})
    runner = SchemaRunner(gate=_gate(), dimension=lambda t: _dim([]),
                          validation=lambda t: _val(True))

    asyncio.run(_workflow(gw, store, runner, review_mode=ReviewMode.MULTI).re_review(100))

    assert runner.tasks, "sanity: this patch DID change, so the review ran"
    assert gw.diffs_fetched == 1


def test_an_unreadable_patch_baseline_reviews_rather_than_skips():
    """Fail-safe: a store that cannot answer must never be read as 'unchanged'."""
    class BrokenStore(PatchStore):
        def get_last_patch_id(self, p):
            raise OSError("state dir gone")

    store = BrokenStore()
    _reviewed_pr(store, _REVIEWED)
    gw = DiffGateway(_REBASED, prs={100: PRView(100, head_ref_oid="new-sha")})
    runner = FakeRunner(_approved())

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert len(runner.tasks) == 1


def test_rebound_verdict_prevents_paid_review_when_cache_write_fails():
    """Once GitHub has the new-head verdict, a cache failure must not buy a
    duplicate paid review; the next poll reconciles the remote review."""
    class UnwritableSha(PatchStore):
        def set_last_sha(self, p, s):
            raise OSError("read-only state dir")

    store = UnwritableSha()
    store.patch_ids[100] = patch_signature(_REVIEWED)
    store.pr_sessions[100] = "rs"
    store.rounds[100] = 1
    store.last_sha[100] = "old-sha"
    gw = DiffGateway(_REBASED, prs={100: _rebased_pr()})
    runner = FakeRunner(_approved())

    asyncio.run(_workflow(gw, store, runner).re_review(100))

    assert runner.tasks == []
    assert any(call[0] == "review" for call in gw.calls)


class RecordingJournal:
    """Captures raw TelemetryEvent objects (not the lossy legacy view) so a
    deferral's `code` and `Correlation` can be asserted directly."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_claim_target_changed_defers_and_journals_dispatch_deferred(tmp_path):
    """ADR 0008: when the fresh breaker_key disagrees with the claim the
    dispatcher reserved, review() must defer (no paid run) AND surface a
    TelemetryWarning('dispatch_deferred', ...) with Correlation(pr=N) so a
    persistent linkage flap is visible in the dashboard per-issue event view, not just
    the log."""
    pr = PRView(100, head_ref_name="fix/6-moved", closing_issue=6)
    gw = FakeGateway(prs={100: pr})
    runner = FakeRunner(AgentResult(is_error=False))
    journal = RecordingJournal()

    asyncio.run(
        _workflow(gw, FakeStore(), runner, journal=journal).review(
            100, claim_target=5
        )
    )

    assert runner.tasks == []
    assert gw.calls == []
    warnings = [e for e in journal.events if isinstance(e.payload, TelemetryWarning)]
    assert len(warnings) == 1
    event = warnings[0]
    assert event.payload.code == "dispatch_deferred"
    assert event.correlation == Correlation(pr=100)
    assert event.payload.details == {"observed_target": 5, "fresh_target": 6}


def test_claim_target_changed_defers_even_when_journalling_raises():
    """Telemetry must never block the deferral it is reporting on."""
    class ExplodingJournal:
        def emit(self, event):
            raise OSError("journal disk full")

    pr = PRView(100, head_ref_name="fix/6-moved", closing_issue=6)
    gw = FakeGateway(prs={100: pr})
    runner = FakeRunner(AgentResult(is_error=False))

    result = asyncio.run(
        _workflow(gw, FakeStore(), runner, journal=ExplodingJournal()).review(
            100, claim_target=5
        )
    )

    assert result is None
    assert runner.tasks == []


# ── pinned per-PR review checkouts (keifu #165, 2026-08-01) ──────────────────
def test_review_runs_in_the_pinned_checkout_for_the_reviewed_head():
    """The verdict binds a head SHA; the leg's cwd must be a checkout OF that
    SHA, never the operator's live checkout (three phantom REQUEST_CHANGES
    rounds quoted stale chong-dev code under fresh SHAs)."""
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc", reviews=())})
    store = FakeStore()
    runner = FakeRunner(
        AgentResult(is_error=False, status="APPROVE", summary="ok")
    )
    pinned: list[tuple[int, str]] = []
    removed: list[int] = []

    def checkout(pr, head):
        pinned.append((pr, head))
        return f"/pinned/review-pr-{pr}"

    wf = _workflow(
        gw, store, runner,
        review_checkout=checkout,
        review_checkout_cleanup=removed.append,
    )
    asyncio.run(wf.review(100))

    assert pinned == [(100, "abc")]
    assert [t.cwd for t in runner.tasks] == ["/pinned/review-pr-100"]
    # settled round → pinned checkout released
    assert removed == [100]


def test_review_defers_when_the_pinned_checkout_is_unavailable():
    """Reviewing unpinned code is worse than waiting a poll: no leg runs, no
    verdict is posted, and no reviewed-state advances."""
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="abc", reviews=())})
    store = FakeStore()
    runner = FakeRunner(
        AgentResult(is_error=False, status="APPROVE", summary="ok")
    )

    def checkout(_pr, _head):
        raise RuntimeError("head not reachable after fetch")

    wf = _workflow(gw, store, runner, review_checkout=checkout)
    asyncio.run(wf.review(100))

    assert runner.tasks == []
    assert not store.get_last_sha(100)
    assert not store.get_pr_session(100)
    # 0d2e295 H3: a deferral paid for nothing — it must NOT feed the
    # unproductive-round counter, or five free deferrals escalate the PR.
    assert store.infra_deaths == {}


def test_re_review_pin_deferral_is_not_an_unproductive_round():
    """H3's expensive shape: pre-fix, five pin deferrals walked an APPROVED-
    but-unmergeable PR into needs-human-review without a single leg running."""
    review = Review(
        "CHANGES_REQUESTED", "review-bot[bot]", "t1", commit_id="old",
    )
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_oid="new", reviews=(review,))}
    )
    store = FakeStore()
    runner = FakeRunner(
        AgentResult(is_error=False, status="APPROVE", summary="ok")
    )

    def checkout(_pr, _head):
        raise RuntimeError("worktree add failed")

    wf = _workflow(gw, store, runner, review_checkout=checkout)
    for _ in range(5):
        asyncio.run(wf.re_review(100))

    assert runner.tasks == []
    assert store.infra_deaths == {}


def test_pin_deferrals_are_journalled_and_bounded_not_silent_forever():
    """Adversarial F1: 'free' must not mean 'unbounded and invisible'. Every
    deferral emits a review_pin_deferred TelemetryWarning (ADR 0008 — the
    dashboard's per-issue view, not just the log), and past the free streak the
    unproductive-round counter resumes so an unpinnable PR still escalates."""
    review = Review(
        "CHANGES_REQUESTED", "review-bot[bot]", "t1", commit_id="old",
    )
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_oid="new", reviews=(review,))}
    )
    store = FakeStore()
    runner = FakeRunner(
        AgentResult(is_error=False, status="APPROVE", summary="ok")
    )
    journal = RecordingJournal()

    def checkout(_pr, _head):
        raise RuntimeError("TARGET_DIR is not a git repository")

    wf = _workflow(
        gw, store, runner, journal=journal, review_checkout=checkout
    )
    for _ in range(8):
        asyncio.run(wf.re_review(100))

    assert runner.tasks == []
    warnings = [
        e.payload for e in journal.events
        if isinstance(e.payload, TelemetryWarning)
        and e.payload.code == "review_pin_deferred"
    ]
    assert len(warnings) == 8                     # every deferral is visible
    assert warnings[-1].details["streak"] == 8
    # 5 free, then the escalation bound resumes: deferrals 6-8 count.
    assert store.infra_deaths == {-100: 3}


def test_a_progressed_round_resets_the_deferral_streak():
    """A transient flap (a few deferrals, then a round that actually lands a
    verdict) must never accumulate toward the resumed counting. The reset is
    on confirmed PROGRESS, not on a successful pin — multi-mode's gate pins
    successfully even in rounds a dimension later defers."""
    review = Review(
        "CHANGES_REQUESTED", "review-bot[bot]", "t1", commit_id="old",
    )
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_oid="new", reviews=(review,))}
    )
    store = FakeStore()
    runner = FakeRunner(
        AgentResult(
            is_error=False,
            structured={"decision": "APPROVE", "body": "lgtm"},
        )
    )
    flaky = {"fail": True}

    def checkout(_pr, _head):
        if flaky["fail"]:
            raise RuntimeError("flap")
        return "/pinned"

    wf = _workflow(gw, store, runner, review_checkout=checkout)
    for _ in range(4):                     # 4 deferrals — under the free cap
        asyncio.run(wf.re_review(100))
    flaky["fail"] = False
    asyncio.run(wf.re_review(100))         # pin succeeds → streak resets
    assert wf._pin_deferrals == {}
    assert store.infra_deaths == {}


def test_an_empty_head_sha_defers_rather_than_reviewing_the_live_checkout():
    """0d2e295 M2: with a pinned-checkout policy configured, an empty head SHA
    must DEFER the leg, never silently fall back to the live checkout. (Every
    public path today also fails the revision-bound diff first; this pins the
    contract of the task-preparation seam itself so a future caller cannot
    reintroduce the fallback.)"""
    pinned: list[tuple[int, str]] = []

    def checkout(pr, head):
        pinned.append((pr, head))
        return "/pinned"

    wf = _workflow(
        FakeGateway(prs={}), FakeStore(),
        FakeRunner(AgentResult(is_error=False)),
        review_checkout=checkout,
    )
    out = wf._prepare_task(object(), pr=100, head_sha="")

    assert isinstance(out, AgentResult)
    assert out.is_error and out.status == "review_checkout_deferred"
    assert pinned == []


# ── ADR 0014: grants come from STRUCTURED output, never from the model's prose ─
# The harness renders the canonical grant line itself, binding the patch id the
# model never sees — so a hallucinated or prompt-injected "grant" in the body
# cannot bind, and a real grant cannot be forged by quoting.
_DECLARED_SUBJECT = "tests/test_x.py"
_DECLARING_COMMIT = (
    "fix: the expectation was wrong\n\n"
    f"Gate-Exemption: tests-immutable {_DECLARED_SUBJECT}"
)


class _DeclaringGateway(FakeGateway):
    """FakeGateway whose PR's commits declare a gate exemption."""

    def __init__(self, messages=(_DECLARING_COMMIT,), **kw):
        super().__init__(**kw)
        self.messages = tuple(messages)

    def pr_commit_messages(self, pr: int) -> tuple[str, ...]:
        return self.messages


def _declaring_pr():
    return PRView(
        100,
        head_ref_name="fix/5-x",
        head_ref_oid="head-1",
        base_ref_oid="base-1",
        title="t",
        body="b",
    )


def _submitted_body(gw):
    return next(call[3] for call in gw.calls if call[0] == "review")


# The harness-held key that authenticates grant lines. The model never sees
# it, so a grant it composes in prose cannot carry a valid tag (round-3
# BLOCKER 1: the patch id alone is fixer-computable).
_MAC_KEY = "review-side-key-material"


def _review_with(structured, gw=None, **cfg_overrides):
    gw = gw or _DeclaringGateway(prs={100: _declaring_pr()})
    runner = FakeRunner(AgentResult(is_error=False, structured=structured))
    cfg_overrides.setdefault("exemption_mac_key", _MAC_KEY)
    asyncio.run(
        _workflow(gw, FakeStore(), runner, **cfg_overrides).review(100)
    )
    gw.dispatched_tasks = runner.tasks
    return gw


def _expected_grant_line(key=_MAC_KEY):
    return render_grant_line(
        key, "tests-immutable", 5, _DECLARED_SUBJECT, patch_signature("diff text")
    )


def test_an_approved_structured_grant_is_rendered_as_the_canonical_line():
    gw = _review_with({
        "decision": "APPROVE",
        "body": "the old assertion encoded the defect",
        "exemption_grants": [
            {"rule": "tests-immutable", "subject": _DECLARED_SUBJECT}
        ],
    })
    body = _submitted_body(gw)
    # Column 0, last line: exactly what the merge-side parser accepts.
    assert body.endswith("\n" + _expected_grant_line())
    assert body.startswith("the old assertion encoded the defect")


def test_a_rejection_never_mints_a_grant_however_it_is_phrased():
    # Only an approval authorizes. A REQUEST_CHANGES carrying grants — a model
    # confusing "I considered it" with "I allow it" — must post no grant line.
    gw = _review_with({
        "decision": "REQUEST_CHANGES",
        "body": "restore the test instead",
        "exemption_grants": [
            {"rule": "tests-immutable", "subject": _DECLARED_SUBJECT}
        ],
    })
    body = _submitted_body(gw)
    assert "gate-exemption: granted" not in body
    assert any(call[:3] == ("review", 100, "REQUEST_CHANGES") for call in gw.calls)


def test_a_grant_for_something_never_declared_is_dropped():
    # Allowlist-shaped validation: the model can only grant what the PR's own
    # commits declared, so a fabricated subject cannot reach the merge rule.
    gw = _review_with({
        "decision": "APPROVE",
        "body": "ok",
        "exemption_grants": [
            {"rule": "tests-immutable", "subject": "tests/never_declared.py"},
            {"rule": "required-test", "subject": "*"},
        ],
    })
    assert "gate-exemption: granted" not in _submitted_body(gw)


def test_a_verdict_with_no_grants_field_submits_the_model_body_unchanged():
    gw = _review_with({"decision": "APPROVE", "body": "looks good"})
    assert _submitted_body(gw) == "looks good"


def test_a_grant_line_the_model_wrote_in_prose_is_not_re_minted_by_the_harness():
    # The body is passed through verbatim (it is the model's text, and the
    # merge side authenticates authorship + patch id anyway) — but the HARNESS
    # appends nothing when the structured field is absent, so prose alone can
    # never produce a grant bound to the real patch id.
    forged = (
        "gate-exemption: granted rule=tests-immutable issue=5 "
        f"subject={_DECLARED_SUBJECT} patch=guessed"
    )
    gw = _review_with({"decision": "APPROVE", "body": forged})
    body = _submitted_body(gw)
    assert body == forged
    assert patch_signature("diff text") not in body


def test_the_base_review_schema_offers_no_exemption_field_at_all():
    # An undeclared PR must not even be able to REPRESENT a grant, and the
    # ordinary verdict contract is unchanged.
    assert "exemption_grants" not in REVIEW_SCHEMA["properties"]
    assert REVIEW_SCHEMA["required"] == ["decision", "body"]


def test_the_declared_schema_enumerates_exactly_the_declared_pairs():
    # Object-level enum: an undeclared rule, a cross-matched subject, or free
    # text is unrepresentable at generation time, not merely dropped later.
    schema = review_schema_with_exemptions([("tests-immutable", "tests/a_test.rs")])
    assert schema["properties"]["exemption_grants"]["items"] == {
        "enum": [{"rule": "tests-immutable", "subject": "tests/a_test.rs"}]
    }
    assert schema["required"] == ["decision", "body"]
    assert "decision" in schema["properties"] and "body" in schema["properties"]


def test_building_the_declared_schema_never_mutates_the_base_schema():
    before = deepcopy(REVIEW_SCHEMA)
    review_schema_with_exemptions([("required-test", "*")])
    review_schema_with_exemptions([])
    assert REVIEW_SCHEMA == before


def test_the_review_task_of_a_declared_pr_carries_the_constrained_schema():
    gw = _review_with({"decision": "APPROVE", "body": "ok"})
    task = gw.dispatched_tasks[0]
    assert task.intent.schema["properties"]["exemption_grants"]["items"] == {
        "enum": [{"rule": "tests-immutable", "subject": _DECLARED_SUBJECT}]
    }
    assert "Gate exemption REQUESTED" in task.prompt


def test_the_review_task_of_an_undeclared_pr_keeps_the_base_schema():
    gw = _DeclaringGateway(messages=(), prs={100: _declaring_pr()})
    runner = FakeRunner(
        AgentResult(is_error=False, structured={"decision": "APPROVE", "body": "ok"})
    )
    asyncio.run(_workflow(gw, FakeStore(), runner).review(100))
    assert runner.tasks[0].intent.schema == REVIEW_SCHEMA
    assert "Gate exemption REQUESTED" not in runner.tasks[0].prompt


def test_the_minted_grant_actually_authorizes_the_merge_it_is_bound_to():
    # End-to-end through the real seam: what the workflow POSTs must be what
    # the merge rule accepts — same key, same patch id, same issue.
    gw = _review_with({
        "decision": "APPROVE",
        "body": "the old assertion encoded the defect",
        "exemption_grants": [
            {"rule": "tests-immutable", "subject": _DECLARED_SUBJECT}
        ],
    })
    grants = parse_exemption_grants(_submitted_body(gw))
    facts = ExemptionFacts(
        declarations=(ExemptionDeclaration("tests-immutable", _DECLARED_SUBJECT),),
        grants=grants,
        patch_id=patch_signature("diff text"),
    )
    assert exemptions_permit_merge(
        facts,
        issue_number=5,
        identity_mode=IdentityMode.THREE,
        fix_bot="fix-bot",
        review_bot="review-bot",
        mac_key=_MAC_KEY,
    ) is True
    # ...and not under any other key: the tag is what carries authorship.
    assert exemptions_permit_merge(
        facts,
        issue_number=5,
        identity_mode=IdentityMode.THREE,
        fix_bot="fix-bot",
        review_bot="review-bot",
        mac_key="some-other-key",
    ) is False


def test_no_key_material_mints_no_grant_at_all():
    # A deployment without EXEMPTION_MAC_KEY/REVIEW_BOT_TOKEN cannot
    # authenticate anything, so it must post no grant syntax — the PR then
    # escalates via the withheld arm with an accurate reason.
    gw = _review_with(
        {
            "decision": "APPROVE",
            "body": "ok",
            "exemption_grants": [
                {"rule": "tests-immutable", "subject": _DECLARED_SUBJECT}
            ],
        },
        exemption_mac_key="",
    )
    body = _submitted_body(gw)
    assert "gate-exemption: granted" not in body
    assert parse_exemption_grants(body) == ()


def test_a_flood_of_undeclared_grant_entries_mints_nothing_and_stays_bounded():
    # A loose runtime that ignores the enum schema can emit anything. Every
    # entry is validated against the PR's own declarations, so a flood yields
    # no grant lines — and the drop logging is summarized, not one line each.
    logs: list[str] = []
    gw = _review_with(
        {
            "decision": "APPROVE",
            "body": "ok",
            "exemption_grants": [
                {"rule": "tests-immutable", "subject": f"tests/fake_{i}.py"}
                for i in range(200)
            ],
        },
        log=logs.append,
    )
    assert parse_exemption_grants(_submitted_body(gw)) == ()
    assert len([m for m in logs if "dropped" in m]) <= 6


def test_a_malformed_grants_field_is_ignored_rather_than_guessed_at():
    for raw in ("tests-immutable", {"rule": "tests-immutable"}, 42, [], [None, 7]):
        gw = _review_with(
            {"decision": "APPROVE", "body": "ok", "exemption_grants": raw}
        )
        assert parse_exemption_grants(_submitted_body(gw)) == (), raw


def test_grant_lines_survive_a_model_body_that_ends_inside_an_open_fence():
    # A truncated body ending mid-fence would swallow the appended grants and
    # the PR would escalate with a false "the reviewer did not grant"
    # (round-3 MINOR 7). The submit path closes the fence first.
    gw = _review_with({
        "decision": "APPROVE",
        "body": "I approve. The offending assertion:\n```\n-assert x == 1",
        "exemption_grants": [
            {"rule": "tests-immutable", "subject": _DECLARED_SUBJECT}
        ],
    })
    body = _submitted_body(gw)
    assert body.startswith("I approve.")
    assert parse_exemption_grants(body) == (
        parse_exemption_grants(_expected_grant_line())[0],
    )


def test_a_task_with_no_structured_verdict_channel_gets_no_notice_or_schema():
    # The notice instructs a field the model could not emit, so both halves are
    # skipped and the task is returned unchanged (round-3 MINOR 9). The
    # declared PR then escalates via the withheld arm — fail closed.
    from issue_agent.app.workflows.review import ReviewWorkflow

    declarations = (ExemptionDeclaration("tests-immutable", _DECLARED_SUBJECT),)
    gw = _DeclaringGateway(prs={100: _declaring_pr()})
    runner = FakeRunner(AgentResult(is_error=False))
    task = asyncio.run(_a_review_task(gw, runner))
    for schema in (None, {}, {"properties": {"tier": {"type": "string"}}}):
        bare = replace(task, intent=replace(task.intent, schema=schema))
        assert ReviewWorkflow._with_exemption_notice(
            bare, "\n\nNOTICE", declarations
        ) is bare


async def _a_review_task(gw, runner):
    """The real review task the workflow builds, borrowed for the unit above."""
    await _workflow(gw, FakeStore(), runner, exemption_mac_key=_MAC_KEY).review(100)
    return runner.tasks[0]
