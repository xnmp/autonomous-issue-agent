"""Review-family commands under the review daemon's effect executor (ADR 0015, increment 4).

Real SQLite authority plus the persistent GitHub double. Review obligations are
minted by the pure review policy and committed the way the review control will
commit them; the executor drains them through its ordinary recovery path.
"""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from issue_agent.adapters.control_store import ControlStoreError, SQLiteControlStore, load_retry_authorization_facts
from issue_agent.adapters.effects import GitHubEffectExecutor, action_from_command
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_commit import insert_pending_rearm
from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot, workflow_scope
from issue_agent.domain.actions import Relabel, Target
from issue_agent.domain.contracts import CommandId, CommandKind, EffectStatus, LifecycleCommand
from issue_agent.domain.labels import Label
from issue_agent.domain.review_workflow import (
    CompleteReviews, PendingReviewRearm, ReviewWorkflowSnapshot, body_review_command, mark_review_body,
    review_command_marker,
)
from issue_agent.domain.review_workflow_policy import (
    CeilingEscalation, ReviewSubmissionReceipt, reconcile_review_ceiling, request_review_rearm,
)
from issue_agent.adapters.github import GhUnavailable, GitHubGateway
from issue_agent.domain.workflow import (
    EvidenceUnavailable, NoAuthorizedRemediation, OpenGate, ReconciliationOnly, RemoteOutcomeUncertain,
    RetryAuthorizationRequired, WorkflowChanged,
)
from issue_agent.domain.workflow_reconciliation import prewrite_evidence_gate_id
from issue_agent.domain.world import IssueView, PRView
from tests.domain.test_review_workflow_policy import BOT, TRIGGER, dismissed, gated, observe, rejections, request_for
from tests.fakes.persistent_github import PersistentGitHub
from tests.fakes.review_workflow import ACTOR, HEAD, NOW, OTHER_HEAD, PATCH, PR_KEY
from tests.fakes.workflow_authority import install_workflow

REVIEWER = "o/r:review:test"
FIX = "o/r:fix:test"
PR = 9
T0 = "2026-09-25T11:00:00+00:00"


class Rig:
    def __init__(self, tmp_path, *, fault=lambda _: None):
        self.clock = [NOW]
        self.store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: self.clock[0])
        self.store.initialize()
        self.remote_path = tmp_path / "remote.sqlite"
        self.remote = PersistentGitHub(self.remote_path, fault=fault, clock=lambda: self.clock[0])
        self.remote.seed_pr(PRView(PR, head_ref_oid=HEAD, labels=frozenset({Label.NEEDS_HUMAN_REVIEW})))
        for index in range(2):
            self.remote.seed_review(PR, f"r{index}", author=BOT, commit=HEAD,
                                    submitted_at=f"2026-09-25T11:00:0{index}+00:00")
        self.logs: list[str] = []

    def executor(self, identity=REVIEWER, remote=None):
        return GitHubEffectExecutor(remote or self.remote, self.store, repository="o/r", identity=identity,
                                    log=self.logs.append)

    def drain(self, **kwargs):
        return self.executor(**kwargs).drain_pending_commands()

    def advance(self, seconds):
        self.clock[0] += timedelta(seconds=seconds)

    def state(self, command_id):
        with closing(self.store._connect()) as db:
            return db.execute("SELECT state FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()[0]

    def receipt(self, command_id):
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT status, error FROM effect_receipts WHERE command_id=?", (str(command_id),)).fetchone()
        return None if row is None else (row[0], row[1])

    def receipt_object(self, command_id):
        with closing(self.store._connect()) as db:
            return db.execute("SELECT remote_object_id FROM effect_receipts WHERE command_id=?",
                              (str(command_id),)).fetchone()[0]

    def submission_id(self):
        with closing(self.store._connect()) as db:
            return CommandId(db.execute("SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'").fetchone()[0])

    def gate(self, gate_id):
        return next((g for g in self.workflow().gates if getattr(g, "gate_id", None) == gate_id), None)

    def workflow(self) -> ReviewWorkflowSnapshot:
        with closing(self.store._connect()) as db:
            db.execute("BEGIN")
            snapshot = load_review_workflow_row(db, PR_KEY)
        assert snapshot is not None
        return snapshot

    def calls(self, kind):
        return [call for call in self.remote.calls if call[0] == kind]


def accepted_rearm(executor_identity=REVIEWER) -> WorkflowChanged:
    state = gated()
    change = request_review_rearm(
        state, request_for(state), ACTOR, observe(rejections(2)), now=NOW, active_reviews=(),
        evidence_id="complete-reviews:1", trigger_label=TRIGGER, executor_identity=executor_identity,
    )
    assert isinstance(change, WorkflowChanged), change
    return change


def commit_rearm(store, change: WorkflowChanged, *, extra_gates=()) -> ReviewWorkflowSnapshot:
    """Persist an accepted review re-arm as the review control commits it (increment 5)."""
    after = change.snapshot
    assert isinstance(after, ReviewWorkflowSnapshot) and isinstance(after.intent, PendingReviewRearm)
    after = replace(after, gates=after.gates + tuple(extra_gates))
    install_workflow(store, replace(after, gates=(), intent=None))
    with store.transaction() as db:
        persist_workflow_snapshot(db, after)
        insert_pending_rearm(db, workflow_scope(PR_KEY), after.intent, change.audit)
        for command in change.commands:
            store._enqueue_command(db, command)
            operation = (f"dismiss:{command.payload['review_id']}" if command.kind is CommandKind.DISMISS_REVIEW
                         else "review_projection")
            db.execute("INSERT INTO workflow_rearm_commands VALUES (?, ?, ?, 'required', ?)",
                       (after.intent.request_id, operation, str(command.command_id), encode_workflow(change.audit)))
    return after


def by_kind(change, kind):
    return [command for command in change.commands if command.kind is kind]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def rearm(rig):
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    return change


# -- the accepted re-arm is runnable by the review executor ------------------------

def test_review_executor_dismisses_the_authorized_reviews_and_projects_the_trigger(rig, rearm):
    # Before increment 4 an accepted review obligation was never selectable
    # (the drain read review rows as resolve authority).
    selected = rig.store.runnable_commands(repository="o/r", executor_identity=REVIEWER, diagnose=rig.logs.append)
    assert {p.command.command_id for p in selected} == {c.command_id for c in rearm.commands}
    assert rig.drain() == 3
    assert rig.remote.review_state("r0") == rig.remote.review_state("r1") == "DISMISSED"
    assert rig.remote.get_pr(PR).labels == frozenset({TRIGGER})
    assert [call[:3] for call in rig.calls("dismiss_review")] == [["dismiss_review", PR, "r0"], ["dismiss_review", PR, "r1"]]
    assert all(rig.state(c.command_id) == "succeeded" for c in rearm.commands)
    assert rig.logs == []


def test_a_second_drain_writes_nothing_more(rig, rearm):
    rig.drain()
    before = rig.remote.calls
    assert rig.drain() == 0
    assert rig.remote.calls == before


def test_already_dismissed_review_settles_success_without_a_write(rig, rearm):
    rig.remote.external_review_change("r0", state="DISMISSED")
    rig.drain()
    first = by_kind(rearm, CommandKind.DISMISS_REVIEW)[0]
    assert rig.receipt(first.command_id) == ("succeeded", None)
    assert [call[2] for call in rig.calls("dismiss_review")] == ["r1"]


def test_vanished_review_is_a_verified_refusal_never_success(rig, rearm):
    """Regression: legacy `operator_effects.dismiss_review` counted an absent review as dismissed."""
    rig.remote.external_review_change("r0", delete=True)
    rig.drain()
    first = by_kind(rearm, CommandKind.DISMISS_REVIEW)[0]
    assert rig.receipt(first.command_id) == ("permanent_failure", "guard changed: review no longer exists")
    assert "r0" not in [call[2] for call in rig.calls("dismiss_review")]
    assert any("review no longer exists" in line for line in rig.logs)


def test_a_review_vanishing_during_an_unknown_outcome_is_never_reconciled_as_success(tmp_path):
    def lose(point):
        if point == "dismiss_requested":
            raise TimeoutError("request lost")
    rig = Rig(tmp_path, fault=lose)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    first = by_kind(change, CommandKind.DISMISS_REVIEW)[0]
    rig.drain()
    assert rig.state(first.command_id) == "unknown_outcome"
    rig.remote.fault = lambda _: None
    rig.remote.external_review_change("r0", delete=True)
    rig.advance(60)
    rig.drain()
    assert rig.receipt(first.command_id) == ("permanent_failure", "guard changed: review no longer exists")


@pytest.mark.parametrize("change, reason", [
    (lambda remote: remote.external_review_change("r0", state="APPROVED"), "review state is APPROVED"),
    (lambda remote: remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD), "head moved"),
    (lambda remote: remote.external_pr_change(PR, state="MERGED"), "pull request is merged"),
])
def test_a_changed_dismissal_guard_is_refused_without_a_write(rig, rearm, change, reason):
    change(rig.remote)
    rig.drain()
    first = by_kind(rearm, CommandKind.DISMISS_REVIEW)[0]
    status, error = rig.receipt(first.command_id)
    assert status == "permanent_failure" and reason in error
    assert "r0" not in [call[2] for call in rig.calls("dismiss_review")]


