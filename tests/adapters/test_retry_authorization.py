"""One-attempt retry authorization against isolated SQLite (ADR 0015).

An operator grant buys exactly one execution of an existing reconciliation-only
command, only after the harness's own read proves current absence.
"""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import threading

import pytest

from issue_agent.adapters.control_store import ControlStoreError, SQLiteControlStore
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, workflow_scope
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow import (
    ActiveReconciliation, AuthorizeAbsentRetry, NoAuthorizedRemediation, OpenGate, OperatorActor,
    ReconciliationOnly, RemoteOutcomeUncertain, RetiredSettledCommand, RetryAlreadyConsumed, RetryBegan,
    RetryDeclined, RetryDisposition, RetryStale,
    RetryAuthorizationRequired, WorkflowCommitted, WorkflowConflict, WorkflowRejected,
)
from issue_agent.domain.workflow_identity import WorkflowBinding
from issue_agent.domain.workflow_retry import RetryAbsenceRead
from tests.adapters.test_effects import Gateway
from tests.adapters.test_return_command_recovery import executor, finalized
from tests.adapters.test_workflow_control import ACTOR, NOW, WRITER, adapter, command_row

UNRESOLVED_SINCE = NOW - timedelta(hours=2)


class CompletionGateway(Gateway):
    """Issue facts matching the fixture command's captured completion guard."""

    def __init__(self):
        super().__init__()
        self.issue_revision = "revision:1"
        self.labels = {("issue", 1, Label.AI_WORKING.value)}
        self.fail_relabel = False
        self.fail_reads = False
        self.body = "body"
        self.conversation = ()

    def get_issue(self, issue):
        if self.fail_reads:
            raise ConnectionError("GitHub unavailable")
        return replace(super().get_issue(issue), title="title", body=self.body, author="alice",
                       comments=self.conversation)

    def has_label(self, kind, number, label):
        if self.fail_reads:
            raise ConnectionError("GitHub unavailable")
        return super().has_label(kind, number, label)

    def relabel(self, kind, number, remove, add):
        if self.fail_relabel:
            self.calls.append(("relabel-lost", kind, number, remove, add))
            raise TimeoutError("response lost")
        super().relabel(kind, number, remove, add)

    def writes(self):
        return [call for call in self.calls if call[0].startswith("relabel")]


@pytest.fixture
def clock():
    return [NOW]


@pytest.fixture
def control(tmp_path, clock):
    store = SQLiteControlStore(str(tmp_path / "control.sqlite"), now=lambda: clock[0])
    store.initialize()
    return store


def _expire(control, snapshot, command):
    """Real outbox command whose first write expired into reconciliation-only."""
    control.begin_command(command.command_id)
    control.record_effect(EffectReceipt(command.command_id, WRITER.owner_id, NOW, NOW,
                                        EffectStatus.UNKNOWN_OUTCOME, error="response lost"))
    gate = OpenGate("uncertain", snapshot.binding.fence.generation,
                    RemoteOutcomeUncertain(command.command_id, UNRESOLVED_SINCE, 20, "lost"),
                    NoAuthorizedRemediation("observe"))
    state = ReconciliationOnly(ActiveReconciliation(UNRESOLVED_SINCE, NOW, 20), gate.gate_id)
    with control.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                   (encode_workflow(state), str(command.command_id)))
        db.execute("INSERT INTO workflow_gates VALUES (?,?,?,?,?,?,'open',?)",
                   (gate.gate_id, *workflow_scope(snapshot.binding.fence.key), gate.opened_generation,
                    "uncertain", encode_workflow(gate)))


@pytest.fixture
def absent(control):
    """Expired command whose harness read proved current absence."""
    snapshot, command = finalized(control)
    _expire(control, snapshot, command)
    gateway = CompletionGateway()
    assert executor(control, gateway).drain_pending_commands() == 0
    assert gateway.writes() == []
    return command, gateway


def _current(control, command):
    return adapter(control).load_workflow(command.workflow.fence.key)


def _gate(control, command):
    return next(g for g in _current(control, command).gates if getattr(g, "gate_id", None) == "uncertain")


def _grant(control, command, authorization_id="grant-1", **changes):
    state = _current(control, command)
    gate = _gate(control, command)
    assert isinstance(gate.cause, RetryAuthorizationRequired)
    values = dict(authorization_id=authorization_id, expected=state.revision, command_id=command.command_id,
                  gate_id=gate.gate_id, evidence_id=gate.cause.absence_evidence_id)
    values.update(changes)
    return AuthorizeAbsentRetry(**values)


def _authorize(control, grant, actor=ACTOR, binding=None):
    return adapter(control).authorize_retry(grant, actor, binding=binding)


def _grant_row(control, authorization_id="grant-1"):
    with closing(control._connect()) as db:
        return db.execute("SELECT * FROM workflow_retry_authorizations WHERE authorization_id=?",
                          (authorization_id,)).fetchone()


def _read(clock, revision="revision:1", guard=True):
    return RetryAbsenceRead(clock[0], revision, guard)


def _begin(store, command, state, clock, authorization_id="grant-1", *, attempts=1, identity=None, **read):
    return store.begin_authorized_retry(
        command.command_id, authorization_id, expected=state, expected_attempts=attempts,
        read=_read(clock, **read), executor_identity=identity or WRITER.owner_id,
    )


def _offered(control, command):
    view = adapter(control).resolve_control_view(command.workflow.fence.key)
    return "uncertain" in view.retry_authorizable


def test_absence_offers_authorization_and_acceptance_persists_one_grant_without_new_allowance(control, absent):
    command, _ = absent
    before = _current(control, command)
    history = control.command_reconciliation(command.command_id)
    assert _offered(control, command)
    grant = _grant(control, command)
    result = _authorize(control, grant, binding=before.binding)
    assert isinstance(result, WorkflowCommitted) and not result.idempotent
    after = _current(control, command)
    assert after.revision.fence == before.revision.fence
    assert after.revision.version == before.revision.version + 1
    assert after.episode == before.episode and after.intent == before.intent and after.gates == before.gates
    assert control.command_reconciliation(command.command_id) == history
    row = _grant_row(control)
    assert row["consumed_attempt"] is None and row["association_kind"] == "standalone"
    assert row["request_id"] is None and row["generation"] == before.binding.fence.generation
    assert "local" not in row["actor_json"] and ACTOR.subject in row["actor_json"]
    assert len(row["request_digest"]) == 64
    assert not _offered(control, command)


