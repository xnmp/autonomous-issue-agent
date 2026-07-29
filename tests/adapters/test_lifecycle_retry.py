"""Durable retries for GitHub lifecycle writes."""

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.journal import Journal, read_events
from issue_agent.adapters.store import Store, decode_lifecycle_action
from issue_agent.app.execute import Executor
from issue_agent.app.loop import Daemon
from issue_agent.domain.actions import (
    AgentKind,
    CloseIssue,
    EscalateCiPending,
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
        self.comment_visible = False

    def comment(self, kind, number, body):
        self.comments.append((kind, number, body))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        self.comment_visible = True

    def has_comment(self, kind, number, body):
        return self.comment_visible

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


def test_ambiguous_comment_failure_is_verified_before_retrying_the_post():
    class LostAcknowledgementGateway:
        def __init__(self):
            self.comments = []
            self.visible = False

        def comment(self, kind, number, body):
            self.comments.append((kind, number, body))
            self.visible = True
            raise unavailable()

        def has_comment(self, kind, number, body):
            return self.visible

    gateway = LostAcknowledgementGateway()
    executor = Executor(
        gateway,
        retry_delays=(0,),
        sleep=lambda _seconds: None,
    )

    executor.execute(PostComment(Target.ISSUE, 29, "Need clarification"))

    assert gateway.comments == [("issue", 29, "Need clarification")]


def test_lost_ack_escalation_replay_still_journals_and_alerts(tmp_path, monkeypatch):
    class LostAcknowledgementGateway:
        def __init__(self):
            self.label_visible = False

        def relabel(self, kind, number, remove, add):
            self.label_visible = True
            raise unavailable("request timed out after GitHub accepted the label")

        def has_label(self, kind, number, label):
            return (
                self.label_visible
                and label == Label.NEEDS_HUMAN_REVIEW.value
            )

    store = Store(str(tmp_path))
    journal = RecordingJournal()
    alerts = []
    monkeypatch.setattr(
        "issue_agent.app.execute.alert",
        lambda command, line: alerts.append((command, line)),
    )
    action = Relabel(
        Target.PR,
        64,
        Label.AI_PR_OPEN,
        Label.NEEDS_HUMAN_REVIEW,
    )
    executor = Executor(
        LostAcknowledgementGateway(),
        journal=journal,
        alert_cmd="notify-operator",
        pending_actions=store,
        retry_delays=(),
    )

    executor.execute_all([action])
    assert store.load_pending_actions() == (action,)

    assert executor.replay_pending() is True

    assert [event for event, _ in journal.events].count("escalation") == 1
    assert len(alerts) == 1
    assert alerts[0][0] == "notify-operator"
    assert store.load_pending_actions() == ()


def test_pending_store_round_trips_every_terminal_lifecycle_action(tmp_path):
    store = Store(str(tmp_path))
    actions = (
        PostComment(Target.ISSUE, 29, "Need clarification"),
        Relabel(Target.PR, 64, Label.AI_WORKING, Label.AI_PR_OPEN),
        CloseIssue(29, "completed", "Done"),
        EscalateCiPending(64, "head-sha", "CI is stalled"),
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


def test_successful_durable_action_is_reconstructable_for_stall_reconciliation(
    tmp_path,
):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gateway = ScriptedGateway([None])
    action = PostComment(Target.ISSUE, 29, "Need clarification")
    executor = Executor(
        gateway,
        journal=journal,
        pending_actions=Store(str(tmp_path / "state")),
        retry_delays=(),
    )
    executor.execute(action)
    accepted_at = datetime.now(timezone.utc)

    gateway.comment_visible = False
    executor.reconcile_recent_actions(
        accepted_at - timedelta(minutes=1),
        accepted_at + timedelta(minutes=1),
    )

    assert gateway.comments == [
        ("issue", 29, "Need clarification"),
        ("issue", 29, "Need clarification"),
    ]
    actions = [event for event in read_events(journal.path) if event["event"] == "action"]
    assert actions[0]["action"] == {
        "type": "comment",
        "target": "ISSUE",
        "number": 29,
        "body": "Need clarification",
    }
    assert actions[-1]["replayed"] is True


def test_stall_reconciliation_verifies_accepted_action_without_duplicate(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    gateway = ScriptedGateway([None])
    action = PostComment(Target.ISSUE, 29, "Need clarification")
    executor = Executor(
        gateway,
        journal=journal,
        pending_actions=Store(str(tmp_path / "state")),
        retry_delays=(),
    )
    executor.execute(action)
    accepted_at = datetime.now(timezone.utc)

    executor.reconcile_recent_actions(
        accepted_at - timedelta(minutes=1),
        accepted_at + timedelta(minutes=1),
    )

    assert gateway.comments == [("issue", 29, "Need clarification")]
    assert any(
        event["event"] == "action_verified"
        and event["desc"] == "comment issue #29 (18 chars)"
        for event in read_events(journal.path)
    )


def test_stall_reconciliation_persists_action_when_verification_is_unavailable(
    tmp_path,
):
    class VerificationUnavailableGateway(ScriptedGateway):
        verification_unavailable = False

        def has_comment(self, kind, number, body):
            if self.verification_unavailable:
                raise unavailable()
            return super().has_comment(kind, number, body)

    journal = Journal(str(tmp_path / "journal.jsonl"))
    store = Store(str(tmp_path / "state"))
    gateway = VerificationUnavailableGateway([None])
    action = PostComment(Target.ISSUE, 29, "Need clarification")
    executor = Executor(
        gateway,
        journal=journal,
        pending_actions=store,
        retry_delays=(),
    )
    executor.execute(action)
    accepted_at = datetime.now(timezone.utc)

    gateway.verification_unavailable = True
    executor.reconcile_recent_actions(
        accepted_at - timedelta(minutes=1),
        accepted_at + timedelta(minutes=1),
    )

    assert store.load_pending_actions() == (action,)
    assert any(
        event["event"] == "action_failed"
        and event["pending"] is True
        and "authentication" in event["error"]
        for event in read_events(journal.path)
    )


def test_stall_reconciliation_keeps_exhausted_missing_action_pending(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    store = Store(str(tmp_path / "state"))
    gateway = ScriptedGateway([None, unavailable()])
    action = PostComment(Target.ISSUE, 29, "Need clarification")
    executor = Executor(
        gateway,
        journal=journal,
        pending_actions=store,
        retry_delays=(),
    )
    executor.execute(action)
    accepted_at = datetime.now(timezone.utc)

    gateway.comment_visible = False
    executor.reconcile_recent_actions(
        accepted_at - timedelta(minutes=1),
        accepted_at + timedelta(minutes=1),
    )

    assert store.load_pending_actions() == (action,)
    assert any(
        event["event"] == "action_failed" and event["pending"] is True
        for event in read_events(journal.path)
    )


def test_stall_reconciliation_verifies_relabel_without_duplicate(tmp_path):
    class LabelGateway:
        def __init__(self):
            self.labels = {Label.AI_WORKING.value}
            self.relabels = []

        def relabel(self, kind, number, remove, add):
            self.relabels.append((kind, number, remove, add))
            if remove:
                self.labels.discard(remove)
            if add:
                self.labels.add(add)

        def has_label(self, kind, number, label):
            return label in self.labels

    journal = Journal(str(tmp_path / "journal.jsonl"))
    gateway = LabelGateway()
    action = Relabel(
        Target.ISSUE,
        29,
        Label.AI_WORKING,
        Label.AI_BLOCKED,
    )
    executor = Executor(gateway, journal=journal, retry_delays=())
    executor.execute(action)
    accepted_at = datetime.now(timezone.utc)

    executor.reconcile_recent_actions(
        accepted_at - timedelta(minutes=1),
        accepted_at + timedelta(minutes=1),
    )

    assert gateway.relabels == [
        ("issue", 29, Label.AI_WORKING.value, Label.AI_BLOCKED.value)
    ]
    assert any(
        event["event"] == "action_verified"
        and event["desc"] == "relabel issue #29: -ai-working +ai-blocked"
        for event in read_events(journal.path)
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "comment", "target": "ISSUE", "number": 29, "body": None},
        {
            "type": "escalate_ci_pending",
            "pr": 64,
            "head": None,
            "body": "CI is stalled",
        },
        {"type": "close", "number": "29", "reason": "completed", "comment": ""},
    ],
)
def test_structured_lifecycle_receipts_reject_malformed_fields(payload):
    with pytest.raises(ValueError, match="invalid lifecycle action"):
        decode_lifecycle_action(payload)


def test_stall_reconciliation_skips_malformed_receipt_without_github_write(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    journal.emit(
        "action",
        desc="comment issue #29",
        action={
            "type": "comment",
            "target": "ISSUE",
            "number": 29,
            "body": None,
        },
    )
    gateway = ScriptedGateway([])
    executor = Executor(gateway, journal=journal, retry_delays=())
    now = datetime.now(timezone.utc)

    executor.reconcile_recent_actions(
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
    )

    assert gateway.comments == []
    assert any(
        event["event"] == "action_reconcile_skipped"
        for event in read_events(journal.path)
    )


def test_malformed_pending_queue_fails_closed_before_observe(tmp_path):
    store = Store(str(tmp_path))
    tmp_path.joinpath("pending-actions.json").write_text("null")
    observed = []
    daemon = Daemon(
        "fix",
        lambda: observed.append(True) or WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 29)],
        Executor(ScriptedGateway([]), pending_actions=store),
    )

    assert daemon.tick() == []
    assert observed == []


def test_unreadable_pending_queue_fails_closed_before_observe(tmp_path):
    store = Store(str(tmp_path))
    tmp_path.joinpath("pending-actions.json").mkdir()
    observed = []
    daemon = Daemon(
        "fix",
        lambda: observed.append(True) or WorldState(),
        lambda _world: [RunAgent(AgentKind.RESOLVE, 29)],
        Executor(ScriptedGateway([]), pending_actions=store),
    )

    assert daemon.tick() == []
    assert observed == []


class CompositeGateway:
    def __init__(self, *, label_visible=False, fail_first_relabel=False):
        self.comment_visible = False
        self.label_visible = label_visible
        self.fail_first_relabel = fail_first_relabel
        self.calls = []

    def comment(self, kind, number, body):
        self.calls.append(("comment", kind, number, body))
        self.comment_visible = True

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))
        if self.fail_first_relabel:
            self.fail_first_relabel = False
            raise unavailable("HTTP 503: Service unavailable")
        self.label_visible = True

    def has_comment(self, kind, number, body):
        return self.comment_visible

    def has_label(self, kind, number, label):
        return self.label_visible