def test_a_review_by_another_author_is_never_dismissed(tmp_path):
    rig = Rig(tmp_path)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with closing(rig.remote._connect()) as db, db:
        db.execute("UPDATE reviews SET author='alice' WHERE review_id='r1'")
    rig.drain()
    second = by_kind(change, CommandKind.DISMISS_REVIEW)[1]
    assert rig.receipt(second.command_id) == ("permanent_failure", "guard changed: review author is no longer the review bot")
    assert rig.remote.review_state("r1") == "CHANGES_REQUESTED"


def test_a_partial_review_read_never_succeeds_and_writes_nothing(tmp_path):
    def page_fails(point):
        if point == "reviews_page:0":
            raise ConnectionError("page 1 reset")
    rig = Rig(tmp_path, fault=page_fails)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    rig.drain()
    for command in by_kind(change, CommandKind.DISMISS_REVIEW):
        assert rig.state(command.command_id) == "pending"
        assert rig.receipt(command.command_id) is None
        assert rig.store.prewrite_read(command.command_id).failed_reads == 1
    assert rig.calls("dismiss_review") == []
    assert any("pre-write observation unavailable (partial review read" in line for line in rig.logs)


@pytest.mark.parametrize("sql_hold", [False, True])
def test_linked_issue_hold_arriving_after_acceptance_preserves_hold_and_refuses_trigger(rig, rearm, sql_hold):
    rig.remote.external_pr_change(PR, closing_issue=7)
    if sql_hold:
        from tests.adapters.test_review_linkage import held_issue
        install_workflow(rig.store, held_issue())
        rig.remote.seed(IssueView(7))
    else:
        rig.remote.seed(IssueView(7, labels=frozenset({Label.AI_BLOCKED})))
    rig.drain()
    # The selected review dismissals remain authorized; releasing the PR does not.
    assert rig.remote.review_state("r0") == rig.remote.review_state("r1") == "DISMISSED"
    projection = by_kind(rearm, CommandKind.RELABEL)[0]
    assert rig.receipt(projection.command_id)[0] == "permanent_failure"
    assert "linked issue #7" in rig.receipt(projection.command_id)[1]
    assert rig.remote.get_pr(PR).labels == frozenset({Label.NEEDS_HUMAN_REVIEW})
    assert all(call[0] == "dismiss_review" for call in rig.remote.calls)


@pytest.mark.parametrize("operation", ["submission", "projection"])
def test_linked_issue_guard_and_review_write_exclude_concurrent_operator_barrier(rig, monkeypatch, operation):
    from issue_agent.adapters.deployment_authority import _deployment_effect_lock
    from tests.domain.test_review_workflow_policy import fresh

    state = fresh()
    if operation == "submission":
        install_workflow(rig.store, state)
    else:
        commit_rearm(rig.store, accepted_rearm())
    rig.remote.external_pr_change(PR, closing_issue=7)
    rig.remote.seed(IssueView(7))
    blocked = set()

    def operator_cannot_enter(phase):
        with pytest.raises(BlockingIOError):
            with _deployment_effect_lock(rig.store.path, "o/r", "review", exclusive=True, blocking=False):
                pass
        blocked.add(phase)

    issue_read = rig.remote.get_issue

    def guarded_issue(number):
        operator_cannot_enter("issue hold read")
        return issue_read(number)

    monkeypatch.setattr(rig.remote, "get_issue", guarded_issue)
    if operation == "submission":
        submit = rig.remote.submit_review

        def guarded_submission(*args, **kwargs):
            operator_cannot_enter("submission write")
            return submit(*args, **kwargs)

        monkeypatch.setattr(rig.remote, "submit_review", guarded_submission)
        assert rig.executor().submit_bound_review(PR, "REQUEST_CHANGES", "approved", HEAD,
                                                  workflow=state.binding, reviewer=BOT, patch_id=PATCH)
        assert rig.remote.review_state("submitted-2") == "CHANGES_REQUESTED"
    else:
        relabel = rig.remote.relabel

        def guarded_projection(*args, **kwargs):
            operator_cannot_enter("projection write")
            return relabel(*args, **kwargs)

        monkeypatch.setattr(rig.remote, "relabel", guarded_projection)
        rig.drain()
        assert TRIGGER in rig.remote.get_pr(PR).labels
    assert {"issue hold read", f"{operation} write"} <= blocked
    # The operator's exclusive barrier becomes available once the operation has ended.
    with _deployment_effect_lock(rig.store.path, "o/r", "review", exclusive=True, blocking=False):
        pass


def test_unreadable_linked_issue_defers_dismissals_and_projection_with_bounded_evidence(rig, rearm):
    rig.remote.external_pr_change(PR, closing_issue=7)
    for _ in range(200):
        rig.drain()
        rig.advance(60)
    for command in rearm.commands:
        assert rig.state(command.command_id) == "pending"
        schedule = rig.store.prewrite_read(command.command_id)
        assert 5 <= schedule.failed_reads <= 13
        gate = rig.gate(prewrite_evidence_gate_id(command.command_id))
        assert isinstance(gate.cause, EvidenceUnavailable)
        assert "linked issue unavailable" in gate.cause.last_error
    assert rig.remote.calls == []
    rig.remote.seed(IssueView(7))
    rig.advance(1800)
    rig.drain()
    assert rig.remote.review_state("r0") == rig.remote.review_state("r1") == "DISMISSED"
    assert TRIGGER in rig.remote.get_pr(PR).labels


def test_a_permanently_unreadable_pull_request_backs_off_to_a_visible_evidence_gate(tmp_path):
    """ADR 0012: an unavailable pre-write read is bounded, journaled and surfaces a gate; it never retries per tick."""
    reads = []
    failing = [True]

    def fault(point):
        if point == "reviews_page:0":
            reads.append(point)
            if failing[0]:
                raise ConnectionError("page 1 reset")
    rig = Rig(tmp_path, fault=fault)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    first = by_kind(change, CommandKind.DISMISS_REVIEW)[0]
    gate_id = prewrite_evidence_gate_id(first.command_id)
    for _ in range(200):  # one drain per minute for over three hours
        rig.drain()
        rig.advance(60)
    per_command = len(reads) // 2
    assert 5 <= per_command <= 13  # exponential backoff, not one read per drain
    gate = rig.gate(gate_id)
    assert isinstance(gate.cause, EvidenceUnavailable) and "partial review read" in gate.cause.last_error
    assert gate.cause.failed_reads == rig.store.prewrite_read(first.command_id).failed_reads
    assert rig.calls("dismiss_review") == [] and rig.state(first.command_id) == "pending"
    # A complete read ends the schedule: the command writes and its gate closes.
    failing[0] = False
    rig.advance(1800)
    rig.drain()
    assert rig.receipt(first.command_id) == ("succeeded", None)
    assert rig.gate(gate_id) is None and rig.store.prewrite_read(first.command_id) is None