def test_exact_replay_returns_original_and_changed_content_conflicts(control, absent):
    command, _ = absent
    grant = _grant(control, command)
    assert isinstance(_authorize(control, grant), WorkflowCommitted)
    replay = _authorize(control, grant, actor=OperatorActor(ACTOR.subject, "cli"))
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent
    assert isinstance(_authorize(control, replace(grant, evidence_id="other")), WorkflowConflict)
    assert isinstance(_authorize(control, grant, actor=OperatorActor("operator:mallory", "dashboard")), WorkflowConflict)
    stale_binding = replace(_current(control, command).binding, episode_id="another-episode")
    assert isinstance(_authorize(control, grant, binding=replace(stale_binding, fence=grant.expected.fence)), WorkflowConflict)
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM workflow_retry_authorizations").fetchone()[0] == 1


def test_second_grant_while_one_is_unconsumed_conflicts(control, absent):
    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    second = _authorize(control, _grant(control, command, "grant-2"))
    assert isinstance(second, WorkflowConflict) and "unconsumed" in second.reason
    assert _grant_row(control, "grant-2") is None


def test_stale_revision_and_foreign_binding_conflict(control, absent):
    command, _ = absent
    grant = _grant(control, command)
    stale = replace(grant, expected=replace(grant.expected, version=grant.expected.version - 1))
    assert isinstance(_authorize(control, stale), WorkflowConflict)
    current = _current(control, command).binding
    assert isinstance(_authorize(control, grant, binding=replace(current, episode_id="other")), WorkflowConflict)
    assert _grant_row(control) is None


@pytest.mark.parametrize("field,value", [
    ("gate_id", "missing-gate"), ("evidence_id", "fabricated-evidence"), ("command_id", "unknown-command"),
    ("evidence_id", "x" * 512),
])
def test_wrong_gate_command_or_evidence_is_rejected(control, absent, field, value):
    command, _ = absent
    result = _authorize(control, _grant(control, command, **{field: value}))
    assert isinstance(result, WorkflowRejected)
    assert _grant_row(control) is None


def test_non_absence_evidence_named_by_a_forged_gate_is_rejected(control, absent):
    command, _ = absent
    gate = _gate(control, command)
    forged = replace(gate, cause=RetryAuthorizationRequired(command.command_id, "observation-evidence"))
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, ?, 'observation', '{}')",
                   ("observation-evidence", *workflow_scope(command.workflow.fence.key), str(command.command_id)))
        db.execute("UPDATE workflow_gates SET payload_json=? WHERE gate_id=?", (encode_workflow(forged), gate.gate_id))
    assert not _offered(control, command)
    result = _authorize(control, _grant(control, command))
    assert isinstance(result, WorkflowRejected) and "harness" in result.reason


def test_retired_command_can_never_be_authorized(control, absent):
    command, _ = absent
    state = control.command_reconciliation(command.command_id)
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_evidence VALUES ('retired', ?, ?, ?, ?, 'observation', '{}')",
                   (*workflow_scope(command.workflow.fence.key), str(command.command_id)))
        from issue_agent.domain.workflow import RetiredUncertainCommand
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                   (encode_workflow(RetiredUncertainCommand(state, "retired")), str(command.command_id)))
    assert not _offered(control, command)
    result = _authorize(control, _grant(control, command))
    assert isinstance(result, WorkflowRejected) and "retired" in result.reason


def test_drain_spends_grant_on_exactly_one_write_and_closes_gate(control, absent, clock):
    command, gateway = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 1
    assert len(gateway.writes()) == 1
    receipt = control.effect_receipt(command.command_id)
    assert receipt is not None and receipt.status is EffectStatus.SUCCEEDED
    assert command_row(control, command.command_id)["attempts"] == 2
    assert _grant_row(control)["consumed_attempt"] == 2
    assert control.command_reconciliation(command.command_id) is None
    assert all(getattr(g, "gate_id", None) != "uncertain" for g in _current(control, command).gates)
    for _ in range(3):
        clock[0] += timedelta(hours=1)
        assert executor(control, gateway).drain_pending_commands() == 0
    assert len(gateway.writes()) == 1
    replay = _authorize(control, _grant_row_command(control, command))
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent
    assert executor(control, gateway).drain_pending_commands() == 0
    assert len(gateway.writes()) == 1


def _grant_row_command(control, command):
    from issue_agent.adapters.workflow_codec import decode_workflow
    from issue_agent.domain.workflow import AuthorizedOperatorCommand

    return decode_workflow(_grant_row(control)["actor_json"], AuthorizedOperatorCommand).command


def test_present_on_recheck_settles_without_spending_the_grant(control, absent, clock):
    command, gateway = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    gateway.labels = {("issue", 1, Label.AI_READY.value)}  # The lost write landed late.
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 1
    assert gateway.writes() == []
    assert _grant_row(control)["consumed_attempt"] is None
    assert _grant_row(control)["disposition"] == "not_needed"
    assert command_row(control, command.command_id)["attempts"] == 1
    assert control.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
    assert all(getattr(g, "gate_id", None) != "uncertain" for g in _current(control, command).gates)


def test_transient_read_failure_keeps_the_grant_open_under_backoff_then_spends_it(control, absent, clock, tmp_path):
    command, gateway = absent
    granted = _gate(control, command).cause.absence_evidence_id
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    grants = _journal(control, tmp_path)
    gateway.fail_reads = True
    clock[0] += timedelta(seconds=5)
    logs = []
    assert executor(control, gateway, logs).drain_pending_commands() == 0
    assert gateway.writes() == []
    row = _grant_row(control)
    assert row["disposition"] is None and row["unavailable_reads"] == 1
    # The gate keeps the grant's absence; only the read schedule backs off.
    assert _gate(control, command).cause == RetryAuthorizationRequired(command.command_id, granted)
    state = control.command_reconciliation(command.command_id)
    assert state.history.unresolved_since == UNRESOLVED_SINCE and state.history.next_read_at > clock[0]
    assert [name for name, _ in grants()] == ["deferred"]
    assert executor(control, gateway, logs).drain_pending_commands() == 0  # not yet due
    assert _grant_row(control)["unavailable_reads"] == 1
    gateway.fail_reads = False
    clock[0] = state.history.next_read_at
    assert executor(control, gateway, logs).drain_pending_commands() == 1
    assert len(gateway.writes()) == 1 and _grant_row(control)["disposition"] == "consumed"


def test_new_human_comment_between_grant_and_spend_makes_the_completion_retry_inapplicable(control, absent, clock):
    """ADR 0015: the retry proves the ORIGINAL decision, and a completion's guard
    includes the conversation. A completion that asked a question must never be
    re-posted over the human's answer."""
    from issue_agent.domain.world import Comment

    command, gateway = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    gateway.issue_revision = "revision:2"
    gateway.conversation = (Comment("alice", "Use the v2 endpoint.", "revision:2"),)
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 0
    assert gateway.writes() == []
    assert _grant_row(control)["consumed_attempt"] is None
    assert _grant_row(control)["disposition"] == "inapplicable"
    assert isinstance(control.command_reconciliation(command.command_id), RetiredSettledCommand)


