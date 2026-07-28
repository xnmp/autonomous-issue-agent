"""Action executor — each Action triggers the right adapter call, no logic."""

from issue_agent.app.execute import Executor
from issue_agent.domain.actions import (
    ClearIssueState,
    CloseIssue,
    EnableAutoMerge,
    UpdateBranch,
    NoOp,
    PostComment,
    Relabel,
    RemoveWorktree,
    RerunFailedChecks,
    RunAgent,
    AgentKind,
    Target,
)
from issue_agent.domain.labels import Label


class FakeGateway:
    def __init__(self):
        self.calls = []

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))

    def comment(self, kind, number, body):
        self.calls.append(("comment", kind, number, body))

    def close_issue(self, number, reason, comment):
        self.calls.append(("close", number, reason, comment))

    def enable_auto_merge(self, pr, method):
        self.calls.append(("automerge", pr, method))
        return getattr(self, "automerge_ok", True)

    def update_branch(self, pr):
        self.calls.append(("update_branch", pr))

    def rerun_failed_checks(self, pr):
        self.calls.append(("rerun", pr))
        return getattr(self, "rerun_run_id", 4242)


def _exec():
    gw = FakeGateway()
    removed, ran = [], []
    ex = Executor(gw, remove_worktree=removed.append, run_agent=ran.append)
    return ex, gw, removed, ran


def test_relabel_passes_label_values():
    ex, gw, _, _ = _exec()
    ex.execute(Relabel(Target.ISSUE, 5, Label.AI_READY, Label.AI_WORKING))
    assert gw.calls == [("relabel", "issue", 5, "ai-ready", "ai-working")]


def test_relabel_pr_target_and_none():
    ex, gw, _, _ = _exec()
    ex.execute(Relabel(Target.PR, 100, Label.AI_WORKING, None))
    assert gw.calls == [("relabel", "pr", 100, "ai-working", None)]


def test_post_comment():
    ex, gw, _, _ = _exec()
    ex.execute(PostComment(Target.ISSUE, 5, "hello"))
    assert gw.calls == [("comment", "issue", 5, "hello")]


def test_close_issue():
    ex, gw, _, _ = _exec()
    ex.execute(CloseIssue(5, "completed", "done"))
    assert gw.calls == [("close", 5, "completed", "done")]


def test_enable_auto_merge():
    ex, gw, _, _ = _exec()
    ex.execute(EnableAutoMerge(100, "squash"))
    assert gw.calls == [("automerge", 100, "squash")]


def test_enable_auto_merge_persists_the_armed_marker():
    # The decide side's marker guard (FixState.automerge_enabled) only works if
    # arming WRITES the marker — it never did (T3 probe: zero production call
    # sites), so every merge-ready PR re-armed on every poll.
    gw = FakeGateway()
    marked = []
    ex = Executor(gw, mark_automerge=marked.append)
    ex.execute(EnableAutoMerge(100, "squash"))
    assert marked == [100]


def test_enable_auto_merge_failure_withholds_the_marker():
    # The inverse guard: the marker records what GITHUB did, not what we
    # attempted. When the arm fails (observed: GraphQL "Branch does not have
    # required protected branch rules" on an unprotected base), writing it
    # anyway suppresses should_automerge forever on a PR that is otherwise
    # merge-ready — #537 sat APPROVED + green + CLEAN and never merged.
    gw = FakeGateway()
    gw.automerge_ok = False
    marked = []
    ex = Executor(gw, mark_automerge=marked.append)
    ex.execute(EnableAutoMerge(100, "squash"))
    assert gw.calls == [("automerge", 100, "squash")]
    assert marked == []


def test_update_branch():
    ex, gw, _, _ = _exec()
    ex.execute(UpdateBranch(100))
    assert gw.calls == [("update_branch", 100)]


# ── free CI re-run (#38) ────────────────────────────────────────────────────
class _Journal:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def test_rerun_marks_the_head_before_asking_github():
    # Ordering is the whole safety property: marker THEN gh. The reverse loses
    # the record if the process dies in between, and a free re-run with no
    # record is an unbounded loop (no retry counter counts a non-agent action).
    gw = FakeGateway()
    marked = []
    ex = Executor(gw, mark_ci_rerun=lambda pr, head: marked.append((pr, head)))
    ex.execute(RerunFailedChecks(100, "sha-1"))
    assert marked == [(100, "sha-1")]
    assert gw.calls == [("rerun", 100)]


def test_rerun_journals_the_run_it_triggered():
    gw = FakeGateway()
    j = _Journal()
    ex = Executor(gw, mark_ci_rerun=lambda pr, head: None, journal=j)
    ex.execute(RerunFailedChecks(100, "sha-1"))
    kind, fields = next(e for e in j.events if e[0] == "ci_rerun")
    assert (fields["pr"], fields["head"], fields["run_id"], fields["ok"]) == (
        100, "sha-1", 4242, True
    )


def test_rerun_that_finds_no_run_still_keeps_the_marker_and_says_so():
    # External status contexts (or a gh refusal) leave nothing to re-run. Soft
    # by design: the marker is already spent, so the next poll buys the paid leg
    # instead of retrying this forever.
    gw = FakeGateway()
    gw.rerun_run_id = None
    marked, logged = [], []
    j = _Journal()
    ex = Executor(gw, mark_ci_rerun=lambda pr, head: marked.append((pr, head)),
                  journal=j, log=logged.append)
    ex.execute(RerunFailedChecks(100, "sha-1"))
    assert marked == [(100, "sha-1")]
    assert any("no failed Actions run" in line for line in logged)
    assert next(f for e, f in j.events if e == "ci_rerun")["ok"] is False


def test_rerun_without_a_marker_writer_does_not_touch_github():
    # The review/steward Executors wire no marker writer. Running the re-run
    # there would be unrecordable — i.e. unbounded — so it is skipped instead.
    gw = FakeGateway()
    logged = []
    ex = Executor(gw, log=logged.append)
    ex.execute(RerunFailedChecks(100, "sha-1"))
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


def test_clear_issue_state_default_is_a_noop():
    # anti-vacuity twin: absent an injected callable (review/steward Executors
    # never wire one), applying the action must not raise or fake success.
    ex, gw, removed, ran = _exec()
    ex.execute(ClearIssueState(pr=100, issue=9, merged=False))  # must not raise
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