def test_an_unknown_dismissal_under_partial_reads_backs_off_to_a_visible_uncertainty_gate(tmp_path):
    faults = {"dismiss_applied"}

    def fault(point):
        if point in faults:
            raise TimeoutError(point)
    rig = Rig(tmp_path, fault=fault)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    first = by_kind(change, CommandKind.DISMISS_REVIEW)[0]
    rig.drain()
    assert rig.state(first.command_id) == "unknown_outcome"
    faults.clear()
    faults.update({"reviews_page:0"})
    writes = len(rig.calls("dismiss_review"))
    assert rig.drain() == 0  # not yet due: no read, no write
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(first.command_id), ReconciliationOnly):
            break
    state = rig.store.command_reconciliation(first.command_id)
    assert isinstance(state, ReconciliationOnly)
    gate = next(g for g in rig.workflow().gates if getattr(g, "gate_id", None) == state.gate_id)
    assert isinstance(gate.cause, RemoteOutcomeUncertain) and gate.cause.command_id == first.command_id
    assert len(rig.calls("dismiss_review")) == writes  # unavailability never bought a write
    faults.clear()
    rig.advance(1800)
    rig.drain()
    # The lost reply's dismissal is observed: settled read-only, gate closed.
    assert rig.receipt(first.command_id)[0] == "succeeded"
    assert all(getattr(g, "gate_id", None) != state.gate_id for g in rig.workflow().gates)
    assert len(rig.calls("dismiss_review")) == writes


def test_a_lost_dismissal_request_is_rewritten_once_after_proven_absence(tmp_path):
    faults = {"dismiss_requested"}

    def fault(point):
        if point in faults:
            faults.discard(point)
            raise TimeoutError(point)
    rig = Rig(tmp_path, fault=fault)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    first = by_kind(change, CommandKind.DISMISS_REVIEW)[0]
    rig.drain()
    assert rig.state(first.command_id) == "unknown_outcome"
    assert rig.remote.review_state("r0") == "CHANGES_REQUESTED"
    rig.advance(60)
    rig.drain()
    assert rig.receipt(first.command_id) == ("succeeded", None)
    assert rig.remote.review_state("r0") == "DISMISSED"


def test_permission_refusal_is_visible_and_permanent_without_a_credential_switch(tmp_path):
    rig = Rig(tmp_path)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    rig.remote.deny_dismissals()
    rig.drain()
    for command in by_kind(change, CommandKind.DISMISS_REVIEW):
        status, error = rig.receipt(command.command_id)
        assert status == "permanent_failure" and "credentials are never switched" in error
    assert len(rig.calls("dismiss_review")) == 2  # one attempt each, as this gateway's identity
    rig.advance(86400)
    rig.drain()
    assert len(rig.calls("dismiss_review")) == 2
    assert rig.remote.review_state("r0") == "CHANGES_REQUESTED"


# -- ownership ---------------------------------------------------------------------

def test_the_fix_executor_never_owns_or_runs_a_dismissal(rig):
    fix = rig.executor(identity=FIX)
    review = rig.executor()
    assert CommandKind.DISMISS_REVIEW not in fix.owned_kinds
    assert CommandKind.DISMISS_REVIEW in review.owned_kinds
    rig.store.enqueue_command(LifecycleCommand(
        CommandId("fix-dismissal"), "idem:fix-dismissal", CommandKind.DISMISS_REVIEW, f"pr:{PR}", HEAD,
        {"pr": PR, "review_id": "r0", "message": "m"}, repository="o/r", executor_identity=FIX,
    ))
    assert fix.drain_pending_commands() == 0
    assert rig.state("fix-dismissal") == "pending"
    assert rig.calls("dismiss_review") == []
    assert any("dismiss_review is not owned" in line for line in rig.logs)


def test_an_unbound_dismissal_is_left_queued_even_for_the_review_executor(rig):
    rig.store.enqueue_command(LifecycleCommand(
        CommandId("legacy-dismissal"), "idem:legacy", CommandKind.DISMISS_REVIEW, f"pr:{PR}", HEAD,
        {"pr": PR, "review_id": "r0", "message": "m"}, repository="o/r", executor_identity=REVIEWER,
    ))
    assert rig.drain() == 0
    assert rig.state("legacy-dismissal") == "pending" and rig.calls("dismiss_review") == []
    assert any("dismiss_review is not owned" in line for line in rig.logs)


# -- guarded PR projection ------------------------------------------------------------

def projection(change):
    return by_kind(change, CommandKind.RELABEL)[0]


def test_a_review_relabel_whose_hold_guard_changed_is_refused_not_written(rig, rearm):
    rig.remote.external_pr_change(PR, labels=sorted({Label.NEEDS_HUMAN_REVIEW.value, Label.AI_BLOCKED.value}))
    rig.drain()
    status, error = rig.receipt(projection(rearm).command_id)
    assert status == "permanent_failure" and "hold labels changed" in error and "ai-blocked" in error
    assert rig.calls("relabel_pr") == []


def test_a_review_relabel_is_refused_when_the_head_moved(rig, rearm):
    rig.remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD)
    rig.drain()
    status, error = rig.receipt(projection(rearm).command_id)
    assert status == "permanent_failure" and "head moved" in error
    assert rig.calls("relabel_pr") == []


def test_a_review_relabel_repairs_its_own_partial_effect(rig, rearm):
    rig.remote.external_pr_change(PR, labels=[])  # its own removal already landed
    rig.drain()
    assert rig.receipt(projection(rearm).command_id) == ("succeeded", None)
    assert rig.remote.get_pr(PR).labels == frozenset({TRIGGER})


def test_ordering_labels_are_not_hold_facts(rig, rearm):
    rig.remote.external_pr_change(PR, labels=sorted({Label.NEEDS_HUMAN_REVIEW.value, Label.P1.value}))
    rig.drain()
    assert rig.receipt(projection(rearm).command_id) == ("succeeded", None)


def test_review_relabels_never_reconstruct_as_unguarded_actions(rig, rearm):
    command = projection(rearm)
    with pytest.raises(ValueError, match="guarded review path"):
        action_from_command(command)
    unbound = replace(command, workflow=None)
    with pytest.raises(ValueError, match="guarded review path"):
        action_from_command(unbound)
    # The legacy decision path never matches (and so never writes) the review obligation.
    rig.remote.external_pr_change(PR, labels=sorted({Label.NEEDS_HUMAN_REVIEW.value, Label.AI_BLOCKED.value}))
    executor = rig.executor()
    assert executor._pending_for_action(Relabel(Target.PR, PR, Label.NEEDS_HUMAN_REVIEW, TRIGGER, HEAD)) is None


def test_a_review_relabel_missing_its_guard_is_never_executed(rig, rearm):
    command = projection(rearm)
    payload = {key: value for key, value in command.payload.items() if key != "pull_request_hold_guard"}
    with pytest.raises(ControlStoreError, match="malformed"):
        rig.store.enqueue_command(replace(command, command_id=CommandId("forged"), idempotency_key="idem:forged",
                                          payload=payload))
    # A stored row that lost its guard (corruption) stays queued and visible.
    with rig.store.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET payload_json=? WHERE command_id=?",
                   (json.dumps(payload), str(command.command_id)))
    rig.drain()
    assert rig.calls("relabel_pr") == []
    assert rig.state(command.command_id) == "pending"
    assert any(str(command.command_id) in line for line in rig.logs)