def test_changed_guard_retires_instead_of_adopting_new_revision(control, absent, clock):
    command, gateway = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    # The completion's requirements changed: its original decision no longer applies.
    gateway.issue_revision = "revision:2"
    gateway.body = "new requirements"
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 0
    assert gateway.writes() == []
    assert _grant_row(control)["consumed_attempt"] is None
    assert _grant_row(control)["disposition"] == "inapplicable"
    retired = control.command_reconciliation(command.command_id)
    assert isinstance(retired, RetiredSettledCommand)
    assert command.expected_revision == "revision:1"
    assert command_row(control, command.command_id)["expected_revision"] == "revision:1"
    assert all(getattr(g, "gate_id", None) != "uncertain" for g in _current(control, command).gates)
    for _ in range(2):
        clock[0] += timedelta(hours=1)
        assert executor(control, gateway).drain_pending_commands() == 0
    assert gateway.writes() == []


def test_unknown_after_consumption_keeps_original_history_and_needs_a_new_grant(control, absent, clock):
    command, gateway = absent
    original = control.command_reconciliation(command.command_id)
    first_evidence = _gate(control, command).cause.absence_evidence_id
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    gateway.fail_relabel = True
    clock[0] += timedelta(seconds=5)
    logs = []
    assert executor(control, gateway, logs).drain_pending_commands() == 0
    assert len(gateway.writes()) == 1 and len(logs) == 1
    assert _grant_row(control)["consumed_attempt"] == 2
    row = command_row(control, command.command_id)
    assert row["state"] == "unknown_outcome" and row["attempts"] == 2
    unknown = control.command_reconciliation(command.command_id)
    assert unknown.gate_id == original.gate_id
    assert unknown.history.unresolved_since == UNRESOLVED_SINCE
    assert unknown.history.failed_reads == original.history.failed_reads
    # The gate reports the attempt's unknown outcome, never the spent absence.
    reported = _gate(control, command).cause
    assert isinstance(reported, RemoteOutcomeUncertain) and "attempt 2 outcome unknown" in reported.last_error
    assert not _offered(control, command)
    # The replayed authorization cannot buy another execution, however often.
    for _ in range(3):
        clock[0] += timedelta(hours=1)
        executor(control, gateway, logs).drain_pending_commands()
    assert len(gateway.writes()) == 1
    later = control.command_reconciliation(command.command_id)
    assert later.history.unresolved_since == original.history.unresolved_since == UNRESOLVED_SINCE
    assert later.history.failed_reads == original.history.failed_reads
    fresh = _gate(control, command)
    assert isinstance(fresh.cause, RetryAuthorizationRequired) and fresh.cause.absence_evidence_id != first_evidence
    # The spent observation cannot be reused; a fresh grant is required.
    spent = _authorize(control, _grant(control, command, "grant-2", evidence_id=first_evidence))
    assert isinstance(spent, WorkflowRejected)
    assert _offered(control, command)
    gateway.fail_relabel = False
    assert isinstance(_authorize(control, _grant(control, command, "grant-2")), WorkflowCommitted)
    assert executor(control, gateway).drain_pending_commands() == 1
    assert len(gateway.writes()) == 2
    assert _grant_row(control, "grant-2")["consumed_attempt"] == 3


def test_crash_before_consumption_leaves_grant_available_after_restart(control, absent, clock, monkeypatch):
    command, gateway = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)

    def crash(*_args, **_kwargs):
        raise SystemExit("killed before consumption")

    monkeypatch.setattr(SQLiteControlStore, "begin_authorized_retry", crash)
    clock[0] += timedelta(seconds=5)
    with pytest.raises(SystemExit):
        executor(control, gateway).drain_pending_commands()
    monkeypatch.undo()
    row = _grant_row(control)
    assert row["disposition"] is None and row["first_visit_at"] is not None and gateway.writes() == []
    restarted = SQLiteControlStore(control.path, now=lambda: clock[0])
    # The bypass was spent by the killed visit; the grant waits for the schedule.
    assert executor(restarted, gateway).drain_pending_commands() == 0
    clock[0] = restarted.command_reconciliation(command.command_id).history.next_read_at
    assert executor(restarted, gateway).drain_pending_commands() == 1
    assert len(gateway.writes()) == 1


@pytest.mark.parametrize("present", [False, True])
def test_crash_after_consumption_reconciles_read_only_after_restart(control, absent, clock, present):
    command, gateway = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    state = control.command_reconciliation(command.command_id)
    began = _begin(control, command, state, clock)
    assert isinstance(began, RetryBegan) and began.consumed.command_attempt == 2
    # Process death: no receipt. The write may or may not have landed.
    if present:
        gateway.labels = {("issue", 1, Label.AI_READY.value)}
    restarted = SQLiteControlStore(control.path, now=lambda: clock[0])
    for _ in range(3):
        clock[0] += timedelta(hours=1)
        executor(restarted, gateway).drain_pending_commands()
    assert gateway.writes() == []
    if present:
        assert restarted.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
        assert all(g.gate_id != "uncertain" for g in _current(restarted, command).gates)
    else:
        assert command_row(restarted, command.command_id)["state"] == "executing"
        assert restarted.command_reconciliation(command.command_id).history.unresolved_since == UNRESOLVED_SINCE
    again = _begin(restarted, command, state, clock)
    assert isinstance(again, RetryAlreadyConsumed)


def test_concurrent_consumers_begin_exactly_one_attempt(control, absent, clock):
    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    state = control.command_reconciliation(command.command_id)
    barrier = threading.Barrier(4)
    outcomes = []

    def consume():
        store = SQLiteControlStore(control.path, now=lambda: clock[0])
        barrier.wait()
        try:
            outcomes.append(_begin(store, command, state, clock))
        except (ControlStoreError, WorkflowAuthorityError) as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=consume) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sum(isinstance(item, RetryBegan) for item in outcomes) == 1
    assert len(outcomes) == 4
    assert command_row(control, command.command_id)["attempts"] == 2