def test_composite_retry_does_not_repost_an_accepted_comment(tmp_path):
    gateway = CompositeGateway(fail_first_relabel=True)
    marked = []
    executor = Executor(
        gateway,
        mark_ci_pending_escalated=lambda pr, head: marked.append((pr, head)),
        pending_actions=Store(str(tmp_path)),
        retry_delays=(0,),
        sleep=lambda _seconds: None,
    )

    executor.execute(EscalateCiPending(64, "head-sha", "CI is stalled"))

    assert [call[0] for call in gateway.calls] == ["comment", "relabel", "relabel"]
    assert marked == [(64, "head-sha")]


def test_composite_replay_repairs_missing_comment_before_clearing_marker(tmp_path):
    action = EscalateCiPending(64, "head-sha", "CI is stalled")
    store = Store(str(tmp_path))
    store.save_pending_actions((action,))
    gateway = CompositeGateway(label_visible=True)
    marked = []
    executor = Executor(
        gateway,
        mark_ci_pending_escalated=lambda pr, head: marked.append((pr, head)),
        pending_actions=store,
        retry_delays=(),
    )

    assert executor.replay_pending() is True

    assert [call[0] for call in gateway.calls] == ["comment"]
    assert marked == [(64, "head-sha")]
    assert store.load_pending_actions() == ()