# -- held replacements ----------------------------------------------------------------------

def test_review_obligations_wait_while_a_review_uncertainty_gate_is_open(tmp_path):
    rig = Rig(tmp_path)
    change = accepted_rearm()
    stranded = OpenGate("gate:uncertain-relabel", 0,
                        RemoteOutcomeUncertain(CommandId("stopped-relabel"), NOW, 3, "timed out"),
                        NoAuthorizedRemediation("reconcile only"))
    commit_rearm(rig.store, change, extra_gates=(stranded,))
    assert rig.store.runnable_commands(repository="o/r", executor_identity=REVIEWER, diagnose=rig.logs.append) == ()
    assert rig.drain() == 0
    assert rig.remote.calls == []
    with pytest.raises(ControlStoreError, match="awaits"):
        rig.store.begin_command(change.commands[0].command_id)


def test_a_superseded_generation_cannot_begin_a_review_command(rig):
    change = committed_release(rig)
    with rig.store.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1, version=version+1 WHERE target_kind='pr'")
    rig.drain()
    assert rig.calls("relabel_pr") == []
    assert rig.state(change.commands[0].command_id) == "pending"
    assert any("stale" in line for line in rig.logs)


# -- retry authorization stays resolve-only -------------------------------------------------

def _absent_after_expiry(rig, command):
    """Drive one dismissal to reconciliation-only, then prove its absence."""
    rig.store.begin_command(command.command_id)
    from issue_agent.domain.contracts import EffectReceipt
    rig.store.record_effect(EffectReceipt(command.command_id, REVIEWER, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME,
                                          error="response lost"))
    rig.remote.fault = lambda point: (_ for _ in ()).throw(ConnectionError(point)) if point == "reviews_page:0" else None
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(command.command_id), ReconciliationOnly):
            break
    rig.remote.fault = lambda _: None
    rig.advance(1800)
    rig.drain()
    state = rig.store.command_reconciliation(command.command_id)
    assert isinstance(state, ReconciliationOnly)
    gate = next(g for g in rig.workflow().gates if getattr(g, "gate_id", None) == state.gate_id)
    assert isinstance(gate.cause, RetryAuthorizationRequired)
    return state, gate


def grant(rig, command, gate, authorization_id="grant-1"):
    """Accept one operator grant exactly as the review control persists it."""
    from issue_agent.adapters.workflow_commit import persist_retry_authorization
    from issue_agent.domain.workflow import AuthorizeAbsentRetry
    from issue_agent.domain.workflow_retry import authorize_absent_retry
    state = rig.workflow()
    with rig.store.transaction() as db:
        facts = load_retry_authorization_facts(rig.store, db, command.command_id, gate.cause.absence_evidence_id)
        request = AuthorizeAbsentRetry(authorization_id, state.revision, command.command_id, gate.gate_id,
                                       gate.cause.absence_evidence_id)
        change = authorize_absent_retry(state, request, ACTOR, facts)
        assert isinstance(change, WorkflowChanged), change
        persist_workflow_snapshot(db, change.snapshot)
        persist_retry_authorization(db, rig.store, change, facts)
    return facts


def grant_row(rig):
    with closing(rig.store._connect()) as db:
        return db.execute("SELECT disposition, disposition_reason FROM workflow_retry_authorizations").fetchone()


def test_a_review_family_grant_buys_exactly_one_guarded_dismissal(rig, rearm):
    command = by_kind(rearm, CommandKind.DISMISS_REVIEW)[0]
    _, gate = _absent_after_expiry(rig, command)
    facts = grant(rig, command, gate)
    assert facts.execution_refusal is None and facts.evidence is not None and facts.workspace_ready
    assert "r0" not in [call[2] for call in rig.calls("dismiss_review")]
    rig.drain()  # a fresh grant is due at once: guard re-checked, one write, verified
    assert [call[2] for call in rig.calls("dismiss_review")].count("r0") == 1
    assert rig.remote.review_state("r0") == "DISMISSED"
    assert rig.receipt(command.command_id)[0] == "succeeded"
    assert rig.gate(gate.gate_id) is None and grant_row(rig)[0] == "consumed"
    rig.advance(3600)
    rig.drain()
    assert len([c for c in rig.calls("dismiss_review") if c[2] == "r0"]) == 1


def authorized_submission_retry(rig):
    from issue_agent.adapters.effects import bound_review_command_id
    from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
    from tests.domain.test_review_workflow_policy import fresh

    state = fresh()
    install_workflow(rig.store, state)
    op = BoundReviewSubmissionOp(PR, "REQUEST_CHANGES", "approved", HEAD, BOT, PATCH, None)
    command_id = bound_review_command_id(op, state.binding)
    command = LifecycleCommand(command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
                               f"pr:{PR}", HEAD, op.payload(), repository="o/r", executor_identity=REVIEWER,
                               workflow=state.binding)
    rig.store.enqueue_command(command)
    _, gate = _absent_after_expiry(rig, command)
    grant(rig, command, gate)
    return command


@pytest.mark.parametrize("unavailable", [False, True])
def test_authorized_submission_retry_still_requires_current_linked_issue_evidence(rig, unavailable):
    authorized_submission_retry(rig)
    rig.remote.external_pr_change(PR, closing_issue=7)
    if not unavailable:
        rig.remote.seed(IssueView(7, labels=frozenset({Label.AI_BLOCKED})))
    rig.drain()
    assert rig.calls("submit_review") == []
    if unavailable:
        assert grant_row(rig)[0] is None
        rig.remote.seed(IssueView(7))
        rig.advance(1800)
        rig.drain()
        assert len(rig.calls("submit_review")) == 1
        assert grant_row(rig)[0] == "consumed"
    else:
        assert grant_row(rig)[0] == "inapplicable"


def test_local_authority_corruption_during_submission_retry_does_not_become_remote_uncertainty(rig):
    from tests.adapters.test_review_linkage import held_issue

    command = authorized_submission_retry(rig)
    rig.remote.external_pr_change(PR, closing_issue=7)
    rig.remote.seed(IssueView(7))
    install_workflow(rig.store, held_issue())
    with rig.store.transaction() as db:
        db.execute("DELETE FROM workflow_authority_epochs WHERE family='resolve'")
    before = rig.store.command_reconciliation(command.command_id)
    rig.drain()
    assert rig.calls("submit_review") == []
    assert grant_row(rig)[0] is None
    assert rig.store.command_reconciliation(command.command_id) == before
    assert any("retained" in line for line in rig.logs)


def test_a_review_family_grant_whose_guard_changed_ends_inapplicable_without_a_write(rig, rearm):
    command = by_kind(rearm, CommandKind.DISMISS_REVIEW)[0]
    _, gate = _absent_after_expiry(rig, command)
    grant(rig, command, gate)
    rig.remote.external_review_change("r0", state="APPROVED")  # the dismissal guard no longer applies
    rig.drain()
    assert "r0" not in [call[2] for call in rig.calls("dismiss_review")]
    disposition, reason = grant_row(rig)
    assert disposition == "inapplicable" and "guard no longer holds" in reason


# -- bound review submission ---------------------------------------------------------------------

def test_a_bound_submission_carries_its_binding_and_mints_receipt_evidence(rig, rearm):
    binding = rearm.snapshot.binding
    executor = rig.executor()
    assert executor.submit_bound_review(PR, "REQUEST_CHANGES", "fix it", HEAD, workflow=binding, reviewer=BOT, patch_id=PATCH)
    with closing(rig.store._connect()) as db:
        row = db.execute("SELECT command_id, workflow_binding_json, state FROM lifecycle_commands "
                         "WHERE kind='submit_review'").fetchone()
    assert decode_workflow(row[1], type(binding)) == binding and row[2] == "succeeded"
    evidence = rig.store.bound_review_submission(CommandId(row[0]))
    assert isinstance(evidence, ReviewSubmissionReceipt)
    assert (evidence.head_sha, evidence.patch_id, evidence.submission.binding) == (HEAD, PATCH, binding)


