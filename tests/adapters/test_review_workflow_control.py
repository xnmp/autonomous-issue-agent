"""The review owner's SQLite workflow authority (ADR 0015 review slice, increment 5).

Real SQLite authority plus the persistent GitHub double. Every transition
reaches storage only through `SQLiteReviewWorkflowControl`, which re-derives
it from facts minted from its own rows; effects run through the ordinary
review executor after acceptance has released the deployment barriers.
"""

from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

import pytest

import issue_agent.adapters.review_workflow_control as review_control_module
from issue_agent.adapters.control_store import (
    ESCALATION_SIGNAL_OWED_NAMESPACE, ControlStoreError, RetiredOwnerError, SQLiteControlStore, owe_escalation_signal,
)
from issue_agent.adapters.deployment_authority import deployment_effect_authority
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.adapters.review_workflow_control import (
    BARRIER_ROLES, SQLiteReviewWorkflowControl, release_refused_gate_id,
)
from issue_agent.adapters.review_execution_activation import (
    read_legacy_review_owner_lineage,
    read_legacy_review_submission_page,
)
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, workflow_scope
from issue_agent.domain.events import EventEnvelope, Producer, new_event_id
from issue_agent.domain.contracts import CommandId, CommandKind, EffectReceipt, EffectStatus, RunId
from issue_agent.domain.labels import Label
from issue_agent.domain.review_rearm import (
    AbsentReviewControl, MigrationRequiredReviewControl, PendingReviewRecovery, ReleaseStatus, ReviewControlView,
)
from issue_agent.domain.review_workflow import (
    PullRequestUnavailable, CancelledReviewRearm, CompletedReviewRearm, FreshReviewRequired, PendingReviewRearm, PullRequestState,
    RecordedPullRequest,
    ReviewedBaseline, ReviewProjectionRefused, ReviewRejectionCeiling, ReviewWorkflowSnapshot, SupersededReviewRearm,
)
from issue_agent.domain.review_workflow_policy import (
    ReleaseProjectionPending, ReviewHeld, ReviewNotOwed, ReviewOwed, SubmissionUnresolved, open_review_ceiling,
    record_review,
    reconcile_review_ceiling, request_review_rearm, review_rearm_observation_gate_id,
)
from issue_agent.domain.review_execution_activation import (
    LegacyReviewOwnerLineage, LegacyReviewSubmissionSource, UnverifiedLegacyRecordedReview,
    VerifiedLegacyRecordedReview,
)
from issue_agent.domain.workflow import (
    AuthorizeAbsentRetry, CancelRearm, ContinueGrantedRearm, EvidenceUnavailable, KeepGuidance, OpenGate,
    ReconciliationPolicy,
    OperatorActor, RearmNeedsDecision, RearmingGate, ReconciliationOnly, RemoteOutcomeUncertain, RequestRearm,
    RetiredUncertainCommand,
    RetiredSettledCommand, RetryAuthorizationRequired, StopReason, WorkflowAbsent, WorkflowCommitted,
    WorkflowConflict, WorkflowMigrationRequired, WorkflowMutation, WorkflowRejected, WorkflowRejection,
)
from issue_agent.domain.workflow_identity import FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey, WorkflowWriter
from issue_agent.domain.workflow_reconciliation import prewrite_evidence_gate_id
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.world import PRView
from tests.fakes.persistent_github import PersistentGitHub
from tests.fakes.review_workflow import ACTOR, HEAD, NOW, OTHER_HEAD, PATCH, PR_KEY

BOT = "review-bot[bot]"
TRIGGER = Label.AI_PR_OPEN
REVIEWER = "o/r:review:test"
WRITER = WorkflowWriter("o/r", "review", REVIEWER)
REVIEW = FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1)
PR = 9


def raising(points, lost=frozenset()):
    def fault(point):
        if point in points:
            raise ConnectionError(point)
        if point in lost:
            raise TimeoutError(point)  # a request lost in flight: outcome unknown
    return fault


class Rig:
    def __init__(self, tmp_path, *, reviews=2):
        self.tmp_path = tmp_path
        self.clock = [NOW]
        self.points: set[str] = set()
        self.lost: set[str] = set()
        self.holds: list[str] = []
        self.store = self._store()
        self.store.initialize()
        self.remote = PersistentGitHub(tmp_path / "remote.sqlite", fault=raising(self.points, self.lost),
                                       clock=lambda: self.clock[0])
        self.remote.seed_pr(PRView(PR, head_ref_oid=HEAD, labels=frozenset({Label.NEEDS_HUMAN_REVIEW})))
        for index in range(reviews):
            self.remote.seed_review(PR, f"r{index}", author=BOT, commit=HEAD,
                                    submitted_at=f"2026-09-25T11:00:0{index}+00:00")
        self.logs: list[str] = []

    def _store(self):
        store = SQLiteControlStore(str(self.tmp_path / "control.db"), now=lambda: self.clock[0])
        producer = Producer("deployment", "o/r", "review", 123, "boot")

        def factory(payload, correlation):
            return EventEnvelope(payload, new_event_id(self.clock[0]), 0, self.clock[0], self.clock[0], 0, producer,
                                 correlation)

        def offline(_event):
            raise RuntimeError("sink offline: events stay in the committed outbox")
        store.bind_event_outbox(factory, offline)
        return store

    def restart(self):
        """A fresh process: new store handle and control over the same files."""
        self.store = self._store()
        return self.control()

    def control(self, *, gateway=None, ceiling=3, **kwargs):
        kwargs.setdefault("external_holds", lambda _pr, _view: tuple(self.holds))
        return SQLiteReviewWorkflowControl(self.store, WRITER, gateway=gateway or self.remote, ceiling=ceiling,
                                           trigger_label=TRIGGER, rule=CountingRule(BOT), **kwargs)

    def activate(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r', ?, 1, 'test-source', 'test-inventory', ?)",
                       (WorkflowFamily.REVIEW_REJECTIONS.value, NOW.isoformat()))

    def executor(self):
        return GitHubEffectExecutor(self.remote, self.store, repository="o/r", identity=REVIEWER, log=self.logs.append)

    def drain(self):
        return self.executor().drain_pending_commands()

    def advance(self, seconds):
        self.clock[0] += timedelta(seconds=seconds)

    def workflow(self) -> ReviewWorkflowSnapshot:
        state = self.control().load_workflow(PR_KEY)
        assert isinstance(state, ReviewWorkflowSnapshot), state
        return state

    def gate(self, gate_id):
        return next((g for g in self.workflow().gates if getattr(g, "gate_id", None) == gate_id), None)

    def query(self, sql, *args):
        with closing(self.store._connect()) as db:
            return db.execute(sql, args).fetchall()

    def state(self, command_id):
        return self.query("SELECT state FROM lifecycle_commands WHERE command_id=?", str(command_id))[0][0]

    def receipt(self, command_id):
        rows = self.query("SELECT status, error FROM effect_receipts WHERE command_id=?", str(command_id))
        return None if not rows else tuple(rows[0])

    def submit(self, event="COMMENT", *, cause="ceiling", patch=PATCH, body="escalating"):
        before = {row[0] for row in self.query("SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'")}
        assert self.executor().submit_bound_review(PR, event, body, HEAD, workflow=self.workflow().binding,
                                                   reviewer=BOT, patch_id=patch, escalate_cause=cause)
        (created,) = {row[0] for row in self.query(
            "SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'")} - before
        return CommandId(created)

    def verdict(self, body="fix"):
        return self.submit("REQUEST_CHANGES", cause=None, patch=PATCH, body=body)

    def gated(self):
        """Activated, observed and ceiling-gated at generation 0."""
        self.activate()
        assert isinstance(self.control().ensure_observed_workflow(PR_KEY), WorkflowCommitted)
        opened = self.control().open_ceiling(self.submit())
        assert isinstance(opened, WorkflowCommitted) and not opened.idempotent, opened
        return opened.snapshot

    def ceiling_gate(self, state=None):
        state = state or self.workflow()
        return next(g for g in state.gates if isinstance(g, OpenGate) and isinstance(g.cause, ReviewRejectionCeiling))

    def request(self, request_id="request:1", *, actor=ACTOR, guidance=None, control=None):
        state = self.workflow()
        command = RequestRearm(request_id, state.revision, self.ceiling_gate(state).gate_id, guidance or KeepGuidance(), expected_binding=state.binding)
        return command, (control or self.control()).request_rearm(command, actor)

    def pending(self, request_id="request:1"):
        self.gated()
        _, result = self.request(request_id)
        assert isinstance(result, WorkflowCommitted) and isinstance(result.snapshot.intent, PendingReviewRearm), result
        return result.snapshot

    def commands(self, kind=None):
        rows = self.query("SELECT command_id, kind FROM lifecycle_commands ORDER BY command_id")
        return [CommandId(row[0]) for row in rows if kind is None or row[1] == kind.value]

    def reconcile(self, control=None):
        return (control or self.control()).reconcile_rearm(self.workflow().revision)

    def journal(self, event):
        return self.query("SELECT event_json FROM event_outbox WHERE event_json LIKE ?", f"%{event}%")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def fresh_observation(rig, key=PR_KEY):
    """Record one harness observation as the control would (for forged-proposal tests)."""
    control = rig.control()
    recorded = control._observe(key)
    assert isinstance(recorded, RecordedPullRequest)
    with rig.store.transaction() as db:
        evidence_id = control._insert_evidence(db, key, recorded)
    return recorded.observation, evidence_id


def commit(rig, proposal):
    return rig.control().commit_workflow(proposal.expected, (proposal.snapshot.authority,), proposal)


def inner_commit(rig, proposal, authorities=None):
    """The fact-minting boundary barrier-ordered paths commit through (defense in depth)."""
    control = rig.control()
    with rig.store.transaction() as db:
        return control._commit(db, proposal.expected,
                               (proposal.snapshot.authority,) if authorities is None else authorities, proposal)


# -- reads and activation ------------------------------------------------------------------

def test_an_inactive_family_reads_as_migration_required_and_is_never_initialized(rig):
    control = rig.control()
    read = control.load_workflow(PR_KEY)
    assert read == WorkflowMigrationRequired(PR_KEY, WorkflowFamily.REVIEW_REJECTIONS)
    assert control.review_control_view(PR_KEY) == MigrationRequiredReviewControl(PR_KEY, ())
    with pytest.raises(WorkflowAuthorityError, match="migration"):
        control.ensure_observed_workflow(PR_KEY)
    assert rig.query("SELECT COUNT(*) FROM workflows")[0][0] == 0


def test_an_activated_family_distinguishes_absence_then_observes_once(rig):
    rig.activate()
    control = rig.control()
    assert control.load_workflow(PR_KEY) == WorkflowAbsent(PR_KEY)
    assert isinstance(control.review_control_view(PR_KEY), AbsentReviewControl)
    first = control.ensure_observed_workflow(PR_KEY)
    assert isinstance(first, WorkflowCommitted) and not first.idempotent
    assert first.snapshot.episode.continuation == FreshReviewRequired(0)
    again = rig.restart().ensure_observed_workflow(PR_KEY)
    assert again.idempotent and again.snapshot == first.snapshot


def test_an_unreadable_pull_request_is_never_initialized(rig):
    rig.activate()
    rig.points.add("read")
    result = rig.control().ensure_observed_workflow(PR_KEY)
    assert result.__class__.__name__ == "PullRequestUnavailable"
    assert rig.control().load_workflow(PR_KEY) == WorkflowAbsent(PR_KEY)


def _activate_execution_family(rig):
    source = hashlib.sha256(b"execution source").hexdigest()
    inventory = hashlib.sha256(b"execution inventory").hexdigest()
    plan = hashlib.sha256(b"execution plan").hexdigest()
    with rig.store.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   ("o/r", WorkflowFamily.REVIEW_EXECUTION.value, 3, source, inventory, rig.clock[0].isoformat()))
        db.execute("INSERT INTO workflow_activation_evidence VALUES (?,?,?,?,?)",
                   ("o/r", WorkflowFamily.REVIEW_EXECUTION.value, 3, 0, plan))


def test_first_observation_after_execution_activation_mints_durable_origin(rig):
    from issue_agent.domain.review_execution import CurrentReviewExecution, ReviewExecutionOriginKind

    rig.activate()
    _activate_execution_family(rig)
    first = rig.control().ensure_observed_workflow(PR_KEY)
    assert isinstance(first, WorkflowCommitted)
    assert isinstance(first.snapshot.execution, CurrentReviewExecution)
    assert first.snapshot.execution.origin.kind is ReviewExecutionOriginKind.INITIALIZED
    assert first.snapshot.execution.last_sequence == 0
    assert first.snapshot.execution.state.failures == 0
    assert rig.restart().load_workflow(PR_KEY) == first.snapshot
    assert rig.control().ensure_observed_workflow(PR_KEY) == WorkflowCommitted(first.snapshot, idempotent=True)
    assert rig.query("SELECT COUNT(*) FROM workflow_components WHERE family='review_execution'")[0][0] == 1
    assert rig.query("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-origin'")[0][0] == 1
    assert rig.query("SELECT COUNT(*) FROM workflow_activation_imports WHERE family='review_execution'")[0][0] == 0


def test_initialized_origin_uses_one_claim_time_at_freshness_boundary(rig):
    rig.activate()
    _activate_execution_family(rig)
    control = rig.control()
    observed_then_claimed = iter((NOW, NOW + timedelta(seconds=300)))
    control._now = lambda: next(observed_then_claimed, NOW + timedelta(seconds=301))
    first = control.ensure_observed_workflow(PR_KEY)
    assert isinstance(first, WorkflowCommitted)
    assert rig.restart().load_workflow(PR_KEY) == first.snapshot
    with rig.store.transaction() as db:
        db.execute("UPDATE workflow_authority_epochs SET activated_at=? WHERE family='review_execution'",
                   ((NOW + timedelta(seconds=1)).isoformat(),))
    with pytest.raises(WorkflowAuthorityError):
        rig.restart().load_workflow(PR_KEY)


def test_first_observation_retries_when_execution_activates_during_remote_read(rig):
    rig.activate()
    control = rig.control()
    read = control._observe

    def activate_after_read(key):
        recorded = read(key)
        rig.advance(1)
        _activate_execution_family(rig)
        return recorded

    control._observe = activate_after_read
    with pytest.raises(WorkflowAuthorityError, match="retry the read"):
        control.ensure_observed_workflow(PR_KEY)
    assert rig.query("SELECT COUNT(*) FROM workflows")[0][0] == 0
    retried = rig.restart().ensure_observed_workflow(PR_KEY)
    assert isinstance(retried, WorkflowCommitted)
    assert rig.restart().load_workflow(PR_KEY) == retried.snapshot


def test_execution_initialization_refuses_oversized_activation_source_before_writing(rig):
    rig.activate()
    _activate_execution_family(rig)
    with rig.store.transaction() as db:
        db.execute("UPDATE workflow_authority_epochs SET source_digest=? WHERE family='review_execution'",
                   ("x" * 262145,))
    with pytest.raises(WorkflowAuthorityError, match="activation evidence"):
        rig.control().ensure_observed_workflow(PR_KEY)
    assert rig.query("SELECT COUNT(*) FROM workflows")[0][0] == 0


def test_execution_initialization_rolls_back_if_activation_is_aliased(rig):
    rig.activate()
    _activate_execution_family(rig)
    with rig.store.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs "
                   "SELECT CAST(repository AS BLOB),family,epoch,source_digest,inventory_digest,activated_at "
                   "FROM workflow_authority_epochs WHERE family='review_execution'")
    with pytest.raises(WorkflowAuthorityError, match="aliased"):
        rig.control().ensure_observed_workflow(PR_KEY)
    assert rig.query("SELECT COUNT(*) FROM workflows")[0][0] == 0
    assert rig.query("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-origin'")[0][0] == 0