def test_unverified_composite_is_failed_and_remains_pending(tmp_path):
    class NeverVerifiedGateway(CompositeGateway):
        def has_label(self, kind, number, label):
            return False

    store = Store(str(tmp_path))
    journal = RecordingJournal()
    action = EscalateCiPending(64, "head-sha", "CI is stalled")
    executor = Executor(
        NeverVerifiedGateway(),
        pending_actions=store,
        journal=journal,
        retry_delays=(),
    )

    executor.execute_all([action])

    assert not any(event == "action" for event, _ in journal.events)
    assert any(
        event == "action_failed" and fields["pending"] is True
        for event, fields in journal.events
    )
    assert store.load_pending_actions() == (action,)


def test_unverifiable_pending_escalation_is_quarantined_and_observe_resumes(
    tmp_path,
    monkeypatch,
):
    class NeverVerifiedGateway(CompositeGateway):
        def has_label(self, kind, number, label):
            return False

    store = Store(str(tmp_path))
    journal = RecordingJournal()
    action = EscalateCiPending(64, "head-sha", "CI is stalled")
    store.save_pending_actions((action,))
    observed = []
    alerts = []
    monkeypatch.setattr(
        "issue_agent.app.execute.alert",
        lambda command, line: alerts.append((command, line)),
    )
    daemon = Daemon(
        "fix",
        lambda: observed.append(True) or WorldState(),
        lambda _world: [],
        Executor(
            NeverVerifiedGateway(),
            pending_actions=store,
            journal=journal,
            alert_cmd="notify-operator",
            retry_delays=(),
        ),
    )

    assert daemon.tick() == []

    assert observed == [True]
    assert store.load_pending_actions() == ()
    assert store.load_quarantined_actions() == (action,)
    assert any(event == "action_quarantined" for event, _ in journal.events)
    assert len(alerts) == 1
    assert alerts[0][0] == "notify-operator"
