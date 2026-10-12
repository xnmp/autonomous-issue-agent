"""Bound, generation-fenced SQLite review-rejection transitions; ADR 0015 review slice.

The review owner's control store commits every review-family transition in
one short `BEGIN IMMEDIATE`: it reads and decodes the stored row, requires the
proposal's prior to equal it, re-derives the proposal from facts it mints from
its own rows (bound SUBMIT_REVIEW receipts, durable pull request observations,
accepted operator audits) and persists workflow, intent, lifecycle commands
and outbox together. Operator acceptance and re-arm reconciliation take the
exclusive deployment barriers (fix, review), read GitHub read-only
through the review gateway with no SQL transaction open, and release every
barrier before any effect runner can execute; nothing here writes remotely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from ..domain.contracts import CommandId, CommandKind, EffectStatus
from ..domain.labels import Label
from ..domain.resolve_rearm import (
    PendingRetry, RearmObservation, UnavailableResolveRearm, defer_rearm_observation, schedule_rearm_observation,
)
from ..domain.review_dispatch_record import (
    DecisionBasis, DecisionEntry, DecisionRead, DecisionUpdate, DepartedReadFailed, RecordedDispatch,
    RecordedReviewDecision, departed_backoff_at_ceiling, departed_backoff_seconds, departed_read_after,
    departed_read_due, next_decision_record,
)
from ..domain.review_rearm import (
    AbsentReviewControl, GateFactsUnavailable, HoldRestoration, MigrationRequiredReviewControl,
    PendingReviewRecovery, RearmCommandProgress, RearmCommandStatus, ReleaseProjection, ReleaseStatus, RetryRefusal,
    ReviewControlRead, ReviewControlView, UnreadableHoldRestoration, UnreadableReviewDecision,
    decide_review_rearm_reconciliation, update_review_rearm_observation,
)
from ..domain.review_evidence import (
    EvidenceSchedule, ReviewEvidenceKind, evidence_due, evidence_gate_id, review_evidence_gate_kind,
    unavailable_evidence, unavailable_first_observation,
)
from ..domain.review_execution import (
    initialized_review_execution, BlockedReviewExecutionMigration,
    CurrentReviewExecution, CompletedReviewExecutionRound, freeze_current_review_generation,
    renew_current_review_execution,
)
from ..domain.review_rounds import (
    FinalizedReviewRound, ReviewParentAbandoned, ReviewPlanningOpen, ReviewPlanningSealed,
)
from ..domain.review_rejection import CountingRule, ReviewProjectionRefused, ReviewRejectionCeiling, ReviewVerdictHold
from ..domain.review_workflow import (
    CancelledReviewRearm, CompleteReviews, CompletedReviewRearm, ObservedPullRequest, PendingReviewRearm,
    PullRequestState, PullRequestUnavailable, RecordedPullRequest, ReviewRearmIntent, ReviewRearmPreflight,
    ReviewWorkflowRead, ReviewWorkflowSnapshot, SupersededReviewRearm, BoundReviewSubmission, ReviewedBaseline,
)
from ..domain.review_workflow_policy import (
    ESCALATION_PROJECTION_PREFIX, OBSERVATION_MAX_AGE_SECONDS, CeilingEscalation, EscalationHoldPresent,
    CeilingProductivityDecision, EscalationMoot, EscalationProjectionExhausted, EscalationReprojected,
    HoldContested, RestoreHold, ReviewDispatch,
    ReviewSubmissionReceipt, ReviewTransitionFacts, cancel_review_rearm, ceiling_hold_restoration,
    continue_review_rearm, escalation_projection_identity, initial_review_workflow, is_review_escalation_projection,
    open_review_ceiling, reconcile_review_ceiling, record_escalated_review, record_review, replan_escalation_hold,
    request_review_rearm, restorable_ceiling_gate_ids, review_dispatch_decision, review_release_command_id,
    validate_review_transition, verified_ceiling_holds,
)
from ..domain.workflow import (
    ActiveReconciliation, AuthorizeAbsentRetry, AuthorizedOperatorCommand, CancelRearm, ContinueGrantedRearm,
    HeldProjection, NoAuthorizedRemediation, ObservedTransition, OpenGate, OperatorActor, RearmingGate,
    ReconciliationOnly, ReconciliationPolicy, RemoteOutcomeUncertain, RequestRearm, RetiredSettledCommand,
    RequestReviewExecutionRenewal, RequestReviewVerdictRelease,
    RetiredUncertainCommand, RetryAuthorizationRequired, RetryDisposition, StopReason, WorkflowChanged,
    WorkflowCommitted, WorkflowConflict, WorkflowMigrationRequired, WorkflowMutation, WorkflowRejected,
    WorkflowRejection, WorkflowUnavailable, WorkflowUnchanged, WorkflowAbsent,
)
from ..domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision, WorkflowWriter,
    validate_workflow_identifier,
)
from ..domain.workflow_retry import RetryAuthorizationFacts, authorize_absent_retry, retry_authorization_refusal
from .control_store import (
    ControlStoreError, SQLiteControlStore, _command_from_row, _REVIEW_GENERATION_CANDIDATE, load_bound_review_submission,
    load_retry_authorization_facts, owe_escalation_signal,
)
from .deployment_authority import deployment_effect_barrier, deployment_role
from .review_effects import (
    PullRequestProjectionOp, ReviewCheck, ReviewReads, check_review_op, observe_pull_request,
    read_pull_request, review_family_op,
)
from .review_dispatch_holds import (
    RECORDED_REVIEW_NAMESPACE as _RECORDED_REVIEW_NAMESPACE,
    RELEASE_RESOLVED_NAMESPACE as _RELEASE_RESOLVED_NAMESPACE,
    SETTLED_SUBMISSION_NAMESPACE as _SETTLED_SUBMISSION_NAMESPACE,
    review_release_projection, review_submission_settled, unresolved_review_submissions,
)
from .workflow_codec import WorkflowEncodingError, decode_workflow, encode_workflow
from .workflow_commit import (
    accepted_operator_audit, accepted_rearm_audit,
    insert_pending_rearm, intent_state, owns_command, persist_retry_authorization, record_commit,
    retire_rearm_obligation,
    same_receipt, transition_dedupe_key, retry_request_replay, transition_receipt, update_rearm_state,
)
from .workflow_rows import (
    WorkflowAuthorityError, ReviewExecutionHeld, active_families, load_family_authority, load_review_workflow_row, load_workflow_read,
    persist_workflow_snapshot, read_review_submission_registration, workflow_run_scope_bounds, workflow_scope,
    REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE,
    insert_initialized_review_execution_origin, insert_workflow_component, encode_workflow_episodes,
    load_review_execution_import_decision,
    read_current_review_pending, REVIEW_EXECUTION_ORIGIN_NAMESPACE, _read_current_round_outcome,
)

# Deployment barriers are always taken in this order (ADR 0015 acceptance).
BARRIER_ROLES = ("fix", "review")
_OBSERVATION_NAMESPACE = "workflow-rearm-observation"
_HOLD_OBSERVATION_NAMESPACE = "review-hold-observation"
_FIRST_OBSERVATION_NAMESPACE = "review-first-observation"
# Content-addressed harness pull request reads (`RecordedPullRequest`).
_PR_OBSERVATION_PREFIX = "pr-observation:"
# Per open ceiling gate: how often the harness restored its hold, and whether
# the restorations were spent against an actor removing it (contested).
_HOLD_RESTORES_NAMESPACE = "review-hold-restores"
# A refused escalation projection is resolved exactly once: verified present,
# moot, re-planned, or exhausted into a visible gate (whose operator signal the
# executor's claim sends from its owed record).
_ESCALATION_RESOLVED_NAMESPACE = "review-escalation-resolved"
# Per pull request, upserted: the review daemon's latest dispatch decision
# (`RecordedReviewDecision`). Written only by the review plan through
# `record_dispatch_decision`; `claimed_at` is from when a pull request missing
# from the daemon's review read is due its own read (`departed_read_after`).
_DECISION_NAMESPACE = "review-dispatch-decision"
_NEVER_DUE = "9999-12-31T23:59:59.999999+00:00"
_EVIDENCE_NAMESPACES = {
    ReviewEvidenceKind.EXEMPTION_TRAILERS: "review-exemption-evidence",
    ReviewEvidenceKind.REVIEW_HISTORY: "review-history-evidence",
    ReviewEvidenceKind.LINKED_ISSUE: "review-linked-issue-evidence",
    ReviewEvidenceKind.RUNTIME_PREPARATION: "review-runtime-preparation-evidence",
}
_REVIEW_INTENTS = PendingReviewRearm | CompletedReviewRearm | SupersededReviewRearm | CancelledReviewRearm
_BARRIER_ORDERED = frozenset({
    WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM, WorkflowMutation.RELEASE_CEILING,
    WorkflowMutation.AUTHORIZE_RETRY,
})


def _no_external_holds(_observed: ObservedPullRequest, _view: object) -> tuple[str, ...]:
    return ()


def _reason(exc: BaseException) -> str:
    return (str(exc).replace("\x00", "") or type(exc).__name__)[:16384]


def _due_stamp(value: datetime | None) -> str:
    """A sortable UTC stamp: every writer uses this one format, so SQL can compare stamps as text."""
    return _NEVER_DUE if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def review_family_activated(control: SQLiteControlStore, repository: str) -> bool:
    """Whether a repository's review family is activated; read-only, never initializes it.

    Raises `WorkflowAuthorityError` when the authority is unreadable, so a
    caller holds rather than silently taking the legacy path.
    """
    if not isinstance(control, SQLiteControlStore):
        raise ValueError("review family activation is read from the control store")
    try:
        with closing(control._connect()) as db:
            db.execute("BEGIN")
            return load_family_authority(db, WorkflowFamily.REVIEW_REJECTIONS, repository) is not None
    except (sqlite3.Error, ControlStoreError, ValueError) as exc:
        raise WorkflowAuthorityError(f"review family activation is unreadable: {_reason(exc)[:512]}") from exc


def read_review_guidance(control: SQLiteControlStore, repository: str, pr: int) -> str | None:
    """A pull request's committed review-episode guidance, read-only for any role.

    None means the review family requires migration; callers must defer.
    An activated but unobserved
    workflow has no guidance (""). An unreadable authority raises
    `WorkflowAuthorityError`: a caller defers rather than guessing (ADR 0015).
    """
    if not isinstance(control, SQLiteControlStore):
        raise ValueError("review guidance is read from the control store")
    key = WorkflowKey(repository, PullRequestRef(pr))
    try:
        with closing(control._connect()) as db:
            db.execute("BEGIN")
            read = load_workflow_read(db, key)
    except (sqlite3.Error, ControlStoreError, ValueError) as exc:
        raise WorkflowAuthorityError(f"review guidance is unavailable: {_reason(exc)[:512]}") from exc
    if isinstance(read, WorkflowMigrationRequired):
        return None
    if isinstance(read, ReviewWorkflowSnapshot):
        return read.episode.guidance
    if isinstance(read, WorkflowAbsent):
        return ""
    raise WorkflowAuthorityError("review guidance belongs to another workflow family")


def release_refused_gate_id(command_id: CommandId) -> str:
    validate_workflow_identifier(command_id, "release projection command")
    return "review-projection-refused:" + hashlib.sha256(str(command_id).encode()).hexdigest()


@dataclass(frozen=True)
class DecisionsRecorded:
    """How many decision rows one recording wrote, and each entry it could not record, with why."""
    written: int
    skipped: tuple[str, ...]


class SQLiteReviewWorkflowControl:
    """The review owner's PR-scoped workflow authority (implements `ReviewWorkflowControl`)."""

    def __init__(
        self, control: SQLiteControlStore, writer: WorkflowWriter, *, gateway: object, ceiling: int,
        trigger_label: Label, rule: CountingRule,
        external_holds: Callable[[ObservedPullRequest, object], tuple[str, ...]] = _no_external_holds,
        policy: ReconciliationPolicy = ReconciliationPolicy(),
        runtime_preflight: Callable[[], str | None] | None = None,
        execution_ceiling: int = 0,
    ) -> None:
        if not isinstance(control, SQLiteControlStore) or not isinstance(writer, WorkflowWriter):
            raise ValueError("review workflow control requires the existing store and a typed writer")
        if writer.role != "review" or deployment_role(writer.repository, writer.owner_id) != writer.role:
            raise ValueError("review workflow authority requires the repository's review owner")
        if not isinstance(gateway, ReviewReads):
            raise ValueError("review workflow control requires a read-only pull request review gateway")
        if type(ceiling) is not int or not -(2**63) <= ceiling <= 2**63 - 1:
            raise ValueError("review rejection ceiling must be a bounded integer")
        if type(execution_ceiling) is not int or not 0 <= execution_ceiling <= 2**63 - 1:
            raise ValueError("review execution ceiling must be a bounded nonnegative integer")
        if not isinstance(trigger_label, Label) or trigger_label is Label.NEEDS_HUMAN_REVIEW:
            raise ValueError("review trigger must be a label other than the attention hold")
        if not isinstance(rule, CountingRule) or not isinstance(policy, ReconciliationPolicy) or not callable(external_holds):
            raise ValueError("review workflow control requires a counting rule, hold source and read policy")
        if runtime_preflight is not None and not callable(runtime_preflight):
            raise ValueError("the runtime preparation reader must be callable")
        self._control = control
        self._writer = writer
        self._gateway = gateway
        self._ceiling = ceiling
        self._execution_ceiling = execution_ceiling
        self._trigger = trigger_label
        self._rule = rule
        self._external_holds = external_holds
        self._policy = policy
        self._runtime_preflight = runtime_preflight

    @property
    def writer(self) -> WorkflowWriter:
        return self._writer

    # -- scope, clock, barriers and observations ----------------------------------------------

    def _scope(self, key: object) -> WorkflowKey:
        if (not isinstance(key, WorkflowKey) or key.repository != self._writer.repository
                or not isinstance(key.target, PullRequestRef)):
            raise WorkflowAuthorityError("workflow is outside the bound review writer scope")
        return key

    def _now(self) -> datetime:
        return self._control._now()

    @contextmanager
    def _barriers(self) -> Iterator[None]:
        """Exclusive fix and review barriers; no SQL transaction is held while waiting.

        Every effect runner holds its role's barrier shared, so acceptance
        waits for an executing effect to finish (or become durable
        uncertainty) and no effect can start until the barriers are released.
        """
        with ExitStack() as barriers:
            for role in BARRIER_ROLES:
                barriers.enter_context(deployment_effect_barrier(self._control.path, self._writer.repository, role))
            yield

    def _observe(self, key: WorkflowKey) -> RecordedPullRequest | PullRequestUnavailable:
        """One fresh read-only observation, stamped at its start on the store clock.

        The hold source sees the same read's pull request view, so a hold
        keyed by the PR's linked issue is derived from this read, not a second.
        """
        started = self._now()
        observed, view = read_pull_request(self._gateway, key, started, reviews=True)
        if isinstance(observed, PullRequestUnavailable):
            return observed
        try:
            return RecordedPullRequest(observed, tuple(self._external_holds(observed, view)))
        except (sqlite3.Error, ControlStoreError, WorkflowAuthorityError):
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable hold source is unavailable evidence
            return PullRequestUnavailable(key, started, f"external holds unavailable: {_reason(exc)[:480]}")

    def external_holds(self, observed: ObservedPullRequest, view: object) -> tuple[str, ...]:
        """The trusted holds a re-arm keeps, from one read's observation and pull request view; raises if unreadable."""
        if not isinstance(observed, ObservedPullRequest):
            raise ValueError("external holds are read from a present pull request observation")
        self._scope(observed.key)
        return tuple(self._external_holds(observed, view))

    @staticmethod
    def _evidence_id(recorded: RecordedPullRequest) -> str:
        return _PR_OBSERVATION_PREFIX + hashlib.sha256(encode_workflow(recorded).encode()).hexdigest()

    def _insert_evidence(self, db: sqlite3.Connection, key: WorkflowKey, recorded: RecordedPullRequest) -> str:
        if recorded.observation.key != key:
            raise WorkflowAuthorityError("observation belongs to another pull request")
        encoded = encode_workflow(recorded)
        evidence_id = self._evidence_id(recorded)
        existing = db.execute("SELECT payload_json FROM workflow_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if existing is not None:
            if existing["payload_json"] != encoded:
                raise WorkflowAuthorityError("observation evidence identity contains different facts")
            return evidence_id
        db.execute("INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                   (evidence_id, *workflow_scope(key), encoded))
        return evidence_id

    @staticmethod
    def _recorded(db: sqlite3.Connection, key: WorkflowKey, evidence_id: str) -> RecordedPullRequest:
        """A durable harness observation of this pull request; nothing else counts as one."""
        row = db.execute("SELECT * FROM workflow_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if row is None or row["kind"] != "observation" or row["command_id"] is not None or (
            row["repository"], row["target_kind"], row["target_number"]
        ) != workflow_scope(key):
            raise WorkflowAuthorityError("transition lacks its durable harness pull request observation")
        try:
            recorded = decode_workflow(row["payload_json"], RecordedPullRequest)
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("pull request observation evidence is malformed") from exc
        if recorded.observation.key != key:
            raise WorkflowAuthorityError("pull request observation belongs to another workflow")
        return recorded

    def _load(self, db: sqlite3.Connection, key: WorkflowKey) -> ReviewWorkflowSnapshot:
        self._control._assert_owner_active(db, self._writer.owner_id)
        prior = load_review_workflow_row(db, key)
        if prior is None:
            raise WorkflowAuthorityError("review workflow must be observed before mutation")
        return prior

    # -- reads ---------------------------------------------------------------------------------

    def load_workflow(self, key: WorkflowKey) -> ReviewWorkflowRead:
        self._scope(key)
        try:
            with closing(self._control._connect()) as db:
                db.execute("BEGIN")
                read = load_workflow_read(db, key)
        except sqlite3.OperationalError as exc:
            return WorkflowUnavailable(key, f"workflow read unavailable: {exc}")
        if not isinstance(read, (ReviewWorkflowSnapshot, WorkflowAbsent, WorkflowMigrationRequired)):
            raise WorkflowAuthorityError("a pull request key decoded a non-review workflow")
        return read

    def held_projection(self, key: WorkflowKey) -> HeldProjection:
        state = self.load_workflow(key)
        if not isinstance(state, ReviewWorkflowSnapshot):
            raise WorkflowAuthorityError("held projection requires available review workflow authority")
        return HeldProjection(state)

    def lookup_rearm(self, key: WorkflowKey, request_id: str) -> ReviewRearmIntent | None:
        self._scope(key)
        validate_workflow_identifier(request_id, "request")
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            load_review_workflow_row(db, key)
            return self._lookup_rearm(db, key, request_id)

    @staticmethod
    def _lookup_rearm(db: sqlite3.Connection, key: WorkflowKey, request_id: str) -> ReviewRearmIntent | None:
        row = db.execute(
            "SELECT * FROM workflow_rearms WHERE request_id=? AND repository=? AND target_kind=? AND target_number=?",
            (request_id, *workflow_scope(key)),
        ).fetchone()
        if row is None:
            return None
        intent = decode_workflow(row["intent_json"], _REVIEW_INTENTS)
        if (intent.request_id != request_id or intent.gate_id != row["gate_id"]
                or intent.to_generation != row["to_generation"] or row["state"] != intent_state(intent)):
            raise WorkflowAuthorityError("review re-arm history disagrees with its typed payload")
        return intent

    def accepted_rearm(self, key: WorkflowKey, request_id: str) -> AuthorizedOperatorCommand | None:
        self._scope(key)
        validate_workflow_identifier(request_id, "request")
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            load_review_workflow_row(db, key)
            return self._accepted_rearm(db, key, request_id)

    _accepted_rearm = staticmethod(accepted_rearm_audit)

    def _accepted_preflight(self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot) -> ReviewRearmPreflight:
        intent = state.intent
        assert isinstance(intent, PendingReviewRearm)
        audit = self._accepted_rearm(db, state.revision.fence.key, intent.request_id)
        if audit is None or not isinstance(audit.preflight, ReviewRearmPreflight):
            raise WorkflowAuthorityError("pending review re-arm lacks its accepted preflight")
        return audit.preflight

    def _rearm_observation(self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot) -> RearmObservation:
        intent = state.intent
        if not isinstance(intent, PendingReviewRearm):
            raise WorkflowAuthorityError("review re-arm observation requires a pending intent")
        row = db.execute(
            f"SELECT payload_json FROM dedupe_keys WHERE namespace='{_OBSERVATION_NAMESPACE}' AND scope=? AND key=?",
            (self._writer.repository, intent.request_id),
        ).fetchone()
        if row is None or row["payload_json"] is None:
            raise WorkflowAuthorityError("pending review re-arm lacks observation scheduling evidence")
        try:
            observation = decode_workflow(row["payload_json"], RearmObservation)
            PendingReviewRecovery(state, observation)
            return observation
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("invalid pending review re-arm observation") from exc

    def _release_projection(self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot) -> ReleaseProjection | None:
        return review_release_projection(db, self._writer, state)

    def release_projection(self, key: WorkflowKey) -> ReleaseProjection | None:
        """The harness-owned "release projection verified" fact that holds review dispatch."""
        self._scope(key)
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            state = load_review_workflow_row(db, key)
            return self._release_projection(db, state) if state is not None else None

    def dispatch_decision(self, pr: ObservedPullRequest, *, patch_id: str | None) -> ReviewDispatch:
        """Held beats owed; an unresolved submission or unverified ceiling release holds (ADR 0015)."""
        if not isinstance(pr, ObservedPullRequest):
            raise ValueError("review dispatch requires a present pull request observation")
        self._scope(pr.key)
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            read = load_workflow_read(db, pr.key)
            if isinstance(read, WorkflowMigrationRequired):
                raise WorkflowAuthorityError("review workflow migration is required")
            state = read if isinstance(read, ReviewWorkflowSnapshot) else None
            release = self._release_projection(db, state) if state is not None else None
            unresolved = self._unresolved_submissions(db, state.binding) if state is not None else ()
        pending = release.command_id if release is not None and not release.verified else None
        return review_dispatch_decision(state, pr, patch_id=patch_id, release_pending=pending,
                                        unresolved_submissions=unresolved)

    def _unresolved_submissions(self, db: sqlite3.Connection, binding: WorkflowBinding) -> tuple[CommandId, ...]:
        return unresolved_review_submissions(db, self._control, binding)

    def review_control_view(
        self, key: WorkflowKey, *, request_id: str | None = None,
    ) -> ReviewControlRead | WorkflowUnavailable:
        """One read snapshot; inspection never schedules recovery or initializes state.

        The workflow row and family authority are required. Auxiliary facts
        degrade where they belong: a malformed recorded decision reads as
        unreadable, and inconsistent grant, receipt or restoration rows mark
        only their own gate, so one bad row never hides every other gate.
        """
        self._scope(key)
        if request_id is not None:
            validate_workflow_identifier(request_id, "requested re-arm")
        try:
            with closing(self._control._connect()) as db:
                db.execute("BEGIN")
                self._control._assert_owner_active(db, self._writer.owner_id)
                authority = load_family_authority(db, WorkflowFamily.REVIEW_REJECTIONS, key.repository)
                if authority is None:
                    return MigrationRequiredReviewControl(key, active_families(db, key.repository))
                state = load_review_workflow_row(db, key)
                if state is None:
                    return AbsentReviewControl(key, authority)
                selected = request_id or (state.intent.request_id if state.intent is not None else None)
                requested = self._lookup_rearm(db, key, selected) if selected is not None else None
                observation = self._rearm_observation(db, state) if isinstance(state.intent, PendingReviewRearm) else None
                pending, unavailable = self._retry_pending(db, state)
                progress, unreadable = self._command_progress(db, state)
                unavailable.update((gate_id, reason) for gate_id, reason in unreadable.items() if gate_id not in unavailable)
                authorizable, refusals = self._retry_offers(db, state, frozenset(unavailable))
                live = restorable_ceiling_gate_ids(state)
                try:
                    decision: RecordedReviewDecision | UnreadableReviewDecision | None = self._decision(db, key)
                except WorkflowAuthorityError as exc:
                    decision = UnreadableReviewDecision(_reason(exc)[:512])
                verdicts: list[ReviewSubmissionReceipt] = []
                if isinstance(state.execution, CurrentReviewExecution):
                    selections: list[tuple[str | None, CommandId | None]] = [(None, None)]
                    selections.extend((gate.gate_id, gate.cause.submission_command_id) for gate in state.gates
                        if isinstance(gate, OpenGate) and isinstance(gate.cause, ReviewVerdictHold))
                    for gate_id, identity in selections:
                        try:
                            receipt = self._fixed_verdict_receipt(db, state, identity)
                            if receipt is not None and receipt not in verdicts:
                                verdicts.append(receipt)
                        except (WorkflowAuthorityError, ControlStoreError, ValueError, TypeError) as exc:
                            if gate_id is not None:
                                unavailable[gate_id] = _reason(exc)
                return ReviewControlView(
                    state, selected, requested, observation, self._release_projection(db, state), authorizable,
                    refusals, pending, decision, self._hold_restorations(db, key, live),
                    self._active_review_runs(db, key), progress,
                    tuple(GateFactsUnavailable(gate_id, reason[:512]) for gate_id, reason in sorted(unavailable.items())),
                    trigger_label=self._trigger,
                    execution_ceiling=self._execution_ceiling,
                    execution_episode_open=self._old_execution_episode_open(db, state)
                    if isinstance(state.execution, CurrentReviewExecution) else False,
                    confirmed_verdicts=tuple(verdicts),
                )
        except (sqlite3.Error, ValueError, TypeError) as exc:
            return WorkflowUnavailable(key, _reason(exc))

    def _hold_restorations(
        self, db: sqlite3.Connection, key: WorkflowKey, live: frozenset[str],
    ) -> tuple[HoldRestoration | UnreadableHoldRestoration, ...]:
        """Each open ceiling's restoration record; a malformed one reads unreadable on its own gate."""
        found: dict[str, HoldRestoration | UnreadableHoldRestoration] = {}
        for row in self._restore_records(db, key):
            gate_id = row["key"].split(":", 2)[2]
            if gate_id not in live:
                continue
            try:
                found[gate_id] = HoldRestoration(gate_id, *self._restore_count(row["payload_json"]))
            except (WorkflowAuthorityError, ValueError) as exc:
                found[gate_id] = UnreadableHoldRestoration(gate_id, _reason(exc)[:512])
        return tuple(found[gate_id] for gate_id in sorted(found))

    def _command_progress(
        self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot,
    ) -> tuple[tuple[RearmCommandProgress, ...], dict[str, str]]:
        """Each pending obligation's status from the store's own receipts and gates (never a remote read).

        Returns the gates whose command facts disagree too: an inconsistent
        obligation reads `unavailable` and marks the gate that covers it.
        """
        intent = state.intent
        if not isinstance(intent, PendingReviewRearm):
            return (), {}
        opened = [g.gate if isinstance(g, RearmingGate) else g for g in state.gates]
        gated = {cause.command_id: gate.gate_id for gate in opened
                 if isinstance(cause := gate.cause, (RemoteOutcomeUncertain, RetryAuthorizationRequired))}
        facts: dict[CommandId, str] = {}
        broken: dict[str, str] = {}
        try:
            rows = self._rearm_command_rows(db, intent)
        except WorkflowAuthorityError as exc:
            rows, reason = [], _reason(exc)
            facts = {command_id: "unavailable" for command_id in intent.required_commands}
            broken = {gated[command_id]: reason for command_id in intent.required_commands if command_id in gated}
        gates = {gate.gate_id: gate for gate in opened}
        for row in rows:
            command_id = CommandId(row["command_id"])
            try:
                outcome, uncertain = self._rearm_command_fact(db, state, gates, row, None)
            except (WorkflowAuthorityError, ControlStoreError, ValueError, TypeError) as exc:
                facts[command_id] = "unavailable"
                if command_id in gated:
                    broken.setdefault(gated[command_id], f"command {command_id} facts disagree: {_reason(exc)[:256]}")
                continue
            facts[command_id] = "uncertain" if outcome == "pending" and uncertain is not None else outcome
        return tuple(RearmCommandProgress(command_id, (
            RearmCommandStatus.UNAVAILABLE if facts.get(command_id) == "unavailable"
            or gated.get(command_id) in broken else
            RearmCommandStatus.VERIFIED if facts.get(command_id) == "verified" else
            RearmCommandStatus.REFUSED if facts.get(command_id) == "rejected" else
            RearmCommandStatus.HELD if command_id in gated else
            RearmCommandStatus.UNCERTAIN if facts.get(command_id) == "uncertain" else
            RearmCommandStatus.PENDING)) for command_id in intent.required_commands), broken

    @staticmethod
    def _retry_pending(
        db: sqlite3.Connection, snapshot: ReviewWorkflowSnapshot,
    ) -> tuple[tuple[PendingRetry, ...], dict[str, str]]:
        """Open grants awaiting the executor, one per retry gate; a gate whose grants disagree is unavailable."""
        commands = {g.gate_id: g.cause.command_id for g in snapshot.gates
                    if isinstance(g, OpenGate) and isinstance(g.cause, RetryAuthorizationRequired)}
        visible = {(g.gate if isinstance(g, RearmingGate) else g).gate_id for g in snapshot.gates}
        grants: dict[str, list[sqlite3.Row]] = {}
        for row in db.execute(
            "SELECT g.authorization_id, g.gate_id, g.command_id FROM workflow_retry_authorizations g "
            "JOIN workflow_gates w USING(gate_id) WHERE g.disposition IS NULL "
            "AND w.repository=? AND w.target_kind=? AND w.target_number=? ORDER BY g.gate_id, g.authorization_id",
            workflow_scope(snapshot.revision.fence.key),
        ):
            if row["gate_id"] in visible:
                grants.setdefault(row["gate_id"], []).append(row)
        pending: list[PendingRetry] = []
        unavailable: dict[str, str] = {}
        for gate_id, rows in grants.items():
            if len(rows) > 1:
                unavailable[gate_id] = f"{len(rows)} open retry grants name this gate; at most one may"
            elif gate_id not in commands or str(commands[gate_id]) != rows[0]["command_id"]:
                unavailable[gate_id] = "an open retry grant names a command this gate does not hold"
            else:
                pending.append(PendingRetry(gate_id, rows[0]["authorization_id"], rows[0]["command_id"]))
        return tuple(pending), unavailable

    # -- the review daemon's recorded dispatch decision -------------------------------------------

    def _decision(self, db: sqlite3.Connection, key: WorkflowKey) -> RecordedReviewDecision | None:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                         (_DECISION_NAMESPACE, key.repository, f"pr:{key.target.number}")).fetchone()
        if row is None:
            return None
        try:
            record = decode_workflow(row["payload_json"], RecordedReviewDecision)
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError(f"recorded review decision is malformed: {_reason(exc)[:256]}") from exc
        if record.key != key:
            raise WorkflowAuthorityError("recorded review decision belongs to another pull request")
        return record

    def record_dispatch_decision(
        self, key: WorkflowKey, decision: RecordedDispatch, basis: DecisionBasis | None, read: DecisionRead,
    ) -> bool:
        """Record one decision (`record_dispatch_decisions`); True when a row was written."""
        return self.record_dispatch_decisions((DecisionUpdate(key, decision, basis, read),)).written == 1

    def record_departed_read_failure(self, key: WorkflowKey, reason: str) -> bool:
        """Record one more failed departed read (`record_dispatch_decisions`); True when written."""
        return self.record_dispatch_decisions((DepartedReadFailed(key, reason),)).written == 1

    def record_dispatch_decisions(self, entries: tuple[DecisionEntry, ...]) -> DecisionsRecorded:
        """Record the review plan's decisions in at most one write transaction.

        Harness-owned: only the review daemon's plan calls this, as the active
        review owner, from its own reads. A changed decision is written at
        once and an unchanged one refreshes its as-of time on a bounded
        cadence (`record_review_decision`). A failed departed read backs the
        next one off exponentially, journalled at the first failure and when
        the backoff tops out (ADR 0012). A pull request with no workflow has
        no view to show it, so nothing is recorded; a malformed prior record is
        replaced. An entry that cannot be recorded is skipped and reported,
        never a reason to drop the others.
        """
        if not isinstance(entries, tuple) or any(not isinstance(e, (DecisionUpdate, DepartedReadFailed))
                                                 for e in entries):
            raise ValueError("decision entries must be a tuple of typed entries")
        for entry in entries:
            self._scope(entry.key)
        now = self._now()
        pending: list[DecisionEntry] = []
        skipped: list[str] = []
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            for entry in entries:
                record, _, error = self._decision_change(db, entry, now)
                if error:
                    skipped.append(f"#{entry.key.target.number}: {error}"[:512])
                elif record is not None:
                    pending.append(entry)
        if not pending:
            return DecisionsRecorded(0, tuple(skipped))
        written = 0
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            for entry in pending:
                record, workflow, error = self._decision_change(db, entry, now)
                if error:
                    skipped.append(f"#{entry.key.target.number}: {error}"[:512])
                if record is None or workflow is None:
                    continue
                key = entry.key
                # Written with the gates as they stand: a gate opening on a departed,
                # closed pull request (practically never) waits at most the closed cadence.
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(namespace,scope,key) DO UPDATE SET claimed_at=excluded.claimed_at,"
                    "payload_json=excluded.payload_json",
                    (_DECISION_NAMESPACE, key.repository, f"pr:{key.target.number}",
                     _due_stamp(departed_read_after(record, holds_gates=bool(workflow.gates))),
                     encode_workflow(record)),
                )
                written += 1
                failure = record.read_failure
                if isinstance(entry, DepartedReadFailed) and failure is not None and (
                    failure.failed_reads == 1 or departed_backoff_at_ceiling(failure.failed_reads)
                ):
                    self._control._transition(
                        db, "review_departed_read", f"pr:{key.target.number}",
                        "read_failed" if failure.failed_reads == 1 else "read_backoff_ceiling", {
                            "failed_reads": failure.failed_reads, "since": failure.first_failed_at.isoformat(),
                            "next_read_seconds": departed_backoff_seconds(failure.failed_reads),
                            "reason": failure.reason[:512],
                        })
        return DecisionsRecorded(written, tuple(skipped))

    def journal_decision_record_failure(self, detail: str, suppressed: int) -> None:
        """Journal that the plan's decisions could not be recorded; the caller bounds how often (ADR 0012)."""
        if type(suppressed) is not int or suppressed < 0:
            raise ValueError("suppressed failures are a non-negative count")
        with self._control.transaction() as db:
            self._control._transition(db, "review_dispatch_record", self._writer.repository, "record_failed",
                                      {"detail": str(detail)[:512], "suppressed": suppressed})

    def _decision_change(
        self, db: sqlite3.Connection, entry: DecisionEntry, now: datetime,
    ) -> tuple[RecordedReviewDecision | None, ReviewWorkflowSnapshot | None, str]:
        """The record one entry would write, its workflow, and why it cannot be recorded (empty when it can)."""
        try:
            workflow = load_review_workflow_row(db, entry.key)
            if workflow is None:
                return None, None, ""
            return next_decision_record(self._decision_or_none(db, entry.key), entry, now), workflow, ""
        except (WorkflowAuthorityError, ValueError, TypeError) as exc:
            return None, None, _reason(exc)

    def _decision_or_none(self, db: sqlite3.Connection, key: WorkflowKey) -> RecordedReviewDecision | None:
        """A malformed prior record is replaced by the next decision, never a reason to record nothing."""
        try:
            return self._decision(db, key)
        except WorkflowAuthorityError:
            return None

    def recorded_decision(self, key: WorkflowKey) -> RecordedReviewDecision | None:
        """The review daemon's latest recorded decision for this pull request; raises when it is malformed."""
        self._scope(key)
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            return self._decision(db, key)

    def departed_pull_requests(self, present: frozenset[int], *, limit: int) -> tuple[int, ...]:
        """Observed pull requests missing from the daemon's review read whose own read is due, most overdue first.

        `present` is every pull request number the daemon's read returned (any
        state). The stored due stamp prefilters; the decoded record decides.
        """
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("departed pull request reads per pass must be between 1 and 64")
        now = self._now()
        due: list[int] = []
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT w.target_number AS number FROM workflows w LEFT JOIN dedupe_keys d ON d.namespace=? "
                "AND d.scope=w.repository AND d.key='pr:'||w.target_number WHERE w.repository=? "
                "AND w.target_kind='pr' AND (d.key IS NULL OR d.claimed_at <= ?) "
                "ORDER BY d.key IS NOT NULL, d.claimed_at, w.target_number",
                (_DECISION_NAMESPACE, self._writer.repository, _due_stamp(now)),
            )
            for row in rows:
                number = row["number"]
                if number in present:
                    continue
                key = WorkflowKey(self._writer.repository, PullRequestRef(number))
                record = self._decision_or_none(db, key)
                try:
                    workflow = load_review_workflow_row(db, key)
                    holds_gates = workflow is None or bool(workflow.gates)
                except WorkflowAuthorityError:
                    holds_gates = True  # unreadable: read sooner, never later
                if departed_read_due(record, now, holds_gates=holds_gates):
                    due.append(number)
                    if len(due) == limit:
                        break
        return tuple(due)

    # -- discovery -----------------------------------------------------------------------------

    def ensure_observed_workflow(self, key: WorkflowKey) -> WorkflowCommitted | PullRequestUnavailable:
        """Create the initial workflow of a present pull request; never initializes a family.

        An unavailable first read backs off on its own schedule
        (`first_observation_due`), journalled when it starts and once at its
        ceiling; a later read that succeeds ends it.
        """
        self._scope(key)
        recorded = self._observe(key)
        if isinstance(recorded, PullRequestUnavailable):
            self._first_observation_failed(key, recorded.reason or "pull request read unavailable")
            return recorded
        with self._control.transaction() as db:
            db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                       (_FIRST_OBSERVATION_NAMESPACE, self._writer.repository, f"pr:{key.target.number}"))
            self._control._assert_owner_active(db, self._writer.owner_id)
            authority = load_family_authority(db, WorkflowFamily.REVIEW_REJECTIONS, key.repository)
            if authority is None:
                raise WorkflowAuthorityError("review workflow migration is required")
            prior = load_review_workflow_row(db, key)
            if prior is not None:
                from .continuation_activation import retain_deferred_pr_repair
                after = retain_deferred_pr_repair(db, prior)
                if after != prior:
                    after = replace(after, revision=replace(prior.revision, version=prior.revision.version + 1))
                    persist_workflow_snapshot(db, after)
                    self._control._transition(db, "workflow", encode_workflow(key), "migration_hold_installed",
                                              {"family": "pr_repair", "version": after.revision.version})
                    return WorkflowCommitted(after)
                return WorkflowCommitted(prior, idempotent=True)
            execution_authority = load_family_authority(db, WorkflowFamily.REVIEW_EXECUTION, key.repository)
            snapshot = initial_review_workflow(key, authority)
            origin_receipt = None
            origin_claimed_at = None
            retained_decision = load_review_execution_import_decision(db, key)
            if isinstance(retained_decision, BlockedReviewExecutionMigration):
                snapshot = replace(snapshot, execution=retained_decision)
            elif retained_decision is not None:
                raise WorkflowAuthorityError("imported review target lost its workflow")
            elif execution_authority is not None:
                origin_claimed_at = self._now()
                if not isinstance(recorded.observation.reviews, CompleteReviews):
                    raise WorkflowAuthorityError("execution initialization requires a complete first observation")
                age = (origin_claimed_at - recorded.observation.observed_at).total_seconds()
                if age < 0 or age > OBSERVATION_MAX_AGE_SECONDS:
                    raise WorkflowAuthorityError("execution initialization requires a fresh first observation")
                activation = db.execute(
                    "SELECT source_digest,inventory_digest,activated_at FROM workflow_authority_epochs "
                    "WHERE repository=? AND family=? AND epoch=? "
                    "AND length(CAST(source_digest AS BLOB))<=64 "
                    "AND length(CAST(inventory_digest AS BLOB))<=64 "
                    "AND length(CAST(activated_at AS BLOB))<=64",
                    (key.repository, WorkflowFamily.REVIEW_EXECUTION.value, execution_authority.epoch),
                ).fetchone()
                evidence = db.execute(
                    "SELECT plan_digest FROM workflow_activation_evidence "
                    "WHERE repository=? AND family=? AND epoch=? "
                    "AND length(CAST(plan_digest AS BLOB))<=64",
                    (key.repository, WorkflowFamily.REVIEW_EXECUTION.value, execution_authority.epoch),
                ).fetchone()
                if activation is None or evidence is None:
                    raise WorkflowAuthorityError("execution initialization requires complete activation evidence")
                try:
                    activated_at = datetime.fromisoformat(activation["activated_at"].replace("Z", "+00:00"))
                    if (activated_at.tzinfo is None or activated_at.utcoffset() is None
                            or activated_at > origin_claimed_at):
                        raise ValueError("activation time is not established")
                except (TypeError, ValueError, AttributeError) as exc:
                    raise WorkflowAuthorityError("execution initialization activation time is unreadable") from exc
                if recorded.observation.observed_at < activated_at:
                    raise WorkflowAuthorityError("execution activated after first observation; retry the read")
                evidence_id = self._evidence_id(recorded)
                try:
                    execution, origin_receipt = initialized_review_execution(
                        snapshot.binding, execution_authority.epoch, evidence_id,
                        evidence["plan_digest"], activation["source_digest"], activation["inventory_digest"],
                        activated_at,
                    )
                except (TypeError, ValueError) as exc:
                    raise WorkflowAuthorityError("execution initialization activation digests are unreadable") from exc
                snapshot = replace(snapshot, execution=CurrentReviewExecution(
                    execution.origin, execution.checkpoint, execution.state, 0, execution.last_sequence,
                ))
            db.execute("INSERT INTO workflows VALUES (?, ?, ?, 0, 0, ?)",
                       (*workflow_scope(key), encode_workflow_episodes(snapshot)))
            from .workflow_rows import initialize_new_continuations
            snapshot = initialize_new_continuations(db, snapshot)
            db.execute("UPDATE workflows SET episode_json=? WHERE repository=? AND target_kind=? AND target_number=?",
                       (encode_workflow_episodes(snapshot), *workflow_scope(key)))
            insert_workflow_component(db, key, authority, snapshot.episode.episode_id)
            observed_id = self._insert_evidence(db, key, recorded)
            if execution_authority is not None and not isinstance(snapshot.execution, BlockedReviewExecutionMigration):
                if origin_receipt is None or origin_claimed_at is None:
                    raise WorkflowAuthorityError("execution initialization lost its origin evidence")
                if observed_id != origin_receipt.observation_evidence_id:
                    raise WorkflowAuthorityError("first observation identity changed during initialization")
                insert_workflow_component(
                    db, key, execution_authority,
                    origin_receipt.checkpoint.state.binding.execution_episode,
                )
                insert_initialized_review_execution_origin(
                    db, key.repository, origin_receipt, claimed_at=origin_claimed_at,
                )
            if execution_authority is not None and load_review_workflow_row(db, key) != snapshot:
                raise WorkflowAuthorityError("discovered execution failed canonical readback")
            self._control._transition(db, "workflow", encode_workflow(key), "discovered",
                                      {"generation": 0, "version": 0, "epoch": authority.epoch})
            return WorkflowCommitted(snapshot)

    def _first_observation(
        self, db: sqlite3.Connection, key: WorkflowKey,
    ) -> tuple[ActiveReconciliation, bool] | None:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                         (_FIRST_OBSERVATION_NAMESPACE, self._writer.repository, f"pr:{key.target.number}")).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row["payload_json"])
            surfaced, reads = raw["surfaced"], raw["reads"]
            if not isinstance(surfaced, bool) or type(reads) is not int:
                raise ValueError("untyped surfaced flag or read count")
            return ActiveReconciliation(datetime.fromisoformat(raw["since"]), datetime.fromisoformat(raw["next"]),
                                        reads), surfaced
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowAuthorityError("first observation schedule is malformed") from exc

    def first_observation_due(self, key: WorkflowKey) -> bool:
        """Whether a never-observed pull request's first read may be attempted now."""
        self._scope(key)
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            prior = self._first_observation(db, key)
        return prior is None or prior[0].next_read_at <= self._now()

    def _first_observation_failed(self, key: WorkflowKey, error: str) -> None:
        with self._control.transaction() as db:
            prior = self._first_observation(db, key)
            surfaced = prior is not None and prior[1]
            progress = unavailable_first_observation(prior[0] if prior else None, surfaced=surfaced, now=self._now(),
                                                     policy=self._policy)
            history = progress.history
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?) "
                "ON CONFLICT(namespace,scope,key) DO UPDATE SET payload_json=excluded.payload_json",
                (_FIRST_OBSERVATION_NAMESPACE, self._writer.repository, f"pr:{key.target.number}",
                 self._now().isoformat(), json.dumps({"since": history.unresolved_since.isoformat(),
                                                      "next": history.next_read_at.isoformat(),
                                                      "reads": history.failed_reads,
                                                      "surfaced": surfaced or progress.surface})),
            )
            if prior is None or progress.surface:
                self._control._transition(db, "workflow", encode_workflow(key),
                                          "first_observation_ceiling" if progress.surface
                                          else "first_observation_unavailable",
                                          {"failed_reads": history.failed_reads, "error": error[:512],
                                           "next_read_at": history.next_read_at.isoformat()})

    # -- the one commit boundary ------------------------------------------------------------------

    def commit_workflow(
        self, expected: WorkflowRevision, authorities: tuple[FamilyAuthority, ...], transition: WorkflowChanged,
    ) -> WorkflowCommitted | WorkflowConflict:
        """Commit an observed transition that needs no barrier ordering.

        Generation advances (request, continuation, ceiling release) and retry
        grants move only through their own paths, which take the exclusive
        deployment barriers and check active review runs first; this entry
        point cannot bypass either (ADR 0015 acceptance ordering).
        """
        if isinstance(transition, WorkflowChanged) and transition.mutation in _BARRIER_ORDERED:
            raise WorkflowAuthorityError(
                f"{transition.mutation.value} commits only through its barrier-ordered acceptance path")
        with self._control.transaction() as db:
            return self._commit(db, expected, authorities, transition)

    def _commit(
        self, db: sqlite3.Connection, expected: WorkflowRevision, authorities: tuple[FamilyAuthority, ...],
        transition: WorkflowChanged,
        *, observation: RecordedPullRequest | None = None,
        stored_prior: ReviewWorkflowSnapshot | None = None,
    ) -> WorkflowCommitted | WorkflowConflict:
        with self._control.mutation_savepoint(db):
            if observation is not None:
                self._insert_evidence(db, expected.fence.key, observation)
            return self._commit_in_tx(db, expected, authorities, transition, stored_prior=stored_prior)

    def _commit_in_tx(
        self, db: sqlite3.Connection, expected: WorkflowRevision, authorities: tuple[FamilyAuthority, ...],
        transition: WorkflowChanged,
        *, stored_prior: ReviewWorkflowSnapshot | None = None,
    ) -> WorkflowCommitted | WorkflowConflict:
        if not isinstance(expected, WorkflowRevision) or not isinstance(transition, WorkflowChanged):
            raise ValueError("commit requires a typed revision and proposed transition")
        key = self._scope(expected.fence.key)
        if transition.expected != expected:
            raise WorkflowAuthorityError("commit and transition expect different workflow revisions")
        if not isinstance(transition.snapshot, ReviewWorkflowSnapshot):
            raise WorkflowAuthorityError("review commit requires a review-rejection snapshot")
        if not isinstance(authorities, tuple) or authorities != (transition.snapshot.authority,):
            raise WorkflowAuthorityError("review commit requires exactly its captured family authority")
        # Acceptance paths already read and validated this row under the same
        # BEGIN IMMEDIATE. Public proposal commits must load their own authority.
        prior = self._load(db, key) if stored_prior is None else stored_prior
        if authorities != (prior.authority,):
            raise WorkflowAuthorityError("workflow family activation epoch changed")
        encoded = transition_receipt(transition)
        replayed = self._replay(db, prior, transition, encoded)
        if replayed is not None:
            return replayed
        if isinstance(transition.audit, AuthorizedOperatorCommand):
            conflict = self._operator_binding_conflict(prior, transition.audit.command)
            if conflict is not None:
                return conflict
        # The proposal's prior is a caller claim: only the stored row is authority.
        if transition.prior != prior:
            raise WorkflowAuthorityError("review proposal does not replace the stored workflow snapshot")
        if transition.mutation is WorkflowMutation.RECORD_REVIEW:
            submission = self._recorded_submission(transition)
            if self._submission_recorded(db, submission):
                return WorkflowConflict(prior.revision, "this review receipt was already recorded")
        facts = self._facts(db, prior, transition)
        try:
            validate_review_transition(prior, transition, facts)
        except ValueError as exc:
            raise WorkflowAuthorityError(f"review proposal refused: {exc}") from exc
        self._validate_receipts(db, prior, transition, facts)
        # Enqueued commands load the new aggregate before this transaction ends;
        # its generation boundary requires the accepted transition receipt.
        committed = record_commit(db, self._control, self._writer.repository, transition, encoded)
        self._persist(db, prior, transition, encoded)
        if (transition.mutation is WorkflowMutation.RECORD_REVIEW
                and isinstance(prior.execution, CurrentReviewExecution)):
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       (_RECORDED_REVIEW_NAMESPACE, self._writer.repository, self._recorded_submission(transition),
                        self._now().isoformat(), encode_workflow(transition.snapshot.revision)))
        if (transition.mutation is WorkflowMutation.RECORD_REVIEW
                and isinstance(prior.execution, CurrentReviewExecution)):
            # The confirmed verdict is the reset boundary. A later settled
            # marker cannot repair a crash between baseline and round writes.
            from .review_execution_control import SQLiteReviewExecutionControl

            SQLiteReviewExecutionControl(self._control, self._writer, self._gateway)._settle_productive_in_tx(
                db, self._recorded_submission(transition), transition.snapshot,
            )
        if (transition.mutation is WorkflowMutation.RECORD_REVIEW
                and not isinstance(prior.execution, CurrentReviewExecution)):
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       (_RECORDED_REVIEW_NAMESPACE, self._writer.repository, self._recorded_submission(transition),
                        self._now().isoformat(), encode_workflow(transition.snapshot.revision)))
        # Retiring an uncertain older-generation command opens its gate in this
        # transaction: the caller receives the stored row, never a stale one.
        stored = self._load(db, key)
        return committed if stored == committed.snapshot else WorkflowCommitted(stored)

    def _replay(
        self, db: sqlite3.Connection, prior: ReviewWorkflowSnapshot, transition: WorkflowChanged, encoded: str,
    ) -> WorkflowCommitted | WorkflowConflict | None:
        """An exact replay of the current decision is idempotent; reuse or a moved revision conflicts."""
        namespace, dedupe = transition_dedupe_key(transition)
        receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                             (namespace, self._writer.repository, dedupe)).fetchone()
        if receipt is None:
            if prior.revision != transition.expected:
                return WorkflowConflict(prior.revision, "workflow revision changed before commit")
            return None
        if not same_receipt(receipt["payload_json"], transition, encoded):
            return WorkflowConflict(prior.revision, "durable workflow identity was reused with different content")
        if namespace == "workflow-request" and (prior.intent is None or prior.intent.request_id != dedupe):
            # Dismissal command identities derive from request identities.
            return WorkflowConflict(prior.revision, "request identity belongs to an older, replaced review re-arm")
        return WorkflowCommitted(prior, idempotent=True)

    @staticmethod
    def _recorded_submission(transition: WorkflowChanged) -> str:
        audit = transition.audit
        if not isinstance(audit, ObservedTransition) or not audit.evidence_ids:
            raise WorkflowAuthorityError("a recorded review names its bound submission")
        return audit.evidence_ids[0]

    def _submission_recorded(self, db: sqlite3.Connection, command_id: str) -> bool:
        return db.execute(
            f"SELECT 1 FROM dedupe_keys WHERE namespace='{_RECORDED_REVIEW_NAMESPACE}' AND scope=? AND key=?",
            (self._writer.repository, command_id),
        ).fetchone() is not None

    def _facts(
        self, db: sqlite3.Connection, prior: ReviewWorkflowSnapshot, transition: WorkflowChanged,
    ) -> ReviewTransitionFacts:
        """Mint every validation fact from harness-owned rows, never from the proposal."""
        key = prior.revision.fence.key
        now = self._now()
        audit, mutation = transition.audit, transition.mutation
        observation: ObservedPullRequest | None = None
        holds: tuple[str, ...] = ()
        escalation: CeilingEscalation | None = None
        submission: ReviewSubmissionReceipt | None = None
        accepted = self._accepted_preflight(db, prior) if isinstance(prior.intent, PendingReviewRearm) else None
        if isinstance(audit, ObservedTransition):
            ids = list(audit.evidence_ids)
            if mutation in (WorkflowMutation.OPEN_GATE, WorkflowMutation.RECORD_REVIEW) and db.execute(
                "SELECT 1 FROM lifecycle_commands WHERE command_id=?", (ids[0],),
            ).fetchone() is not None:
                bound = load_bound_review_submission(db, CommandId(ids[0]))
                if bound is None or len(ids) < 2 or bound.receipt_id != ids[1] or (
                    bound.submission.binding.fence.key != key
                ):
                    raise WorkflowAuthorityError("transition names no verified bound review submission of this workflow")
                if mutation is WorkflowMutation.OPEN_GATE:
                    if not isinstance(bound, CeilingEscalation):
                        raise WorkflowAuthorityError("a new ceiling requires the harness's escalation receipt")
                    escalation = bound
                else:
                    if not isinstance(bound, ReviewSubmissionReceipt):
                        raise WorkflowAuthorityError("a recorded review requires a verdict receipt")
                    submission = bound
                ids = ids[2:]
            elif mutation is WorkflowMutation.RECORD_REVIEW:
                raise WorkflowAuthorityError("a recorded review requires its bound submission receipt")
            records = [self._recorded(db, key, evidence_id) for evidence_id in ids]
            if len(records) > 1:
                raise WorkflowAuthorityError("an observed review transition carries exactly one observation")
            if records:
                observation, holds = records[0].observation, records[0].external_holds
        elif isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.preflight, ReviewRearmPreflight):
            recorded = self._recorded(db, key, audit.preflight.complete_reviews_evidence_id)
            observation, holds = recorded.observation, recorded.external_holds
            if not isinstance(observation.reviews, CompleteReviews):
                raise WorkflowAuthorityError("a re-arm preflight requires a complete review read")
            if not 0 <= (now - observation.observed_at).total_seconds() <= OBSERVATION_MAX_AGE_SECONDS:
                raise WorkflowAuthorityError("a re-arm preflight requires a fresh read under the acceptance barrier")
        retry = (self._retry_facts(db, audit.command) if isinstance(audit, AuthorizedOperatorCommand)
                 and isinstance(audit.command, AuthorizeAbsentRetry) else None)
        return ReviewTransitionFacts(
            now, self._ceiling, self._trigger, self._writer.owner_id, rule=self._rule, observation=observation,
            accepted_preflight=accepted, external_holds=holds, escalation=escalation, submission=submission,
            retry=retry,
            execution_ceiling=self._execution_ceiling,
            verdict_receipt=self._fixed_verdict_receipt(db, prior,
                audit.command.submission_command_id if isinstance(audit.command, RequestReviewVerdictRelease) else None)
            if isinstance(audit, AuthorizedOperatorCommand)
            and isinstance(audit.command, RequestReviewExecutionRenewal) else None,
            applied_sequences=self._generation_applied_sequences(db, prior) if (
                transition.snapshot.revision.fence.generation > prior.revision.fence.generation
            ) else (),
        )

    def _fixed_verdict_receipt(
        self, db: sqlite3.Connection, snapshot: ReviewWorkflowSnapshot, identity: CommandId | None = None,
    ) -> ReviewSubmissionReceipt | None:
        """Mint release eligibility from registered commands and harness completions."""
        baseline = snapshot.episode.continuation
        selected = baseline.source.command_id if isinstance(baseline, ReviewedBaseline) and isinstance(
            baseline.source, BoundReviewSubmission,
        ) else None
        if identity is not None:
            if identity != selected and not any(
                isinstance(gate, OpenGate) and isinstance(gate.cause, ReviewVerdictHold)
                and gate.cause.submission_command_id == identity for gate in snapshot.gates
            ):
                return None
        elif selected is not None:
            identity = selected
        else:
            return None
        try:
            receipt = load_bound_review_submission(db, CommandId(identity))
        except KeyError as exc:
            raise WorkflowAuthorityError("selected verdict command is absent") from exc
        if not isinstance(receipt, ReviewSubmissionReceipt) or not receipt.escalated:
            return None
        registration = read_review_submission_registration(db, self._writer.repository, identity)
        if registration is None:
            raise WorkflowAuthorityError("selected verdict lacks its exact registered round source")
        round = registration.round
        key = snapshot.revision.fence.key
        if (registration.submission.command_id != identity or round.binding.rejection.fence.key != key
                or round.binding.rejection.authority != snapshot.authority
                or round.binding.rejection.episode_id != snapshot.episode.episode_id):
            raise WorkflowAuthorityError("selected verdict belongs to another registered component")
        outcome = _read_current_round_outcome(db,
            (key.repository, key.target.number, round.binding.execution_epoch, round.binding.execution_episode),
            round.sequence)
        if outcome.round != round:
            raise WorkflowAuthorityError("selected verdict source changed its exact round identity")
        return receipt

    def _old_execution_episode_open(self, db: sqlite3.Connection, snapshot: ReviewWorkflowSnapshot) -> bool:
        execution = snapshot.execution
        if not isinstance(execution, CurrentReviewExecution):
            return True
        # Renewal, unlike ordinary reads, must drain every generation of the
        # old episode. Final round receipts already require classified returns,
        # money settlement and resolved registered submission effects.
        binding = execution.binding
        return db.execute(
            "SELECT 1 FROM review_execution_rounds INDEXED BY review_execution_rounds_open "
            "WHERE repository=? AND target_number=? "
            "AND execution_epoch=? AND execution_episode=? AND outcome_json IS NULL LIMIT 1",
            (binding.rejection.fence.key.repository, binding.rejection.fence.key.target.number,
             binding.execution_epoch, binding.execution_episode),
        ).fetchone() is not None

    def _generation_applied_sequences(
        self, db: sqlite3.Connection, snapshot: ReviewWorkflowSnapshot,
    ) -> tuple[int, ...]:
        execution = snapshot.execution
        if isinstance(execution, CurrentReviewExecution):
            return tuple(item.round.sequence for item in read_current_review_pending(db, execution)
                         if isinstance(item, CompletedReviewExecutionRound))
        return ()

    def _retry_facts(self, db: sqlite3.Connection, command: AuthorizeAbsentRetry) -> RetryAuthorizationFacts:
        """The shared grant facts, scoped to this review owner's own command of this workflow."""
        facts = load_retry_authorization_facts(self._control, db, command.command_id, command.evidence_id)
        row = db.execute("SELECT executor_identity FROM lifecycle_commands WHERE command_id=?",
                         (str(command.command_id),)).fetchone()
        if facts.command_binding.fence.key != command.expected.fence.key or not owns_command(
            db, self._writer, row["executor_identity"]
        ):
            raise WorkflowAuthorityError("retry authorization names a command outside this review owner's workflow")
        return facts

    def _retry_offers(
        self, db: sqlite3.Connection, snapshot: ReviewWorkflowSnapshot, unavailable: frozenset[str] = frozenset(),
    ) -> tuple[frozenset[str], tuple[RetryRefusal, ...]]:
        """Retry gates offered exactly when acceptance would admit a grant now, and why each other one is not.

        A gate in `unavailable` has inconsistent facts; it is neither offered nor explained here.
        """
        available: set[str] = set()
        refusals: list[RetryRefusal] = []
        for gate in snapshot.gates:
            if (not isinstance(gate, OpenGate) or not isinstance(gate.cause, RetryAuthorizationRequired)
                    or gate.gate_id in unavailable):
                continue
            try:
                facts = load_retry_authorization_facts(self._control, db, gate.cause.command_id,
                                                       gate.cause.absence_evidence_id)
                row = db.execute("SELECT executor_identity FROM lifecycle_commands WHERE command_id=?",
                                 (str(gate.cause.command_id),)).fetchone()
                owned = facts.command_binding.fence.key == snapshot.revision.fence.key and owns_command(
                    db, self._writer, row["executor_identity"])
            except (WorkflowAuthorityError, ControlStoreError, ValueError, TypeError) as exc:
                # Unreadable authority is never offered as an action.
                refusals.append(RetryRefusal(gate.gate_id, f"retry authority is unreadable: {_reason(exc)[:512]}"))
                continue
            if not owned:
                refusals.append(RetryRefusal(gate.gate_id, "the command belongs to another executor or workflow"))
                continue
            refusal = retry_authorization_refusal(
                snapshot, command_id=gate.cause.command_id, gate_id=gate.gate_id,
                evidence_id=gate.cause.absence_evidence_id, facts=facts,
            )
            if refusal is None:
                available.add(gate.gate_id)
            else:
                refusals.append(RetryRefusal(gate.gate_id, refusal.reason))
        return frozenset(available), tuple(refusals)

    def _validate_receipts(
        self, db: sqlite3.Connection, prior: ReviewWorkflowSnapshot, transition: WorkflowChanged,
        facts: ReviewTransitionFacts,
    ) -> None:
        """Receipt facts the pure policy cannot see (ADR 0015: dispositions retain every receipt)."""
        after = transition.snapshot
        assert isinstance(after, ReviewWorkflowSnapshot)
        intent = after.intent
        if isinstance(prior.intent, PendingReviewRearm) and isinstance(
            intent, (CompletedReviewRearm, CancelledReviewRearm, SupersededReviewRearm)
        ):
            pr = facts.observation if isinstance(facts.observation, ObservedPullRequest) else None
            verified, rejected, uncertainty = self._rearm_command_facts(db, prior, pr)
            if set(intent.verified_commands) != set(verified):
                raise WorkflowAuthorityError("a re-arm disposition must retain exactly the successful required receipts")
            if isinstance(intent, SupersededReviewRearm) and intent.reason is StopReason.REQUIRED_EFFECT_REJECTED and not (
                set(rejected) & set(intent.retired_obligations)
            ):
                raise WorkflowAuthorityError("effect-rejected supersession requires an exact required command refusal")
            existing = {(g.gate if isinstance(g, RearmingGate) else g).gate_id for g in prior.gates}
            claimed = {cause.command_id for g in after.gates
                       if (g.gate if isinstance(g, RearmingGate) else g).gate_id not in existing
                       and isinstance(cause := (g.gate if isinstance(g, RearmingGate) else g).cause, RemoteOutcomeUncertain)}
            if not claimed <= {item.command_id for item in uncertainty}:
                raise WorkflowAuthorityError("stopped re-arm uncertainty disagrees with its command receipts")
        if any(command.executor_identity != self._writer.owner_id for command in transition.commands):
            raise WorkflowAuthorityError("review command does not belong to the accepting review owner")

    def _rearm_command_facts(
        self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot, pr: ObservedPullRequest | None,
    ) -> tuple[tuple[CommandId, ...], tuple[CommandId, ...], tuple[RemoteOutcomeUncertain, ...]]:
        """Verified, refused and pre-expiry-uncertain obligations from the store's own receipts.

        A reconciliation-only obligation whose absence the harness proved is
        refused when `pr` (a fresh complete read) shows its accepted guard no
        longer applies. A grant spent against that guard ends inapplicable
        without a write, so this read, not a grant, carries a changed guard to
        `REQUIRED_EFFECT_REJECTED` (ADR 0015).
        """
        intent = state.intent
        if not isinstance(intent, PendingReviewRearm):
            raise WorkflowAuthorityError("re-arm command facts require a pending intent")
        rows = self._rearm_command_rows(db, intent)
        gates = {(g.gate if isinstance(g, RearmingGate) else g).gate_id: (g.gate if isinstance(g, RearmingGate) else g)
                 for g in state.gates}
        verified: list[CommandId] = []
        rejected: list[CommandId] = []
        uncertainty: list[RemoteOutcomeUncertain] = []
        for row in rows:
            outcome, uncertain = self._rearm_command_fact(db, state, gates, row, pr)
            command_id = CommandId(row["command_id"])
            if outcome == "verified":
                verified.append(command_id)
            elif outcome == "rejected":
                rejected.append(command_id)
            if uncertain is not None:
                uncertainty.append(uncertain)
        order = {command: index for index, command in enumerate(intent.required_commands)}
        return (tuple(sorted(verified, key=order.__getitem__)), tuple(sorted(rejected, key=order.__getitem__)),
                tuple(sorted(uncertainty, key=lambda item: order[item.command_id])))

    @staticmethod
    def _rearm_command_rows(db: sqlite3.Connection, intent: PendingReviewRearm) -> list[sqlite3.Row]:
        """The pending intent's associated commands with receipts; exactly its required obligations."""
        rows = db.execute(
            "SELECT association.disposition,command.*,receipt.executor_identity AS receipt_executor,"
            "receipt.started_at AS receipt_started,receipt.completed_at AS receipt_completed,"
            "receipt.status AS receipt_status,receipt.error AS receipt_error "
            "FROM workflow_rearm_commands association JOIN lifecycle_commands command USING(command_id) "
            "LEFT JOIN effect_receipts receipt USING(command_id) WHERE association.request_id=?",
            (intent.request_id,),
        ).fetchall()
        required = set(intent.required_commands)
        if {CommandId(row["command_id"]) for row in rows} != required or len(rows) != len(required):
            raise WorkflowAuthorityError("pending review re-arm commands disagree with required obligations")
        return rows

    def _rearm_command_fact(
        self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot, gates: dict[str, OpenGate], row: sqlite3.Row,
        pr: ObservedPullRequest | None,
    ) -> tuple[str, RemoteOutcomeUncertain | None]:
        """One required obligation: ("verified" | "rejected" | "pending", its unresolved outcome if any).

        Raises `WorkflowAuthorityError` when the store's rows about it disagree.
        """
        command = _command_from_row(row)
        if (row["disposition"] != "required" or command.workflow != state.binding
                or not owns_command(db, self._writer, command.executor_identity)):
            raise WorkflowAuthorityError("pending review re-arm command belongs to another authority")
        reconciliation = self._control._command_reconciliation(row)
        if isinstance(reconciliation, (RetiredUncertainCommand, RetiredSettledCommand)):
            raise WorkflowAuthorityError("a pending re-arm obligation cannot already be retired")
        if isinstance(reconciliation, ReconciliationOnly):
            if row["state"] not in ("executing", "unknown_outcome"):
                raise WorkflowAuthorityError("reconciliation-only obligation lacks an unresolved outcome")
            self._control._require_reconciliation_gate(db, command, reconciliation)
            gate = gates.get(reconciliation.gate_id)
            if pr is not None and gate is not None and isinstance(gate.cause, RetryAuthorizationRequired):
                op = review_family_op(command)
                if op is not None and check_review_op(op, pr).check is ReviewCheck.REFUSED:
                    return "rejected", None
            return "pending", None
        status = row["receipt_status"]
        if status is None:
            if row["state"] not in ("pending", "executing"):
                raise WorkflowAuthorityError("settled re-arm command lacks its exact receipt")
            history = self._control._unresolved_history(row)
            return "pending", (None if history is None else RemoteOutcomeUncertain(
                command.command_id, history.unresolved_since, history.failed_reads,
                "command outcome remains unresolved"))
        try:
            settled = EffectStatus(status)
            started = datetime.fromisoformat(row["receipt_started"])
            completed = datetime.fromisoformat(row["receipt_completed"])
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("re-arm command has malformed receipt evidence") from exc
        if (row["state"] != settled.value or not row["attempt_owner"]
                or row["receipt_executor"] != row["attempt_owner"] or completed < started):
            raise WorkflowAuthorityError("re-arm command receipt disagrees with its execution")
        if settled is EffectStatus.SUCCEEDED:
            return "verified", None
        if settled is EffectStatus.PERMANENT_FAILURE:
            return "rejected", None
        if settled is EffectStatus.UNKNOWN_OUTCOME:
            history = self._control._unresolved_history(row)
            assert history is not None
            return "pending", RemoteOutcomeUncertain(
                command.command_id, history.unresolved_since, history.failed_reads,
                row["receipt_error"] or "command outcome remains unresolved")
        if reconciliation is not None:
            raise WorkflowAuthorityError("settled command retained incompatible uncertainty")
        return "pending", None

    def _persist(
        self, db: sqlite3.Connection, prior: ReviewWorkflowSnapshot, transition: WorkflowChanged, encoded: str,
    ) -> None:
        after = transition.snapshot
        assert isinstance(after, ReviewWorkflowSnapshot)
        key = after.revision.fence.key
        scope = workflow_scope(key)
        if (after.revision.fence.generation != prior.revision.fence.generation
                and isinstance(prior.execution, CurrentReviewExecution)):
            advanced, boundary = freeze_current_review_generation(
                prior.execution, prior.revision, after.revision, transition.mutation,
                self._generation_applied_sequences(db, prior),
            )
            audit = transition.audit
            if (isinstance(audit, AuthorizedOperatorCommand)
                    and isinstance(audit.command, RequestReviewExecutionRenewal)
                    and not isinstance(audit.command, RequestReviewVerdictRelease)):
                renewed, origin = renew_current_review_execution(
                    prior.execution, prior.revision, after.binding, audit.command.request_id,
                )
                if after.execution != renewed or self._old_execution_episode_open(db, prior):
                    raise WorkflowAuthorityError("execution renewal requires the exact quiescent old episode")
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    (REVIEW_EXECUTION_ORIGIN_NAMESPACE, self._writer.repository, origin.origin.evidence_id,
                     self._now().isoformat(), encode_workflow(origin)),
                )
                db.execute(
                    "UPDATE workflow_components SET episode_id=? WHERE repository=? AND target_kind=? "
                    "AND target_number=? AND family=? AND epoch=? AND episode_id=?",
                    (renewed.binding.execution_episode, *scope, WorkflowFamily.REVIEW_EXECUTION.value,
                     prior.execution.binding.execution_epoch, prior.execution.binding.execution_episode),
                )
            elif after.execution != advanced:
                raise WorkflowAuthorityError("generation advance lost its exact current execution freeze")
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                ("review-execution-generation", self._writer.repository, boundary.receipt_id,
                 self._now().isoformat(), encode_workflow(boundary)),
            )
        persist_workflow_snapshot(db, after)
        intent = after.intent
        if intent is not None and intent != prior.intent:
            if isinstance(intent, PendingReviewRearm):
                assert isinstance(transition.audit, AuthorizedOperatorCommand)
                insert_pending_rearm(db, scope, intent, transition.audit)
                now = self._now()
                seed = RearmObservation(intent.request_id, after.binding, ActiveReconciliation(now, now, 0))
                observation = schedule_rearm_observation(seed, now=now, policy=self._policy)
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                    (_OBSERVATION_NAMESPACE, self._writer.repository, intent.request_id,
                     observation.history.next_read_at.isoformat(), encode_workflow(observation)),
                )
            else:
                update_rearm_state(db, intent)
        for command in transition.commands:
            self._control._enqueue_command(db, command)
            if isinstance(intent, PendingReviewRearm) and command.command_id in intent.required_commands:
                operation = (f"dismiss:{command.payload['review_id']}" if command.kind is CommandKind.DISMISS_REVIEW
                             else "review_projection")
                db.execute("INSERT INTO workflow_rearm_commands VALUES (?, ?, ?, 'required', ?)",
                           (intent.request_id, operation, str(command.command_id), encode_workflow(transition.audit)))
        if after.revision.fence.generation != prior.revision.fence.generation:
            # The policy removed their holds: no older-generation command can begin.
            self._retire_stale_generation(db, prior, after)
        if transition.mutation is WorkflowMutation.AUTHORIZE_RETRY:
            audit = transition.audit
            assert isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, AuthorizeAbsentRetry)
            persist_retry_authorization(db, self._control, transition, self._retry_facts(db, audit.command))
        if isinstance(intent, (CancelledReviewRearm, SupersededReviewRearm)) and intent != prior.intent:
            # Keyed by the stopped request too: two stops can cite one identical observation.
            retirement_id = "workflow-stop:" + hashlib.sha256(
                encode_workflow((intent.request_id, transition.audit)).encode()).hexdigest()
            db.execute("INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                       (retirement_id, *scope, encode_workflow(transition.audit)))
            for command_id in intent.retired_obligations:
                retire_rearm_obligation(db, intent, str(command_id))
                self._retire_obligation(db, prior, after, str(command_id), retirement_id)

    def _retire_stale_generation(
        self, db: sqlite3.Connection, prior: ReviewWorkflowSnapshot, after: ReviewWorkflowSnapshot,
    ) -> None:
        """End every older-generation command in the advance's transaction.

        A command bound to an older generation can never begin. One that never
        began, or failed retryably (including a proven-absent attempt awaiting
        re-execution), retires settled with typed evidence, so no drain
        re-selects it every tick. A begun or uncertain one retires uncertain
        behind a `RemoteOutcomeUncertain` gate: no grant can ever buy it a
        write, and its next read settles it either way, so no hold is left
        that neither a grant nor a read can clear. Settled commands stay as
        they are. Every older pre-write schedule ends.
        """
        key = after.revision.fence.key
        scope = workflow_scope(key)
        retirement_id = "generation-advance:" + hashlib.sha256(
            encode_workflow((after.revision.fence, after.binding)).encode()).hexdigest()
        payload = json.dumps({"reason": "generation advanced", "from_generation": prior.revision.fence.generation,
                              "to_generation": after.revision.fence.generation}, sort_keys=True)
        recorded = False
        for candidate in db.execute(
            "SELECT command_id,state,prewrite_read_json,workflow_reconciliation_json "
            "FROM lifecycle_commands WHERE repository=? AND target=? AND workflow_binding_json IS NOT NULL "
            f"AND {_REVIEW_GENERATION_CANDIDATE}",
            (key.repository, f"pr:{key.target.number}"),
        ).fetchall():
            if (candidate["prewrite_read_json"] is None
                    and candidate["state"] not in ("succeeded", "permanent_failure")
                    and candidate["workflow_reconciliation_json"] is not None):
                try:
                    reconciliation = self._control._command_reconciliation(candidate)
                except WorkflowEncodingError:
                    # Preserve the binding checks and error ordering below.
                    pass
                else:
                    if isinstance(reconciliation, RetiredSettledCommand):
                        # Fully settled history with no scheduled cleanup cannot
                        # authorize an action; its old payload is not current authority.
                        continue
            row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?",
                             (candidate["command_id"],)).fetchone()
            if row is None:
                raise WorkflowAuthorityError("selected generation command disappeared")
            command = _command_from_row(row)
            binding = command.workflow
            assert binding is not None
            if binding.fence.key != key or binding == after.binding:
                continue
            if row["prewrite_read_json"] is not None:
                db.execute("UPDATE lifecycle_commands SET prewrite_read_json=NULL WHERE command_id=?",
                           (row["command_id"],))
                self._control._transition(db, "lifecycle_command", row["command_id"], "prewrite_evidence_ended",
                                          {"reason": "generation advanced"})
            if row["state"] in ("succeeded", "permanent_failure") or isinstance(
                self._control._command_reconciliation(row), (RetiredSettledCommand, RetiredUncertainCommand)
            ):
                continue
            if not owns_command(db, self._writer, command.executor_identity):
                raise WorkflowAuthorityError("an older-generation command belongs to another owner")
            if not recorded:
                db.execute("INSERT OR IGNORE INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                           (retirement_id, *scope, payload))
                recorded = True
            self._control._retire_workflow_command(db, row, command, after, retirement_id, create_uncertainty=True)
            self._control._transition(db, "lifecycle_command", row["command_id"], "stale_generation_retired", {
                "evidence_id": retirement_id, "generation": binding.fence.generation,
                "current_generation": after.revision.fence.generation,
            })

    def _retire_obligation(
        self, db: sqlite3.Connection, prior: ReviewWorkflowSnapshot, after: ReviewWorkflowSnapshot,
        command_id: str, retirement_id: str,
    ) -> None:
        row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            raise WorkflowAuthorityError("retired obligation has no durable command")
        command = _command_from_row(row)
        if not owns_command(db, self._writer, command.executor_identity) or command.workflow != after.binding:
            raise WorkflowAuthorityError("retired obligation belongs to a different workflow owner")
        reconciliation = self._control._command_reconciliation(row)
        proven = next((opened for g in prior.gates
                       if isinstance((opened := g.gate if isinstance(g, RearmingGate) else g).cause,
                                     RetryAuthorizationRequired)
                       and opened.cause.command_id == command.command_id), None)
        if isinstance(reconciliation, ReconciliationOnly) and proven is not None and proven.gate_id == reconciliation.gate_id:
            # The harness already proved the current postcondition absent and the
            # command can never execute: the stop retires that hold (the policy
            # removed its gate), never asserting the write historically failed.
            assert isinstance(proven.cause, RetryAuthorizationRequired)
            evidence = db.execute("SELECT kind, command_id FROM workflow_evidence WHERE evidence_id=?",
                                  (proven.cause.absence_evidence_id,)).fetchone()
            if evidence is None or evidence["kind"] != "absence" or evidence["command_id"] != command_id:
                raise WorkflowAuthorityError("proven-absent obligation lacks its absence evidence")
            state = RetiredSettledCommand(retirement_id, proven.cause.absence_evidence_id)
            db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                       (encode_workflow(state), command_id))
            grant = self._control._open_authorization(db, command.command_id)
            if grant is not None:
                self._control._dispose_grant(db, grant, RetryDisposition.VOID.value,
                                             "the command was retired before the grant was spent",
                                             details={"evidence_id": retirement_id})
            self._control._transition(db, "lifecycle_command", command_id, "superseded", {"evidence_id": retirement_id})
        else:
            self._control._retire_workflow_command(db, row, command, after, retirement_id)
        db.execute("UPDATE lifecycle_commands SET prewrite_read_json=NULL WHERE command_id=?", (command_id,))

    # -- observed transitions ------------------------------------------------------------------

    def open_ceiling(self, escalation: CommandId) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        """Open a NEW rejection ceiling proven by the harness's own verified escalation receipt."""
        with closing(self._control._connect()) as db:
            bound = load_bound_review_submission(db, escalation)
        if not isinstance(bound, CeilingEscalation):
            return WorkflowRejected(WorkflowRejection.INVALID_SCOPE,
                                    "a rejection ceiling opens only on a verified ceiling escalation receipt")
        key = self._scope(bound.submission.binding.fence.key)
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            current = load_review_workflow_row(db, key)
            if current is not None and isinstance(current.execution, CurrentReviewExecution):
                registration = read_review_submission_registration(db, self._writer.repository, str(escalation))
                if registration is None:
                    raise WorkflowAuthorityError("active ceiling escalation lacks its originating round")
                receipt = db.execute(
                    "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                    (REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, self._writer.repository,
                     registration.round.round_id),
                ).fetchone()
                if receipt is not None:
                    recorded = decode_workflow(receipt["payload_json"], tuple)
                    if (len(recorded) != 3 or not isinstance(recorded[0], CeilingProductivityDecision)
                            or recorded[0].escalation != bound):
                        raise WorkflowAuthorityError("ceiling escalation replay disagrees with its round")
                    return (recorded[0].refusal if recorded[0].refusal is not None
                            else WorkflowCommitted(current, idempotent=True))
        recorded = self._observe(key)
        with self._control.transaction() as db:
            prior = self._load(db, key)
            if isinstance(recorded, PullRequestUnavailable):
                return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, recorded.reason)
            decided_at = self._now()
            proposal = open_review_ceiling(
                prior, recorded.observation, now=decided_at, rule=self._rule, ceiling=self._ceiling,
                escalation=bound, trigger_label=self._trigger, executor_identity=self._writer.owner_id,
                evidence_ids=(self._evidence_id(recorded),),
            )
            result = self._apply(db, prior, key, recorded, proposal)
            if (isinstance(prior.execution, CurrentReviewExecution)
                    and not isinstance(result, WorkflowConflict)
                    and not (isinstance(result, WorkflowRejected)
                             and result.code is WorkflowRejection.OBSERVATION_UNAVAILABLE)):
                from .review_execution_control import SQLiteReviewExecutionControl

                evidence_id = self._insert_evidence(db, key, recorded)
                decision = CeilingProductivityDecision(
                    bound, prior, evidence_id, decided_at, self._rule, self._ceiling,
                    self._trigger, self._writer.owner_id,
                    result if isinstance(result, WorkflowRejected) else None,
                )
                SQLiteReviewExecutionControl(self._control, self._writer, self._gateway)._settle_productive_in_tx(
                    db, str(escalation), self._load(db, key), ceiling=decision,
                )
                settled = self._load(db, key)
                if isinstance(result, WorkflowCommitted):
                    return WorkflowCommitted(settled)
            return result

    def reconcile_ceiling(self, key: WorkflowKey) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        """Refresh or release this episode's ceiling from a fresh complete read (under the barriers).

        A release advances the generation, which fences every older command,
        so it is ordered with executing effects like an operator re-arm.
        """
        return self._reconcile_ceiling(key)[0]

    def _reconcile_ceiling(
        self, key: WorkflowKey,
    ) -> tuple[WorkflowCommitted | WorkflowConflict | WorkflowRejected, bool]:
        """`reconcile_ceiling`, and whether the same read re-projected an open ceiling's missing hold."""
        self._scope(key)
        with self._barriers():
            recorded = self._observe(key)
            with self._control.transaction() as db:
                prior = self._load(db, key)
                if isinstance(recorded, PullRequestUnavailable):
                    return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, recorded.reason), False
                proposal = reconcile_review_ceiling(
                    prior, recorded.observation, now=self._now(), ceiling=self._ceiling,
                    evidence_ids=(self._evidence_id(recorded),), trigger_label=self._trigger,
                    executor_identity=self._writer.owner_id, external_holds=recorded.external_holds,
                    applied_sequences=self._generation_applied_sequences(db, prior),
                )
                result = self._apply(db, prior, key, recorded, proposal)
                if not isinstance(result, WorkflowCommitted):
                    return result, False
                return result, self._restore_ceiling_hold(db, key, recorded)

    def _restore_ceiling_hold(self, db: sqlite3.Connection, key: WorkflowKey, recorded: RecordedPullRequest) -> bool:
        """Re-project an open ceiling's missing `needs-human-review` from this read (keifu #173 class).

        One workflow-owned, hold-guarded relabel per read that found the hold
        missing, journalled, at most `MAX_HOLD_RESTORES` per gate; past that
        the contest is journalled once and the gate alone keeps holding
        reviews. Never while another review-family relabel of this pull
        request is still unsettled (the escalation's own projection, a
        re-arm's), which already decides these labels. A restoration is the
        harness's own write, so it never resets the hold's read backoff.
        """
        state = self._load(db, key)
        for gate_id in verified_ceiling_holds(state, recorded.observation, trigger_label=self._trigger):
            # The hold is on GitHub whoever applied it; its signal is owed once.
            self._owe_signal(db, key, gate_id, "hold_verified")
        restores = self._clear_stale_restores(db, key, state)
        evidence_id = self._evidence_id(recorded)
        decision = ceiling_hold_restoration(
            state, recorded.observation, evidence_id, restores={g: n for g, (n, _) in restores.items()},
            trigger_label=self._trigger, executor_identity=self._writer.owner_id,
        )
        if decision is None or self._unsettled_projection(db, key) or self._unresolved_escalation(db, key):
            return False
        if isinstance(decision, HoldContested):
            if not restores.get(decision.gate_id, (0, False))[1]:
                self._owe_signal(db, key, decision.gate_id, "hold_contested")
                self._write_restores(db, key, decision.gate_id, decision.restores, contested=True)
                self._control._transition(db, "workflow_hold", f"pr:{key.target.number}", "hold_contested", {
                    "gate_id": decision.gate_id, "restores": decision.restores,
                    "generation": state.revision.fence.generation,
                    "labels": sorted(label.value for label in recorded.observation.labels),
                })
            return False
        assert isinstance(decision, RestoreHold)
        count = restores.get(decision.gate_id, (0, False))[0] + 1
        self._insert_evidence(db, key, recorded)
        self._control._enqueue_command(db, decision.command)
        self._write_restores(db, key, decision.gate_id, count, contested=False)
        self._control._transition(db, "workflow_hold", f"pr:{key.target.number}", "hold_reprojected", {
            "command_id": str(decision.command.command_id), "generation": state.revision.fence.generation,
            "gate_id": decision.gate_id, "restores": count,
            "labels": sorted(label.value for label in recorded.observation.labels), "evidence_id": evidence_id,
        })
        return True

    def _restore_records(self, db: sqlite3.Connection, key: WorkflowKey) -> list[sqlite3.Row]:
        return db.execute("SELECT key, payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key LIKE ?",
                          (_HOLD_RESTORES_NAMESPACE, self._writer.repository, f"pr:{key.target.number}:%")).fetchall()

    @staticmethod
    def _restore_count(payload_json: str) -> tuple[int, bool]:
        try:
            payload = json.loads(payload_json)
            restores, contested = int(payload["restores"]), payload["contested"]
            if restores < 0 or not isinstance(contested, bool):
                raise ValueError("negative or untyped restoration count")
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowAuthorityError("hold restoration count is malformed") from exc
        return restores, contested

    def _restore_rows(self, db: sqlite3.Connection, key: WorkflowKey) -> dict[str, tuple[int, bool]]:
        return {row["key"].split(":", 2)[2]: self._restore_count(row["payload_json"])
                for row in self._restore_records(db, key)}

    def _clear_stale_restores(
        self, db: sqlite3.Connection, key: WorkflowKey, state: ReviewWorkflowSnapshot,
    ) -> dict[str, tuple[int, bool]]:
        """Drop counts of gates that changed or released; a cleared contest is journalled."""
        rows = self._restore_rows(db, key)
        live = restorable_ceiling_gate_ids(state)
        for gate_id, (_, contested) in list(rows.items()):
            if gate_id in live:
                continue
            db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                       (_HOLD_RESTORES_NAMESPACE, self._writer.repository, f"pr:{key.target.number}:{gate_id}"))
            del rows[gate_id]
            if contested:
                self._control._transition(db, "workflow_hold", f"pr:{key.target.number}", "hold_contest_cleared",
                                          {"gate_id": gate_id, "generation": state.revision.fence.generation})
        return rows

    def _write_restores(self, db: sqlite3.Connection, key: WorkflowKey, gate_id: str, restores: int, *,
                        contested: bool) -> None:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?) "
            "ON CONFLICT(namespace,scope,key) DO UPDATE SET payload_json=excluded.payload_json",
            (_HOLD_RESTORES_NAMESPACE, self._writer.repository, f"pr:{key.target.number}:{gate_id}",
             self._now().isoformat(), json.dumps({"restores": restores, "contested": contested})),
        )

    # -- refused escalation projections (increment 6 re-review N1) --------------------------------

    def _refused_escalations(self, db: sqlite3.Connection, key: WorkflowKey) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM lifecycle_commands c WHERE c.repository=? AND c.target=? AND c.kind=? AND c.state=? "
            "AND c.idempotency_key LIKE ? AND NOT EXISTS (SELECT 1 FROM dedupe_keys d WHERE d.namespace=? "
            "AND d.scope=c.repository AND d.key=c.command_id) ORDER BY c.created_at",
            (key.repository, f"pr:{key.target.number}", CommandKind.RELABEL.value,
             EffectStatus.PERMANENT_FAILURE.value, ESCALATION_PROJECTION_PREFIX + "%",
             _ESCALATION_RESOLVED_NAMESPACE),
        ).fetchall()

    def _unresolved_escalation(self, db: sqlite3.Connection, key: WorkflowKey) -> bool:
        return bool(self._refused_escalations(db, key))

    def _replan_escalations(self, key: WorkflowKey) -> str:
        """Resolve every refused escalation projection of this pull request from one fresh read.

        A ceiling's refused projection is the open gate's restoration's to
        repair (it is the same hold). A verdict's is re-planned while the
        escalation still applies, bounded, and surfaced as a visible
        `ReviewProjectionRefused` gate plus its operator signal when the
        bound is spent. Returns an unavailable read's error, else "".
        """
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            if not self._refused_escalations(db, key):
                return ""
        recorded = self._observe(key)
        if isinstance(recorded, PullRequestUnavailable):
            return recorded.reason or "pull request read unavailable"
        with self._control.transaction() as db:
            for row in self._refused_escalations(db, key):
                self._replan_escalation(db, key, _command_from_row(row), recorded)
        return ""

    def _replan_escalation(self, db: sqlite3.Connection, key: WorkflowKey, command, recorded: RecordedPullRequest) -> None:
        identity = escalation_projection_identity(command)
        if identity is None or not owns_command(db, self._writer, command.executor_identity):
            raise WorkflowAuthorityError("a refused escalation projection belongs to another executor")
        state = self._load(db, key)
        subject = f"pr:{key.target.number}"
        # A ceiling's projection names its gate, not a submission.
        submitted = db.execute("SELECT 1 FROM lifecycle_commands WHERE command_id=?", (identity,)).fetchone()
        receipt = load_bound_review_submission(db, CommandId(identity)) if submitted is not None else None
        error = db.execute("SELECT error FROM effect_receipts WHERE command_id=?", (str(command.command_id),)).fetchone()
        refusal = (error["error"] if error is not None and error["error"] else "escalation projection refused")[:512]

        def resolve(outcome: str, detail: dict) -> None:
            db.execute("INSERT OR IGNORE INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       (_ESCALATION_RESOLVED_NAMESPACE, self._writer.repository, str(command.command_id),
                        self._now().isoformat(), json.dumps({"outcome": outcome, **detail})))
            self._control._transition(db, "workflow_escalation", subject, f"escalation_{outcome}", {
                "command_id": str(command.command_id), "escalation": identity, "refusal": refusal, **detail})

        if not isinstance(receipt, ReviewSubmissionReceipt) or not receipt.escalated:
            resolve("owned_by_ceiling", {})  # restored by the open ceiling's hold restoration
            return
        replans = db.execute("SELECT COUNT(*) FROM lifecycle_commands WHERE repository=? AND idempotency_key LIKE ?",
                             (key.repository, ESCALATION_PROJECTION_PREFIX + identity + "%")).fetchone()[0] - 1
        decision = replan_escalation_hold(state, recorded.observation, receipt, replans=replans,
                                          trigger_label=self._trigger, executor_identity=self._writer.owner_id)
        if isinstance(decision, EscalationHoldPresent):
            # Someone else applied the hold: no receipt of ours will carry the signal.
            self._owe_signal(db, key, identity, "hold_verified")
            resolve("hold_verified", {})
        elif isinstance(decision, EscalationMoot):
            resolve("moot", {"reason": decision.reason})
        elif isinstance(decision, EscalationReprojected):
            evidence_id = self._insert_evidence(db, key, recorded)
            self._control._enqueue_command(db, decision.command)
            resolve("reprojected", {"replacement": str(decision.command.command_id), "attempt": replans + 1,
                                    "evidence_id": evidence_id})
        else:
            assert isinstance(decision, EscalationProjectionExhausted)
            gate_id = self._open_refused_gate(db, state, command, refusal, (
                "apply needs-human-review (and remove the trigger) by hand; the next harness read verifies "
                "the labels and releases this hold"))
            self._owe_signal(db, key, identity, "projection_exhausted")
            resolve("projection_exhausted", {"gate_id": gate_id, "replans": decision.replans})

    def _owe_signal(self, db: sqlite3.Connection, key: WorkflowKey, escalation: str, reason: str) -> None:
        owe_escalation_signal(db, executor_identity=self._writer.owner_id, escalation=escalation,
                              pr=key.target.number, reason=reason, at=self._now())

    def _open_refused_gate(self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot, command, reason: str,
                           remediation: str) -> str:
        binding = command.workflow
        gate_id = release_refused_gate_id(command.command_id)
        self._control._reconciliation_gate(db, binding, gate_id, OpenGate(
            gate_id, state.revision.fence.generation,
            ReviewProjectionRefused(str(command.command_id), binding.fence.generation, reason[:16384]),
            NoAuthorizedRemediation(remediation),
        ))
        return gate_id

    def _unsettled_projection(self, db: sqlite3.Connection, key: WorkflowKey) -> bool:
        for row in db.execute(
            "SELECT * FROM lifecycle_commands WHERE repository=? AND target=? AND kind=? "
            "AND workflow_binding_json IS NOT NULL AND state NOT IN ('succeeded','permanent_failure')",
            (key.repository, f"pr:{key.target.number}", CommandKind.RELABEL.value),
        ):
            if not isinstance(self._control._command_reconciliation(row), RetiredSettledCommand):
                return True
        return False

    def record_review(self, submission: CommandId) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        """Record a verified current-generation verdict as the reviewed baseline, at most once per receipt.

        An escalated verdict also needs a fresh read: its baseline commits
        together with the attention-hold projection that read guards.
        """
        with self._control.transaction() as db:
            bound = load_bound_review_submission(db, submission)
            if bound is None:
                return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE,
                                        "the submission has no verified bound review receipt")
            if not isinstance(bound, ReviewSubmissionReceipt):
                return WorkflowRejected(WorkflowRejection.INVALID_SCOPE, "an escalation is not a reviewed baseline")
            key = self._scope(bound.submission.binding.fence.key)
            recorded_submission = self._submission_recorded(db, str(submission))
            try:
                prior = self._load(db, key)
            except ReviewExecutionHeld as held:
                if recorded_submission:
                    return WorkflowCommitted(held.snapshot, idempotent=True)
                raise
            if recorded_submission:
                return WorkflowCommitted(prior, idempotent=True)
            if not bound.escalated:
                proposal = record_review(prior, bound)
                if isinstance(proposal, WorkflowRejected):
                    return proposal
                if isinstance(proposal, WorkflowUnchanged):
                    return WorkflowCommitted(prior, idempotent=True)
                return self._commit(db, prior.revision, (prior.authority,), proposal)
        recorded = self._observe(key)
        with self._control.transaction() as db:
            prior = self._load(db, key)
            if self._submission_recorded(db, str(submission)):
                return WorkflowCommitted(prior, idempotent=True)
            if isinstance(recorded, PullRequestUnavailable):
                return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, recorded.reason)
            escalated = record_escalated_review(
                prior, bound, recorded.observation, now=self._now(), trigger_label=self._trigger,
                executor_identity=self._writer.owner_id, evidence_ids=(self._evidence_id(recorded),),
            )
            return self._apply(db, prior, key, recorded, escalated)

    def _apply(
        self, db: sqlite3.Connection, prior: ReviewWorkflowSnapshot, key: WorkflowKey, recorded: RecordedPullRequest,
        proposal: WorkflowChanged | WorkflowUnchanged | WorkflowRejected,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        if isinstance(proposal, WorkflowRejected):
            if proposal.code is WorkflowRejection.STALE_REVISION:
                return WorkflowConflict(prior.revision, proposal.reason)
            return proposal
        if isinstance(proposal, WorkflowUnchanged):
            return WorkflowCommitted(prior, idempotent=True)
        return self._commit(db, prior.revision, (prior.authority,), proposal,
                            observation=recorded, stored_prior=prior)

    # -- operator acceptance -------------------------------------------------------------------

    @staticmethod
    def _operator_binding_conflict(
        state: ReviewWorkflowSnapshot,
        command: RequestRearm | ContinueGrantedRearm | CancelRearm | AuthorizeAbsentRetry,
        *, replay: bool = False,
    ) -> WorkflowConflict | None:
        binding = command.expected_binding
        if not isinstance(binding, WorkflowBinding) or binding.fence != command.expected.fence:
            return WorkflowConflict(state.revision, "review operator commands require their captured workflow binding")
        if binding.authority != state.authority or (not replay and binding.episode_id != state.episode.episode_id):
            return WorkflowConflict(state.revision, "the command names another family epoch or episode")
        return None

    def _request_replay(
        self, db: sqlite3.Connection, command: RequestRearm | ContinueGrantedRearm, actor: OperatorActor,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected | None:
        """Replay precedes any read: identical current request is idempotent, reuse conflicts, an older one is refused."""
        key = command.expected.fence.key
        receipt = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-request' AND scope=? AND key=?",
            (self._writer.repository, command.request_id),
        ).fetchone()
        if receipt is None:
            return None
        current = self._load(db, key)
        audit = accepted_operator_audit(receipt["payload_json"])
        if audit.command != command or audit.actor.subject != actor.subject:
            return WorkflowConflict(current.revision, "request identity was reused with different content or operator")
        if current.intent is None or current.intent.request_id != command.request_id:
            return WorkflowRejected(WorkflowRejection.INVALID_SCOPE,
                                    "request identity belongs to an older review re-arm that a newer decision replaced")
        return WorkflowCommitted(current, idempotent=True)

    def _active_review_runs(self, db: sqlite3.Connection, key: WorkflowKey) -> bool:
        """Bound runs of any generation, and unbound review legs admitted before activation.

        Once the family is active, admission refuses every unbound review leg,
        so the unbound half can only find runs admitted before the switch
        (ADR 0015: indexed bound queries cannot prove legacy quiescence).
        """
        lower, upper = workflow_run_scope_bounds(key)
        live = "(r.state IN ('reserved','starting','running') OR l.run_id IS NOT NULL OR b.state='active')"
        bound = db.execute(
            "SELECT 1 FROM runs r LEFT JOIN leases l USING(run_id) LEFT JOIN budget_reservations b USING(run_id) "
            f"WHERE r.workflow_episode_key>=? AND r.workflow_episode_key<? AND {live} LIMIT 1",
            (lower, upper),
        ).fetchone() is not None
        return bound or db.execute(
            "SELECT 1 FROM runs r LEFT JOIN leases l USING(run_id) LEFT JOIN budget_reservations b USING(run_id) "
            "WHERE r.workflow_episode_key IS NULL AND r.kind IN ('REVIEW','RE_REVIEW') "
            "AND json_valid(r.dispatch_id) AND json_extract(r.dispatch_id,'$.role')='review' "
            "AND json_extract(r.dispatch_id,'$.repository')=? AND json_extract(r.dispatch_id,'$.number')=? "
            f"AND {live} LIMIT 1",
            (key.repository, key.target.number),
        ).fetchone() is not None

    def _accept(
        self, command: RequestRearm | ContinueGrantedRearm, actor: OperatorActor,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        """Barriers, read-only refresh, then one short commit of intent, commands and outbox.

        The preflight is minted here from this acceptance's own complete read;
        no caller or operator input can supply it. Barriers are released
        before any effect runner may execute the accepted obligations.
        """
        if not isinstance(command, (RequestRearm, ContinueGrantedRearm)) or not isinstance(actor, OperatorActor):
            raise ValueError("review re-arm requires a typed command and authenticated operator")
        key = self._scope(command.expected.fence.key)
        with self._control.transaction() as db:
            current = self._load(db, key)
            conflict = self._operator_binding_conflict(current, command, replay=True)
            if conflict is not None:
                return conflict
            replayed = self._request_replay(db, command, actor)
        if replayed is not None:
            return replayed
        with self._barriers():
            with self._control.transaction() as db:
                current = self._load(db, key)
                conflict = self._operator_binding_conflict(current, command, replay=True)
                if conflict is not None:
                    return conflict
                replayed = self._request_replay(db, command, actor)
                if replayed is not None:
                    return replayed
                conflict = self._operator_binding_conflict(current, command)
                if conflict is not None:
                    return conflict
                if current.revision != command.expected:
                    return WorkflowConflict(current.revision, "workflow changed before re-arm acceptance")
            recorded = self._observe(key)
            with self._control.transaction() as db:
                prior = self._load(db, key)
                conflict = self._operator_binding_conflict(prior, command, replay=True)
                if conflict is not None:
                    return conflict
                replayed = self._request_replay(db, command, actor)
                if replayed is not None:
                    return replayed
                conflict = self._operator_binding_conflict(prior, command)
                if conflict is not None:
                    return conflict
                if prior.revision != command.expected:
                    return WorkflowConflict(prior.revision, "workflow changed before re-arm acceptance")
                if isinstance(recorded, PullRequestUnavailable):
                    return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, recorded.reason)
                if self._active_review_runs(db, key):
                    return WorkflowRejected(WorkflowRejection.OTHER_HOLD, "active review runs must drain before a re-arm")
                if (isinstance(command, RequestReviewExecutionRenewal)
                        and not isinstance(command, RequestReviewVerdictRelease)
                        and self._old_execution_episode_open(db, prior)):
                    return WorkflowRejected(WorkflowRejection.OTHER_HOLD,
                                            "every old execution episode round must settle before renewal")
                evidence_id = self._evidence_id(recorded)
                common = dict(now=self._now(), active_reviews=(), evidence_id=evidence_id,
                              trigger_label=self._trigger, executor_identity=self._writer.owner_id,
                              external_holds=recorded.external_holds,
                              applied_sequences=self._generation_applied_sequences(db, prior))
                if isinstance(command, RequestRearm):
                    try:
                        verdict = self._fixed_verdict_receipt(db, prior,
                            command.submission_command_id if isinstance(command, RequestReviewVerdictRelease) else None) \
                            if isinstance(command, RequestReviewExecutionRenewal) else None
                    except (WorkflowAuthorityError, ControlStoreError, ValueError, TypeError) as exc:
                        return WorkflowRejected(WorkflowRejection.INVALID_SCOPE,
                                                f"selected verdict evidence is unavailable: {_reason(exc)}")
                    proposal = request_review_rearm(
                        prior, command, actor, recorded.observation, **common,  # type: ignore[arg-type]
                        execution_ceiling=self._execution_ceiling, rearm_rule=self._rule,
                        verdict_receipt=verdict,
                    )
                else:
                    proposal = continue_review_rearm(prior, command, actor, recorded.observation, **common)  # type: ignore[arg-type]
                return self._apply(db, prior, key, recorded, proposal)

    request_rearm = _accept
    continue_rearm = _accept

    def cancel_rearm(self, command: CancelRearm, actor: OperatorActor) -> WorkflowCommitted | WorkflowConflict:
        if not isinstance(command, CancelRearm) or not isinstance(actor, OperatorActor):
            raise ValueError("review re-arm cancellation requires a typed command and actor")
        key = self._scope(command.expected.fence.key)
        with self._barriers():
            with self._control.transaction() as db:
                prior = self._load(db, key)
                conflict = self._operator_binding_conflict(prior, command, replay=True)
                if conflict is not None:
                    return conflict
                receipt = db.execute(
                    "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-cancellation' AND scope=? AND key=?",
                    (self._writer.repository, command.cancellation_id),
                ).fetchone()
                if receipt is not None:
                    audit = accepted_operator_audit(receipt["payload_json"])
                    if audit.command == command and audit.actor.subject == actor.subject:
                        return WorkflowCommitted(prior, idempotent=True)
                    return WorkflowConflict(prior.revision, "durable workflow identity was reused with different content")
                conflict = self._operator_binding_conflict(prior, command)
                if conflict is not None:
                    return conflict
                if prior.revision != command.expected or not isinstance(prior.intent, PendingReviewRearm):
                    return WorkflowConflict(prior.revision, "review re-arm cancellation requires exact pending authority")
                verified, _, uncertainty = self._rearm_command_facts(db, prior, None)
                proposal = cancel_review_rearm(prior, command, actor, verified_commands=verified, uncertainty=uncertainty)
                if isinstance(proposal, WorkflowRejected):
                    return WorkflowConflict(prior.revision, proposal.reason)
                if isinstance(proposal, WorkflowUnchanged):
                    return WorkflowCommitted(prior, idempotent=True)
                return self._commit(db, prior.revision, (prior.authority,), proposal, stored_prior=prior)

    def authorize_retry(
        self, command: AuthorizeAbsentRetry, actor: OperatorActor,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        """Persist one operator grant under every deployment barrier (the shared grant contract).

        The grant buys exactly one execution of a reconciliation-only review
        command whose current postcondition the harness proved absent. The
        executor spends it only after re-checking the command's own guard
        (dismissal check, hold guard, or a bound submission's reviewed head);
        the store makes the one spend decision with the resolve grant's
        disposition, ceiling and binding rules (ADR 0015).
        """
        if not isinstance(command, AuthorizeAbsentRetry) or not isinstance(actor, OperatorActor):
            raise ValueError("retry authorization requires a typed command and authenticated actor")
        key = self._scope(command.expected.fence.key)
        with self._barriers():
            with self._control.transaction() as db:
                prior = self._load(db, key)
                conflict = self._operator_binding_conflict(prior, command, replay=True)
                if conflict is not None:
                    return conflict
                replayed = retry_request_replay(db, prior, command, actor)
                if replayed is not None:
                    return replayed
                conflict = self._operator_binding_conflict(prior, command)
                if conflict is not None:
                    return conflict
                if prior.revision != command.expected:
                    return WorkflowConflict(prior.revision, "workflow changed before retry authorization")
            # A grant on a closed pull request would be spent on a certain
            # refusal. Only a read that finds it terminal refuses here; an
            # unavailable read leaves the decision to the executor's own guard.
            observed, _ = read_pull_request(self._gateway, key, self._now(), reviews=False)
            terminal = isinstance(observed, ObservedPullRequest) and observed.state is not PullRequestState.OPEN
            with self._control.transaction() as db:
                prior = self._load(db, key)
                conflict = self._operator_binding_conflict(prior, command, replay=True)
                if conflict is not None:
                    return conflict
                replayed = retry_request_replay(db, prior, command, actor)
                if replayed is not None:
                    return replayed
                conflict = self._operator_binding_conflict(prior, command)
                if conflict is not None:
                    return conflict
                if prior.revision != command.expected:
                    return WorkflowConflict(prior.revision, "workflow changed before retry authorization")
                if terminal:
                    return WorkflowRejected(WorkflowRejection.INVALID_SCOPE, "the pull request is terminal")
                gate = next((g for g in prior.gates if isinstance(g, OpenGate) and g.gate_id == command.gate_id), None)
                if gate is None or not isinstance(gate.cause, RetryAuthorizationRequired) or (
                    gate.cause.command_id, gate.cause.absence_evidence_id,
                ) != (command.command_id, command.evidence_id):
                    # Unknown commands and fabricated evidence stop here, before
                    # any command row is consulted.
                    return WorkflowRejected(WorkflowRejection.INVALID_SCOPE,
                                            "the named gate does not await this command's retry authorization")
                proposal = authorize_absent_retry(prior, command, actor, self._retry_facts(db, command))
                if isinstance(proposal, WorkflowRejected):
                    if proposal.code in (WorkflowRejection.STALE_REVISION, WorkflowRejection.AUTHORIZATION_OUTSTANDING):
                        return WorkflowConflict(prior.revision, proposal.reason)
                    return proposal
                assert isinstance(proposal, WorkflowChanged)
                return self._commit(db, prior.revision, (prior.authority,), proposal)

    # -- pending re-arm reconciliation ----------------------------------------------------------

    def pending_rearms(self, *, limit: int = 32) -> tuple[PendingReviewRecovery | UnavailableResolveRearm, ...]:
        """Due pending review re-arms; unreadable authority is reported and rescheduled, never skipped silently."""
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("re-arm reconciliation limit must be between 1 and 128")
        pending: list[PendingReviewRecovery | UnavailableResolveRearm] = []
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            now = self._now()
            rows = db.execute(
                "SELECT rearm.request_id,rearm.target_number FROM workflow_rearms rearm INDEXED BY workflow_one_pending_rearm "
                "LEFT JOIN dedupe_keys observation ON observation.namespace=? "
                "AND observation.scope=rearm.repository AND observation.key=rearm.request_id "
                "WHERE rearm.repository=? AND rearm.target_kind='pr' AND rearm.state='pending' "
                "AND COALESCE(julianday(observation.claimed_at),0)<=julianday(?) "
                "ORDER BY rearm.target_number,rearm.request_id LIMIT ?",
                (_OBSERVATION_NAMESPACE, self._writer.repository, now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                db.execute("SAVEPOINT pending_review_rearm")
                try:
                    key = WorkflowKey(self._writer.repository, PullRequestRef(row["target_number"]))
                    state = load_review_workflow_row(db, key)
                    if state is None or not isinstance(state.intent, PendingReviewRearm) or (
                        state.intent.request_id != row["request_id"]
                    ):
                        raise WorkflowAuthorityError("pending review re-arm index disagrees with current intent")
                    self._accepted_preflight(db, state)
                    observation = self._rearm_observation(db, state)
                    if observation.history.next_read_at <= now:
                        pending.append(PendingReviewRecovery(state, observation))
                except (ValueError, TypeError, KeyError) as exc:
                    db.execute("ROLLBACK TO pending_review_rearm")
                    pending.append(self._defer_unreadable(db, row["request_id"], _reason(exc), now))
                finally:
                    db.execute("RELEASE pending_review_rearm")
        return tuple(pending)

    def _defer_unreadable(
        self, db: sqlite3.Connection, request_id: str, reason: str, now: datetime,
    ) -> UnavailableResolveRearm:
        """Back one PR's unreadable re-arm authority off on its own schedule, journaled (ADR 0012)."""
        next_read = now + timedelta(hours=1)
        record_key = request_id
        try:
            validate_workflow_identifier(record_key, "re-arm request index")
        except ValueError:
            record_key = "invalid-index:" + hashlib.sha256(
                repr(record_key).encode("utf-8", "backslashreplace")).hexdigest()
        changed = db.execute(
            "UPDATE dedupe_keys SET claimed_at=? WHERE namespace=? AND scope=? AND key=?",
            (next_read.isoformat(), _OBSERVATION_NAMESPACE, self._writer.repository, request_id),
        ).rowcount
        if not changed:
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,NULL)",
                       (_OBSERVATION_NAMESPACE, self._writer.repository, request_id, next_read.isoformat()))
        self._control._transition(db, "workflow_rearm", record_key, "authority_unavailable",
                                  {"reason": reason, "next_read_at": next_read.isoformat()})
        return UnavailableResolveRearm(record_key, reason, next_read)

    def reconcile_rearm(self, expected: WorkflowRevision) -> WorkflowCommitted | WorkflowConflict:
        """Join obligation receipts with one fresh read: complete, supersede, keep waiting or defer.

        Runs under the exclusive barriers like acceptance: a supersession
        retires obligations, so no obligation may be mid-write. An unavailable
        or undecidable read backs off on the intent's schedule, is journaled,
        and at the ceiling opens a visible `EvidenceUnavailable` hold.
        """
        if not isinstance(expected, WorkflowRevision):
            raise ValueError("review re-arm reconciliation requires a typed revision")
        key = self._scope(expected.fence.key)
        with self._barriers():
            with self._control.transaction() as db:
                current = self._load(db, key)
                if current.revision != expected:
                    return WorkflowConflict(current.revision, "workflow changed before reconciliation")
                if not isinstance(current.intent, PendingReviewRearm):
                    raise WorkflowAuthorityError("review re-arm reconciliation requires a pending intent")
            recorded = self._observe(key)
            with self._control.transaction() as db:
                prior = self._load(db, key)
                if prior.revision != expected or not isinstance(prior.intent, PendingReviewRearm):
                    return WorkflowConflict(prior.revision, "workflow changed during reconciliation")
                previous = self._rearm_observation(db, prior)
                if isinstance(recorded, PullRequestUnavailable):
                    return self._defer(db, prior, previous, recorded.reason)
                now = self._now()
                evidence_id = self._insert_evidence(db, key, recorded)
                # A successful read ends this intent's own read-failure hold first.
                working = update_review_rearm_observation(prior, None, now=now, policy=self._policy)
                if working != prior:
                    persist_workflow_snapshot(db, working)
                verified, rejected, uncertainty = self._rearm_command_facts(db, working, recorded.observation)
                proposal = decide_review_rearm_reconciliation(
                    working, self._accepted_preflight(db, working), recorded.observation,
                    verified_commands=verified, rejected_commands=rejected, uncertainty=uncertainty,
                    evidence_id=evidence_id, now=now, ceiling=self._ceiling, trigger_label=self._trigger,
                    external_holds=recorded.external_holds,
                )
                if isinstance(proposal, WorkflowRejected):
                    # Completion refused by fresh facts: an undecidable read, bounded and then visible.
                    return self._defer(db, working, previous, f"completion refused: {proposal.reason}")
                if isinstance(proposal, WorkflowChanged):
                    return self._commit(db, working.revision, (working.authority,), proposal)
                scheduled = schedule_rearm_observation(previous, now=now, policy=self._policy)
                self._write_schedule(db, working, scheduled)
                return WorkflowCommitted(working)

    def reconcile_pending_rearms(self, *, limit: int = 32) -> int:
        """Drive every due pending review re-arm once; returns how many progressed.

        One PR's inconsistent authority never stops the pass: its re-arm is
        deferred on its own bounded schedule and journaled, and every other
        PR is still reconciled.
        """
        progressed = 0
        for candidate in self.pending_rearms(limit=limit):
            if not isinstance(candidate, PendingReviewRecovery):
                continue
            try:
                result = self.reconcile_rearm(candidate.snapshot.revision)
            except (WorkflowAuthorityError, ControlStoreError) as exc:
                with self._control.transaction() as db:
                    self._defer_unreadable(db, candidate.observation.request_id, _reason(exc), self._now())
                continue
            if isinstance(result, WorkflowCommitted) and result.snapshot.intent != candidate.snapshot.intent:
                progressed += 1
        return progressed

    def _write_schedule(self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot, observation: RearmObservation) -> None:
        assert isinstance(state.intent, PendingReviewRearm)
        changed = db.execute(
            "UPDATE dedupe_keys SET claimed_at=?,payload_json=? WHERE namespace=? AND scope=? AND key=?",
            (observation.history.next_read_at.isoformat(), encode_workflow(observation), _OBSERVATION_NAMESPACE,
             self._writer.repository, state.intent.request_id),
        ).rowcount
        if changed != 1:
            raise WorkflowAuthorityError("review re-arm observation schedule changed during reconciliation")

    def _defer(
        self, db: sqlite3.Connection, state: ReviewWorkflowSnapshot, previous: RearmObservation, error: str,
    ) -> WorkflowCommitted:
        now = self._now()
        deferred = defer_rearm_observation(previous, error[:16384] or "review re-arm read unavailable", now=now,
                                           policy=self._policy)
        after = update_review_rearm_observation(state, deferred, now=now, policy=self._policy)
        self._write_schedule(db, state, deferred)
        if after != state:
            persist_workflow_snapshot(db, after)
        assert isinstance(state.intent, PendingReviewRearm)
        self._control._transition(db, "workflow_rearm", state.intent.request_id, "observation_deferred", {
            "failed_reads": deferred.history.failed_reads,
            "next_read_at": deferred.history.next_read_at.isoformat(),
            "gated": any((g.gate if isinstance(g, RearmingGate) else g).cause.__class__.__name__ == "EvidenceUnavailable"
                         for g in after.gates),
            "error": error[:512],
        })
        return WorkflowCommitted(after)

    # -- ceiling-release projection ------------------------------------------------------------

    @staticmethod
    def _refused_gates(state: ReviewWorkflowSnapshot) -> dict[str, OpenGate]:
        return {g.cause.command_id: g for g in state.gates
                if isinstance(g, OpenGate) and isinstance(g.cause, ReviewProjectionRefused)}

    def reconcile_release(self, key: WorkflowKey) -> ReleaseProjection | None:
        """Make a refused release projection visible, and end each such hold on fresh evidence.

        The executor settles a refused relabel as PERMANENT_FAILURE; this opens
        `ReviewProjectionRefused` so dispatch is visibly held. Every open
        refused gate is reconciled by its OWN recorded command, whatever the
        current generation: a fresh complete read showing the labels that
        command planned (an operator corrected them by hand) or a terminal
        pull request verifies it and closes the hold. Uncertain relabels keep
        the existing `RemoteOutcomeUncertain` machinery. Returns the current
        generation's release fact.
        """
        self._scope(key)
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            state = load_review_workflow_row(db, key)
            release = self._release_projection(db, state) if state is not None else None
            refused = self._refused_gates(state) if state is not None else {}
        if not refused and (release is None or release.status is not ReleaseStatus.REFUSED):
            return release
        recorded = self._observe(key)
        with self._control.transaction() as db:
            state = self._load(db, key)
            release = self._release_projection(db, state)
            refused = self._refused_gates(state)
            targets = dict.fromkeys(refused)
            if release is not None and release.status is ReleaseStatus.REFUSED:
                targets.setdefault(str(release.command_id), None)
            for command_id in targets:
                self._reconcile_refused(db, key, state, command_id, refused.get(command_id), recorded)
            return self._release_projection(db, self._load(db, key))

    def _reconcile_refused(
        self, db: sqlite3.Connection, key: WorkflowKey, state: ReviewWorkflowSnapshot, command_id: str,
        gate: OpenGate | None, recorded: RecordedPullRequest | PullRequestUnavailable,
    ) -> None:
        row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            raise WorkflowAuthorityError("a refused release gate names no durable command")
        command = _command_from_row(row)
        op = review_family_op(command)
        binding = command.workflow
        if (binding is None or binding.fence.key != key or not isinstance(op, PullRequestProjectionOp)
                or not owns_command(db, self._writer, command.executor_identity)):
            raise WorkflowAuthorityError("a refused release gate names another workflow's projection")
        gate_id = release_refused_gate_id(command.command_id)
        if isinstance(recorded, RecordedPullRequest) and isinstance(recorded.observation.reviews, CompleteReviews):
            pr = recorded.observation
            planned = op.add in pr.labels and (op.remove is None or op.remove not in pr.labels)
            # An escalation holds one head: a new head is owed its own review.
            superseded = is_review_escalation_projection(command) and pr.head_sha != op.guard.head_sha
            if planned or superseded or pr.state is not PullRequestState.OPEN:
                evidence_id = self._insert_evidence(db, key, recorded)
                db.execute("INSERT OR IGNORE INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                           "VALUES (?,?,?,?,?)", (_RELEASE_RESOLVED_NAMESPACE, self._writer.repository, command_id,
                                                  self._now().isoformat(), evidence_id))
                if gate is not None:
                    self._control._reconciliation_gate(db, binding, gate_id, None)
                self._control._transition(db, "workflow_release", command_id, "verified_by_observation", {
                    "evidence_id": evidence_id, "terminal": pr.state is not PullRequestState.OPEN,
                    "superseded": superseded, "generation": binding.fence.generation,
                })
                return
        if gate is None:
            error = db.execute("SELECT error FROM effect_receipts WHERE command_id=?", (command_id,)).fetchone()
            reason = (error["error"] if error is not None and error["error"] else "release projection refused")
            planned = (f"remove {op.remove.value!r} and add {op.add.value!r}" if op.remove is not None
                       else f"add {op.add.value!r}")
            self._open_refused_gate(db, state, command, reason, (
                f"correct the pull request labels by hand ({planned}); the next harness read verifies "
                "them and releases this hold"))
            self._control._transition(db, "workflow_release", command_id, "refusal_gated",
                                      {"gate_id": gate_id, "generation": binding.fence.generation})

    # -- runtime: activation, settled submissions, holds and evidence --------------------------

    def family_active(self) -> bool:
        """Whether this repository's review family is activated; never initializes it."""
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            self._control._assert_owner_active(db, self._writer.owner_id)
            return load_family_authority(db, WorkflowFamily.REVIEW_REJECTIONS, self._writer.repository) is not None

    def _submission_settled(self, db: sqlite3.Connection, command_id: str) -> bool:
        return review_submission_settled(db, self._writer.repository, command_id)

    def settle_submission(
        self, submission: CommandId,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected | None:
        """Apply one settled bound review submission to its workflow exactly once.

        The durable consequence of every submission is decided here, from its
        SUCCEEDED receipt, never by the dispatch that posted it: a verdict
        becomes the reviewed baseline (RECORD_REVIEW); an ESCALATE verdict
        becomes the baseline plus the needs-human-review projection; a ceiling
        escalation opens its gate plus that projection. Each commits in one
        transaction, and the projection is a workflow-owned, hold-guarded
        command the executor drains. Both the submitting review and the
        reconciliation pass call this, so a crash or a lost reply anywhere
        between the write and the relabel neither loses the hold nor re-buys
        the review. A fresh read that refuses the escalation (the head moved,
        or a human dismissed a counted review) settles with no hold at all.
        An unavailable read is retried on the bounded review-history
        schedule, which gates visibly at its ceiling. Returns None when
        nothing is (yet) settled to apply.
        """
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            if self._submission_settled(db, str(submission)):
                return None
            row = db.execute("SELECT executor_identity FROM lifecycle_commands WHERE command_id=?",
                             (str(submission),)).fetchone()
            if row is None or not owns_command(db, self._writer, row["executor_identity"]):
                return None
            bound = load_bound_review_submission(db, submission)
        if bound is None:
            return None
        key = self._scope(bound.submission.binding.fence.key)
        result: WorkflowCommitted | WorkflowConflict | WorkflowRejected
        stale_submission = False
        if isinstance(bound, ReviewSubmissionReceipt):
            stale_round_id: str | None = None
            active_round_id: str | None = None
            superseded_finalized = False
            planning: ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned = ReviewPlanningOpen()
            with closing(self._control._connect()) as db:
                db.execute("BEGIN")
                current = load_review_workflow_row(db, key)
                if current is not None and isinstance(current.execution, CurrentReviewExecution):
                    registration = read_review_submission_registration(db, self._writer.repository, str(submission))
                    if registration is None:
                        raise WorkflowAuthorityError("active review verdict lacks its originating round")
                    planning_row = db.execute(
                        "SELECT planning_json FROM review_execution_rounds WHERE round_id=?",
                        (registration.round.round_id,),
                    ).fetchone()
                    if planning_row is None:
                        raise WorkflowAuthorityError("active review verdict lost its originating round")
                    active_round_id = registration.round.round_id
                    superseded_finalized = db.execute(
                        "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                        (REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE, self._writer.repository, active_round_id),
                    ).fetchone() is not None
                    planning = decode_workflow(
                        planning_row["planning_json"], ReviewParentAbandoned | ReviewPlanningSealed
                        | ReviewPlanningOpen,
                    )
                    if isinstance(planning, ReviewPlanningOpen):
                        return None
                    stale_submission = (
                        bound.submission.binding.fence.generation < current.revision.fence.generation
                    )
                    if stale_submission:
                        stale_round_id = registration.round.round_id
            if stale_round_id is not None:
                from .review_execution_control import SQLiteReviewExecutionControl

                execution = SQLiteReviewExecutionControl(self._control, self._writer, self._gateway)
                finalized = (
                    execution.finalize_completed_round(stale_round_id)
                    if isinstance(planning, ReviewPlanningSealed)
                    else execution.finalize_abandoned_round(stale_round_id)
                )
                if not isinstance(finalized, FinalizedReviewRound):
                    return None
                result = WorkflowRejected(WorkflowRejection.INVALID_SCOPE,
                                          "older-generation review verdict has no baseline authority")
            elif superseded_finalized:
                result = WorkflowRejected(WorkflowRejection.INVALID_SCOPE,
                                          "superseded review verdict has no baseline authority")
            else:
                result = self.record_review(submission)
            if (active_round_id is not None and not stale_submission and not superseded_finalized
                    and isinstance(result, WorkflowRejected)):
                if result.code is not WorkflowRejection.INVALID_SCOPE or not bound.escalated:
                    return result
                observed = self._observe(key)
                if isinstance(observed, PullRequestUnavailable):
                    self.record_evidence(key, bound.head_sha, ReviewEvidenceKind.REVIEW_HISTORY,
                                         error=observed.reason)
                    return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, observed.reason)
                age = (self._now() - observed.observation.observed_at).total_seconds()
                if not 0 <= age <= OBSERVATION_MAX_AGE_SECONDS:
                    self.record_evidence(key, bound.head_sha, ReviewEvidenceKind.REVIEW_HISTORY,
                                         error="supersession observation expired before settlement")
                    return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE,
                                            "supersession observation expired before settlement")
                if (observed.observation.head_sha == bound.head_sha
                        and observed.observation.state is PullRequestState.OPEN):
                    return result
                from .review_execution_control import SQLiteReviewExecutionControl

                with self._control.transaction() as db:
                    current = self._load(db, key)
                    evidence_id = self._insert_evidence(db, key, observed)
                    SQLiteReviewExecutionControl(self._control, self._writer, self._gateway)._settle_productive_in_tx(
                        db, str(submission), current, superseded_evidence_id=evidence_id,
                    )
        else:
            stale_round_id: str | None = None
            ceiling_replay: CeilingProductivityDecision | None = None
            planning: ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned = ReviewPlanningOpen()
            with closing(self._control._connect()) as db:
                db.execute("BEGIN")
                current = load_review_workflow_row(db, key)
                if current is not None and isinstance(current.execution, CurrentReviewExecution):
                    registration = read_review_submission_registration(db, self._writer.repository, str(submission))
                    if registration is None:
                        raise WorkflowAuthorityError("active ceiling escalation lacks its originating round")
                    planning_row = db.execute(
                        "SELECT planning_json FROM review_execution_rounds WHERE round_id=?",
                        (registration.round.round_id,),
                    ).fetchone()
                    if planning_row is None:
                        raise WorkflowAuthorityError("active ceiling escalation lost its originating round")
                    planning = decode_workflow(
                        planning_row["planning_json"], ReviewParentAbandoned | ReviewPlanningSealed
                        | ReviewPlanningOpen,
                    )
                    if isinstance(planning, ReviewPlanningOpen):
                        return None
                    receipt = db.execute(
                        "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                        (REVIEW_CEILING_PRODUCTIVITY_NAMESPACE, self._writer.repository,
                         registration.round.round_id),
                    ).fetchone()
                    if receipt is not None:
                        captured = decode_workflow(receipt["payload_json"], tuple)
                        if (len(captured) != 3 or not isinstance(captured[0], CeilingProductivityDecision)
                                or captured[0].escalation != bound):
                            raise WorkflowAuthorityError("ceiling escalation replay disagrees with its round")
                        ceiling_replay = captured[0]
                    stale_submission = (
                        bound.submission.binding.fence.generation < current.revision.fence.generation
                    )
                    if stale_submission and ceiling_replay is None:
                        stale_round_id = registration.round.round_id
            if ceiling_replay is not None:
                result = (ceiling_replay.refusal if ceiling_replay.refusal is not None
                          else WorkflowCommitted(current, idempotent=True))
            elif stale_round_id is not None:
                from .review_execution_control import SQLiteReviewExecutionControl

                execution = SQLiteReviewExecutionControl(self._control, self._writer, self._gateway)
                finalized = (
                    execution.finalize_completed_round(stale_round_id)
                    if isinstance(planning, ReviewPlanningSealed)
                    else execution.finalize_abandoned_round(stale_round_id)
                )
                if not isinstance(finalized, FinalizedReviewRound):
                    return None
                result = WorkflowRejected(WorkflowRejection.INVALID_SCOPE,
                                          "older-generation ceiling escalation has no gate authority")
            else:
                result = self.open_ceiling(submission)
        if isinstance(result, WorkflowRejected) and result.code is WorkflowRejection.OBSERVATION_UNAVAILABLE:
            self.record_evidence(key, bound.head_sha, ReviewEvidenceKind.REVIEW_HISTORY,
                                 error=result.reason or "pull request read unavailable")
            return result  # retried by a later reconciliation pass on its schedule
        if isinstance(result, WorkflowConflict):
            return result  # retried by the next reconciliation pass
        if self._reads_review_history(bound) and not stale_submission:
            self.record_evidence(key, bound.head_sha, ReviewEvidenceKind.REVIEW_HISTORY, error=None)
        outcome = ("applied" if isinstance(result, WorkflowCommitted) and not result.idempotent
                   else "unchanged" if isinstance(result, WorkflowCommitted)
                   else f"refused: {result.code.value}")
        with self._control.transaction() as db:
            db.execute("INSERT OR IGNORE INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       (_SETTLED_SUBMISSION_NAMESPACE, self._writer.repository, str(submission),
                        self._now().isoformat(), json.dumps({"outcome": outcome})))
            self._control._transition(db, "review_submission", str(submission), "settled", {
                "outcome": outcome, "reason": result.reason[:512] if isinstance(result, WorkflowRejected) else "",
                "escalation": ("ceiling" if isinstance(bound, CeilingEscalation)
                               else "verdict" if bound.escalated else ""),
            })
        return result

    @staticmethod
    def _reads_review_history(bound: ReviewSubmissionReceipt | CeilingEscalation) -> bool:
        return isinstance(bound, CeilingEscalation) or bound.escalated

    def reconcile_submissions(self, *, limit: int = 32) -> int:
        """Settle bound review submissions whose receipt landed without being applied.

        Covers a crash after the receipt and a submission the drain settled
        after an unknown outcome. A settlement that needs a fresh read waits
        for that pull request's review-history schedule, so an unreadable
        pull request costs a bounded number of reads. Returns how many were
        applied.
        """
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("submission reconciliation limit must be between 1 and 128")
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            self._control._assert_owner_active(db, self._writer.owner_id)
            rows = db.execute(
                "SELECT c.command_id FROM lifecycle_commands c WHERE c.repository=? AND c.kind=? AND c.state=? "
                "AND c.workflow_binding_json IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dedupe_keys d "
                "WHERE d.namespace IN (?, ?) AND d.scope=c.repository AND d.key=c.command_id) "
                "ORDER BY c.created_at, c.command_id LIMIT ?",
                (self._writer.repository, CommandKind.SUBMIT_REVIEW.value, EffectStatus.SUCCEEDED.value,
                 _SETTLED_SUBMISSION_NAMESPACE, _RECORDED_REVIEW_NAMESPACE, limit),
            ).fetchall()
        applied = 0
        for row in rows:
            try:
                with closing(self._control._connect()) as db:
                    db.execute("BEGIN")
                    bound = load_bound_review_submission(db, CommandId(row["command_id"]))
                if bound is not None and self._reads_review_history(bound):
                    with closing(self._control._connect()) as db:
                        db.execute("BEGIN")
                        snapshot = load_review_workflow_row(db, bound.submission.binding.fence.key)
                        registration = read_review_submission_registration(
                            db, self._writer.repository, row["command_id"],
                        )
                        finalized_productivity = registration is not None and db.execute(
                            "SELECT 1 FROM dedupe_keys WHERE namespace IN (?, ?) AND scope=? AND key=?",
                            (REVIEW_SUPERSEDED_PRODUCTIVITY_NAMESPACE, REVIEW_CEILING_PRODUCTIVITY_NAMESPACE,
                             self._writer.repository, registration.round.round_id),
                        ).fetchone() is not None
                    stale = (snapshot is not None
                             and bound.submission.binding.fence.generation < snapshot.revision.fence.generation)
                    if not stale and not finalized_productivity and not self.evidence_due(
                        bound.submission.binding.fence.key, bound.head_sha, ReviewEvidenceKind.REVIEW_HISTORY,
                    ):
                        continue
                result = self.settle_submission(CommandId(row["command_id"]))
            except (WorkflowAuthorityError, ControlStoreError, KeyError) as exc:
                with self._control.transaction() as db:
                    self._control._transition(db, "review_submission", row["command_id"], "settle_unavailable",
                                              {"reason": _reason(exc)[:512]})
                continue
            applied += isinstance(result, WorkflowCommitted) and not result.idempotent
        return applied

    # Holds whose fresh-read reconciliation this pass drives (ADR 0015 review slice).
    def _hold_candidates(self, db: sqlite3.Connection) -> tuple[int, ...]:
        numbers = {row[0] for row in db.execute(
            "SELECT DISTINCT target_number FROM workflow_gates WHERE repository=? AND target_kind='pr' "
            "AND state IN ('open','rearming')", (self._writer.repository,),
        )}
        for row in db.execute(
            "SELECT c.command_id, w.target_number, w.generation FROM lifecycle_commands c JOIN workflows w "
            "ON w.repository=c.repository AND w.target_kind='pr' AND c.target='pr:'||w.target_number "
            "WHERE c.repository=? AND c.kind=? AND c.state=? AND c.idempotency_key LIKE 'workflow:release:%' "
            "AND NOT EXISTS (SELECT 1 FROM dedupe_keys d WHERE d.namespace=? AND d.scope=c.repository "
            "AND d.key=c.command_id)",
            (self._writer.repository, CommandKind.RELABEL.value, EffectStatus.PERMANENT_FAILURE.value,
             _RELEASE_RESOLVED_NAMESPACE),
        ):
            key = WorkflowKey(self._writer.repository, PullRequestRef(row["target_number"]))
            if row["generation"] >= 1 and str(review_release_command_id(key, row["generation"])) == row["command_id"]:
                numbers.add(row["target_number"])
        for row in db.execute(
            "SELECT DISTINCT c.target FROM lifecycle_commands c WHERE c.repository=? AND c.kind=? AND c.state=? "
            "AND c.idempotency_key LIKE ? AND NOT EXISTS (SELECT 1 FROM dedupe_keys d WHERE d.namespace=? "
            "AND d.scope=c.repository AND d.key=c.command_id)",
            (self._writer.repository, CommandKind.RELABEL.value, EffectStatus.PERMANENT_FAILURE.value,
             ESCALATION_PROJECTION_PREFIX + "%", _ESCALATION_RESOLVED_NAMESPACE),
        ):
            kind, _, number = row["target"].partition(":")
            if kind == "pr" and number.isdigit():
                numbers.add(int(number))
        return tuple(sorted(numbers))

    def _hold_schedule(self, db: sqlite3.Connection, number: int) -> tuple[datetime, int] | None:
        row = db.execute(
            f"SELECT claimed_at, payload_json FROM dedupe_keys WHERE namespace='{_HOLD_OBSERVATION_NAMESPACE}' "
            "AND scope=? AND key=?", (self._writer.repository, f"pr:{number}"),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
            return datetime.fromisoformat(row["claimed_at"]), int(payload["failed_reads"])
        except (ValueError, TypeError, KeyError):
            return None  # an unreadable schedule is due, never a silent skip

    def _write_hold_schedule(self, number: int, failed_reads: int, delay: float, error: str) -> None:
        now = self._now()
        with self._control.transaction() as db:
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?) "
                "ON CONFLICT(namespace,scope,key) DO UPDATE SET claimed_at=excluded.claimed_at,"
                "payload_json=excluded.payload_json",
                (_HOLD_OBSERVATION_NAMESPACE, self._writer.repository, f"pr:{number}",
                 (now + timedelta(seconds=delay)).isoformat(),
                 json.dumps({"failed_reads": failed_reads, "error": error[:512]})),
            )
            if error:
                self._control._transition(db, "workflow_hold", f"pr:{number}", "observation_deferred", {
                    "failed_reads": failed_reads, "error": error[:512], "delay_seconds": delay,
                })

    def reconcile_holds(self, *, limit: int = 16) -> int:
        """Drive every due review hold from one fresh read: ceilings, refused releases, evidence.

        A ceiling whose cause is now false is released (RELEASE_CEILING) and
        one still true is refreshed; a refused release projection becomes a
        visible gate until a read verifies it; an evidence hold is
        re-read on its own schedule. Bounded by `limit` per pass and by a
        per-pull-request schedule that backs off on unavailable reads, so a
        held or terminal pull request never becomes a tight polling loop.
        Must run outside every effect runner's shared authority: ceiling
        reconciliation takes the exclusive deployment barriers.
        """
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("hold reconciliation limit must be between 1 and 128")
        now = self._now()
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            self._control._assert_owner_active(db, self._writer.owner_id)
            due = [number for number in self._hold_candidates(db)
                   if (schedule := self._hold_schedule(db, number)) is None or schedule[0] <= now][:limit]
            prior = {number: self._hold_schedule(db, number) for number in due}
        progressed = 0
        for number in due:
            key = WorkflowKey(self._writer.repository, PullRequestRef(number))
            reads = (prior[number] or (now, 0))[1]
            try:
                changed, error, terminal = self._reconcile_hold(key)
            except (WorkflowAuthorityError, ControlStoreError, ValueError) as exc:
                changed, error, terminal = False, _reason(exc), False
            progressed += changed
            if changed:
                # Progress may enable the next step (a released ceiling's
                # projection, a refreshed gate): look again soon.
                self._write_hold_schedule(number, 0, self._policy.initial_delay_seconds, "")
                continue
            # An unavailable or an unchanged read backs off, so a hold that
            # sits for days costs a bounded number of complete reads.
            reads = min(reads + 1, 2**31)
            delay = (self._policy.maximum_delay_seconds if terminal else
                     min(self._policy.maximum_delay_seconds,
                         self._policy.initial_delay_seconds * 2 ** min(reads - 1, 17)))
            self._write_hold_schedule(number, reads, delay, error)
        return progressed

    def _reconcile_hold(self, key: WorkflowKey) -> tuple[bool, str, bool]:
        """(changed, unavailable-read error or "", pull request terminal) for one held pull request."""
        state = self.load_workflow(key)
        if not isinstance(state, ReviewWorkflowSnapshot):
            return False, (state.reason if isinstance(state, WorkflowUnavailable) else ""), False
        changed = False
        self.reconcile_release(key)
        after = self.load_workflow(key)
        changed |= after != state
        if not isinstance(after, ReviewWorkflowSnapshot):
            return changed, "", False
        error = self._replan_escalations(key)
        if error:
            return changed, error, False
        stopped = isinstance(after.intent, (SupersededReviewRearm, CancelledReviewRearm))
        ceilings = any(isinstance(g, OpenGate) and isinstance(g.cause, ReviewRejectionCeiling) for g in after.gates)
        terminal = False
        if not isinstance(after.intent, PendingReviewRearm) and (ceilings or stopped):
            # A restoration is the harness's own write: it never resets the backoff.
            result, _restored = self._reconcile_ceiling(key)
            if isinstance(result, WorkflowRejected):
                if result.code is WorkflowRejection.OBSERVATION_UNAVAILABLE:
                    return changed, result.reason or "pull request read unavailable", False
                terminal = "terminal" in result.reason or "verified absent" in result.reason
            elif isinstance(result, WorkflowCommitted):
                changed |= not result.idempotent
        current = self.load_workflow(key)
        if isinstance(current, ReviewWorkflowSnapshot):
            for kind in dict.fromkeys(k for g in current.gates if (k := review_evidence_gate_kind(g)) is not None):
                evidence_changed, error = self._reread_evidence(current, kind)
                changed |= evidence_changed
                if error:
                    return changed, error, False
        return changed, "", terminal

    # -- unreadable evidence (the ADR 0012/0015 UNVERIFIABLE loops) ------------------------------

    def _evidence_schedule(
        self, db: sqlite3.Connection, key: WorkflowKey, kind: ReviewEvidenceKind,
    ) -> EvidenceSchedule | None:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (_EVIDENCE_NAMESPACES[kind], self._writer.repository, f"pr:{key.target.number}"),
        ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row["payload_json"])
            return EvidenceSchedule(raw["head"], ActiveReconciliation(
                datetime.fromisoformat(raw["since"]), datetime.fromisoformat(raw["next"]), int(raw["reads"])),
                raw["error"])
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowAuthorityError(f"{kind.value} schedule is malformed") from exc

    def evidence_due(self, key: WorkflowKey, head_sha: str, kind: ReviewEvidenceKind) -> bool:
        """Whether this head's evidence of this kind may be read now (its backoff has elapsed)."""
        self._scope(key)
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            schedule = self._evidence_schedule(db, key, kind)
        return evidence_due(schedule, head_sha, now=self._now())

    def record_evidence(self, key: WorkflowKey, head_sha: str, kind: ReviewEvidenceKind, *, error: str | None) -> None:
        """Persist one bounded evidence read and its visible gate atomically."""
        self._scope(key)
        with self._control.transaction() as db:
            self._record_evidence_in_tx(db, key, head_sha, kind, error=error)

    def _record_evidence_in_tx(
        self, db: sqlite3.Connection, key: WorkflowKey, head_sha: str,
        kind: ReviewEvidenceKind, *, error: str | None,
    ) -> None:
        """Share the existing evidence schedule with a round's outcome transaction."""
        self._scope(key)
        namespace, scope = _EVIDENCE_NAMESPACES[kind], f"pr:{key.target.number}"
        state = self._load(db, key)
        prior = self._evidence_schedule(db, key, kind)
        now = self._now()
        held = [(g.gate if isinstance(g, RearmingGate) else g).gate_id for g in state.gates
                if review_evidence_gate_kind(g) is kind]
        if error is None:
            if prior is None and not held:
                return
            db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                       (namespace, self._writer.repository, scope))
            for stale_id in held:
                self._control._reconciliation_gate(db, state.binding, stale_id, None)
            self._control._transition(db, "workflow", encode_workflow(key), f"{kind.value}_available",
                                      {"head": head_sha, "closed_gates": len(held)})
            return
        progress = unavailable_evidence(kind, state.binding, head_sha, prior, now=now, error=error,
                                        policy=(replace(self._policy, maximum_reads=1)
                                                    if kind is ReviewEvidenceKind.RUNTIME_PREPARATION else self._policy))
        gate_id = evidence_gate_id(kind, key.target.number, head_sha)
        schedule = progress.schedule
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?) "
            "ON CONFLICT(namespace,scope,key) DO UPDATE SET claimed_at=excluded.claimed_at,"
            "payload_json=excluded.payload_json",
            (namespace, self._writer.repository, scope, schedule.history.next_read_at.isoformat(), json.dumps({
                "head": schedule.head_sha, "since": schedule.history.unresolved_since.isoformat(),
                "next": schedule.history.next_read_at.isoformat(), "reads": schedule.history.failed_reads,
                "error": schedule.last_error})),
        )
        # Another head's hold is a different, now obsolete, question.
        for stale_id in held:
            if stale_id != gate_id:
                self._control._reconciliation_gate(db, state.binding, stale_id, None)
        if progress.gate is not None:
            self._control._reconciliation_gate(db, state.binding, gate_id, progress.gate)
        self._control._transition(db, "workflow", encode_workflow(key), f"{kind.value}_unavailable", {
            "head": head_sha, "failed_reads": schedule.history.failed_reads,
            "next_read_at": schedule.history.next_read_at.isoformat(), "gated": progress.gate is not None,
            "error": schedule.last_error[:512],
        })

    def _record_runtime_evidence_if_current(
        self, state: ReviewWorkflowSnapshot, schedule: EvidenceSchedule | None,
        head_sha: str, *, error: str | None,
    ) -> bool:
        key = state.revision.fence.key
        with self._control.transaction() as db:
            current = self._load(db, key)
            if (current.binding != state.binding
                    or self._evidence_schedule(db, key, ReviewEvidenceKind.RUNTIME_PREPARATION) != schedule):
                return False
            self._record_evidence_in_tx(db, key, head_sha, ReviewEvidenceKind.RUNTIME_PREPARATION, error=error)
        return True

    def _reread_evidence(self, state: ReviewWorkflowSnapshot, kind: ReviewEvidenceKind) -> tuple[bool, str]:
        """The scheduled read that alone may release an unreadable-evidence hold."""
        key = state.revision.fence.key
        if kind is ReviewEvidenceKind.EXEMPTION_TRAILERS:
            # Retired ADR 0014 holds can still be present in persisted workflows.
            self.record_evidence(key, "", kind, error=None)
            return True, ""
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            schedule = self._evidence_schedule(db, key, kind)
        if schedule is not None and not evidence_due(schedule, schedule.head_sha, now=self._now()):
            return False, ""
        history = kind is ReviewEvidenceKind.REVIEW_HISTORY
        observed, view = read_pull_request(self._gateway, key, self._now(), reviews=history)
        if isinstance(observed, PullRequestUnavailable):
            if kind in {ReviewEvidenceKind.REVIEW_HISTORY, ReviewEvidenceKind.LINKED_ISSUE} and schedule is not None:
                # The evidence schedule is this hold's backoff; its failure is journalled there.
                self.record_evidence(key, schedule.head_sha, kind, error=observed.reason or "pull request unavailable")
                return False, ""
            return False, observed.reason or "pull request read unavailable"
        held = {(g.gate if isinstance(g, RearmingGate) else g).gate_id for g in state.gates
                if review_evidence_gate_kind(g) is kind}
        if observed.state is not PullRequestState.OPEN or evidence_gate_id(kind, key.target.number,
                                                                           observed.head_sha) not in held:
            # A new head (or a terminal pull request) is a different question:
            # the next dispatch or settlement reads its evidence afresh.
            if kind is ReviewEvidenceKind.RUNTIME_PREPARATION:
                return self._record_runtime_evidence_if_current(
                    state, schedule, observed.head_sha, error=None,
                ), ""
            self.record_evidence(key, observed.head_sha, kind, error=None)
            return True, ""
        if kind is ReviewEvidenceKind.RUNTIME_PREPARATION:
            if self._runtime_preflight is None:
                return False, "local review runtime preflight reader is unavailable"
            try:
                reason = self._runtime_preflight()
            except Exception as exc:  # noqa: BLE001 - a failed local probe cannot release its hold
                reason = _reason(exc) or "local review runtime preflight failed"
            committed = self._record_runtime_evidence_if_current(
                state, schedule, observed.head_sha, error=reason,
            )
            return committed and reason is None, ""
        if history:
            reviews = observed.reviews
            complete = isinstance(reviews, CompleteReviews)
            self.record_evidence(key, observed.head_sha, kind, error=(
                None if complete else getattr(reviews, "reason", "") or "review history incomplete"))
            return complete, ""
        if kind is ReviewEvidenceKind.LINKED_ISSUE:
            from .review_linkage import ReviewLinkedIssueUnavailable, review_linked_issue_holds

            try:
                review_linked_issue_holds(self._control, self._gateway, key, view)
            except ReviewLinkedIssueUnavailable as exc:
                self.record_evidence(key, observed.head_sha, kind, error=str(exc))
                return False, ""
            self.record_evidence(key, observed.head_sha, kind, error=None)
            return True, ""
        return False, ""