def test_a_ceiling_escalation_submission_is_identifiable(rig, rearm):
    binding = rearm.snapshot.binding
    counted_past = ("a" * 40,)
    rig.executor().submit_bound_review(PR, "COMMENT", "escalating", HEAD, workflow=binding, reviewer=BOT,
                                       patch_id=PATCH, escalate_cause="ceiling", non_resetting_commits=counted_past)
    with closing(rig.store._connect()) as db:
        command_id = db.execute("SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'").fetchone()[0]
    evidence = rig.store.bound_review_submission(CommandId(command_id))
    assert isinstance(evidence, CeilingEscalation) and evidence.escalate_cause == "ceiling"
    # Settlement judges the gate under exactly the rule the verdict counted with.
    assert evidence.rule.review_bot == BOT and evidence.rule.non_resetting_commits == counted_past


def test_a_verdict_escalation_settles_as_an_escalated_baseline_receipt(rig, rearm):
    from issue_agent.domain.review_workflow_policy import ReviewSubmissionReceipt
    rig.executor().submit_bound_review(PR, "COMMENT", "escalating", HEAD, workflow=rearm.snapshot.binding,
                                       reviewer=BOT, patch_id=PATCH, escalate_cause="verdict")
    with closing(rig.store._connect()) as db:
        command_id = db.execute("SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'").fetchone()[0]
    evidence = rig.store.bound_review_submission(CommandId(command_id))
    assert isinstance(evidence, ReviewSubmissionReceipt) and evidence.escalated and evidence.patch_id == PATCH


def test_an_unbound_submission_is_unchanged_and_mints_no_evidence_while_inactive(rig):
    assert rig.executor().submit_review(PR, "APPROVE", "lgtm", HEAD)
    with closing(rig.store._connect()) as db:
        row = db.execute("SELECT command_id, workflow_binding_json, payload_json FROM lifecycle_commands "
                         "WHERE kind='submit_review'").fetchone()
    assert row[1] is None and json.loads(row[2]) == {"pr": PR, "event": "APPROVE", "body": "lgtm", "head": HEAD}
    assert rig.store.bound_review_submission(CommandId(row[0])) is None


def test_an_active_review_family_refuses_every_unbound_submission_before_any_write(rig, rearm):
    """One submission path once active: an unbound verdict could double-post beside a bound one."""
    with pytest.raises(ControlStoreError, match="captured workflow authority"):
        rig.executor().submit_review(PR, "APPROVE", "lgtm", HEAD)
    assert rig.calls("submit_review") == []
    with closing(rig.store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_commands WHERE kind='submit_review'").fetchone()[0] == 0


def test_a_stale_generation_submission_is_refused_before_any_write(rig, rearm):
    stale = replace(rearm.snapshot.binding, fence=replace(rearm.snapshot.binding.fence, generation=0))
    with pytest.raises(ControlStoreError, match="stale"):
        rig.executor().submit_bound_review(PR, "REQUEST_CHANGES", "fix", HEAD, workflow=stale, reviewer=BOT, patch_id=PATCH)
    assert rig.calls("submit_review") == []


@pytest.mark.parametrize("kwargs", [
    dict(patch_id=None),
    dict(patch_id="not-a-digest"),
    dict(patch_id=PATCH, escalate_cause="panic"),
    dict(patch_id=PATCH, reviewer=""),
    dict(patch_id=PATCH, escalate_cause="ceiling"),  # an escalation is a COMMENT review
    dict(patch_id=PATCH, escalate_cause=["ceiling"]),
])
def test_malformed_bound_submissions_are_rejected(rig, rearm, kwargs):
    with pytest.raises(ValueError):
        rig.executor().submit_bound_review(PR, "REQUEST_CHANGES", "x", HEAD, workflow=rearm.snapshot.binding,
                                           **{"reviewer": BOT, **kwargs})
    assert rig.calls("submit_review") == []


def test_a_resolve_binding_cannot_bind_a_review_submission(rig):
    from tests.fakes.workflow_authority import resolve_snapshot
    with pytest.raises(ValueError, match="review-rejection binding"):
        rig.executor().submit_bound_review(1, "COMMENT", "x", HEAD, workflow=resolve_snapshot().binding,
                                           reviewer=BOT, patch_id=PATCH)


# -- ceiling release projection ---------------------------------------------------------------------

def committed_release(rig):
    state = gated()
    change = reconcile_review_ceiling(
        state, observe(dismissed(rejections(2), "r0", "r1")), now=NOW, ceiling=3, evidence_ids=("reconcile:1",),
        trigger_label=TRIGGER, executor_identity=REVIEWER,
    )
    assert isinstance(change, WorkflowChanged) and len(change.commands) == 1
    install_workflow(rig.store, change.snapshot)
    with rig.store.transaction() as db:
        rig.store._enqueue_command(db, change.commands[0])
    return change


def test_a_verified_release_projection_succeeds(rig):
    change = committed_release(rig)
    rig.drain()
    assert rig.receipt(change.commands[0].command_id) == ("succeeded", None)
    assert rig.remote.get_pr(PR).labels == frozenset({TRIGGER})


def test_a_refused_release_projection_is_a_permanent_receipt_never_a_write(rig):
    change = committed_release(rig)
    rig.remote.external_pr_change(PR, labels=sorted({Label.NEEDS_HUMAN_REVIEW.value, Label.AI_BLOCKED.value}))
    rig.drain()
    status, error = rig.receipt(change.commands[0].command_id)
    assert status == "permanent_failure" and "hold labels changed" in error
    assert rig.calls("relabel_pr") == []


def test_an_uncertain_release_projection_surfaces_a_visible_gate(tmp_path):
    faults = {"relabel_pr_requested"}

    def fault(point):
        if point in faults:
            raise TimeoutError(point)
    rig = Rig(tmp_path, fault=fault)
    change = committed_release(rig)
    command = change.commands[0]
    rig.drain()
    assert rig.state(command.command_id) == "unknown_outcome"
    faults.update({"read"})
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(command.command_id), ReconciliationOnly):
            break
    state = rig.store.command_reconciliation(command.command_id)
    assert isinstance(state, ReconciliationOnly)
    gates = {getattr(g, "gate_id", None): g for g in rig.workflow().gates}
    assert isinstance(gates[state.gate_id].cause, RemoteOutcomeUncertain)
    assert rig.calls("relabel_pr") == []


def test_a_bound_submission_with_a_lost_reply_is_reconciled_on_its_schedule(tmp_path):
    faults = {"review_submitted"}

    def fault(point):
        if point in faults:
            if point == "review_submitted":
                faults.discard(point)  # the reply is lost once; the review itself landed
            raise TimeoutError(point)
    rig = Rig(tmp_path, fault=fault)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with pytest.raises(TimeoutError):
        rig.executor().submit_bound_review(PR, "REQUEST_CHANGES", "fix", HEAD, workflow=change.snapshot.binding,
                                           reviewer=BOT, patch_id=PATCH)
    command_id = rig.submission_id()
    assert rig.state(command_id) == "unknown_outcome"
    assert rig.store.bound_review_submission(command_id) is None
    faults.add("reviews_page:0")  # unreadable: bounded backoff to a visible gate, never a blind resubmission
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(command_id), ReconciliationOnly):
            break
    state = rig.store.command_reconciliation(command_id)
    assert isinstance(state, ReconciliationOnly)
    assert any(getattr(g, "gate_id", None) == state.gate_id for g in rig.workflow().gates)
    faults.clear()
    rig.advance(1800)
    rig.drain()
    assert rig.state(command_id) == "succeeded"
    assert len(rig.calls("submit_review")) == 1  # the landed review is observed, never resubmitted
    assert isinstance(rig.store.bound_review_submission(command_id), ReviewSubmissionReceipt)
    assert rig.receipt_object(command_id) == "submitted-2"