def test_consumption_requires_the_grant_executor_and_original_revision(control, absent, clock):
    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    state = control.command_reconciliation(command.command_id)
    with pytest.raises(WorkflowAuthorityError):
        _begin(control, command, state, clock, identity="o/r:review:other")
    with pytest.raises(WorkflowAuthorityError):
        _begin(control, command, state, clock, "missing-grant")
    assert _grant_row(control)["disposition"] is None
    declined = _begin(control, command, state, clock, revision="revision:2", guard=False)
    assert isinstance(declined, RetryDeclined) and declined.closed.disposition is RetryDisposition.INAPPLICABLE
    assert _grant_row(control)["consumed_attempt"] is None
    assert command_row(control, command.command_id)["attempts"] == 1
    # A terminal grant answers replays with its disposition, never an attempt.
    assert _begin(control, command, state, clock) == declined


def test_rearm_obligation_association_is_minted_from_linkage(control):
    from tests.adapters.test_return_command_recovery import rearm
    from issue_agent.domain.workflow import PendingRearm, RearmObligation
    from issue_agent.adapters.control_store import load_retry_authorization_facts

    snapshot, _ = finalized(control)
    renewed = rearm(control, snapshot)
    assert isinstance(renewed.intent, PendingRearm)
    obligation = renewed.intent.required_commands[0]
    with closing(control._connect()) as db:
        facts = load_retry_authorization_facts(control, db, obligation, "absent")
    assert facts.association == RearmObligation(renewed.intent.request_id)
    assert facts.evidence is None and facts.prior == ()
    assert isinstance(facts.command_binding, WorkflowBinding)


# --- review 2026-09-25: every grant ends once; no refusal can loop ---

class CountingGateway(CompletionGateway):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def get_issue(self, issue):
        self.reads += 1
        return super().get_issue(issue)

    def has_label(self, kind, number, label):
        self.reads += 1
        return super().has_label(kind, number, label)


def _journal(control, tmp_path):
    from pathlib import Path
    from issue_agent.adapters.event_log import EventLog
    from issue_agent.domain.events import ControlTransitionCommitted, Producer, decode_event_line

    events = EventLog(str(tmp_path / "events-v2.jsonl"), Producer("deployment", "o/r", "fix", 123, "boot"))
    control.bind_event_outbox(events.prepare, events.append_committed, destination=events.producer.feed_identity)

    def grants():
        path = Path(events.path)
        lines = path.read_text().splitlines() if path.exists() else []
        return [(event.payload.state, dict(event.payload.attributes)) for event in map(decode_event_line, lines)
                if isinstance(event.payload, ControlTransitionCommitted)
                and event.payload.entity_kind == "workflow_retry_authorization"]
    return grants


def _ticks(control, gateway, clock, count, logs=None):
    """Drain `count` one-minute ticks; returns GitHub reads made after the first."""
    first = None
    for _ in range(count):
        clock[0] += timedelta(seconds=60)
        executor(control, gateway, logs).drain_pending_commands()
        first = gateway.reads if first is None else first
    return gateway.reads - first


def test_another_hold_is_refused_at_acceptance_and_never_offered(control, absent):
    from tests.adapters.test_return_command_recovery import add_hold

    command, _ = absent
    add_hold(control, _current(control, command))
    assert not _offered(control, command)
    rejected = _authorize(control, _grant(control, command))
    assert isinstance(rejected, WorkflowRejected) and "another gate" in rejected.reason
    assert _grant_row(control) is None


def test_hold_after_grant_voids_once_with_bounded_reads_and_rotated_evidence(control, absent, clock, tmp_path):
    from tests.adapters.test_return_command_recovery import add_hold

    command, _ = absent
    granted = _gate(control, command).cause.absence_evidence_id
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    add_hold(control, _current(control, command))
    grants = _journal(control, tmp_path)
    gateway, logs = CountingGateway(), []
    # One visit reads the postcondition and the guard, then the schedule holds.
    assert _ticks(control, gateway, clock, 10, logs) == 0 and gateway.reads > 0
    assert gateway.writes() == []
    row = _grant_row(control)
    assert row["disposition"] == "void" and "another gate" in row["disposition_reason"]
    assert row["consumed_attempt"] is None
    assert [state for state, _ in grants()] == ["void"]
    rotated = _gate(control, command).cause
    assert isinstance(rotated, RetryAuthorizationRequired) and rotated.absence_evidence_id != granted
    state = control.command_reconciliation(command.command_id)
    assert state.history.next_read_at >= clock[0] + timedelta(minutes=20)
    assert command_row(control, command.command_id)["attempts"] == 1
    assert any("void" in line for line in logs)


def test_generation_change_after_grant_ends_it_without_a_write(control, absent, clock):
    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    # The operator's re-arm path is blocked by these gates in this fixture, so
    # advance the fence directly: only the stale binding matters here.
    with control.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1, version=version+1")
    gateway = CountingGateway()
    assert _ticks(control, gateway, clock, 10) == 0
    assert gateway.writes() == []
    # The superseded return retires; its one read settles current absence
    # and ends the grant in the same transaction.
    assert isinstance(control.command_reconciliation(command.command_id), RetiredSettledCommand)
    assert all(getattr(g, "gate_id", None) != "uncertain" for g in _current(control, command).gates)
    assert _grant_row(control)["disposition"] == "void"
    assert command_row(control, command.command_id)["attempts"] == 1


def test_repeated_faults_before_consumption_back_off_then_void_at_the_ceiling(control, absent, clock, monkeypatch, tmp_path):
    from issue_agent.domain.workflow_retry import MAX_GRANT_UNAVAILABLE_READS

    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    grants = _journal(control, tmp_path)

    def broken(*_args, **_kwargs):
        raise ControlStoreError("injected spend fault")

    monkeypatch.setattr(SQLiteControlStore, "begin_authorized_retry", broken)
    gateway, logs = CountingGateway(), []
    visits = ticks = 0
    while _grant_row(control)["disposition"] is None and ticks < 60:
        before = gateway.reads
        clock[0] += timedelta(minutes=5)  # The backoff here is 30 minutes.
        executor(control, gateway, logs).drain_pending_commands()
        visits += gateway.reads > before
        ticks += 1
    assert gateway.writes() == []
    # One bypass, then backoff: never a per-tick loop, and a bounded count.
    assert visits == MAX_GRANT_UNAVAILABLE_READS and ticks > 4 * visits
    row = _grant_row(control)
    assert row["disposition"] == "void" and "ceiling" in row["disposition_reason"]
    assert [name for name, _ in grants()] == ["deferred"] * (MAX_GRANT_UNAVAILABLE_READS - 1) + ["void"]


