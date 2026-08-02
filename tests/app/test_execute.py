"""Action executor — each Action triggers the right adapter call, no logic."""

import pytest

from issue_agent.adapters.journal import _legacy_event_record
from issue_agent.app.execute import Executor as DurableExecutor
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.domain.actions import (
    ClearIssueState,
    CloseIssue,
    EnableAutoMerge,
    EscalateCiPending,
    UpdateBranch,
    NoOp,
    PostComment,
    Relabel,
    RemoveWorktree,
    CancelWorkflowRuns,
    RerunFailedChecks,
    RunAgent,
    AgentKind,
    Target,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.world import PRView
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway


def _exec():
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_oid="head-100")}
    )
    removed, ran = [], []
    ex = Executor(
        gw,
        remove_worktree=removed.append,
        run_agent=ran.append,
        mark_automerge=lambda _pr: None,
    )
    return ex, gw, removed, ran


def test_relabel_passes_label_values():
    ex, gw, _, _ = _exec()
    ex.execute(Relabel(Target.ISSUE, 5, Label.AI_READY, Label.AI_WORKING))
    assert gw.calls == [("relabel", "issue", 5, "ai-ready", "ai-working")]


def test_remote_action_fails_closed_without_a_durable_effect_executor():
    gateway = FakeGateway()

    with pytest.raises(
        RuntimeError, match="durable effect executor is not configured"
    ):
        DurableExecutor(gateway).execute(
            Relabel(Target.ISSUE, 5, Label.AI_READY, Label.AI_WORKING)
        )

    assert gateway.calls == []


def test_relabel_pr_target_and_none():
    ex, gw, _, _ = _exec()
    ex.execute(Relabel(Target.PR, 100, Label.AI_WORKING, None))
    assert gw.calls == [("relabel", "pr", 100, "ai-working", None)]


def test_post_comment():
    ex, gw, _, _ = _exec()
    ex.execute(PostComment(Target.ISSUE, 5, "hello"))
    assert gw.calls == [("comment", "issue", 5, "hello")]


def test_ci_pending_escalation_marks_head_after_verified_write():
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_oid="head-100")}
    )
    gw.has_label = lambda kind, number, label: True
    marked = []
    ex = Executor(gw, mark_ci_pending_escalated=lambda pr, head: marked.append(
        (pr, head)
    ))

    ex.execute(EscalateCiPending(100, "sha-1", "stalled"))

    assert marked == [(100, "sha-1")]
    assert [call[0] for call in gw.calls] == ["comment"]


def test_ci_pending_escalation_withholds_marker_when_relabel_is_not_verified():
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_oid="head-100")}
    )
    gw.has_label = lambda kind, number, label: False
    marked = []
    ex = Executor(gw, mark_ci_pending_escalated=lambda pr, head: marked.append(
        (pr, head)
    ))

    with pytest.raises(RuntimeError, match="not verified"):
        ex.execute(EscalateCiPending(100, "sha-1", "stalled"))

    assert marked == []


def test_close_issue():
    ex, gw, _, _ = _exec()
    ex.execute(CloseIssue(5, "completed", "done"))
    assert gw.calls == [("close", 5, "completed", "done")]


def test_enable_auto_merge():
    ex, gw, _, _ = _exec()
    ex.execute(EnableAutoMerge(100, "squash", "head-100"))
    assert gw.calls == [("automerge", 100, "squash")]


def test_enable_auto_merge_persists_the_armed_marker():
    # The decide side's marker guard (FixState.automerge_enabled) only works if
    # arming WRITES the marker — it never did (T3 probe: zero production call
    # sites), so every merge-ready PR re-armed on every poll.
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_oid="head-100")}
    )
    marked = []
    ex = Executor(gw, mark_automerge=marked.append)
    ex.execute(EnableAutoMerge(100, "squash", "head-100"))
    assert marked == [100]


def test_enable_auto_merge_failure_withholds_the_marker():
    # The inverse guard: the marker records what GITHUB did, not what we
    # attempted. When the arm fails (observed: GraphQL "Branch does not have
    # required protected branch rules" on an unprotected base), writing it
    # anyway suppresses should_automerge forever on a PR that is otherwise
    # merge-ready — #537 sat APPROVED + green + CLEAN and never merged.
    gw = FakeGateway(
        prs={100: PRView(100, head_ref_oid="head-100")}
    )
    gw.automerge_ok = False
    marked = []
    ex = Executor(gw, mark_automerge=marked.append)
    ex.execute(EnableAutoMerge(100, "squash", "head-100"))
    assert gw.calls == [("automerge", 100, "squash")]
    assert marked == []


