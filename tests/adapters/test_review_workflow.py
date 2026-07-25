"""ReviewWorkflow wiring tests."""

import asyncio

from issue_agent.app.execute import Executor
from issue_agent.app.workflows.review import ReviewWorkflow, ReviewWorkflowConfig
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.review import ReviewMode
from issue_agent.app.planners.tasks import ReviewTaskConfig
from issue_agent.app.planners.tasks import REVIEW_DIMENSIONS
from issue_agent.domain.world import PRView, Review

from test_dispatch import FakeGateway, FakeRunner, FakeStore, SchemaRunner, _dim, _gate, _val


class FakeJournal:
    """Records what the write funnel emitted. `escalation` is the event an
    operator is paged on (Executor emits it for any move to needs-human-review)."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def _workflow(gw, store, runner, journal=None, **cfg_overrides):
    cfg_kwargs = dict(
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER"),
        review_mode=ReviewMode.SINGLE,
        max_review_rejections=4,
        review_bot="review-bot",
        review_trigger_label="ai-pr-open",
        log=lambda _m: None,
    )
    cfg_kwargs.update(cfg_overrides)
    cfg = ReviewWorkflowConfig(**cfg_kwargs)
    # The REAL Executor, not a spy: M10's whole point is that escalation must
    # travel the same funnel every other write does (journal event + ALERT_CMD
    # come from the funnel, not from the workflow).
    return ReviewWorkflow(cfg, gw, store, runner, Executor(gw, journal=journal))


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