@pytest.mark.parametrize("corruption", (
    "missing_origin", "changed_origin", "aliased_origin", "missing_observation",
    "changed_observation", "aliased_observation", "missing_plan", "changed_plan",
    "changed_source", "changed_inventory", "oversized_source", "changed_epoch",
    "changed_early_activation", "import_conflict",
))
def test_initialized_execution_read_refuses_broken_ancestry(rig, corruption):
    rig.activate()
    _activate_execution_family(rig)
    assert isinstance(rig.control().ensure_observed_workflow(PR_KEY), WorkflowCommitted)
    with rig.store.transaction() as db:
        if corruption == "missing_origin":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-origin'")
        elif corruption == "changed_origin":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='review-execution-origin'")
        elif corruption == "aliased_origin":
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "SELECT CAST(namespace AS BLOB),scope,key,claimed_at,payload_json FROM dedupe_keys "
                       "WHERE namespace='review-execution-origin'")
        elif corruption == "missing_observation":
            db.execute("DELETE FROM workflow_evidence WHERE kind='observation'")
        elif corruption == "changed_observation":
            db.execute("UPDATE workflow_evidence SET payload_json='{}' WHERE kind='observation'")
        elif corruption == "aliased_observation":
            db.execute("INSERT INTO workflow_evidence "
                       "SELECT CAST(evidence_id AS BLOB),repository,target_kind,target_number,command_id,kind,payload_json "
                       "FROM workflow_evidence WHERE kind='observation'")
        elif corruption == "missing_plan":
            db.execute("DELETE FROM workflow_activation_evidence WHERE family='review_execution'")
        elif corruption == "changed_plan":
            db.execute("UPDATE workflow_activation_evidence SET plan_digest=? WHERE family='review_execution'",
                       ("f" * 64,))
        elif corruption == "changed_source":
            db.execute("UPDATE workflow_authority_epochs SET source_digest=? WHERE family='review_execution'",
                       ("f" * 64,))
        elif corruption == "changed_inventory":
            db.execute("UPDATE workflow_authority_epochs SET inventory_digest=? WHERE family='review_execution'",
                       ("f" * 64,))
        elif corruption == "oversized_source":
            db.execute("UPDATE workflow_authority_epochs SET source_digest=? WHERE family='review_execution'",
                       ("x" * 262145,))
        elif corruption == "changed_epoch":
            db.execute("UPDATE workflow_authority_epochs SET epoch=4 WHERE family='review_execution'")
        elif corruption == "changed_early_activation":
            db.execute("UPDATE workflow_authority_epochs SET activated_at=? WHERE family='review_execution'",
                       ((NOW - timedelta(seconds=1)).isoformat(),))
        else:
            db.execute("INSERT INTO workflow_activation_imports VALUES (?,?,?,?,?,?,?)",
                       ("o/r", "review_execution", 3, "pr", PR, "source", "{}"))
    with pytest.raises(WorkflowAuthorityError):
        rig.restart().load_workflow(PR_KEY)


@pytest.mark.parametrize("writer", [
    WorkflowWriter("o/r", "fix", "o/r:fix:test"),
    WorkflowWriter("o/r", "review", "o/r:fix:test"),
    WorkflowWriter("o/r", "review", "other/repo:review:test"),
])
def test_only_the_repositorys_review_owner_holds_review_authority(rig, writer):
    with pytest.raises(ValueError, match="owner"):
        SQLiteReviewWorkflowControl(rig.store, writer, gateway=rig.remote, ceiling=3, trigger_label=TRIGGER,
                                    rule=CountingRule(BOT))


def test_a_gateway_without_review_reads_is_refused(rig):
    with pytest.raises(ValueError, match="gateway"):
        rig.control(gateway=object())


def test_a_foreign_or_issue_key_is_outside_scope(rig):
    from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowKey
    for key in (WorkflowKey("o/r", IssueRef(9)), WorkflowKey("x/y", PullRequestRef(9))):
        with pytest.raises(WorkflowAuthorityError, match="scope"):
            rig.control().load_workflow(key)


# -- ceiling opening and recorded reviews -----------------------------------------------------

def test_a_verified_ceiling_escalation_opens_the_gate_once(rig):
    state = rig.gated()
    gate = rig.ceiling_gate(state)
    assert [r.review_id for r in gate.cause.counted] == ["r0", "r1"] and gate.cause.head_sha == HEAD
    escalation = rig.commands(CommandKind.SUBMIT_REVIEW)[0]
    replay = rig.restart().open_ceiling(escalation)
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent and replay.snapshot == state


def test_a_verdict_or_unsettled_submission_cannot_open_a_ceiling(rig):
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    verdict = rig.verdict()
    refused = rig.control().open_ceiling(verdict)
    assert isinstance(refused, WorkflowRejected) and refused.code is WorkflowRejection.INVALID_SCOPE
    rig.points.add("review_requested")
    with pytest.raises(ConnectionError):
        rig.submit()
    rig.points.clear()
    unsettled = [c for c in rig.commands(CommandKind.SUBMIT_REVIEW) if c != verdict][0]
    assert isinstance(rig.control().open_ceiling(unsettled), WorkflowRejected)
    assert rig.workflow().gates == ()


def test_an_unavailable_read_never_opens_a_ceiling(rig):
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    escalation = rig.submit()
    rig.points.add("reviews_page:0")
    refused = rig.control().open_ceiling(escalation)
    assert isinstance(refused, WorkflowRejected) and refused.code is WorkflowRejection.OBSERVATION_UNAVAILABLE
    assert rig.workflow().gates == ()


def test_a_recorded_review_names_the_github_review_it_matched_and_records_once(rig):
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    first = rig.verdict("fix one")
    recorded = rig.control().record_review(first)
    assert isinstance(recorded, WorkflowCommitted) and not recorded.idempotent
    baseline = recorded.snapshot.episode.continuation
    assert isinstance(baseline, ReviewedBaseline) and baseline.source.command_id == first
    assert rig.restart().record_review(first).idempotent
    second = rig.verdict("fix two")
    newer = rig.control().record_review(second).snapshot
    # Re-recording the older receipt cannot roll the baseline back, through
    # the control or through a proposal built directly from the receipt.
    assert rig.control().record_review(first).idempotent
    bound = rig.store.bound_review_submission(first)
    assert bound.receipt_id.startswith("github-review:submitted-")
    rollback = record_review(newer, bound)
    assert rollback.mutation is WorkflowMutation.RECORD_REVIEW
    assert isinstance(commit(rig, rollback), WorkflowConflict)
    assert rig.workflow().episode.continuation.source.command_id == second


# -- the commit boundary refuses forged priors, facts and preflights ----------------------------

def test_a_forged_prior_cannot_keep_or_drop_the_stored_baseline(rig):
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    stored = rig.control().record_review(rig.verdict()).snapshot
    escalation = rig.store.bound_review_submission(rig.submit())
    observation, evidence_id = fresh_observation(rig)
    forged = replace(stored, episode=replace(stored.episode, continuation=FreshReviewRequired(0)))
    proposal = open_review_ceiling(forged, observation, now=rig.clock[0], rule=CountingRule(BOT), ceiling=3,
                                   escalation=escalation, trigger_label=TRIGGER, executor_identity=REVIEWER,
                                   evidence_ids=(evidence_id,))
    assert proposal.mutation is WorkflowMutation.OPEN_GATE and proposal.expected == stored.revision
    with pytest.raises(WorkflowAuthorityError, match="stored workflow snapshot"):
        commit(rig, proposal)
    assert rig.workflow() == stored
    honest = open_review_ceiling(stored, observation, now=rig.clock[0], rule=CountingRule(BOT), ceiling=3,
                                 escalation=escalation, trigger_label=TRIGGER, executor_identity=REVIEWER,
                                   evidence_ids=(evidence_id,))
    kept = commit(rig, honest).snapshot
    assert kept.episode == stored.episode and rig.ceiling_gate(kept)


def test_a_ceiling_proposal_without_a_harness_escalation_is_refused(rig):
    rig.activate()
    stored = rig.control().ensure_observed_workflow(PR_KEY).snapshot
    verdict = rig.store.bound_review_submission(rig.verdict())
    from issue_agent.domain.review_workflow_policy import CeilingEscalation
    forged = CeilingEscalation(verdict.submission, HEAD, verdict.receipt_id, "ceiling", CountingRule(BOT))
    observation, evidence_id = fresh_observation(rig)
    proposal = open_review_ceiling(stored, observation, now=rig.clock[0], rule=CountingRule(BOT), ceiling=3,
                                   escalation=forged, trigger_label=TRIGGER, executor_identity=REVIEWER,
                                   evidence_ids=(evidence_id,))
    with pytest.raises(WorkflowAuthorityError, match="escalation"):
        commit(rig, proposal)
    assert rig.workflow().gates == ()


def test_a_proposal_naming_absent_or_foreign_observation_evidence_is_refused(rig):
    rig.activate()
    stored = rig.control().ensure_observed_workflow(PR_KEY).snapshot
    escalation = rig.store.bound_review_submission(rig.submit())
    observation, _ = fresh_observation(rig)
    proposal = open_review_ceiling(stored, observation, now=rig.clock[0], rule=CountingRule(BOT), ceiling=3,
                                   escalation=escalation, trigger_label=TRIGGER, executor_identity=REVIEWER,
                                   evidence_ids=("pr-observation:invented",))
    with pytest.raises(WorkflowAuthorityError, match="durable harness"):
        commit(rig, proposal)


def test_a_stale_observation_cannot_decide_a_ceiling(rig):
    rig.activate()
    stored = rig.control().ensure_observed_workflow(PR_KEY).snapshot
    escalation = rig.store.bound_review_submission(rig.submit())
    observation, evidence_id = fresh_observation(rig)
    rig.advance(301)
    proposal = open_review_ceiling(stored, observation, now=rig.clock[0] - timedelta(seconds=301),
                                   rule=CountingRule(BOT), ceiling=3, escalation=escalation,
                                   trigger_label=TRIGGER, executor_identity=REVIEWER, evidence_ids=(evidence_id,))
    with pytest.raises(WorkflowAuthorityError, match="refused"):
        commit(rig, proposal)


def forged_request(rig, *, evidence_id=None, observed_at=None):
    state = rig.workflow()
    observation, recorded_id = fresh_observation(rig)
    if observed_at is not None:
        observation = replace(observation, observed_at=observed_at)
    command = RequestRearm("request:forged", state.revision, rig.ceiling_gate(state).gate_id, KeepGuidance(), expected_binding=state.binding)
    return request_review_rearm(state, command, ACTOR, observation, now=rig.clock[0], active_reviews=(),
                                evidence_id=evidence_id or recorded_id, trigger_label=TRIGGER,
                                executor_identity=REVIEWER)


def test_a_preflight_must_name_a_fresh_durable_complete_read(rig):
    rig.gated()
    before = rig.workflow()
    with pytest.raises(WorkflowAuthorityError, match="durable harness"):
        inner_commit(rig, forged_request(rig, evidence_id="complete-reviews:invented"))
    rig.advance(301)
    stale = forged_request(rig, observed_at=rig.clock[0])  # recorded now, then aged past the bound
    rig.advance(301)
    with pytest.raises(WorkflowAuthorityError):
        inner_commit(rig, stale)
    future = forged_request(rig)
    with rig.store.transaction() as db:
        evidence_id = future.audit.preflight.complete_reviews_evidence_id
        payload = json.loads(db.execute("SELECT payload_json FROM workflow_evidence WHERE evidence_id=?",
                                        (evidence_id,)).fetchone()[0])
    rig.clock[0] -= timedelta(seconds=10)  # the store clock is now before the read
    with pytest.raises(WorkflowAuthorityError, match="fresh read"):
        inner_commit(rig, future)
    assert payload and rig.workflow() == before


def test_a_preflight_naming_another_kind_of_evidence_is_refused(rig):
    rig.gated()
    proposal = forged_request(rig)
    evidence_id = proposal.audit.preflight.complete_reviews_evidence_id
    with rig.store.transaction() as db:
        escalation = db.execute("SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'").fetchone()[0]
        db.execute("UPDATE workflow_evidence SET kind='absence', command_id=? WHERE evidence_id=?",
                   (escalation, evidence_id))
    with pytest.raises(WorkflowAuthorityError, match="durable harness"):
        inner_commit(rig, proposal)


def test_a_direct_commit_of_a_true_proposal_is_accepted(rig):
    """Generation advances never bypass the barriers or the active-run drain.

    `commit_workflow` refuses a true request proposal outright; the same
    proposal is accepted by the inner fact-minting boundary (the non-vacuity
    anchor for the forged cases above).
    """
    rig.gated()
    proposal = forged_request(rig)
    with pytest.raises(WorkflowAuthorityError, match="barrier-ordered"):
        commit(rig, proposal)
    assert rig.query("SELECT COUNT(*) FROM workflow_rearms")[0][0] == 0
    result = inner_commit(rig, proposal)
    assert isinstance(result.snapshot.intent, PendingReviewRearm)


def test_commit_workflow_refuses_every_barrier_ordered_mutation(rig):
    rig.gated()
    rig.remote.external_review_change("r0", state="DISMISSED")
    observation, evidence_id = fresh_observation(rig)
    state = rig.workflow()
    release = reconcile_review_ceiling(state, observation, now=rig.clock[0], ceiling=3, evidence_ids=(evidence_id,),
                                       trigger_label=TRIGGER, executor_identity=REVIEWER)
    assert release.mutation is WorkflowMutation.RELEASE_CEILING
    with pytest.raises(WorkflowAuthorityError, match="barrier-ordered"):
        commit(rig, release)
    assert rig.workflow() == state
    pending = rig.request()[1].snapshot
    stopped = rig.control().cancel_rearm(
        CancelRearm("cancel:1", pending.revision, "request:1", "x", expected_binding=pending.binding), ACTOR).snapshot
    observation, evidence_id = fresh_observation(rig)
    from issue_agent.domain.review_workflow_policy import continue_review_rearm
    carried = continue_review_rearm(stopped, ContinueGrantedRearm("request:2", stopped.revision, "request:1", expected_binding=stopped.binding), ACTOR,
                                    observation, now=rig.clock[0], active_reviews=(), evidence_id=evidence_id,
                                    trigger_label=TRIGGER, executor_identity=REVIEWER)
    assert carried.mutation is WorkflowMutation.CONTINUE_REARM
    with pytest.raises(WorkflowAuthorityError, match="barrier-ordered"):
        commit(rig, carried)
    assert rig.workflow() == stopped


# -- operator acceptance ---------------------------------------------------------------------

def test_request_commits_intent_commands_evidence_schedule_and_outbox_together(rig):
    state = rig.gated()
    outbox_before = rig.query("SELECT COUNT(*) FROM event_outbox")[0][0]
    command, result = rig.request()
    after = result.snapshot
    intent = after.intent
    assert isinstance(intent, PendingReviewRearm) and after.revision.fence.generation == 1
    assert [op.review.review_id for op in intent.operations if hasattr(op, "review")] == ["r0", "r1"]
    commands = rig.query("SELECT operation_key, disposition FROM workflow_rearm_commands WHERE request_id='request:1'")
    assert sorted(row[0] for row in commands) == ["dismiss:r0", "dismiss:r1", "review_projection"]
    assert set(rig.commands()) >= set(intent.required_commands)
    audit = rig.control().accepted_rearm(PR_KEY, "request:1")
    assert audit.command == command and audit.preflight.counted == rig.ceiling_gate(state).cause.counted
    evidence = rig.query("SELECT kind, payload_json FROM workflow_evidence WHERE evidence_id=?",
                         audit.preflight.complete_reviews_evidence_id)
    assert evidence[0][0] == "observation"
    view = rig.control().review_control_view(PR_KEY)
    assert isinstance(view, ReviewControlView) and view.observation is not None
    assert rig.query("SELECT COUNT(*) FROM event_outbox")[0][0] > outbox_before