def test_read_taken_before_an_attempt_cannot_back_absence_after_it(control, absent, clock):
    """A concurrent drain's pre-write read must not rotate the gate afterwards."""
    command, _ = absent
    before = control.command_reconciliation(command.command_id)
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] += timedelta(seconds=5)
    assert isinstance(_begin(control, command, before, clock), RetryBegan)
    # Mid-write the gate already reports the attempt, not the spent absence.
    assert isinstance(_gate(control, command).cause, RemoteOutcomeUncertain)
    assert not _offered(control, command)
    assert not control.record_reconciliation_read(command.command_id, before, expected_attempts=1, read_started_at=clock[0],
                                                  executor_identity=WRITER.owner_id, observed=False)
    assert isinstance(_gate(control, command).cause, RemoteOutcomeUncertain)
    # A read that began after consumption but landed mid-write is superseded
    # by the attempt's unknown outcome, and a read from before the settlement
    # cannot land after it.
    during = control.command_reconciliation(command.command_id)
    assert during != before
    control.record_reconciliation_read(command.command_id, during, expected_attempts=2, read_started_at=clock[0],
                                       executor_identity=WRITER.owner_id, observed=False)
    mid_write = control.command_reconciliation(command.command_id)
    control.record_effect(EffectReceipt(command.command_id, WRITER.owner_id, clock[0], clock[0],
                                        EffectStatus.UNKNOWN_OUTCOME, error="response lost"))
    assert isinstance(_gate(control, command).cause, RemoteOutcomeUncertain)
    assert not _offered(control, command)
    assert not control.record_reconciliation_read(command.command_id, mid_write, expected_attempts=2, read_started_at=clock[0],
                                                  executor_identity=WRITER.owner_id, observed=False)
    assert isinstance(_gate(control, command).cause, RemoteOutcomeUncertain)


def test_absence_recorded_before_the_attempt_settled_cannot_buy_a_grant(control, absent, clock):
    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] += timedelta(seconds=5)
    assert isinstance(_begin(control, command, control.command_reconciliation(command.command_id), clock), RetryBegan)
    consumed_at = clock[0]
    # A read stamped at the consumption instant, recorded with current CAS facts.
    control.record_reconciliation_read(command.command_id, control.command_reconciliation(command.command_id),
                                       expected_attempts=2, read_started_at=clock[0], executor_identity=WRITER.owner_id, observed=False)
    gate = _gate(control, command)
    assert isinstance(gate.cause, RetryAuthorizationRequired)
    assert clock[0] == consumed_at
    assert not _offered(control, command)
    rejected = _authorize(control, _grant(control, command, "grant-2"))
    assert isinstance(rejected, WorkflowRejected) and "last authorized attempt" in rejected.reason


def test_evidence_is_bound_to_the_executor_read_not_the_store_clock(control, absent, clock):
    from issue_agent.adapters.workflow_codec import decode_workflow
    from issue_agent.domain.workflow import CommandAbsenceEvidence

    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    state = control.command_reconciliation(command.command_id)
    read_at = clock[0] + timedelta(seconds=2)
    clock[0] += timedelta(seconds=30)
    began = control.begin_authorized_retry(command.command_id, "grant-1", expected=state, expected_attempts=1,
                                           read=RetryAbsenceRead(read_at, "revision:1", True),
                                           executor_identity=WRITER.owner_id)
    assert isinstance(began, RetryBegan)
    with closing(control._connect()) as db:
        rows = db.execute("SELECT payload_json FROM workflow_evidence WHERE evidence_id LIKE 'retry-absence:%'").fetchall()
    [evidence] = [decode_workflow(row[0], CommandAbsenceEvidence) for row in rows]
    assert evidence.observed_at == read_at
    assert evidence.remote_revision.startswith("current-postcondition:")
    assert evidence.remote_revision != "current-postcondition:revision:1"


@pytest.mark.parametrize("offset", [timedelta(seconds=-301), timedelta(seconds=1)])
def test_stale_or_future_read_voids_without_minting_evidence(control, absent, clock, offset):
    command, _ = absent
    granted = _gate(control, command).cause.absence_evidence_id
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] += timedelta(seconds=400)
    state = control.command_reconciliation(command.command_id)
    declined = control.begin_authorized_retry(
        command.command_id, "grant-1", expected=state, expected_attempts=1,
        read=RetryAbsenceRead(clock[0] + offset, "revision:1", True), executor_identity=WRITER.owner_id,
    )
    assert isinstance(declined, RetryDeclined) and declined.closed.disposition is RetryDisposition.VOID
    assert _gate(control, command).cause.absence_evidence_id == granted
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM workflow_evidence WHERE evidence_id LIKE 'retry-absence:%'").fetchone()[0] == 0
    assert command_row(control, command.command_id)["attempts"] == 1


def test_moved_state_or_attempt_is_stale_and_spends_nothing(control, absent, clock):
    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    state = control.command_reconciliation(command.command_id)
    moved = replace(state, history=replace(state.history, next_read_at=state.history.next_read_at + timedelta(seconds=1)))
    assert isinstance(_begin(control, command, moved, clock), RetryStale)
    assert isinstance(_begin(control, command, state, clock, attempts=0), RetryStale)
    assert _grant_row(control)["disposition"] is None


def test_every_grant_disposition_is_terminal_and_one_open_grant_per_command(control, absent, clock):
    import sqlite3

    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    with pytest.raises(sqlite3.IntegrityError):
        with control.transaction() as db:
            db.execute("INSERT INTO workflow_retry_authorizations (authorization_id, request_id, command_id, gate_id, "
                       "association_kind, binding_json, evidence_id, request_digest, actor_json, generation) "
                       "SELECT 'grant-2', request_id, command_id, gate_id, association_kind, binding_json, evidence_id, "
                       "request_digest, actor_json, generation FROM workflow_retry_authorizations")
    clock[0] += timedelta(seconds=5)
    assert executor(control, CompletionGateway()).drain_pending_commands() == 1
    row = _grant_row(control)
    assert row["disposition"] == "consumed" and row["consumed_attempt"] == 2 and row["disposed_at"]
    for change in ("disposition='void', consumed_attempt=NULL", "disposition_reason='rewritten'", "evidence_id='other'"):
        with pytest.raises(sqlite3.IntegrityError):
            with control.transaction() as db:
                db.execute(f"UPDATE workflow_retry_authorizations SET {change}")
    # A deterministic gate id can never rematch a terminal grant.
    assert control.retry_authorization(command.command_id) is None


# --- re-arm obligations: executor-side spend (review 2026-09-25) ---