# -- bound submissions reconcile by identity, never by content ----------------------------------

def lost(point_names, *, once=True):
    faults = set(point_names)

    def fault(point):
        if point in faults:
            if once:
                faults.discard(point)
            raise TimeoutError(point)
    return fault


def submit_verdict(rig, change, body="fix"):
    return rig.executor().submit_bound_review(PR, "REQUEST_CHANGES", body, HEAD, workflow=change.snapshot.binding,
                                              reviewer=BOT, patch_id=PATCH)


@pytest.mark.parametrize("sql_hold", [False, True])
def test_linked_issue_hold_arriving_after_submission_acceptance_refuses_write(rig, rearm, sql_hold):
    rig.remote.external_pr_change(PR, closing_issue=7)
    # Unreadable linkage journals the command before any hold has been observed.
    with pytest.raises(Exception, match="pre-write observation unavailable"):
        submit_verdict(rig, rearm)
    command_id = rig.submission_id()
    if sql_hold:
        from tests.adapters.test_review_linkage import held_issue
        install_workflow(rig.store, held_issue())
        rig.remote.seed(IssueView(7))
    else:
        rig.remote.seed(IssueView(7, labels=frozenset({Label.NEEDS_BUDGET_REVIEW})))
    rig.advance(1800)
    rig.drain()
    assert rig.state(command_id) == "permanent_failure"
    assert "linked issue #7" in rig.receipt(command_id)[1]
    assert rig.calls("submit_review") == []


def test_unreadable_linked_issue_bounds_submission_reads_and_exposes_evidence_gate(rig, rearm):
    rig.remote.external_pr_change(PR, closing_issue=7)
    with pytest.raises(Exception, match="pre-write observation unavailable"):
        submit_verdict(rig, rearm)
    command_id = rig.submission_id()
    for _ in range(200):
        rig.advance(60)
        rig.drain()
    assert 5 <= rig.store.prewrite_read(command_id).failed_reads <= 13
    gate = rig.gate(prewrite_evidence_gate_id(command_id))
    assert isinstance(gate.cause, EvidenceUnavailable)
    assert "linked issue unavailable" in gate.cause.last_error
    assert rig.state(command_id) == "pending" and rig.calls("submit_review") == []


def test_lost_submission_present_receipt_settles_without_reading_new_unavailable_linkage(tmp_path):
    rig = Rig(tmp_path, fault=lost({"review_submitted"}))
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with pytest.raises(TimeoutError):
        submit_verdict(rig, change)
    command_id = rig.submission_id()
    rig.remote.external_pr_change(PR, closing_issue=7)
    rig.advance(1800)
    rig.drain()
    assert rig.state(command_id) == "succeeded"
    assert rig.receipt_object(command_id) == "submitted-2"
    assert len(rig.calls("submit_review")) == 1


def test_local_linkage_database_failure_is_not_submission_remote_read_backoff(rig, rearm):
    rig.remote.external_pr_change(PR, closing_issue=7)
    rig.remote.seed(IssueView(7))
    from tests.adapters.test_review_linkage import held_issue
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    install_workflow(rig.store, held_issue())
    with rig.store.transaction() as db:
        db.execute("DELETE FROM workflow_authority_epochs WHERE family='resolve'")
    with pytest.raises(WorkflowAuthorityError, match="retained"):
        submit_verdict(rig, rearm)
    command_id = rig.submission_id()
    assert rig.store.prewrite_read(command_id) is None
    assert rig.calls("submit_review") == []


def test_a_bound_submission_receipt_names_its_own_marked_review(rig, rearm):
    assert submit_verdict(rig, rearm)
    command_id = rig.submission_id()
    assert rig.receipt(command_id) == ("succeeded", None)
    assert rig.receipt_object(command_id) == "submitted-2"
    with closing(rig.remote._connect()) as db:
        body = db.execute("SELECT body FROM reviews WHERE review_id='submitted-2'").fetchone()[0]
    assert body.startswith("fix") and body.splitlines()[-1] == review_command_marker(command_id)


def test_an_earlier_identical_review_is_not_this_commands_review(tmp_path):
    """Regression: content matching let a pre-existing identical review settle a lost request as success."""
    rig = Rig(tmp_path, fault=lost({"review_requested"}))
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    rig.remote.seed_review(PR, "earlier", author=BOT, commit=HEAD, submitted_at="2026-09-25T11:00:05+00:00", body="fix")
    with pytest.raises(TimeoutError):
        submit_verdict(rig, change)
    command_id = rig.submission_id()
    assert rig.state(command_id) == "unknown_outcome" and rig.calls("submit_review") == []
    rig.advance(60)
    rig.drain()
    # Proven absence of its own review re-submits exactly once; the earlier review is not its receipt.
    assert len(rig.calls("submit_review")) == 1
    assert rig.receipt(command_id) == ("succeeded", None)
    assert rig.receipt_object(command_id) != "earlier"


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "DISMISSED"])
def test_a_later_review_of_another_command_is_never_claimed(tmp_path, state):
    """Re-review A/C: another command's review (even dismissed) on the same head during a long reconciliation."""
    rig = Rig(tmp_path, fault=lost({"review_requested"}))
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with pytest.raises(TimeoutError):
        rig.executor().submit_bound_review(PR, "REQUEST_CHANGES", "lgtm", HEAD, workflow=change.snapshot.binding,
                                           reviewer=BOT, patch_id=PATCH)
    command_id = rig.submission_id()
    rig.advance(3000)
    rig.remote.seed_review(PR, "other-command", author=BOT, state=state, commit=HEAD,
                           submitted_at=rig.clock[0].isoformat(), body="fix\n\n" + review_command_marker("b" * 64))
    rig.drain()
    assert len(rig.calls("submit_review")) == 1
    assert rig.receipt(command_id) == ("succeeded", None)
    assert rig.receipt_object(command_id) != "other-command"


@pytest.mark.parametrize("skew", [-200, 200])
def test_host_clock_skew_never_duplicates_or_loses_the_review(tmp_path, skew):
    """Re-review B: GitHub's clock ±200s from this host (WSL drift)."""
    rig = Rig(tmp_path, fault=lost({"review_submitted"}))
    rig.remote.clock = lambda: rig.clock[0] + timedelta(seconds=skew)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with pytest.raises(TimeoutError):
        submit_verdict(rig, change)
    command_id = rig.submission_id()
    rig.advance(60)
    rig.drain()
    assert len(rig.calls("submit_review")) == 1
    assert rig.receipt_object(command_id) == "submitted-2"


def test_a_marker_typed_by_another_author_never_matches(tmp_path):
    rig = Rig(tmp_path, fault=lost({"review_requested"}))
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with pytest.raises(TimeoutError):
        submit_verdict(rig, change)
    command_id = rig.submission_id()
    rig.remote.seed_review(PR, "spoof", author="mallory", commit=HEAD, submitted_at=rig.clock[0].isoformat(),
                           body=review_command_marker(command_id))
    rig.advance(60)
    rig.drain()
    assert len(rig.calls("submit_review")) == 1
    assert rig.receipt_object(command_id) not in {None, "spoof"}


def test_two_reviews_carrying_the_marker_are_ambiguous_and_never_buy_a_write(tmp_path):
    rig = Rig(tmp_path, fault=lost({"review_submitted"}))
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with pytest.raises(TimeoutError):
        submit_verdict(rig, change)
    command_id = rig.submission_id()
    rig.remote.seed_review(PR, "twin", author=BOT, commit=HEAD, submitted_at=NOW.isoformat(),
                           body=review_command_marker(command_id))
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(command_id), ReconciliationOnly):
            break
    state = rig.store.command_reconciliation(command_id)
    assert isinstance(state, ReconciliationOnly)
    gate = rig.gate(state.gate_id)
    assert isinstance(gate.cause, RemoteOutcomeUncertain) and "ambiguous" in gate.cause.last_error
    assert len(rig.calls("submit_review")) == 1
    assert rig.store.bound_review_submission(command_id) is None