def test_update_branch():
    ex, gw, _, _ = _exec()
    ex.execute(UpdateBranch(100, "head-100"))
    assert gw.calls == [("update_branch", 100)]


def test_branch_refresh_marks_the_attempt_before_asking_github():
    # Same safety property (and the same ordering) as the CI re-run marker:
    # marker THEN gh. Refreshed-but-not-marked is an unbounded push loop.
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="head-100")})
    marked = []
    ex = Executor(
        gw, mark_branch_update=lambda pr, key: marked.append((pr, key))
    )
    ex.execute(UpdateBranch(100, "head-100", "base-1"))
    assert marked == [(100, "head-100:base-1")]
    assert ("update_branch", 100) in gw.calls


def test_failed_branch_refresh_is_logged_and_journalled_not_silent():
    # Adversarial F2: the attempt is spent either way (marker written first);
    # what must not happen is spending it INVISIBLY.
    gw = FakeGateway(prs={})  # no PR 100 → update_branch returns False
    marked, logged = [], []
    j = _Journal()
    ex = Executor(
        gw,
        mark_branch_update=lambda pr, key: marked.append((pr, key)),
        journal=j, log=logged.append,
    )
    ex.execute(UpdateBranch(100, "head-100", "base-1"))
    assert marked == [(100, "head-100:base-1")]
    assert any("did not land" in line for line in logged)
    kind, fields = next(e for e in j.events if e[0] == "branch_refresh")
    assert (fields["pr"], fields["head"], fields["base"], fields["ok"]) == (
        100, "head-100", "base-1", False
    )


# ── free CI re-run (#38) ────────────────────────────────────────────────────
class _Journal:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        if not isinstance(event, str):
            event, fields = _legacy_event_record(event)
        self.events.append((event, fields))


def test_rerun_marks_the_head_before_asking_github():
    # Ordering is the whole safety property: marker THEN gh. The reverse loses
    # the record if the process dies in between, and a free re-run with no
    # record is an unbounded loop (no retry counter counts a non-agent action).
    gw = FakeGateway()
    marked = []
    ex = Executor(gw, mark_ci_rerun=lambda pr, head: marked.append((pr, head)))
    ex.execute(RerunFailedChecks(100, "sha-1", (4242,)))
    assert marked == [(100, "sha-1")]
    assert gw.calls == [("rerun", "sha-1", (4242,))]


def test_rerun_journals_the_run_it_triggered():
    gw = FakeGateway()
    j = _Journal()
    ex = Executor(gw, mark_ci_rerun=lambda pr, head: None, journal=j)
    ex.execute(RerunFailedChecks(100, "sha-1", (4242,)))
    kind, fields = next(e for e in j.events if e[0] == "ci_rerun")
    assert (fields["pr"], fields["head"], fields["run_id"], fields["ok"]) == (
        100, "sha-1", 4242, True
    )


def test_rejected_exact_rerun_still_keeps_the_marker_and_says_so():
    # A compatibility executor may receive a refusal after the marker is spent;
    # the next poll buys the bounded repair leg instead of retrying forever.
    gw = FakeGateway()
    gw.rerun_run_id = None
    marked, logged = [], []
    j = _Journal()
    ex = Executor(gw, mark_ci_rerun=lambda pr, head: marked.append((pr, head)),
                  journal=j, log=logged.append)
    ex.execute(RerunFailedChecks(100, "sha-1", (4242,)))
    assert marked == [(100, "sha-1")]
    assert any("not executable" in line for line in logged)
    assert next(f for e, f in j.events if e == "ci_rerun")["ok"] is False


# ── wedged-run cancel (keifu PR #177) ───────────────────────────────────────
def _wedged_gateway(status="in_progress"):
    gw = FakeGateway()
    gw.run_states["sha-1"] = {4242: status}
    return gw