def test_request_replay_is_idempotent_without_a_read_and_reuse_conflicts(rig):
    rig.pending()
    reads = []

    class Counting:
        def get_pr(self, number, **kwargs):
            reads.append(number)
            return rig.remote.get_pr(number, **kwargs)

        def pull_request_reviews(self, pr):
            return rig.remote.pull_request_reviews(pr)

    control = rig.control(gateway=Counting())
    state = rig.workflow()
    original = rig.control().accepted_rearm(PR_KEY, "request:1").command
    replay = control.request_rearm(original, ACTOR)
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent and replay.snapshot == state
    assert reads == []
    other_actor = OperatorActor("operator:mallory", "dashboard")
    assert isinstance(control.request_rearm(original, other_actor), WorkflowConflict)
    from issue_agent.domain.workflow import SetGuidance
    rewritten = replace(original, guidance=SetGuidance("different"))
    assert isinstance(control.request_rearm(rewritten, ACTOR), WorkflowConflict)
    assert rig.workflow() == state


def test_an_older_request_id_is_refused_after_a_newer_decision(rig):
    state = rig.pending()
    original = rig.control().accepted_rearm(PR_KEY, "request:1").command
    cancelled = rig.control().cancel_rearm(CancelRearm("cancel:1", state.revision, "request:1", "scope", expected_binding=state.binding), ACTOR)
    assert isinstance(cancelled.snapshot.intent, CancelledReviewRearm)
    continued = rig.control().continue_rearm(
        ContinueGrantedRearm("request:2", cancelled.snapshot.revision, "request:1", expected_binding=cancelled.snapshot.binding), ACTOR)
    assert isinstance(continued, WorkflowCommitted) and continued.snapshot.intent.request_id == "request:2", continued
    older = rig.restart().request_rearm(original, ACTOR)
    assert isinstance(older, WorkflowRejected) and older.code is WorkflowRejection.INVALID_SCOPE
    # The same holds for a direct replay of the older proposal's receipt.
    assert rig.workflow().intent.request_id == "request:2"


def test_a_stale_revision_conflicts_before_any_read(rig):
    rig.gated()
    state = rig.workflow()
    stale = RequestRearm("request:1", replace(state.revision, version=state.revision.version - 1),
                         rig.ceiling_gate(state).gate_id, KeepGuidance(), expected_binding=state.binding)
    rig.points.add("read")  # a read would be unavailable; a conflict proves none happened
    assert isinstance(rig.control().request_rearm(stale, ACTOR), WorkflowConflict)


@pytest.mark.parametrize("point,code", [
    ("read", WorkflowRejection.OBSERVATION_UNAVAILABLE),
    ("reviews_page:0", None),
])
def test_an_unavailable_or_partial_refresh_accepts_nothing(rig, point, code):
    state = rig.gated()
    rig.points.add(point)
    _, result = rig.request()
    assert isinstance(result, WorkflowRejected)
    if code is not None:
        assert result.code is code
    assert rig.workflow() == state
    assert rig.query("SELECT COUNT(*) FROM workflow_rearms")[0][0] == 0


def test_an_active_review_run_blocks_acceptance(rig):
    state = rig.gated()
    from issue_agent.adapters.control_store import workflow_episode_key
    rig.store.start_run(RunId("review:1"), json.dumps({"repository": "o/r", "pr": PR, "number": PR, "kind": "REVIEW"}),
                        "REVIEW", REVIEWER, "config", state="running")
    with rig.store.transaction() as db:
        db.execute("UPDATE runs SET workflow_binding_json=?, workflow_episode_key=? WHERE run_id='review:1'",
                   (encode_workflow(state.binding), workflow_episode_key(state.binding)))
    _, result = rig.request()
    assert isinstance(result, WorkflowRejected) and result.code is WorkflowRejection.OTHER_HOLD
    assert rig.workflow() == state


@pytest.mark.parametrize("number, state, blocks", [
    (PR, "running", True),
    (PR, "completed", False),
    (PR + 1, "running", False),
], ids=["live-for-this-pr", "finished", "another-pr"])
def test_an_unbound_review_run_admitted_before_activation_blocks_acceptance_until_it_ends(rig, number, state, blocks):
    """Admission refuses unbound review legs once active, so only a pre-activation run can match (ADR 0015)."""
    gated = rig.gated()
    rig.store.start_run(RunId("legacy-review"), json.dumps({
        "repository": "o/r", "role": "review", "pr": number, "number": number, "kind": "REVIEW"}),
        "REVIEW", REVIEWER, "config", state=state)
    _, result = rig.request()
    if blocks:
        assert isinstance(result, WorkflowRejected) and result.code is WorkflowRejection.OTHER_HOLD
        assert rig.workflow() == gated
    else:
        assert isinstance(result, WorkflowCommitted), result


def test_a_crash_inside_the_acceptance_commit_leaves_nothing_and_a_restart_accepts(rig, monkeypatch):
    state = rig.gated()

    def crash(*args, **kwargs):
        raise RuntimeError("process died mid-commit")
    monkeypatch.setattr(review_control_module, "record_commit", crash)
    with pytest.raises(RuntimeError):
        rig.request()
    monkeypatch.undo()
    assert rig.workflow() == state
    for table in ("workflow_rearms", "workflow_rearm_commands"):
        assert rig.query(f"SELECT COUNT(*) FROM {table}")[0][0] == 0
    assert rig.commands(CommandKind.DISMISS_REVIEW) == []
    _, result = rig.request(control=rig.restart())
    assert isinstance(result.snapshot.intent, PendingReviewRearm)


def test_after_the_acceptance_commit_a_restart_sees_one_pending_rearm(rig):
    state = rig.pending()
    control = rig.restart()
    assert control.load_workflow(PR_KEY) == state
    assert control.pending_rearms() == ()  # scheduled, not yet due
    rig.advance(3600)
    (due,) = control.pending_rearms()
    assert isinstance(due, PendingReviewRecovery) and due.snapshot == state


def test_acceptance_takes_every_barrier_in_order_reads_outside_sql_and_releases_before_effects(rig, monkeypatch):
    rig.gated()
    events = []
    real = review_control_module.deployment_effect_barrier

    @contextmanager
    def recording(path, repository, role):
        with real(path, repository, role):
            events.append(("acquire", role))
            yield
            events.append(("release", role))
    monkeypatch.setattr(review_control_module, "deployment_effect_barrier", recording)

    class Probing:
        def get_pr(self, number, **kwargs):
            events.append(("read", None))
            for role in BARRIER_ROLES:  # every effect runner is excluded during the refresh
                with pytest.raises(BlockingIOError):
                    with deployment_effect_authority(rig.store.path, "o/r", role, blocking=False):
                        pass
            with rig.store.transaction() as db:  # no SQL write transaction is held across the read
                db.execute("SELECT 1")
            return rig.remote.get_pr(number, **kwargs)

        def pull_request_reviews(self, pr):
            return rig.remote.pull_request_reviews(pr)

    _, result = rig.request(control=rig.control(gateway=Probing()))
    assert isinstance(result.snapshot.intent, PendingReviewRearm)
    assert events[:3] == [("acquire", "fix"), ("acquire", "review"), ("acquire", "steward")]
    assert ("read", None) in events[3:events.index(("release", "steward"))]
    assert events[-3:] == [("release", "steward"), ("release", "review"), ("release", "fix")]
    with deployment_effect_authority(rig.store.path, "o/r", "review", blocking=False):
        assert rig.drain() == 3  # effects run only after acceptance released the barriers
    assert rig.remote.review_state("r0") == "DISMISSED"


def retired(rig, command_id):
    return isinstance(rig.store.command_reconciliation(command_id), RetiredSettledCommand) and rig.query(
        "SELECT disposition FROM workflow_rearm_commands WHERE command_id=?", str(command_id))[0][0] == "retired"


def later_review(rig, review_id, commit=HEAD):
    rig.advance(1)
    rig.remote.seed_review(PR, review_id, author=BOT, commit=commit, submitted_at=rig.clock[0].isoformat())


def test_cancel_takes_the_barriers_and_is_idempotent_with_reuse_conflicting(rig):
    state = rig.pending()
    calls = rig.remote.calls
    command = CancelRearm("cancel:1", state.revision, "request:1", "scope changed", expected_binding=state.binding)
    first = rig.control().cancel_rearm(command, ACTOR)
    intent = first.snapshot.intent
    assert isinstance(intent, CancelledReviewRearm) and intent.retired_obligations == state.intent.required_commands
    assert all(retired(rig, c) for c in state.intent.required_commands)
    assert rig.restart().cancel_rearm(command, ACTOR).idempotent
    assert isinstance(rig.control().cancel_rearm(replace(command, reason="other"), ACTOR), WorkflowConflict)
    assert rig.drain() == 0 and rig.remote.calls == calls


# -- reconciliation of pending review re-arms ------------------------------------------------

def test_reconciliation_completes_after_every_obligation_is_verified(rig):
    state = rig.pending()
    assert isinstance(rig.reconcile().snapshot.intent, PendingReviewRearm)  # nothing verified yet
    rig.drain()
    done = rig.reconcile()
    assert isinstance(done.snapshot.intent, CompletedReviewRearm)
    assert set(done.snapshot.intent.verified_commands) == set(state.intent.required_commands)
    assert done.snapshot.gates == ()
    assert rig.restart().pending_rearms() == ()


def test_a_closed_pull_request_supersedes_as_target_terminal(rig):
    rig.pending()
    rig.remote.external_pr_change(PR, state="CLOSED")
    result = rig.reconcile()
    assert isinstance(result.snapshot.intent, SupersededReviewRearm)
    assert result.snapshot.intent.reason is StopReason.TARGET_TERMINAL


def test_a_moved_head_supersedes_as_decision_changed_and_retires_obligations(rig):
    state = rig.pending()
    rig.remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD)
    result = rig.reconcile()
    intent = result.snapshot.intent
    assert isinstance(intent, SupersededReviewRearm) and intent.reason is StopReason.DECISION_CHANGED
    assert all(retired(rig, c) for c in state.intent.required_commands)
    decision = [g for g in result.snapshot.gates if isinstance(g.cause, RearmNeedsDecision)]
    assert decision and decision[0].cause.stopped_request_id == "request:1"
    assert rig.drain() == 0


def test_a_refused_dismissal_supersedes_as_required_effect_rejected(rig):
    state = rig.pending()
    rig.remote.deny_dismissals()
    rig.drain()
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    assert rig.receipt(dismissal)[0] == "permanent_failure"
    result = rig.reconcile()
    intent = result.snapshot.intent
    assert isinstance(intent, SupersededReviewRearm) and intent.reason is StopReason.REQUIRED_EFFECT_REJECTED
    assert dismissal in intent.retired_obligations
    assert set(state.intent.required_commands) == set(intent.verified_commands) | set(intent.retired_obligations)


def test_a_rejected_supersession_needs_the_stores_refusal_receipt(rig):
    """A caller cannot claim REQUIRED_EFFECT_REJECTED without a refused obligation."""
    state = rig.pending()
    from issue_agent.domain.review_workflow_policy import supersede_review_rearm
    observation, evidence_id = fresh_observation(rig)
    proposal = supersede_review_rearm(state, StopReason.REQUIRED_EFFECT_REJECTED, evidence_id,
                                      verified_commands=(), uncertainty=())
    with pytest.raises(WorkflowAuthorityError, match="refusal"):
        commit(rig, proposal)
    assert rig.workflow() == state


def test_a_forged_verified_set_cannot_complete(rig):
    state = rig.pending()
    rig.drain()
    from issue_agent.domain.review_workflow_policy import complete_review_rearm, supersede_review_rearm
    observation, evidence_id = fresh_observation(rig)
    rig.remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD)
    moved, moved_id = fresh_observation(rig)
    lying = supersede_review_rearm(state, StopReason.DECISION_CHANGED, moved_id, verified_commands=(), uncertainty=())
    with pytest.raises(WorkflowAuthorityError, match="successful required receipts"):
        commit(rig, lying)


def test_an_unavailable_read_backs_off_journals_and_gates_at_the_ceiling(rig):
    state = rig.pending()
    rig.points.add("read")
    gate_id = review_rearm_observation_gate_id("request:1")
    control = rig.control()
    for _ in range(40):
        rig.advance(3600)
        for due in control.pending_rearms():
            assert isinstance(due, PendingReviewRecovery)
            control.reconcile_rearm(due.snapshot.revision)
        if rig.gate(gate_id) is not None:
            break
    gate = rig.gate(gate_id)
    assert isinstance(gate.cause, EvidenceUnavailable) and gate.cause.failed_reads >= 1
    assert rig.journal("observation_deferred")
    assert isinstance(rig.workflow().intent, PendingReviewRearm)
    rig.points.clear()
    rig.drain()
    rig.advance(3600)
    finished = rig.reconcile()
    assert isinstance(finished.snapshot.intent, CompletedReviewRearm)
    assert rig.gate(gate_id) is None
    assert state.intent.request_id == finished.snapshot.intent.request_id


def test_a_rejection_during_a_pending_rearm_stops_it_visibly_and_regates(rig):
    """Re-rejection to the ceiling while the re-arm is pending is an explicit stop, then a new ceiling gate.

    Carried from increment 5: completion must not silently back off into a
    generic observation hold when the reviewer has re-rejected to the ceiling.
    """
    state = rig.pending()
    rig.drain()
    for index in range(3):  # the bot requests changes three more times
        later_review(rig, f"n{index}")
    stopped = rig.reconcile().snapshot
    assert isinstance(stopped.intent, SupersededReviewRearm)
    assert stopped.intent.reason is StopReason.CEILING_REACHED_AGAIN
    assert stopped.intent.request_id == "request:1"
    assert rig.gate(review_rearm_observation_gate_id("request:1")) is None
    regated = rig.control().reconcile_ceiling(PR_KEY)
    assert isinstance(regated, WorkflowCommitted)
    ceilings = [g for g in rig.workflow().gates if isinstance(g, OpenGate)
                and isinstance(g.cause, ReviewRejectionCeiling)]
    assert len(ceilings) == 1 and ceilings[0].gate_id != state.intent.gate_id
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewHeld)


# -- retirement closes every hold without an operator path ---------------------------------

def gate_prewrite(rig, command_id):
    """Drive one command's unavailable pre-write read to its visible evidence gate."""
    rig.points.add("reviews_page:0")
    for _ in range(200):
        rig.drain()
        rig.advance(60)
        if rig.gate(prewrite_evidence_gate_id(command_id)) is not None:
            break
    rig.points.discard("reviews_page:0")
    assert rig.gate(prewrite_evidence_gate_id(command_id)) is not None


def test_cancellation_closes_prewrite_gates_in_the_same_transaction(rig):
    state = rig.pending()
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    gate_prewrite(rig, dismissal)
    current = rig.workflow()
    cancelled = rig.control().cancel_rearm(CancelRearm("cancel:1", current.revision, "request:1", "stuck", expected_binding=current.binding), ACTOR)
    assert all(not str(getattr(g, "gate_id", "")).startswith("prewrite-evidence:") for g in cancelled.snapshot.gates)
    assert rig.store.prewrite_read(dismissal) is None
    assert rig.query("SELECT state FROM workflow_gates WHERE gate_id=?", prewrite_evidence_gate_id(dismissal))[0][0] == "closed"
    assert state.intent.request_id == "request:1"


def test_a_permanently_partial_read_never_holds_a_later_rearm(rig):
    rig.pending()
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    gate_prewrite(rig, dismissal)
    current = rig.workflow()
    stopped = rig.control().cancel_rearm(CancelRearm("cancel:1", current.revision, "request:1", "stuck", expected_binding=current.binding), ACTOR)
    continued = rig.control().continue_rearm(
        ContinueGrantedRearm("request:2", stopped.snapshot.revision, "request:1", expected_binding=stopped.snapshot.binding), ACTOR)
    assert isinstance(continued, WorkflowCommitted), continued
    assert all(not str(getattr(g, "gate_id", "")).startswith("prewrite-evidence:") for g in continued.snapshot.gates)
    rig.drain()
    assert isinstance(rig.reconcile().snapshot.intent, CompletedReviewRearm)