@pytest.fixture
def obligation(control):
    """A required re-arm relabel whose attempt expired, then read absent."""
    from tests.adapters.test_workflow_control import commit, held, install_workflow, prepare_workspace, request

    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    prepare_workspace(control, pending)
    command = next(item.command for item in control.pending_commands(repository="o/r", executor_identity=WRITER.owner_id)
                   if item.command.command_id == proposal.commands[0].command_id)
    _expire(control, pending, command)
    gateway = CountingGateway()
    gateway.labels = {("issue", 1, Label.NEEDS_HUMAN_REVIEW.value)}  # the accepted hold facts
    assert executor(control, gateway).drain_pending_commands() == 0
    assert isinstance(_gate(control, command).cause, RetryAuthorizationRequired)
    return command, gateway, pending


def _reconcile_rearm(control, clock, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}), workspace=None):
    from issue_agent.domain.workflow import NoLinkedPr
    from tests.adapters.test_workflow_control import observation

    current = _current(control, _any_command(control))
    return adapter(control).reconcile_rearm(
        current.revision, observation(current, labels, at=clock[0]),
        NoLinkedPr(current.revision.fence.key, clock[0]), workspace_verified=workspace,
    )


def _any_command(control):
    from issue_agent.adapters.control_store import _command_from_row

    with closing(control._connect()) as db:
        return _command_from_row(db.execute("SELECT * FROM lifecycle_commands LIMIT 1").fetchone())


def test_rearm_obligation_grant_spends_on_exactly_one_write(control, obligation, clock):
    command, gateway, pending = obligation
    assert _offered(control, command)
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    assert (_grant_row(control)["association_kind"], _grant_row(control)["request_id"]) == ("rearm", pending.intent.request_id)
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 1
    assert len(gateway.writes()) == 1
    assert _grant_row(control)["disposition"] == "consumed"
    assert control.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
    assert all(getattr(g, "gate_id", None) != "uncertain" for g in _current(control, command).gates)


def test_inapplicable_obligation_ends_its_grant_and_can_never_be_granted_again(control, obligation, clock):
    command, gateway, _ = obligation
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    gateway.issue_revision = "revision:2"
    gateway.labels.add(("issue", 1, Label.RECURRENCE.value))  # a real intervention
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 0
    assert gateway.writes() == []
    row = _grant_row(control)
    assert row["disposition"] == "inapplicable" and row["consumed_attempt"] is None
    assert control.retry_authorization(command.command_id) is None
    # The obligation keeps its restriction (its intent owns retirement) and
    # its fresh absence, but no grant can revive it — not even after the
    # guard returns to its original value (ABA).
    assert isinstance(control.command_reconciliation(command.command_id), ReconciliationOnly)
    assert isinstance(_gate(control, command).cause, RetryAuthorizationRequired)
    gateway.issue_revision = "revision:1"
    gateway.labels.discard(("issue", 1, Label.RECURRENCE.value))
    assert not _offered(control, command)
    rejected = _authorize(control, _grant(control, command, "grant-2"))
    assert isinstance(rejected, WorkflowRejected) and "inapplicable" in rejected.reason
    for _ in range(3):
        clock[0] += timedelta(hours=1)
        executor(control, gateway).drain_pending_commands()
    assert gateway.writes() == []


@pytest.mark.parametrize("activity", ["comment", "body", "priority"])
def test_harmless_activity_keeps_the_obligation_retry_applicable(control, obligation, clock, activity):
    """ADR 0015 semantic fence: only hold facts decide a projection's applicability."""
    command, gateway, _ = obligation
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    gateway.issue_revision = "revision:2"
    if activity == "comment":
        gateway.comments.add(("issue", 1, "is this unblocked yet?"))
    elif activity == "body":
        gateway.body = "clarified requirements"
    else:
        gateway.labels.add(("issue", 1, Label.P1.value))
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 1
    assert len(gateway.writes()) == 1
    assert _grant_row(control)["disposition"] == "consumed"
    assert control.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED


def test_obligation_half_applied_by_its_own_attempt_stays_applicable(control, obligation, clock):
    """The retry exists to repair its own partial write; that is never a changed guard."""
    command, gateway, _ = obligation
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    gateway.labels.add(("issue", 1, Label.AI_READY.value))  # own add landed, hold removal did not
    gateway.issue_revision = "revision:2"
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 1
    assert _grant_row(control)["disposition"] == "consumed"
    assert len(gateway.writes()) == 1
    assert {label for kind, number, label in gateway.labels} == {Label.AI_READY.value}
    assert control.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED


def test_obligation_retired_after_grant_voids_it_without_a_write(control, obligation, clock):
    # Cancelling this particular pending re-arm is blocked by the pre-existing
    # reconciliation-only re-arm facts wedge (tracked separately); the
    # durable part a stop leaves behind is the retired obligation link, and
    # the spend decision must refuse it on its own.
    command, gateway, _ = obligation
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    with control.transaction() as db:
        db.execute("UPDATE workflow_rearm_commands SET disposition='retired' WHERE command_id=?", (str(command.command_id),))
    assert not _offered(control, command)
    assert _ticks(control, gateway, clock, 10) == 0
    assert gateway.writes() == []
    row = _grant_row(control)
    assert row["disposition"] == "void" and row["disposition_reason"] == "the re-arm request owning this obligation is no longer pending"
    assert control.command_reconciliation(command.command_id).history.next_read_at >= clock[0] + timedelta(minutes=20)


def test_v23_backfills_legacy_consumed_grants_as_terminal(control, absent, clock):
    command, gateway = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 1
    with control.transaction() as db:  # Isolated v22 fixture, never a supported rollback.
        for statement in ("DROP INDEX effect_receipts_remote_object",
                          "ALTER TABLE lifecycle_commands DROP COLUMN prewrite_read_json",
                          "DELETE FROM schema_migrations WHERE version=24",
                          "DROP TRIGGER workflow_retry_grant_insert", "DROP TRIGGER workflow_retry_grant_update",
                          "DROP INDEX workflow_retry_open_grant",
                          "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposition",
                          "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposed_at",
                          "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposition_reason",
                          "ALTER TABLE workflow_retry_authorizations DROP COLUMN accepted_at",
                          "ALTER TABLE workflow_retry_authorizations DROP COLUMN first_visit_at",
                          "ALTER TABLE workflow_retry_authorizations DROP COLUMN unavailable_reads",
                          "DELETE FROM schema_migrations WHERE version=23"):
            db.execute(statement)
    SQLiteControlStore(control.path, now=lambda: clock[0]).initialize()
    row = _grant_row(control)
    assert row["disposition"] == "consumed" and row["consumed_attempt"] == 2
    assert row["disposed_at"] == command_row(control, command.command_id)["updated_at"]