def test_cancel_marks_the_head_before_asking_github():
    # Same ordering property as the re-run marker: marker THEN gh. Reversed, a
    # death in between leaves a cancel nothing recorded, and the next poll
    # cancels again, forever.
    gw = _wedged_gateway()
    marked = []
    ex = Executor(gw, mark_ci_cancel=lambda pr, head: marked.append((pr, head)))

    ex.execute(CancelWorkflowRuns(100, "sha-1", (4242,)))

    assert marked == [(100, "sha-1")]
    assert gw.calls == [("cancel", "sha-1", (4242,))]


def test_cancel_journals_the_runs_it_took_down():
    gw = _wedged_gateway()
    j = _Journal()
    ex = Executor(gw, mark_ci_cancel=lambda pr, head: None, journal=j)

    ex.execute(CancelWorkflowRuns(100, "sha-1", (4242,)))

    _kind, fields = next(e for e in j.events if e[0] == "ci_cancel")
    assert (fields["pr"], fields["head"], fields["run_ids"], fields["ok"]) == (
        100, "sha-1", [4242], True
    )


def test_a_refused_cancel_is_logged_and_journalled_not_silent():
    # The runs belong to a head this gateway does not know — the SHA guard
    # refuses. The marker is still spent, so the bounded escalation follows;
    # what must not happen is that being invisible.
    gw = FakeGateway()
    marked, logged = [], []
    j = _Journal()
    ex = Executor(gw, mark_ci_cancel=lambda pr, head: marked.append((pr, head)),
                  journal=j, log=logged.append)

    ex.execute(CancelWorkflowRuns(100, "sha-1", (4242,)))

    assert marked == [(100, "sha-1")]
    assert any("not cancellable" in line for line in logged)
    assert next(f for e, f in j.events if e == "ci_cancel")["ok"] is False


def test_cancel_without_a_marker_writer_does_not_touch_github():
    # An Executor with no marker writer cannot bound the loop, so it must not
    # start one — the stalled-CI escalation stands instead.
    gw = _wedged_gateway()
    logged = []
    ex = Executor(gw, log=logged.append)

    ex.execute(CancelWorkflowRuns(100, "sha-1", (4242,)))

    assert gw.calls == []
    assert any("no marker writer" in line for line in logged)


def test_a_bool_effect_receipt_never_crashes_the_run_id_journal():
    # The durable executor normalises every outcome to a bool; an observability
    # field may not take a remote mutation down with it.
    gw = _wedged_gateway()
    j = _Journal()
    ex = Executor(gw, mark_ci_cancel=lambda pr, head: None,
                  mark_ci_rerun=lambda pr, head: None, journal=j,
                  effect_executor=_BoolReceiptExecutor(gw))

    ex.execute(CancelWorkflowRuns(100, "sha-1", (4242,)))
    ex.execute(RerunFailedChecks(100, "sha-1", (4242,)))

    cancelled = next(f for e, f in j.events if e == "ci_cancel")
    assert (cancelled["run_ids"], cancelled["requested_run_ids"],
            cancelled["ok"]) == ([], [4242], True)
    assert next(f for e, f in j.events if e == "ci_rerun")["run_ids"] == []


class _BoolReceiptExecutor:
    """Stands in for the durable executor's bool-only receipt."""

    def __init__(self, gateway):
        self.gateway = gateway

    def handles(self, action) -> bool:
        return isinstance(action, (CancelWorkflowRuns, RerunFailedChecks))

    def execute(self, action) -> bool:
        return True


def test_rerun_without_a_marker_writer_does_not_touch_github():
    # The review/steward Executors wire no marker writer. Running the re-run
    # there would be unrecordable — i.e. unbounded — so it is skipped instead.
    gw = FakeGateway()
    logged = []
    ex = Executor(gw, log=logged.append)
    ex.execute(RerunFailedChecks(100, "sha-1", (4242,)))
    assert gw.calls == []
    assert any("no marker writer" in line for line in logged)


def test_remove_worktree_delegated():
    ex, _, removed, _ = _exec()
    ex.execute(RemoveWorktree(9))
    assert removed == [9]


def test_run_agent_delegated():
    ex, _, _, ran = _exec()
    action = RunAgent(AgentKind.RESOLVE, 5)
    ex.execute(action)
    assert ran == [action]