def test_a_generation_advance_retires_older_commands_and_ends_their_schedules(rig):
    """A standalone old-generation command can never begin: the advance retires it (typed, journaled)."""
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    escalation = rig.submit()
    rig.remote.soft_failures.add("get_pr")
    with pytest.raises(Exception, match="pre-write observation unavailable"):
        rig.verdict()
    (stuck,) = [c for c in rig.commands(CommandKind.SUBMIT_REVIEW) if c != escalation]
    for _ in range(200):
        rig.advance(60)
        rig.drain()
        if rig.gate(prewrite_evidence_gate_id(stuck)) is not None:
            break
    assert isinstance(rig.gate(prewrite_evidence_gate_id(stuck)).cause, EvidenceUnavailable)
    rig.remote.soft_failures.clear()
    assert isinstance(rig.control().open_ceiling(escalation), WorkflowCommitted)
    rig.remote.external_review_change("r0", state="DISMISSED")
    released = rig.control().reconcile_ceiling(PR_KEY)
    assert released.snapshot.revision.fence.generation == 1
    assert released.snapshot.gates == ()
    assert rig.store.prewrite_read(stuck) is None
    retired_state = rig.store.command_reconciliation(stuck)
    assert isinstance(retired_state, RetiredSettledCommand)
    assert rig.journal("stale_generation_retired")
    selected = rig.store.runnable_commands(repository="o/r", executor_identity=REVIEWER, diagnose=rig.logs.append)
    assert stuck not in {p.command.command_id for p in selected}  # never re-selected every tick
    rig.advance(3600)
    rig.drain()
    assert rig.gate(prewrite_evidence_gate_id(stuck)) is None and rig.remote.calls.count(
        ["submit_review", PR, "REQUEST_CHANGES", HEAD]) == 0
    assert rig.control().release_projection(PR_KEY).verified


def submit_calls(rig):
    return [call for call in rig.remote.calls if call[0] == "submit_review"]


def release_by_dismissal(rig):
    with rig.remote._connect() as db:
        rejections = [row[0] for row in db.execute("SELECT review_id FROM reviews WHERE state='CHANGES_REQUESTED'")]
    for review_id in rejections:  # a human clears every counted rejection
        rig.remote.external_review_change(review_id, state="DISMISSED")
    return rig.control().reconcile_ceiling(PR_KEY)


@pytest.mark.parametrize("outcome", ["absent", "landed", "expired-absent"])
def test_an_uncertain_older_generation_submission_is_cleared_by_a_read_not_a_grant(rig, outcome):
    """Probe J: an in-doubt verdict left behind an advance never becomes an ungrantable dead gate."""
    rig.gated()
    (rig.lost if outcome == "landed" else rig.points).add(
        "review_submitted" if outcome == "landed" else "review_requested")
    with pytest.raises((ConnectionError, TimeoutError)):
        rig.verdict()
    rig.points.clear()
    rig.lost.clear()
    (verdict,) = [c for c in rig.commands(CommandKind.SUBMIT_REVIEW) if rig.state(c) == "unknown_outcome"]
    cause = RemoteOutcomeUncertain
    if outcome == "expired-absent":  # already awaiting a grant when the generation advances
        expire_proven_absent(rig, verdict)
        cause = RetryAuthorizationRequired
    writes = len(submit_calls(rig))
    released = release_by_dismissal(rig)
    assert isinstance(released, WorkflowCommitted) and released.snapshot == rig.workflow()
    assert released.snapshot.revision.fence.generation == 1
    retired = rig.store.command_reconciliation(verdict)
    assert isinstance(retired, RetiredUncertainCommand)
    assert isinstance(rig.gate(retired.uncertainty.gate_id).cause, cause)
    assert rig.journal("stale_generation_retired")
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewHeld)
    assert rig.control().review_control_view(PR_KEY).retry_authorizable == frozenset()
    rig.points.add("reviews_page:0")  # unreadable for a while: the hold stays visible, nothing is re-written
    for _ in range(6):
        rig.advance(3600)
        rig.drain()
    assert rig.gate(retired.uncertainty.gate_id) is not None
    rig.points.clear()
    for _ in range(4):
        rig.advance(86400)
        rig.drain()
    assert isinstance(rig.store.command_reconciliation(verdict), RetiredSettledCommand)
    assert rig.workflow().gates == ()
    assert len(submit_calls(rig)) == writes  # settled by the read alone, never by a grant or a re-write
    assert rig.control().release_projection(PR_KEY).verified
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewOwed)


def absent_awaiting_reexecution(rig):
    """A lost verdict proven absent whose re-execution cannot begin: retryable, receipt archived."""
    rig.gated()
    rig.points.add("review_requested")
    with pytest.raises(ConnectionError):
        rig.verdict()
    rig.points.clear()
    (verdict,) = [c for c in rig.commands(CommandKind.SUBMIT_REVIEW) if rig.state(c) == "unknown_outcome"]
    rig.remote.soft_failures.add("get_pr")
    rig.advance(60)
    rig.drain()
    assert rig.receipt(verdict) is None and rig.state(verdict) == "retryable_failure"
    rig.remote.soft_failures.clear()
    return verdict


@pytest.mark.parametrize("advance", ["request", "release"])
def test_an_absent_submission_awaiting_reexecution_retires_at_every_advance(rig, advance):
    """Probe I: no receipt (archived by the absence proof) never makes an advance raise."""
    verdict = absent_awaiting_reexecution(rig)
    writes = len(submit_calls(rig))
    result = rig.request()[1] if advance == "request" else release_by_dismissal(rig)
    assert isinstance(result, WorkflowCommitted) and result.snapshot.revision.fence.generation == 1
    retired = rig.store.command_reconciliation(verdict)
    assert isinstance(retired, RetiredSettledCommand)
    outcome = json.loads(rig.query("SELECT payload_json FROM workflow_evidence WHERE evidence_id=?",
                                   retired.outcome_evidence_id)[0][0])
    assert outcome["observation"] == "proven_absent_never_settled" and outcome["prior_receipts"]
    assert rig.journal("stale_generation_retired")
    rig.advance(3600)
    rig.drain()
    assert len(submit_calls(rig)) == writes  # never re-executed under the old generation


def lost_verdict(rig, body):
    rig.points.add("review_requested")
    with pytest.raises(ConnectionError):
        rig.verdict(body)
    rig.points.clear()
    (first,) = [c for c in rig.commands(CommandKind.SUBMIT_REVIEW) if rig.state(c) == "unknown_outcome"]
    return first


def expire_proven_absent(rig, command_id):
    rig.points.add("reviews_page:0")
    for _ in range(24):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(command_id), ReconciliationOnly):
            break
    rig.points.clear()
    rig.advance(1800)
    rig.drain()
    gate = rig.gate(rig.store.command_reconciliation(command_id).gate_id)
    assert isinstance(gate.cause, RetryAuthorizationRequired)
    return gate


@pytest.mark.parametrize("recorded", [True, False])
def test_a_grant_for_a_verdict_superseded_by_a_landed_one_never_posts_a_second(rig, recorded):
    """Probe H4: lost A, landed B at the same head, A proven absent and granted: A must not post."""
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    first = lost_verdict(rig, "first verdict")
    second = rig.verdict("second verdict")
    assert rig.state(second) == "succeeded"
    if recorded:
        assert isinstance(rig.control().record_review(second), WorkflowCommitted)
    gate = expire_proven_absent(rig, first)
    current = rig.workflow()
    accepted = rig.control().authorize_retry(
        AuthorizeAbsentRetry("grant:1", current.revision, first, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=current.binding), ACTOR)
    assert isinstance(accepted, WorkflowCommitted)
    for _ in range(3):
        rig.advance(1800)
        rig.drain()
    assert len(submit_calls(rig)) == 1  # only B ever posted
    assert isinstance(rig.store.command_reconciliation(first), RetiredSettledCommand)
    assert rig.gate(gate.gate_id) is None  # the grant's decline cleared the hold: no dead state
    assert rig.query("SELECT disposition FROM workflow_retry_authorizations WHERE authorization_id='grant:1'"
                     )[0][0] == "inapplicable"
    # The retired verdict no longer holds dispatch; only the landed B decides what is owed,
    # and until B is applied to the workflow it is the one hold (never the retired A).
    decision = rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH)
    if recorded:
        assert not isinstance(decision, (SubmissionUnresolved, ReviewHeld))
    else:
        assert decision == SubmissionUnresolved((second,))


def test_a_granted_verdict_at_a_new_head_is_still_owed_after_an_earlier_head_was_reviewed(tmp_path):
    """Only a landed verdict for the same binding AND head supersedes a granted one."""
    rig = Rig(tmp_path, reviews=0)
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    earlier = rig.verdict("earlier head")
    assert isinstance(rig.control().record_review(earlier), WorkflowCommitted)
    rig.remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD)
    rig.points.add("review_requested")
    with pytest.raises(ConnectionError):
        rig.executor().submit_bound_review(PR, "REQUEST_CHANGES", "new head", OTHER_HEAD,
                                           workflow=rig.workflow().binding, reviewer=BOT, patch_id="e" * 64)
    rig.points.clear()
    (lost,) = [c for c in rig.commands(CommandKind.SUBMIT_REVIEW) if rig.state(c) == "unknown_outcome"]
    gate = expire_proven_absent(rig, lost)
    current = rig.workflow()
    assert isinstance(rig.control().authorize_retry(
        AuthorizeAbsentRetry("grant:1", current.revision, lost, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=current.binding), ACTOR),
        WorkflowCommitted)
    rig.drain()
    assert rig.state(lost) == "succeeded"
    assert [call[3] for call in submit_calls(rig)] == [HEAD, OTHER_HEAD]


def test_dispatch_waits_while_a_bound_submission_is_unresolved(rig):
    """Probe H4 (b): no second verdict is dispatched while the first is in doubt, pre- or post-absence."""
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    first = lost_verdict(rig, "first verdict")
    assert rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH) == SubmissionUnresolved((first,))
    rig.remote.soft_failures.add("get_pr")  # absence proven, re-execution cannot begin yet
    rig.advance(60)
    rig.drain()
    assert rig.state(first) == "retryable_failure"
    rig.remote.soft_failures.clear()
    assert rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH) == SubmissionUnresolved((first,))
    rig.advance(3600)
    rig.drain()
    assert rig.state(first) == "succeeded" and len(submit_calls(rig)) == 1
    # Landed but not yet applied: a second paid review would double-cover the head.
    assert rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH) == SubmissionUnresolved((first,))
    rig.control().record_review(first)
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewNotOwed)


def expire_absent(rig, command_id):
    """Drive one dismissal to reconciliation-only, then prove its absence (RetryAuthorizationRequired)."""
    rig.store.begin_command(command_id)
    rig.store.record_effect(EffectReceipt(command_id, REVIEWER, rig.clock[0], rig.clock[0],
                                          EffectStatus.UNKNOWN_OUTCOME, error="response lost"))
    rig.points.add("reviews_page:0")
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(command_id), ReconciliationOnly):
            break
    rig.points.discard("reviews_page:0")
    rig.advance(1800)
    rig.drain()
    state = rig.store.command_reconciliation(command_id)
    assert isinstance(state, ReconciliationOnly)
    gate = rig.gate(state.gate_id)
    assert isinstance(gate.cause, RetryAuthorizationRequired)
    return gate


def no_retry_holds(rig):
    return not any(isinstance(getattr(g, "cause", None), RetryAuthorizationRequired) for g in rig.workflow().gates)


def test_a_review_obligation_grant_is_offered_accepted_once_and_buys_one_write(rig):
    rig.pending()
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    gate = expire_absent(rig, dismissal)
    state = rig.workflow()
    view = rig.control().review_control_view(PR_KEY)
    assert view.retry_authorizable == frozenset({gate.gate_id})  # offered exactly when acceptance would admit it
    command = AuthorizeAbsentRetry("grant:1", state.revision, dismissal, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=state.binding)
    accepted = rig.control().authorize_retry(command, ACTOR)
    assert isinstance(accepted, WorkflowCommitted) and not accepted.idempotent
    assert accepted.snapshot.revision.version == state.revision.version + 1
    assert accepted.snapshot.revision.fence == state.revision.fence  # a version bump only
    assert rig.restart().authorize_retry(command, ACTOR).idempotent
    other = OperatorActor("operator:mallory", "dashboard")
    assert isinstance(rig.control().authorize_retry(command, other), WorkflowConflict)
    assert rig.control().review_control_view(PR_KEY).retry_authorizable == frozenset()  # one outstanding grant
    rig.drain()
    assert [call[2] for call in rig.remote.calls if call[0] == "dismiss_review"].count("r0") == 1
    assert rig.receipt(dismissal)[0] == "succeeded" and rig.gate(gate.gate_id) is None
    rig.advance(3600)
    rig.drain()
    assert [call[2] for call in rig.remote.calls if call[0] == "dismiss_review"].count("r0") == 1


def test_a_forged_or_mismatched_grant_is_refused_before_any_command_is_consulted(rig):
    rig.pending()
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    gate = expire_absent(rig, dismissal)
    state = rig.workflow()
    forged = AuthorizeAbsentRetry("grant:1", state.revision, dismissal, gate.gate_id, "absence:invented", expected_binding=state.binding)
    refused = rig.control().authorize_retry(forged, ACTOR)
    assert isinstance(refused, WorkflowRejected) and refused.code is WorkflowRejection.INVALID_SCOPE
    stale = AuthorizeAbsentRetry("grant:2", replace(state.revision, version=state.revision.version - 1), dismissal,
                                 gate.gate_id, gate.cause.absence_evidence_id, expected_binding=state.binding)
    assert isinstance(rig.control().authorize_retry(stale, ACTOR), WorkflowConflict)
    assert rig.query("SELECT COUNT(*) FROM workflow_retry_authorizations")[0][0] == 0
    proposal_state = rig.workflow()
    from issue_agent.domain.workflow_retry import authorize_absent_retry
    with rig.store.transaction() as db:
        facts = rig.control()._retry_facts(db, AuthorizeAbsentRetry(
            "grant:3", proposal_state.revision, dismissal, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=proposal_state.binding))
    direct = authorize_absent_retry(proposal_state, AuthorizeAbsentRetry(
        "grant:3", proposal_state.revision, dismissal, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=proposal_state.binding), ACTOR, facts)
    with pytest.raises(WorkflowAuthorityError, match="barrier-ordered"):
        commit(rig, direct)


def operator_command(rig, operation):
    if operation == "request_rearm":
        state = rig.gated()
        return RequestRearm("request:1", state.revision, rig.ceiling_gate(state).gate_id, KeepGuidance(),
                            expected_binding=state.binding)
    state = rig.pending()
    if operation == "cancel_rearm":
        return CancelRearm("cancel:1", state.revision, "request:1", "scope changed", expected_binding=state.binding)
    if operation == "continue_rearm":
        cancelled = rig.control().cancel_rearm(CancelRearm(
            "cancel:1", state.revision, "request:1", "scope changed", expected_binding=state.binding), ACTOR)
        state = cancelled.snapshot
        return ContinueGrantedRearm("request:2", state.revision, "request:1", expected_binding=state.binding)
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    gate = expire_absent(rig, dismissal)
    state = rig.workflow()
    return AuthorizeAbsentRetry("grant:1", state.revision, dismissal, gate.gate_id, gate.cause.absence_evidence_id,
                                expected_binding=state.binding)


@pytest.mark.parametrize("operation", ["request_rearm", "continue_rearm", "cancel_rearm", "authorize_retry"])
@pytest.mark.parametrize("binding_error", ["missing", "epoch", "episode"])
def test_review_operator_acceptance_requires_current_epoch_and_episode_before_reading(rig, operation, binding_error):
    command = operator_command(rig, operation)
    state = rig.workflow()
    bindings = {
        "missing": None,
        "epoch": replace(command.expected_binding, authority=replace(state.authority, epoch=2)),
        "episode": replace(command.expected_binding, episode_id="episode:another"),
    }
    command = replace(command, expected_binding=bindings[binding_error])
    reads = []

    class Counting:
        def get_pr(self, number, **kwargs):
            reads.append(number)
            return rig.remote.get_pr(number, **kwargs)

        def pull_request_reviews(self, pr):
            reads.append(pr)
            return rig.remote.pull_request_reviews(pr)

    result = getattr(rig.control(gateway=Counting()), operation)(command, ACTOR)
    assert isinstance(result, WorkflowConflict), result
    assert reads == []
    assert rig.workflow() == state


