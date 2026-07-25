"""FixWorkflow wiring tests.

These cover the extracted fix-side workflow directly so AgentDispatcher can keep
shrinking into a router without losing behavior coverage.
"""

import asyncio

from issue_agent.app.execute import Executor
from issue_agent.app.workflows.fix import FixWorkflow, FixWorkflowConfig
from issue_agent.domain.outcome import AgentResult
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.domain.world import Check, Comment, IssueView, PRView, Review

from test_dispatch import FakeGateway, FakeRunner, FakeStore, FakeWorktrees, SeqRunner


def _workflow(gw, store, runner, wt=None, **cfg_overrides):
    cfg_kwargs = dict(
        base_branch="dev",
        fix=FixTaskConfig("o/r", agent_md="AM"),
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
    executor = Executor(gw, remove_worktree=worktrees.remove)
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


def test_fix_ci_records_retry_and_clears_working_on_push():
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_name="fix/5-token", closing_issue=5,
                         checks=(Check("unit", bucket="fail"),))},
    )
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))

    asyncio.run(_workflow(gw, store, runner).fix_ci(100))

    assert store.sessions[5] == "c5"
    assert "unit" in runner.tasks[0].prompt
    assert ("relabel", "pr", 100, "ai-working", None) in gw.calls


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
