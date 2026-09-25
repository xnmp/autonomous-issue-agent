"""Bound, generation-fenced SQLite review-rejection transitions; ADR 0015 review slice.

The review owner's control store commits every review-family transition in
one short `BEGIN IMMEDIATE`: it reads and decodes the stored row, requires the
proposal's prior to equal it, re-derives the proposal from facts it mints from
its own rows (bound SUBMIT_REVIEW receipts, durable pull request observations,
accepted operator audits) and persists workflow, intent, lifecycle commands
and outbox together. Operator acceptance and re-arm reconciliation take the
exclusive deployment barriers (fix, review, steward), read GitHub read-only
through the review gateway with no SQL transaction open, and release every
barrier before any effect runner can execute; nothing here writes remotely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta

from ..domain.contracts import CommandId, CommandKind, EffectStatus
from ..domain.labels import Label
from ..domain.resolve_rearm import (
    RearmObservation, UnavailableResolveRearm, defer_rearm_observation, schedule_rearm_observation,
)
from ..domain.review_rearm import (
    AbsentReviewControl, InactiveReviewControl, PendingReviewRecovery, ReleaseProjection, ReleaseStatus,
    ReviewControlRead, ReviewControlView, decide_review_rearm_reconciliation,
    update_review_rearm_observation,
)
from ..domain.review_rejection import CountingRule, ReviewProjectionRefused
from ..domain.review_workflow import (
    CancelledReviewRearm, CompleteReviews, CompletedReviewRearm, ObservedPullRequest, PendingReviewRearm,
    PullRequestState, PullRequestUnavailable, RecordedPullRequest, ReviewRearmIntent, ReviewRearmPreflight,
    ReviewWorkflowRead, ReviewWorkflowSnapshot, SupersededReviewRearm,
)
from ..domain.review_workflow_policy import (
    OBSERVATION_MAX_AGE_SECONDS, CeilingEscalation, ReviewDispatch, ReviewSubmissionReceipt, ReviewTransitionFacts,
    cancel_review_rearm, continue_review_rearm, initial_review_workflow, open_review_ceiling, reconcile_review_ceiling,
    record_review, request_review_rearm, review_dispatch_decision, review_release_command_id,
    validate_review_transition,
)
from ..domain.workflow import (
    ActiveReconciliation, AuthorizeAbsentRetry, AuthorizedOperatorCommand, CancelRearm, ContinueGrantedRearm,
    HeldProjection, NoAuthorizedRemediation, ObservedTransition, OpenGate, OperatorActor, RearmingGate,
    ReconciliationOnly, ReconciliationPolicy, RemoteOutcomeUncertain, RequestRearm, RetiredSettledCommand,
    RetiredUncertainCommand, RetryAuthorizationRequired, RetryDisposition, StopReason, WorkflowChanged,
    WorkflowCommitted, WorkflowConflict, WorkflowMigrationRequired, WorkflowMutation, WorkflowRejected,
    WorkflowRejection, WorkflowUnavailable, WorkflowUnchanged, WorkflowAbsent,
)
from ..domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision, WorkflowWriter,
    validate_workflow_identifier,
)
from ..domain.workflow_retry import RetryAuthorizationFacts, authorize_absent_retry, retry_authorization_available
from .control_store import (
    ControlStoreError, SQLiteControlStore, _command_from_row, load_bound_review_submission,
    load_retry_authorization_facts,
)
from .deployment_authority import deployment_effect_barrier, deployment_role
from .review_effects import PullRequestProjectionOp, ReviewCheck, ReviewReads, check_review_op, observe_pull_request, review_family_op
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_commit import (
    insert_pending_rearm, intent_state, owns_command, persist_retry_authorization, record_commit,
    retire_rearm_obligation,
    same_receipt, transition_dedupe_key, transition_receipt, update_rearm_state,
)
from .workflow_rows import (
    WorkflowAuthorityError, load_family_authority, load_review_workflow_row, load_workflow_read,
    persist_workflow_snapshot, workflow_run_scope_bounds, workflow_scope,
)

# Deployment barriers are always taken in this order (ADR 0015 acceptance).
BARRIER_ROLES = ("fix", "review", "steward")
_OBSERVATION_NAMESPACE = "workflow-rearm-observation"
_RECORDED_REVIEW_NAMESPACE = "review-recorded-submission"
_RELEASE_RESOLVED_NAMESPACE = "review-release-resolved"
_REVIEW_INTENTS = PendingReviewRearm | CompletedReviewRearm | SupersededReviewRearm | CancelledReviewRearm
_BARRIER_ORDERED = frozenset({
    WorkflowMutation.REQUEST_REARM, WorkflowMutation.CONTINUE_REARM, WorkflowMutation.RELEASE_CEILING,
    WorkflowMutation.AUTHORIZE_RETRY,
})


def _no_external_holds(_: ObservedPullRequest) -> tuple[str, ...]:
    return ()


def _reason(exc: BaseException) -> str:
    return (str(exc).replace("\x00", "") or type(exc).__name__)[:16384]


def release_refused_gate_id(command_id: CommandId) -> str:
    validate_workflow_identifier(command_id, "release projection command")
    return "review-projection-refused:" + hashlib.sha256(str(command_id).encode()).hexdigest()


class SQLiteReviewWorkflowControl:
    """The review owner's PR-scoped workflow authority (implements `ReviewWorkflowControl`)."""

    def __init__(
        self, control: SQLiteControlStore, writer: WorkflowWriter, *, gateway: object, ceiling: int,
        trigger_label: Label, rule: CountingRule,
        external_holds: Callable[[ObservedPullRequest], tuple[str, ...]] = _no_external_holds,
        policy: ReconciliationPolicy = ReconciliationPolicy(),
    ) -> None:
        if not isinstance(control, SQLiteControlStore) or not isinstance(writer, WorkflowWriter):
            raise ValueError("review workflow control requires the existing store and a typed writer")
        if writer.role != "review" or deployment_role(writer.repository, writer.owner_id) != writer.role:
            raise ValueError("review workflow authority requires the repository's review owner")
        if not isinstance(gateway, ReviewReads):
            raise ValueError("review workflow control requires a read-only pull request review gateway")
        if type(ceiling) is not int or not -(2**63) <= ceiling <= 2**63 - 1:
            raise ValueError("review rejection ceiling must be a bounded integer")
        if not isinstance(trigger_label, Label) or trigger_label is Label.NEEDS_HUMAN_REVIEW:
            raise ValueError("review trigger must be a label other than the attention hold")
        if not isinstance(rule, CountingRule) or not isinstance(policy, ReconciliationPolicy) or not callable(external_holds):
            raise ValueError("review workflow control requires a counting rule, hold source and read policy")
        self._control = control
        self._writer = writer
        self._gateway = gateway
        self._ceiling = ceiling
        self._trigger = trigger_label
        self._rule = rule
        self._external_holds = external_holds
        self._policy = policy

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
        """Exclusive fix, review and steward barriers; no SQL transaction is held while waiting.

        Every effect runner holds its role's barrier shared, so acceptance
        waits for an executing effect to finish (or become durable
        uncertainty) and no effect can start until the barriers are released.
        """
        with ExitStack() as barriers:
            for role in BARRIER_ROLES:
                barriers.enter_context(deployment_effect_barrier(self._control.path, self._writer.repository, role))
            yield

    def _observe(self, key: WorkflowKey) -> RecordedPullRequest | PullRequestUnavailable:
        """One fresh read-only observation, stamped at its start on the store clock."""
        started = self._now()
        observed = observe_pull_request(self._gateway, key, started, reviews=True)
        if isinstance(observed, PullRequestUnavailable):
            return observed
        try:
            return RecordedPullRequest(observed, tuple(self._external_holds(observed)))
        except sqlite3.Error:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable hold source is unavailable evidence
            return PullRequestUnavailable(key, started, f"external holds unavailable: {_reason(exc)[:480]}")

    @staticmethod
    def _evidence_id(recorded: RecordedPullRequest) -> str:
        return "pr-observation:" + hashlib.sha256(encode_workflow(recorded).encode()).hexdigest()

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

    @staticmethod
    def _accepted_rearm(db: sqlite3.Connection, key: WorkflowKey, request_id: str) -> AuthorizedOperatorCommand | None:
        row = db.execute("SELECT * FROM workflow_rearms WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if (row["repository"], row["target_kind"], row["target_number"]) != workflow_scope(key):
            raise WorkflowAuthorityError("re-arm request identity belongs to another workflow")
        try:
            audit = decode_workflow(row["actor_json"], AuthorizedOperatorCommand)
            intent = decode_workflow(row["intent_json"], _REVIEW_INTENTS)
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("invalid accepted review re-arm audit") from exc
        command = audit.command
        if (
            not isinstance(command, (RequestRearm, ContinueGrantedRearm)) or command.request_id != request_id
            or command.expected.fence.key != key or not isinstance(audit.preflight, ReviewRearmPreflight)
            or encode_workflow(audit) != row["actor_json"] or intent.request_id != request_id
            or intent.gate_id != row["gate_id"] or intent.to_generation != row["to_generation"]
            or row["state"] != intent_state(intent)
            or row["request_digest"] != hashlib.sha256(encode_workflow((audit.actor.subject, command)).encode()).hexdigest()
        ):
            raise WorkflowAuthorityError("accepted review re-arm audit disagrees with its durable identity")
        return audit

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
        """The current generation's ceiling-release relabel and whether it is verified (harness receipts only)."""
        generation = state.revision.fence.generation
        if generation < 1:
            return None
        command_id = review_release_command_id(state.revision.fence.key, generation)
        row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
        if row is None:
            return None
        command = _command_from_row(row)
        if command.workflow is None or command.workflow.fence != state.revision.fence or not owns_command(
            db, self._writer, command.executor_identity
        ):
            raise WorkflowAuthorityError("release projection belongs to another workflow authority")
        receipt = db.execute("SELECT status FROM effect_receipts WHERE command_id=?", (str(command_id),)).fetchone()
        status = receipt["status"] if receipt is not None and receipt["status"] == row["state"] else None
        resolved = db.execute(
            f"SELECT 1 FROM dedupe_keys WHERE namespace='{_RELEASE_RESOLVED_NAMESPACE}' AND scope=? AND key=?",
            (self._writer.repository, str(command_id)),
        ).fetchone() is not None
        if status == EffectStatus.SUCCEEDED.value or resolved:
            return ReleaseProjection(command_id, ReleaseStatus.VERIFIED)
        if status == EffectStatus.PERMANENT_FAILURE.value:
            return ReleaseProjection(command_id, ReleaseStatus.REFUSED)
        return ReleaseProjection(command_id, ReleaseStatus.PENDING)

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
        """The binding's bound review submissions whose outcome is not yet known.

        Unknown, executing (a crashed attempt) or proven absent and awaiting
        re-execution, before or after expiry; a retired command has its own
        gate. Harness-owned: the dispatch hold's only input.
        """
        key = binding.fence.key
        unresolved: list[CommandId] = []
        for row in db.execute(
            "SELECT * FROM lifecycle_commands WHERE repository=? AND target=? AND kind=? "
            "AND state IN ('executing','unknown_outcome','retryable_failure') ORDER BY created_at, command_id",
            (key.repository, f"pr:{key.target.number}", CommandKind.SUBMIT_REVIEW.value),
        ).fetchall():
            command = _command_from_row(row)
            if command.workflow == binding and not isinstance(
                self._control._command_reconciliation(row), (RetiredSettledCommand, RetiredUncertainCommand)
            ):
                unresolved.append(command.command_id)
        return tuple(unresolved)

    def review_control_view(
        self, key: WorkflowKey, *, request_id: str | None = None,
    ) -> ReviewControlRead | WorkflowUnavailable:
        """One read snapshot; inspection never schedules recovery or initializes state."""
        self._scope(key)
        if request_id is not None:
            validate_workflow_identifier(request_id, "requested re-arm")
        try:
            with closing(self._control._connect()) as db:
                db.execute("BEGIN")
                self._control._assert_owner_active(db, self._writer.owner_id)
                authority = load_family_authority(db, WorkflowFamily.REVIEW_REJECTIONS, key.repository)
                if authority is None:
                    return InactiveReviewControl(key)
                state = load_review_workflow_row(db, key)
                if state is None:
                    return AbsentReviewControl(key, authority)
                selected = request_id or (state.intent.request_id if state.intent is not None else None)
                requested = self._lookup_rearm(db, key, selected) if selected is not None else None
                observation = self._rearm_observation(db, state) if isinstance(state.intent, PendingReviewRearm) else None
                return ReviewControlView(state, selected, requested, observation,
                                         self._release_projection(db, state), self._retry_authorizable(db, state))
        except (sqlite3.Error, ValueError, TypeError) as exc:
            return WorkflowUnavailable(key, _reason(exc))

    # -- discovery -----------------------------------------------------------------------------

    def ensure_observed_workflow(self, key: WorkflowKey) -> WorkflowCommitted | PullRequestUnavailable:
        """Create the initial workflow of a present pull request; never initializes a family."""
        self._scope(key)
        recorded = self._observe(key)
        if isinstance(recorded, PullRequestUnavailable):
            return recorded
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self._writer.owner_id)
            authority = load_family_authority(db, WorkflowFamily.REVIEW_REJECTIONS, key.repository)
            if authority is None:
                raise WorkflowAuthorityError("review workflow migration is required")
            prior = load_review_workflow_row(db, key)
            if prior is not None:
                return WorkflowCommitted(prior, idempotent=True)
            snapshot = initial_review_workflow(key, authority)
            db.execute("INSERT INTO workflows VALUES (?, ?, ?, 0, 0, ?)",
                       (*workflow_scope(key), encode_workflow((authority, snapshot.episode))))
            self._insert_evidence(db, key, recorded)
            self._control._transition(db, "workflow", encode_workflow(key), "discovered",
                                      {"generation": 0, "version": 0, "epoch": authority.epoch})
            return WorkflowCommitted(snapshot)

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
        prior = self._load(db, key)
        if authorities != (prior.authority,):
            raise WorkflowAuthorityError("workflow family activation epoch changed")
        encoded = transition_receipt(transition)
        replayed = self._replay(db, prior, transition, encoded)
        if replayed is not None:
            return replayed
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
        self._persist(db, prior, transition, encoded)
        committed = record_commit(db, self._control, self._writer.repository, transition, encoded)
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
        )

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

    def _retry_authorizable(self, db: sqlite3.Connection, snapshot: ReviewWorkflowSnapshot) -> frozenset[str]:
        """Retry gates offered exactly when acceptance would admit a grant now."""
        available: set[str] = set()
        for gate in snapshot.gates:
            if not isinstance(gate, OpenGate) or not isinstance(gate.cause, RetryAuthorizationRequired):
                continue
            try:
                facts = load_retry_authorization_facts(self._control, db, gate.cause.command_id,
                                                       gate.cause.absence_evidence_id)
                row = db.execute("SELECT executor_identity FROM lifecycle_commands WHERE command_id=?",
                                 (str(gate.cause.command_id),)).fetchone()
                owned = facts.command_binding.fence.key == snapshot.revision.fence.key and owns_command(
                    db, self._writer, row["executor_identity"])
            except (WorkflowAuthorityError, ControlStoreError, ValueError, TypeError):
                continue  # Unreadable authority is never offered as an action.
            if owned and retry_authorization_available(snapshot, gate, facts):
                available.add(gate.gate_id)
        return frozenset(available)

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
        gates = {(g.gate if isinstance(g, RearmingGate) else g).gate_id: (g.gate if isinstance(g, RearmingGate) else g)
                 for g in state.gates}
        verified: list[CommandId] = []
        rejected: list[CommandId] = []
        uncertainty: list[RemoteOutcomeUncertain] = []
        for row in rows:
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
                        rejected.append(command.command_id)
                continue
            status = row["receipt_status"]
            if status is None:
                if row["state"] not in ("pending", "executing"):
                    raise WorkflowAuthorityError("settled re-arm command lacks its exact receipt")
                history = self._control._unresolved_history(row)
                if history is not None:
                    uncertainty.append(RemoteOutcomeUncertain(
                        command.command_id, history.unresolved_since, history.failed_reads,
                        "command outcome remains unresolved"))
                continue
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
                verified.append(command.command_id)
            elif settled is EffectStatus.PERMANENT_FAILURE:
                rejected.append(command.command_id)
            elif settled is EffectStatus.UNKNOWN_OUTCOME:
                history = self._control._unresolved_history(row)
                assert history is not None
                uncertainty.append(RemoteOutcomeUncertain(
                    command.command_id, history.unresolved_since, history.failed_reads,
                    row["receipt_error"] or "command outcome remains unresolved"))
            elif reconciliation is not None:
                raise WorkflowAuthorityError("settled command retained incompatible uncertainty")
        order = {command: index for index, command in enumerate(intent.required_commands)}
        return (tuple(sorted(verified, key=order.__getitem__)), tuple(sorted(rejected, key=order.__getitem__)),
                tuple(sorted(uncertainty, key=lambda item: order[item.command_id])))

    def _persist(
        self, db: sqlite3.Connection, prior: ReviewWorkflowSnapshot, transition: WorkflowChanged, encoded: str,
    ) -> None:
        after = transition.snapshot
        assert isinstance(after, ReviewWorkflowSnapshot)
        key = after.revision.fence.key
        scope = workflow_scope(key)
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
        if transition.mutation is WorkflowMutation.RECORD_REVIEW:
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       (_RECORDED_REVIEW_NAMESPACE, self._writer.repository, self._recorded_submission(transition),
                        self._now().isoformat(), encode_workflow(after.revision)))

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
        for row in db.execute(
            "SELECT * FROM lifecycle_commands WHERE repository=? AND target=? AND workflow_binding_json IS NOT NULL",
            (key.repository, f"pr:{key.target.number}"),
        ).fetchall():
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
        recorded = self._observe(key)
        with self._control.transaction() as db:
            prior = self._load(db, key)
            if isinstance(recorded, PullRequestUnavailable):
                return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, recorded.reason)
            proposal = open_review_ceiling(
                prior, recorded.observation, now=self._now(), rule=self._rule, ceiling=self._ceiling,
                escalation=bound, evidence_ids=(self._evidence_id(recorded),),
            )
            return self._apply(db, prior, key, recorded, proposal)

    def reconcile_ceiling(self, key: WorkflowKey) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        """Refresh or release this episode's ceiling from a fresh complete read (under the barriers).

        A release advances the generation, which fences every older command,
        so it is ordered with executing effects like an operator re-arm.
        """
        self._scope(key)
        with self._barriers():
            recorded = self._observe(key)
            with self._control.transaction() as db:
                prior = self._load(db, key)
                if isinstance(recorded, PullRequestUnavailable):
                    return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, recorded.reason)
                proposal = reconcile_review_ceiling(
                    prior, recorded.observation, now=self._now(), ceiling=self._ceiling,
                    evidence_ids=(self._evidence_id(recorded),), trigger_label=self._trigger,
                    executor_identity=self._writer.owner_id, external_holds=recorded.external_holds,
                )
                return self._apply(db, prior, key, recorded, proposal)

    def record_review(self, submission: CommandId) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        """Record a verified current-generation verdict as the reviewed baseline, at most once per receipt."""
        with self._control.transaction() as db:
            bound = load_bound_review_submission(db, submission)
            if bound is None:
                return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE,
                                        "the submission has no verified bound review receipt")
            if not isinstance(bound, ReviewSubmissionReceipt):
                return WorkflowRejected(WorkflowRejection.INVALID_SCOPE, "an escalation is not a reviewed baseline")
            key = self._scope(bound.submission.binding.fence.key)
            prior = self._load(db, key)
            if self._submission_recorded(db, str(submission)):
                return WorkflowCommitted(prior, idempotent=True)
            proposal = record_review(prior, bound)
            if isinstance(proposal, WorkflowRejected):
                return proposal
            if isinstance(proposal, WorkflowUnchanged):
                return WorkflowCommitted(prior, idempotent=True)
            return self._commit(db, prior.revision, (prior.authority,), proposal)

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
        self._insert_evidence(db, key, recorded)
        return self._commit(db, prior.revision, (prior.authority,), proposal)

    # -- operator acceptance -------------------------------------------------------------------

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
        try:
            accepted = json.loads(receipt["payload_json"])
            raw = accepted.get("audit") if isinstance(accepted, dict) else None
            if not isinstance(raw, str):
                raise ValueError("request receipt lacks its accepted audit")
            audit = decode_workflow(raw, AuthorizedOperatorCommand)
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("invalid accepted review request audit") from exc
        if audit.command != command or audit.actor.subject != actor.subject:
            return WorkflowConflict(current.revision, "request identity was reused with different content or operator")
        if current.intent is None or current.intent.request_id != command.request_id:
            return WorkflowRejected(WorkflowRejection.INVALID_SCOPE,
                                    "request identity belongs to an older review re-arm that a newer decision replaced")
        return WorkflowCommitted(current, idempotent=True)

    def _active_review_runs(self, db: sqlite3.Connection, key: WorkflowKey) -> bool:
        lower, upper = workflow_run_scope_bounds(key)
        return db.execute(
            "SELECT 1 FROM runs r LEFT JOIN leases l USING(run_id) LEFT JOIN budget_reservations b USING(run_id) "
            "WHERE r.workflow_episode_key>=? AND r.workflow_episode_key<? "
            "AND (r.state IN ('reserved','starting','running') OR l.run_id IS NOT NULL OR b.state='active') LIMIT 1",
            (lower, upper),
        ).fetchone() is not None

    def request_rearm(
        self, command: RequestRearm, actor: OperatorActor,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        return self._accept(command, actor)

    def continue_rearm(
        self, command: ContinueGrantedRearm, actor: OperatorActor,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        return self._accept(command, actor)

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
            replayed = self._request_replay(db, command, actor)
        if replayed is not None:
            return replayed
        with self._barriers():
            with self._control.transaction() as db:
                current = self._load(db, key)
                if current.revision != command.expected:
                    return WorkflowConflict(current.revision, "workflow changed before re-arm acceptance")
            recorded = self._observe(key)
            with self._control.transaction() as db:
                prior = self._load(db, key)
                replayed = self._request_replay(db, command, actor)
                if replayed is not None:
                    return replayed
                if isinstance(recorded, PullRequestUnavailable):
                    return WorkflowRejected(WorkflowRejection.OBSERVATION_UNAVAILABLE, recorded.reason)
                if self._active_review_runs(db, key):
                    return WorkflowRejected(WorkflowRejection.OTHER_HOLD, "active review runs must drain before a re-arm")
                evidence_id = self._evidence_id(recorded)
                common = dict(now=self._now(), active_reviews=(), evidence_id=evidence_id,
                              trigger_label=self._trigger, executor_identity=self._writer.owner_id,
                              external_holds=recorded.external_holds)
                if isinstance(command, RequestRearm):
                    proposal = request_review_rearm(prior, command, actor, recorded.observation, **common)  # type: ignore[arg-type]
                else:
                    proposal = continue_review_rearm(prior, command, actor, recorded.observation, **common)  # type: ignore[arg-type]
                return self._apply(db, prior, key, recorded, proposal)

    def cancel_rearm(self, command: CancelRearm, actor: OperatorActor) -> WorkflowCommitted | WorkflowConflict:
        if not isinstance(command, CancelRearm) or not isinstance(actor, OperatorActor):
            raise ValueError("review re-arm cancellation requires a typed command and actor")
        key = self._scope(command.expected.fence.key)
        with self._barriers():
            with self._control.transaction() as db:
                prior = self._load(db, key)
                receipt = db.execute(
                    "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-cancellation' AND scope=? AND key=?",
                    (self._writer.repository, command.cancellation_id),
                ).fetchone()
                if receipt is not None:
                    try:
                        accepted = json.loads(receipt["payload_json"])
                        raw = accepted.get("audit") if isinstance(accepted, dict) else None
                        if not isinstance(raw, str):
                            raise ValueError("cancellation receipt lacks its accepted audit")
                        audit = decode_workflow(raw, AuthorizedOperatorCommand)
                    except (ValueError, TypeError) as exc:
                        raise WorkflowAuthorityError("invalid accepted cancellation audit") from exc
                    if audit.command == command and audit.actor.subject == actor.subject:
                        return WorkflowCommitted(prior, idempotent=True)
                    return WorkflowConflict(prior.revision, "durable workflow identity was reused with different content")
                if prior.revision != command.expected or not isinstance(prior.intent, PendingReviewRearm):
                    return WorkflowConflict(prior.revision, "review re-arm cancellation requires exact pending authority")
                verified, _, uncertainty = self._rearm_command_facts(db, prior, None)
                proposal = cancel_review_rearm(prior, command, actor, verified_commands=verified, uncertainty=uncertainty)
                if isinstance(proposal, WorkflowRejected):
                    return WorkflowConflict(prior.revision, proposal.reason)
                if isinstance(proposal, WorkflowUnchanged):
                    return WorkflowCommitted(prior, idempotent=True)
                return self._commit(db, prior.revision, (prior.authority,), proposal)

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
                stored = db.execute(
                    "SELECT actor_json FROM workflow_retry_authorizations WHERE authorization_id=?",
                    (command.authorization_id,),
                ).fetchone()
                if stored is not None:
                    try:
                        accepted = decode_workflow(stored["actor_json"], AuthorizedOperatorCommand)
                    except (ValueError, TypeError) as exc:
                        raise WorkflowAuthorityError("invalid accepted retry authorization audit") from exc
                    if accepted.command == command and accepted.actor.subject == actor.subject:
                        return WorkflowCommitted(prior, idempotent=True)
                    return WorkflowConflict(prior.revision,
                                            "authorization identity was reused with different content or operator")
                if prior.revision != command.expected:
                    return WorkflowConflict(prior.revision, "workflow changed before retry authorization")
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
            if planned or pr.state is not PullRequestState.OPEN:
                evidence_id = self._insert_evidence(db, key, recorded)
                db.execute("INSERT OR IGNORE INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                           "VALUES (?,?,?,?,?)", (_RELEASE_RESOLVED_NAMESPACE, self._writer.repository, command_id,
                                                  self._now().isoformat(), evidence_id))
                if gate is not None:
                    self._control._reconciliation_gate(db, binding, gate_id, None)
                self._control._transition(db, "workflow_release", command_id, "verified_by_observation", {
                    "evidence_id": evidence_id, "terminal": pr.state is not PullRequestState.OPEN,
                    "generation": binding.fence.generation,
                })
                return
        if gate is None:
            error = db.execute("SELECT error FROM effect_receipts WHERE command_id=?", (command_id,)).fetchone()
            reason = (error["error"] if error is not None and error["error"] else "release projection refused")
            planned = (f"remove {op.remove.value!r} and add {op.add.value!r}" if op.remove is not None
                       else f"add {op.add.value!r}")
            self._control._reconciliation_gate(db, binding, gate_id, OpenGate(
                gate_id, state.revision.fence.generation,
                ReviewProjectionRefused(command_id, binding.fence.generation, reason[:16384]),
                NoAuthorizedRemediation(
                    f"correct the pull request labels by hand ({planned}); the next harness read verifies "
                    "them and releases this hold"),
            ))
            self._control._transition(db, "workflow_release", command_id, "refusal_gated",
                                      {"gate_id": gate_id, "generation": binding.fence.generation})