@pytest.mark.parametrize("operation", ["request_rearm", "continue_rearm", "cancel_rearm", "authorize_retry"])
def test_review_operator_replay_is_bound_to_the_original_activation(rig, operation):
    from issue_agent.adapters.workflow_rows import persist_workflow_snapshot

    command = operator_command(rig, operation)
    result = getattr(rig.control(), operation)(command, ACTOR)
    assert isinstance(result, WorkflowCommitted), result
    replay = getattr(rig.restart(), operation)(command, ACTOR)
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent
    # A restored/repointed fixture has the same revision and audit under a
    # different activation. Old operator requests cannot cross that boundary.
    state = rig.workflow()
    restored = replace(state, authority=replace(state.authority, epoch=2))
    with rig.store.transaction() as db:
        db.execute("UPDATE workflow_authority_epochs SET epoch=2 WHERE repository='o/r' AND family=?",
                   (WorkflowFamily.REVIEW_REJECTIONS.value,))
        persist_workflow_snapshot(db, restored)
    stale = getattr(rig.restart(), operation)(command, ACTOR)
    assert isinstance(stale, WorkflowConflict), stale
    assert rig.workflow() == restored


@pytest.mark.parametrize("binding_error", ["missing", "epoch", "episode"])
def test_a_direct_cancellation_commit_cannot_bypass_operator_binding(rig, binding_error):
    from issue_agent.domain.review_workflow_policy import cancel_review_rearm

    command = operator_command(rig, "cancel_rearm")
    state = rig.workflow()
    bindings = {
        "missing": None,
        "epoch": replace(state.binding, authority=replace(state.authority, epoch=2)),
        "episode": replace(state.binding, episode_id="episode:another"),
    }
    command = replace(command, expected_binding=bindings[binding_error])
    proposal = cancel_review_rearm(state, command, ACTOR, verified_commands=(), uncertainty=())
    result = commit(rig, proposal)
    assert isinstance(result, WorkflowConflict), result
    assert rig.workflow() == state


@pytest.mark.parametrize("binding_error", ["epoch", "episode"])
@pytest.mark.parametrize("operation", ["request_rearm", "continue_rearm", "authorize_retry"])
def test_review_operator_acceptance_rechecks_binding_after_its_remote_read(rig, operation, binding_error):
    from issue_agent.adapters.workflow_rows import persist_workflow_snapshot

    command = operator_command(rig, operation)
    state = rig.workflow()
    raced = []

    class Racing:
        def get_pr(self, number, **kwargs):
            if not raced:
                if binding_error == "epoch":
                    replaced = replace(state, authority=replace(state.authority, epoch=2))
                else:
                    replaced = replace(state, episode=replace(state.episode, episode_id="episode:another"))
                with rig.store.transaction() as db:
                    db.execute("UPDATE workflow_authority_epochs SET epoch=? WHERE repository='o/r' AND family=?",
                               (replaced.authority.epoch, WorkflowFamily.REVIEW_REJECTIONS.value))
                    persist_workflow_snapshot(db, replaced)
                raced.append(replaced)
            return rig.remote.get_pr(number, **kwargs)

        def pull_request_reviews(self, pr):
            return rig.remote.pull_request_reviews(pr)

    result = getattr(rig.control(gateway=Racing()), operation)(command, ACTOR)
    assert isinstance(result, WorkflowConflict), result
    assert rig.workflow() == raced[0]


@pytest.mark.parametrize("request_kind", ["replay", "reused", "stale"])
def test_retry_replay_and_revision_conflicts_do_not_read_github(rig, request_kind):
    command = operator_command(rig, "authorize_retry")
    if request_kind in ("replay", "reused"):
        assert isinstance(rig.control().authorize_retry(command, ACTOR), WorkflowCommitted)
    if request_kind == "reused":
        command = replace(command, evidence_id="evidence:another")
    elif request_kind == "stale":
        command = replace(command, expected=replace(command.expected, version=command.expected.version - 1))
    reads = []

    class Unavailable:
        def get_pr(self, number, **kwargs):
            reads.append(number)
            raise ConnectionError("GitHub unavailable")

        def pull_request_reviews(self, pr):
            reads.append(pr)
            raise ConnectionError("GitHub unavailable")

    result = rig.control(gateway=Unavailable()).authorize_retry(command, ACTOR)
    if request_kind == "replay":
        assert isinstance(result, WorkflowCommitted) and result.idempotent
    else:
        assert isinstance(result, WorkflowConflict)
    assert reads == []


def test_cancel_clears_a_proven_absent_obligation_hold(rig):
    rig.pending()
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    expire_absent(rig, dismissal)
    current = rig.workflow()
    cancelled = rig.control().cancel_rearm(CancelRearm("cancel:1", current.revision, "request:1", "stuck", expected_binding=current.binding), ACTOR)
    assert isinstance(cancelled.snapshot.intent, CancelledReviewRearm)
    assert no_retry_holds(rig)
    assert isinstance(rig.store.command_reconciliation(dismissal), RetiredSettledCommand)
    rig.advance(3600)
    rig.drain()
    assert rig.remote.review_state("r0") == "CHANGES_REQUESTED"  # never rewritten after retirement


def test_a_changed_guard_on_a_proven_absent_obligation_supersedes_as_rejected(rig):
    rig.pending()
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    expire_absent(rig, dismissal)
    rig.remote.external_review_change("r0", delete=True)  # the authorized review vanished
    result = rig.reconcile()
    intent = result.snapshot.intent
    assert isinstance(intent, SupersededReviewRearm) and intent.reason is StopReason.REQUIRED_EFFECT_REJECTED
    assert dismissal in intent.retired_obligations
    assert no_retry_holds(rig)


# -- the ceiling release and its projection ---------------------------------------------------

def released(rig):
    rig.gated()
    rig.remote.external_review_change("r0", state="DISMISSED")
    rig.remote.external_review_change("r1", state="DISMISSED")
    result = rig.control().reconcile_ceiling(PR_KEY)
    assert isinstance(result, WorkflowCommitted) and result.snapshot.revision.fence.generation == 1, result
    return result.snapshot


def pr_now(rig):
    observation, _ = fresh_observation(rig)
    return observation


def test_dispatch_is_held_until_the_release_projection_is_verified(rig):
    state = released(rig)
    assert state.gates == ()
    release = rig.control().release_projection(PR_KEY)
    assert release.status is ReleaseStatus.PENDING
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReleaseProjectionPending)
    rig.drain()
    assert rig.control().release_projection(PR_KEY).verified
    assert rig.remote.get_pr(PR).labels == frozenset({TRIGGER})
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewOwed)


def test_a_refused_release_opens_a_visible_gate_until_labels_are_fixed(rig):
    released(rig)
    rig.remote.external_pr_change(PR, labels=sorted({Label.NEEDS_HUMAN_REVIEW.value, Label.AI_BLOCKED.value}))
    rig.drain()
    release = rig.control().release_projection(PR_KEY)
    assert release.status is ReleaseStatus.REFUSED
    assert rig.control().reconcile_release(PR_KEY).status is ReleaseStatus.REFUSED
    gate = rig.gate(release_refused_gate_id(release.command_id))
    assert isinstance(gate.cause, ReviewProjectionRefused) and "hold labels changed" in gate.cause.reason
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewHeld)
    assert rig.restart().reconcile_release(PR_KEY).status is ReleaseStatus.REFUSED  # idempotent while unresolved
    # An operator restores the released labels on GitHub: a fresh read verifies the release.
    rig.remote.external_pr_change(PR, labels=[TRIGGER.value])
    assert rig.control().reconcile_release(PR_KEY).verified
    assert rig.gate(release_refused_gate_id(release.command_id)) is None
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewOwed)


def test_an_uncertain_release_surfaces_remote_outcome_uncertainty(rig):
    released(rig)
    rig.points.add("relabel_pr_requested")
    rig.drain()
    (release,) = [CommandId(row[0]) for row in rig.query(
        "SELECT command_id FROM lifecycle_commands WHERE idempotency_key LIKE 'workflow:release:%'")]
    assert rig.state(release) == "unknown_outcome"
    rig.points.add("read")
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(release), ReconciliationOnly):
            break
    state = rig.store.command_reconciliation(release)
    assert isinstance(rig.gate(state.gate_id).cause, RemoteOutcomeUncertain)
    assert rig.control().release_projection(PR_KEY).status is ReleaseStatus.PENDING
    assert isinstance(rig.control().dispatch_decision(_open_pr(rig), patch_id=PATCH), ReviewHeld)


def _open_pr(rig):
    from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState
    return ObservedPullRequest(PR_KEY, HEAD, PullRequestState.OPEN, frozenset({Label.NEEDS_HUMAN_REVIEW}),
                               CompleteReviews(()), rig.clock[0])


def test_a_release_requires_the_barriers_and_a_fresh_complete_read(rig):
    rig.gated()
    rig.remote.external_review_change("r0", state="DISMISSED")
    rig.points.add("reviews_page:0")
    refused = rig.control().reconcile_ceiling(PR_KEY)
    assert isinstance(refused, WorkflowRejected)
    assert rig.workflow().revision.fence.generation == 0


# -- the conflicting-uncertainty residual from increment 3 ------------------------------------

def test_a_resolved_conflicting_relabel_uncertainty_never_dead_ends_a_held_replacement(rig):
    """Increment-3 residual: a HOLD replacement planned around a direction-conflicting
    uncertain relabel must reach a decision once that uncertainty resolves."""
    state = rig.pending()
    projection = state.intent.projection.command_id
    assert state.intent.projection.remove is Label.NEEDS_HUMAN_REVIEW  # a release relabel
    rig.points.add("relabel_pr_requested")
    rig.drain()
    assert rig.state(projection) == "unknown_outcome"
    rig.points.discard("relabel_pr_requested")
    rig.remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD)
    stopped = rig.reconcile().snapshot
    assert isinstance(stopped.intent, SupersededReviewRearm)
    assert any(isinstance(getattr(g, "cause", None), RemoteOutcomeUncertain) for g in stopped.gates)
    # An external hold exists at continuation: the replacement plans a HOLD, which
    # conflicts with the stopped release relabel, so that uncertainty stays independent.
    rig.holds.append("ci is red")
    continued = rig.control().continue_rearm(ContinueGrantedRearm("request:2", stopped.revision, "request:1", expected_binding=stopped.binding), ACTOR)
    assert isinstance(continued, WorkflowCommitted), continued
    replacement = continued.snapshot.intent
    assert replacement.projection.remove is None and replacement.projection.add is Label.NEEDS_HUMAN_REVIEW
    # The external hold clears: a changed hold fact supersedes the HOLD plan at once.
    rig.holds.clear()
    changed = rig.reconcile().snapshot
    assert isinstance(changed.intent, SupersededReviewRearm) and changed.intent.reason is StopReason.DECISION_CHANGED
    # Continued while the first relabel is still uncertain: that conflicting
    # uncertainty is now the only hold, so the replacement plans a HOLD again.
    held = rig.control().continue_rearm(ContinueGrantedRearm("request:3", changed.revision, "request:2", expected_binding=changed.binding), ACTOR)
    assert isinstance(held, WorkflowCommitted), held
    assert held.snapshot.intent.projection.remove is None
    assert isinstance(rig.reconcile().snapshot.intent, PendingReviewRearm)  # still right while uncertain
    # The lost relabel's outcome is reconciled read-only: the uncertainty resolves.
    for _ in range(24):
        rig.advance(1800)
        rig.drain()
        if not any(isinstance(getattr(g, "cause", None), RemoteOutcomeUncertain) for g in rig.workflow().gates):
            break
    assert not any(isinstance(getattr(g, "cause", None), RemoteOutcomeUncertain) for g in rig.workflow().gates)
    # No hold remains, so the HOLD plan can never complete: it is a changed decision, not a dead end.
    outcome = rig.reconcile().snapshot
    assert isinstance(outcome.intent, SupersededReviewRearm), outcome.intent
    assert outcome.intent.reason is StopReason.DECISION_CHANGED
    again = rig.control().continue_rearm(ContinueGrantedRearm("request:4", outcome.revision, "request:3", expected_binding=outcome.binding), ACTOR)
    assert isinstance(again, WorkflowCommitted), again
    assert again.snapshot.intent.projection.add is TRIGGER
    rig.drain()
    done = rig.reconcile().snapshot
    assert isinstance(done.intent, CompletedReviewRearm), done
    assert rig.remote.get_pr(PR).labels == frozenset({TRIGGER})


# -- every commit path: crash before, restart after, exact replay -----------------------------

def _open(rig):
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    escalation = rig.submit()
    return lambda control: control.open_ceiling(escalation)


def _record(rig):
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    verdict = rig.verdict()
    return lambda control: control.record_review(verdict)


def _request(rig):
    rig.gated()
    state = rig.workflow()
    command = RequestRearm("request:1", state.revision, rig.ceiling_gate(state).gate_id, KeepGuidance(), expected_binding=state.binding)
    return lambda control: control.request_rearm(command, ACTOR)


def _cancel(rig):
    state = rig.pending()
    command = CancelRearm("cancel:1", state.revision, "request:1", "scope", expected_binding=state.binding)
    return lambda control: control.cancel_rearm(command, ACTOR)


def _complete(rig):
    state = rig.pending()
    rig.drain()
    return lambda control: control.reconcile_rearm(state.revision)


def _supersede(rig):
    state = rig.pending()
    rig.remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD)
    return lambda control: control.reconcile_rearm(state.revision)


def _release(rig):
    rig.gated()
    rig.remote.external_review_change("r0", state="DISMISSED")
    return lambda control: control.reconcile_ceiling(PR_KEY)


@pytest.mark.parametrize("prepare", [_open, _record, _request, _cancel, _complete, _supersede, _release])
def test_every_commit_path_is_atomic_across_a_crash_and_idempotent_after_restart(rig, monkeypatch, prepare):
    operate = prepare(rig)
    before = rig.workflow()
    tables = ("lifecycle_commands", "workflow_rearms", "workflow_gates", "dedupe_keys", "event_outbox")
    counts = {table: rig.query(f"SELECT COUNT(*) FROM {table}")[0][0] for table in tables}

    def crash(*args, **kwargs):
        raise RuntimeError("process died mid-commit")
    monkeypatch.setattr(review_control_module, "record_commit", crash)
    with pytest.raises(RuntimeError):
        operate(rig.control())
    monkeypatch.undo()
    assert rig.workflow() == before
    assert {table: rig.query(f"SELECT COUNT(*) FROM {table}")[0][0] for table in tables} == counts
    first = operate(rig.restart())
    assert isinstance(first, WorkflowCommitted) and not first.idempotent and first.snapshot != before, first
    after = rig.workflow()
    assert after == first.snapshot
    replay = operate(rig.restart())
    assert isinstance(replay, (WorkflowCommitted, WorkflowConflict))
    assert rig.workflow() == after  # a replay never moves authority


def test_an_exact_proposal_replay_is_idempotent_and_identity_reuse_conflicts(rig):
    rig.activate()
    stored = rig.control().ensure_observed_workflow(PR_KEY).snapshot
    escalation = rig.store.bound_review_submission(rig.submit())
    observation, evidence_id = fresh_observation(rig)
    proposal = open_review_ceiling(stored, observation, now=rig.clock[0], rule=CountingRule(BOT), ceiling=3,
                                   escalation=escalation, trigger_label=TRIGGER, executor_identity=REVIEWER,
                                   evidence_ids=(evidence_id,))
    assert not commit(rig, proposal).idempotent
    assert commit(rig, proposal).idempotent
    other = open_review_ceiling(stored, observation, now=rig.clock[0], rule=CountingRule(BOT), ceiling=2,
                                escalation=escalation, trigger_label=TRIGGER, executor_identity=REVIEWER,
                                   evidence_ids=(evidence_id,))
    assert isinstance(commit(rig, other), WorkflowConflict)