def test_the_commands_own_review_matches_after_it_was_dismissed(tmp_path):
    rig = Rig(tmp_path, fault=lost({"review_submitted"}))
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with pytest.raises(TimeoutError):
        submit_verdict(rig, change)
    command_id = rig.submission_id()
    rig.remote.external_review_change("submitted-2", state="DISMISSED")
    rig.advance(60)
    rig.drain()
    assert rig.receipt_object(command_id) == "submitted-2" and len(rig.calls("submit_review")) == 1


def test_one_review_is_the_receipt_of_at_most_one_submission(rig, rearm):
    assert submit_verdict(rig, rearm)
    first = rig.submission_id()
    with closing(rig.store._connect()) as db:
        row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(first),)).fetchone()
    from issue_agent.adapters.control_store import _command_from_row
    from issue_agent.domain.contracts import EffectReceipt
    command = _command_from_row(row)
    other = replace(command, command_id=CommandId("second"), idempotency_key="idem:second",
                    payload={**command.payload, "body": "another verdict"})
    rig.store.enqueue_command(other)
    rig.store.begin_command(other.command_id)
    with pytest.raises(ControlStoreError, match="already the receipt"):
        rig.store.record_effect(EffectReceipt(other.command_id, REVIEWER, rig.clock[0], rig.clock[0],
                                              EffectStatus.SUCCEEDED, remote_object_id="submitted-2"))
    assert rig.state(other.command_id) == "executing"


def test_a_soft_failed_review_read_keeps_a_bound_submission_unknown_on_its_schedule(tmp_path):
    """A refused read (the real gateway's empty answer) is partial, never absence."""
    rig = Rig(tmp_path, fault=lost({"review_submitted"}))
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    with pytest.raises(TimeoutError):
        submit_verdict(rig, change)
    command_id = rig.submission_id()
    rig.remote.soft_failures.add("reviews")
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(command_id), ReconciliationOnly):
            break
    state = rig.store.command_reconciliation(command_id)
    assert isinstance(state, ReconciliationOnly) and rig.gate(state.gate_id) is not None
    assert len(rig.calls("submit_review")) == 1


def test_an_unreadable_head_defers_a_bound_submission_to_a_visible_evidence_gate(tmp_path):
    reads = []
    rig = Rig(tmp_path, fault=lambda point: reads.append(point) if point == "read" else None)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    rig.remote.soft_failures.add("get_pr")
    with pytest.raises(Exception, match="pre-write observation unavailable"):
        submit_verdict(rig, change)
    command_id = rig.submission_id()
    for _ in range(200):
        rig.advance(60)
        rig.drain()
    # Backoff: a handful of head reads over three hours, never one per drain.
    assert 5 <= rig.store.prewrite_read(command_id).failed_reads <= 13
    assert len(reads) <= 4 * 13  # this submission plus the re-arm's three obligations, each on its own schedule
    gate = rig.gate(prewrite_evidence_gate_id(command_id))
    assert isinstance(gate.cause, EvidenceUnavailable)
    assert rig.calls("submit_review") == [] and rig.state(command_id) == "pending"
    rig.remote.soft_failures.clear()
    rig.advance(1800)
    rig.drain()
    assert rig.state(command_id) == "succeeded" and rig.gate(prewrite_evidence_gate_id(command_id)) is None


@pytest.mark.parametrize("event, cause, patch", [
    ("COMMENT", None, PATCH),               # a verdict is APPROVE or REQUEST_CHANGES
    ("APPROVE", "ceiling", None),           # an escalation is a COMMENT
    ("REQUEST_CHANGES", "verdict", PATCH),
    ("COMMENT", "panic", None),
    ("COMMENT", "verdict", None),           # every bound submission records its reviewed patch
    ("COMMENT", "ceiling", None),
    ("DISMISS", None, PATCH),
    ("approve", None, PATCH),
])
def test_event_and_escalation_cause_must_pair(rig, rearm, event, cause, patch):
    with pytest.raises(ValueError):
        rig.executor().submit_bound_review(PR, event, "x", HEAD, workflow=rearm.snapshot.binding, reviewer=BOT,
                                           patch_id=patch, escalate_cause=cause)
    assert rig.calls("submit_review") == []


@pytest.mark.parametrize("event, cause, patch", [
    ("APPROVE", None, PATCH), ("REQUEST_CHANGES", None, PATCH), ("COMMENT", "verdict", PATCH), ("COMMENT", "ceiling", PATCH),
])
def test_valid_pairings_submit(rig, rearm, event, cause, patch):
    submitted = rig.executor().submit_bound_review(PR, event, "x", HEAD, workflow=rearm.snapshot.binding, reviewer=BOT,
                                              patch_id=patch, escalate_cause=cause)
    assert submitted is (event != "APPROVE")
    if event == "APPROVE":
        assert rig.calls("submit_review") == []


def test_a_mispaired_stored_submission_is_refused_at_enqueue(rig, rearm):
    rig.executor().submit_bound_review(PR, "APPROVE", "x", HEAD, workflow=rearm.snapshot.binding, reviewer=BOT,
                                       patch_id=PATCH)
    with closing(rig.store._connect()) as db:
        row = db.execute("SELECT * FROM lifecycle_commands WHERE kind='submit_review'").fetchone()
    from issue_agent.adapters.control_store import _command_from_row
    command = _command_from_row(row)
    forged = replace(command, command_id=CommandId("forged"), idempotency_key="idem:forged",
                     payload={**command.payload, "escalate_cause": "ceiling"})
    with pytest.raises(ControlStoreError):
        rig.store.enqueue_command(forged)


# -- unbound submissions: a failed read is never absence -------------------------------------------

def test_an_unbound_unknown_submission_whose_read_fails_is_never_resubmitted(tmp_path):
    rig = Rig(tmp_path, fault=lost({"review_submitted"}))
    with pytest.raises(TimeoutError):
        rig.executor().submit_review(PR, "APPROVE", "lgtm", HEAD)
    command_id = rig.submission_id()
    assert rig.state(command_id) == "unknown_outcome"
    rig.remote.soft_failures.add("has_review")
    for _ in range(5):
        rig.advance(600)
        rig.drain()
    assert rig.state(command_id) == "unknown_outcome"
    assert len(rig.calls("submit_review")) == 1
    assert any("ReviewReadUnavailable" in line for line in rig.logs)
    rig.remote.soft_failures.clear()
    rig.drain()
    assert rig.state(command_id) == "succeeded" and len(rig.calls("submit_review")) == 1


def test_a_refused_pre_submission_read_fails_closed_and_says_so(rig):
    """D: GitHub refusing the review read is reported as a refused read, not an infra death."""
    rig.remote.soft_failures.add("has_review")
    assert rig.executor().submit_review(PR, "APPROVE", "lgtm", HEAD) is False
    command_id = rig.submission_id()
    assert rig.state(command_id) == "pending" and rig.calls("submit_review") == []
    assert any("not submitted: review read refused" in line for line in rig.logs)