def test_attempt_count_fences_a_pre_attempt_read_even_when_the_schedule_coincides(control, absent, clock):
    """The rewritten schedule can equal the old one; the attempt count still differs."""
    command, _ = absent
    before = control.command_reconciliation(command.command_id)
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] = before.history.next_read_at - timedelta(seconds=30)
    stale_read = RetryAbsenceRead(clock[0], "revision:1", True)
    gate = _gate(control, command)
    with control.transaction() as db:  # Keep the grant's evidence fresh at this instant.
        db.execute("UPDATE workflow_evidence SET payload_json=? WHERE evidence_id=?", (
            encode_workflow(replace(_absence(control, gate.cause.absence_evidence_id), observed_at=clock[0] - timedelta(seconds=1))),
            gate.cause.absence_evidence_id,
        ))
    began = control.begin_authorized_retry(command.command_id, "grant-1", expected=before, expected_attempts=1,
                                           read=stale_read, executor_identity=WRITER.owner_id)
    assert isinstance(began, RetryBegan)
    assert control.command_reconciliation(command.command_id) == before
    assert not control.record_reconciliation_read(command.command_id, before, expected_attempts=1, read_started_at=clock[0],
                                                  executor_identity=WRITER.owner_id, observed=False)
    assert isinstance(_gate(control, command).cause, RemoteOutcomeUncertain)


def _absence(control, evidence_id):
    from issue_agent.adapters.workflow_codec import decode_workflow
    from issue_agent.domain.workflow import CommandAbsenceEvidence

    with closing(control._connect()) as db:
        return decode_workflow(db.execute("SELECT payload_json FROM workflow_evidence WHERE evidence_id=?",
                                          (evidence_id,)).fetchone()[0], CommandAbsenceEvidence)



# --- re-arm obligations through the intent's own lifecycle (review 2026-09-25) ---

def test_inapplicable_obligation_supersedes_its_intent_and_the_operator_can_continue(control, obligation, clock):
    from issue_agent.domain.workflow import (
        ContinueGrantedRearm, NoLinkedPr, RetiredUncertainCommand, StopReason, SupersededRearm,
    )
    from issue_agent.domain.workflow_policy import continue_resolve_rearm
    from tests.adapters.test_workflow_control import commit, enrich, observation

    command, gateway, pending = obligation
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    gateway.issue_revision = "revision:2"
    gateway.labels.add(("issue", 1, Label.RECURRENCE.value))
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 0
    assert _grant_row(control)["disposition"] == "inapplicable"
    stopped = _reconcile_rearm(control, clock)
    assert isinstance(stopped, WorkflowCommitted), stopped
    intent = stopped.snapshot.intent
    assert isinstance(intent, SupersededRearm) and intent.reason is StopReason.REQUIRED_EFFECT_REJECTED
    assert command.command_id in intent.retired_obligations
    retired = control.command_reconciliation(command.command_id)
    assert isinstance(retired, RetiredUncertainCommand)
    assert retired.uncertainty.history.unresolved_since == UNRESOLVED_SINCE
    # The retired obligation settles read-only; it is never written.
    clock[0] += timedelta(hours=1)
    executor(control, gateway).drain_pending_commands()
    assert gateway.writes() == []
    assert isinstance(control.command_reconciliation(command.command_id), RetiredSettledCommand)
    stopped = _current(control, command)
    resumed = commit(control, enrich(continue_resolve_rearm(
        stopped, ContinueGrantedRearm("continue", stopped.revision, pending.intent.request_id), ACTOR,
        observation(stopped, at=clock[0]), NoLinkedPr(stopped.revision.fence.key, clock[0]),
        now=clock[0], active_attempts=(),
    )))
    assert isinstance(resumed, WorkflowCommitted), resumed
    assert resumed.snapshot.intent.request_id != pending.intent.request_id


def test_cancel_after_grant_cancels_cleanly_and_voids_the_grant(control, obligation, clock):
    from issue_agent.domain.workflow import CancelRearm, CancelledRearm, RetiredUncertainCommand

    command, gateway, pending = obligation
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    current = _current(control, command)
    cancelled = adapter(control).cancel_rearm(
        CancelRearm("cancel-1", current.revision, pending.intent.request_id, "operator stopped it"), ACTOR,
    )
    assert isinstance(cancelled, WorkflowCommitted), cancelled
    assert isinstance(cancelled.snapshot.intent, CancelledRearm)
    row = _grant_row(control)
    assert row["disposition"] == "void" and "retired" in row["disposition_reason"]
    assert isinstance(control.command_reconciliation(command.command_id), RetiredUncertainCommand)
    clock[0] += timedelta(minutes=31)  # Its next scheduled read.
    executor(control, gateway).drain_pending_commands()
    assert gateway.writes() == []
    assert isinstance(control.command_reconciliation(command.command_id), RetiredSettledCommand)


def test_spent_obligation_grant_success_completes_the_rearm(control, obligation, clock):
    from issue_agent.domain.workflow import CompletedRearm

    command, gateway, pending = obligation
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 1
    workspace = adapter(control).prepared_workspace(_current(control, command).revision)
    completed = _reconcile_rearm(control, clock, frozenset({Label.AI_READY}), workspace)
    assert isinstance(completed, WorkflowCommitted), completed
    assert isinstance(completed.snapshot.intent, CompletedRearm)
    assert command.command_id in completed.snapshot.intent.verified_commands


def test_unknown_obligation_keeps_the_intent_reconcilable(control, obligation, clock):
    from issue_agent.domain.workflow import PendingRearm

    command, gateway, _ = obligation
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    gateway.fail_relabel = True
    clock[0] += timedelta(seconds=5)
    assert executor(control, gateway).drain_pending_commands() == 0
    waiting = _reconcile_rearm(control, clock)
    assert isinstance(waiting, WorkflowCommitted) and isinstance(waiting.snapshot.intent, PendingRearm)


def test_a_drain_overlapping_a_live_attempt_neither_reads_nor_rotates_the_gate(control, absent, clock):
    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    overlapping = CountingGateway()
    observed = {}

    class SlowWriter(CompletionGateway):
        def relabel(self, kind, number, remove, add):
            # The write outlives the post-consumption schedule; a second
            # drain of the same deployment runs meanwhile.
            clock[0] += timedelta(minutes=10)
            executor(control, overlapping).drain_pending_commands()
            observed["reads"] = overlapping.reads
            observed["cause"] = _gate(control, command).cause
            super().relabel(kind, number, remove, add)

    clock[0] += timedelta(seconds=5)
    assert executor(control, SlowWriter()).drain_pending_commands() == 1
    assert observed["reads"] == 0
    assert isinstance(observed["cause"], RemoteOutcomeUncertain)
    assert control.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED


def test_grant_acceptance_waits_for_an_in_flight_read(control, absent, clock):
    """The fix owner's shared effect authority excludes acceptance's barriers."""
    command, _ = absent
    grant = _grant(control, command)
    results = []
    reading = threading.Event()

    class InFlightRead(CompletionGateway):
        def has_label(self, kind, number, label):
            if not reading.is_set():
                reading.set()
                accept = threading.Thread(target=lambda: results.append(_authorize(control, grant)))
                accept.start()
                accept.join(timeout=1.0)
                results.append("blocked" if accept.is_alive() else "raced")
                self.accept = accept
            return super().has_label(kind, number, label)

    gateway = InFlightRead()
    clock[0] += timedelta(minutes=31)
    executor(control, gateway).drain_pending_commands()
    gateway.accept.join(timeout=30)
    assert results[0] == "blocked"
    # The read settled first, so the grant names an observation that no
    # longer backs the gate and acceptance refuses it.
    assert isinstance(results[1], (WorkflowConflict, WorkflowRejected))
    assert _grant_row(control) is None


def test_a_read_that_began_before_acceptance_never_judges_the_grant(control, absent, clock):
    command, _ = absent
    state = control.command_reconciliation(command.command_id)
    started = clock[0]
    clock[0] += timedelta(seconds=1)
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] += timedelta(seconds=1)
    control.record_reconciliation_read(command.command_id, state, expected_attempts=1, read_started_at=started,
                                       executor_identity=WRITER.owner_id, observed=None, error="unavailable")
    row = _grant_row(control)
    assert row["disposition"] is None and row["unavailable_reads"] == 0


@pytest.mark.parametrize("offset", [timedelta(seconds=-301), timedelta(seconds=1)])
def test_reads_stamped_stale_or_in_the_future_are_refused(control, absent, clock, offset):
    command, _ = absent
    state = control.command_reconciliation(command.command_id)
    with pytest.raises(ValueError):
        control.record_reconciliation_read(command.command_id, state, expected_attempts=1,
                                           read_started_at=clock[0] + offset,
                                           executor_identity=WRITER.owner_id, observed=False)


def test_spend_and_read_evidence_are_stamped_at_read_start(control, absent, clock):
    from issue_agent.adapters.workflow_codec import decode_workflow
    from issue_agent.domain.workflow import CommandAbsenceEvidence

    command, _ = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    clock[0] += timedelta(seconds=5)
    started = clock[0]

    class SlowRead(CompletionGateway):
        def get_issue(self, issue):
            clock[0] += timedelta(seconds=20)
            return super().get_issue(issue)

    assert executor(control, SlowRead()).drain_pending_commands() == 1
    with closing(control._connect()) as db:
        [payload] = db.execute("SELECT payload_json FROM workflow_evidence WHERE evidence_id LIKE 'retry-absence:%'").fetchone()
    assert decode_workflow(payload, CommandAbsenceEvidence).observed_at == started


def test_authorized_attempts_per_command_are_capped(control, absent, clock, tmp_path):
    from issue_agent.domain.workflow_retry import MAX_AUTHORIZED_ATTEMPTS

    command, gateway = absent
    grants = _journal(control, tmp_path)
    gateway.fail_relabel = True
    for index in range(MAX_AUTHORIZED_ATTEMPTS):
        assert isinstance(_authorize(control, _grant(control, command, f"grant-{index}")), WorkflowCommitted)
        clock[0] += timedelta(seconds=5)
        executor(control, gateway).drain_pending_commands()
        clock[0] += timedelta(hours=1)
        executor(control, gateway).drain_pending_commands()  # a fresh absence after the unknown attempt
        assert isinstance(_gate(control, command).cause, RetryAuthorizationRequired)
    assert len(gateway.writes()) == MAX_AUTHORIZED_ATTEMPTS
    assert not _offered(control, command)
    rejected = _authorize(control, _grant(control, command, "one-too-many"))
    assert isinstance(rejected, WorkflowRejected) and "authorized attempts" in rejected.reason
    assert [name for name, _ in grants()].count("ceiling_reached") == 1


def test_execution_authority_refusal_is_never_offered_or_accepted(control, absent, monkeypatch):
    command, _ = absent
    assert _offered(control, command)

    def refuse(*_args, **_kwargs):
        raise ControlStoreError("publication command lacks its captured run authority")

    monkeypatch.setattr(SQLiteControlStore, "_validate_workflow_command", refuse)
    assert not _offered(control, command)
    rejected = _authorize(control, _grant(control, command))
    assert isinstance(rejected, WorkflowRejected) and "captured run authority" in rejected.reason
    assert _grant_row(control) is None


def test_cancel_uses_one_durable_uncertainty_start_despite_executor_clock_skew(control, clock):
    """The unknown receipt's executor timestamps never compete with the store's."""
    from issue_agent.domain.workflow import CancelRearm, CancelledRearm, RetiredUncertainCommand
    from tests.adapters.test_workflow_control import commit, held, install_workflow, prepare_workspace, request

    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    prepare_workspace(control, pending)
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    skewed = clock[0] - timedelta(seconds=17)
    control.record_effect(EffectReceipt(command_id, WRITER.owner_id, skewed, skewed,
                                        EffectStatus.UNKNOWN_OUTCOME, error="response lost"))
    current = adapter(control).load_workflow(pending.revision.fence.key)
    cancelled = adapter(control).cancel_rearm(
        CancelRearm("cancel-skew", current.revision, pending.intent.request_id, "stop"), ACTOR,
    )
    assert isinstance(cancelled, WorkflowCommitted), cancelled
    assert isinstance(cancelled.snapshot.intent, CancelledRearm)
    retired = control.command_reconciliation(command_id)
    assert isinstance(retired, RetiredUncertainCommand)
    assert retired.uncertainty.history.unresolved_since == clock[0]


def test_human_comment_landing_after_the_spend_decision_still_refuses_the_authorized_write(control, absent, clock):
    """The authorized write re-validates the same full guard, conversation included."""
    from issue_agent.domain.world import Comment

    command, gateway = absent
    assert isinstance(_authorize(control, _grant(control, command)), WorkflowCommitted)
    reads = [0]
    original = gateway.get_issue

    def racing_answer(issue):
        reads[0] += 1
        if reads[0] > 1:  # after the spend decision's own read
            gateway.conversation = (Comment("alice", "Use the v2 endpoint.", "revision:2"),)
        return original(issue)

    gateway.get_issue = racing_answer
    clock[0] += timedelta(seconds=5)
    executor(control, gateway).drain_pending_commands()
    assert gateway.writes() == []
    receipt = control.effect_receipt(command.command_id)
    assert receipt is None or receipt.status is not EffectStatus.SUCCEEDED