def test_a_commit_must_carry_exactly_its_captured_family_authority(rig):
    rig.gated()
    proposal = forged_request(rig)
    with pytest.raises(WorkflowAuthorityError, match="authority"):
        inner_commit(rig, proposal, ())
    with pytest.raises(WorkflowAuthorityError, match="authority"):
        inner_commit(rig, proposal, (replace(REVIEW, epoch=2),))


def test_a_retired_owner_cannot_commit(rig):
    rig.gated()
    proposal = forged_request(rig)
    with rig.store.transaction() as db:
        db.execute("INSERT INTO retired_owners VALUES (?, ?, 'o/r', 'review', ?)",
                   (REVIEWER, "o/r:review:new", NOW.isoformat()))
    from issue_agent.adapters.control_store import RetiredOwnerError
    with pytest.raises(RetiredOwnerError):
        inner_commit(rig, proposal)


def test_proposed_commands_must_belong_to_the_accepting_owner(rig):
    rig.gated()
    state = rig.workflow()
    observation, evidence_id = fresh_observation(rig)
    command = RequestRearm("request:x", state.revision, rig.ceiling_gate(state).gate_id, KeepGuidance(), expected_binding=state.binding)
    foreign = request_review_rearm(state, command, ACTOR, observation, now=rig.clock[0], active_reviews=(),
                                   evidence_id=evidence_id, trigger_label=TRIGGER, executor_identity="o/r:fix:test")
    with pytest.raises(WorkflowAuthorityError):
        inner_commit(rig, foreign)
    assert rig.workflow() == state


@pytest.mark.parametrize("operation", ["cancel", "reconcile", "release"])
def test_other_generation_moving_paths_take_the_barriers_in_order(rig, monkeypatch, operation):
    events = []
    real = review_control_module.deployment_effect_barrier

    @contextmanager
    def recording(path, repository, role):
        with real(path, repository, role):
            events.append(("acquire", role))
            yield
            events.append(("release", role))
    if operation == "release":
        rig.gated()
        rig.remote.external_review_change("r0", state="DISMISSED")
        monkeypatch.setattr(review_control_module, "deployment_effect_barrier", recording)
        result = rig.control().reconcile_ceiling(PR_KEY)
    else:
        state = rig.pending()
        monkeypatch.setattr(review_control_module, "deployment_effect_barrier", recording)
        if operation == "cancel":
            result = rig.control().cancel_rearm(CancelRearm("cancel:1", state.revision, "request:1", "x", expected_binding=state.binding), ACTOR)
        else:
            rig.remote.external_pr_change(PR, head_ref_oid=OTHER_HEAD)
            result = rig.reconcile()
    assert isinstance(result, WorkflowCommitted) and not result.idempotent
    assert events == [("acquire", "fix"), ("acquire", "review"), ("acquire", "steward"),
                      ("release", "steward"), ("release", "review"), ("release", "fix")]


def test_unreadable_pending_authority_is_reported_rescheduled_and_journaled(rig):
    rig.pending()
    with rig.store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-rearm-observation'")
    rig.advance(3600)
    (unavailable,) = rig.control().pending_rearms()
    assert unavailable.__class__.__name__ == "UnavailableResolveRearm"
    assert unavailable.request_id == "request:1" and unavailable.next_read_at > rig.clock[0]
    assert rig.control().pending_rearms() == ()  # backed off, not re-reported every tick
    assert rig.journal("authority_unavailable")


def test_a_proven_absent_workflow_owes_the_initial_review(rig):
    rig.activate()
    assert isinstance(rig.control().dispatch_decision(_open_pr(rig), patch_id=PATCH), ReviewOwed)
    assert rig.control().release_projection(PR_KEY) is None


def test_inspection_never_writes(rig):
    rig.pending()
    before = {table: rig.query(f"SELECT COUNT(*) FROM {table}")[0][0]
              for table in ("workflow_evidence", "dedupe_keys", "event_outbox", "workflow_gates")}
    control = rig.control()
    control.review_control_view(PR_KEY)
    control.load_workflow(PR_KEY)
    control.release_projection(PR_KEY)
    control.dispatch_decision(_open_pr(rig), patch_id=PATCH)
    assert before == {table: rig.query(f"SELECT COUNT(*) FROM {table}")[0][0] for table in before}


def test_a_stop_cannot_invent_command_uncertainty(rig):
    """A forged uncertainty gate would hold the workflow forever: only store receipts can claim one."""
    state = rig.pending()
    dismissal = state.intent.required_commands[0]
    from issue_agent.domain.review_workflow_policy import cancel_review_rearm
    forged = cancel_review_rearm(state, CancelRearm("cancel:1", state.revision, "request:1", "x", expected_binding=state.binding), ACTOR,
                                 verified_commands=(), uncertainty=(
                                     RemoteOutcomeUncertain(dismissal, rig.clock[0], 0, "invented"),))
    with pytest.raises(WorkflowAuthorityError, match="uncertainty"):
        commit(rig, forged)
    assert rig.workflow() == state


def test_observation_evidence_bound_to_a_command_is_not_a_pull_request_read(rig):
    rig.gated()
    proposal = forged_request(rig)
    evidence_id = proposal.audit.preflight.complete_reviews_evidence_id
    with rig.store.transaction() as db:
        escalation = db.execute("SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'").fetchone()[0]
        db.execute("UPDATE workflow_evidence SET command_id=? WHERE evidence_id=?", (escalation, evidence_id))
    with pytest.raises(WorkflowAuthorityError, match="durable harness"):
        inner_commit(rig, proposal)


# -- adversarial review fixes -------------------------------------------------------------------

def refused_release(rig):
    """A ceiling release whose relabel the hold guard refused, with its visible gate."""
    released(rig)
    rig.remote.external_pr_change(PR, labels=sorted({Label.NEEDS_HUMAN_REVIEW.value, Label.AI_BLOCKED.value}))
    rig.drain()
    release = rig.control().release_projection(PR_KEY)
    assert release.status is ReleaseStatus.REFUSED
    rig.control().reconcile_release(PR_KEY)
    gate = rig.gate(release_refused_gate_id(release.command_id))
    assert isinstance(gate.cause, ReviewProjectionRefused) and gate.cause.generation == 1
    assert "by hand" in gate.remediation.reason
    return release, gate


def test_a_refused_release_gate_is_never_stranded_by_a_later_rearm(rig):
    """Probe C: a re-arm supersedes the refused release; it is neither kept forever nor a second hold."""
    release, gate = refused_release(rig)
    for index in range(2):  # the bot rejects the released head again
        later_review(rig, f"n{index}")
    escalation = rig.submit()
    opened = rig.control().open_ceiling(escalation)
    assert isinstance(opened, WorkflowCommitted) and not opened.idempotent, opened
    assert rig.gate(gate.gate_id) is not None  # an observed gate change keeps it
    _, accepted = rig.request()
    intent = accepted.snapshot.intent
    assert isinstance(intent, PendingReviewRearm) and accepted.snapshot.revision.fence.generation == 2
    assert rig.gate(gate.gate_id) is None  # superseded by the re-arm's own projection
    assert intent.projection.remove is Label.NEEDS_HUMAN_REVIEW  # a release, not a HOLD counted against it
    rig.drain()
    done = rig.reconcile().snapshot
    assert isinstance(done.intent, CompletedReviewRearm) and done.gates == ()
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewOwed)


def test_a_refused_gate_from_an_earlier_generation_is_reconciled_by_its_own_command(rig):
    """A gate left by an older generation still closes on its own command's planned labels."""
    release, gate = refused_release(rig)
    state = rig.workflow()
    advanced = replace(state, revision=replace(state.revision, fence=replace(state.revision.fence, generation=2),
                                               version=state.revision.version + 1))
    from issue_agent.adapters.workflow_rows import persist_workflow_snapshot
    with rig.store.transaction() as db:
        persist_workflow_snapshot(db, advanced)
    assert rig.control().release_projection(PR_KEY) is None  # no release at the current generation
    assert rig.control().reconcile_release(PR_KEY) is None
    assert rig.gate(gate.gate_id) is not None  # labels still wrong: the hold stays visible
    rig.remote.external_pr_change(PR, labels=[TRIGGER.value])  # corrected by hand
    rig.control().reconcile_release(PR_KEY)
    assert rig.gate(gate.gate_id) is None
    assert rig.journal("verified_by_observation")


def test_a_terminal_pull_request_closes_a_refused_release_gate(rig):
    release, gate = refused_release(rig)
    rig.remote.external_pr_change(PR, state="CLOSED")
    assert rig.control().reconcile_release(PR_KEY).verified
    assert rig.gate(gate.gate_id) is None


def test_a_partial_read_never_closes_a_refused_release_gate(rig):
    release, gate = refused_release(rig)
    rig.remote.external_pr_change(PR, labels=[TRIGGER.value])
    rig.points.add("reviews_page:0")
    assert rig.control().reconcile_release(PR_KEY).status is ReleaseStatus.REFUSED
    assert rig.gate(gate.gate_id) is not None


def test_a_lost_bound_submission_is_granted_exactly_one_write_and_releases_dispatch(rig):
    """Probe H: bound SUBMIT_REVIEW unknown -> expiry -> proven absent -> grant -> one write -> verified."""
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    rig.lost.add("review_requested")
    with pytest.raises(TimeoutError):
        rig.verdict()
    (submission,) = rig.commands(CommandKind.SUBMIT_REVIEW)
    assert rig.state(submission) == "unknown_outcome"
    rig.points.add("reviews_page:0")
    for _ in range(24):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(submission), ReconciliationOnly):
            break
    rig.points.clear()
    rig.advance(1800)
    rig.drain()
    state = rig.store.command_reconciliation(submission)
    gate = rig.gate(state.gate_id)
    assert isinstance(gate.cause, RetryAuthorizationRequired)
    assert rig.remote.calls == []  # the lost request never landed and nothing re-wrote it
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewHeld)
    view = rig.control().review_control_view(PR_KEY)
    assert view.retry_authorizable == frozenset({gate.gate_id})
    current = rig.workflow()
    accepted = rig.control().authorize_retry(
        AuthorizeAbsentRetry("grant:1", current.revision, submission, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=current.binding),
        ACTOR)
    assert isinstance(accepted, WorkflowCommitted) and not accepted.idempotent
    rig.lost.clear()
    rig.drain()
    assert [call[0] for call in rig.remote.calls] == ["submit_review"]  # exactly one write
    assert rig.receipt(submission)[0] == "succeeded" and rig.gate(gate.gate_id) is None
    assert not isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewHeld)
    recorded = rig.control().record_review(submission)
    assert isinstance(recorded, WorkflowCommitted) and not recorded.idempotent
    assert isinstance(rig.control().dispatch_decision(pr_now(rig), patch_id=PATCH), ReviewNotOwed)
    rig.advance(3600)
    rig.drain()
    assert [call[0] for call in rig.remote.calls] == ["submit_review"]


@pytest.mark.parametrize("change", [dict(head_ref_oid=OTHER_HEAD), dict(state="CLOSED")])
def test_a_granted_submission_whose_guard_changed_is_inapplicable_and_never_written(rig, change):
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    rig.lost.add("review_requested")
    with pytest.raises(TimeoutError):
        rig.verdict()
    (submission,) = rig.commands(CommandKind.SUBMIT_REVIEW)
    rig.points.add("reviews_page:0")
    for _ in range(24):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(submission), ReconciliationOnly):
            break
    rig.points.clear()
    rig.advance(1800)
    rig.drain()
    gate = rig.gate(rig.store.command_reconciliation(submission).gate_id)
    current = rig.workflow()
    rig.control().authorize_retry(
        AuthorizeAbsentRetry("grant:1", current.revision, submission, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=current.binding),
        ACTOR)
    rig.lost.clear()
    rig.remote.external_pr_change(PR, **change)
    rig.drain()
    assert rig.remote.calls == []
    row = rig.query("SELECT disposition, disposition_reason FROM workflow_retry_authorizations")[0]
    assert row[0] == "inapplicable" and "guard no longer holds" in row[1]


def seed_pending_pr(rig, number, prefix):
    """A second PR's pending re-arm, driven through the same control."""
    from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey
    key = WorkflowKey("o/r", PullRequestRef(number))
    rig.remote.seed_pr(PRView(number, head_ref_oid=HEAD, labels=frozenset({Label.NEEDS_HUMAN_REVIEW})))
    for index in range(2):
        rig.remote.seed_review(number, f"{prefix}{index}", author=BOT, commit=HEAD,
                               submitted_at=f"2026-09-25T11:00:0{index}+00:00")
    control = rig.control()
    state = control.ensure_observed_workflow(key).snapshot
    before = {row[0] for row in rig.query("SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'")}
    assert rig.executor().submit_bound_review(number, "COMMENT", "escalating", HEAD, workflow=state.binding,
                                              reviewer=BOT, patch_id=PATCH, escalate_cause="ceiling")
    (escalation,) = {row[0] for row in rig.query(
        "SELECT command_id FROM lifecycle_commands WHERE kind='submit_review'")} - before
    gated_state = control.open_ceiling(CommandId(escalation)).snapshot
    gate = next(g for g in gated_state.gates if isinstance(g.cause, ReviewRejectionCeiling))
    pending = control.request_rearm(
        RequestRearm(f"request:{number}", gated_state.revision, gate.gate_id, KeepGuidance(), expected_binding=gated_state.binding), ACTOR)
    assert isinstance(pending.snapshot.intent, PendingReviewRearm), pending
    return key


_CORRUPTIONS = {
    # Each raises a different typed store error from inside PR 9's reconciliation.
    "authority": "DELETE FROM workflow_rearm_commands WHERE request_id='request:1' AND operation_key='dismiss:r0'",
    "store": "UPDATE lifecycle_commands SET payload_json='[]' WHERE kind='dismiss_review' AND target='pr:9'",
}


@pytest.mark.parametrize("corruption", sorted(_CORRUPTIONS))
def test_one_inconsistent_pull_request_never_stops_the_reconciliation_pass(rig, corruption):
    rig.pending()
    healthy = seed_pending_pr(rig, 10, "t")
    rig.drain()
    with rig.store.transaction() as db:
        db.execute(_CORRUPTIONS[corruption])
    rig.advance(3600)
    control = rig.control()
    assert control.reconcile_pending_rearms() == 1
    done = control.load_workflow(healthy)
    assert isinstance(done.intent, CompletedReviewRearm)
    assert isinstance(rig.workflow().intent, PendingReviewRearm)
    assert rig.journal("authority_unavailable")
    assert control.pending_rearms() == ()  # the broken PR backs off rather than failing every tick
    rig.advance(3600)
    (again,) = control.pending_rearms()
    assert again.__class__.__name__ == "PendingReviewRecovery"


def test_an_effect_rejected_stop_waits_for_a_complete_read(rig):
    rig.pending()
    rig.remote.deny_dismissals()
    rig.drain()
    rig.points.add("reviews_page:0")
    held = rig.reconcile().snapshot
    assert isinstance(held.intent, PendingReviewRearm)
    assert "complete review read" in rig.control().review_control_view(PR_KEY).observation.last_error
    rig.points.clear()
    rig.advance(3600)
    stopped = rig.reconcile().snapshot
    assert stopped.intent.reason is StopReason.REQUIRED_EFFECT_REJECTED