def test_the_real_gateway_soft_failure_never_resubmits_an_unknown_review(rig):
    """The production gateway, not the fake: GitHub refusing the review read (gh answers "") is not absence."""
    posts = []
    answers = {"read": "[[]]"}

    def runner(argv, _token):
        if "--method" in argv and "POST" in argv:
            posts.append(argv)
            raise GhUnavailable("network", "connection reset after send")
        if argv[1:3] == ["pr", "view"]:  # argv[0] is gh
            return json.dumps({"number": PR, "headRefOid": HEAD, "state": "OPEN"})
        return answers["read"]
    gateway = GitHubGateway("o/r", runner=runner)
    executor = rig.executor(remote=gateway)
    with pytest.raises(GhUnavailable):
        executor.submit_review(PR, "APPROVE", "lgtm", HEAD)
    command_id = rig.submission_id()
    assert rig.state(command_id) == "unknown_outcome" and len(posts) == 1
    answers["read"] = ""  # GitHub now refuses the review read (403/404)
    for _ in range(3):
        rig.advance(600)
        executor.drain_pending_commands()
    assert rig.state(command_id) == "unknown_outcome" and len(posts) == 1
    answers["read"] = json.dumps([[{"state": "APPROVED", "body": "lgtm", "commit_id": HEAD}]])
    executor.drain_pending_commands()
    assert rig.state(command_id) == "succeeded" and len(posts) == 1


# -- the bound-submission matcher and marker (pure) -----------------------------------------------

def _fact(review_id, *, command=None, author=BOT, state="CHANGES_REQUESTED", at=NOW):
    from issue_agent.domain.review_workflow import SubmittedReviewFact, SubmittedReviewState
    return SubmittedReviewFact(review_id, author, SubmittedReviewState(state), HEAD, at, command)


def _verdict(event="REQUEST_CHANGES"):
    from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
    return BoundReviewSubmissionOp(PR, event, "fix", HEAD, BOT, PATCH, None)


MINE = "c" * 64


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "APPROVED", "COMMENTED", "DISMISSED"])
def test_the_marked_review_by_the_review_identity_is_present_in_any_state(state):
    from issue_agent.adapters.review_effects import ReviewCheck, check_bound_submission
    found = check_bound_submission(_verdict("APPROVE"), CompleteReviews((
        _fact("earlier", command=None, at=NOW - timedelta(days=1)), _fact("mine", command=MINE, state=state),
        _fact("later", command="d" * 64, at=NOW + timedelta(days=1)),
    )), command_id=MINE)
    assert (found.check, found.remote_object_id) == (ReviewCheck.PRESENT, "mine")


@pytest.mark.parametrize("fact", [
    _fact("unmarked"), _fact("other", command="d" * 64), _fact("other-dismissed", command="d" * 64, state="DISMISSED"),
    _fact("spoof", command=MINE, author="mallory"),
])
def test_no_other_review_is_ever_this_commands(fact):
    from issue_agent.adapters.review_effects import ReviewCheck, check_bound_submission
    found = check_bound_submission(_verdict(), CompleteReviews((fact,)), command_id=MINE)
    assert found.check is ReviewCheck.APPLIES and found.remote_object_id is None


def test_a_partial_read_is_unavailable_and_an_empty_read_is_absent():
    from issue_agent.adapters.review_effects import ReviewCheck, check_bound_submission
    from issue_agent.domain.review_workflow import PartialReviews
    assert check_bound_submission(_verdict(), PartialReviews("page 2"), command_id=MINE).check is ReviewCheck.UNAVAILABLE
    assert check_bound_submission(_verdict(), CompleteReviews(()), command_id=MINE).check is ReviewCheck.APPLIES


def test_a_full_page_of_marked_reviews_is_ambiguous_not_present():
    from issue_agent.adapters.review_effects import ReviewCheck, check_bound_submission
    reviews = CompleteReviews(tuple(_fact(str(i), command=MINE, at=NOW + timedelta(seconds=i)) for i in range(512)))
    found = check_bound_submission(_verdict(), reviews, command_id=MINE)
    assert found.check is ReviewCheck.UNAVAILABLE and "512 reviews" in found.detail


def test_login_comparison_follows_github_case_insensitivity():
    from issue_agent.adapters.review_effects import ReviewCheck, check_bound_submission
    found = check_bound_submission(_verdict(), CompleteReviews((_fact("a", command=MINE, author=BOT.upper()),)),
                                   command_id=MINE)
    assert found.check is ReviewCheck.PRESENT


def test_marking_a_body_neutralizes_forged_markers_and_ends_with_its_own():
    forged = f"looks fine\n{review_command_marker('d' * 64)}\n"
    marked = mark_review_body(forged, MINE)
    assert body_review_command(marked) == MINE
    assert review_command_marker("d" * 64) not in marked and marked.startswith("looks fine")
    assert body_review_command(mark_review_body("", MINE)) == MINE
    assert body_review_command(mark_review_body("x" * 70000, MINE)) == MINE


@pytest.mark.parametrize("body", [
    None, 123, "", "no marker", f"{review_command_marker(MINE)}\ntrailing text",
    f"<!-- issue-agent-review-command id={MINE} extra -->", "<!-- issue-agent-review-command id= -->",
    "<!-- issue-agent-review-command id=a b -->",
])
def test_a_malformed_or_absent_body_names_no_command(body):
    assert body_review_command(body) is None


def test_a_marker_survives_line_ending_and_trailing_whitespace_normalization():
    assert body_review_command(f"verdict\r\n\r\n{review_command_marker(MINE)}\r\n  ") == MINE


@pytest.mark.parametrize("command_id", ["", "a b", "x>y", "a" * 513])
def test_a_marker_names_one_bounded_command(command_id):
    with pytest.raises(ValueError):
        review_command_marker(command_id)


# -- the trigger returns only after the counted reviews are dismissed (increment 6) ---------------

def test_a_refused_dismissal_keeps_the_trigger_projection_unwritten(rig, rearm):
    rig.remote.deny_dismissals()
    for _ in range(3):
        rig.drain()
        rig.advance(3600)
    assert rig.calls("relabel_pr") == []
    assert rig.state(projection(rearm).command_id) == "pending"
    assert Label.NEEDS_HUMAN_REVIEW in rig.remote.get_pr(PR).labels


def test_an_undecided_dismissal_holds_the_projection_until_it_is_proven(tmp_path):
    lost = {"dismiss_applied"}

    def fault(point):
        if point in lost:
            lost.discard(point)
            raise TimeoutError(point)
    rig = Rig(tmp_path, fault=fault)
    change = accepted_rearm()
    commit_rearm(rig.store, change)
    rig.drain()
    assert rig.calls("relabel_pr") == []  # the first dismissal's reply was lost
    rig.advance(60)
    rig.drain()
    assert rig.receipt(projection(change).command_id) == ("succeeded", None)
    assert len(rig.calls("dismiss_review")) == 2 and len(rig.calls("relabel_pr")) == 1


def test_a_projection_refusal_or_present_label_settles_without_waiting_for_dismissals(rig, rearm):
    rig.remote.deny_dismissals()
    rig.remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD)
    rig.drain()
    status, error = rig.receipt(projection(rearm).command_id)
    assert status == "permanent_failure" and "head moved" in error


def test_legacy_present_approval_reconciles_before_missing_test_receipt_or_linkage(rig):
    from issue_agent.adapters.effects import bound_review_command_id
    from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
    from tests.domain.test_review_workflow_policy import fresh
    state = fresh()
    install_workflow(rig.store, state)
    op = BoundReviewSubmissionOp(PR, "APPROVE", "legacy approval", HEAD, BOT, PATCH, None)
    command_id = bound_review_command_id(op, state.binding)
    command = LifecycleCommand(command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
        f"pr:{PR}", HEAD, op.payload(), repository="o/r", executor_identity=REVIEWER, workflow=state.binding)
    rig.store.enqueue_command(command)
    rig.remote.seed_review(PR, "legacy-present", author=BOT, state="APPROVED", commit=HEAD,
        submitted_at=NOW.isoformat(), body=op.body + "\n\n" + review_command_marker(str(command_id)))
    rig.remote.external_pr_change(PR, closing_issue=7)
    rig.drain()
    assert rig.state(command_id) == "succeeded" and rig.receipt_object(command_id) == "legacy-present"
    assert rig.calls("submit_review") == []