def test_noop_does_nothing():
    ex, gw, removed, ran = _exec()
    ex.execute(NoOp("idle"))
    assert gw.calls == [] and removed == [] and ran == []


def test_every_action_is_logged_except_noop():
    gw = FakeGateway()
    logged: list[str] = []
    ex = Executor(gw, remove_worktree=lambda i: None, run_agent=lambda a: None,
                  log=logged.append)
    ex.execute(Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_WORKING))
    ex.execute(CloseIssue(7))
    ex.execute(RunAgent(AgentKind.RESOLVE, 7))
    ex.execute(NoOp("idle"))
    # Three real actions logged, NoOp silent — the audit trail nobody has to guess.
    assert len(logged) == 3
    assert "relabel issue #7" in logged[0]
    assert "-ai-ready" in logged[0] and "+ai-working" in logged[0]
    assert "close issue #7" in logged[1]
    assert "dispatch RESOLVE #7" in logged[2]


def test_execute_all_sequences():
    ex, gw, _, ran = _exec()
    ex.execute_all([
        Relabel(Target.ISSUE, 5, Label.AI_READY, Label.AI_WORKING),
        RunAgent(AgentKind.RESOLVE, 5),
    ])
    assert gw.calls[0][0] == "relabel"
    assert len(ran) == 1


# ── ClearIssueState wiring (review M4/D8) ───────────────────────────────────
def test_clear_issue_state_action_invokes_injected_callable():
    gw = FakeGateway()
    calls = []
    ex = Executor(
        gw,
        clear_issue_state=lambda pr, issue, merged: calls.append((pr, issue, merged)),
    )
    ex.execute(ClearIssueState(pr=100, issue=9, merged=True))
    assert calls == [(100, 9, True)]


def test_clear_issue_state_requires_operational_wiring():
    ex, gw, removed, ran = _exec()
    with pytest.raises(RuntimeError, match="clear_issue_state"):
        ex.execute(ClearIssueState(pr=100, issue=9, merged=False))
    assert gw.calls == [] and removed == [] and ran == []


# ── execute_all per-action isolation (review S7) ────────────────────────────
def test_execute_all_isolates_a_failing_action_and_runs_the_rest():
    # Before this fix, a raising Relabel propagated out of execute_all
    # IMMEDIATELY, so the sibling RemoveWorktree never ran and the caller
    # (e.g. AgentDispatcher.dispatch's tail) never completed the batch. Now
    # the failure is isolated + logged, and the batch completes.
    gw = FakeGateway()

    def _boom(*_a, **_k):
        raise RuntimeError("gh 500")

    gw.relabel = _boom
    removed = []
    logged: list[str] = []
    ex = Executor(gw, remove_worktree=removed.append, log=logged.append)

    ex.execute_all([
        Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_WORKING),  # raises
        RemoveWorktree(9),  # must still run despite the sibling failure above
    ])

    assert removed == [9]
    assert any("FAILED" in line and "relabel issue #7" in line for line in logged)


def test_execute_all_logs_nothing_extra_when_every_action_succeeds():
    # anti-vacuity twin: a clean batch reports no failure line.
    gw = FakeGateway()
    removed = []
    logged: list[str] = []
    ex = Executor(gw, remove_worktree=removed.append, log=logged.append)
    ex.execute_all([RemoveWorktree(9)])
    assert removed == [9]
    assert not any("FAILED" in line for line in logged)


def test_failed_worktree_cleanup_withholds_dependent_state_transitions():
    gw = FakeGateway()
    cleared = []
    logged = []

    def fail_remove(_issue):
        raise RuntimeError("stale registry")

    ex = Executor(
        gw,
        remove_worktree=fail_remove,
        clear_issue_state=lambda pr, issue, merged: cleared.append(
            (pr, issue, merged)
        ),
        log=logged.append,
    )

    succeeded = ex.execute_all(
        [
            RemoveWorktree(9),
            Relabel(
                Target.ISSUE,
                9,
                Label.AI_PR_OPEN,
                Label.AI_DONE,
            ),
            ClearIssueState(pr=100, issue=9, merged=True),
            RemoveWorktree(10),
        ]
    )

    assert succeeded is False
    assert gw.calls == []
    assert cleared == []
    assert any("SKIPPED" in line and "issue #9" in line for line in logged)
