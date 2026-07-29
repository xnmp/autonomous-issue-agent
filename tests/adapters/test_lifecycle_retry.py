"""Durable retries for GitHub lifecycle writes."""

from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.store import Store
from issue_agent.app.execute import Executor
from issue_agent.app.loop import Daemon
from issue_agent.domain.actions import (
    AgentKind,
    CloseIssue,
    PostComment,
    Relabel,
    RunAgent,
    Target,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.world import WorldState


class RecordingJournal:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


class ScriptedGateway:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.comments = []

    def comment(self, kind, number, body):
        self.comments.append((kind, number, body))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome

    def relabel(self, kind, number, remove, add):
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


def unavailable(detail="HTTP 401: Requires authentication"):
    return GhUnavailable("auth", detail)


def test_transient_write_retries_with_backoff_and_journals_only_accepted_success():
    gateway = ScriptedGateway([unavailable(), unavailable("HTTP 503"), None])
    journal = RecordingJournal()
    slept = []
    executor = Executor(
        gateway,
        journal=journal,
        retry_delays=(1, 3),
        sleep=slept.append,
    )

    executor.execute(PostComment(Target.ISSUE, 29, "Need clarification"))

    assert len(gateway.comments) == 3
    assert slept == [1, 3]
    assert [event for event, _ in journal.events] == [
        "action_retry",
        "action_retry",
        "action",
    ]
    assert journal.events[-1][1]["attempts"] == 3


def test_relabel_is_covered_by_the_same_transient_retry_contract():
    gateway = ScriptedGateway([unavailable("HTTP 502"), None])
    executor = Executor(
        gateway,
        retry_delays=(0,),
        sleep=lambda _seconds: None,
    )

    executor.execute(
        Relabel(Target.ISSUE, 29, Label.AI_WORKING, Label.AI_BLOCKED)
    )

    assert gateway.outcomes == []


def test_pending_store_round_trips_every_terminal_lifecycle_action(tmp_path):
    store = Store(str(tmp_path))
    actions = (
        PostComment(Target.ISSUE, 29, "Need clarification"),
        Relabel(Target.PR, 64, Label.AI_WORKING, Label.AI_PR_OPEN),
        CloseIssue(29, "completed", "Done"),
    )

    store.save_pending_actions(actions)

    assert Store(str(tmp_path)).load_pending_actions() == actions


def test_exhausted_write_is_failed_not_done_and_survives_reconstruction(tmp_path):
    store = Store(str(tmp_path))
    journal = RecordingJournal()
    action = PostComment(Target.ISSUE, 29, "Need clarification")
    executor = Executor(
        ScriptedGateway([unavailable(), unavailable(), unavailable()]),
        journal=journal,
        pending_actions=store,
        retry_delays=(0, 0),
        sleep=lambda _seconds: None,
    )

    executor.execute_all([action])

    assert [event for event, _ in journal.events].count("action") == 0
    failed = [fields for event, fields in journal.events if event == "action_failed"]
    assert len(failed) == 1
    assert failed[0]["pending"] is True
    assert Store(str(tmp_path)).load_pending_actions() == (action,)


def test_pending_write_blocks_observe_and_paid_dispatch_while_github_is_down(tmp_path):
    store = Store(str(tmp_path))
    action = PostComment(Target.ISSUE, 29, "Need clarification")
    store.save_pending_actions((action,))
    observed = []
    dispatched = []
    executor = Executor(
        ScriptedGateway([unavailable(), unavailable()]),
        run_agent=dispatched.append,
        pending_actions=store,
        retry_delays=(0,),
        sleep=lambda _seconds: None,
    )
    daemon = Daemon(
        "fix",
        lambda: observed.append(True) or WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 29)],
        executor,
    )

    assert daemon.tick() == []
    assert observed == []
    assert dispatched == []
    assert store.load_pending_actions() == (action,)


def test_later_tick_replays_then_removes_pending_write_without_reapplying_it(tmp_path):
    store = Store(str(tmp_path))
    action = PostComment(Target.ISSUE, 29, "Need clarification")
    store.save_pending_actions((action,))
    gateway = ScriptedGateway([None])
    observed = []
    executor = Executor(
        gateway,
        pending_actions=Store(str(tmp_path)),
        retry_delays=(),
        sleep=lambda _seconds: None,
    )
    daemon = Daemon(
        "fix",
        lambda: observed.append(True) or WorldState(),
        lambda _world: [],
        executor,
    )

    daemon.tick()
    daemon.tick()

    assert observed == [True, True]
    assert gateway.comments == [("issue", 29, "Need clarification")]
    assert Store(str(tmp_path)).load_pending_actions() == ()