def test_an_effect_rejected_stop_cannot_cite_a_read_from_before_acceptance(rig):
    rig.gated()
    early_observation, early_id = fresh_observation(rig)
    rig.advance(10)
    _, accepted = rig.request()
    state = accepted.snapshot
    rig.remote.deny_dismissals()
    rig.drain()
    from issue_agent.domain.review_workflow_policy import supersede_review_rearm
    assert rig.receipt(rig.commands(CommandKind.DISMISS_REVIEW)[0])[0] == "permanent_failure"
    proposal = supersede_review_rearm(rig.workflow(), StopReason.REQUIRED_EFFECT_REJECTED, early_id,
                                      verified_commands=(), uncertainty=())
    with pytest.raises(WorkflowAuthorityError, match="before the re-arm was accepted"):
        commit(rig, proposal)
    assert rig.workflow().intent == state.intent
    assert early_observation.observed_at < rig.control().accepted_rearm(PR_KEY, "request:1").preflight.observed_at


def test_an_older_request_proposal_replayed_at_the_boundary_conflicts(rig):
    """`_replay`'s request clause: an exact older request receipt never re-applies."""
    rig.gated()
    proposal = forged_request(rig)
    first = inner_commit(rig, proposal)
    assert first.snapshot.intent.request_id == "request:forged"
    stopped = rig.control().cancel_rearm(
        CancelRearm("cancel:1", first.snapshot.revision, "request:forged", "x", expected_binding=first.snapshot.binding), ACTOR).snapshot
    rig.control().continue_rearm(ContinueGrantedRearm("request:2", stopped.revision, "request:forged", expected_binding=stopped.binding), ACTOR)
    current = rig.workflow()
    assert isinstance(inner_commit(rig, proposal), WorkflowConflict)
    assert rig.workflow() == current


@pytest.mark.parametrize("race_point", ["barrier", "remote_read"])
def test_a_concurrent_identical_acceptance_is_idempotent_under_the_barriers(rig, monkeypatch, race_point):
    """The second request replay inside the barriers: another acceptance won between the two checks."""
    state = rig.gated()
    command = RequestRearm("request:1", state.revision, rig.ceiling_gate(state).gate_id, KeepGuidance(), expected_binding=state.binding)
    raced = []

    def accept_competing_request():
        raced.append(True)
        observation, evidence_id = fresh_observation(rig)
        winner = request_review_rearm(
            rig.workflow(), command, ACTOR, observation, now=rig.clock[0], active_reviews=(),
            evidence_id=evidence_id, trigger_label=TRIGGER, executor_identity=REVIEWER)
        inner_commit(rig, winner)

    original_barriers = SQLiteReviewWorkflowControl._barriers

    @contextmanager
    def racing_barriers(control):
        with original_barriers(control):
            if race_point == "barrier" and not raced:
                accept_competing_request()
            yield

    monkeypatch.setattr(SQLiteReviewWorkflowControl, "_barriers", racing_barriers)

    class Racing:
        def get_pr(self, number, **kwargs):
            if race_point == "remote_read" and not raced:
                accept_competing_request()
            return rig.remote.get_pr(number, **kwargs)

        def pull_request_reviews(self, pr):
            return rig.remote.pull_request_reviews(pr)

    result = rig.control(gateway=Racing()).request_rearm(command, ACTOR)
    assert raced and isinstance(result, WorkflowCommitted) and result.idempotent, result
    assert rig.query("SELECT COUNT(*) FROM workflow_rearms")[0][0] == 1


def test_an_observed_transition_cites_exactly_one_observation(rig):
    rig.gated()
    first, first_id = fresh_observation(rig)
    rig.advance(1)
    later_review(rig, "n0")
    observation, second_id = fresh_observation(rig)
    state = rig.workflow()
    refreshed = reconcile_review_ceiling(state, observation, now=rig.clock[0], ceiling=3,
                                         evidence_ids=(second_id, first_id), trigger_label=TRIGGER,
                                         executor_identity=REVIEWER)
    assert refreshed.mutation is WorkflowMutation.OPEN_GATE
    with pytest.raises(WorkflowAuthorityError, match="exactly one observation"):
        commit(rig, refreshed)
    assert rig.workflow() == state and first.observed_at < observation.observed_at


def test_a_submission_receipt_without_its_matched_review_is_malformed(rig):
    rig.activate()
    rig.control().ensure_observed_workflow(PR_KEY)
    verdict = rig.verdict()
    with rig.store.transaction() as db:
        db.execute("UPDATE effect_receipts SET remote_object_id=NULL WHERE command_id=?", (str(verdict),))
    with pytest.raises(WorkflowAuthorityError, match="malformed"):
        rig.control().record_review(verdict)


# -- escalation operator signals and first-observation backoff (increment 6 pass 3) ----------------

SUCCESSOR = "o/r:review:successor"


def escalation_signals(rig) -> int:
    return rig.query("SELECT COUNT(*) FROM event_outbox WHERE event_json LIKE '%lifecycle.escalation.raised%'")[0][0]


def settled_escalation(rig) -> CommandId:
    """A ceiling escalation whose hold projection settled SUCCEEDED, then a crash before any signal claim."""
    rig.gated()
    return settle_projection(rig)


def settle_projection(rig) -> CommandId:
    (projection,) = [CommandId(row[0]) for row in rig.query(
        "SELECT command_id FROM lifecycle_commands WHERE idempotency_key LIKE 'workflow:escalation:%'")]
    rig.store.begin_command(projection)
    rig.store.record_effect(EffectReceipt(projection, REVIEWER, rig.clock[0], rig.clock[0], EffectStatus.SUCCEEDED))
    assert rig.state(projection) == "succeeded"
    return projection


def owe(rig, escalation, *, executor=REVIEWER, pr=PR, reason="hold_verified"):
    with rig.store.transaction() as db:
        owe_escalation_signal(db, executor_identity=executor, escalation=escalation, pr=pr, reason=reason,
                              at=rig.clock[0])


@pytest.mark.parametrize("retirements", [1, 2])
def test_a_retired_owners_unclaimed_escalation_signals_are_sent_once_by_its_successor(rig, retirements):
    settled_escalation(rig)
    owe(rig, "verdict-escalation", pr=11, reason="projection_exhausted")
    owners = [REVIEWER, *(f"{SUCCESSOR}-{n}" for n in range(retirements))]
    for prior, successor in zip(owners, owners[1:]):
        rig.store.adopt_legacy_owner("o/r", "review", successor, prior_owner_ids=(prior,))
    with pytest.raises(RetiredOwnerError):
        rig.store.claim_escalation_signals("o/r", REVIEWER)
    assert sorted(rig.store.claim_escalation_signals("o/r", owners[-1])) == [PR, 11]
    assert rig.store.claim_escalation_signals("o/r", owners[-1]) == ()
    assert escalation_signals(rig) == 2


@pytest.mark.parametrize("foreign", ["o/r:review:elsewhere", "o/r:fix:test"])
def test_another_executors_owed_signals_neither_reach_nor_starve_this_executor(rig, foreign):
    for index in range(40):
        owe(rig, f"a-foreign-{index:02}", executor=foreign, pr=100 + index)
    for index in range(3):
        owe(rig, f"b-own-{index}", pr=200 + index)
    assert sorted(rig.store.claim_escalation_signals("o/r", REVIEWER)) == [200, 201, 202]
    assert rig.store.claim_escalation_signals("o/r", REVIEWER) == ()
    assert escalation_signals(rig) == 3


def test_more_owed_signals_than_one_claim_holds_are_all_sent_in_bounded_batches(rig):
    for index in range(40):
        owe(rig, f"escalation-{index:02}", pr=100 + index)
    first = rig.store.claim_escalation_signals("o/r", REVIEWER)
    second = rig.store.claim_escalation_signals("o/r", REVIEWER)
    assert (len(first), len(second)) == (32, 8) and sorted(first + second) == list(range(100, 140))
    assert rig.store.claim_escalation_signals("o/r", REVIEWER) == () and escalation_signals(rig) == 40


def test_one_escalation_signals_once_across_its_projection_and_every_owed_record(rig):
    settled_escalation(rig)
    gate_id = rig.ceiling_gate().gate_id
    owe(rig, gate_id, reason="hold_verified")
    assert rig.store.claim_escalation_signals("o/r", REVIEWER) == (PR,)
    owe(rig, gate_id, reason="hold_contested")  # a later terminal of the same escalation
    assert rig.store.claim_escalation_signals("o/r", REVIEWER) == () and escalation_signals(rig) == 1


def malformed_signal(rig, payload, key="corrupt"):
    with rig.store.transaction() as db:
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   (ESCALATION_SIGNAL_OWED_NAMESPACE, REVIEWER, key, rig.clock[0].isoformat(), payload))


MALFORMED_SIGNALS = [
    "not json", "null", "[]", '"text"', "{}", '{"pr": 9}', '{"pr": "9", "reason": "hold_verified"}',
    '{"pr": 0, "reason": "hold_verified"}', '{"pr": true, "reason": "hold_verified"}',
    '{"pr": 9, "reason": "guessed"}', '{"pr": 9, "reason": []}',
]


@pytest.mark.parametrize("payload", MALFORMED_SIGNALS)
def test_a_malformed_owed_signal_is_quarantined_once_and_never_blocks_the_well_formed(rig, payload):
    rig.gated()
    gate_id = rig.ceiling_gate().gate_id
    malformed_signal(rig, payload)
    malformed_signal(rig, payload, key=gate_id)  # corrupt, under a real escalation's identity
    owe(rig, "verdict-escalation", pr=11)
    assert rig.store.claim_escalation_signals("o/r", REVIEWER) == (11,)
    quarantined = rig.journal("escalation_signal_malformed")
    assert {json.loads(row[0])["data"]["entity_id"] for row in quarantined} == {"corrupt", gate_id}
    # Quarantine is per record: that escalation's own settled hold is still signalled, and nothing is re-read.
    settle_projection(rig)
    assert rig.store.claim_escalation_signals("o/r", REVIEWER) == (PR,)
    assert rig.store.claim_escalation_signals("o/r", REVIEWER) == ()
    assert len(rig.journal("escalation_signal_malformed")) == 2 and escalation_signals(rig) == 2


def test_a_drain_past_a_malformed_owed_signal_completes_and_still_signals(rig):
    rig.gated()
    malformed_signal(rig, "not json")
    rig.drain()
    rig.drain()
    assert escalation_signals(rig) == 1 and len(rig.journal("escalation_signal_malformed")) == 1


@pytest.mark.parametrize("fields", [
    {"pr": 0}, {"pr": True}, {"pr": "9"}, {"reason": "guessed"}, {"escalation": ""}, {"executor_identity": ""},
])
def test_an_owed_signal_names_its_escalation_pull_request_and_reason(rig, fields):
    arguments = {"executor_identity": REVIEWER, "escalation": "e", "pr": PR, "reason": "hold_verified", **fields}
    with rig.store.transaction() as db, pytest.raises(ValueError):
        owe_escalation_signal(db, at=rig.clock[0], **arguments)


@pytest.mark.parametrize("limit", [0, 129, True, "8"])
def test_a_signal_claim_is_bounded(rig, limit):
    with pytest.raises(ValueError):
        rig.store.claim_escalation_signals("o/r", REVIEWER, limit=limit)


def test_a_first_read_failure_backs_off_each_pull_request_to_its_exact_boundary(rig):
    policy, control, other = ReconciliationPolicy(), rig.control(), WorkflowKey("o/r", PullRequestRef(PR + 1))
    assert control.first_observation_due(PR_KEY)
    rig.points.add("read")
    delays = []
    for _ in range(12):
        failed_at = rig.clock[0]
        assert isinstance(control.ensure_observed_workflow(PR_KEY), PullRequestUnavailable)
        assert not control.first_observation_due(PR_KEY) and control.first_observation_due(other)
        delay = 1
        while not control.first_observation_due(PR_KEY):
            rig.clock[0] = failed_at + timedelta(seconds=delay)
            delay += 1
        delays.append(delay - 1)
    # Doubling from the initial delay, never past the maximum; due exactly at the boundary, not a second early.
    assert delays[:3] == [policy.initial_delay_seconds * 2 ** n for n in range(3)]
    assert max(delays) == delays[-1] == policy.maximum_delay_seconds
    rig.points.discard("read")
    rig.activate()
    assert isinstance(control.ensure_observed_workflow(PR_KEY), WorkflowCommitted)
    assert control.first_observation_due(PR_KEY)  # a successful read ends the schedule


@pytest.mark.parametrize("payload", [
    "not json", "{}", '{"since": "x", "next": "y", "reads": 1, "surfaced": false}',
    '{"since": "2026-09-25T12:00:00", "next": "2026-09-25T12:00:30", "reads": 1, "surfaced": false}',
    '{"since": "2026-09-25T12:00:00+00:00", "next": "2026-09-25T12:00:30+00:00", "reads": "1", "surfaced": false}',
    '{"since": "2026-09-25T12:00:00+00:00", "next": "2026-09-25T12:00:30+00:00", "reads": 1, "surfaced": "no"}',
    '{"since": "2026-09-25T12:00:30+00:00", "next": "2026-09-25T12:00:00+00:00", "reads": 1, "surfaced": false}',
])
def test_a_malformed_first_observation_schedule_is_never_read_as_due(rig, payload):
    with rig.store.transaction() as db:
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   ("review-first-observation", "o/r", f"pr:{PR}", rig.clock[0].isoformat(), payload))
    with pytest.raises(WorkflowAuthorityError, match="malformed"):
        rig.control().first_observation_due(PR_KEY)


# -- the operator view's harness-owned facts (increment 7) -------------------------------------------

def test_an_inactive_review_family_beside_an_active_resolve_family_reads_migration_required(rig):
    from issue_agent.domain.review_rearm import MigrationRequiredReviewControl

    with rig.store.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r', ?, 1, 'test-source', 'test-inventory', ?)",
                   (WorkflowFamily.RESOLVE.value, NOW.isoformat()))
    before = rig.query("SELECT COUNT(*) FROM workflows")
    assert rig.control().review_control_view(PR_KEY) == MigrationRequiredReviewControl(
        PR_KEY, (WorkflowFamily.RESOLVE,))
    assert rig.query("SELECT COUNT(*) FROM workflows") == before


def test_the_view_stays_readable_after_a_stop_records_its_retirement_evidence(rig):
    state = rig.pending()
    rig.advance(60)
    cancelled = rig.control().cancel_rearm(CancelRearm("cancel:1", state.revision, "request:1", "stop", expected_binding=state.binding), ACTOR)
    assert isinstance(cancelled, WorkflowCommitted)
    view = rig.control().review_control_view(PR_KEY)
    assert isinstance(view, ReviewControlView), view
    assert view.decision is None  # no review plan has recorded a decision; the view never derives one


def test_pending_command_progress_follows_the_stores_receipts(rig):
    from issue_agent.domain.review_rearm import RearmCommandStatus

    state = rig.pending()
    view = rig.control().review_control_view(PR_KEY)
    assert [p.command_id for p in view.command_progress] == list(state.intent.required_commands)
    assert {p.status for p in view.command_progress} == {RearmCommandStatus.PENDING}
    rig.drain()
    drained = rig.control().review_control_view(PR_KEY)
    assert isinstance(drained.snapshot.intent, PendingReviewRearm)
    assert {p.status for p in drained.command_progress} == {RearmCommandStatus.VERIFIED}
    rig.reconcile()
    assert rig.control().review_control_view(PR_KEY).command_progress == ()


def test_an_accepted_grant_is_shown_waiting_and_is_never_offered_again(rig):
    from issue_agent.domain.resolve_rearm import PendingRetry

    rig.pending()
    dismissal = rig.commands(CommandKind.DISMISS_REVIEW)[0]
    gate = expire_absent(rig, dismissal)
    state = rig.workflow()
    command = AuthorizeAbsentRetry("grant:1", state.revision, dismissal, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=state.binding)
    assert isinstance(rig.control().authorize_retry(command, ACTOR), WorkflowCommitted)
    view = rig.control().review_control_view(PR_KEY)
    assert view.retry_pending == (PendingRetry(gate.gate_id, "grant:1", str(dismissal)),)
    assert view.retry_authorizable == frozenset()
    assert [refusal.gate_id for refusal in view.retry_refusals] == [gate.gate_id]
    progress = {p.command_id: p.status.value for p in view.command_progress}
    assert progress[dismissal] == "held"


def test_a_malformed_restoration_row_reads_unreadable_on_its_own_gate_not_a_guess(rig):
    from issue_agent.domain.review_rearm import UnreadableHoldRestoration

    rig.gated()
    gate = rig.ceiling_gate()
    with rig.store.transaction() as db:
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   ("review-hold-restores", "o/r", f"pr:{PR}:{gate.gate_id}", NOW.isoformat(), '{"restores": -1}'))
    view = rig.control().review_control_view(PR_KEY)
    assert isinstance(view, ReviewControlView), view
    (restoration,) = view.hold_restorations
    assert isinstance(restoration, UnreadableHoldRestoration) and restoration.gate_id == gate.gate_id
    assert view.gate_facts_unavailable == () and view.snapshot.gates == (gate,)


# -- the review daemon's recorded decision (ADR 0015 operator surface) -----------------------------------

def _basis(state=PullRequestState.OPEN, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}), holds=()):
    from issue_agent.domain.review_dispatch_record import DecisionBasis

    return DecisionBasis(HEAD, state, labels, holds)


def _listed():
    from issue_agent.domain.review_dispatch_record import DecisionRead

    return DecisionRead.REVIEW_READ


def _departed():
    from issue_agent.domain.review_dispatch_record import DecisionRead

    return DecisionRead.DEPARTED_READ


def _decision_rows(rig):
    return rig.query("SELECT key, claimed_at, payload_json FROM dedupe_keys WHERE namespace='review-dispatch-decision'")


def test_a_decision_is_recorded_only_for_an_observed_workflow_and_rewritten_only_on_change_or_due(rig):
    from issue_agent.domain.review_dispatch_record import DECISION_CONFIRM_SECONDS, ReviewNotAdmitted
    from issue_agent.domain.review_workflow_policy import ReviewKind

    rig.activate()
    held_out = ReviewNotAdmitted(("needs-human-review",))
    assert rig.control().record_dispatch_decision(PR_KEY, held_out, _basis(), _listed()) is False
    assert _decision_rows(rig) == []
    rig.control().ensure_observed_workflow(PR_KEY)
    assert rig.control().record_dispatch_decision(PR_KEY, held_out, _basis(), _listed()) is True
    first = rig.control().recorded_decision(PR_KEY)
    assert first.decision == held_out and first.decided_at == first.as_of == NOW
    rig.advance(DECISION_CONFIRM_SECONDS - 1)
    assert rig.control().record_dispatch_decision(PR_KEY, held_out, _basis(), _listed()) is False
    assert rig.control().recorded_decision(PR_KEY) == first
    rig.advance(1)
    assert rig.control().record_dispatch_decision(PR_KEY, held_out, _basis(), _listed()) is True
    confirmed = rig.control().recorded_decision(PR_KEY)
    assert confirmed.decided_at == NOW and confirmed.as_of == rig.clock[0]
    owed = ReviewOwed(ReviewKind.INITIAL)
    assert rig.control().record_dispatch_decision(PR_KEY, owed, _basis(labels=frozenset({TRIGGER})), _listed()) is True
    changed = rig.control().recorded_decision(PR_KEY)
    assert changed.decision == owed and changed.decided_at == rig.clock[0]
    (row,) = _decision_rows(rig)
    assert row[0] == f"pr:{PR}"


def test_the_view_renders_the_recorded_decision_and_a_malformed_one_reads_unreadable_not_unavailable(rig):
    from issue_agent.domain.review_dispatch_record import ReviewTerminal
    from issue_agent.domain.review_rearm import UnreadableReviewDecision

    rig.gated()
    closed = _basis(state=PullRequestState.CLOSED)
    rig.control().record_dispatch_decision(PR_KEY, ReviewTerminal(PullRequestState.CLOSED), closed, _listed())
    view = rig.control().review_control_view(PR_KEY)
    assert view.decision.decision == ReviewTerminal(PullRequestState.CLOSED) and view.decision.basis == closed
    with rig.store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='{\"format\":1,\"value\":7}' "
                   "WHERE namespace='review-dispatch-decision'")
    degraded = rig.control().review_control_view(PR_KEY)
    assert isinstance(degraded, ReviewControlView) and isinstance(degraded.decision, UnreadableReviewDecision)
    assert degraded.snapshot.gates == view.snapshot.gates
    rig.control().record_dispatch_decision(PR_KEY, ReviewTerminal(PullRequestState.CLOSED), closed, _listed())
    assert rig.control().review_control_view(PR_KEY).decision.decision == ReviewTerminal(PullRequestState.CLOSED)


def test_a_retired_owner_records_no_decision(rig):
    from issue_agent.domain.review_dispatch_record import ReviewNotAdmitted

    rig.gated()
    with rig.store.transaction() as db:
        db.execute("INSERT INTO retired_owners VALUES (?, ?, 'o/r', 'review', ?)",
                   (REVIEWER, "o/r:review:new", NOW.isoformat()))
    with pytest.raises(RetiredOwnerError):
        rig.control().record_dispatch_decision(PR_KEY, ReviewNotAdmitted(("needs-human-review",)), _basis(), _listed())
    assert _decision_rows(rig) == []


def test_departed_pull_requests_are_due_by_their_record_most_overdue_first_and_bounded(rig):
    from issue_agent.domain.review_dispatch_record import (
        DEPARTED_CONFIRM_SECONDS, ReviewNotAdmitted, ReviewTerminal,
    )

    rig.activate()
    numbers = (PR, 11, 12, 13)
    for number in numbers[1:]:
        rig.remote.seed_pr(PRView(number, head_ref_oid=HEAD, labels=frozenset({TRIGGER})))
    for number in numbers:
        rig.control().ensure_observed_workflow(WorkflowKey("o/r", PullRequestRef(number)))
    assert rig.control().departed_pull_requests(frozenset(), limit=64) == (PR, 11, 12, 13)  # all unrecorded
    assert rig.control().departed_pull_requests(frozenset({PR, 12}), limit=1) == (11,)
    key = lambda number: WorkflowKey("o/r", PullRequestRef(number))  # noqa: E731
    rig.control().record_dispatch_decision(key(11), ReviewNotAdmitted(("needs-human-review",)), _basis(), _departed())
    rig.control().record_dispatch_decision(key(12), ReviewTerminal(PullRequestState.MERGED),
                                           _basis(state=PullRequestState.MERGED), _departed())
    # Decided in the review read: stale as soon as it is missing from it, so due at once.
    rig.control().record_dispatch_decision(key(13), ReviewNotAdmitted(("needs-human-review",)), _basis(), _listed())
    assert rig.control().departed_pull_requests(frozenset(), limit=64) == (PR, 13)
    rig.advance(DEPARTED_CONFIRM_SECONDS)
    assert rig.control().departed_pull_requests(frozenset(), limit=64) == (PR, 13, 11)
    rig.advance(10**8)
    assert 12 not in rig.control().departed_pull_requests(frozenset(), limit=64)  # merged: never again
    with pytest.raises(ValueError):
        rig.control().departed_pull_requests(frozenset(), limit=0)


def test_a_closed_departure_is_re_read_on_the_short_cadence_while_its_workflow_holds_gates(rig):
    from issue_agent.domain.review_dispatch_record import (
        CLOSED_RECONFIRM_SECONDS, DEPARTED_CONFIRM_SECONDS, ReviewTerminal,
    )

    rig.gated()
    closed = _basis(state=PullRequestState.CLOSED)
    rig.control().record_dispatch_decision(PR_KEY, ReviewTerminal(PullRequestState.CLOSED), closed, _departed())
    assert rig.control().departed_pull_requests(frozenset(), limit=64) == ()
    rig.advance(DEPARTED_CONFIRM_SECONDS)
    assert rig.control().departed_pull_requests(frozenset(), limit=64) == (PR,)  # a reopen must find its lever
    ungated_key = WorkflowKey("o/r", PullRequestRef(11))
    rig.remote.seed_pr(PRView(11, head_ref_oid=HEAD, labels=frozenset({TRIGGER})))
    rig.control().ensure_observed_workflow(ungated_key)
    rig.control().record_dispatch_decision(ungated_key, ReviewTerminal(PullRequestState.CLOSED), closed, _departed())
    rig.advance(DEPARTED_CONFIRM_SECONDS)
    assert 11 not in rig.control().departed_pull_requests(frozenset(), limit=64)
    rig.advance(CLOSED_RECONFIRM_SECONDS - DEPARTED_CONFIRM_SECONDS)
    assert 11 in rig.control().departed_pull_requests(frozenset(), limit=64)


def test_failed_departed_reads_are_journalled_at_the_first_failure_and_at_the_backoff_ceiling(rig):
    from issue_agent.domain.review_dispatch_record import departed_backoff_seconds

    rig.gated()
    control = rig.control()
    for attempt in range(1, 9):
        assert control.record_departed_read_failure(PR_KEY, "Not Found") is True
        record = control.recorded_decision(PR_KEY)
        assert record.failed_reads == attempt and record.basis is None and record.decided_at == NOW
        # Not due again until its backoff has passed, then due exactly then.
        wait = departed_backoff_seconds(attempt)
        rig.advance(wait - 1)
        assert control.departed_pull_requests(frozenset(), limit=64) == ()
        rig.advance(1)
        assert control.departed_pull_requests(frozenset(), limit=64) == (PR,)
    events = [row[0] for row in rig.journal("review_departed_read")]
    assert len(events) == 2
    assert '"read_failed"' in events[0] and '"failed_reads":1' in events[0].replace(" ", "")
    assert '"read_backoff_ceiling"' in events[1] and '"failed_reads":6' in events[1].replace(" ", "")


def test_decisions_are_recorded_in_one_write_and_an_unrecordable_entry_never_drops_the_others(rig):
    from issue_agent.domain.review_dispatch_record import DecisionUpdate, DepartedReadFailed, ReviewTerminal
    from issue_agent.domain.review_workflow_policy import ReviewKind, ReviewOwed

    rig.gated()
    other = WorkflowKey("o/r", PullRequestRef(11))
    rig.remote.seed_pr(PRView(11, head_ref_oid=HEAD, labels=frozenset({TRIGGER})))
    rig.control().ensure_observed_workflow(other)
    unobserved = WorkflowKey("o/r", PullRequestRef(12))
    # A terminal decision from an open read is contradictory: that entry alone cannot be recorded.
    contradictory = DecisionUpdate(other, ReviewTerminal(PullRequestState.CLOSED), _basis(), _departed())
    before = rig.query("SELECT COUNT(*) FROM event_outbox")[0][0]
    result = rig.control().record_dispatch_decisions((
        DecisionUpdate(PR_KEY, ReviewOwed(ReviewKind.RE_REVIEW), _basis(), _listed()),
        contradictory,
        DepartedReadFailed(unobserved, "no workflow, so no view to show it"),
    ))
    assert result.written == 1 and len(result.skipped) == 1 and result.skipped[0].startswith("#11: ")
    assert rig.control().recorded_decision(PR_KEY).decision == ReviewOwed(ReviewKind.RE_REVIEW)
    assert rig.control().recorded_decision(other) is None and rig.control().recorded_decision(unobserved) is None
    assert rig.query("SELECT COUNT(*) FROM event_outbox")[0][0] == before  # no journal for plain decisions
    # An unchanged decision within its confirmation bound opens no write transaction at all.
    control = rig.control()
    writes = []
    real = control._control.transaction
    control._control.transaction = lambda: (writes.append(1), real())[1]
    unchanged = control.record_dispatch_decisions((DecisionUpdate(PR_KEY, ReviewOwed(ReviewKind.RE_REVIEW), _basis(),
                                                                  _listed()),))
    assert unchanged.written == 0 and writes == []


def test_a_failed_read_of_a_closed_departure_keeps_it_shown_closed(rig):
    from issue_agent.domain.review_dispatch_record import DEPARTED_CONFIRM_SECONDS, ReviewTerminal

    rig.gated()
    closed = _basis(state=PullRequestState.CLOSED)
    control = rig.control()
    control.record_dispatch_decision(PR_KEY, ReviewTerminal(PullRequestState.CLOSED), closed, _departed())
    rig.advance(DEPARTED_CONFIRM_SECONDS)
    assert control.record_departed_read_failure(PR_KEY, "502 Bad Gateway") is True
    record = control.recorded_decision(PR_KEY)
    assert record.decision == ReviewTerminal(PullRequestState.CLOSED) and record.basis == closed
    assert record.as_of == NOW and record.read_failure.reason == "502 Bad Gateway"
    events = [row[0] for row in rig.journal("review_departed_read")]
    assert len(events) == 1 and '"read_failed"' in events[0]


def test_active_workflow_families_are_read_exactly_as_the_legacy_unblock_refuses(rig):
    assert rig.store.active_workflow_families("o/r") == ()
    rig.activate()
    assert rig.store.active_workflow_families("o/r") == (WorkflowFamily.REVIEW_REJECTIONS,)
    with pytest.raises(WorkflowAuthorityError):
        with rig.store.legacy_resolve_authority("o/r"):
            pass


# -- one inconsistent fact degrades only its own gate (finding 6) ------------------------------------------

def test_duplicate_open_grants_on_one_gate_degrade_that_gate_and_leave_the_view_readable(rig):
    rig.pending()
    first, second = rig.commands(CommandKind.DISMISS_REVIEW)[:2]
    gate = expire_absent(rig, first)
    state = rig.workflow()
    command = AuthorizeAbsentRetry("grant:1", state.revision, first, gate.gate_id, gate.cause.absence_evidence_id, expected_binding=state.binding)
    assert isinstance(rig.control().authorize_retry(command, ACTOR), WorkflowCommitted)
    with rig.store.transaction() as db:
        db.execute("DROP TRIGGER IF EXISTS workflow_retry_grant_insert")
        row = dict(db.execute("SELECT * FROM workflow_retry_authorizations WHERE authorization_id='grant:1'").fetchone())
        row.update(authorization_id="grant:2", command_id=str(second))
        db.execute(f"INSERT INTO workflow_retry_authorizations({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                   tuple(row.values()))
    view = rig.control().review_control_view(PR_KEY)
    assert isinstance(view, ReviewControlView), view
    assert [item.gate_id for item in view.gate_facts_unavailable] == [gate.gate_id]
    assert "2 open retry grants" in view.gate_facts_unavailable[0].reason
    assert view.retry_pending == () and gate.gate_id not in view.retry_authorizable
    assert rig.workflow().gates == view.snapshot.gates  # nothing written; the daemon's authority is unchanged


def test_a_gate_whose_read_history_lags_its_command_degrades_alone(rig):
    import re

    from issue_agent.domain.review_rearm import RearmCommandStatus

    rig.pending()
    dismissal, other = rig.commands(CommandKind.DISMISS_REVIEW)[:2]
    rig.store.begin_command(dismissal)
    rig.store.record_effect(EffectReceipt(dismissal, REVIEWER, rig.clock[0], rig.clock[0],
                                          EffectStatus.UNKNOWN_OUTCOME, error="response lost"))
    rig.points.add("reviews_page:0")
    for _ in range(12):
        rig.advance(1800)
        rig.drain()
        if isinstance(rig.store.command_reconciliation(dismissal), ReconciliationOnly):
            break
    gate_id = rig.store.command_reconciliation(dismissal).gate_id
    with rig.store.transaction() as db:
        raw = db.execute("SELECT workflow_reconciliation_json FROM lifecycle_commands WHERE command_id=?",
                         (str(dismissal),)).fetchone()[0]
        lagging = re.sub(r'"failed_reads":(\d+)', lambda m: f'"failed_reads":{int(m.group(1)) + 1}', raw, count=1)
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                   (lagging, str(dismissal)))
    view = rig.control().review_control_view(PR_KEY)
    assert isinstance(view, ReviewControlView), view
    assert [item.gate_id for item in view.gate_facts_unavailable] == [gate_id]
    progress = {item.command_id: item.status for item in view.command_progress}
    assert progress[dismissal] is RearmCommandStatus.UNAVAILABLE
    assert progress[other] is not RearmCommandStatus.UNAVAILABLE
