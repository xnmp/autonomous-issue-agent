"""SQLite operational authority defined by ADR 0003.

The store owns reservations, run/lease state, deduplication, lifecycle commands,
effect receipts, and the event outbox.  Transactions are intentionally short:
no network, model, or subprocess work is accepted by this adapter.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from ..domain import continuation_components as cc
from ..domain.contracts import ContinuationAdmission

from ..domain.contracts import (
    BudgetScope,
    CommandId,
    CommandKind,
    EffectReceipt,
    EffectStatus,
    LifecycleCommand,
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
    SettlementRequest,
    RunId,
    TokenUsage,
    WorkflowAttemptIdentity,
)
from ..domain.workflow_identity import FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision, validate_workflow_identifier, validate_workflow_integer
from ..domain.workflow import (
    ActiveReconciliation, CommandAbsenceEvidence, CommandReconciliation, CommandRetryApplicability,
    ClosedRetryAuthorization, ConsumedRetryAuthorization, RearmObligation, ReconciliationPolicy, RetryAlreadyConsumed,
    RetryBegan, RetryDeclined, RetryDisposition, RetryStale, StandaloneCommand,
    UnconsumedRetryAuthorization, NoAuthorizedRemediation, OpenGate, RearmingGate, ReconciliationOnly,
    RemoteOutcomeUncertain, RetiredSettledCommand, RetiredUncertainCommand, RetryAuthorizationRequired,
    AuthoredResolve, CompletedRearm, PendingRearm, ResolveAdmission, ResolveEpisode, ResolveResultKind, ResolveStage, WorkflowSnapshot, adjudication_allowed,
)
from ..domain.resolve_workspace import EpisodeWorkspacePrepared, PriorResolveAdmission
from ..domain.agent_task import RuntimeName
from ..domain.review_execution import BlockedReviewExecutionMigration, CurrentReviewExecution, LaunchedReviewChild, ReviewCancellationReason, ReviewChildAdmission, ReviewChildAdmissionRefusal, ReviewChildIdentity, ReviewChildLaunchRegistration, ReviewPreparationFailure, ReviewPreparationKind, ReviewUnlaunchedRelease
from ..domain.review_preflight import ReviewPreflightSelection, require_review_execution_eligibility
from ..domain.review_return import ReviewReturnedEnvelope, ReturnedReviewChild, ReviewRuntimePreflightStop
from ..domain.review_rounds import ReviewChildCancelled, ReviewChildEvidence, ReviewChildPending, ReviewChildRefused, ReviewChildReleased, ReviewChildReturned, ReviewChildSettledWithoutLaunch, ReviewParentAbandoned, ReviewPlanningComplete, ReviewPlanningOpen, ReviewPlanningSealed, classify_normal_review_return
from ..domain.review_workflow import AnyWorkflowSnapshot, BoundReviewSubmission, PendingReviewRearm, ReviewEpisode, ReviewWorkflowSnapshot
from ..domain.review_workflow_policy import (
    ESCALATION_PROJECTION_PREFIX, HOLD_RESTORE_PREFIX, CeilingEscalation, ReviewSubmissionReceipt,
    bound_submission_owed, escalation_signal_identity,
)
from ..domain.workflow_retry import (
    MAX_AUTHORIZED_ATTEMPTS, MAX_RELEVANT_AUTHORIZATIONS, BeginRetry, DeclineRetry, grant_unavailable_void, RetryAbsenceRead, RetryAuthorization, RetryAuthorizationFacts,
    retry_spend_decision,
)
from ..domain.workflow_reconciliation import (
    ABSENCE_FRESHNESS_SECONDS, ReconciliationProgress, absent_reconciliation, absent_reexecution, reconciliation_due,
    prewrite_evidence_gate_id, unavailable_prewrite_read, unavailable_reconciliation, unknown_outcome_history,
)
from ..domain.resolve_return import FinalizedResolveReturn, ReturnedResolve
from ..domain.outcome import AuthorFollowup, ExitReason, author_followup
from ..domain.resolve_split import MAX_SPLIT_TRAIL, AdjudicationInput, ContractResetCompleted, ReauthorInput, ReauthorResetIntent, SplitContext, SplitImplement, SplitInput, split_context, validate_contract_rounds
from ..domain.events import (
    ActionApplied,
    ActionFailed,
    AdmissionReconciled,
    BudgetVarianceDetected,
    ControlTransitionCommitted,
    Correlation,
    EscalationRaised,
    EventEnvelope,
    EventPayload,
    JsonValue,
    MAX_EVENT_LINE_BYTES,
    ReservationRetained,
    SpendRecorded,
    WorldFeedRetired,
    WorldObserved,
    decode_event_line,
    encode_event_line,
)
from ..domain.repository import canonical_repository
from ..domain.refs import issue_from_branch
from .deployment_authority import (
    deployment_effect_barrier, deployment_effect_authority,
    deployment_role,
)
from .review_effects import bound_submission_op, review_family_op
from .review_execution_claims import load_review_preflight_record, require_review_parent_claim
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError, active_families, linked_issue_workflow_holds, load_family_authority, load_resolve_workflow_row, load_review_workflow_row, load_workflow_row, load_workspace_ack, read_review_submission_registration, require_resolve_binding, require_review_binding, require_workflow_binding, verified_review_completed_seal, workflow_scope, workflow_run_scope_bounds as workflow_run_scope_bounds

SCHEMA_VERSION = 36


def workflow_episode_key(binding: WorkflowBinding) -> str:
    """An indexed projection of immutable admission scope, not an allowance."""
    return json.dumps((*workflow_scope(binding.fence.key), binding.authority.family.value,
                       binding.authority.epoch, binding.episode_id), ensure_ascii=False, separators=(",", ":"))


def _review_child_key(child: ReviewChildIdentity) -> str:
    return json.dumps((child.round.round_id, child.child_id), separators=(",", ":"))


def _indexed_review_child_admission(
    db: sqlite3.Connection, repository: str, child: ReviewChildIdentity,
) -> ReviewChildAdmission | None:
    indexed = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-child-admission' "
        "AND scope=? AND key=?", (repository, _review_child_key(child)),
    ).fetchone()
    if indexed is None:
        return None
    admission = decode_workflow(indexed["payload_json"], ReviewChildAdmission)
    run_receipt = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-admission' "
        "AND scope=? AND key=?", (repository, admission.run_id),
    ).fetchone()
    if admission.child != child or run_receipt is None or run_receipt["payload_json"] != indexed["payload_json"]:
        raise WorkflowAuthorityError("review child admission index disagrees with its run receipt")
    return admission


def _review_request_fingerprint(request: ReservationRequest) -> str:
    def limit_fields(limit: BudgetLimit) -> tuple[object, ...]:
        scope = limit.scope
        return (scope.repository, scope.dimension, scope.currency, scope.period_id,
                _stamp(scope.period_start), _stamp(scope.period_end), str(scope.period_timezone),
                limit.cap.micros)

    quote = request.quote
    payload = (
        str(request.reservation_id), str(request.run_id), request.dispatch_id,
        request.workload_repository, request.kind, request.owner_id, request.config_version,
        encode_workflow(request.workflow), encode_workflow(request.review_child),
        quote.model, quote.rate_card_id, quote.rate_card_version,
        _stamp(quote.effective_at), quote.maximum_charge.currency, quote.maximum_charge.micros,
        tuple(limit_fields(limit) for limit in (
            BudgetLimit(request.scope, request.cap), *request.additional_limits,
        )),
    )
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _review_refusal_receipt_id(refusal: ReviewChildAdmissionRefusal) -> str:
    return hashlib.sha256(encode_workflow(replace(refusal, receipt_id="pending")).encode()).hexdigest()


def target_has_work_admissions(db: sqlite3.Connection, key: WorkflowKey) -> bool:
    prefix = json.dumps((*workflow_scope(key), WorkflowFamily.RESOLVE.value), ensure_ascii=False, separators=(",", ":"))[:-1] + ","
    return db.execute(
        "SELECT 1 FROM runs WHERE workflow_episode_key>=? AND workflow_episode_key<? "
        "AND kind<>'ROUTE' LIMIT 1", (prefix, prefix + "\uffff"),
    ).fetchone() is not None


def episode_entry_admitted(db: sqlite3.Connection, snapshot: WorkflowSnapshot) -> bool:
    """The first immutable paid admission consumes the handoff, including routing."""
    row = db.execute(
        "SELECT run_id,kind,dispatch_id,workflow_binding_json FROM runs "
        "WHERE workflow_episode_key=? LIMIT 1", (workflow_episode_key(snapshot.binding),),
    ).fetchone()
    if row is None:
        return False
    binding = _indexed_run_binding(db, row)
    if workflow_episode_key(binding) != workflow_episode_key(snapshot.binding):
        raise WorkflowAuthorityError("episode entry index disagrees with its admitted scope")
    receipt = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND scope=? AND key=?",
        (binding.fence.key.repository, row["run_id"]),
    ).fetchone()
    admission = decode_workflow(receipt["payload_json"], ResolveAdmission)
    expected = snapshot.intent.handoff.handoff_id if isinstance(snapshot.intent, CompletedRearm) else None
    if admission.workspace_handoff_id != expected:
        raise WorkflowAuthorityError("episode admission lacks its immutable workspace handoff")
    return True


def admitted_adjudications(db: sqlite3.Connection, binding: WorkflowBinding) -> int:
    """Saturate above every supported ceiling without scanning other episodes."""
    # The full target-scope index also matches this key, but scanning excluded
    # never-launched history would violate the bounded allowance-query contract.
    admitted = 0
    for row in db.execute(
        "SELECT r.run_id,r.kind,r.dispatch_id,r.workflow_binding_json FROM runs r INDEXED BY runs_adjudication_episode "
        "WHERE r.workflow_episode_key=? AND r.kind='ADJUDICATE' "
        "AND NOT(r.state='cancelled' AND r.outcome IS 'unlaunched_released' AND r.provider_receipt_id IS NULL) "
        "LIMIT ?", (workflow_episode_key(binding), MAX_SPLIT_TRAIL + 1),
    ):
        previous = _indexed_run_binding(db, row)
        if workflow_episode_key(previous) != workflow_episode_key(binding):
            raise WorkflowAuthorityError("adjudication index disagrees with immutable run scope")
        admitted += 1
    return admitted


def _indexed_run_binding(db: sqlite3.Connection, row: sqlite3.Row) -> WorkflowBinding:
    binding = decode_workflow(row["workflow_binding_json"], WorkflowBinding)
    reservation = db.execute("SELECT workload_repository FROM budget_reservations WHERE run_id=?", (row["run_id"],)).fetchone()
    receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND scope=? AND key=?", (binding.fence.key.repository, row["run_id"])).fetchone()
    if reservation is None or reservation["workload_repository"] != binding.fence.key.repository or receipt is None:
        raise WorkflowAuthorityError("indexed workflow run lacks its reservation and immutable admission")
    admission = decode_workflow(receipt["payload_json"], ResolveAdmission)
    metadata = json.loads(row["dispatch_id"])
    if (admission.attempt.binding != binding or str(admission.attempt.run_id) != row["run_id"]
        or admission.stage.value != row["kind"] or not isinstance(metadata, dict)
        or metadata.get("leg_id") != admission.attempt.leg_id):
        raise WorkflowAuthorityError("indexed workflow run disagrees with its immutable admission")
    return binding


def _split_admission_input(metadata: dict) -> SplitInput | None:
    raw = metadata.get("split_input")
    fingerprint = metadata.get("split_fingerprint")
    if raw is None and fingerprint is None:
        return None
    if not isinstance(raw, str) or fingerprint != hashlib.sha256(raw.encode()).hexdigest():
        raise WorkflowAuthorityError("split admission lacks exact input provenance")
    return decode_workflow(raw, SplitContext | SplitImplement | AdjudicationInput | ReauthorInput)


def _split_consumption_key(snapshot: WorkflowSnapshot, stage: ResolveStage, supplied: SplitInput | None = None) -> str:
    # The token belongs to the current phase, not caller-supplied input: two
    # differently routed requests must not both consume one ready phase.
    continuation = snapshot.episode.continuation
    cursor = continuation.split if isinstance(continuation, AuthoredResolve) else None
    predecessor = cursor.source if cursor is not None and stage is not ResolveStage.AUTHOR else continuation.last_applied_admission_version
    if stage is ResolveStage.AUTHOR and isinstance(supplied, ReauthorInput):
        predecessor = supplied.reset.intent.source
    return hashlib.sha256(encode_workflow((snapshot.binding, stage, predecessor)).encode()).hexdigest()


def latest_work_admission(db: sqlite3.Connection, binding: WorkflowBinding) -> ResolveAdmission | None:
    latest = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-work-latest' AND scope=? AND key=?",
                        (binding.fence.key.repository, f"issue:{binding.fence.key.target.number}")).fetchone()
    if latest is None:
        return None
    admission = decode_workflow(latest["payload_json"], ResolveAdmission)
    receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND scope=? AND key=?",
                         (binding.fence.key.repository, str(admission.attempt.run_id))).fetchone()
    run = db.execute("SELECT workflow_binding_json,kind FROM runs WHERE run_id=?", (str(admission.attempt.run_id),)).fetchone()
    if admission.attempt.binding.fence.key != binding.fence.key or receipt is None or decode_workflow(receipt["payload_json"], ResolveAdmission) != admission or run is None or run["kind"] != admission.stage.value or decode_workflow(run["workflow_binding_json"], WorkflowBinding) != admission.attempt.binding:
        raise WorkflowAuthorityError("latest split admission disagrees with its admitted target and run")
    return admission


def require_workspace_predecessor(db: sqlite3.Connection, snapshot: WorkflowSnapshot) -> ResolveAdmission | None:
    """Only a verified completed handoff may bridge a retained episode pointer."""
    latest = latest_work_admission(db, snapshot.binding)
    if snapshot.intent is not None and not isinstance(snapshot.intent, CompletedRearm):
        raise WorkflowAuthorityError("workspace admission requires a completed rearm")
    if latest is None:
        if target_has_work_admissions(db, snapshot.revision.fence.key):
            raise WorkflowAuthorityError("retained workflow work has lost its latest-admission pointer")
        if isinstance(snapshot.intent, CompletedRearm) and isinstance(snapshot.intent.handoff.predecessor, PriorResolveAdmission):
            raise WorkflowAuthorityError("completed workspace handoff has lost its admitted predecessor")
        return None
    previous = latest.attempt.binding
    if previous.fence.generation > snapshot.binding.fence.generation or latest.revision.version > snapshot.revision.version:
        raise WorkflowAuthorityError("latest workspace admission belongs to a future revision")
    if (previous.authority, previous.episode_id) == (snapshot.binding.authority, snapshot.binding.episode_id):
        return latest
    intent = snapshot.intent
    if (not isinstance(intent, CompletedRearm)
            or intent.handoff.predecessor != PriorResolveAdmission(latest.attempt, latest.revision.version)
            or load_workspace_ack(db, intent.handoff) != intent.workspace):
        raise WorkflowAuthorityError("foreign episode predecessor requires its exact completed workspace handoff")
    return latest


def proven_unlaunched_run(db: sqlite3.Connection, run_id: RunId) -> bool:
    row = db.execute(
        "SELECT 1 FROM runs r JOIN budget_reservations b USING(run_id) JOIN reservation_recovery rr USING(run_id) "
        "WHERE r.run_id=? AND r.state='cancelled' AND r.outcome='unlaunched_released' AND b.state='released' "
        "AND rr.policy='spawn_pending' AND r.provider_receipt_id IS NULL "
        "AND NOT EXISTS(SELECT 1 FROM charges c WHERE c.run_id=r.run_id) "
        "AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.run_id=r.run_id)", (str(run_id),),
    ).fetchone()
    return row is not None


def reset_predecessor_admission(db: sqlite3.Connection, binding: WorkflowBinding) -> ResolveAdmission | None:
    """A released launch retains its executed predecessor in immutable input."""
    latest = latest_work_admission(db, binding)
    if latest is None or not proven_unlaunched_run(db, latest.attempt.run_id):
        return latest
    run = db.execute("SELECT dispatch_id FROM runs WHERE run_id=?", (str(latest.attempt.run_id),)).fetchone()
    metadata = json.loads(run["dispatch_id"])
    supplied = _split_admission_input(metadata)
    if latest.split_fingerprint != metadata.get("split_fingerprint"):
        raise WorkflowAuthorityError("unlaunched input disagrees with its immutable admission")
    if isinstance(supplied, SplitImplement):
        source = supplied.source
    elif isinstance(supplied, AdjudicationInput):
        source = supplied.petition.source
    elif isinstance(supplied, ReauthorInput):
        source = supplied.reset.intent.source
    else:
        return latest
    if isinstance(supplied, (AdjudicationInput, ReauthorInput)):
        ack = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-contract-reset-completed' AND scope=? AND key=?", (binding.fence.key.repository, supplied.reset.intent.intent_id)).fetchone()
        if ack is None or decode_workflow(ack["payload_json"], ContractResetCompleted) != supplied.reset:
            raise WorkflowAuthorityError("unlaunched reset input lacks its immutable acknowledgement")
    receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND scope=? AND key=?", (binding.fence.key.repository, str(source.run_id))).fetchone()
    prior_run = db.execute("SELECT workflow_binding_json,kind FROM runs WHERE run_id=?", (str(source.run_id),)).fetchone()
    if receipt is None or prior_run is None:
        raise WorkflowAuthorityError("unlaunched input lacks its admitted predecessor")
    prior = decode_workflow(receipt["payload_json"], ResolveAdmission)
    if (prior.attempt != source or prior.revision.version >= latest.revision.version
        or prior_run["kind"] != prior.stage.value or decode_workflow(prior_run["workflow_binding_json"], WorkflowBinding) != source.binding
        or (source.binding.fence.key, source.binding.authority, source.binding.episode_id) != (binding.fence.key, binding.authority, binding.episode_id)
        or source.binding.fence.generation > binding.fence.generation):
        raise WorkflowAuthorityError("unlaunched input names a foreign or future predecessor")
    return prior


def _validate_split_admission(db: sqlite3.Connection, snapshot: WorkflowSnapshot, stage: ResolveStage, metadata: dict, model: str) -> None:
    supplied = _split_admission_input(metadata)
    last = require_workspace_predecessor(db, snapshot)
    if last is not None and (last.attempt.binding.authority, last.attempt.binding.episode_id) != (snapshot.authority, snapshot.episode.episode_id):
        # The exact completed handoff above preserves the old pointer as audit,
        # not as a contract cursor in the newly prepared allowance.
        last = None
    split_stages = (ResolveStage.AUTHOR, ResolveStage.IMPLEMENT, ResolveStage.ADJUDICATE)
    if supplied is None:
        if stage in split_stages:
            raise WorkflowAuthorityError("new split admission requires a typed stage input")
        checkpoint = snapshot.episode.continuation
        same_episode = last is not None and (last.attempt.binding.authority, last.attempt.binding.episode_id) == (snapshot.binding.authority, snapshot.binding.episode_id)
        if stage in (ResolveStage.RESOLVE, ResolveStage.RESUME) and (
            (isinstance(checkpoint, AuthoredResolve) and checkpoint.split is not None)
            or (same_episode and last is not None and last.stage in split_stages)
        ):
            raise WorkflowAuthorityError("ordinary work cannot bypass this episode's split continuation")
        return
    if last is not None:
        if (last.stage in (ResolveStage.RESOLVE, ResolveStage.RESUME)
            and (last.attempt.binding.authority, last.attempt.binding.episode_id) == (snapshot.binding.authority, snapshot.binding.episode_id)):
            raise WorkflowAuthorityError("split contract writing cannot inherit an ordinary implementation episode")
        run = db.execute(
            "SELECT r.state,d.payload_json AS disposition FROM runs r "
            "LEFT JOIN dedupe_keys d ON d.namespace='workflow-return-disposition' AND d.scope=? AND d.key=r.run_id WHERE r.run_id=?",
            (snapshot.binding.fence.key.repository, str(last.attempt.run_id)),
        ).fetchone()
        if (run is None or run["state"] in ("reserved", "starting", "running") or run["disposition"] is None) and not proven_unlaunched_run(db, last.attempt.run_id):
            raise WorkflowAuthorityError("another split phase is still active or awaiting finalization")
    context = split_context(supplied)
    selection = context.selection
    if metadata.get("runtime") != selection.runtime or model != selection.model or metadata.get("effort") != (selection.effort.value if selection.effort is not None else None):
        raise WorkflowAuthorityError("split admission changed its selected runtime, model or effort")
    if issue_from_branch(context.branch) != snapshot.binding.fence.key.target.number:
        raise WorkflowAuthorityError("split admission belongs to another issue branch")
    continuation = snapshot.episode.continuation
    cursor = continuation.split if isinstance(continuation, AuthoredResolve) else None
    if stage is ResolveStage.AUTHOR:
        if not isinstance(supplied, (SplitContext, ReauthorInput)) or context.trail:
            raise WorkflowAuthorityError("author admission requires fresh split context")
        if isinstance(continuation, AuthoredResolve) and continuation.author_admission.input_fingerprint == metadata.get("input_fingerprint"):
            raise WorkflowAuthorityError("the applicable author stage is already complete")
        if isinstance(continuation, AuthoredResolve) and not isinstance(supplied, ReauthorInput):
            raise WorkflowAuthorityError("replacement author requires a verified isolation reset")
        if isinstance(supplied, SplitContext) and last is not None:
            predecessor = reset_predecessor_admission(db, snapshot.binding)
            if predecessor is not None and (predecessor.attempt.binding.authority, predecessor.attempt.binding.episode_id) == (snapshot.binding.authority, snapshot.binding.episode_id):
                raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return' AND scope=? AND key=?", (snapshot.binding.fence.key.repository, str(predecessor.attempt.run_id))).fetchone()
                returned = decode_workflow(raw["payload_json"], ReturnedResolve) if raw is not None else None
                if (predecessor.stage in (ResolveStage.IMPLEMENT, ResolveStage.ADJUDICATE)
                    or (returned is not None and (isinstance(returned.split_input, ReauthorInput)
                        or (returned.stage is ResolveStage.AUTHOR and author_followup(returned.result()) is AuthorFollowup.AUTHORED)))):
                    raise WorkflowAuthorityError("prior split work requires an isolation reset before replacement author")
        if isinstance(supplied, ReauthorInput):
            intent = supplied.reset.intent
            assert isinstance(intent, ReauthorResetIntent)
            predecessor = reset_predecessor_admission(db, snapshot.binding)
            if (intent.binding != snapshot.binding
                or intent.input_fingerprint != metadata.get("input_fingerprint")
                or predecessor is None or predecessor.attempt != intent.source):
                raise WorkflowAuthorityError("replacement author reset does not match current authority and input")
    elif stage is ResolveStage.IMPLEMENT:
        if not isinstance(supplied, SplitImplement) or supplied != cursor:
            raise WorkflowAuthorityError("implementation does not consume the current contract continuation")
    elif stage is ResolveStage.ADJUDICATE:
        if not isinstance(supplied, AdjudicationInput) or supplied.petition != cursor or supplied.reset.intent.binding != snapshot.binding:
            raise WorkflowAuthorityError("adjudication does not consume the current dispute")
    else:
        raise WorkflowAuthorityError("ordinary resolve cannot consume split context")
    if isinstance(supplied, (AdjudicationInput, ReauthorInput)):
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-contract-reset-completed' AND scope=? AND key=?",
                         (snapshot.binding.fence.key.repository, supplied.reset.intent.intent_id)).fetchone()
        if row is None or decode_workflow(row["payload_json"], ContractResetCompleted) != supplied.reset:
            raise WorkflowAuthorityError("contract writing requires the exact durable reset acknowledgement")
    prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-split-consumed' AND scope=? AND key=?",
                       (snapshot.binding.fence.key.repository, _split_consumption_key(snapshot, stage, supplied))).fetchone()
    if prior is None:
        return
    admission = decode_workflow(prior["payload_json"], ResolveAdmission)
    unlaunched = proven_unlaunched_run(db, admission.attempt.run_id)
    if stage is ResolveStage.ADJUDICATE and not unlaunched:
        raise WorkflowAuthorityError("adjudication retry requires a new failed-attempt predecessor and reset")
    finalized = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return-disposition' AND scope=? AND key=?",
                           (snapshot.binding.fence.key.repository, str(admission.attempt.run_id))).fetchone()
    if finalized is None and not unlaunched:
        raise WorkflowAuthorityError("split phase is already consumed by an unfinished paid attempt")
    if finalized is not None:
        receipt = decode_workflow(finalized["payload_json"], FinalizedResolveReturn)
        if receipt.result.attempt != admission.attempt or receipt.result.kind is not ResolveResultKind.INFRA_FAILURE:
            raise WorkflowAuthorityError("split phase is already completed")
    if admission.split_fingerprint != metadata.get("split_fingerprint") or admission.input_fingerprint != metadata.get("input_fingerprint"):
        raise WorkflowAuthorityError("split retry cannot replace the admitted continuation inputs")
    run = db.execute("SELECT dispatch_id FROM runs WHERE run_id=?", (str(admission.attempt.run_id),)).fetchone()
    if run is None or json.loads(run["dispatch_id"]).get("resolve_inputs") != metadata.get("resolve_inputs"):
        raise WorkflowAuthorityError("split retry cannot replace its issue observation inputs")
SCHEMA_BASELINE_VERSION = 9
LEGACY_V8_SCHEMA_DIGEST = (
    "1740c6b03aae7ba5beccea7dd522d0859396e901dee6a40d12ff4284621745b5"
)
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_RESOLVE_PAID_KINDS = frozenset(stage.value for stage in ResolveStage)
# Paid review legs are admitted under the dispatched agent kind's name.
_REVIEW_PAID_KINDS = frozenset({"REVIEW", "RE_REVIEW"})
# ADR 0015 review slice: the only effects a review-rejection re-arm or review
# completion may own. The PR target and each command's exact guard are
# enforced separately at enqueue (review_family_op, bound_submission_op).
_REVIEW_FAMILY_COMMAND_KINDS = frozenset({CommandKind.DISMISS_REVIEW, CommandKind.RELABEL, CommandKind.SUBMIT_REVIEW})
AUTHORITY_WITNESS_FORMAT = "issue-agent-control-authority-v1"


# One operator signal per review escalation (increment 6 N2): claimed by
# escalation identity, and each succeeded hold projection is examined once.
ESCALATION_SIGNAL_NAMESPACE = "review-escalation-signal"
ESCALATION_SIGNAL_COMMAND_NAMESPACE = "review-escalation-signal-command"
# An escalation whose signal is owed although no succeeded receipt of its own
# will carry it: its hold was found already on GitHub, its re-plans were
# exhausted, or its restoration was contested (written by the review workflow
# control). Scoped by the owing executor identity, so the executor's owner
# lineage filters the rows in SQL before any claim limit applies.
ESCALATION_SIGNAL_OWED_NAMESPACE = "review-escalation-signal-owed"
ESCALATION_SIGNAL_REASONS = frozenset({"hold_verified", "projection_exhausted", "hold_contested"})
# A malformed owed record is quarantined once, per row (same scope and key),
# and journalled: at-most-once paging never wedges every drain on one corrupt
# row, and a well-formed signal of the same escalation is still sent.
ESCALATION_SIGNAL_QUARANTINE_NAMESPACE = "review-escalation-signal-quarantined"


def owe_escalation_signal(
    db: sqlite3.Connection, *, executor_identity: str, escalation: str, pr: int, reason: str, at: datetime,
) -> None:
    """Record, once, that this escalation's operator signal is owed; the executor's claim sends it."""
    if reason not in ESCALATION_SIGNAL_REASONS:
        raise ValueError(f"unknown escalation signal reason: {reason!r}")
    if type(pr) is not int or pr < 1:
        raise ValueError("an escalation signal names a pull request number")
    if not executor_identity or not escalation:
        raise ValueError("an escalation signal names its executor and its escalation")
    db.execute(
        "INSERT OR IGNORE INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
        (ESCALATION_SIGNAL_OWED_NAMESPACE, executor_identity, escalation, at.isoformat(),
         json.dumps({"pr": pr, "reason": reason}, sort_keys=True)),
    )

class ControlStoreError(RuntimeError):
    pass


def _owed_escalation_signal(raw: str) -> tuple[int, str] | str:
    """An owed signal's (pull request, reason), or why the record is malformed."""
    try:
        payload = json.loads(raw)
        number, reason = payload["pr"], payload["reason"]
    except (ValueError, TypeError, KeyError) as exc:
        return f"unparseable owed signal: {type(exc).__name__}: {str(exc)[:200]}"
    if type(number) is not int or number < 1:
        return "owed signal names no pull request number"
    if not isinstance(reason, str) or reason not in ESCALATION_SIGNAL_REASONS:
        return "owed signal names an unknown reason"
    return number, reason


class IncompatibleSchema(ControlStoreError):
    pass


class CorruptControlStore(ControlStoreError):
    pass


class LegacySpendMigrationError(ControlStoreError):
    pass


class RetiredOwnerError(ControlStoreError):
    pass


class AccountingDisposition(str, Enum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class AccountingSnapshot:
    disposition: AccountingDisposition
    scope: BudgetScope
    settled: Money
    reserved: Money
    detail: str = ""

    @property
    def committed(self) -> Money:
        return self.settled + self.reserved


@dataclass(frozen=True)
class SettledSpend:
    disposition: AccountingDisposition
    total: Money | None
    by_repository: Mapping[str, Money]
    detail: str = ""

    def for_repository(self, repository: str) -> Money | None:
        if self.total is None:
            return None
        return self.by_repository.get(
            canonical_repository(repository),
            Money.zero(self.total.currency),
        )


def read_settled_spend(
    path: str,
    period_start: datetime,
    period_end: datetime,
    *,
    currency: str = "USD",
) -> SettledSpend:
    """Read settled charges without creating or mutating the control store."""

    if period_end <= period_start:
        raise ValueError("spend period end must follow its start")
    start = _stamp(period_start)
    end = _stamp(period_end)
    if not os.path.isfile(path):
        return SettledSpend(
            AccountingDisposition.UNAVAILABLE,
            None,
            MappingProxyType({}),
            f"control store does not exist: {path}",
        )

    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(
            uri,
            uri=True,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
        )
    except sqlite3.Error as exc:
        return SettledSpend(
            AccountingDisposition.UNAVAILABLE,
            None,
            MappingProxyType({}),
            f"control store could not be opened read-only: {exc}",
        )
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA query_only=ON")
        quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise CorruptControlStore(
                f"control-store integrity check failed: {quick_check}"
            )
        checker = SQLiteControlStore(path)
        checker._validate_migration_history(
            checker._migration_history(db),
            require_current=True,
        )
        checker._assert_semantic_integrity(db)
        rows = db.execute(
            "SELECT r.workload_repository, "
            "COALESCE(SUM(c.amount_micros), 0) AS amount "
            "FROM charges c JOIN budget_reservations r ON r.run_id=c.run_id "
            "WHERE c.currency=? AND c.settled_at>=? AND c.settled_at<? "
            "GROUP BY r.workload_repository",
            (currency, start, end),
        ).fetchall()
    except (sqlite3.Error, ControlStoreError) as exc:
        return SettledSpend(
            AccountingDisposition.UNAVAILABLE,
            None,
            MappingProxyType({}),
            f"settled spend could not be read: {exc}",
        )
    finally:
        db.close()
    by_repository = {
        str(row["workload_repository"]): Money(
            currency, int(row["amount"])
        )
        for row in rows
    }
    return SettledSpend(
        AccountingDisposition.AVAILABLE,
        Money(currency, sum(item.micros for item in by_repository.values())),
        MappingProxyType(by_repository),
    )


@dataclass(frozen=True)
class ReservationRequest:
    reservation_id: ReservationId
    run_id: RunId
    dispatch_id: str
    scope: BudgetScope
    quote: PriceQuote
    cap: Money
    config_version: str
    workload_repository: str
    additional_limits: tuple["BudgetLimit", ...] = ()
    kind: str = "paid"
    owner_id: str = "reservation"
    workflow: WorkflowBinding | None = None
    adjudication_ceiling: int | None = None
    review_child: ReviewChildIdentity | None = None
    continuations: tuple[cc.ContinuationPurchase, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.continuations, tuple) or len(self.continuations) > 3
                or any(not isinstance(item, (cc.IssueRecoveryPurchase, cc.IssueReplyPurchase, cc.IssueCIPurchase, cc.IssueReviewPurchase, cc.PRRepairPurchase, cc.StewardPurchase)) for item in self.continuations)):
            raise ValueError("reservation requires bounded fixed continuation purchases")
        if len({item.capture.binding.authority.family for item in self.continuations}) != len(self.continuations):
            raise ValueError("reservation selects each continuation family once")
        workload_repository = canonical_repository(
            self.workload_repository
        )
        object.__setattr__(
            self, "workload_repository", workload_repository
        )
        if self.workflow is not None and (
            not isinstance(self.workflow, WorkflowBinding)
            or self.workflow.fence.key.repository != workload_repository
        ):
            raise ValueError("reservation workflow binding must match its workload repository")
        if any(item.capture.binding.fence.key.repository != workload_repository for item in self.continuations):
            raise ValueError("reservation continuation belongs to another repository")
        if self.adjudication_ceiling is not None:
            validate_workflow_integer(self.adjudication_ceiling, "adjudication ceiling")
            if self.workflow is None or self.kind != ResolveStage.ADJUDICATE.value:
                raise ValueError("adjudication ceiling requires a bound adjudication admission")
        if self.review_child is not None and (
            not isinstance(self.review_child, ReviewChildIdentity)
            or self.workflow != self.review_child.round.binding.rejection
            or self.kind not in _REVIEW_PAID_KINDS
        ):
            raise ValueError("planned review child requires its matching rejection binding and review leg")
        if self.scope.currency != self.quote.maximum_charge.currency:
            raise ValueError("reservation scope and quote currency differ")
        if self.cap.currency != self.scope.currency:
            raise ValueError("reservation cap and scope currency differ")
        if not str(self.reservation_id) or not str(self.run_id):
            raise ValueError("reservation and run identities are required")
        if (
            not self.config_version
            or not self.kind
            or not self.owner_id
            or not workload_repository
        ):
            raise ValueError(
                "configuration version, run kind, owner identity, and workload "
                "repository are required"
            )
        for limit in self.additional_limits:
            if limit.scope.currency != self.scope.currency:
                raise ValueError("all reservation limits must use one currency")


@dataclass(frozen=True)
class BudgetLimit:
    """One independently enforced ceiling attached to a reservation."""

    scope: BudgetScope
    cap: Money

    def __post_init__(self) -> None:
        if self.scope.currency != self.cap.currency:
            raise ValueError("budget limit scope and cap currency differ")


@dataclass(frozen=True)
class ReservationResult:
    disposition: AccountingDisposition
    reservation_id: ReservationId | None
    committed: Money
    requested: Money
    cap: Money
    detail: str = ""
    # The limit that actually tripped, which is not always the primary scope
    # when a leg carries repository, global, and issue caps at once.
    tripped_dimension: str = ""

    @property
    def admitted(self) -> bool:
        return self.disposition is AccountingDisposition.AVAILABLE


@dataclass(frozen=True)
class SettlementResult:
    disposition: AccountingDisposition
    amount: Money
    idempotent: bool = False
    variance: Money | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReleaseResult:
    disposition: AccountingDisposition
    run_id: RunId
    idempotent: bool = False
    detail: str = ""


@dataclass(frozen=True)
class Lease:
    run_id: RunId
    dispatch_id: str
    owner_id: str
    pid: int
    pgid: int
    started_at: datetime
    process_start_ticks: int = 0


@dataclass(frozen=True)
class _DispatchClaimObservation:
    target: str
    leg: str
    owner_id: str
    owner_pid: int
    owner_start_ticks: int
    occurrence_id: str
    claimed_at: str
    paths_json: str
    bound_runs: tuple[tuple[str, str, int, int], ...]


@dataclass(frozen=True)
class ActiveDispatchClaim:
    """One live paid claim, as another role observes it.

    ``target_key`` uses the harness-wide signed convention: positive is an
    issue number, negative is ``-pr`` for a PR with no resolvable issue.
    ``claim_key`` identifies this CLAIM EPISODE for once-per-key journalling.
    Deliberately NOT the bound run id: a claim is taken at decision time and
    bound to a run later (and may bind several), so a run-derived key changes
    under an observer mid-hold and can be picked nondeterministically when two
    runs are live.
    """

    target_key: int
    role: str
    leg: str
    owner_id: str
    claimed_at: datetime
    claim_key: str


@dataclass(frozen=True)
class PublicationAuthority:
    run_id: RunId
    repository: str
    owner_id: str
    role: str
    kind: str
    issue: int | None
    cwd: str
    branch: str
    pgid: int
    process_start_ticks: int
    workflow: WorkflowBinding | None = None


@dataclass(frozen=True)
class RunCancellationIntent:
    run_id: RunId
    dispatch_id: str
    kind: str
    issue_number: int | None
    reason: str


@dataclass(frozen=True)
class PendingCommand:
    command: LifecycleCommand
    state: str
    attempts: int
    # Verified GitHub REFUSALS only — readbacks that proved the requested state
    # absent. `attempts` also counts executions an outage interrupted, so the
    # ADR 0012 retry ceiling reads this instead (see control-store v18).
    refusals: int = 0


@dataclass(frozen=True)
class MigrationReport:
    migration_id: str
    source_digest: str
    imported: int
    rejected: int
    amount: Money
    idempotent: bool = False


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    source: str
    transform_spec: str = ""

    @property
    def digest(self) -> str:
        material = self.source if not self.transform_spec else self.source + "\0" + self.transform_spec
        return hashlib.sha256(material.encode()).hexdigest()


_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    source_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('reserved','starting','running','completed','cancelled','abandoned')),
    config_version TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    outcome TEXT,
    provider_receipt_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS leases (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    dispatch_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid > 0),
    pgid INTEGER NOT NULL CHECK (pgid > 0),
    process_start_ticks INTEGER NOT NULL DEFAULT 0
        CHECK (process_start_ticks >= 0),
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dedupe_keys (
    namespace TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    reusable_after TEXT,
    payload_json TEXT,
    PRIMARY KEY (namespace, scope, key)
);

CREATE TABLE IF NOT EXISTS level_state (
    namespace TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, scope, key)
);

CREATE TABLE IF NOT EXISTS lifecycle_commands (
    command_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    logical_key TEXT NOT NULL,
    occurrence INTEGER NOT NULL DEFAULT 0 CHECK (occurrence >= 0),
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    expected_revision TEXT,
    config_version TEXT NOT NULL DEFAULT '',
    repository TEXT NOT NULL DEFAULT '',
    executor_identity TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('pending','executing','succeeded','retryable_failure',
         'permanent_failure','unknown_outcome')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS effect_receipts (
    command_id TEXT PRIMARY KEY
        REFERENCES lifecycle_commands(command_id) ON DELETE RESTRICT,
    executor_identity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('succeeded','retryable_failure','permanent_failure','unknown_outcome')),
    remote_object_id TEXT,
    remote_revision TEXT,
    error TEXT,
    receipt_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
    repository TEXT NOT NULL,
    dimension TEXT NOT NULL,
    period_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_timezone TEXT NOT NULL,
    currency TEXT NOT NULL,
    maximum_micros INTEGER NOT NULL CHECK (maximum_micros >= 0),
    cap_micros INTEGER NOT NULL CHECK (cap_micros >= 0),
    model TEXT NOT NULL,
    rate_card_id TEXT NOT NULL,
    rate_card_version TEXT NOT NULL,
    quote_effective_at TEXT NOT NULL,
    config_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active','settled','released')),
    release_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reservations_scope_state
ON budget_reservations(repository, dimension, period_id, currency, state);

CREATE TABLE IF NOT EXISTS reservation_limits (
    run_id TEXT NOT NULL REFERENCES budget_reservations(run_id) ON DELETE RESTRICT,
    repository TEXT NOT NULL,
    dimension TEXT NOT NULL,
    period_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_timezone TEXT NOT NULL,
    currency TEXT NOT NULL,
    cap_micros INTEGER NOT NULL CHECK (cap_micros >= 0),
    PRIMARY KEY (run_id, repository, dimension, period_id, currency)
);

CREATE INDEX IF NOT EXISTS reservation_limits_scope
ON reservation_limits(repository, dimension, period_id, currency);

CREATE TABLE IF NOT EXISTS charges (
    charge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
    provider_receipt_id TEXT NOT NULL UNIQUE,
    amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0),
    currency TEXT NOT NULL,
    basis TEXT NOT NULL,
    usage_json TEXT,
    rate_card_id TEXT,
    rate_card_version TEXT,
    settled_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS charges_scope
ON charges(run_id, currency);

CREATE TABLE IF NOT EXISTS quota_debits (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    quota TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units >= 0),
    calculation_version TEXT NOT NULL,
    PRIMARY KEY (run_id, quota)
);

CREATE TABLE IF NOT EXISTS event_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    destination TEXT NOT NULL DEFAULT '',
    event_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    emitted_at TEXT,
    failed_at TEXT,
    failure TEXT
);

CREATE TABLE IF NOT EXISTS world_observations (
    deployment TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_number INTEGER NOT NULL CHECK (subject_number > 0),
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (deployment, subject_kind, subject_number)
);

CREATE TABLE IF NOT EXISTS migration_imports (
    migration_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    amount_micros INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
"""

_MIGRATION_V10 = """
CREATE TABLE run_cancellation_intents (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE RESTRICT,
    dispatch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    issue_number INTEGER CHECK (issue_number IS NULL OR issue_number > 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    handover_written_at TEXT
);
"""

_MIGRATION_V11 = """
CREATE TABLE dispatch_claims (
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    target TEXT NOT NULL,
    leg TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_start_ticks INTEGER NOT NULL CHECK (owner_start_ticks > 0),
    paths_json TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (repository, role, target)
);

CREATE UNIQUE INDEX runs_active_dispatch_unique
ON runs(dispatch_id)
WHERE state IN ('reserved', 'starting', 'running');
"""

_MIGRATION_V12 = """
DROP INDEX IF EXISTS runs_active_dispatch_unique;
"""

_MIGRATION_V13 = """
UPDATE lifecycle_commands
SET repository=LOWER(TRIM(repository))
WHERE repository <> LOWER(TRIM(repository));

UPDATE budget_reservations
SET repository=LOWER(TRIM(repository))
WHERE repository <> '*' AND repository <> LOWER(TRIM(repository));

UPDATE reservation_limits
SET repository=LOWER(TRIM(repository))
WHERE repository <> '*' AND repository <> LOWER(TRIM(repository));

UPDATE dispatch_claims
SET repository=LOWER(TRIM(repository))
WHERE repository <> LOWER(TRIM(repository));
"""

_MIGRATION_V14 = """
CREATE TABLE dispatch_claim_runs (
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    target TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    bound_at TEXT NOT NULL,
    PRIMARY KEY (repository, role, target, run_id),
    FOREIGN KEY (repository, role, target)
        REFERENCES dispatch_claims(repository, role, target)
        ON DELETE CASCADE
);

CREATE INDEX dispatch_claim_runs_run
ON dispatch_claim_runs(run_id);
"""

_MIGRATION_V15 = """
CREATE TABLE retired_owners (
    owner_id TEXT PRIMARY KEY,
    successor_owner_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    CHECK (owner_id <> ''),
    CHECK (successor_owner_id <> ''),
    CHECK (repository <> ''),
    CHECK (role <> ''),
    CHECK (owner_id <> successor_owner_id)
);

CREATE INDEX retired_owners_successor
ON retired_owners(successor_owner_id);
"""

_MIGRATION_V16 = """
CREATE TABLE reservation_recovery (
    run_id TEXT PRIMARY KEY REFERENCES budget_reservations(run_id)
        ON DELETE CASCADE,
    policy TEXT NOT NULL CHECK (policy IN ('spawn_pending', 'running',
        'reconcile_only')),
    reason TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO reservation_recovery(run_id, policy, reason, updated_at)
SELECT run_id, 'reconcile_only',
       'reservation predates durable recovery classification', updated_at
FROM budget_reservations
WHERE state='active';
"""

_MIGRATION_V17 = """
ALTER TABLE budget_reservations
ADD COLUMN workload_repository TEXT NOT NULL DEFAULT '*'
    CHECK (workload_repository <> '');

UPDATE budget_reservations
SET workload_repository = CASE
    WHEN repository <> '*' THEN repository
    WHEN json_valid((
        SELECT dispatch_id FROM runs
        WHERE runs.run_id=budget_reservations.run_id
    )) THEN CASE
        WHEN json_type((
            SELECT dispatch_id FROM runs
            WHERE runs.run_id=budget_reservations.run_id
        ), '$.repository')='text'
        AND TRIM(json_extract((
            SELECT dispatch_id FROM runs
            WHERE runs.run_id=budget_reservations.run_id
        ), '$.repository')) NOT IN ('', '*')
        THEN LOWER(TRIM(json_extract((
            SELECT dispatch_id FROM runs
            WHERE runs.run_id=budget_reservations.run_id
        ), '$.repository')))
        ELSE '*'
    END
    ELSE '*'
END;

UPDATE budget_reservations
SET workload_repository = (
    SELECT MIN(repository)
    FROM reservation_limits
    WHERE reservation_limits.run_id=budget_reservations.run_id
      AND reservation_limits.repository<>'*'
)
WHERE workload_repository='*'
  AND EXISTS (
      SELECT 1
      FROM reservation_limits
      WHERE reservation_limits.run_id=budget_reservations.run_id
        AND reservation_limits.repository<>'*'
  );

UPDATE reservation_recovery
SET policy='spawn_pending',
    reason='pre-v16 reservation durably proven unlaunched',
    updated_at=(
        SELECT updated_at FROM budget_reservations
        WHERE budget_reservations.run_id=reservation_recovery.run_id
    )
WHERE policy='reconcile_only'
  AND reason='reservation predates durable recovery classification'
  AND EXISTS (
      SELECT 1
      FROM budget_reservations AS reservation
      JOIN runs AS run ON run.run_id=reservation.run_id
      WHERE reservation.run_id=reservation_recovery.run_id
        AND reservation.state='active'
        AND run.state IN ('reserved', 'starting')
        AND NOT EXISTS (
            SELECT 1 FROM leases
            WHERE leases.run_id=reservation.run_id
        )
  );
"""

_MIGRATION_V18 = """
ALTER TABLE lifecycle_commands
ADD COLUMN refusals INTEGER NOT NULL DEFAULT 0 CHECK (refusals >= 0);

UPDATE lifecycle_commands
SET refusals = 1
WHERE state='retryable_failure';
"""

_MIGRATION_V19 = """
CREATE TABLE workflow_authority_epochs (
    repository TEXT NOT NULL,
    family TEXT NOT NULL CHECK (length(family) BETWEEN 1 AND 64),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    source_digest TEXT NOT NULL,
    inventory_digest TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    PRIMARY KEY (repository, family)
);
CREATE TABLE workflows (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('issue', 'pr')),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    version INTEGER NOT NULL CHECK (version >= 0),
    episode_json TEXT NOT NULL,
    PRIMARY KEY (repository, target_kind, target_number)
);
CREATE TABLE workflow_gates (
    gate_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    opened_generation INTEGER NOT NULL CHECK (opened_generation >= 0),
    cause_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'rearming', 'closed')),
    payload_json TEXT NOT NULL,
    UNIQUE (repository, target_kind, target_number, opened_generation, cause_key),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE TABLE workflow_rearms (
    request_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    gate_id TEXT NOT NULL REFERENCES workflow_gates(gate_id),
    from_generation INTEGER NOT NULL CHECK (from_generation >= 0),
    to_generation INTEGER NOT NULL CHECK (to_generation = from_generation + 1),
    state TEXT NOT NULL CHECK (state IN
        ('pending', 'completed', 'superseded', 'cancelled')),
    request_digest TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    UNIQUE (repository, target_kind, target_number, to_generation),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE UNIQUE INDEX workflow_one_pending_rearm
    ON workflow_rearms(repository, target_kind, target_number)
    WHERE state = 'pending';
CREATE TABLE workflow_rearm_commands (
    request_id TEXT NOT NULL REFERENCES workflow_rearms(request_id),
    operation_key TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE REFERENCES lifecycle_commands(command_id),
    disposition TEXT NOT NULL CHECK (disposition IN ('required', 'retired')),
    disposition_evidence_json TEXT NOT NULL,
    PRIMARY KEY (request_id, operation_key)
);
CREATE TABLE workflow_evidence (
    evidence_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    command_id TEXT REFERENCES lifecycle_commands(command_id),
    kind TEXT NOT NULL CHECK (kind IN ('observation', 'absence', 'retry_applicability', 'prior_receipt')),
    payload_json TEXT NOT NULL,
    CHECK (kind = 'observation' OR command_id IS NOT NULL),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE TABLE workflow_retry_authorizations (
    authorization_id TEXT PRIMARY KEY,
    request_id TEXT REFERENCES workflow_rearms(request_id),
    command_id TEXT NOT NULL REFERENCES lifecycle_commands(command_id),
    gate_id TEXT NOT NULL REFERENCES workflow_gates(gate_id),
    association_kind TEXT NOT NULL CHECK (association_kind IN ('standalone', 'rearm')),
    binding_json TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES workflow_evidence(evidence_id),
    request_digest TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    consumed_attempt INTEGER CHECK (consumed_attempt > 0),
    CHECK ((association_kind = 'standalone' AND request_id IS NULL)
        OR (association_kind = 'rearm' AND request_id IS NOT NULL))
);
ALTER TABLE runs ADD COLUMN workflow_binding_json TEXT;
ALTER TABLE lifecycle_commands ADD COLUMN workflow_binding_json TEXT;
ALTER TABLE lifecycle_commands ADD COLUMN workflow_reconciliation_json TEXT;
ALTER TABLE lifecycle_commands ADD COLUMN attempt_owner TEXT;
"""

_MIGRATION_V20 = """
-- The transaction also runs _backfill_workflow_episode_index_v20, validating
-- legacy binding values before recording their immutable episode projection.
ALTER TABLE runs ADD COLUMN workflow_episode_key TEXT;
UPDATE runs SET outcome='unlaunched_released'
WHERE state='cancelled' AND outcome='released' AND provider_receipt_id IS NULL
    AND EXISTS(SELECT 1 FROM budget_reservations b
               WHERE b.run_id=runs.run_id AND b.state='released')
    AND EXISTS(SELECT 1 FROM reservation_recovery rr
               WHERE rr.run_id=runs.run_id AND rr.policy='spawn_pending')
    AND NOT EXISTS(SELECT 1 FROM charges c WHERE c.run_id=runs.run_id)
    AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.run_id=runs.run_id);
CREATE INDEX runs_adjudication_episode ON runs(workflow_episode_key)
WHERE kind='ADJUDICATE'
    AND NOT(state='cancelled' AND outcome IS 'unlaunched_released' AND provider_receipt_id IS NULL);
CREATE TRIGGER runs_require_workflow_episode_insert BEFORE INSERT ON runs
WHEN (NEW.workflow_binding_json IS NULL) != (NEW.workflow_episode_key IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'workflow admission requires its episode projection');
END;
CREATE TRIGGER runs_require_workflow_episode_update BEFORE UPDATE ON runs
WHEN (NEW.workflow_binding_json IS NULL) != (NEW.workflow_episode_key IS NULL)
    OR (OLD.workflow_episode_key IS NOT NULL AND NEW.workflow_episode_key IS NOT OLD.workflow_episode_key)
BEGIN
    SELECT RAISE(ABORT, 'workflow episode projection is immutable');
END;
"""

_V20_TRANSFORM_SPEC = """workflow-episode-index-v1:
Decode every non-null immutable run binding with the tagged workflow codec.
Require a matching reservation repository and immutable admission run, binding,
stage and leg identity. Persist compact UTF-8 JSON array of repository, typed
target discriminator (issue/pr), target number, family, epoch and episode;
generation is excluded.
Iterate with bounded memory inside the same migration transaction. Any invalid
bound history aborts the entire migration. Never use the current workflow row.
"""


def _backfill_workflow_episode_index_v20(db: sqlite3.Connection) -> None:
    # This versioned transform belongs to v20's immutable migration contract.
    # Ambiguous historical authority blocks migration, never buys fresh rounds.
    for row in db.execute("SELECT run_id,kind,dispatch_id,workflow_binding_json FROM runs WHERE workflow_binding_json IS NOT NULL"):
        try:
            binding = _indexed_run_binding(db, row)
        except ValueError as exc:
            raise IncompatibleSchema(f"cannot project historical workflow run {row['run_id']!r}: {exc}") from exc
        db.execute("UPDATE runs SET workflow_episode_key=? WHERE run_id=?", (workflow_episode_key(binding), row["run_id"]))


def _backfill_workflow_components_v25(db: sqlite3.Connection) -> None:
    for row in db.execute("SELECT repository,target_kind,target_number,episode_json FROM workflows"):
        try:
            family = decode_workflow(row["episode_json"], tuple)
            if len(family) != 2 or not isinstance(family[0], FamilyAuthority):
                raise ValueError("legacy workflow family is malformed")
            authority, episode = family
            expected = (
                WorkflowFamily.RESOLVE if row["target_kind"] == "issue" and isinstance(episode, ResolveEpisode)
                else WorkflowFamily.REVIEW_REJECTIONS if row["target_kind"] == "pr" and isinstance(episode, ReviewEpisode)
                else None
            )
            if authority.family is not expected:
                raise ValueError("legacy workflow family disagrees with its target")
            active = db.execute(
                "SELECT epoch FROM workflow_authority_epochs WHERE repository=? AND family=?",
                (row["repository"], authority.family.value),
            ).fetchone()
            if active is None or active["epoch"] != authority.epoch:
                raise ValueError("legacy workflow family disagrees with activation")
            db.execute(
                "INSERT INTO workflow_components(repository,target_kind,target_number,family,epoch,episode_id) "
                "VALUES (?,?,?,?,?,?)",
                (row["repository"], row["target_kind"], row["target_number"],
                 authority.family.value, authority.epoch, episode.episode_id),
            )
        except (ValueError, TypeError) as exc:
            raise IncompatibleSchema(f"cannot project historical workflow component: {exc}") from exc
    for row in db.execute("SELECT repository,role,target,leg,owner_id,owner_pid,owner_start_ticks,claimed_at FROM dispatch_claims"):
        digest = hashlib.sha256(_json(tuple(row)).encode()).hexdigest()
        occurrence_id = f"legacy:{digest}"
        db.execute(
            "UPDATE dispatch_claims SET occurrence_id=? WHERE repository=? AND role=? AND target=?",
            (occurrence_id, row["repository"], row["role"], row["target"]),
        )
        _claim_lifecycle_receipt(db, row["repository"], occurrence_id, "created", row["claimed_at"], {
            "role": row["role"], "target": row["target"], "leg": row["leg"],
            "owner_id": row["owner_id"],
            "owner_pid": row["owner_pid"], "owner_start_ticks": row["owner_start_ticks"],
            "claimed_at": row["claimed_at"], "source": "v24-migration",
        })


def _claim_lifecycle_receipt(
    db: sqlite3.Connection, repository: str, occurrence_id: str, event: str,
    observed_at: str, fact: dict[str, object],
) -> None:
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
        ("dispatch-claim-lifecycle", repository, f"{occurrence_id}:{event}",
         observed_at, _json(fact)),
    )


def _review_execution_lineage_migration() -> str:
    # A global witness also covers REPLACE's implicit deletion when SQLite's
    # recursive delete triggers are disabled: every replacement inserts a row.
    tables = (
        "dedupe_keys", "dispatch_claims", "runs", "leases", "budget_reservations",
        "reservation_recovery", "charges", "workflow_evidence", "reservation_limits",
        "quota_debits", "run_cancellation_intents",
    )
    identities = """CREATE TABLE review_archive_identity_members (
    source_table TEXT NOT NULL,
    source_rowid INTEGER NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    first TEXT NOT NULL,
    second TEXT NOT NULL,
    third TEXT NOT NULL,
    types_json TEXT NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY(source_table,source_rowid)
) WITHOUT ROWID;
CREATE INDEX review_archive_identity_lookup ON review_archive_identity_members
    (source_table,snapshot_sha256,first,second,third);
CREATE TABLE review_archive_identity_certifications (
    round_rows_version INTEGER NOT NULL,
    lifecycle_version INTEGER NOT NULL,
    projection_version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    PRIMARY KEY(round_rows_version,lifecycle_version,projection_version)
) WITHOUT ROWID;
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('identity_members',X'',0);
"""
    identities += "\n".join(
        f"""CREATE TRIGGER review_identity_{table}_{event.lower()}
AFTER {event} ON review_archive_identity_{table}
BEGIN
    SELECT RAISE(ABORT,'identity projection witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='identity_members' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='identity_members' AND scope=X'';
END;"""
        for table in ("members", "certifications") for event in ("INSERT", "UPDATE", "DELETE")
    )
    return identities + "\n" + "\n".join(
        f"""CREATE TRIGGER review_lineage_{table}_{event.lower()}
AFTER {event} ON {table}
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;"""
        for table in tables for event in ("INSERT", "UPDATE", "DELETE")
    )


_SCHEMA_MIGRATIONS = (
    # Version 9 is the immutable adoption baseline shipped by the architectural
    # hardening cutover. Its exact source digest already exists in deployed
    # databases, so future code validates rather than rewriting that history.
    SchemaMigration(SCHEMA_BASELINE_VERSION, _DDL),
    SchemaMigration(10, _MIGRATION_V10),
    SchemaMigration(11, _MIGRATION_V11),
    SchemaMigration(12, _MIGRATION_V12),
    SchemaMigration(13, _MIGRATION_V13),
    SchemaMigration(14, _MIGRATION_V14),
    SchemaMigration(15, _MIGRATION_V15),
    SchemaMigration(16, _MIGRATION_V16),
    SchemaMigration(17, _MIGRATION_V17),
    # v18 separates VERIFIED REFUSALS from attempts (ADR 0012). `attempts` is
    # incremented by every `begin_command`, including the ones a GitHub outage
    # produced (unknown_outcome → retry_reconciled → re-execute), so a ten-tick
    # degradation used to pre-burn the retry ceiling and retire a command on
    # its first genuine 403. Existing `retryable_failure` rows backfill to 1 —
    # exactly what that state asserts (the last receipt was a refusal) — rather
    # than to `attempts`, which would import the very over-count being fixed.
    SchemaMigration(18, _MIGRATION_V18),
    SchemaMigration(19, _MIGRATION_V19),
    SchemaMigration(20, _MIGRATION_V20, _V20_TRANSFORM_SPEC),
    SchemaMigration(21, """
CREATE INDEX runs_workflow_scope ON runs(workflow_episode_key);
CREATE INDEX lifecycle_publication_runs ON lifecycle_commands(CAST(json_extract(payload_json,'$.run_id') AS TEXT))
WHERE kind IN ('publish_branch','open_pull_request','update_pull_request_body')
AND state NOT IN ('succeeded','permanent_failure');
"""),
    # ADR 0015 family activation evidence: one immutable decision per imported
    # target, bound to its epoch and installed workflow, plus one row per
    # activation recording its ceiling and confirmed plan digest, so even a
    # row-less activation (nothing to import) can be recognised exactly.
    SchemaMigration(22, """
CREATE TABLE workflow_activation_imports (
    repository TEXT NOT NULL,
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    target_kind TEXT NOT NULL CHECK (target_kind IN ('issue', 'pr')),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    source_digest TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (repository, family, target_kind, target_number),
    FOREIGN KEY (repository, family)
        REFERENCES workflow_authority_epochs (repository, family),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE TABLE workflow_activation_evidence (
    repository TEXT NOT NULL,
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    ceiling INTEGER NOT NULL CHECK (ceiling >= 0),
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64),
    PRIMARY KEY (repository, family, epoch),
    FOREIGN KEY (repository, family)
        REFERENCES workflow_authority_epochs (repository, family)
);
"""),
    # ADR 0015 retry grants (review 2026-09-25). Every grant ends terminal;
    # an open grant is the only row that can match; at most one exists per
    # command; a disposition never changes. An open grant may only record its
    # single schedule bypass and count unavailable reads toward its ceiling.
    # Legacy consumed rows take their command's last update as the attempt
    # time, which only ever demands a later absence observation.
    SchemaMigration(23, """
ALTER TABLE workflow_retry_authorizations ADD COLUMN disposition TEXT
    CHECK (disposition IN ('consumed', 'not_needed', 'inapplicable', 'void'));
ALTER TABLE workflow_retry_authorizations ADD COLUMN disposed_at TEXT;
ALTER TABLE workflow_retry_authorizations ADD COLUMN disposition_reason TEXT;
ALTER TABLE workflow_retry_authorizations ADD COLUMN accepted_at TEXT;
ALTER TABLE workflow_retry_authorizations ADD COLUMN first_visit_at TEXT;
ALTER TABLE workflow_retry_authorizations ADD COLUMN unavailable_reads INTEGER NOT NULL DEFAULT 0
    CHECK (unavailable_reads >= 0);
UPDATE workflow_retry_authorizations
SET disposition='consumed', disposition_reason='authorized attempt began',
    disposed_at=(SELECT updated_at FROM lifecycle_commands c
                 WHERE c.command_id=workflow_retry_authorizations.command_id)
WHERE consumed_attempt IS NOT NULL;
UPDATE workflow_retry_authorizations
SET accepted_at=(SELECT updated_at FROM lifecycle_commands c
                 WHERE c.command_id=workflow_retry_authorizations.command_id);
CREATE UNIQUE INDEX workflow_retry_open_grant ON workflow_retry_authorizations(command_id)
WHERE disposition IS NULL;
CREATE TRIGGER workflow_retry_grant_insert BEFORE INSERT ON workflow_retry_authorizations
WHEN NEW.disposition IS NOT NULL OR NEW.consumed_attempt IS NOT NULL OR NEW.disposed_at IS NOT NULL
    OR NEW.disposition_reason IS NOT NULL OR NEW.first_visit_at IS NOT NULL
    OR NEW.unavailable_reads != 0 OR NEW.accepted_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'a retry grant is created open');
END;
CREATE TRIGGER workflow_retry_grant_update BEFORE UPDATE ON workflow_retry_authorizations
WHEN OLD.disposition IS NOT NULL
    OR NEW.authorization_id IS NOT OLD.authorization_id OR NEW.request_id IS NOT OLD.request_id
    OR NEW.command_id IS NOT OLD.command_id OR NEW.gate_id IS NOT OLD.gate_id
    OR NEW.association_kind IS NOT OLD.association_kind OR NEW.binding_json IS NOT OLD.binding_json
    OR NEW.evidence_id IS NOT OLD.evidence_id OR NEW.request_digest IS NOT OLD.request_digest
    OR NEW.actor_json IS NOT OLD.actor_json OR NEW.generation IS NOT OLD.generation
    OR NEW.accepted_at IS NOT OLD.accepted_at
    OR (OLD.first_visit_at IS NOT NULL AND NEW.first_visit_at IS NOT OLD.first_visit_at)
    OR NEW.unavailable_reads NOT IN (OLD.unavailable_reads, OLD.unavailable_reads + 1)
    OR (NEW.disposition IS NULL AND (NEW.disposed_at IS NOT NULL OR NEW.disposition_reason IS NOT NULL
        OR NEW.consumed_attempt IS NOT NULL))
    OR (NEW.disposition IS NOT NULL AND (NEW.disposed_at IS NULL OR NEW.disposition_reason IS NULL
        OR (NEW.disposition = 'consumed') != (NEW.consumed_attempt IS NOT NULL)))
BEGIN
    SELECT RAISE(ABORT, 'a retry grant is disposed exactly once and is otherwise immutable');
END;
"""),
    # ADR 0015 review slice. `prewrite_read_json` is the bounded schedule of a
    # pending command whose pre-write guard read was unavailable (ADR 0012
    # ceiling); it is not outcome uncertainty and never enters the
    # reconciliation JSON. The remote-object index serves the one-receipt-per-
    # review check on bound review submissions.
    SchemaMigration(24, """
ALTER TABLE lifecycle_commands ADD COLUMN prewrite_read_json TEXT;
CREATE INDEX effect_receipts_remote_object ON effect_receipts(remote_object_id)
WHERE remote_object_id IS NOT NULL;
"""),
    SchemaMigration(25, """
CREATE TABLE workflow_components (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    episode_id TEXT NOT NULL CHECK (length(episode_id) BETWEEN 1 AND 512),
    PRIMARY KEY (repository,target_kind,target_number,family),
    FOREIGN KEY (repository,target_kind,target_number)
        REFERENCES workflows(repository,target_kind,target_number),
    CHECK ((family='resolve' AND target_kind='issue') OR
           (family IN ('review_rejections','review_execution') AND target_kind='pr'))
);
CREATE INDEX workflow_components_family ON workflow_components(repository,family);
CREATE TABLE review_execution_rounds (
    round_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind='pr'),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    execution_epoch INTEGER NOT NULL CHECK (execution_epoch > 0),
    execution_episode TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    claim_occurrence_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    planning_json TEXT NOT NULL,
    closure_json TEXT,
    outcome_json TEXT,
    UNIQUE (repository,target_number,execution_epoch,execution_episode,sequence),
    FOREIGN KEY (repository,target_kind,target_number)
        REFERENCES workflows(repository,target_kind,target_number)
);
CREATE INDEX review_execution_rounds_episode
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence);
CREATE INDEX review_execution_rounds_open
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence)
WHERE outcome_json IS NULL;
CREATE INDEX review_execution_rounds_unclosed
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence)
WHERE closure_json IS NULL;
ALTER TABLE runs ADD COLUMN review_round_id TEXT REFERENCES review_execution_rounds(round_id);
ALTER TABLE runs ADD COLUMN review_child_id TEXT;
CREATE UNIQUE INDEX runs_review_child ON runs(review_round_id,review_child_id)
WHERE review_round_id IS NOT NULL;
CREATE TRIGGER runs_review_attachment_insert BEFORE INSERT ON runs
WHEN (NEW.review_round_id IS NULL) != (NEW.review_child_id IS NULL)
BEGIN SELECT RAISE(ABORT,'review round and child must be attached together'); END;
CREATE TRIGGER runs_review_attachment_update BEFORE UPDATE ON runs
WHEN (NEW.review_round_id IS NULL) != (NEW.review_child_id IS NULL)
    OR NEW.review_round_id IS NOT OLD.review_round_id
    OR NEW.review_child_id IS NOT OLD.review_child_id
BEGIN SELECT RAISE(ABORT,'review run attachment is immutable'); END;
ALTER TABLE dispatch_claims ADD COLUMN occurrence_id TEXT;
CREATE UNIQUE INDEX dispatch_claims_occurrence ON dispatch_claims(occurrence_id);
CREATE TRIGGER dispatch_claims_occurrence_insert BEFORE INSERT ON dispatch_claims
WHEN NEW.occurrence_id IS NULL OR length(NEW.occurrence_id)=0
BEGIN SELECT RAISE(ABORT,'dispatch claim requires an occurrence'); END;
CREATE TRIGGER dispatch_claims_occurrence_update BEFORE UPDATE ON dispatch_claims
WHEN NEW.occurrence_id IS NULL OR length(NEW.occurrence_id)=0
    OR (OLD.occurrence_id IS NOT NULL AND NEW.occurrence_id IS NOT OLD.occurrence_id)
BEGIN SELECT RAISE(ABORT,'dispatch claim occurrence is immutable'); END;
"""),
    SchemaMigration(26, """
CREATE INDEX review_execution_rounds_repository
ON review_execution_rounds(repository);
"""),
    SchemaMigration(27, """
CREATE TABLE review_archive_source_versions (
    source TEXT NOT NULL CHECK (typeof(source)='text' AND length(source) BETWEEN 1 AND 64),
    scope BLOB NOT NULL CHECK (typeof(scope)='blob'),
    version INTEGER NOT NULL CHECK (
        typeof(version)='integer' AND version BETWEEN 0 AND 9223372036854775806
    ),
    PRIMARY KEY (source,scope)
) WITHOUT ROWID;
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('round_receipts',X'',0);
INSERT INTO review_archive_source_versions(source,scope,version)
SELECT 'round_rows',CAST(repository AS BLOB),0
FROM workflows GROUP BY CAST(repository AS BLOB);
CREATE TRIGGER review_workflow_round_source_insert AFTER INSERT ON workflows
WHEN NOT EXISTS (
    SELECT 1 FROM review_archive_source_versions
    WHERE source='round_rows' AND scope=CAST(NEW.repository AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'existing workflow repository lost its round source witness')
    WHERE EXISTS (
        SELECT 1 FROM workflows
        WHERE repository IN (
            NEW.repository,CAST(NEW.repository AS BLOB),CAST(NEW.repository AS TEXT)
        ) AND rowid<>NEW.rowid LIMIT 1
    );
    INSERT INTO review_archive_source_versions(source,scope,version)
    VALUES ('round_rows',CAST(NEW.repository AS BLOB),0);
END;
-- REPLACE may remove a conflicting row without firing its DELETE trigger.
CREATE TRIGGER review_round_source_no_replace_insert BEFORE INSERT ON review_execution_rounds
WHEN EXISTS (
    SELECT 1 FROM review_execution_rounds
    WHERE round_id=NEW.round_id OR (
        repository=NEW.repository AND target_number=NEW.target_number
        AND execution_epoch=NEW.execution_epoch AND execution_episode=NEW.execution_episode
        AND sequence=NEW.sequence
    ) LIMIT 1
)
BEGIN
    SELECT RAISE(ABORT,'review round replacement would hide a source mutation');
END;
CREATE TRIGGER review_round_source_no_replace_update BEFORE UPDATE ON review_execution_rounds
WHEN (
    NEW.round_id IS NOT OLD.round_id OR NEW.repository IS NOT OLD.repository
    OR NEW.target_number IS NOT OLD.target_number
    OR NEW.execution_epoch IS NOT OLD.execution_epoch
    OR NEW.execution_episode IS NOT OLD.execution_episode
    OR NEW.sequence IS NOT OLD.sequence
) AND EXISTS (
    SELECT 1 FROM review_execution_rounds
    WHERE rowid<>OLD.rowid AND (
        round_id=NEW.round_id OR (
            repository=NEW.repository AND target_number=NEW.target_number
            AND execution_epoch=NEW.execution_epoch AND execution_episode=NEW.execution_episode
            AND sequence=NEW.sequence
        )
    ) LIMIT 1
)
BEGIN
    SELECT RAISE(ABORT,'review round replacement would hide a source mutation');
END;
CREATE TRIGGER review_round_source_insert AFTER INSERT ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_rows' AND scope=CAST(NEW.repository AS BLOB)
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_rows' AND scope=CAST(NEW.repository AS BLOB);
END;
CREATE TRIGGER review_round_source_update AFTER UPDATE ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'prior round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_rows' AND scope=CAST(OLD.repository AS BLOB)
    );
    SELECT RAISE(ABORT,'new round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_rows' AND scope=CAST(NEW.repository AS BLOB)
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_rows' AND scope IN (
        CAST(OLD.repository AS BLOB),CAST(NEW.repository AS BLOB)
    );
END;
CREATE TRIGGER review_round_source_delete AFTER DELETE ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_rows' AND scope=CAST(OLD.repository AS BLOB)
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_rows' AND scope=CAST(OLD.repository AS BLOB);
END;
CREATE TRIGGER review_receipt_source_insert AFTER INSERT ON dedupe_keys
WHEN substr(CAST(NEW.namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
    OR CAST(NEW.namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
BEGIN
    SELECT RAISE(ABORT,'round receipt source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_receipts' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_receipts' AND scope=X'';
END;
CREATE TRIGGER review_receipt_source_update AFTER UPDATE ON dedupe_keys
WHEN substr(CAST(OLD.namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
    OR CAST(OLD.namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
    OR substr(CAST(NEW.namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
    OR CAST(NEW.namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
BEGIN
    SELECT RAISE(ABORT,'round receipt source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_receipts' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_receipts' AND scope=X'';
END;
CREATE TRIGGER review_receipt_source_delete AFTER DELETE ON dedupe_keys
WHEN substr(CAST(OLD.namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
    OR CAST(OLD.namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
BEGIN
    SELECT RAISE(ABORT,'round receipt source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_receipts' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_receipts' AND scope=X'';
END;
"""),
    SchemaMigration(28, """
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('all_round_rows',X'',0);
CREATE TRIGGER review_all_round_source_insert AFTER INSERT ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'global round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='all_round_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='all_round_rows' AND scope=X'';
END;
CREATE TRIGGER review_all_round_source_update AFTER UPDATE ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'global round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='all_round_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='all_round_rows' AND scope=X'';
END;
CREATE TRIGGER review_all_round_source_delete AFTER DELETE ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'global round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='all_round_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='all_round_rows' AND scope=X'';
END;
"""),
    SchemaMigration(29, """
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('generation_sources',X'',0);
-- REPLACE can delete a selected source without running its DELETE trigger.
CREATE TRIGGER review_archive_dedupe_no_replace_insert BEFORE INSERT ON dedupe_keys
WHEN NEW.rowid IS NOT -1 AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=NEW.rowid AND (
        substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
        OR CAST(namespace AS BLOB) IN (
            CAST('review-recorded-submission' AS BLOB),
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    )
)
BEGIN
    SELECT RAISE(ABORT,'review archive replacement would hide a source mutation');
END;
-- SQLite reports an unspecified NEW.rowid (currently -1) for automatic inserts.
-- Conservatively advance a source at -1 so ordinary inserts remain possible.
CREATE TRIGGER review_archive_dedupe_minus_one_insert BEFORE INSERT ON dedupe_keys
WHEN NEW.rowid=-1 AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND (
        substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
        OR CAST(namespace AS BLOB) IN (
            CAST('review-recorded-submission' AS BLOB),
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    )
)
BEGIN
    SELECT RAISE(ABORT,'round receipt source witness is absent')
    WHERE EXISTS (
        SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND (
            substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
            OR CAST(namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
        )
    ) AND NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_receipts' AND scope=X''
    );
    SELECT RAISE(ABORT,'generation source witness is absent')
    WHERE EXISTS (
        SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND CAST(namespace AS BLOB) IN (
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    ) AND NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='generation_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_receipts' AND scope=X'' AND EXISTS (
        SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND (
            substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
            OR CAST(namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
        )
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='generation_sources' AND scope=X'' AND EXISTS (
        SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND CAST(namespace AS BLOB) IN (
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    );
END;
CREATE TRIGGER review_archive_dedupe_no_replace_update BEFORE UPDATE ON dedupe_keys
WHEN NEW.rowid IS NOT OLD.rowid AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=NEW.rowid AND (
        substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
        OR CAST(namespace AS BLOB) IN (
            CAST('review-recorded-submission' AS BLOB),
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    )
)
BEGIN
    SELECT RAISE(ABORT,'review archive replacement would hide a source mutation');
END;
CREATE TRIGGER review_generation_source_insert AFTER INSERT ON dedupe_keys
WHEN CAST(NEW.namespace AS BLOB) IN (
    CAST('review-execution-generation' AS BLOB),
    CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'generation source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='generation_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='generation_sources' AND scope=X'';
END;
CREATE TRIGGER review_generation_source_update AFTER UPDATE ON dedupe_keys
WHEN CAST(OLD.namespace AS BLOB) IN (
    CAST('review-execution-generation' AS BLOB),
    CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
) OR CAST(NEW.namespace AS BLOB) IN (
    CAST('review-execution-generation' AS BLOB),
    CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'generation source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='generation_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='generation_sources' AND scope=X'';
END;
CREATE TRIGGER review_generation_source_delete AFTER DELETE ON dedupe_keys
WHEN CAST(OLD.namespace AS BLOB) IN (
    CAST('review-execution-generation' AS BLOB),
    CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'generation source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='generation_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='generation_sources' AND scope=X'';
END;
"""),
    SchemaMigration(30, """
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('checkpoint_sources',X'',0),('workflow_rows',X'',0);
CREATE TRIGGER review_checkpoint_source_no_replace_insert BEFORE INSERT ON dedupe_keys
WHEN NEW.rowid IS NOT -1 AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=NEW.rowid AND CAST(namespace AS BLOB) IN (
        CAST('review-execution-origin' AS BLOB),
        CAST('review-execution-checkpoint' AS BLOB),
        CAST('review-checkpoint-application' AS BLOB)
    )
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source replacement would hide a mutation');
END;
-- An automatic INSERT exposes NEW.rowid=-1 before SQLite allocates its rowid.
CREATE TRIGGER review_checkpoint_source_minus_one_insert BEFORE INSERT ON dedupe_keys
WHEN NEW.rowid=-1 AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND CAST(namespace AS BLOB) IN (
        CAST('review-execution-origin' AS BLOB),
        CAST('review-execution-checkpoint' AS BLOB),
        CAST('review-checkpoint-application' AS BLOB)
    )
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='checkpoint_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='checkpoint_sources' AND scope=X'';
END;
CREATE TRIGGER review_checkpoint_source_no_replace_update BEFORE UPDATE ON dedupe_keys
WHEN NEW.rowid IS NOT OLD.rowid AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=NEW.rowid AND CAST(namespace AS BLOB) IN (
        CAST('review-execution-origin' AS BLOB),
        CAST('review-execution-checkpoint' AS BLOB),
        CAST('review-checkpoint-application' AS BLOB)
    )
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source replacement would hide a mutation');
END;
CREATE TRIGGER review_checkpoint_source_insert AFTER INSERT ON dedupe_keys
WHEN CAST(NEW.namespace AS BLOB) IN (
    CAST('review-execution-origin' AS BLOB),
    CAST('review-execution-checkpoint' AS BLOB),
    CAST('review-checkpoint-application' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='checkpoint_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='checkpoint_sources' AND scope=X'';
END;
CREATE TRIGGER review_checkpoint_source_update AFTER UPDATE ON dedupe_keys
WHEN CAST(OLD.namespace AS BLOB) IN (
    CAST('review-execution-origin' AS BLOB),
    CAST('review-execution-checkpoint' AS BLOB),
    CAST('review-checkpoint-application' AS BLOB)
) OR CAST(NEW.namespace AS BLOB) IN (
    CAST('review-execution-origin' AS BLOB),
    CAST('review-execution-checkpoint' AS BLOB),
    CAST('review-checkpoint-application' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='checkpoint_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='checkpoint_sources' AND scope=X'';
END;
CREATE TRIGGER review_checkpoint_source_delete AFTER DELETE ON dedupe_keys
WHEN CAST(OLD.namespace AS BLOB) IN (
    CAST('review-execution-origin' AS BLOB),
    CAST('review-execution-checkpoint' AS BLOB),
    CAST('review-checkpoint-application' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='checkpoint_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='checkpoint_sources' AND scope=X'';
END;
CREATE TRIGGER review_workflow_source_insert AFTER INSERT ON workflows
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_workflow_source_update AFTER UPDATE ON workflows
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_workflow_source_delete AFTER DELETE ON workflows
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_component_source_insert AFTER INSERT ON workflow_components
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_component_source_update AFTER UPDATE ON workflow_components
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_component_source_delete AFTER DELETE ON workflow_components
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
"""),
    SchemaMigration(31, """
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('lifecycle_rows',X'',0);
CREATE TRIGGER review_archive_source_version_overflow
BEFORE UPDATE OF version ON review_archive_source_versions
WHEN typeof(NEW.version)!='integer'
    OR NEW.version NOT BETWEEN 0 AND 9223372036854775806
BEGIN
    SELECT RAISE(ABORT,'archive source witness version overflow');
END;
CREATE TRIGGER review_lifecycle_command_source_insert AFTER INSERT ON lifecycle_commands
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_command_source_update AFTER UPDATE ON lifecycle_commands
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_command_source_delete AFTER DELETE ON lifecycle_commands
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_effect_source_insert AFTER INSERT ON effect_receipts
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_effect_source_update AFTER UPDATE ON effect_receipts
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_effect_source_delete AFTER DELETE ON effect_receipts
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
"""),
    SchemaMigration(32, _review_execution_lineage_migration()),
    SchemaMigration(33, """
CREATE INDEX review_execution_rounds_generation_pending
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,generation,sequence);
"""),
    SchemaMigration(34, """
DROP TRIGGER review_all_round_source_delete;
DROP TRIGGER review_all_round_source_insert;
DROP TRIGGER review_all_round_source_update;
DROP TRIGGER review_archive_dedupe_minus_one_insert;
DROP TRIGGER review_archive_dedupe_no_replace_insert;
DROP TRIGGER review_archive_dedupe_no_replace_update;
DROP TRIGGER review_archive_source_version_overflow;
DROP TRIGGER review_checkpoint_source_delete;
DROP TRIGGER review_checkpoint_source_insert;
DROP TRIGGER review_checkpoint_source_minus_one_insert;
DROP TRIGGER review_checkpoint_source_no_replace_insert;
DROP TRIGGER review_checkpoint_source_no_replace_update;
DROP TRIGGER review_checkpoint_source_update;
DROP TRIGGER review_component_source_delete;
DROP TRIGGER review_component_source_insert;
DROP TRIGGER review_component_source_update;
DROP TRIGGER review_generation_source_delete;
DROP TRIGGER review_generation_source_insert;
DROP TRIGGER review_generation_source_update;
DROP TRIGGER review_identity_certifications_delete;
DROP TRIGGER review_identity_certifications_insert;
DROP TRIGGER review_identity_certifications_update;
DROP TRIGGER review_identity_members_delete;
DROP TRIGGER review_identity_members_insert;
DROP TRIGGER review_identity_members_update;
DROP TRIGGER review_lifecycle_command_source_delete;
DROP TRIGGER review_lifecycle_command_source_insert;
DROP TRIGGER review_lifecycle_command_source_update;
DROP TRIGGER review_lifecycle_effect_source_delete;
DROP TRIGGER review_lifecycle_effect_source_insert;
DROP TRIGGER review_lifecycle_effect_source_update;
DROP TRIGGER review_lineage_budget_reservations_delete;
DROP TRIGGER review_lineage_budget_reservations_insert;
DROP TRIGGER review_lineage_budget_reservations_update;
DROP TRIGGER review_lineage_charges_delete;
DROP TRIGGER review_lineage_charges_insert;
DROP TRIGGER review_lineage_charges_update;
DROP TRIGGER review_lineage_dedupe_keys_delete;
DROP TRIGGER review_lineage_dedupe_keys_insert;
DROP TRIGGER review_lineage_dedupe_keys_update;
DROP TRIGGER review_lineage_dispatch_claims_delete;
DROP TRIGGER review_lineage_dispatch_claims_insert;
DROP TRIGGER review_lineage_dispatch_claims_update;
DROP TRIGGER review_lineage_leases_delete;
DROP TRIGGER review_lineage_leases_insert;
DROP TRIGGER review_lineage_leases_update;
DROP TRIGGER review_lineage_quota_debits_delete;
DROP TRIGGER review_lineage_quota_debits_insert;
DROP TRIGGER review_lineage_quota_debits_update;
DROP TRIGGER review_lineage_reservation_limits_delete;
DROP TRIGGER review_lineage_reservation_limits_insert;
DROP TRIGGER review_lineage_reservation_limits_update;
DROP TRIGGER review_lineage_reservation_recovery_delete;
DROP TRIGGER review_lineage_reservation_recovery_insert;
DROP TRIGGER review_lineage_reservation_recovery_update;
DROP TRIGGER review_lineage_run_cancellation_intents_delete;
DROP TRIGGER review_lineage_run_cancellation_intents_insert;
DROP TRIGGER review_lineage_run_cancellation_intents_update;
DROP TRIGGER review_lineage_runs_delete;
DROP TRIGGER review_lineage_runs_insert;
DROP TRIGGER review_lineage_runs_update;
DROP TRIGGER review_lineage_workflow_evidence_delete;
DROP TRIGGER review_lineage_workflow_evidence_insert;
DROP TRIGGER review_lineage_workflow_evidence_update;
DROP TRIGGER review_receipt_source_delete;
DROP TRIGGER review_receipt_source_insert;
DROP TRIGGER review_receipt_source_update;
DROP TRIGGER review_round_source_delete;
DROP TRIGGER review_round_source_insert;
DROP TRIGGER review_round_source_no_replace_insert;
DROP TRIGGER review_round_source_no_replace_update;
DROP TRIGGER review_round_source_update;
DROP TRIGGER review_workflow_round_source_insert;
DROP TRIGGER review_workflow_source_delete;
DROP TRIGGER review_workflow_source_insert;
DROP TRIGGER review_workflow_source_update;
DROP TABLE review_archive_identity_members;
DROP TABLE review_archive_identity_certifications;
DROP TABLE review_archive_source_versions;
"""),
    SchemaMigration(35, """
CREATE INDEX workflow_gates_current
    ON workflow_gates(repository,target_kind,target_number,gate_id)
    WHERE state<>'closed';
"""),
    SchemaMigration(36, """
CREATE TABLE workflow_components_v36 (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    episode_id TEXT NOT NULL CHECK (length(episode_id) BETWEEN 1 AND 512),
    PRIMARY KEY (repository,target_kind,target_number,family),
    FOREIGN KEY (repository,target_kind,target_number)
        REFERENCES workflows(repository,target_kind,target_number),
    CHECK ((family IN ('resolve','issue_recovery','steward') AND target_kind='issue') OR
           (family IN ('review_rejections','review_execution','pr_repair') AND target_kind='pr'))
);
INSERT INTO workflow_components_v36 SELECT * FROM workflow_components;
DROP TABLE workflow_components;
ALTER TABLE workflow_components_v36 RENAME TO workflow_components;
CREATE INDEX workflow_components_family ON workflow_components(repository,family);
ALTER TABLE runs ADD COLUMN continuations_json TEXT NOT NULL DEFAULT '{"format":1,"value":[]}'
    CHECK (length(CAST(continuations_json AS BLOB)) BETWEEN 1 AND 262144);
CREATE TRIGGER runs_continuation_selections_update BEFORE UPDATE OF continuations_json ON runs
WHEN NEW.continuations_json IS NOT OLD.continuations_json
BEGIN SELECT RAISE(ABORT,'run continuation selections are immutable'); END;
CREATE TRIGGER runs_continuation_identity_update BEFORE UPDATE ON runs
WHEN OLD.continuations_json <> '{"format":1,"value":[]}' AND (
    NEW.workflow_binding_json IS NOT OLD.workflow_binding_json OR
    NEW.workflow_episode_key IS NOT OLD.workflow_episode_key OR
    NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.owner_id IS NOT OLD.owner_id OR
    NEW.kind IS NOT OLD.kind OR NEW.config_version IS NOT OLD.config_version)
BEGIN SELECT RAISE(ABORT,'continuation run identity is immutable'); END;
"""),
)

if tuple(migration.version for migration in _SCHEMA_MIGRATIONS) != tuple(
    range(SCHEMA_BASELINE_VERSION, SCHEMA_VERSION + 1)
):
    raise RuntimeError("control-store migrations must be contiguous")

_KNOWN_SCHEMA_DIGESTS = {
    8: LEGACY_V8_SCHEMA_DIGEST,
    **{
        migration.version: migration.digest
        for migration in _SCHEMA_MIGRATIONS
    },
}


class SQLiteControlStore:
    """One SQLite database, safe for threads and cooperating daemon processes."""

    def __init__(
        self,
        path: str,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        now: Callable[[], datetime] | None = None,
        migration_observer: Callable[[int, str], None] | None = None,
    ):
        if not path:
            raise ValueError("control-store path is required")
        if busy_timeout_ms <= 0:
            raise ValueError("busy timeout must be positive")
        self.path = os.path.abspath(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._migration_observer = migration_observer
        self._event_factory: (
            Callable[[EventPayload, Correlation], EventEnvelope] | None
        ) = None
        self._event_sink: Callable[[EventEnvelope], None] | None = None
        self._event_destination: str | None = None
        self._event_line_limit = MAX_EVENT_LINE_BYTES
        self._flushing_outbox = False
        self._bound_owner_id: str | None = None
        self._telemetry_enqueue_failures = 0

    @property
    def telemetry_enqueue_failures(self) -> int:
        return self._telemetry_enqueue_failures

    def bind_event_outbox(
        self,
        factory: Callable[[EventPayload, Correlation], EventEnvelope],
        sink: Callable[[EventEnvelope], None],
        *,
        line_limit: int = MAX_EVENT_LINE_BYTES,
        destination: str | None = None,
    ) -> None:
        """Bind telemetry publication after store initialization.

        State commits do not depend on the sink. A failed publication leaves its
        row pending and a later mutation/startup retries it.
        """

        if line_limit <= 0:
            raise ValueError("outbox event line limit must be positive")
        self._event_factory = factory
        self._event_sink = sink
        self._event_line_limit = line_limit
        self._event_destination = destination
        self._flush_bound_outbox()

    def _transition(
        self,
        db: sqlite3.Connection,
        entity_kind: str,
        entity_id: str,
        state: str,
        detail: Mapping[str, JsonValue] | None = None,
        *,
        correlation: Correlation = Correlation(),
    ) -> None:
        if self._event_factory is None:
            return
        payload = ControlTransitionCommitted(
            entity_kind,
            entity_id,
            state,
            dict(detail or {}),
        )
        self._enqueue_operational_event(db, payload, correlation)

    def _event(
        self,
        db: sqlite3.Connection,
        payload: EventPayload,
        *,
        correlation: Correlation = Correlation(),
    ) -> None:
        self._enqueue_operational_event(db, payload, correlation)

    def _enqueue_operational_event(
        self,
        db: sqlite3.Connection,
        payload: EventPayload,
        correlation: Correlation,
    ) -> None:
        """Keep telemetry encoding failures outside authoritative transactions."""

        if self._event_factory is None:
            return
        try:
            self.enqueue_event(
                self._event_factory(payload, correlation),
                db,
            )
        except Exception:
            self._telemetry_enqueue_failures += 1

    def _flush_bound_outbox(self) -> int:
        if self._event_sink is None or self._flushing_outbox:
            return 0
        self._flushing_outbox = True
        try:
            return self.flush_outbox(
                self._event_sink, destination=self._event_destination
            )
        except Exception:
            return 0
        finally:
            self._flushing_outbox = False

    def flush_bound_outbox(self) -> int:
        """Retry every committed event for this role's bound destination."""

        return self._flush_bound_outbox()

    def initialize(self) -> None:
        parent = os.path.dirname(self.path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, stat.S_IRWXU)
        with self._initialization_lock():
            witness_exists = self._authority_witness_exists()
            database_exists = self._database_exists(
                allow_empty=not witness_exists
            )
            if witness_exists:
                self._validate_authority_witness()
                if not database_exists:
                    raise CorruptControlStore(
                        "control-store authority database disappeared after "
                        "initialization"
                    )
            if database_exists:
                self.integrity_check()
            with closing(self._connect(create=not database_exists)) as db, db:
                applied = self._migration_history(db)
                if not applied and self._has_user_schema(db):
                    raise IncompatibleSchema(
                        "historyless nonempty control store cannot be adopted"
                    )
            self._validate_migration_history(applied)
            for migration in _SCHEMA_MIGRATIONS:
                self._apply_schema_migration(migration)
            with closing(self._connect()) as db, db:
                final_history = self._migration_history(db)
            self._validate_migration_history(
                final_history, require_current=True
            )
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            self.integrity_check()
            if not witness_exists:
                self._write_authority_witness()

    @property
    def authority_witness_path(self) -> str:
        return f"{self.path}.authority"

    def require_initialized(self) -> None:
        """Open operator authority without creating, migrating or adopting it."""
        if not self._database_exists() or not self._authority_witness_exists():
            raise CorruptControlStore("operator control requires initialized authority")
        self._validate_authority_witness()
        with closing(self._connect()) as db, db:
            self._validate_migration_history(self._migration_history(db), require_current=True)

    @contextmanager
    def legacy_resolve_authority(self, repository: str) -> Iterator[None]:
        """A legacy operator write cannot cross a resolve-family activation."""
        from ..domain.workflow_identity import canonical_workflow_repository

        repository = canonical_workflow_repository(repository)
        # Initialization has a different lock order; operator writes never
        # create, adopt or migrate accounting while holding these barriers.
        self.require_initialized()
        with ExitStack() as barriers:
            for role in ("fix", "review", "steward"):
                barriers.enter_context(deployment_effect_barrier(self.path, repository, role))
            with closing(self._connect()) as db:
                db.execute("BEGIN")
                active = active_families(db, repository)
                if WorkflowFamily.RESOLVE in active:
                    raise WorkflowAuthorityError("legacy unblock is retired for active resolve control; use a typed re-arm command")
                # Family-scoped reads no longer count PR rows as resolve
                # authority; the legacy unblock still never crosses them
                # (ADR 0015: never two authorities, no fallback).
                if active:
                    raise WorkflowAuthorityError("legacy unblock is retired for active review rejection control; use a typed re-arm command")
            yield

    def active_workflow_families(self, repository: str) -> tuple[WorkflowFamily, ...]:
        """The families activated for a repository, read-only: exactly what `legacy_resolve_authority` refuses on."""
        from ..domain.workflow_identity import canonical_workflow_repository

        self.require_initialized()
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            return active_families(db, canonical_workflow_repository(repository))

    def _database_exists(self, *, allow_empty: bool = False) -> bool:
        try:
            metadata = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            raise CorruptControlStore(
                f"control-store path is not a regular file: {self.path}"
            )
        if metadata.st_size <= 0:
            if allow_empty:
                return False
            raise CorruptControlStore(
                f"control-store authority database is empty: {self.path}"
            )
        return True

    def _authority_witness_exists(self) -> bool:
        try:
            os.stat(self.authority_witness_path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _validate_authority_witness(self) -> None:
        path = self.authority_witness_path
        try:
            metadata = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise CorruptControlStore(
                    f"unsafe control-store authority witness: {path}"
                )
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                encoded = os.read(fd, 256)
                if os.read(fd, 1):
                    raise CorruptControlStore(
                        "control-store authority witness is oversized"
                    )
            finally:
                os.close(fd)
        except OSError as exc:
            raise CorruptControlStore(
                f"cannot read control-store authority witness: {exc}"
            ) from exc
        if encoded.decode(errors="replace").strip() != AUTHORITY_WITNESS_FORMAT:
            raise CorruptControlStore(
                "control-store authority witness has an unknown format"
            )

    def _write_authority_witness(self) -> None:
        path = self.authority_witness_path
        parent = os.path.dirname(path)
        prefix = f".{os.path.basename(path)}."
        fd, temporary = tempfile.mkstemp(prefix=prefix, dir=parent)
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            encoded = f"{AUTHORITY_WITNESS_FORMAT}\n".encode()
            written = 0
            while written < len(encoded):
                written += os.write(fd, encoded[written:])
            os.fsync(fd)
            os.close(fd)
            fd = -1
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                self._validate_authority_witness()
            directory_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        """Serialize WAL selection and schema migration across role processes."""

        path = f"{self.path}.initialize.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise ControlStoreError(
                    f"unsafe control-store initialization lock: {path}"
                )
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _validate_migration_history(
        self,
        history: tuple[tuple[int, str], ...],
        *,
        require_current: bool = False,
    ) -> None:
        if not history:
            if require_current:
                raise IncompatibleSchema("control-store migration history is empty")
            return
        versions = tuple(version for version, _digest in history)
        if versions[0] not in {8, SCHEMA_BASELINE_VERSION} or versions != tuple(
            range(versions[0], versions[-1] + 1)
        ):
            raise IncompatibleSchema(
                "control-store migration history is not contiguous from "
                f"a supported baseline (8 or {SCHEMA_BASELINE_VERSION}): "
                f"{versions}"
            )
        if versions[-1] > SCHEMA_VERSION:
            raise IncompatibleSchema(
                f"control store schema {versions[-1]} is newer than "
                f"{SCHEMA_VERSION}"
            )
        for version, digest in history:
            if _KNOWN_SCHEMA_DIGESTS.get(version) != digest:
                raise IncompatibleSchema(
                    f"control-store migration {version} digest mismatch"
                )
        if require_current and versions[-1] != SCHEMA_VERSION:
            raise IncompatibleSchema(
                f"control store stopped at schema {versions[-1]}, expected "
                f"{SCHEMA_VERSION}"
            )

    @staticmethod
    def _migration_history(
        db: sqlite3.Connection,
    ) -> tuple[tuple[int, str], ...]:
        existing = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone()
        if existing is None:
            return ()
        return tuple(
            (int(row["version"]), str(row["source_digest"]))
            for row in db.execute(
                "SELECT version, source_digest FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        )

    @staticmethod
    def _has_user_schema(db: sqlite3.Connection) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            is not None
        )

    def _apply_schema_migration(self, migration: SchemaMigration) -> None:
        applied = False
        with closing(self._connect()) as db, db:
            try:
                db.execute("BEGIN IMMEDIATE")
                history = self._migration_history(db)
                self._validate_migration_history(history)
                if migration.version in {
                    version for version, _digest in history
                }:
                    db.commit()
                    return
                for statement in _sql_statements(migration.source):
                    db.execute(statement)
                if migration.version == 20:
                    _backfill_workflow_episode_index_v20(db)
                if migration.version == 25:
                    _backfill_workflow_components_v25(db)
                db.execute(
                    "INSERT INTO schema_migrations("
                    "version, applied_at, source_digest) VALUES (?, ?, ?)",
                    (
                        migration.version,
                        _stamp(self._now()),
                        migration.digest,
                    ),
                )
                if self._migration_observer is not None:
                    self._migration_observer(
                        migration.version,
                        "before_commit",
                    )
                applied = True
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise IncompatibleSchema(
                    f"control-store migration {migration.version} found "
                    "conflicting authority rows; operator repair is required"
                ) from exc
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        if applied and self._migration_observer is not None:
            self._migration_observer(migration.version, "committed")

    def integrity_check(self) -> None:
        if not self._database_exists():
            raise CorruptControlStore(
                "control-store authority database does not exist"
            )
        try:
            with closing(self._connect()) as db, db:
                result = db.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise CorruptControlStore(
                        f"control-store integrity check failed: {result}"
                    )
                history = self._migration_history(db)
                self._validate_migration_history(history)
                if history and history[-1][0] == SCHEMA_VERSION:
                    self._assert_semantic_integrity(db)
        except sqlite3.DatabaseError as exc:
            raise CorruptControlStore(f"control-store integrity check failed: {exc}") from exc

    @staticmethod
    def _assert_semantic_integrity(db: sqlite3.Connection) -> None:
        foreign_key_error = db.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise CorruptControlStore(
                "control-store foreign-key integrity check failed: "
                f"{tuple(foreign_key_error)}"
            )

        violations = (
            (
                "workflow binding lacks its episode projection",
                "SELECT run_id FROM runs WHERE (workflow_binding_json IS NULL) != (workflow_episode_key IS NULL) LIMIT 1",
            ),
            (
                "never-launched release contradicts execution or accounting evidence",
                """
                SELECT r.run_id FROM runs r
                LEFT JOIN budget_reservations b USING(run_id)
                LEFT JOIN reservation_recovery rr USING(run_id)
                WHERE r.outcome='unlaunched_released' AND (
                    r.state != 'cancelled' OR r.provider_receipt_id IS NOT NULL
                    OR b.state IS NOT 'released' OR rr.policy IS NOT 'spawn_pending'
                    OR EXISTS(SELECT 1 FROM charges c WHERE c.run_id=r.run_id)
                    OR EXISTS(SELECT 1 FROM leases l WHERE l.run_id=r.run_id)
                ) LIMIT 1
                """,
            ),
            (
                "active reservation recovery classification is absent",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                LEFT JOIN reservation_recovery AS rr ON rr.run_id=r.run_id
                WHERE r.state='active' AND rr.run_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "reservation primary limit is absent or inconsistent",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                WHERE NOT EXISTS (
                    SELECT 1 FROM reservation_limits AS l
                    WHERE l.run_id=r.run_id
                      AND l.repository=r.repository
                      AND l.dimension=r.dimension
                      AND l.period_id=r.period_id
                      AND l.period_start=r.period_start
                      AND l.period_end=r.period_end
                      AND l.period_timezone=r.period_timezone
                      AND l.currency=r.currency
                      AND l.cap_micros=r.cap_micros
                )
                LIMIT 1
                """,
            ),
            (
                "reservation limits use a conflicting currency",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                JOIN reservation_limits AS l ON l.run_id=r.run_id
                WHERE l.currency<>r.currency
                LIMIT 1
                """,
            ),
            (
                "workload repository conflicts with reservation limits",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                JOIN reservation_limits AS l ON l.run_id=r.run_id
                WHERE l.repository<>'*'
                  AND l.repository<>r.workload_repository
                LIMIT 1
                """,
            ),
            (
                "reservation settlement and charge disagree",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                LEFT JOIN charges AS c ON c.run_id=r.run_id
                GROUP BY r.run_id, r.state
                HAVING (r.state='settled' AND COUNT(c.charge_id)<>1)
                    OR (r.state<>'settled' AND COUNT(c.charge_id)<>0)
                LIMIT 1
                """,
            ),
            (
                "charge and reservation currencies disagree",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                JOIN charges AS c ON c.run_id=r.run_id
                WHERE r.currency<>c.currency
                LIMIT 1
                """,
            ),
            (
                "run and charge provider receipts disagree",
                """
                SELECT r.run_id
                FROM runs AS r
                JOIN charges AS c ON c.run_id=r.run_id
                WHERE r.provider_receipt_id IS NULL
                   OR r.provider_receipt_id<>c.provider_receipt_id
                LIMIT 1
                """,
            ),
            (
                "lifecycle command and effect receipt states disagree",
                """
                SELECT c.command_id
                FROM lifecycle_commands AS c
                LEFT JOIN effect_receipts AS r ON r.command_id=c.command_id
                WHERE (
                    c.state IN (
                        'succeeded', 'retryable_failure',
                        'permanent_failure', 'unknown_outcome'
                    )
                ) <> (r.command_id IS NOT NULL)
                   OR (r.command_id IS NOT NULL AND r.status<>c.state)
                LIMIT 1
                """,
            ),
            (
                "retired owner scope disagrees with its successor",
                """
                SELECT retired.owner_id
                FROM retired_owners AS retired
                JOIN retired_owners AS successor
                  ON successor.owner_id=retired.successor_owner_id
                WHERE retired.repository<>successor.repository
                   OR retired.role<>successor.role
                LIMIT 1
                """,
            ),
        )
        for detail, query in violations:
            row = db.execute(query).fetchone()
            if row is not None:
                raise CorruptControlStore(
                    f"control-store semantic integrity failed: {detail} "
                    f"({row[0]})"
                )

        from .workflow_commit import owns_command
        from ..domain.workflow_identity import WorkflowWriter
        for row in db.execute(
            "SELECT r.run_id,r.owner_id,r.continuations_json,dc.repository,dc.role,dc.owner_id AS successor "
            "FROM dispatch_claim_runs dcr JOIN dispatch_claims dc "
            "ON dc.repository=dcr.repository AND dc.role=dcr.role AND dc.target=dcr.target "
            "JOIN runs r ON r.run_id=dcr.run_id WHERE dc.owner_id<>r.owner_id"
        ):
            if (row["continuations_json"] == encode_workflow(()) or not owns_command(
                    db, WorkflowWriter(row["repository"], row["role"], row["successor"]), row["owner_id"])):
                raise CorruptControlStore(
                    f"dispatch claim and bound run owners disagree ({row['run_id']})"
                )

        for row in db.execute(
            "SELECT * "
            "FROM lifecycle_commands"
        ).fetchall():
            binding = row["workflow_binding_json"] if "workflow_binding_json" in row.keys() else None
            digest = _encoded_payload_digest(str(row["payload_json"]), binding)
            if digest != row["payload_digest"]:
                raise CorruptControlStore(
                    "control-store semantic integrity failed: lifecycle "
                    f"command payload digest disagrees ({row['command_id']})"
                )

    @staticmethod
    def _assert_owner_active(
        db: sqlite3.Connection,
        owner_id: str,
    ) -> None:
        row = db.execute(
            "SELECT successor_owner_id FROM retired_owners WHERE owner_id=?",
            (owner_id,),
        ).fetchone()
        if row is not None:
            raise RetiredOwnerError(
                f"deployment owner {owner_id!r} was retired in favor of "
                f"{row['successor_owner_id']!r}"
            )

    def assert_owner_active(self, owner_id: str) -> None:
        """Fail when a deployment identity has permanently lost authority."""

        if not owner_id:
            raise ValueError("deployment owner identity is required")
        with closing(self._connect()) as db, db:
            self._assert_owner_active(db, owner_id)

    def bind_active_owner(self, owner_id: str) -> None:
        """Fence every subsequent write made through this role's store."""

        self.assert_owner_active(owner_id)
        self._bound_owner_id = owner_id

    def adopt_legacy_owner(
        self,
        repository: str,
        role: str,
        owner_id: str,
        *,
        prior_owner_ids: tuple[str, ...] = (),
    ) -> tuple[int, int]:
        """Atomically move pre-deployment ownership into the first upgrader.

        Older schemas scoped all same-repository role daemons as
        ``<repository>:<role>``. The first deployment to boot after the
        deployment-identity cutover adopts that legacy scope, preserving active
        run recovery and reconcile-before-retry effect semantics.
        """

        legacy = f"{repository}:{role}"
        if not repository or not role or not owner_id:
            raise ValueError("legacy owner adoption identity is incomplete")
        if deployment_role(repository, owner_id) != role:
            raise ValueError("legacy owner adoption role does not match owner")
        prior = tuple(
            candidate
            for candidate in dict.fromkeys((legacy, *prior_owner_ids))
            if candidate and candidate != owner_id
        )
        if not prior:
            return (0, 0)
        if any(deployment_role(repository, f"{canonical_repository(candidate.partition(':')[0])}:{candidate.partition(':')[2]}") != role
               for candidate in prior):
            raise ValueError("prior owner adoption must retain repository and role")
        placeholders = ",".join("?" for _candidate in prior)
        with deployment_effect_barrier(
            self.path,
            repository,
            role,
        ), self.transaction() as db:
            self._assert_owner_active(db, owner_id)
            prior_rows = db.execute(
                f"SELECT owner_id, repository, role FROM retired_owners "
                f"WHERE owner_id IN ({placeholders})",
                prior,
            ).fetchall()
            for row in prior_rows:
                if (
                    row["repository"] != repository
                    or row["role"] != role
                ):
                    raise ControlStoreError(
                        f"retired owner {row['owner_id']!r} belongs to "
                        f"{row['repository']}:{row['role']}, not "
                        f"{repository}:{role}"
                    )
            runs = db.execute(
                f"UPDATE runs SET owner_id=? WHERE owner_id IN ({placeholders}) AND continuations_json=?",
                (owner_id, *prior, encode_workflow(())),
            ).rowcount
            db.execute(
                f"UPDATE leases SET owner_id=? WHERE owner_id IN ({placeholders})",
                (owner_id, *prior),
            )
            db.execute(
                f"UPDATE dispatch_claims SET owner_id=? "
                f"WHERE repository=? AND role=? "
                f"AND owner_id IN ({placeholders})",
                (owner_id, repository, role, *prior),
            )
            commands = db.execute(
                "UPDATE lifecycle_commands SET executor_identity=? "
                f"WHERE repository=? AND executor_identity IN ({placeholders}) "
                "AND state IN ('pending','executing','retryable_failure',"
                "'unknown_outcome')",
                (owner_id, repository, *prior),
            ).rowcount
            retired_at = _stamp(self._now())
            db.executemany(
                "INSERT INTO retired_owners("
                "owner_id, successor_owner_id, repository, role, retired_at"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_id) DO UPDATE SET "
                "successor_owner_id=excluded.successor_owner_id, "
                "retired_at=excluded.retired_at",
                (
                    (
                        candidate,
                        owner_id,
                        repository,
                        role,
                        retired_at,
                    )
                    for candidate in prior
                ),
            )
            return (runs, commands)

    def adopt_projection_feed(
        self,
        *,
        prior_deployment: str,
        current_deployment: str,
        repository: str,
        role: str,
        prior_destination: str,
        current_destination: str,
    ) -> tuple[int, int]:
        """Retire a case-alias feed without stranding membership or outbox rows."""

        if (
            not prior_deployment
            or not current_deployment
            or not repository
            or not role
            or not prior_destination
            or not current_destination
        ):
            raise ValueError("projection feed adoption identity is incomplete")
        if prior_deployment == current_deployment:
            return (0, 0)
        if (
            self._event_factory is None
            or self._event_destination != current_destination
        ):
            raise ValueError(
                "current projection event destination must be bound before adoption"
            )
        retirement_key = hashlib.sha256(
            f"{prior_destination}->{current_destination}".encode()
        ).hexdigest()
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO dedupe_keys("
                "namespace, scope, key, claimed_at"
                ") VALUES ('world-feed-retirement', ?, ?, ?)",
                (
                    current_destination,
                    retirement_key,
                    _stamp(self._now()),
                ),
            )
            if not int(db.execute("SELECT changes()").fetchone()[0]):
                return (0, 0)
            moved_outbox = db.execute(
                "UPDATE event_outbox SET destination=? "
                "WHERE destination=? AND emitted_at IS NULL",
                (current_destination, prior_destination),
            ).rowcount
            prior_rows = int(
                db.execute(
                    "SELECT COUNT(*) FROM world_observations "
                    "WHERE deployment=?",
                    (prior_deployment,),
                ).fetchone()[0]
            )
            db.execute(
                "INSERT OR IGNORE INTO world_observations("
                "deployment, subject_kind, subject_number, content_hash, "
                "observed_at"
                ") SELECT ?, subject_kind, subject_number, content_hash, "
                "observed_at FROM world_observations WHERE deployment=?",
                (current_deployment, prior_deployment),
            )
            db.execute(
                "DELETE FROM world_observations WHERE deployment=?",
                (prior_deployment,),
            )
            self._event(
                db,
                WorldFeedRetired(
                    prior_deployment,
                    repository,
                    role,
                ),
            )
            return (prior_rows, moved_outbox)

    def now(self) -> datetime:
        """This store's authority clock; executors stamp their reads with it."""
        return self._now()

    @staticmethod
    @contextmanager
    def mutation_savepoint(db: sqlite3.Connection) -> Iterator[None]:
        """A caught helper error cannot leave half a mutation in its caller's transaction."""
        if not db.in_transaction:
            raise ControlStoreError("workflow mutation requires an enclosing transaction")
        name = "workflow_mutation_" + uuid.uuid4().hex
        db.execute(f"SAVEPOINT {name}")
        try:
            yield
            db.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            db.execute(f"RELEASE SAVEPOINT {name}")
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db, db:
            try:
                db.execute("BEGIN IMMEDIATE")
                if self._bound_owner_id is not None:
                    self._assert_owner_active(db, self._bound_owner_id)
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
                self._flush_bound_outbox()

    def start_run(
        self,
        run_id: RunId,
        dispatch_id: str,
        kind: str,
        owner_id: str,
        config_version: str,
        *,
        state: str = "starting",
    ) -> None:
        now = _stamp(self._now())
        with self.transaction() as db:
            self._assert_owner_active(db, owner_id)
            db.execute(
                "INSERT INTO runs(run_id, dispatch_id, kind, owner_id, state, "
                "config_version, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(run_id),
                    dispatch_id,
                    kind,
                    owner_id,
                    state,
                    config_version,
                    now,
                ),
            )
            self._transition(
                db,
                "run",
                str(run_id),
                state,
                {"dispatch_id": dispatch_id, "kind": kind, "owner_id": owner_id},
                correlation=Correlation(
                    dispatch_id=dispatch_id,
                    run_id=str(run_id),
                ),
            )

    def complete_run(
        self,
        run_id: RunId,
        *,
        state: str,
        outcome: str,
        provider_receipt_id: str | None = None,
    ) -> bool:
        if state not in {"completed", "cancelled", "abandoned"}:
            raise ValueError("run completion requires a terminal state")
        if outcome == "unlaunched_released":
            raise ValueError("never-launched evidence can only be minted by reservation release")
        with self.transaction() as db:
            if self._has_review_preflight_stop(db, run_id):
                raise ControlStoreError("review preflight stop requires atomic accounting completion")
            row = db.execute(
                "SELECT state, outcome, provider_receipt_id FROM runs WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(run_id))
            if row["state"] in {"completed", "cancelled", "abandoned"}:
                return (
                    row["state"] == state
                    and row["outcome"] == outcome
                    and row["provider_receipt_id"] == provider_receipt_id
                )
            db.execute(
                "UPDATE runs SET state=?, outcome=?, provider_receipt_id=?, "
                "completed_at=? WHERE run_id=?",
                (state, outcome, provider_receipt_id, _stamp(self._now()), str(run_id)),
            )
            db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
            self._transition(
                db,
                "run",
                str(run_id),
                state,
                {
                    "outcome": outcome,
                    "provider_receipt_id": provider_receipt_id,
                },
                correlation=Correlation(run_id=str(run_id)),
            )
            return True

    def register_lease(self, lease: Lease) -> None:
        with self.transaction() as db:
            run = db.execute(
                "SELECT state,review_round_id,workflow_binding_json,continuations_json,owner_id,dispatch_id FROM runs WHERE run_id=?", (str(lease.run_id),),
            ).fetchone()
            if run is None or run["state"] in {"completed", "cancelled", "abandoned"}:
                raise ControlStoreError("process lease requires a nonterminal run")
            if run["review_round_id"] is not None:
                raise ControlStoreError("review child requires guarded process registration")
            if run["continuations_json"] != encode_workflow(()) and (lease.owner_id != run["owner_id"] or lease.dispatch_id != run["dispatch_id"]):
                raise ControlStoreError("continuation lease conflicts with its actual run identity")
            self._assert_owner_active(db, lease.owner_id)
            self._require_continuation_launch(db, lease.run_id)
            if run["workflow_binding_json"] is not None:
                captured = decode_workflow(run["workflow_binding_json"], WorkflowBinding)
                if captured.authority.family is WorkflowFamily.REVIEW_REJECTIONS:
                    snapshot = load_review_workflow_row(db, captured.fence.key)
                    if snapshot is not None and isinstance(snapshot.execution, BlockedReviewExecutionMigration):
                        raise ControlStoreError("review execution migration is blocked")
            db.execute(
                "INSERT INTO leases(run_id, dispatch_id, owner_id, pid, pgid, "
                "process_start_ticks, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(lease.run_id),
                    lease.dispatch_id,
                    lease.owner_id,
                    lease.pid,
                    lease.pgid,
                    lease.process_start_ticks,
                    _stamp(lease.started_at),
                ),
            )
            db.execute(
                "UPDATE runs SET state='running' WHERE run_id=?",
                (str(lease.run_id),),
            )
            db.execute(
                "UPDATE reservation_recovery SET policy='running', "
                "reason=NULL, updated_at=? WHERE run_id=?",
                (_stamp(self._now()), str(lease.run_id)),
            )
            self._transition(
                db,
                "lease",
                str(lease.run_id),
                "active",
                {
                    "dispatch_id": lease.dispatch_id,
                    "owner_id": lease.owner_id,
                    "pid": lease.pid,
                    "pgid": lease.pgid,
                    "process_start_ticks": lease.process_start_ticks,
                },
                correlation=Correlation(
                    dispatch_id=lease.dispatch_id,
                    run_id=str(lease.run_id),
                ),
            )

    def register_process_lease(
        self,
        run_id: RunId,
        *,
        expected_owner_id: str,
        pid: int,
        pgid: int,
        process_start_ticks: int,
    ) -> Lease:
        """Bind a spawned process to its reserved run before paid work executes."""
        if (
            not expected_owner_id
            or type(pid) is not int or pid <= 0
            or type(pgid) is not int or pgid <= 0
            or type(process_start_ticks) is not int or process_start_ticks <= 0
        ):
            raise ValueError("live process identity is required")
        scope = self._review_launch_scope(run_id)
        if scope is None:
            return self._register_process_lease(
                run_id, expected_owner_id=expected_owner_id, pid=pid, pgid=pgid,
                process_start_ticks=process_start_ticks,
            )
        repository, round_id, had_lease = scope
        with deployment_effect_authority(
            self.path, repository, deployment_role(repository, expected_owner_id), blocking=False,
        ):
            child_identity_proven = False
            if not had_lease:
                from .process_supervisor import ProcessSupervisor
                try:
                    same_group = os.getpgid(pid) == pgid
                except OSError:
                    same_group = False
                if not same_group or not ProcessSupervisor.matches_identity(pid, process_start_ticks):
                    raise ControlStoreError("review launch process identity is not live")
                child_identity_proven = True
            parent_identity = self._review_round_parent_identity(repository, round_id)
            return self._register_process_lease(
                run_id, expected_owner_id=expected_owner_id, pid=pid, pgid=pgid,
                process_start_ticks=process_start_ticks, review_scope=(repository, round_id),
                review_parent_identity=parent_identity,
                review_child_identity_proven=child_identity_proven,
            )

    def _review_launch_scope(self, run_id: RunId) -> tuple[str, str, bool] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT b.workload_repository,r.review_round_id,l.run_id AS leased FROM runs r "
                "JOIN budget_reservations b ON b.run_id=r.run_id "
                "LEFT JOIN leases l ON l.run_id=r.run_id WHERE r.run_id=?",
                (str(run_id),),
            ).fetchone()
        return ((row["workload_repository"], row["review_round_id"], row["leased"] is not None)
                if row is not None and row["review_round_id"] is not None else None)

    def _review_round_parent_identity(self, repository: str, round_id: str) -> tuple[int, int] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT dc.owner_pid,dc.owner_start_ticks FROM review_execution_rounds rr "
                "JOIN dispatch_claims dc ON dc.repository=rr.repository AND dc.occurrence_id=rr.claim_occurrence_id "
                "WHERE rr.round_id=? AND rr.repository=? AND dc.role='review'",
                (round_id, repository),
            ).fetchone()
        if row is None:
            return None
        from .process_supervisor import ProcessSupervisor
        identity = (row["owner_pid"], row["owner_start_ticks"])
        return identity if ProcessSupervisor.matches_identity(*identity) else None

    def _register_process_lease(
        self, run_id: RunId, *, expected_owner_id: str, pid: int, pgid: int,
        process_start_ticks: int, review_scope: tuple[str, str] | None = None,
        review_parent_identity: tuple[int, int] | None = None,
        review_child_identity_proven: bool = False,
    ) -> Lease:
        with self.transaction() as db:
            self._assert_owner_active(db, expected_owner_id)
            run = db.execute(
                "SELECT dispatch_id, kind, owner_id, state, workflow_binding_json, "
                "workflow_episode_key, review_round_id, review_child_id "
                "FROM runs WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if run is None:
                raise ControlStoreError(
                    f"process lease has no reserved run: {run_id}"
                )
            if run["state"] in {"completed", "cancelled", "abandoned"}:
                raise ControlStoreError(
                    f"process lease cannot attach to terminal run: {run_id}"
                )
            review_attached = run["review_round_id"] is not None or run["review_child_id"] is not None
            if review_attached and (
                review_scope is None or run["review_round_id"] != review_scope[1]
                or run["review_child_id"] is None
            ):
                raise ControlStoreError("review launch lacks its captured round scope")
            if review_scope is not None and not review_attached:
                raise ControlStoreError("review launch scope changed before process registration")
            if review_attached and db.execute(
                "SELECT 1 FROM dedupe_keys WHERE namespace='review-execution-preflight-stop' "
                "AND scope=? AND key=?", (review_scope[0], str(run_id)),
            ).fetchone() is not None:
                raise ControlStoreError("review preflight stop fenced child launch")
            existing = db.execute(
                "SELECT dispatch_id, owner_id, pid, pgid, process_start_ticks, "
                "started_at FROM leases WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if existing is not None:
                from .workflow_commit import owns_command
                from ..domain.workflow_identity import WorkflowWriter
                repository = expected_owner_id.partition(":")[0]
                identical = (
                    int(existing["pid"]) == pid
                    and int(existing["pgid"]) == pgid
                    and int(existing["process_start_ticks"]) == process_start_ticks
                    and existing["owner_id"] == expected_owner_id
                    and existing["dispatch_id"] == run["dispatch_id"]
                    and (run["owner_id"] == expected_owner_id or owns_command(
                        db, WorkflowWriter(repository, deployment_role(repository, expected_owner_id), expected_owner_id), run["owner_id"]))
                )
                if not identical:
                    raise ControlStoreError(
                        f"conflicting live process lease for run: {run_id}"
                    )
                if review_attached:
                    self._require_review_launch_receipt(
                        db, run_id, review_scope[0], run, existing,
                    )
                return Lease(
                    run_id,
                    existing["dispatch_id"],
                    existing["owner_id"],
                    pid,
                    pgid,
                    _parse_stamp(existing["started_at"]),
                    process_start_ticks,
                )
            if run["owner_id"] != expected_owner_id:
                raise RetiredOwnerError(
                    f"reserved run owner changed from {expected_owner_id!r} "
                    f"to {run['owner_id']!r} before process registration"
                )
            if run["workflow_binding_json"] is not None:
                captured = decode_workflow(run["workflow_binding_json"], WorkflowBinding)
                if captured.authority.family is WorkflowFamily.REVIEW_REJECTIONS:
                    snapshot = load_review_workflow_row(db, captured.fence.key)
                    if snapshot is not None and isinstance(snapshot.execution, BlockedReviewExecutionMigration):
                        raise ControlStoreError("review execution migration is blocked")
            review_admission = None
            if review_attached:
                if not review_child_identity_proven:
                    raise ControlStoreError("review launch lacks a live registered process")
                review_admission = self._require_review_launch_admission(
                    db, run_id, review_scope[0], run, review_parent_identity,
                )
            self._require_continuation_launch(db, run_id)
            started_at = _parse_stamp(_stamp(self._now()))
            lease = Lease(
                run_id,
                run["dispatch_id"],
                run["owner_id"],
                pid,
                pgid,
                started_at,
                process_start_ticks,
            )
            db.execute(
                "INSERT INTO leases(run_id, dispatch_id, owner_id, pid, pgid, "
                "process_start_ticks, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(lease.run_id),
                    lease.dispatch_id,
                    lease.owner_id,
                    lease.pid,
                    lease.pgid,
                    lease.process_start_ticks,
                    _stamp(lease.started_at),
                ),
            )
            db.execute(
                "UPDATE runs SET state='running', "
                "started_at=COALESCE(started_at, ?) WHERE run_id=?",
                (_stamp(started_at), str(run_id)),
            )
            db.execute(
                "UPDATE reservation_recovery SET policy='running', "
                "reason=NULL, updated_at=? WHERE run_id=?",
                (_stamp(started_at), str(run_id)),
            )
            if review_admission is not None:
                launch = ReviewChildLaunchRegistration(
                    review_admission, f"review-execution-launch:{run_id}",
                    lease.dispatch_id, pid, pgid, process_start_ticks, started_at,
                )
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                    "VALUES ('review-execution-launch',?,?,?,?)",
                    (review_scope[0], str(run_id), _stamp(started_at), encode_workflow(launch)),
                )
            self._transition(
                db,
                "lease",
                str(run_id),
                "active",
                {
                    "dispatch_id": lease.dispatch_id,
                    "owner_id": lease.owner_id,
                    "pid": pid,
                    "pgid": pgid,
                    "process_start_ticks": process_start_ticks,
                },
                correlation=Correlation(
                    dispatch_id=lease.dispatch_id,
                    run_id=str(run_id),
                ),
            )
            return lease

    @staticmethod
    def _load_review_launch_admission(
        db: sqlite3.Connection, run_id: RunId, repository: str, run: sqlite3.Row,
    ) -> ReviewChildAdmission:
        receipt = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-admission' "
            "AND scope=? AND key=?",
            (repository, str(run_id)),
        ).fetchone()
        budget = db.execute(
            "SELECT reservation_id,workload_repository,state FROM budget_reservations WHERE run_id=?",
            (str(run_id),),
        ).fetchone()
        if receipt is None or budget is None:
            raise ControlStoreError("review launch lacks its admission or reservation")
        try:
            admission = decode_workflow(receipt["payload_json"], ReviewChildAdmission)
            indexed = _indexed_review_child_admission(db, repository, admission.child)
        except WorkflowEncodingError as exc:
            raise ControlStoreError("review launch admission receipt is unreadable") from exc
        except WorkflowAuthorityError as exc:
            raise ControlStoreError(str(exc)) from exc
        binding = admission.child.round.binding.rejection
        if (indexed != admission or admission.run_id != str(run_id)
                or admission.reservation_id != budget["reservation_id"]
                or budget["workload_repository"] != repository or admission.owner_id != run["owner_id"]
                or admission.child.round.round_id != run["review_round_id"]
                or admission.child.child_id != run["review_child_id"]
                or run["workflow_binding_json"] != encode_workflow(binding)
                or run["workflow_episode_key"] != workflow_episode_key(binding)
                or run["kind"] not in _REVIEW_PAID_KINDS):
            raise ControlStoreError("review launch disagrees with its admitted run")
        return admission

    def _require_review_launch_admission(
        self, db: sqlite3.Connection, run_id: RunId, repository: str,
        run: sqlite3.Row, parent_identity: tuple[int, int] | None,
    ) -> ReviewChildAdmission:
        if run["state"] != "reserved":
            raise ControlStoreError("review launch requires a still reserved run")
        if db.execute(
            "SELECT 1 FROM dedupe_keys WHERE namespace='review-execution-launch' "
            "AND scope=? AND key=?", (repository, str(run_id)),
        ).fetchone() is not None:
            raise ControlStoreError("review run already has a launch registration")
        admission = self._load_review_launch_admission(db, run_id, repository, run)
        budget = db.execute(
            "SELECT state FROM budget_reservations WHERE run_id=?", (str(run_id),),
        ).fetchone()
        recovery = db.execute(
            "SELECT policy FROM reservation_recovery WHERE run_id=?", (str(run_id),),
        ).fetchone()
        if (budget is None or budget["state"] != "active" or recovery is None
                or recovery["policy"] != "spawn_pending"):
            raise ControlStoreError("review launch requires an active unlaunched reservation")
        snapshot = require_review_binding(db, admission.child.round.binding.rejection)
        if (admission.admitted_revision.fence != snapshot.revision.fence
                or admission.admitted_revision.version > snapshot.revision.version):
            raise ControlStoreError("review launch admission revision is stale or impossible")
        try:
            dispatch = json.loads(run["dispatch_id"])
        except (TypeError, ValueError) as exc:
            raise ControlStoreError("review launch dispatch identity is unreadable") from exc
        if not isinstance(dispatch, dict):
            raise ControlStoreError("review launch dispatch identity is malformed")
        selection = self._require_review_child_authority(
            db, repository, run["owner_id"], admission.child, run["kind"],
            dispatch, snapshot, parent_identity,
        )
        if admission.claim_occurrence_id != selection.claim_occurrence_id:
            raise ControlStoreError("review launch changed its parent claim occurrence")
        target = (f"issue:{selection.claim_target}" if selection.claim_target > 0
                  else f"pr:{-selection.claim_target}")
        if db.execute(
            "SELECT 1 FROM dispatch_claim_runs WHERE repository=? AND role='review' "
            "AND target=? AND run_id=?",
            (repository, target, str(run_id)),
        ).fetchone() is None:
            raise ControlStoreError("review launch lost its parent claim/run link")
        return admission

    def _require_review_launch_receipt(
        self, db: sqlite3.Connection, run_id: RunId, repository: str,
        run: sqlite3.Row, lease: sqlite3.Row,
    ) -> None:
        admission = self._load_review_launch_admission(db, run_id, repository, run)
        receipt = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-launch' "
            "AND scope=? AND key=?", (repository, str(run_id)),
        ).fetchone()
        if receipt is None:
            raise ControlStoreError("review process lease lacks its immutable launch receipt")
        try:
            recorded = decode_workflow(receipt["payload_json"], ReviewChildLaunchRegistration)
            expected = ReviewChildLaunchRegistration(
                admission, f"review-execution-launch:{run_id}", run["dispatch_id"],
                lease["pid"], lease["pgid"], lease["process_start_ticks"],
                _parse_stamp(lease["started_at"]),
            )
        except (WorkflowEncodingError, ValueError, TypeError) as exc:
            raise ControlStoreError("review launch receipt is unreadable") from exc
        if (recorded != expected or lease["dispatch_id"] != run["dispatch_id"]
                or lease["owner_id"] != run["owner_id"] or run["state"] != "running"):
            raise ControlStoreError("review launch receipt disagrees with its live lease")

    def release_lease(self, run_id: RunId) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
            self._transition(
                db,
                "lease",
                str(run_id),
                "released",
                correlation=Correlation(run_id=str(run_id)),
            )

    def leases(self, owner_id: str | None = None) -> tuple[Lease, ...]:
        with closing(self._connect()) as db, db:
            rows = (
                db.execute(
                    "SELECT l.run_id, l.dispatch_id, l.owner_id, l.pid, "
                    "l.pgid, l.started_at, l.process_start_ticks FROM leases l JOIN runs r "
                    "ON r.run_id=l.run_id WHERE l.owner_id=? "
                    "ORDER BY l.started_at",
                    (owner_id,),
                ).fetchall()
                if owner_id is not None
                else db.execute(
                    "SELECT run_id, dispatch_id, owner_id, pid, pgid, "
                    "started_at, process_start_ticks FROM leases ORDER BY started_at"
                ).fetchall()
            )
        return tuple(
            Lease(
                RunId(row["run_id"]),
                row["dispatch_id"],
                row["owner_id"],
                row["pid"],
                row["pgid"],
                _parse_stamp(row["started_at"]),
                int(row["process_start_ticks"]),
            )
            for row in rows
        )

    def publication_authority(
        self, run_id: RunId
    ) -> PublicationAuthority | None:
        """Return authority only for an active, supervised paid run."""

        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT b.repository, r.owner_id, r.dispatch_id, l.pgid, "
                "l.process_start_ticks, r.workflow_binding_json FROM runs r "
                "JOIN budget_reservations b ON b.run_id=r.run_id "
                "JOIN leases l ON l.run_id=r.run_id "
                "WHERE r.run_id=? "
                "AND r.state IN ('starting', 'running') "
                "AND b.state='active'",
                (str(run_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            dispatch = json.loads(str(row["dispatch_id"]))
            role = str(dispatch["role"])
            kind = str(dispatch["kind"])
            raw_issue = dispatch.get("issue")
            issue = int(raw_issue) if raw_issue is not None else None
            cwd = str(dispatch["cwd"])
            branch = str(dispatch["publication_branch"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            role != "fix"
            or (issue is not None and issue <= 0)
            or not kind
            or not cwd
            or not branch
            or not Path(cwd).is_absolute()
        ):
            return None
        return PublicationAuthority(
            run_id,
            str(row["repository"]),
            str(row["owner_id"]),
            role,
            kind,
            issue,
            cwd,
            branch,
            int(row["pgid"]),
            int(row["process_start_ticks"]),
            decode_workflow(row["workflow_binding_json"], WorkflowBinding) if row["workflow_binding_json"] is not None else None,
        )

    def reclaim_dead_leases(
        self,
        owner_id: str | Callable[[int, int], bool],
        is_alive: Callable[[int, int], bool] | None = None,
    ) -> tuple[RunId, ...]:
        """Abandon leases whose snapshotted durable owner process is gone.

        Process inspection may invoke ``ps`` on macOS, so all liveness probes
        run before the short write transaction. The transaction revalidates the
        complete lease identity before changing state to avoid reclaiming a
        lease that was replaced while the probe ran.
        """

        if callable(owner_id):
            alive = owner_id
            scoped_owner: str | None = None
        else:
            if is_alive is None:
                raise ValueError("liveness probe is required")
            alive = is_alive
            scoped_owner = owner_id
        with closing(self._connect()) as db, db:
            if scoped_owner is not None:
                self._assert_owner_active(db, scoped_owner)
            rows = (
                db.execute(
                    "SELECT l.run_id, l.dispatch_id, l.owner_id, l.pid, "
                    "l.pgid, l.process_start_ticks, l.started_at "
                    "FROM leases l JOIN runs r "
                    "ON r.run_id=l.run_id WHERE l.owner_id=? "
                    "ORDER BY l.started_at",
                    (scoped_owner,),
                ).fetchall()
                if scoped_owner is not None
                else db.execute(
                    "SELECT run_id, dispatch_id, owner_id, pid, pgid, "
                    "process_start_ticks, started_at "
                    "FROM leases ORDER BY started_at"
                ).fetchall()
            )
        dead = tuple(
            (
                str(row["run_id"]),
                str(row["dispatch_id"]),
                str(row["owner_id"]),
                int(row["pid"]),
                int(row["pgid"]),
                int(row["process_start_ticks"]),
                str(row["started_at"]),
            )
            for row in rows
            if not alive(int(row["pid"]), int(row["process_start_ticks"]))
        )
        reclaimed: list[RunId] = []
        with self.transaction() as db:
            for identity in dead:
                run_id = RunId(identity[0])
                current = db.execute(
                    "SELECT run_id, dispatch_id, owner_id, pid, pgid, "
                    "process_start_ticks, started_at FROM leases "
                    "WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                if current is None:
                    continue
                current_identity = (
                    str(current["run_id"]),
                    str(current["dispatch_id"]),
                    str(current["owner_id"]),
                    int(current["pid"]),
                    int(current["pgid"]),
                    int(current["process_start_ticks"]),
                    str(current["started_at"]),
                )
                if current_identity != identity:
                    continue
                db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
                updated = db.execute(
                    "UPDATE runs SET state='abandoned', outcome='dead_lease', "
                    "completed_at=? WHERE run_id=? AND state NOT IN "
                    "('completed','cancelled','abandoned')",
                    (_stamp(self._now()), str(run_id)),
                ).rowcount
                if not updated:
                    continue
                retained_reason = (
                    "process ownership lost before spend reconciliation"
                )
                db.execute(
                    "UPDATE reservation_recovery SET policy='reconcile_only', "
                    "reason=?, updated_at=? WHERE run_id=?",
                    (retained_reason, _stamp(self._now()), str(run_id)),
                )
                reservation = db.execute(
                    "SELECT state FROM budget_reservations WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                if reservation is not None and reservation["state"] == "active":
                    self._event(
                        db,
                        ReservationRetained(str(run_id), retained_reason),
                        correlation=Correlation(run_id=str(run_id)),
                    )
                self._transition(
                    db,
                    "run",
                    str(run_id),
                    "abandoned",
                    {"outcome": "dead_lease"},
                    correlation=Correlation(run_id=str(run_id)),
                )
                reclaimed.append(run_id)
        return tuple(reclaimed)

    def active_reservation_runs(
        self, owner_id: str | None = None
    ) -> tuple[RunId, ...]:
        with closing(self._connect()) as db, db:
            owners = self._recovery_owners(db, owner_id) if owner_id is not None else ()
            rows = (
                db.execute(
                    "SELECT b.run_id FROM budget_reservations b JOIN runs r "
                    "ON r.run_id=b.run_id WHERE b.state='active' "
                    f"AND r.owner_id IN ({','.join('?' for _ in owners)}) ORDER BY b.created_at",
                    owners,
                ).fetchall()
                if owner_id is not None
                else db.execute(
                    "SELECT run_id FROM budget_reservations "
                    "WHERE state='active' ORDER BY created_at"
                ).fetchall()
            )
        return tuple(RunId(row["run_id"]) for row in rows)

    def stale_active_reservation_runs(
        self,
        owner_id: str,
        created_before: datetime,
    ) -> tuple[RunId, ...]:
        """Return active reservations old enough to be outside spawn grace."""

        cutoff = _stamp(created_before)
        with closing(self._connect()) as db, db:
            owners = self._recovery_owners(db, owner_id)
            rows = db.execute(
                "SELECT b.run_id FROM budget_reservations b JOIN runs r "
                "ON r.run_id=b.run_id WHERE b.state='active' "
                f"AND r.owner_id IN ({','.join('?' for _ in owners)}) AND b.created_at<=? ORDER BY b.created_at",
                (*owners, cutoff),
            ).fetchall()
        return tuple(RunId(row["run_id"]) for row in rows)

    def record_reservation_retained(
        self,
        run_id: RunId,
        reason: str,
        *,
        returned_review: ReturnedReviewChild | None = None,
    ) -> bool:
        """Retain uncertain spend, or capture its return after reconciliation wins."""

        if not reason:
            raise ValueError("reservation retention reason is required")
        if returned_review is not None and (
            not isinstance(returned_review, ReturnedReviewChild)
            or returned_review.run_id != str(run_id)
        ):
            raise ValueError("retained review return requires its exact run")
        with self.transaction() as db:
            row = db.execute(
                "SELECT b.state,b.workload_repository,r.dispatch_id FROM budget_reservations b "
                "JOIN runs r ON r.run_id=b.run_id WHERE b.run_id=?",
                (str(run_id),),
            ).fetchone()
            review_return = (
                self._review_return_authority(db, returned_review)
                if returned_review is not None else None
            )
            if row is None:
                if returned_review is not None:
                    raise ControlStoreError("review return lacks its paid reservation")
                return False
            stop = db.execute(
                "SELECT payload_json FROM dedupe_keys "
                "WHERE namespace='review-execution-preflight-stop' AND scope=? AND key=?",
                (row["workload_repository"], str(run_id)),
            ).fetchone()
            if stop is not None:
                stopped = decode_workflow(stop["payload_json"], ReviewRuntimePreflightStop)
                if (stopped.admission.run_id != str(run_id)
                        or (returned_review is not None and stopped.result != returned_review.result)):
                    raise ControlStoreError("review retained return disagrees with its runtime preflight stop")
                return True
            if row["state"] == "settled" and review_return is not None:
                charge = db.execute(
                    "SELECT 1 FROM charges WHERE run_id=?", (str(run_id),),
                ).fetchone()
                if charge is None:
                    raise ControlStoreError("settled review return lacks its actual charge")
                if not review_return[1]:
                    self._insert_review_return(db, review_return[0], returned_review)
                return False
            if row["state"] != "active":
                if returned_review is not None:
                    raise ControlStoreError("review return has no retainable or settled reservation")
                return False
            now = _stamp(self._now())
            recovery_changed = db.execute(
                "UPDATE reservation_recovery SET policy='reconcile_only', "
                "reason=?, updated_at=? WHERE run_id=? "
                "AND (policy IS NOT 'reconcile_only' OR reason IS NOT ?)",
                (reason, now, str(run_id), reason),
            ).rowcount
            run_changed = db.execute(
                "UPDATE runs SET "
                "state=CASE WHEN state IN ('completed','cancelled','abandoned') "
                "THEN state ELSE 'completed' END, "
                "outcome=CASE WHEN state IN ('completed','cancelled','abandoned') "
                "THEN outcome ELSE 'spend_unreconciled' END, "
                "completed_at=COALESCE(completed_at, ?) WHERE run_id=? "
                "AND (state NOT IN ('completed','cancelled','abandoned') "
                "OR completed_at IS NULL)",
                (now, str(run_id)),
            ).rowcount
            lease_removed = db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),)).rowcount
            if review_return is not None and not review_return[1]:
                self._insert_review_return(db, review_return[0], returned_review)
            if recovery_changed or run_changed or lease_removed or review_return is not None and not review_return[1]:
                self._event(
                    db,
                    ReservationRetained(str(run_id), reason),
                    correlation=_run_correlation(run_id, row["dispatch_id"]),
                )
            return True

    def record_admission_reconciled(
        self,
        run_id: RunId,
        scope: str,
        released_runs: int,
        admitted: bool,
        reason: str,
    ) -> None:
        """Journal the single reconcile-then-retry admission took (ADR 0012)."""

        if not reason or not scope:
            raise ValueError("admission reconcile scope and reason are required")
        if released_runs < 0:
            raise ValueError("released run count cannot be negative")
        with self.transaction() as db:
            self._event(
                db,
                AdmissionReconciled(
                    str(run_id),
                    scope,
                    released_runs,
                    admitted,
                    reason,
                ),
                correlation=Correlation(run_id=str(run_id)),
            )

    def record_owned_cancellation_intents(
        self,
        owner_id: str,
        reason: str,
    ) -> tuple[RunCancellationIntent, ...]:
        """Persist hard-shutdown handovers without discarding spend authority.

        A killed provider process can leave rollout usage that becomes readable
        only after process reap. The active reservation and lease therefore stay
        intact until post-kill or boot recovery settles that evidence.
        """

        if not owner_id or not reason:
            raise ValueError("run owner and cancellation reason are required")
        intents: list[RunCancellationIntent] = []
        with self.transaction() as db:
            owners = self._recovery_owners(db, owner_id)
            rows = db.execute(
                "SELECT r.run_id, r.dispatch_id, r.kind, r.state FROM runs r "
                "JOIN budget_reservations b ON b.run_id=r.run_id "
                f"WHERE r.owner_id IN ({','.join('?' for _ in owners)}) AND b.state='active' "
                "AND r.state NOT IN ('completed','cancelled','abandoned') "
                "ORDER BY b.created_at",
                owners,
            ).fetchall()
            now = _stamp(self._now())
            for row in rows:
                run_id = RunId(row["run_id"])
                db.execute(
                    "INSERT OR IGNORE INTO run_cancellation_intents("
                    "run_id, dispatch_id, kind, issue_number, reason, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(run_id),
                        row["dispatch_id"],
                        row["kind"],
                        _dispatch_issue(row["dispatch_id"]),
                        reason,
                        now,
                    ),
                )
                stored = db.execute(
                    "SELECT dispatch_id, kind, issue_number, reason "
                    "FROM run_cancellation_intents WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                intents.append(
                    RunCancellationIntent(
                        run_id,
                        stored["dispatch_id"],
                        stored["kind"],
                        (
                            int(stored["issue_number"])
                            if stored["issue_number"] is not None
                            else None
                        ),
                        stored["reason"],
                    )
                )
                self._transition(
                    db,
                    "run",
                    str(run_id),
                    row["state"],
                    {"reason": reason, "spend_recovery_pending": True},
                    correlation=_run_correlation(
                        run_id, row["dispatch_id"]
                    ),
                )
        return tuple(intents)

    def pending_cancellation_intents(
        self,
        owner_id: str,
    ) -> tuple[RunCancellationIntent, ...]:
        if not owner_id:
            raise ValueError("run owner is required")
        with closing(self._connect()) as db, db:
            owners = self._recovery_owners(db, owner_id)
            rows = db.execute(
                "SELECT i.run_id, i.dispatch_id, i.kind, i.issue_number, "
                "i.reason FROM run_cancellation_intents i JOIN runs r "
                f"ON r.run_id=i.run_id WHERE r.owner_id IN ({','.join('?' for _ in owners)}) "
                "AND i.handover_written_at IS NULL ORDER BY i.created_at",
                owners,
            ).fetchall()
        return tuple(
            RunCancellationIntent(
                RunId(row["run_id"]),
                row["dispatch_id"],
                row["kind"],
                (
                    int(row["issue_number"])
                    if row["issue_number"] is not None
                    else None
                ),
                row["reason"],
            )
            for row in rows
        )

    def has_cancellation_intent(self, run_id: RunId) -> bool:
        with closing(self._connect()) as db, db:
            return (
                db.execute(
                    "SELECT 1 FROM run_cancellation_intents WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                is not None
            )

    def mark_cancellation_handover_written(self, run_id: RunId) -> None:
        with self.transaction() as db:
            updated = db.execute(
                "UPDATE run_cancellation_intents SET handover_written_at=? "
                "WHERE run_id=? AND handover_written_at IS NULL",
                (_stamp(self._now()), str(run_id)),
            )
            if updated.rowcount != 1:
                raise KeyError(str(run_id))

    def abandoned_dispatch_counts(self, owner_id: str) -> dict[int, int]:
        """Return durable crash counts keyed by the domain breaker target.

        New admissions encode their target in ``dispatch_id``. Historical
        opaque ids are ignored: JSONL remains telemetry and is never a fallback
        control database.
        """

        if not owner_id:
            raise ValueError("run owner identity is required")
        with closing(self._connect()) as db, db:
            owners = self._recovery_owners(db, owner_id)
            rows = db.execute(
                "SELECT dispatch_id FROM runs "
                f"WHERE owner_id IN ({','.join('?' for _ in owners)}) AND state='abandoned' ORDER BY completed_at",
                owners,
            ).fetchall()
        counts: dict[int, int] = {}
        for row in rows:
            try:
                payload = json.loads(row["dispatch_id"])
                target = int(payload["breaker_key"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            counts[target] = counts.get(target, 0) + 1
        return counts

    def claim(
        self,
        namespace: str,
        scope: str,
        key: str,
        *,
        reusable_after: datetime | None = None,
    ) -> bool:
        if not namespace or not scope or not key:
            raise ValueError("dedupe namespace, scope, and key are required")
        now = self._now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT reusable_after FROM dedupe_keys "
                "WHERE namespace=? AND scope=? AND key=?",
                (namespace, scope, key),
            ).fetchone()
            if row is not None:
                expires = (
                    _parse_stamp(row["reusable_after"])
                    if row["reusable_after"] is not None
                    else None
                )
                if expires is None or expires > now:
                    return False
                db.execute(
                    "DELETE FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                    (namespace, scope, key),
                )
            db.execute(
                "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, "
                "reusable_after) VALUES (?, ?, ?, ?, ?)",
                (
                    namespace,
                    scope,
                    key,
                    _stamp(now),
                    _stamp(reusable_after) if reusable_after else None,
                ),
            )
            return True

    def claim_dispatch(
        self,
        repository: str,
        role: str,
        issue: int,
        leg: str,
        owner_id: str,
        paths: frozenset[str],
    ) -> bool:
        """Atomically reserve one paid target/path set across deployments."""

        if (
            not repository
            or not role
            or issue == 0
            or not leg
            or not owner_id
        ):
            raise ValueError("dispatch claim identity is incomplete")
        from .process_supervisor import ProcessSupervisor

        identity = ProcessSupervisor.process_identity(os.getpid())
        if identity is None:
            raise ProcessLookupError("dispatch owner identity is unavailable")
        target = _dispatch_claim_target(issue)
        encoded_paths = _json(sorted(paths))
        with closing(self._connect()) as db, db:
            observed = self._dispatch_claim_observations(
                db, repository, role
            )
        liveness = {
            claim: (
                ProcessSupervisor.matches_identity(
                    claim.owner_pid,
                    claim.owner_start_ticks,
                )
                or any(
                    state in {"reserved", "starting", "running"}
                    for (
                        _run_id,
                        state,
                        _lease_pid,
                        _lease_start_ticks,
                    ) in claim.bound_runs
                )
            )
            for claim in observed
        }
        try:
            with self.transaction() as db:
                self._assert_owner_active(db, owner_id)
                current = self._dispatch_claim_observations(
                    db, repository, role
                )
                live: list[_DispatchClaimObservation] = []
                for claim in current:
                    # A claim or child binding changed after the read-only snapshot
                    # is conservatively live. Process inspection must never run
                    # while SQLite's cross-process write lock is held.
                    if liveness.get(claim, True):
                        live.append(claim)
                        continue
                    db.execute(
                        "DELETE FROM dispatch_claims "
                        "WHERE repository=? AND role=? AND target=? "
                        "AND owner_pid=? AND owner_start_ticks=? AND occurrence_id=?",
                        (
                            repository,
                            role,
                            claim.target,
                            claim.owner_pid,
                            claim.owner_start_ticks,
                            claim.occurrence_id,
                        ),
                    )
                    _claim_lifecycle_receipt(db, repository, claim.occurrence_id, "reclaimed", _stamp(self._now()), {
                        "role": role, "target": claim.target, "leg": claim.leg,
                        "owner_id": claim.owner_id, "owner_pid": claim.owner_pid,
                        "owner_start_ticks": claim.owner_start_ticks,
                        "claimed_at": claim.claimed_at,
                    })
                for claim in live:
                    try:
                        other_paths = frozenset(json.loads(claim.paths_json))
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise CorruptControlStore(
                            "dispatch claim contains invalid paths"
                        ) from exc
                    if (
                        claim.target == target
                        or not paths
                        or not other_paths
                        or bool(paths & other_paths)
                    ):
                        return False
                occurrence_id = uuid.uuid4().hex
                claimed_at = _stamp(self._now())
                db.execute(
                    "INSERT INTO dispatch_claims("
                    "repository, role, target, leg, owner_id, owner_pid, "
                    "owner_start_ticks, paths_json, claimed_at, occurrence_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        repository,
                        role,
                        target,
                        leg,
                        owner_id,
                        identity.pid,
                        identity.start_ticks,
                        encoded_paths,
                        claimed_at,
                        occurrence_id,
                    ),
                )
                _claim_lifecycle_receipt(db, repository, occurrence_id, "created", claimed_at, {
                    "role": role, "target": target, "leg": leg, "owner_id": owner_id,
                    "owner_pid": identity.pid, "owner_start_ticks": identity.start_ticks,
                    "claimed_at": claimed_at, "source": "admission",
                })
                return True
        except RetiredOwnerError:
            return False

    @staticmethod
    def _dispatch_claim_observations(
        db: sqlite3.Connection,
        repository: str,
        role: str,
    ) -> tuple[_DispatchClaimObservation, ...]:
        rows = db.execute(
            "SELECT dc.target, dc.leg, dc.owner_id, dc.owner_pid, "
            "dc.owner_start_ticks, dc.occurrence_id, dc.claimed_at, dc.paths_json, dcr.run_id, r.state, "
            "l.pid AS lease_pid, l.process_start_ticks AS lease_start_ticks "
            "FROM dispatch_claims dc "
            "LEFT JOIN dispatch_claim_runs dcr "
            "ON dcr.repository=dc.repository AND dcr.role=dc.role "
            "AND dcr.target=dc.target "
            "LEFT JOIN runs r ON r.run_id=dcr.run_id "
            "LEFT JOIN leases l ON l.run_id=dcr.run_id "
            "WHERE dc.repository=? AND dc.role=? "
            "ORDER BY dc.target, dcr.run_id",
            (repository, role),
        ).fetchall()
        grouped: dict[
            str,
            tuple[sqlite3.Row, list[tuple[str, str, int, int]]],
        ] = {}
        for row in rows:
            target = str(row["target"])
            base, bindings = grouped.setdefault(target, (row, []))
            if row["run_id"] is not None:
                bindings.append(
                    (
                        str(row["run_id"]),
                        str(row["state"]),
                        int(row["lease_pid"] or 0),
                        int(row["lease_start_ticks"] or 0),
                    )
                )
        return tuple(
            _DispatchClaimObservation(
                target,
                str(base["leg"]),
                str(base["owner_id"]),
                int(base["owner_pid"]),
                int(base["owner_start_ticks"]),
                str(base["occurrence_id"]),
                str(base["claimed_at"]),
                str(base["paths_json"]),
                tuple(bindings),
            )
            for target, (base, bindings) in grouped.items()
        )

    def bind_dispatch_claim(
        self,
        repository: str,
        role: str,
        issue: int,
        owner_id: str,
        run_id: RunId,
    ) -> bool:
        """Attach one admitted run so child liveness protects its global claim."""

        if (
            not repository
            or not role
            or issue == 0
            or not owner_id
            or not str(run_id)
        ):
            raise ValueError("dispatch claim binding identity is incomplete")
        target = _dispatch_claim_target(issue)
        try:
            with self.transaction() as db:
                self._assert_owner_active(db, owner_id)
                claim = db.execute(
                    "SELECT owner_id FROM dispatch_claims "
                    "WHERE repository=? AND role=? AND target=?",
                    (repository, role, target),
                ).fetchone()
                run = db.execute(
                    "SELECT owner_id, state FROM runs WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                if (
                    claim is None
                    or run is None
                    or claim["owner_id"] != owner_id
                    or run["owner_id"] != owner_id
                    or run["state"] not in {"reserved", "starting", "running"}
                ):
                    return False
                db.execute(
                    "INSERT OR IGNORE INTO dispatch_claim_runs("
                    "repository, role, target, run_id, bound_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        repository,
                        role,
                        target,
                        str(run_id),
                        _stamp(self._now()),
                    ),
                )
                return True
        except RetiredOwnerError:
            return False

    def expand_dispatch_claim(
        self,
        repository: str,
        role: str,
        issue: int,
        owner_id: str,
        paths: frozenset[str],
    ) -> bool:
        """Add router-discovered paths without racing another deployment."""

        target = _dispatch_claim_target(issue)
        with self.transaction() as db:
            owned = db.execute(
                "SELECT paths_json FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target=? AND owner_id=?",
                (repository, role, target, owner_id),
            ).fetchone()
            if owned is None:
                return False
            try:
                expanded = frozenset(json.loads(owned["paths_json"])) | paths
            except (TypeError, json.JSONDecodeError) as exc:
                raise CorruptControlStore(
                    "dispatch claim contains invalid paths"
                ) from exc
            rows = db.execute(
                "SELECT target, paths_json FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target<>?",
                (repository, role, target),
            ).fetchall()
            for row in rows:
                try:
                    other_paths = frozenset(json.loads(row["paths_json"]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise CorruptControlStore(
                        "dispatch claim contains invalid paths"
                    ) from exc
                if (
                    not expanded
                    or not other_paths
                    or bool(expanded & other_paths)
                ):
                    return False
            db.execute(
                "UPDATE dispatch_claims SET paths_json=? "
                "WHERE repository=? AND role=? AND target=? AND owner_id=?",
                (
                    _json(sorted(expanded)),
                    repository,
                    role,
                    target,
                    owner_id,
                ),
            )
            return True

    def release_dispatch_claim(
        self,
        repository: str,
        role: str,
        issue: int,
        owner_id: str,
        *,
        expected_occurrence_id: str | None = None,
        expected_process_identity: tuple[int, int] | None = None,
    ) -> bool:
        target = _dispatch_claim_target(issue)
        with self.transaction() as db:
            claim = db.execute(
                "SELECT occurrence_id,leg,owner_pid,owner_start_ticks,claimed_at FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target=? AND owner_id=?",
                (repository, role, target, owner_id),
            ).fetchone()
            if (claim is None
                    or (expected_occurrence_id is not None and claim["occurrence_id"] != expected_occurrence_id)
                    or (expected_process_identity is not None
                        and (claim["owner_pid"], claim["owner_start_ticks"]) != expected_process_identity)):
                return False
            cursor = db.execute(
                "DELETE FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target=? AND owner_id=? AND occurrence_id=?",
                (repository, role, target, owner_id, claim["occurrence_id"]),
            )
            if cursor.rowcount == 1:
                _claim_lifecycle_receipt(db, repository, claim["occurrence_id"], "released", _stamp(self._now()), {
                    "role": role, "target": target, "leg": claim["leg"],
                    "owner_id": owner_id, "owner_pid": claim["owner_pid"],
                    "owner_start_ticks": claim["owner_start_ticks"],
                    "claimed_at": claim["claimed_at"],
                })
            return cursor.rowcount == 1

    def active_dispatch_claims(
        self, repository: str, role: str
    ) -> tuple[ActiveDispatchClaim, ...]:
        """Live paid targets one role currently holds (harness #176).

        Read-only cross-role observation: the reviewer asks whether the fixer is
        mid-push on a PR before paying for a review that the freshness fence
        would then reject. Liveness uses the same rule `claim_dispatch` applies
        when it garbage-collects — owner process alive, or a bound run still
        non-terminal.

        A row whose target or timestamp cannot be parsed is SKIPPED rather than
        reported. This reader only withholds work, so an unreadable row must
        fail open to dispatch: the submit-time head fence still protects
        correctness, whereas failing closed would let one corrupt row starve
        review indefinitely.
        """

        with closing(self._connect()) as db, db:
            rows = db.execute(
                "SELECT dc.target, dc.leg, dc.owner_id, dc.owner_pid, "
                "dc.owner_start_ticks, dc.claimed_at, dc.occurrence_id, dcr.run_id, r.state "
                "FROM dispatch_claims dc "
                "LEFT JOIN dispatch_claim_runs dcr "
                "ON dcr.repository=dc.repository AND dcr.role=dc.role "
                "AND dcr.target=dc.target "
                "LEFT JOIN runs r ON r.run_id=dcr.run_id "
                "WHERE dc.repository=? AND dc.role=? "
                "ORDER BY dc.target, dcr.run_id",
                (repository, role),
            ).fetchall()
        grouped: dict[str, tuple[sqlite3.Row, list[tuple[str, str]]]] = {}
        for row in rows:
            _base, bindings = grouped.setdefault(str(row["target"]), (row, []))
            if row["run_id"] is not None:
                bindings.append((str(row["run_id"]), str(row["state"])))
        from .process_supervisor import ProcessSupervisor

        claims: list[ActiveDispatchClaim] = []
        for target, (base, bindings) in grouped.items():
            target_key = _dispatch_claim_issue(target)
            if target_key is None:
                continue
            try:
                claimed_at = _parse_stamp(str(base["claimed_at"]))
            except ValueError:
                continue
            occurrence_id = base["occurrence_id"]
            if not isinstance(occurrence_id, str) or not occurrence_id:
                continue
            live_run = any(
                state in {"reserved", "starting", "running"}
                for _run_id, state in bindings
            )
            if not live_run and not ProcessSupervisor.matches_identity(
                int(base["owner_pid"]), int(base["owner_start_ticks"])
            ):
                continue
            owner_id = str(base["owner_id"])
            claims.append(
                ActiveDispatchClaim(
                    target_key=target_key,
                    role=role,
                    leg=str(base["leg"]),
                    owner_id=owner_id,
                    claimed_at=claimed_at,
                    claim_key=occurrence_id,
                )
            )
        return tuple(claims)

    def receipt(
        self,
        namespace: str,
        scope: str,
        key: str,
        payload: Mapping[str, object],
    ) -> bool:
        encoded = _json(payload)
        with self.transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys "
                "WHERE namespace=? AND scope=? AND key=?",
                (namespace, scope, key),
            ).fetchone()
            if row is not None:
                return row["payload_json"] == encoded
            db.execute(
                "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, "
                "payload_json) VALUES (?, ?, ?, ?, ?)",
                (namespace, scope, key, _stamp(self._now()), encoded),
            )
            return True

    def load_level_state(
        self, namespace: str, scope: str
    ) -> dict[str, dict[str, JsonValue]]:
        """Read a named level snapshot; malformed persisted payload fails closed."""

        if not namespace or not scope:
            raise ValueError("level namespace and scope are required")
        with closing(self._connect()) as db, db:
            rows = db.execute(
                "SELECT key, payload_json FROM level_state "
                "WHERE namespace=? AND scope=? ORDER BY key",
                (namespace, scope),
            ).fetchall()
        result: dict[str, dict[str, JsonValue]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise CorruptControlStore(
                    f"invalid level payload for {namespace}/{scope}/{row['key']}"
                ) from exc
            if not isinstance(payload, dict):
                raise CorruptControlStore(
                    f"level payload for {namespace}/{scope}/{row['key']} "
                    "is not an object"
                )
            result[str(row["key"])] = payload
        return result

    def replace_level_state(
        self,
        namespace: str,
        scope: str,
        values: Mapping[str, Mapping[str, JsonValue]],
    ) -> None:
        """Atomically replace a restart-safe level snapshot."""

        if not namespace or not scope:
            raise ValueError("level namespace and scope are required")
        if any(not key for key in values):
            raise ValueError("level keys are required")
        updated_at = _stamp(self._now())
        with self.transaction() as db:
            db.execute(
                "DELETE FROM level_state WHERE namespace=? AND scope=?",
                (namespace, scope),
            )
            db.executemany(
                "INSERT INTO level_state(namespace, scope, key, payload_json, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    (namespace, scope, key, _json(payload), updated_at)
                    for key, payload in sorted(values.items())
                ),
            )

    def record_world_observation(
        self,
        *,
        deployment: str,
        subject_kind: str,
        subject_number: int,
        content_hash: str,
        facts: Mapping[str, JsonValue],
        correlation: Correlation,
    ) -> bool:
        """Commit a changed observation and its outbox event atomically.

        Deduplication is against the subject's *latest* hash, not every hash ever
        seen, so ``A → B → A`` remains three observable transitions. A failed
        event sink leaves the committed outbox row pending for retry.
        """

        if (
            not deployment
            or subject_kind not in {"issue", "pr"}
            or subject_number <= 0
            or not content_hash
        ):
            raise ValueError("world observation identity is incomplete")
        with self.transaction() as db:
            row = db.execute(
                "SELECT content_hash FROM world_observations "
                "WHERE deployment=? AND subject_kind=? AND subject_number=?",
                (deployment, subject_kind, subject_number),
            ).fetchone()
            if row is not None and row["content_hash"] == content_hash:
                return False
            db.execute(
                "INSERT INTO world_observations("
                "deployment, subject_kind, subject_number, content_hash, observed_at"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(deployment, subject_kind, subject_number) DO UPDATE SET "
                "content_hash=excluded.content_hash, observed_at=excluded.observed_at",
                (
                    deployment,
                    subject_kind,
                    subject_number,
                    content_hash,
                    _stamp(self._now()),
                ),
            )
            self._event(
                db,
                WorldObserved(
                    subject_kind,
                    subject_number,
                    content_hash,
                    dict(facts),
                ),
                correlation=correlation,
            )
            return True

    def record_world_snapshot(
        self,
        *,
        deployment: str,
        subject_kind: str,
        observations: Mapping[
            int,
            tuple[str, Mapping[str, JsonValue], Correlation],
        ],
    ) -> int:
        """Atomically publish one role feed's complete membership for a kind."""

        if not deployment or subject_kind not in {"issue", "pr"}:
            raise ValueError("world snapshot identity is incomplete")
        if any(number <= 0 for number in observations):
            raise ValueError("world snapshot subject numbers must be positive")
        changed = 0
        with self.transaction() as db:
            prior = {
                int(row["subject_number"]): str(row["content_hash"])
                for row in db.execute(
                    "SELECT subject_number, content_hash "
                    "FROM world_observations "
                    "WHERE deployment=? AND subject_kind=?",
                    (deployment, subject_kind),
                ).fetchall()
            }
            for number, (content_hash, raw_facts, correlation) in sorted(
                observations.items()
            ):
                if not content_hash or content_hash.startswith("tombstone:"):
                    raise ValueError("world observation hash is invalid")
                facts = {"present": True, **dict(raw_facts)}
                encoded = json.dumps(
                    facts,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                expected_hash = hashlib.sha256(encoded.encode()).hexdigest()
                if content_hash != expected_hash:
                    raise ValueError(
                        "world observation hash does not match its facts"
                    )
                if prior.get(number) == content_hash:
                    continue
                db.execute(
                    "INSERT INTO world_observations("
                    "deployment, subject_kind, subject_number, content_hash, "
                    "observed_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(deployment, subject_kind, subject_number) "
                    "DO UPDATE SET content_hash=excluded.content_hash, "
                    "observed_at=excluded.observed_at",
                    (
                        deployment,
                        subject_kind,
                        number,
                        content_hash,
                        _stamp(self._now()),
                    ),
                )
                self._event(
                    db,
                    WorldObserved(
                        subject_kind,
                        number,
                        content_hash,
                        facts,
                    ),
                    correlation=correlation,
                )
                changed += 1
            for number, old_hash in sorted(prior.items()):
                if number in observations or old_hash.startswith("tombstone:"):
                    continue
                tombstone_hash = "tombstone:" + hashlib.sha256(
                    f"{subject_kind}:{number}".encode()
                ).hexdigest()
                db.execute(
                    "UPDATE world_observations SET content_hash=?, observed_at=? "
                    "WHERE deployment=? AND subject_kind=? AND subject_number=?",
                    (
                        tombstone_hash,
                        _stamp(self._now()),
                        deployment,
                        subject_kind,
                        number,
                    ),
                )
                correlation = (
                    Correlation(issue=number)
                    if subject_kind == "issue"
                    else Correlation(pr=number)
                )
                self._event(
                    db,
                    WorldObserved(
                        subject_kind,
                        number,
                        tombstone_hash,
                        {
                            "present": False,
                            "reason": "absent_from_feed",
                        },
                    ),
                    correlation=correlation,
                )
                changed += 1
        return changed

    def retire_projection_feed(
        self,
        *,
        deployment: str,
        repository: str,
        role: str,
    ) -> int:
        """Remove a stopped role's membership and commit its fold tombstone."""

        repository = canonical_repository(repository)
        destination = f"{deployment}:{repository}:{role}"
        if not deployment or not role:
            raise ValueError("projection feed retirement identity is incomplete")
        if (
            self._event_factory is None
            or self._event_destination != destination
        ):
            raise ValueError(
                "retiring projection feed must be bound to its event destination"
            )
        with self.transaction() as db:
            removed = db.execute(
                "DELETE FROM world_observations WHERE deployment=?",
                (deployment,),
            ).rowcount
            self._event(
                db,
                WorldFeedRetired(
                    deployment,
                    repository,
                    role,
                    restartable=True,
                ),
            )
            return removed

    def snapshot(self, scope: BudgetScope) -> AccountingSnapshot:
        try:
            with closing(self._connect()) as db, db:
                settled, reserved = self._balance(db, scope)
        except sqlite3.DatabaseError as exc:
            return AccountingSnapshot(
                AccountingDisposition.CORRUPT,
                scope,
                Money.zero(scope.currency),
                Money.zero(scope.currency),
                str(exc),
            )
        return AccountingSnapshot(
            AccountingDisposition.AVAILABLE,
            scope,
            Money(scope.currency, settled),
            Money(scope.currency, reserved),
        )

    def reserve(
        self,
        request: ReservationRequest,
        *,
        lease: Lease | None = None,
    ) -> ReservationResult:
        if request.workflow is None and not request.continuations:
            return self._reserve(request, lease=lease)
        # A reset/retirement owns the exclusive side of this existing barrier.
        # Never wait for Git while retaining a reservation or SQL transaction.
        try:
            with deployment_effect_authority(self.path, request.workload_repository,
                                             deployment_role(request.workload_repository, request.owner_id), blocking=False):
                parent_identity = self._review_parent_identity(request) if request.review_child is not None else None
                return self._reserve(request, lease=lease, review_parent_identity=parent_identity)
        except (OSError, sqlite3.DatabaseError) as exc:
            return ReservationResult(AccountingDisposition.UNAVAILABLE, None,
                Money.zero(request.scope.currency), request.quote.maximum_charge,
                request.cap, f"deployment effect authority unavailable: {exc}")

    def record_review_admission_refusal(
        self, request: ReservationRequest,
    ) -> ReviewChildAdmissionRefusal | None:
        """Record a final cap trip only while the planned child is still eligible.

        The earlier exhausted result is advisory. This transaction repeats both
        authority and every cap check after reconciliation and races with reserve
        under SQLite's write lock.
        """
        child = request.review_child
        if child is None:
            raise ValueError("review refusal requires a planned child")
        self.integrity_check()
        with deployment_effect_authority(
            self.path, request.workload_repository,
            deployment_role(request.workload_repository, request.owner_id), blocking=False,
        ):
            parent_identity = self._review_parent_identity(request)
            with self.transaction() as db:
                key = _review_child_key(child)
                fingerprint = _review_request_fingerprint(request)
                prior = db.execute(
                    "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-refusal' "
                    "AND scope=? AND key=?",
                    (request.workload_repository, key),
                ).fetchone()
                if prior is not None:
                    refusal = decode_workflow(prior["payload_json"], ReviewChildAdmissionRefusal)
                    recorded = load_review_preflight_record(
                        db, request.workload_repository, request.owner_id, child.round.round_id,
                    )
                    limits = (BudgetLimit(request.scope, request.cap), *request.additional_limits)
                    matching_limit = any(
                        (refusal.limit_repository, refusal.limit_dimension, refusal.limit_period_id,
                         refusal.limit_period_start, refusal.limit_period_end, refusal.limit_period_timezone,
                         refusal.currency, refusal.cap_micros) == (
                             limit.scope.repository, limit.scope.dimension, limit.scope.period_id,
                             limit.scope.period_start, limit.scope.period_end, str(limit.scope.period_timezone),
                             limit.scope.currency, limit.cap.micros,
                         ) for limit in limits
                    )
                    workflow_row = db.execute(
                        "SELECT generation,version FROM workflows WHERE repository=? AND target_kind=? AND target_number=?",
                        workflow_scope(child.round.binding.rejection.fence.key),
                    ).fetchone()
                    if (refusal.child != child or refusal.owner_id != request.owner_id
                            or refusal.request_fingerprint != fingerprint
                            or refusal.run_id != str(request.run_id)
                            or refusal.reservation_id != str(request.reservation_id)
                            or refusal.receipt_id != _review_refusal_receipt_id(refusal)
                            or refusal.requested_micros != request.quote.maximum_charge.micros
                            or not matching_limit or recorded is None
                            or refusal.claim_occurrence_id != recorded[0].claim_occurrence_id
                            or workflow_row is None
                            or (workflow_row["generation"] == refusal.refused_revision.fence.generation
                                and workflow_row["version"] < refusal.refused_revision.version)):
                        raise WorkflowAuthorityError("review child has a conflicting refusal receipt")
                    return refusal
                if _indexed_review_child_admission(
                    db, request.workload_repository, child,
                ) is not None or db.execute(
                    "SELECT 1 FROM runs WHERE review_round_id=? AND review_child_id=?",
                    (child.round.round_id, child.child_id),
                ).fetchone() or db.execute(
                    "SELECT 1 FROM runs WHERE run_id=?", (str(request.run_id),),
                ).fetchone():
                    raise WorkflowAuthorityError("review child already has a paid admission")
                self._assert_owner_active(db, request.owner_id)
                snapshot = self._validate_workflow_reservation(
                    db, request, review_parent_identity=parent_identity,
                )
                if not isinstance(snapshot, ReviewWorkflowSnapshot):
                    raise WorkflowAuthorityError("review refusal requires active execution authority")
                recorded = load_review_preflight_record(
                    db, request.workload_repository, request.owner_id, child.round.round_id,
                )
                if recorded is None:
                    raise WorkflowAuthorityError("review refusal lost its preflight receipt")
                requested = request.quote.maximum_charge
                for limit in (BudgetLimit(request.scope, request.cap), *request.additional_limits):
                    settled, reserved = self._balance(db, limit.scope)
                    committed = settled + reserved
                    if committed + requested.micros <= limit.cap.micros:
                        continue
                    revision = WorkflowRevision(snapshot.revision.fence, snapshot.revision.version + 1)
                    now = self._now()
                    refusal = ReviewChildAdmissionRefusal(
                        child, "pending", str(request.run_id), str(request.reservation_id),
                        recorded[0].claim_occurrence_id, revision, request.owner_id, fingerprint,
                        limit.scope.repository, limit.scope.dimension, limit.scope.period_id,
                        limit.scope.period_start, limit.scope.period_end, str(limit.scope.period_timezone),
                        limit.scope.currency, committed, requested.micros, limit.cap.micros, now,
                    )
                    refusal = replace(refusal, receipt_id=_review_refusal_receipt_id(refusal))
                    changed = db.execute(
                        "UPDATE workflows SET version=? WHERE repository=? AND target_kind=? AND target_number=? "
                        "AND generation=? AND version=?",
                        (revision.version, *workflow_scope(revision.fence.key), revision.fence.generation,
                         snapshot.revision.version),
                    ).rowcount
                    if changed != 1:
                        raise WorkflowAuthorityError("review refusal lost the aggregate version")
                    db.execute(
                        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                        "VALUES ('review-execution-refusal',?,?,?,?)",
                        (request.workload_repository, key, _stamp(now), encode_workflow(refusal)),
                    )
                    return refusal
                return None

    def _review_parent_identity(self, request: ReservationRequest) -> tuple[int, int] | None:
        """Probe the selected parent outside SQLite's write transaction."""
        if request.review_child is None:
            return None
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            if db.execute("SELECT 1 FROM budget_reservations WHERE run_id=?", (str(request.run_id),)).fetchone():
                return None
            row = db.execute(
                "SELECT dc.owner_pid,dc.owner_start_ticks FROM review_execution_rounds rr "
                "JOIN dispatch_claims dc ON dc.repository=rr.repository AND dc.occurrence_id=rr.claim_occurrence_id "
                "WHERE rr.round_id=? AND rr.repository=? AND dc.role='review'",
                (request.review_child.round.round_id, request.workload_repository),
            ).fetchone()
        if row is None:
            return None
        from .process_supervisor import ProcessSupervisor
        identity = (row["owner_pid"], row["owner_start_ticks"])
        return identity if ProcessSupervisor.matches_identity(*identity) else None

    @contextmanager
    def workspace_entry_authority(self, request: ReservationRequest) -> Iterator[EpisodeWorkspacePrepared | None]:
        """Keep Git verification and first admission inside the deployment fence."""
        if request.workflow is None:
            raise WorkflowAuthorityError("workspace entry requires a workflow binding")
        with ExitStack() as barriers:
            for role in ("fix", "review", "steward"):
                barriers.enter_context(deployment_effect_barrier(self.path, request.workload_repository, role))
            with self.transaction() as db:
                prepared = self._workspace_entry(db, request)
            yield prepared

    def requires_workspace_entry(self, request: ReservationRequest) -> bool:
        if request.workflow is None or request.workflow.authority.family is not WorkflowFamily.RESOLVE:
            return False
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            return self._workspace_entry(db, request) is not None

    def _workspace_entry(self, db: sqlite3.Connection, request: ReservationRequest) -> EpisodeWorkspacePrepared | None:
        self._assert_owner_active(db, request.owner_id)
        if request.workflow is None or deployment_role(request.workload_repository, request.owner_id) != "fix":
            raise WorkflowAuthorityError("workspace entry requires the bound fix owner")
        snapshot = require_resolve_binding(db, request.workflow)
        if not isinstance(snapshot.intent, CompletedRearm):
            return None
        require_workspace_predecessor(db, snapshot)
        return None if episode_entry_admitted(db, snapshot) else snapshot.intent.workspace

    def _reserve(
        self, request: ReservationRequest, *, lease: Lease | None = None,
        workspace_verified: EpisodeWorkspacePrepared | None = None,
        review_parent_identity: tuple[int, int] | None = None,
    ) -> ReservationResult:
        requested = request.quote.maximum_charge
        if request.review_child is not None and lease is not None:
            raise ValueError("planned review admission cannot register a process lease")
        if lease is not None and (
            lease.run_id != request.run_id
            or lease.dispatch_id != request.dispatch_id
        ):
            raise ValueError("reservation and lease identities must match")
        limits = (
            BudgetLimit(request.scope, request.cap),
            *request.additional_limits,
        )
        try:
            self.integrity_check()
            with self.transaction() as db, self.mutation_savepoint(db):
                self._assert_owner_active(db, request.owner_id)
                prior = db.execute(
                    "SELECT * FROM budget_reservations WHERE run_id=?",
                    (str(request.run_id),),
                ).fetchone()
                if prior is not None:
                    identical = self._reservation_request_matches(
                        db,
                        prior,
                        request,
                        limits,
                        lease,
                    )
                    disposition = (
                        AccountingDisposition.AVAILABLE
                        if identical
                        else AccountingDisposition.CONFLICT
                    )
                    settled, reserved = self._balance(db, request.scope)
                    return ReservationResult(
                        disposition,
                        ReservationId(prior["reservation_id"]),
                        Money(request.scope.currency, settled + reserved),
                        requested,
                        request.cap,
                        "idempotent reservation" if identical else "conflicting reservation",
                    )
                workflow_snapshot = self._validate_workflow_reservation(
                    db, request, workspace_verified=workspace_verified,
                    review_parent_identity=review_parent_identity,
                )
                continuation_updates, continuation_receipts = self._prepare_continuation_admissions(db, request)
                committed = Money.zero(request.scope.currency)
                for limit in limits:
                    settled, reserved = self._balance(db, limit.scope)
                    limit_committed = Money(
                        limit.scope.currency, settled + reserved
                    )
                    if limit.scope == request.scope:
                        committed = limit_committed
                    if limit_committed.micros + requested.micros > limit.cap.micros:
                        return ReservationResult(
                            AccountingDisposition.EXHAUSTED,
                            None,
                            limit_committed,
                            requested,
                            limit.cap,
                            "settled + active reservations + requested maximum "
                            f"exceeds {limit.scope.dimension} cap",
                            limit.scope.dimension,
                        )
                now = _stamp(self._now())
                if isinstance(workflow_snapshot, ReviewWorkflowSnapshot):
                    self._record_review_admission(db, request, workflow_snapshot, now)
                elif workflow_snapshot is not None:
                    self._record_workflow_admission(db, request, workflow_snapshot, now)
                from .workflow_rows import persist_workflow_snapshot
                for aggregate in continuation_updates:
                    persist_workflow_snapshot(db, aggregate)
                for purchase, admission in continuation_receipts:
                    db.execute(
                        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('continuation-admission',?,?,?,?)",
                        (request.workload_repository, self._continuation_receipt_key(request.run_id, purchase.capture.binding.authority.family),
                         now, encode_workflow((purchase, admission))),
                    )
                db.execute(
                    "INSERT INTO runs(run_id, dispatch_id, kind, owner_id, state, "
                    "config_version, workflow_binding_json, workflow_episode_key,review_round_id,review_child_id,continuations_json) "
                    "VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)",
                    (
                        str(request.run_id),
                        request.dispatch_id,
                        request.kind,
                        request.owner_id,
                        request.config_version,
                        encode_workflow(request.workflow) if request.workflow is not None else None,
                        workflow_episode_key(request.workflow) if request.workflow is not None else None,
                        request.review_child.round.round_id if request.review_child is not None else None,
                        request.review_child.child_id if request.review_child is not None else None,
                        encode_workflow(request.continuations),
                    ),
                )
                db.execute(
                    "INSERT INTO budget_reservations("
                    "reservation_id, run_id, repository, workload_repository, "
                    "dimension, period_id, period_start, period_end, "
                    "period_timezone, currency, "
                    "maximum_micros, cap_micros, model, rate_card_id, "
                    "rate_card_version, quote_effective_at, config_version, "
                    "state, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, 'active', ?, ?)",
                    (
                        str(request.reservation_id),
                        str(request.run_id),
                        request.scope.repository,
                        request.workload_repository,
                        request.scope.dimension,
                        request.scope.period_id,
                        _stamp(request.scope.period_start),
                        _stamp(request.scope.period_end),
                        str(request.scope.period_timezone),
                        request.scope.currency,
                        requested.micros,
                        request.cap.micros,
                        request.quote.model,
                        request.quote.rate_card_id,
                        request.quote.rate_card_version,
                        _stamp(request.quote.effective_at),
                        request.config_version,
                        now,
                        now,
                    ),
                )
                db.execute(
                    "INSERT INTO reservation_recovery("
                    "run_id, policy, reason, updated_at"
                    ") VALUES (?, 'spawn_pending', NULL, ?)",
                    (str(request.run_id), now),
                )
                if request.review_child is not None:
                    recorded = load_review_preflight_record(
                        db, request.workload_repository, request.owner_id,
                        request.review_child.round.round_id,
                    )
                    if recorded is None:
                        raise WorkflowAuthorityError("review admission lost its preflight receipt")
                    selection = recorded[0]
                    target = (f"issue:{selection.claim_target}" if selection.claim_target > 0
                              else f"pr:{-selection.claim_target}")
                    db.execute(
                        "INSERT INTO dispatch_claim_runs(repository,role,target,run_id,bound_at) "
                        "VALUES (?,'review',?,?,?)",
                        (request.workload_repository, target, str(request.run_id), now),
                    )
                for limit in limits:
                    db.execute(
                        "INSERT INTO reservation_limits("
                        "run_id, repository, dimension, period_id, period_start, "
                        "period_end, period_timezone, currency, cap_micros"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(request.run_id),
                            limit.scope.repository,
                            limit.scope.dimension,
                            limit.scope.period_id,
                            _stamp(limit.scope.period_start),
                            _stamp(limit.scope.period_end),
                            str(limit.scope.period_timezone),
                            limit.scope.currency,
                            limit.cap.micros,
                        ),
                    )
                if lease is not None:
                    db.execute(
                        "INSERT INTO leases(run_id, dispatch_id, owner_id, pid, "
                        "pgid, process_start_ticks, started_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(lease.run_id),
                            lease.dispatch_id,
                            lease.owner_id,
                            lease.pid,
                            lease.pgid,
                            lease.process_start_ticks,
                            _stamp(lease.started_at),
                        ),
                    )
                    db.execute(
                        "UPDATE runs SET state='running' WHERE run_id=?",
                        (str(lease.run_id),),
                    )
                    db.execute(
                        "UPDATE reservation_recovery SET policy='running', "
                        "reason=NULL, updated_at=? WHERE run_id=?",
                        (now, str(lease.run_id)),
                    )
                    self._transition(
                        db,
                        "lease",
                        str(lease.run_id),
                        "active",
                        {
                            "dispatch_id": lease.dispatch_id,
                            "owner_id": lease.owner_id,
                            "pid": lease.pid,
                            "pgid": lease.pgid,
                            "process_start_ticks": lease.process_start_ticks,
                        },
                        correlation=Correlation(
                            dispatch_id=lease.dispatch_id,
                            run_id=str(lease.run_id),
                        ),
                    )
                self._transition(
                    db,
                    "reservation",
                    str(request.run_id),
                    "active",
                    {
                        "reservation_id": str(request.reservation_id),
                        "maximum_micros": requested.micros,
                        "currency": requested.currency,
                        "rate_card_id": request.quote.rate_card_id,
                        "rate_card_version": request.quote.rate_card_version,
                        "config_version": request.config_version,
                    },
                    correlation=Correlation(
                        dispatch_id=request.dispatch_id,
                        run_id=str(request.run_id),
                    ),
                )
                return ReservationResult(
                    AccountingDisposition.AVAILABLE,
                    request.reservation_id,
                    committed + requested,
                    requested,
                    request.cap,
                )
        except CorruptControlStore as exc:
            return ReservationResult(
                AccountingDisposition.CORRUPT,
                None,
                Money.zero(request.scope.currency),
                requested,
                request.cap,
                str(exc),
            )
        except RetiredOwnerError as exc:
            return ReservationResult(
                AccountingDisposition.UNAVAILABLE,
                None,
                Money.zero(request.scope.currency),
                requested,
                request.cap,
                str(exc),
            )
        except (sqlite3.DatabaseError, OSError, WorkflowAuthorityError, WorkflowEncodingError) as exc:
            return ReservationResult(
                AccountingDisposition.UNAVAILABLE,
                None,
                Money.zero(request.scope.currency),
                requested,
                request.cap,
                str(exc),
            )

    @staticmethod
    def _continuation_receipt_key(run_id: RunId, family: WorkflowFamily) -> str:
        return hashlib.sha256(str(run_id).encode()).hexdigest() + ":" + family.value

    @staticmethod
    def _require_issue_reply_projection(db: sqlite3.Connection, key: WorkflowKey, marker: str, *, allow_unresolved: bool = False) -> None:
        if not marker:
            return
        receipt_key = hashlib.sha256(encode_workflow((key, marker)).encode()).hexdigest()
        receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='issue-reply-noop' AND scope=? AND key=? AND typeof(payload_json)='text' AND length(CAST(payload_json AS BLOB))<=262144", (key.repository, receipt_key)).fetchone()
        row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=? AND typeof(payload_json)='text' AND length(CAST(payload_json AS BLOB))<=262144", (receipt_key,)).fetchone()
        if receipt is None and row is None:
            return
        if receipt is None or row is None:
            raise WorkflowAuthorityError("blank reply lost its selected consumption or projection source")
        request = decode_workflow(receipt[0], tuple)
        command = _command_from_row(row)
        if (len(request) != 4 or not isinstance(request[0], WorkflowBinding)
                or request[0].fence.key != key or request[1] != marker
                or command.workflow != request[0] or command.kind is not CommandKind.RELABEL
                or command.repository != key.repository or command.target != f"issue:{key.target.number}"
                or command.expected_revision is not None or command.config_version != ""
                or request[2] != command.executor_identity
                or request[3] != json.dumps(dict(command.payload), sort_keys=True, separators=(",", ":"))
                or command.idempotency_key != "issue-reply-noop:" + receipt_key):
            raise WorkflowAuthorityError("blank reply projection disagrees with its selected source")
        if row["state"] not in ("succeeded", "permanent_failure"):
            if allow_unresolved:
                return
            raise WorkflowAuthorityError("blank reply re-block projection is still unresolved")
        effect = db.execute("SELECT * FROM effect_receipts WHERE command_id=?", (receipt_key,)).fetchone()
        if effect is None or effect["status"] != row["state"] or effect["executor_identity"] != row["attempt_owner"]:
            raise WorkflowAuthorityError("blank reply lost its selected terminal projection effect")
        settled = EffectReceipt(command.command_id, effect["executor_identity"], _parse_stamp(effect["started_at"]),
                                _parse_stamp(effect["completed_at"]), EffectStatus(effect["status"]),
                                effect["remote_object_id"], effect["remote_revision"], effect["error"])
        if _receipt_digest(settled) != effect["receipt_digest"]:
            raise WorkflowAuthorityError("blank reply terminal effect disagrees with its receipt")

    def _require_continuation_launch(self, db: sqlite3.Connection, run_id: RunId) -> None:
        from .workflow_rows import load_issue_aggregate
        run = db.execute("SELECT r.continuations_json,r.workflow_binding_json,b.workload_repository FROM runs r "
                         "LEFT JOIN budget_reservations b USING(run_id) WHERE r.run_id=?", (str(run_id),)).fetchone()
        if run is None:
            return
        purchases = decode_workflow(run["continuations_json"], tuple)
        if (len(purchases) > 3 or any(not isinstance(item, (cc.IssueRecoveryPurchase, cc.IssueReplyPurchase, cc.IssueCIPurchase, cc.IssueReviewPurchase, cc.PRRepairPurchase, cc.StewardPurchase)) for item in purchases)
                or len({item.capture.binding.authority.family for item in purchases}) != len(purchases)):
            raise WorkflowAuthorityError("run continuation selections are malformed")
        if run["workflow_binding_json"] is not None:
            principal = decode_workflow(run["workflow_binding_json"], WorkflowBinding)
            if (principal.authority.family in (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR, WorkflowFamily.STEWARD)
                    and not any(item.capture.binding == principal for item in purchases)):
                raise WorkflowAuthorityError("continuation run lacks its selected principal")
        for purchase in purchases:
            family = purchase.capture.binding.authority.family
            receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-admission' AND scope=? AND key=?",
                                 (run["workload_repository"], self._continuation_receipt_key(run_id, family))).fetchone()
            if receipt is None:
                raise WorkflowAuthorityError("continuation launch lacks its selected admission receipt")
            captured = decode_workflow(receipt[0], tuple)
            if (len(captured) != 2 or captured[0] != purchase or not isinstance(captured[1], ContinuationAdmission)
                    or captured[1].attempt != WorkflowAttemptIdentity(purchase.capture.binding, run_id, purchase.capture.leg_id)
                    or captured[1].admission_version != purchase.capture.revision.version + 1):
                raise WorkflowAuthorityError("continuation launch lost its exact run admission")
            admission = captured[1]
            key = admission.attempt.binding.fence.key
            aggregate = load_review_workflow_row(db, key) if family is WorkflowFamily.PR_REPAIR else load_issue_aggregate(db, key)
            selection = (cc.select_pr_repair(aggregate) if family is WorkflowFamily.PR_REPAIR else
                         cc.select_issue_recovery(aggregate) if family is WorkflowFamily.ISSUE_RECOVERY else cc.select_steward(aggregate)) if aggregate is not None else None
            if (not isinstance(selection, (cc.SelectedIssueRecovery, cc.SelectedPRRepair, cc.SelectedSteward))
                    or getattr(aggregate, "gates", ()) or selection.component.gates
                    or not (cc.ci_return_is_current(selection, admission) if isinstance(purchase, cc.IssueCIPurchase)
                            and isinstance(selection, cc.SelectedIssueRecovery) else cc.review_return_is_current(selection, admission)
                            if isinstance(purchase, cc.IssueReviewPurchase) and isinstance(selection, cc.SelectedIssueRecovery)
                            else cc.continuation_return_is_current(selection, admission))):
                raise WorkflowAuthorityError("continuation launch admission is stale or held")

    def reserve_issue_recovery(self, request: ReservationRequest, purchase: cc.IssueRecoveryPurchase | cc.IssueReplyPurchase) -> ReservationResult:
        if not isinstance(purchase, (cc.IssueRecoveryPurchase, cc.IssueReplyPurchase)):
            raise ValueError("issue recovery reservation requires its typed purchase")
        return self.reserve(replace(request, continuations=(purchase,)))

    def reserve_pr_repair(self, request: ReservationRequest, purchase: cc.PRRepairPurchase) -> ReservationResult:
        if not isinstance(purchase, cc.PRRepairPurchase):
            raise ValueError("PR repair reservation requires its typed purchase")
        return self.reserve(replace(request, continuations=(purchase,)))

    def reserve_steward(self, request: ReservationRequest, purchase: cc.StewardPurchase) -> ReservationResult:
        if not isinstance(purchase, cc.StewardPurchase):
            raise ValueError("steward reservation requires its typed purchase")
        return self.reserve(replace(request, continuations=(purchase,)))

    def _prepare_continuation_admissions(self, db: sqlite3.Connection, request: ReservationRequest) -> tuple[tuple, tuple]:
        from .workflow_rows import load_issue_aggregate
        updates, receipts = {}, []
        role = deployment_role(request.workload_repository, request.owner_id) if request.continuations else None
        if request.kind == "ADDRESS_REVIEW" and (len(request.continuations) != 2
                or sum(isinstance(item, cc.PRRepairPurchase) and bool(item.branch) for item in request.continuations) != 1
                or sum(isinstance(item, cc.IssueReviewPurchase) for item in request.continuations) != 1):
            raise WorkflowAuthorityError("address-review admission requires its selected PR and issue allowance")
        for repair in (item for item in request.continuations if isinstance(item, cc.PRRepairPurchase) and item.branch):
            reviews = [item for item in request.continuations if isinstance(item, cc.IssueReviewPurchase)]
            if (request.kind not in {"FIX_CI", "ADDRESS_REVIEW"}
                    or request.kind == "ADDRESS_REVIEW" and (len(reviews) != 1 or not repair.advance_resume
                        or repair.allowance is not cc.RepairAllowance.REVIEW)
                    or request.kind == "FIX_CI" and (reviews or repair.advance_resume or repair.allowance is not cc.RepairAllowance.CI)):
                raise WorkflowAuthorityError("repair admission lacks its fixed selected lane and issue allowance")
        for purchase in request.continuations:
            capture = purchase.capture
            key = capture.binding.fence.key
            if isinstance(purchase, cc.IssueCIPurchase):
                dispatch = json.loads(request.dispatch_id)
                repairs = [item for item in request.continuations if isinstance(item, cc.PRRepairPurchase)]
                if (request.kind != "FIX_CI" or dispatch.get("repository") != key.repository
                        or dispatch.get("role") != "fix" or dispatch.get("issue") != key.target.number
                        or dispatch.get("number") != purchase.pr.number or dispatch.get("ci_head") != purchase.head
                        or dispatch.get("leg_id") != capture.leg_id or len(repairs) != 1
                        or repairs[0].capture.binding.fence.key != WorkflowKey(key.repository, purchase.pr)):
                    raise WorkflowAuthorityError("CI allowance differs from its exact issue/PR/head context")
            if isinstance(purchase, cc.IssueReviewPurchase):
                dispatch = json.loads(request.dispatch_id)
                repairs = [item for item in request.continuations if isinstance(item, cc.PRRepairPurchase)]
                if (request.kind != "ADDRESS_REVIEW" or dispatch.get("kind") != "ADDRESS_REVIEW"
                        or dispatch.get("repository") != key.repository
                        or dispatch.get("role") != "fix" or type(dispatch.get("issue")) is not int
                        or dispatch.get("issue") != key.target.number or type(dispatch.get("number")) is not int
                        or dispatch.get("number") != purchase.pr.number
                        or dispatch.get("review_input") != encode_workflow((purchase.head, purchase.review_at))
                        or dispatch.get("leg_id") != capture.leg_id or len(repairs) != 1
                        or not repairs[0].advance_resume or repairs[0].allowance is not cc.RepairAllowance.REVIEW
                        or request.workflow != repairs[0].capture.binding
                        or repairs[0].capture.binding.fence.key != WorkflowKey(key.repository, purchase.pr)):
                    raise WorkflowAuthorityError("review allowance differs from its exact issue/PR/head/timestamp context")
            if isinstance(purchase, cc.StewardPurchase):
                self._require_steward_dispatch(request.dispatch_id, request.kind, purchase)
            if isinstance(purchase, cc.IssueReplyPurchase) or (
                    isinstance(purchase, cc.IssueRecoveryPurchase) and request.workflow is not None
                    and request.workflow.authority.family is WorkflowFamily.RESOLVE
                    and request.kind in {"RESUME", "AUTHOR", "IMPLEMENT", "ADJUDICATE"}):
                try:
                    if not isinstance(request.dispatch_id, str) or len(request.dispatch_id.encode()) > MAX_WORKFLOW_PAYLOAD_BYTES:
                        raise ValueError("issue reply dispatch exceeds its bound")
                    dispatch = json.loads(request.dispatch_id)
                    if not isinstance(dispatch, dict) or not isinstance(dispatch.get("issue_reply"), str):
                        raise ValueError("issue reply dispatch lacks its selected input")
                    inputs = decode_workflow(dispatch["issue_reply"], tuple)
                except (ValueError, TypeError) as exc:
                    raise WorkflowAuthorityError("issue reply lacks its exact bounded dispatch input") from exc
                if (len(inputs) != 3 or inputs[0] != purchase.reply_at or not isinstance(inputs[1], str)
                        or not inputs[1].strip() or len(inputs[1]) > 65536 or "\x00" in inputs[1]
                        or dispatch.get("repository") != key.repository or dispatch.get("role") != "fix"
                        or dispatch.get("kind") != request.kind or type(dispatch.get("issue")) is not int
                        or dispatch["issue"] != key.target.number or dispatch.get("leg_id") != capture.leg_id):
                    raise WorkflowAuthorityError("issue reply disagrees with its selected timestamp/body/leg context")
                if isinstance(purchase, cc.IssueReplyPurchase):
                    if (request.kind != "PR_RESUME" or request.workflow is not None
                            or type(dispatch.get("number")) is not int or not 0 < dispatch["number"] < 2**63
                            or type(inputs[2]) is not int or inputs[2] != dispatch["number"]):
                        raise WorkflowAuthorityError("linked PR issue reply disagrees with its actual PR context")
                elif inputs[2] != purchase.branch or dispatch.get("number") != key.target.number or issue_from_branch(purchase.branch) != key.target.number:
                    raise WorkflowAuthorityError("issue reply disagrees with its selected issue branch")
            aggregate = (load_review_workflow_row(db, key) if isinstance(purchase, cc.PRRepairPurchase)
                         else load_issue_aggregate(db, key))
            if aggregate is None or aggregate.revision != capture.revision:
                raise WorkflowAuthorityError("captured continuation aggregate is absent or stale")
            if getattr(aggregate, "gates", ()):
                raise WorkflowAuthorityError("continuation aggregate has unresolved primary gates")
            expected_role = "steward" if isinstance(purchase, cc.StewardPurchase) else "fix"
            if role != expected_role:
                raise WorkflowAuthorityError("continuation admission belongs to another deployment role")
            version = aggregate.revision.version + 1
            after = updates.get(key, aggregate)
            try:
                if isinstance(purchase, (cc.IssueRecoveryPurchase, cc.IssueReplyPurchase, cc.IssueCIPurchase, cc.IssueReviewPurchase)):
                    selection = cc.select_issue_recovery(aggregate)
                    if not isinstance(selection, cc.SelectedIssueRecovery):
                        raise ValueError("issue recovery is inactive or migration blocked")
                    self._require_issue_reply_projection(db, key, selection.component.state.consumed_reply_at)
                    after = replace(after, issue_recovery=(cc.consume_issue_review(selection, purchase, ContinuationAdmission(
                        WorkflowAttemptIdentity(capture.binding, request.run_id, capture.leg_id), version)) if isinstance(purchase, cc.IssueReviewPurchase)
                        else cc.consume_issue_ci(selection, purchase, ContinuationAdmission(
                        WorkflowAttemptIdentity(capture.binding, request.run_id, capture.leg_id), version)) if isinstance(purchase, cc.IssueCIPurchase)
                        else cc.consume_issue_recovery(selection, purchase, version) if isinstance(purchase, cc.IssueRecoveryPurchase) else cc.consume_issue_reply(selection, purchase, version)),
                                    revision=WorkflowRevision(aggregate.revision.fence, version))
                elif isinstance(purchase, cc.PRRepairPurchase):
                    selection = cc.select_pr_repair(aggregate)
                    if not isinstance(selection, cc.SelectedPRRepair):
                        raise ValueError("PR repair is inactive or migration blocked")
                    if purchase.branch:
                        self._validate_pr_repair_sources(db, selection)
                    if purchase.handoff is not None:
                        from .workflow_control import SQLiteWorkflowControl
                        from ..domain.workflow_identity import WorkflowWriter
                        dispatch = json.loads(request.dispatch_id)
                        source_issue = purchase.handoff.source.binding.fence.key.target.number
                        if dispatch.get("issue") != source_issue:
                            raise WorkflowAuthorityError("repair handoff differs from actual linked issue")
                        bound = SQLiteWorkflowControl(self, WorkflowWriter(key.repository, "fix", request.owner_id), resolve_ceiling=0)
                        verified = bound._pr_repair_handoff(db, source_issue, key.target, purchase.branch, purchase.handoff.runtime)
                        if verified != purchase.handoff:
                            raise WorkflowAuthorityError("repair handoff changed before paid admission")
                    after = replace(after, pr_repair=cc.consume_pr_repair(selection, purchase, version),
                                    revision=WorkflowRevision(aggregate.revision.fence, version))
                else:
                    selection = cc.select_steward(aggregate)
                    if not isinstance(selection, cc.SelectedSteward):
                        raise ValueError("steward is inactive or migration blocked")
                    after = replace(after, steward=cc.consume_steward(selection, purchase, version),
                                    revision=WorkflowRevision(aggregate.revision.fence, version))
            except (ValueError, TypeError) as exc:
                raise WorkflowAuthorityError(str(exc)) from exc
            updates[key] = after
            receipts.append((purchase, ContinuationAdmission(
                WorkflowAttemptIdentity(capture.binding, request.run_id, capture.leg_id), version)))
        return tuple(updates.values()), tuple(receipts)

    def _validate_workflow_reservation(
        self, db: sqlite3.Connection, request: ReservationRequest, *,
        workspace_verified: EpisodeWorkspacePrepared | None = None,
        review_parent_identity: tuple[int, int] | None = None,
    ) -> WorkflowSnapshot | ReviewWorkflowSnapshot | None:
        binding = request.workflow
        if binding is not None and binding.authority.family in (
            WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR, WorkflowFamily.STEWARD,
        ):
            if not any(item.capture.binding == binding for item in request.continuations):
                raise WorkflowAuthorityError("secondary workflow admission requires its captured consumption")
            return None
        families = {family.value for family in active_families(db, request.workload_repository)}
        if binding is None and not families & {
            WorkflowFamily.RESOLVE.value, WorkflowFamily.REVIEW_REJECTIONS.value,
            WorkflowFamily.REVIEW_EXECUTION.value,
        }:
            return
        try:
            role = deployment_role(request.workload_repository, request.owner_id)
        except ValueError as exc:
            raise WorkflowAuthorityError(str(exc)) from exc
        if binding is None:
            if role == "fix" and request.kind in _RESOLVE_PAID_KINDS and WorkflowFamily.RESOLVE.value in families:
                raise WorkflowAuthorityError("activated resolve admission requires a captured workflow binding")
            # Once review rejection is active every paid review is bound, so
            # no unbound review run can overlap a re-arm (ADR 0015 quiescence).
            if (role == "review" and request.kind in _REVIEW_PAID_KINDS
                    and families & {WorkflowFamily.REVIEW_REJECTIONS.value,
                                    WorkflowFamily.REVIEW_EXECUTION.value}):
                raise WorkflowAuthorityError("activated review admission requires a captured workflow binding")
            return
        if binding.authority.family is WorkflowFamily.REVIEW_REJECTIONS:
            if role != "review" or request.kind not in _REVIEW_PAID_KINDS:
                raise WorkflowAuthorityError("review rejection admission requires the review owner's review leg")
            try:
                snapshot = require_review_binding(db, binding)
                dispatch = json.loads(request.dispatch_id)
            except (ValueError, TypeError) as exc:
                raise WorkflowAuthorityError(str(exc)) from exc
            if (not isinstance(dispatch, dict) or dispatch.get("repository") != binding.fence.key.repository
                    or type(dispatch.get("number")) is not int or dispatch["number"] != binding.fence.key.target.number):
                raise WorkflowAuthorityError("paid review identity disagrees with its workflow")
            issue = dispatch.get("issue")
            if issue is not None:
                if type(issue) is not int or not 0 < issue < 2**63:
                    raise WorkflowAuthorityError("paid review linked issue identity is malformed")
                holds = linked_issue_workflow_holds(db, WorkflowKey(binding.fence.key.repository, IssueRef(issue)))
                if holds:
                    raise WorkflowAuthorityError(f"linked issue #{issue}: {'; '.join(holds)}")
            if isinstance(snapshot.execution, CurrentReviewExecution):
                if request.review_child is None:
                    raise WorkflowAuthorityError("active review execution requires a planned child")
                if _indexed_review_child_admission(
                    db, request.workload_repository, request.review_child,
                ) is not None:
                    raise WorkflowAuthorityError("review child already has a paid admission")
                self._require_review_child_authority(
                    db, request.workload_repository, request.owner_id, request.review_child,
                    request.kind, dispatch, snapshot, review_parent_identity,
                )
                return snapshot
            if request.review_child is not None:
                raise WorkflowAuthorityError("review execution child belongs to an inactive component")
            # The run row records the captured binding (and so its generation);
            # this transaction proved it current and ungated. Review has no
            # ordered continuation to receipt, unlike resolve.
            return None
        if role != "fix":
            raise WorkflowAuthorityError("resolve admission requires the repository's fix owner")
        try:
            snapshot = require_resolve_binding(db, binding)
            if isinstance(snapshot.issue_recovery, cc.ActiveIssueRecovery):
                self._require_issue_reply_projection(db, binding.fence.key, snapshot.issue_recovery.state.consumed_reply_at)
            dispatch = json.loads(request.dispatch_id)
            if not isinstance(dispatch, dict) or dispatch.get("repository") != binding.fence.key.repository or type(dispatch.get("issue")) is not int or dispatch["issue"] != binding.fence.key.target.number:
                raise WorkflowAuthorityError("paid dispatch identity disagrees with its workflow")
            stage = ResolveStage(request.kind)
            if isinstance(snapshot.intent, CompletedRearm) and not episode_entry_admitted(db, snapshot):
                if workspace_verified != snapshot.intent.workspace:
                    raise WorkflowAuthorityError("first episode admission requires fresh workspace verification")
                if stage not in (ResolveStage.ROUTE, ResolveStage.RESOLVE, ResolveStage.AUTHOR):
                    raise WorkflowAuthorityError("first episode admission must start fresh work")
            if dispatch.get("kind") != stage.value or not isinstance(dispatch.get("leg_id"), str) or not dispatch["leg_id"]:
                raise WorkflowAuthorityError("resolve admission requires its captured stage and leg identity")
            fingerprint = dispatch.get("input_fingerprint")
            if stage is ResolveStage.AUTHOR and fingerprint is None:
                raise WorkflowAuthorityError("author admission requires its supplied input fingerprint")
            _validate_split_admission(db, snapshot, stage, dispatch, request.quote.model)
            if stage is ResolveStage.ADJUDICATE:
                if request.adjudication_ceiling is None:
                    raise WorkflowAuthorityError("adjudication admission requires a configured contract-round ceiling")
                validate_contract_rounds(request.adjudication_ceiling)
                admitted = admitted_adjudications(db, binding)
                if not adjudication_allowed(admitted, request.adjudication_ceiling):
                    raise WorkflowAuthorityError("resolve episode's admitted adjudication allowance is exhausted")
            return snapshot
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError(str(exc)) from exc

    def _require_review_child_authority(
        self, db: sqlite3.Connection, repository: str, owner_id: str,
        child: ReviewChildIdentity, kind: str, dispatch: dict[str, object],
        snapshot: ReviewWorkflowSnapshot, parent_identity: tuple[int, int] | None,
    ) -> ReviewPreflightSelection:
        if parent_identity is None or not isinstance(snapshot.execution, CurrentReviewExecution):
            raise WorkflowAuthorityError("review child admission lacks its live parent or execution authority")
        key = _review_child_key(child)
        if db.execute(
            "SELECT 1 FROM dedupe_keys WHERE namespace='review-execution-refusal' AND scope=? AND key=?",
            (repository, key),
        ).fetchone():
            raise WorkflowAuthorityError("review child has a final admission refusal")
        recorded = load_review_preflight_record(
            db, repository, owner_id, child.round.round_id,
        )
        if recorded is None:
            raise WorkflowAuthorityError("review child admission lacks its exact preflight receipt")
        selection, _, plan, row = recorded
        planning = decode_workflow(
            row["planning_json"], ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
        )
        if (child.round.binding != snapshot.execution.binding or child not in plan.children
                or row["closure_json"] is not None or row["outcome_json"] is not None):
            raise WorkflowAuthorityError("review child is unplanned, stale or finalized")
        sealed_at = verified_review_completed_seal(
            db, repository, owner_id, selection.claim_occurrence_id, plan, planning,
        )
        if isinstance(planning, ReviewPlanningSealed) and not isinstance(planning.conclusion, ReviewPlanningComplete):
            raise WorkflowAuthorityError("review parent stopped planning and fenced child admission")
        if not isinstance(planning, ReviewPlanningOpen) and (
                sealed_at is None or sealed_at.version > snapshot.revision.version):
            raise WorkflowAuthorityError("review child planning lacks an applicable completed seal")
        try:
            require_review_execution_eligibility(snapshot, plan.head_sha, plan.failure_ceiling)
        except ValueError as exc:
            raise WorkflowAuthorityError(str(exc)) from exc
        from .review_execution_control import SQLiteReviewExecutionControl
        recovery_hold = SQLiteReviewExecutionControl._pending_recovery_hold_in_tx(db, snapshot.execution)
        if recovery_hold is not None:
            raise WorkflowAuthorityError(recovery_hold)
        if (dispatch.get("repository") != repository or dispatch.get("role") != "review"
                or dispatch.get("runtime") != plan.runtime.value
                or type(dispatch.get("number")) is not int
                or dispatch.get("number") != child.round.binding.rejection.fence.key.target.number
                or type(dispatch.get("claim_target")) is not int
                or dispatch.get("kind") != kind or dispatch.get("claim_target") != selection.claim_target
                or not isinstance(dispatch.get("leg_id"), str) or not dispatch["leg_id"]
                or (selection.claim_target > 0 and dispatch.get("issue") != selection.claim_target)
                or (selection.claim_target < 0 and dispatch.get("issue") is not None)):
            raise WorkflowAuthorityError("review child dispatch identity disagrees with its preflight")
        require_review_parent_claim(
            db, repository, owner_id, selection.claim_occurrence_id,
            selection.claim_target, probed_identity=parent_identity,
        )
        from .review_dispatch_holds import review_release_projection, unresolved_review_submissions
        from ..domain.review_rearm import ReleaseStatus
        from ..domain.workflow_identity import WorkflowWriter
        issue = dispatch.get("issue")
        if issue is not None and (type(issue) is not int or not 0 < issue < 2**63):
            raise WorkflowAuthorityError("review child linked issue identity is malformed")
        if issue is not None and linked_issue_workflow_holds(db, WorkflowKey(repository, IssueRef(issue))):
            raise WorkflowAuthorityError("review child admission is held by linked issue workflow")
        if unresolved_review_submissions(db, self, snapshot.binding):
            raise WorkflowAuthorityError("review child admission is held by unresolved submission")
        release = review_release_projection(
            db, WorkflowWriter(repository, "review", owner_id), snapshot,
        )
        if release is not None and release.status is not ReleaseStatus.VERIFIED:
            raise WorkflowAuthorityError("review child admission is held by release projection")
        return selection

    @staticmethod
    def _record_workflow_admission(
        db: sqlite3.Connection, request: ReservationRequest, snapshot: WorkflowSnapshot, now: str,
    ) -> None:
        """Admission order is a durable revision, independent of clocks and delivery."""
        if request.workflow is None or request.workflow.authority.family is not WorkflowFamily.RESOLVE or snapshot.binding != request.workflow:
            raise WorkflowAuthorityError("only a resolve binding records a resolve admission receipt")
        try:
            metadata = json.loads(request.dispatch_id)
            revision = WorkflowRevision(snapshot.revision.fence, snapshot.revision.version + 1)
            admission = ResolveAdmission(
                WorkflowAttemptIdentity(snapshot.binding, request.run_id, metadata["leg_id"]),
                revision, ResolveStage(request.kind), request.adjudication_ceiling, metadata.get("input_fingerprint"), metadata.get("split_fingerprint"),
                snapshot.intent.handoff.handoff_id if isinstance(snapshot.intent, CompletedRearm) else None,
            )
            db.execute(
                "UPDATE workflows SET version=? WHERE repository=? AND target_kind=? AND target_number=?",
                (revision.version, *workflow_scope(revision.fence.key)),
            )
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-admission',?,?,?,?)",
                (request.workload_repository, str(request.run_id), now, encode_workflow(admission)),
            )
            indexes = []
            if admission.stage is not ResolveStage.ROUTE:
                indexes.append(("workflow-work-latest", f"issue:{snapshot.binding.fence.key.target.number}"))
            if _split_admission_input(metadata) is not None:
                indexes.append(("workflow-split-consumed", _split_consumption_key(snapshot, admission.stage, _split_admission_input(metadata))))
            for namespace, key in indexes:
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(namespace,scope,key) DO UPDATE SET claimed_at=excluded.claimed_at,payload_json=excluded.payload_json",
                    (namespace, request.workload_repository, key, now, encode_workflow(admission)),
                )
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowAuthorityError(f"invalid workflow admission: {exc}") from exc

    @staticmethod
    def _record_review_admission(
        db: sqlite3.Connection, request: ReservationRequest,
        snapshot: ReviewWorkflowSnapshot, now: str,
    ) -> None:
        child = request.review_child
        if child is None or not isinstance(snapshot.execution, CurrentReviewExecution):
            raise WorkflowAuthorityError("review admission requires an active planned child")
        recorded = load_review_preflight_record(
            db, request.workload_repository, request.owner_id, child.round.round_id,
        )
        if recorded is None:
            raise WorkflowAuthorityError("review admission lost its preflight receipt")
        revision = WorkflowRevision(snapshot.revision.fence, snapshot.revision.version + 1)
        admission = ReviewChildAdmission(
            child, str(request.run_id), str(request.reservation_id),
            recorded[0].claim_occurrence_id, revision, request.owner_id,
        )
        changed = db.execute(
            "UPDATE workflows SET version=? WHERE repository=? AND target_kind=? AND target_number=? "
            "AND generation=? AND version=?",
            (revision.version, *workflow_scope(revision.fence.key), revision.fence.generation,
             snapshot.revision.version),
        ).rowcount
        if changed != 1:
            raise WorkflowAuthorityError("review admission lost the aggregate version")
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-execution-admission',?,?,?,?)",
            (request.workload_repository, str(request.run_id), now, encode_workflow(admission)),
        )
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-execution-child-admission',?,?,?,?)",
            (request.workload_repository, _review_child_key(child), now, encode_workflow(admission)),
        )

    @staticmethod
    def _reservation_request_matches(
        db: sqlite3.Connection,
        prior: sqlite3.Row,
        request: ReservationRequest,
        limits: tuple[BudgetLimit, ...],
        lease: Lease | None,
    ) -> bool:
        """Match the complete authority, not a convenient subset of its IDs."""

        run = db.execute(
            "SELECT dispatch_id, kind, owner_id, state, config_version, workflow_binding_json, "
            "workflow_episode_key,review_round_id,review_child_id,continuations_json "
            "FROM runs WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
        if run is None:
            return False
        if run["continuations_json"] != encode_workflow(request.continuations):
            return False
        selected = {item.capture.binding.authority.family: item for item in request.continuations}
        for family in (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR, WorkflowFamily.STEWARD):
            receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-admission' AND scope=? AND key=?",
                (request.workload_repository, SQLiteControlStore._continuation_receipt_key(request.run_id, family))).fetchone()
            purchase = selected.get(family)
            if (receipt is None) != (purchase is None):
                return False
            if receipt is not None:
                captured = decode_workflow(receipt["payload_json"], tuple)
                if (len(captured) != 2 or captured[0] != purchase or not isinstance(captured[1], ContinuationAdmission)
                        or captured[1].attempt != WorkflowAttemptIdentity(purchase.capture.binding, request.run_id, purchase.capture.leg_id)
                        or captured[1].admission_version != purchase.capture.revision.version + 1):
                    return False
        if request.workflow is not None and request.workflow.authority.family is WorkflowFamily.REVIEW_REJECTIONS:
            if (run["workflow_episode_key"] != workflow_episode_key(request.workflow)
                    or run["workflow_binding_json"] != encode_workflow(request.workflow)):
                return False
            receipt = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-admission' "
                "AND scope=? AND key=?",
                (request.workload_repository, str(request.run_id)),
            ).fetchone()
            if request.review_child is None:
                if receipt is not None or run["review_round_id"] is not None or run["review_child_id"] is not None:
                    return False
            else:
                if (receipt is None or run["review_round_id"] != request.review_child.round.round_id
                        or run["review_child_id"] != request.review_child.child_id):
                    return False
                admission = decode_workflow(receipt["payload_json"], ReviewChildAdmission)
                if _indexed_review_child_admission(
                    db, request.workload_repository, request.review_child,
                ) != admission:
                    return False
                recorded = load_review_preflight_record(
                    db, request.workload_repository, request.owner_id,
                    request.review_child.round.round_id,
                )
                current = db.execute(
                    "SELECT generation,version FROM workflows WHERE repository=? AND target_kind='pr' AND target_number=?",
                    (request.workload_repository, request.workflow.fence.key.target.number),
                ).fetchone()
                if (recorded is None or current is None
                        or current["generation"] != admission.admitted_revision.fence.generation
                        or admission.admitted_revision.version > current["version"]
                        or admission.child != request.review_child
                        or admission.run_id != str(request.run_id)
                        or admission.reservation_id != str(request.reservation_id)
                        or admission.claim_occurrence_id != recorded[0].claim_occurrence_id
                        or admission.owner_id != request.owner_id
                        or admission.admitted_revision.fence != request.workflow.fence):
                    return False
        elif request.workflow is not None and request.workflow.authority.family is WorkflowFamily.RESOLVE:
            if run["workflow_episode_key"] != workflow_episode_key(request.workflow):
                return False
            receipt = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND scope=? AND key=?",
                (request.workload_repository, str(request.run_id)),
            ).fetchone()
            if receipt is None:
                return False
            try:
                admission = decode_workflow(receipt["payload_json"], ResolveAdmission)
                metadata = json.loads(request.dispatch_id)
                expected_attempt = WorkflowAttemptIdentity(request.workflow, request.run_id, metadata["leg_id"])
                current = load_resolve_workflow_row(db, request.workflow.fence.key)
            except (ValueError, TypeError, KeyError) as exc:
                raise WorkflowAuthorityError("invalid admitted workflow receipt") from exc
            if current is None or admission.attempt != expected_attempt or admission.stage.value != request.kind or admission.adjudication_ceiling != request.adjudication_ceiling or admission.input_fingerprint != metadata.get("input_fingerprint") or admission.split_fingerprint != metadata.get("split_fingerprint") or admission.revision.version > current.revision.version:
                return False
        actual_reservation = tuple(
            prior[name]
            for name in (
                "reservation_id",
                "repository",
                "workload_repository",
                "dimension",
                "period_id",
                "period_start",
                "period_end",
                "period_timezone",
                "currency",
                "maximum_micros",
                "cap_micros",
                "model",
                "rate_card_id",
                "rate_card_version",
                "quote_effective_at",
                "config_version",
                "state",
            )
        )
        expected_reservation = (
            str(request.reservation_id),
            request.scope.repository,
            request.workload_repository,
            request.scope.dimension,
            request.scope.period_id,
            _stamp(request.scope.period_start),
            _stamp(request.scope.period_end),
            str(request.scope.period_timezone),
            request.scope.currency,
            request.quote.maximum_charge.micros,
            request.cap.micros,
            request.quote.model,
            request.quote.rate_card_id,
            request.quote.rate_card_version,
            _stamp(request.quote.effective_at),
            request.config_version,
            "active",
        )
        actual_run = tuple(
            run[name]
            for name in (
                "dispatch_id",
                "kind",
                "owner_id",
                "state",
                "config_version",
                "workflow_binding_json",
                "review_round_id",
                "review_child_id",
            )
        )
        expected_run = (
            request.dispatch_id,
            request.kind,
            request.owner_id,
            "running" if lease is not None else "reserved",
            request.config_version,
            encode_workflow(request.workflow) if request.workflow is not None else None,
            request.review_child.round.round_id if request.review_child is not None else None,
            request.review_child.child_id if request.review_child is not None else None,
        )
        actual_limits = [
            tuple(row)
            for row in db.execute(
                "SELECT repository, dimension, period_id, period_start, "
                "period_end, period_timezone, currency, cap_micros "
                "FROM reservation_limits WHERE run_id=? "
                "ORDER BY repository, dimension, period_id, currency",
                (str(request.run_id),),
            ).fetchall()
        ]
        expected_limits = sorted(
            (
                limit.scope.repository,
                limit.scope.dimension,
                limit.scope.period_id,
                _stamp(limit.scope.period_start),
                _stamp(limit.scope.period_end),
                str(limit.scope.period_timezone),
                limit.scope.currency,
                limit.cap.micros,
            )
            for limit in limits
        )
        persisted_lease = db.execute(
            "SELECT dispatch_id, owner_id, pid, pgid, process_start_ticks, "
            "started_at FROM leases WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
        expected_lease = (
            None
            if lease is None
            else (
                lease.dispatch_id,
                lease.owner_id,
                lease.pid,
                lease.pgid,
                lease.process_start_ticks,
                _stamp(lease.started_at),
            )
        )
        actual_lease = (
            None if persisted_lease is None else tuple(persisted_lease)
        )
        return (
            actual_reservation == expected_reservation
            and actual_run == expected_run
            and actual_limits == expected_limits
            and actual_lease == expected_lease
        )

    def settle(self, request: SettlementRequest) -> SettlementResult:
        with self.transaction() as db:
            return self._settle(db, request)

    def settle_issue_recovery(self, request: SettlementRequest, admission: ContinuationAdmission,
                              state: cc.IssueRecoveryState) -> SettlementResult:
        if not isinstance(state, cc.IssueRecoveryState):
            raise ValueError("issue recovery return requires typed state")
        return self._settle_continuation(request, admission, state, WorkflowFamily.ISSUE_RECOVERY)

    def settle_pr_repair(self, request: SettlementRequest, admission: ContinuationAdmission,
                         state: cc.PRRepairState, *, writer=None) -> SettlementResult:
        if not isinstance(state, cc.PRRepairState):
            raise ValueError("PR repair return requires typed state")
        if state.session is not None and state.session.source != admission:
            raise ValueError("PR repair return session requires its exact admission")
        return self._settle_continuation(request, admission, state, WorkflowFamily.PR_REPAIR, writer=writer)

    def pr_repair_admission(self, attempt: WorkflowAttemptIdentity) -> ContinuationAdmission:
        with self.transaction() as db:
            return self._pr_repair_admission(db, attempt)

    def ci_repair_admission(self, attempt: WorkflowAttemptIdentity) -> ContinuationAdmission:
        with self.transaction() as db:
            return self._ci_repair_admission(db, attempt)

    def review_repair_admission(self, attempt: WorkflowAttemptIdentity) -> ContinuationAdmission:
        with self.transaction() as db:
            return self._review_repair_admission(db, attempt)

    def _review_repair_admission(self, db, attempt: WorkflowAttemptIdentity) -> ContinuationAdmission:
        run = db.execute("SELECT continuations_json FROM runs WHERE run_id=?", (str(attempt.run_id),)).fetchone()
        purchases = decode_workflow(run[0], tuple) if run is not None else ()
        selected = [item for item in purchases if isinstance(item, cc.IssueReviewPurchase)]
        if len(selected) != 1:
            raise WorkflowAuthorityError("review output lacks its exact selected issue purchase")
        purchase = selected[0]
        key = purchase.capture.binding.fence.key
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-admission' AND scope=? AND key=?",
            (key.repository, self._continuation_receipt_key(attempt.run_id, WorkflowFamily.ISSUE_RECOVERY))).fetchone()
        if raw is None:
            raise WorkflowAuthorityError("review output lacks its selected issue admission receipt")
        admission = decode_workflow(raw[0], tuple)[1]
        expected = WorkflowAttemptIdentity(purchase.capture.binding, attempt.run_id, attempt.leg_id)
        if not isinstance(admission, ContinuationAdmission) or admission.attempt != expected:
            raise WorkflowAuthorityError("review output conflicts with its selected issue attempt")
        self._continuation_return_admission(db, admission, WorkflowFamily.ISSUE_RECOVERY)
        return admission

    def _ci_repair_admission(self, db, attempt: WorkflowAttemptIdentity) -> ContinuationAdmission:
        run = db.execute("SELECT continuations_json FROM runs WHERE run_id=?", (str(attempt.run_id),)).fetchone()
        purchases = decode_workflow(run[0], tuple) if run is not None else ()
        selected = [item for item in purchases if isinstance(item, cc.IssueCIPurchase)]
        if len(selected) != 1:
            raise WorkflowAuthorityError("CI output lacks its exact selected issue purchase")
        purchase = selected[0]
        key = purchase.capture.binding.fence.key
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-admission' AND scope=? AND key=?",
            (key.repository, self._continuation_receipt_key(attempt.run_id, WorkflowFamily.ISSUE_RECOVERY))).fetchone()
        if raw is None:
            raise WorkflowAuthorityError("CI output lacks its selected issue admission receipt")
        admission = decode_workflow(raw[0], tuple)[1]
        expected = WorkflowAttemptIdentity(purchase.capture.binding, attempt.run_id, attempt.leg_id)
        if not isinstance(admission, ContinuationAdmission) or admission.attempt != expected:
            raise WorkflowAuthorityError("CI output conflicts with its selected issue attempt")
        self._continuation_return_admission(db, admission, WorkflowFamily.ISSUE_RECOVERY)
        return admission

    def _pr_repair_admission(self, db, attempt: WorkflowAttemptIdentity) -> ContinuationAdmission:
        if attempt.binding.authority.family is not WorkflowFamily.PR_REPAIR:
            raise WorkflowAuthorityError("repair lookup requires its family attempt")
        key = attempt.binding.fence.key
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-admission' AND scope=? AND key=?",
            (key.repository, self._continuation_receipt_key(attempt.run_id, WorkflowFamily.PR_REPAIR))).fetchone()
        if raw is None:
            raise WorkflowAuthorityError("repair output lacks its selected admission receipt")
        admission = decode_workflow(raw[0], tuple)[1]
        if not isinstance(admission, ContinuationAdmission) or admission.attempt != attempt:
            raise WorkflowAuthorityError("repair output conflicts with its admitted attempt")
        self._continuation_return_admission(db, admission, WorkflowFamily.PR_REPAIR)
        return admission
    def capture_pr_repair_result(self, returned: cc.PRRepairResult, writer) -> None:
        if not isinstance(returned, cc.PRRepairResult):
            raise ValueError("repair capture requires typed returned evidence")
        admission = returned.admission
        key = admission.attempt.binding.fence.key
        receipt_key = self._continuation_receipt_key(admission.attempt.run_id, WorkflowFamily.PR_REPAIR)
        encoded = encode_workflow(returned)
        with self.transaction() as db:
            purchase, run = self._continuation_return_admission(db, admission, WorkflowFamily.PR_REPAIR)
            self._require_repair_writer(db, writer, admission, run["owner_id"])
            if run["reservation_state"] == "released":
                raise WorkflowAuthorityError("released repair reservation cannot produce paid output")
            dispatch = json.loads(run['dispatch_id'])
            ci_purchases = [item for item in decode_workflow(run["continuations_json"], tuple) if isinstance(item, cc.IssueCIPurchase)]
            if bool(ci_purchases) != (returned.ci is not None) or returned.ci is not None and returned.ci.admission != self._ci_repair_admission(db, admission.attempt):
                raise WorkflowAuthorityError("CI result differs from its selected issue admission")
            review_purchases = [item for item in decode_workflow(run["continuations_json"], tuple) if isinstance(item, cc.IssueReviewPurchase)]
            if bool(review_purchases) != (returned.review is not None) or returned.review is not None and returned.review.admission != self._review_repair_admission(db, admission.attempt):
                raise WorkflowAuthorityError("review result differs from its selected issue admission")
            if (not isinstance(purchase, cc.PRRepairPurchase) or not purchase.branch
                    or returned.state.branch != purchase.branch
                    or dispatch.get('kind') != ('ADDRESS_REVIEW' if returned.review is not None else 'FIX_CI') or dispatch.get('number') != key.target.number
                    or dispatch.get('runtime') != returned.state.actual_model.runtime.value):
                raise WorkflowAuthorityError("repair output changed its admitted branch, lane or runtime")
            prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result' AND scope=? AND key=?",
                (key.repository, receipt_key)).fetchone()
            if prior is not None:
                if prior[0] != encoded:
                    raise WorkflowAuthorityError("repair result conflicts with its immutable receipt")
                return
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('pr-repair-result',?,?,?,?)",
                (key.repository, receipt_key, _stamp(self._now()), encoded))

    def settle_pr_repair_result(self, returned: cc.PRRepairResult, writer) -> SettlementResult:
        # Capture survives an interrupted money/continuity transaction. Restart
        # uses this exact output and charge, never an in-memory provider envelope.
        self.capture_pr_repair_result(returned, writer)
        return self.settle_pr_repair(returned.settlement, returned.admission, returned.state, writer=writer)

    def reconcile_pr_repair_returns(self, writer) -> int:
        with self.transaction() as db:
            self._assert_owner_active(db, writer.owner_id)
            lineage = self._owner_lineage(db, writer.repository, writer.owner_id)
            owners = ",".join("?" for _ in lineage)
            rows = db.execute("SELECT raw.payload_json FROM dedupe_keys raw JOIN runs r "
                "ON r.run_id=json_extract(raw.payload_json,'$.value.fields.admission.fields.attempt.fields.run_id') "
                "LEFT JOIN dedupe_keys done ON done.namespace='continuation-return' AND done.scope=raw.scope AND done.key=raw.key "
                f"WHERE raw.namespace='pr-repair-result' AND raw.scope=? AND r.owner_id IN ({owners}) AND done.key IS NULL "
                "ORDER BY raw.claimed_at,raw.key LIMIT 32", (writer.repository, *lineage)).fetchall()
        completed = 0
        for row in rows:
            returned = decode_workflow(row[0], cc.PRRepairResult)
            settled = self.settle_pr_repair_result(returned, writer)
            completed += int(settled.disposition is AccountingDisposition.AVAILABLE)
        return completed

    def settle_steward(self, request: SettlementRequest, admission: ContinuationAdmission,
                       state: cc.StewardState) -> SettlementResult:
        if not isinstance(state, cc.StewardState):
            raise ValueError("steward return requires typed state")
        return self._settle_continuation(request, admission, state, WorkflowFamily.STEWARD)

    def read_steward(self, key: WorkflowKey) -> cc.StewardSelection:
        from .workflow_rows import load_issue_aggregate
        with self.transaction() as db:
            aggregate = load_issue_aggregate(db, key)
            return cc.select_steward(aggregate) if aggregate is not None else cc.InactiveSteward()

    def _validate_pr_repair_sources(self, db, selected: cc.SelectedPRRepair) -> None:
        state = selected.component.state
        proofs: dict[ContinuationAdmission, cc.PRRepairState] = {}
        for reference, field in ((state.session, "session"), (state.actual_model, "actual_model")):
            if reference is None:
                continue
            source = reference.source
            if source not in proofs:
                self._continuation_return_admission(db, source, WorkflowFamily.PR_REPAIR)
                row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                    (selected.binding.fence.key.repository, self._continuation_receipt_key(source.attempt.run_id, WorkflowFamily.PR_REPAIR))).fetchone()
                payload = decode_workflow(row[0], tuple) if row is not None else ()
                if len(payload) != 2 or payload[0] != source or not isinstance(payload[1], cc.PRRepairState):
                    raise WorkflowAuthorityError("selected repair continuity lost its exact applied output receipt")
                proofs[source] = payload[1]
            if getattr(proofs[source], field) != reference:
                raise WorkflowAuthorityError("selected repair continuity lost its exact applied output receipt")

    def read_pr_repair(self, key: WorkflowKey) -> cc.PRRepairSelection:
        from .workflow_rows import load_review_workflow_row
        with self.transaction() as db:
            aggregate = load_review_workflow_row(db, key)
            selected = cc.select_pr_repair(aggregate) if aggregate is not None else cc.InactivePRRepair()
            if isinstance(selected, cc.SelectedPRRepair):
                self._validate_pr_repair_sources(db, selected)
            return selected

    def read_issue_recovery(self, key: WorkflowKey) -> cc.IssueRecoverySelection:
        from .workflow_rows import load_issue_aggregate
        with self.transaction() as db:
            aggregate = load_issue_aggregate(db, key)
            selected = cc.select_issue_recovery(aggregate) if aggregate is not None else cc.InactiveIssueRecovery()
            if isinstance(selected, cc.SelectedIssueRecovery) and selected.component.state.pending_ci is not None:
                pending = selected.component.state.pending_ci
                purchase, _ = self._continuation_return_admission(db, pending, WorkflowFamily.ISSUE_RECOVERY)
                if not isinstance(purchase, cc.IssueCIPurchase) or pending.attempt.binding.fence.generation > selected.revision.fence.generation:
                    raise WorkflowAuthorityError("pending CI lacks its selected admitted provenance")
            if isinstance(selected, cc.SelectedIssueRecovery) and selected.component.state.review_failures.pending is not None:
                pending = selected.component.state.review_failures.pending
                purchase, _ = self._continuation_return_admission(db, pending, WorkflowFamily.ISSUE_RECOVERY)
                if (not isinstance(purchase, cc.IssueReviewPurchase)
                        or pending.attempt.binding.fence.generation > selected.revision.fence.generation
                        or purchase.review_at != selected.component.state.review_failures.review_at
                        or purchase.pr != selected.component.state.review_failures.pr):
                    raise WorkflowAuthorityError("pending review lacks its selected admitted provenance")
            return selected

    def reset_ci_episode(self, source: cc.CIEpisodeReset, writer) -> bool:
        from .workflow_rows import load_issue_aggregate, persist_workflow_snapshot
        if not isinstance(source, cc.CIEpisodeReset):
            raise ValueError("CI reset requires its exact green observation")
        key = source.binding.fence.key
        encoded = encode_workflow(source)
        receipt_key = hashlib.sha256(encoded.encode()).hexdigest()
        with self.transaction() as db, self.mutation_savepoint(db):
            if writer.repository != key.repository or writer.role != "fix":
                raise WorkflowAuthorityError("CI reset belongs to another deployment")
            self._assert_owner_active(db, writer.owner_id)
            prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='ci-episode-reset' AND scope=? AND key=?",
                               (key.repository, receipt_key)).fetchone()
            if prior is not None:
                if prior[0] != encoded:
                    raise WorkflowAuthorityError("CI reset conflicts with its original observation")
                return False
            aggregate = load_issue_aggregate(db, key)
            selected = cc.select_issue_recovery(aggregate) if aggregate is not None else None
            pr = load_review_workflow_row(db, source.pr_fence.key)
            if (not isinstance(selected, cc.SelectedIssueRecovery) or selected.binding != source.binding
                    or aggregate.revision != source.revision or pr is None or pr.revision.fence != source.pr_fence):
                raise WorkflowAuthorityError("CI reset observation is stale or unavailable")
            component = selected.component
            if component.state.pending_ci is not None and component.state.pending_ci.attempt.binding == selected.binding:
                raise WorkflowAuthorityError("CI reset waits for the selected admitted attempt")
            if not component.state.ci_failures:
                raise WorkflowAuthorityError("CI reset has no spent allowance")
            after = replace(aggregate, revision=WorkflowRevision(aggregate.revision.fence, aggregate.revision.version+1),
                issue_recovery=replace(component, state=replace(component.state, ci_failures=0)))
            persist_workflow_snapshot(db, after)
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('ci-episode-reset',?,?,?,?)",
                       (key.repository, receipt_key, _stamp(self._now()), encoded))
            return True

    def steward_admission(self, attempt: WorkflowAttemptIdentity) -> cc.StewardAttemptSource:
        with self.transaction() as db:
            return self._steward_admission(db, attempt)

    @staticmethod
    def _require_steward_dispatch(dispatch_id: str, kind: str, purchase: cc.StewardPurchase) -> None:
        try:
            if not isinstance(dispatch_id, str) or len(dispatch_id.encode()) > MAX_WORKFLOW_PAYLOAD_BYTES:
                raise ValueError("steward dispatch exceeds its bound")
            dispatch = json.loads(dispatch_id)
            key = purchase.capture.binding.fence.key
            if (not isinstance(dispatch, dict) or dispatch.get("repository") != key.repository
                    or dispatch.get("role") != "steward" or dispatch.get("kind") != kind
                    or kind != purchase.lane.value.upper() or type(dispatch.get("issue")) is not int
                    or dispatch["issue"] != key.target.number or type(dispatch.get("number")) is not int
                    or dispatch["number"] != key.target.number or dispatch.get("leg_id") != purchase.capture.leg_id):
                raise ValueError("steward dispatch changed its selected issue, lane or leg")
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("steward admission lacks its exact bounded dispatch identity") from exc

    def _steward_admission(self, db: sqlite3.Connection, attempt: WorkflowAttemptIdentity) -> cc.StewardAttemptSource:
        if not isinstance(attempt, WorkflowAttemptIdentity) or attempt.binding.authority.family is not WorkflowFamily.STEWARD:
            raise WorkflowAuthorityError("steward return requires its actual family attempt")
        key = attempt.binding.fence.key
        receipt_key = self._continuation_receipt_key(attempt.run_id, WorkflowFamily.STEWARD)
        row = db.execute(
            "SELECT d.payload_json,r.continuations_json,r.owner_id,r.kind,r.workflow_binding_json,r.dispatch_id,b.state "
            "FROM runs r JOIN budget_reservations b USING(run_id) JOIN dedupe_keys d "
            "ON d.namespace='continuation-admission' AND d.scope=b.workload_repository AND d.key=? "
            "WHERE r.run_id=? AND b.workload_repository=?",
            (receipt_key, str(attempt.run_id), key.repository),
        ).fetchone()
        if row is None:
            raise WorkflowAuthorityError("steward return lost its selected immutable run admission")
        captured = decode_workflow(row["payload_json"], tuple)
        if (len(captured) != 2 or not isinstance(captured[0], cc.StewardPurchase)
                or not isinstance(captured[1], ContinuationAdmission) or captured[1].attempt != attempt
                or captured[0].capture.binding != attempt.binding or captured[0].capture.leg_id != attempt.leg_id
                or captured[1].admission_version != captured[0].capture.revision.version + 1
                or captured[0] not in decode_workflow(row["continuations_json"], tuple)
                or row["workflow_binding_json"] != encode_workflow(attempt.binding)
                or deployment_role(key.repository, row["owner_id"]) != "steward"
                or row["kind"] != captured[0].lane.value.upper()):
            raise WorkflowAuthorityError("steward return disagrees with its selected lane and run")
        self._require_steward_dispatch(row["dispatch_id"], row["kind"], captured[0])
        if row["state"] != "settled" or db.execute("SELECT 1 FROM charges WHERE run_id=?", (str(attempt.run_id),)).fetchone() is None:
            raise WorkflowAuthorityError("steward return requires retained actual money settlement")
        return cc.StewardAttemptSource(captured[1], captured[0].lane, captured[0].baseline_at,
                                      captured[0].content_signature)

    def record_steward_outcome(self, outcome: cc.StewardReturned,
                               commands: tuple[LifecycleCommand, ...] = ()) -> bool:
        from .workflow_rows import load_issue_aggregate, persist_workflow_snapshot
        if not isinstance(outcome, (cc.StewardFailed, cc.StewardVerdict, cc.StewardStopped, cc.GroomNoFinding)):
            raise ValueError("steward outcome must be a fixed classified return")
        source = outcome.source
        key = source.admission.attempt.binding.fence.key
        receipt_key = self._continuation_receipt_key(source.admission.attempt.run_id, WorkflowFamily.STEWARD)
        if (not isinstance(commands, tuple) or any(not isinstance(command, LifecycleCommand) for command in commands)
                or (tuple(str(command.command_id) for command in commands) != outcome.commands
                    if isinstance(outcome, (cc.StewardVerdict, cc.StewardStopped)) else bool(commands))):
            raise ValueError("steward outcome requires its exact bounded command set")
        encoded = encode_workflow((source.admission, outcome))
        with self.transaction() as db, self.mutation_savepoint(db):
            if self._steward_admission(db, source.admission.attempt) != source:
                raise WorkflowAuthorityError("steward outcome changed its admitted input")
            prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                               (key.repository, receipt_key)).fetchone()
            if prior is not None:
                if prior[0] != encoded:
                    raise WorkflowAuthorityError("steward outcome conflicts with its immutable result")
                for command in commands:
                    row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command.command_id),)).fetchone()
                    if row is None:
                        raise WorkflowAuthorityError("steward retry lost its exact verdict command")
                    stored = _command_from_row(row)
                    from .workflow_commit import owns_command
                    from ..domain.workflow_identity import WorkflowWriter
                    if (stored != replace(command, executor_identity=stored.executor_identity)
                            or not owns_command(db, WorkflowWriter(key.repository, "steward", stored.executor_identity),
                                                command.executor_identity)):
                        raise WorkflowAuthorityError("steward retry changed its exact verdict command")
                return False
            aggregate = load_issue_aggregate(db, key)
            selection = cc.select_steward(aggregate) if aggregate is not None else cc.InactiveSteward()
            state = cc.steward_return_state(selection, outcome) if isinstance(selection, cc.SelectedSteward) else None
            if state is not None:
                component = replace(selection.component, state=state,
                                    watermarks=replace(selection.component.watermarks, last_applied=source.admission.admission_version))
                after = replace(aggregate, revision=WorkflowRevision(aggregate.revision.fence, aggregate.revision.version + 1), steward=component)
                persist_workflow_snapshot(db, after)
                for command in commands:
                    self._enqueue_command(db, command, registering_steward_verdict=True)
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('continuation-return',?,?,?,?)",
                       (key.repository, receipt_key, _stamp(self._now()), encoded))
            event = ControlTransitionCommitted("steward_return", receipt_key, "applied" if state is not None else "stale", {})
            if self._event_factory is None:
                raise WorkflowAuthorityError("steward return event sink is unbound")
            self.enqueue_event(self._event_factory(event, Correlation(issue=key.target.number, run_id=str(source.admission.attempt.run_id))), db)
            return state is not None

    def _steward_effect(self, db: sqlite3.Connection, command_id: str) -> EffectStatus | None:
        row = db.execute("SELECT c.state,c.attempt_owner,r.* FROM lifecycle_commands c LEFT JOIN effect_receipts r USING(command_id) "
                         "WHERE c.command_id=?", (command_id,)).fetchone()
        if row is None:
            raise WorkflowAuthorityError("steward verdict lost an owed command")
        if row["status"] is None:
            if row["state"] in ("succeeded", "permanent_failure", "unknown_outcome", "retryable_failure"):
                raise WorkflowAuthorityError("steward verdict lost its selected effect receipt")
            return None
        if row["status"] != row["state"] or row["executor_identity"] != row["attempt_owner"]:
            raise WorkflowAuthorityError("steward verdict lost its exact terminal effect")
        receipt = EffectReceipt(CommandId(command_id), row["executor_identity"], _parse_stamp(row["started_at"]),
                                _parse_stamp(row["completed_at"]), EffectStatus(row["status"]),
                                row["remote_object_id"], row["remote_revision"], row["error"])
        if _receipt_digest(receipt) != row["receipt_digest"]:
            raise WorkflowAuthorityError("steward verdict effect receipt conflicts with its source")
        return receipt.status if receipt.status in (EffectStatus.SUCCEEDED, EffectStatus.PERMANENT_FAILURE) else None

    def _require_steward_command_source(self, db: sqlite3.Connection, command: LifecycleCommand) -> cc.StewardCommandGuard:
        guard = decode_workflow(command.payload.get("steward_guard"), cc.StewardCommandGuard)
        if self._steward_admission(db, guard.source.admission.attempt) != guard.source:
            raise WorkflowAuthorityError("steward command lost its immutable selected source")
        self._steward_effect(db, str(command.command_id))
        return guard

    def _validate_steward_command(self, db: sqlite3.Connection, command: LifecycleCommand, *, registering: bool = False,
                                  settling: bool = False, refusing: bool = False) -> tuple[LifecycleCommand, ...]:
        from .workflow_rows import load_issue_aggregate
        from .workflow_commit import owns_command
        from ..domain.workflow_identity import WorkflowWriter
        guard = decode_workflow(command.payload.get("steward_guard"), cc.StewardCommandGuard)
        source = guard.source
        key = source.admission.attempt.binding.fence.key
        if (command.workflow is not None or command.repository != key.repository
                or command.target != f"issue:{key.target.number}"
                or command.kind not in {CommandKind.POST_COMMENT, CommandKind.RELABEL, CommandKind.CLOSE_ISSUE, CommandKind.ADD_DEPENDENCY}
                or deployment_role(key.repository, command.executor_identity) != "steward"
                or command.idempotency_key != f"steward-verdict:{source.admission.attempt.run_id}:{guard.position}"):
            raise WorkflowAuthorityError("steward verdict command disagrees with its fixed family source")
        if not settling:
            self._assert_owner_active(db, command.executor_identity)
        owner = db.execute("SELECT owner_id FROM runs WHERE run_id=?", (str(source.admission.attempt.run_id),)).fetchone()
        if owner is None or not owns_command(db, WorkflowWriter(key.repository, "steward", command.executor_identity), owner[0]):
            raise WorkflowAuthorityError("steward command does not belong to its admitted owner")
        if self._steward_admission(db, source.admission.attempt) != source:
            raise WorkflowAuthorityError("steward command changed its selected run input")
        aggregate = load_issue_aggregate(db, key)
        selection = cc.select_steward(aggregate) if aggregate is not None else cc.InactiveSteward()
        if not isinstance(selection, cc.SelectedSteward) or selection.binding != source.admission.attempt.binding:
            raise WorkflowAuthorityError("steward command belongs to an older component or generation")
        if selection.component.gates and not settling and not registering:
            raise WorkflowAuthorityError("steward command has unresolved component holds")
        lane = selection.component.state.triage if source.lane is cc.StewardLane.TRIAGE else selection.component.state.groom
        if (lane.delivery_source != source or guard.position >= len(lane.pending_delivery)
                or lane.pending_delivery[guard.position] != str(command.command_id)
                or selection.component.watermarks.last_admitted != source.admission.admission_version
                or selection.component.watermarks.last_applied != source.admission.admission_version):
            raise WorkflowAuthorityError("steward command lost its exact owed verdict")
        prefix = []
        for position, command_id in enumerate(lane.pending_delivery[:guard.position]):
            row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (command_id,)).fetchone()
            if row is None:
                raise WorkflowAuthorityError("steward verdict lost a predecessor command")
            prior = _command_from_row(row)
            prior_guard = decode_workflow(prior.payload.get("steward_guard"), cc.StewardCommandGuard)
            if prior_guard != replace(guard, position=position):
                raise WorkflowAuthorityError("steward verdict predecessor changed its exact facts")
            if not registering:
                status = self._steward_effect(db, command_id)
                if status is not EffectStatus.SUCCEEDED and not (refusing and status is EffectStatus.PERMANENT_FAILURE):
                    raise WorkflowAuthorityError("steward verdict awaits its exact predecessor delivery")
            prefix.append(prior)
        return tuple(prefix)

    def steward_command_predecessors(self, command: LifecycleCommand) -> tuple[LifecycleCommand, ...]:
        with self.transaction() as db:
            return self._validate_steward_command(db, command)

    def steward_command_is_stopped(self, command: LifecycleCommand) -> bool:
        with self.transaction() as db:
            guard = self._require_steward_command_source(db, command)
            source = guard.source
            key = source.admission.attempt.binding.fence.key
            row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                (key.repository, self._continuation_receipt_key(source.admission.attempt.run_id, WorkflowFamily.STEWARD))).fetchone()
            result = decode_workflow(row[0], tuple) if row is not None else ()
            return len(result) == 2 and isinstance(result[1], cc.StewardStopped) and result[1].source == source

    def complete_steward_delivery(self, source: cc.StewardAttemptSource, observed) -> bool:
        from .workflow_rows import load_issue_aggregate, persist_workflow_snapshot
        from .effects import action_from_command
        from ..domain.actions import PostComment, Relabel, CloseIssue
        from ..domain.world import IssueView
        if not isinstance(observed, IssueView) or observed.number != source.admission.attempt.binding.fence.key.target.number:
            raise ValueError("steward delivery requires its selected observed issue")
        key = source.admission.attempt.binding.fence.key
        with self.transaction() as db, self.mutation_savepoint(db):
            if self._steward_admission(db, source.admission.attempt) != source:
                raise WorkflowAuthorityError("steward delivery changed its admitted source")
            aggregate = load_issue_aggregate(db, key)
            selection = cc.select_steward(aggregate) if aggregate is not None else cc.InactiveSteward()
            if not isinstance(selection, cc.SelectedSteward):
                return False
            lane = selection.component.state.triage if source.lane is cc.StewardLane.TRIAGE else selection.component.state.groom
            if lane.delivery_source != source or selection.binding != source.admission.attempt.binding:
                return False
            outcome_row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                (key.repository, self._continuation_receipt_key(source.admission.attempt.run_id, WorkflowFamily.STEWARD))).fetchone()
            outcome = decode_workflow(outcome_row[0], tuple) if outcome_row is not None else ()
            if (len(outcome) != 2 or outcome[0] != source.admission
                    or not isinstance(outcome[1], (cc.StewardVerdict, cc.StewardStopped))
                    or outcome[1].source != source or outcome[1].commands != lane.pending_delivery):
                raise WorkflowAuthorityError("steward delivery lost its immutable classified outcome")
            commands, rows, statuses = [], [], []
            for command_id in lane.pending_delivery:
                row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (command_id,)).fetchone()
                if row is None:
                    raise WorkflowAuthorityError("steward delivery lost a selected verdict command")
                command = _command_from_row(row)
                self._validate_steward_command(db, command, registering=True, settling=True)
                commands.append(command)
                rows.append(row)
                statuses.append(self._steward_effect(db, command_id))
            delivered_comments = []
            for command, status in zip(commands, statuses):
                if status is not EffectStatus.SUCCEEDED:
                    continue
                action = action_from_command(command)
                body = action.body if isinstance(action, PostComment) else action.comment if isinstance(action, (Relabel, CloseIssue)) else ""
                if not body:
                    continue
                receipt = db.execute("SELECT remote_revision FROM effect_receipts WHERE command_id=?", (str(command.command_id),)).fetchone()
                facts = decode_workflow(receipt[0], tuple) if receipt is not None and receipt[0] is not None else ()
                if len(facts) != 1 or not isinstance(facts[0], tuple) or len(facts[0]) != 3 or facts[0][1] != body:
                    raise WorkflowAuthorityError("steward delivery lost its exact comment facts")
                delivered_comments.append(facts[0])
            failed = EffectStatus.PERMANENT_FAILURE in statuses
            if failed:
                first = statuses.index(EffectStatus.PERMANENT_FAILURE)
                if any(status is not EffectStatus.SUCCEEDED for status in statuses[:first]):
                    return False
                # Ordered admission proves an untouched suffix never bought a
                # remote attempt. Executing/uncertain debt stays in recovery.
                if any(status is None and (row["state"] != "pending" or row["attempts"] != 0 or row["attempt_owner"] is not None)
                       for row, status in zip(rows[first+1:], statuses[first+1:])):
                    return False
                for command, status in zip(commands[first+1:], statuses[first+1:]):
                    if status is not None:
                        continue
                    self._validate_steward_command(db, command, settling=True, refusing=True)
                    self._mark_executing(db, command.command_id)
                    now = self._now()
                    receipt = EffectReceipt(command.command_id, command.executor_identity, now, now, EffectStatus.PERMANENT_FAILURE,
                        error="selected predecessor permanently refused before this suffix command could start")
                    self._insert_effect(db, receipt, _receipt_digest(receipt))
                guard = decode_workflow(commands[first].payload["steward_guard"], cc.StewardCommandGuard)
                failed_action = action_from_command(commands[first])
                failed_body = failed_action.body if isinstance(failed_action, PostComment) else failed_action.comment if isinstance(failed_action, (Relabel, CloseIssue)) else ""
                prefix = tuple(action_from_command(c) for c in commands[:first])
                unchanged = cc.steward_completion_matches(guard, observed, prefix, delivered_comments=tuple(delivered_comments))
                own_at = observed.updated_at if first and unchanged else None
                partial = cc.steward_completion_matches(guard, observed, prefix, partial=failed_action, delivered_comments=tuple(delivered_comments))
                if not unchanged and partial:
                    captured = db.execute("SELECT remote_revision FROM effect_receipts WHERE command_id=?", (str(commands[first].command_id),)).fetchone()
                    exact = decode_workflow(captured[0], tuple) if captured is not None and captured[0] is not None else ()
                    observed_facts = tuple((c.author,c.body,c.created_at) for c in observed.comments)
                    if (not failed_body or len(exact) != 1 or not isinstance(exact[0], tuple) or len(exact[0]) != 3
                            or exact[0][1] != failed_body or exact[0] not in observed_facts
                            or exact[0] in (*guard.facts.comments, *delivered_comments)
                            or not isinstance(exact[0][0], str) or exact[0][0].casefold() != guard.writer_login.casefold()):
                        return False
                    own_at = observed.updated_at
                state = cc.steward_stopped_state(selection, source, own_at, lost_verdict=isinstance(outcome[1], cc.StewardVerdict))
            else:
                if any(status is not EffectStatus.SUCCEEDED for status in statuses):
                    return False
                last = commands[-1]
                action = action_from_command(last)
                last_body = action.body if isinstance(action, PostComment) else action.comment if isinstance(action, (Relabel, CloseIssue)) else ""
                prefix_comments = tuple(delivered_comments[:-1] if last_body else delivered_comments)
                guard = decode_workflow(last.payload["steward_guard"], cc.StewardCommandGuard)
                unchanged = (all(fact in tuple((c.author,c.body,c.created_at) for c in observed.comments) for fact in delivered_comments)
                    and cc.steward_completion_matches(guard, observed, tuple(action_from_command(c) for c in commands[:-1]),
                        partial=action, delivered_comments=prefix_comments))
                own_at = observed.updated_at if unchanged else None
                state = (cc.steward_stopped_state(selection, source, own_at) if isinstance(outcome[1], cc.StewardStopped)
                         else cc.steward_delivered_state(selection, source, own_at or source.input_at))
            if state is None:
                return False
            after = replace(aggregate, revision=WorkflowRevision(aggregate.revision.fence, aggregate.revision.version + 1),
                            steward=replace(selection.component, state=state))
            persist_workflow_snapshot(db, after)
            event = ControlTransitionCommitted("steward_delivery", str(source.admission.attempt.run_id),
                "failed" if failed else "delivered", {"input_at": source.input_at})
            if self._event_factory is None:
                raise WorkflowAuthorityError("steward delivery event sink is unbound")
            self.enqueue_event(self._event_factory(event, Correlation(issue=key.target.number)), db)
            return True

    def _require_repair_writer(self, db, writer, admission, historical_owner):
        from .workflow_commit import owns_command
        if writer is None or writer.repository != admission.attempt.binding.fence.key.repository or writer.role != "fix":
            raise WorkflowAuthorityError("repair output requires its current bound fix writer")
        self._assert_owner_active(db, writer.owner_id)
        if not owns_command(db, writer, historical_owner):
            raise WorkflowAuthorityError("repair output belongs to another deployment owner")

    def _continuation_return_admission(self, db, admission: ContinuationAdmission, family: WorkflowFamily):
        key = admission.attempt.binding.fence.key
        receipt_key = self._continuation_receipt_key(admission.attempt.run_id, family)
        row = db.execute(
            "SELECT d.payload_json,r.owner_id,r.dispatch_id,r.continuations_json,r.workflow_binding_json,b.state AS reservation_state FROM dedupe_keys d JOIN runs r ON r.run_id=? "
            "JOIN budget_reservations b USING(run_id) WHERE d.namespace='continuation-admission' "
            "AND d.scope=? AND d.key=? AND b.workload_repository=?",
            (str(admission.attempt.run_id), key.repository, receipt_key, key.repository),
        ).fetchone()
        if row is None:
            raise WorkflowAuthorityError("continuation return lacks its immutable run admission")
        captured = decode_workflow(row["payload_json"], tuple)
        if (len(captured) != 2 or captured[1] != admission
                or not isinstance(captured[0], (cc.IssueRecoveryPurchase, cc.IssueReplyPurchase, cc.IssueCIPurchase, cc.IssueReviewPurchase, cc.PRRepairPurchase, cc.StewardPurchase))
                or captured[0].capture.binding != admission.attempt.binding
                or captured[0].capture.leg_id != admission.attempt.leg_id
                or captured[0].capture.revision.version + 1 != admission.admission_version
                or deployment_role(key.repository, row["owner_id"]) != ("steward" if family is WorkflowFamily.STEWARD else "fix")):
            raise WorkflowAuthorityError("continuation return conflicts with its exact admission")
        if captured[0] not in decode_workflow(row["continuations_json"], tuple):
            raise WorkflowAuthorityError("continuation return was never selected on its actual run")
        if row["workflow_binding_json"] is not None:
            principal = decode_workflow(row["workflow_binding_json"], WorkflowBinding)
            if (principal.fence.key == key and principal.fence != admission.attempt.binding.fence
                    or principal.authority.family is family and principal != admission.attempt.binding):
                raise WorkflowAuthorityError("continuation return conflicts with its actual run binding")
        return captured[0], row

    def _settle_continuation(self, request: SettlementRequest, admission: ContinuationAdmission,
                             state, family: WorkflowFamily, *, writer=None) -> SettlementResult:
        from .workflow_rows import load_issue_aggregate, persist_workflow_snapshot
        if (not isinstance(admission, ContinuationAdmission) or request.run_id != admission.attempt.run_id
                or admission.attempt.binding.authority.family is not family):
            raise ValueError("continuation return requires its exact run and family")
        key = admission.attempt.binding.fence.key
        receipt_key = self._continuation_receipt_key(request.run_id, family)
        with self.transaction() as db, self.mutation_savepoint(db):
            purchase, run = self._continuation_return_admission(db, admission, family)
            if isinstance(purchase, (cc.IssueCIPurchase, cc.IssueReviewPurchase)):
                raise WorkflowAuthorityError("paired issue settlement requires its captured paired repair result")
            ci = None
            review = None
            if isinstance(purchase, cc.PRRepairPurchase) and purchase.branch:
                self._require_repair_writer(db, writer, admission, run["owner_id"])
                if state.branch != purchase.branch:
                    raise WorkflowAuthorityError("repair return changed its purchased branch")
                raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-repair-result' AND scope=? AND key=?",
                    (key.repository, receipt_key)).fetchone()
                captured = decode_workflow(raw[0], cc.PRRepairResult) if raw is not None else None
                if captured is None or (captured.admission, captured.state, captured.settlement) != (admission, state, request):
                    raise WorkflowAuthorityError("repair settlement conflicts with its captured result")
                ci = captured.ci
                review = captured.review
            prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                               (key.repository, receipt_key)).fetchone()
            encoded = encode_workflow((admission, state))
            if prior is not None and prior["payload_json"] != encoded:
                raise WorkflowAuthorityError("continuation return conflicts with its immutable result")
            if ci is not None:
                selected_ci = self._ci_repair_admission(db, admission.attempt)
                if selected_ci != ci.admission:
                    raise WorkflowAuthorityError("CI settlement changed its selected issue source")
                if prior is not None:
                    paired = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                        (key.repository, self._continuation_receipt_key(request.run_id, WorkflowFamily.ISSUE_RECOVERY))).fetchone()
                    if paired is None or paired[0] != encode_workflow((ci.admission, ci)):
                        raise WorkflowAuthorityError("CI exact retry lost its selected applied result receipt")
            if review is not None:
                if self._review_repair_admission(db, admission.attempt) != review.admission:
                    raise WorkflowAuthorityError("review settlement changed its selected issue source")
                if prior is not None:
                    paired = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                        (key.repository, self._continuation_receipt_key(request.run_id, WorkflowFamily.ISSUE_RECOVERY))).fetchone()
                    if paired is None or paired[0] != encode_workflow((review.admission, review)):
                        raise WorkflowAuthorityError("review exact retry lost its selected applied result receipt")
            settled = self._settle(db, request)
            if settled.disposition is not AccountingDisposition.AVAILABLE or prior is not None:
                return settled
            if ci is not None:
                self._apply_ci_result(db, ci)
            if review is not None:
                self._apply_review_result(db, review)
            aggregate = load_review_workflow_row(db, key) if family is WorkflowFamily.PR_REPAIR else load_issue_aggregate(db, key)
            selection = (cc.select_pr_repair(aggregate) if family is WorkflowFamily.PR_REPAIR else
                         cc.select_issue_recovery(aggregate) if family is WorkflowFamily.ISSUE_RECOVERY else cc.select_steward(aggregate)) if aggregate is not None else None
            if isinstance(selection, (cc.SelectedIssueRecovery, cc.SelectedPRRepair, cc.SelectedSteward)) and cc.continuation_return_is_current(selection, admission):
                component = selection.component
                watermarks = replace(component.watermarks, last_applied=admission.admission_version)
                revision = WorkflowRevision(aggregate.revision.fence, aggregate.revision.version + 1)
                if isinstance(selection, cc.SelectedIssueRecovery):
                    from ..domain.workflow import IssueWorkflowShell
                    if not isinstance(aggregate, (WorkflowSnapshot, IssueWorkflowShell)):
                        raise WorkflowAuthorityError("issue return lost its issue aggregate")
                    applied = replace(state, consumed_reply_at=component.state.consumed_reply_at, resumes=component.state.resumes,
                                      ci_failures=component.state.ci_failures, pending_ci=component.state.pending_ci,
                                      review_failures=component.state.review_failures)
                    after = replace(aggregate, revision=revision, issue_recovery=replace(component, state=applied, watermarks=watermarks))
                elif isinstance(selection, cc.SelectedPRRepair):
                    if not isinstance(aggregate, ReviewWorkflowSnapshot):
                        raise WorkflowAuthorityError("repair return lost its PR aggregate")
                    applied = replace(state, consumed_reply_at=component.state.consumed_reply_at, resumes=component.state.resumes)
                    after = replace(aggregate, revision=revision, pr_repair=replace(component, state=applied, watermarks=watermarks))
                else:
                    from ..domain.workflow import IssueWorkflowShell
                    if not isinstance(aggregate, (WorkflowSnapshot, IssueWorkflowShell)):
                        raise WorkflowAuthorityError("steward return lost its issue aggregate")
                    after = replace(aggregate, revision=revision, steward=replace(component, state=state, watermarks=watermarks))
                persist_workflow_snapshot(db, after)
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('continuation-return',?,?,?,?)",
                       (key.repository, receipt_key, _stamp(self._now()), encoded))
            return settled

    def _apply_review_result(self, db, returned: cc.ReviewRepairResult) -> None:
        from .workflow_rows import load_issue_aggregate, persist_workflow_snapshot
        admission = returned.admission
        purchase, _ = self._continuation_return_admission(db, admission, WorkflowFamily.ISSUE_RECOVERY)
        if not isinstance(purchase, cc.IssueReviewPurchase):
            raise WorkflowAuthorityError("review result selected another issue recovery operation")
        key = admission.attempt.binding.fence.key
        receipt_key = self._continuation_receipt_key(admission.attempt.run_id, WorkflowFamily.ISSUE_RECOVERY)
        encoded = encode_workflow((admission, returned))
        prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                           (key.repository, receipt_key)).fetchone()
        if prior is not None:
            if prior[0] != encoded:
                raise WorkflowAuthorityError("review return conflicts with its immutable result")
            return
        aggregate = load_issue_aggregate(db, key)
        selected = cc.select_issue_recovery(aggregate) if aggregate is not None else None
        if isinstance(selected, cc.SelectedIssueRecovery) and cc.review_return_is_current(selected, admission):
            component = selected.component
            debt = replace(component.state.review_failures, pending=None,
                count=component.state.review_failures.count + (1 if returned.charge is cc.ReviewCharge.ATTEMPT else 0))
            after = replace(aggregate, revision=WorkflowRevision(aggregate.revision.fence, aggregate.revision.version+1),
                issue_recovery=replace(component, state=replace(component.state, review_failures=debt), watermarks=replace(component.watermarks,
                    last_applied=max(component.watermarks.last_applied, admission.admission_version))))
            persist_workflow_snapshot(db, after)
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('continuation-return',?,?,?,?)",
                   (key.repository, receipt_key, _stamp(self._now()), encoded))

    def _apply_ci_result(self, db, returned: cc.CIRepairResult) -> None:
        from .workflow_rows import load_issue_aggregate, persist_workflow_snapshot
        admission = returned.admission
        purchase, _ = self._continuation_return_admission(db, admission, WorkflowFamily.ISSUE_RECOVERY)
        if not isinstance(purchase, cc.IssueCIPurchase):
            raise WorkflowAuthorityError("CI result selected another issue recovery operation")
        key = admission.attempt.binding.fence.key
        receipt_key = self._continuation_receipt_key(admission.attempt.run_id, WorkflowFamily.ISSUE_RECOVERY)
        encoded = encode_workflow((admission, returned))
        prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-return' AND scope=? AND key=?",
                           (key.repository, receipt_key)).fetchone()
        if prior is not None:
            if prior[0] != encoded:
                raise WorkflowAuthorityError("CI return conflicts with its immutable result")
            return
        aggregate = load_issue_aggregate(db, key)
        selected = cc.select_issue_recovery(aggregate) if aggregate is not None else None
        if isinstance(selected, cc.SelectedIssueRecovery) and cc.ci_return_is_current(selected, admission):
            component = selected.component
            state = replace(component.state, pending_ci=None,
                ci_failures=component.state.ci_failures + (1 if returned.charge is cc.CICharge.ATTEMPT else 0))
            after = replace(aggregate, revision=WorkflowRevision(aggregate.revision.fence, aggregate.revision.version+1),
                issue_recovery=replace(component, state=state, watermarks=replace(component.watermarks,
                    last_applied=max(component.watermarks.last_applied, admission.admission_version))))
            persist_workflow_snapshot(db, after)
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('continuation-return',?,?,?,?)",
                   (key.repository, receipt_key, _stamp(self._now()), encoded))

    def record_review_runtime_preflight_stop(
        self, run_id: RunId, expected_owner_id: str, result: ReviewReturnedEnvelope,
    ) -> ReviewRuntimePreflightStop:
        """Retain the Codex harness branch that returned before any child spawn."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT r.*,b.workload_repository,b.state AS budget_state, "
                "rr.policy AS recovery_policy FROM runs r "
                "JOIN budget_reservations b USING(run_id) "
                "JOIN reservation_recovery rr USING(run_id) WHERE r.run_id=?",
                (str(run_id),),
            ).fetchone()
            if row is None or row["review_round_id"] is None:
                raise ControlStoreError("review preflight stop lacks an attached paid child")
            repository = row["workload_repository"]
            admission = self._load_review_launch_admission(db, run_id, repository, row)
            recorded = load_review_preflight_record(
                db, repository, expected_owner_id, admission.child.round.round_id,
            )
            if (admission.owner_id != expected_owner_id or recorded is None
                    or admission.child not in recorded[2].children
                    or recorded[2].runtime is not RuntimeName.CODEX):
                raise ControlStoreError("review preflight stop disagrees with its admission or runtime")
            if (row["started_at"] is not None
                    or db.execute("SELECT 1 FROM leases WHERE run_id=?", (str(run_id),)).fetchone()
                    or db.execute("SELECT 1 FROM dedupe_keys WHERE namespace='review-execution-launch' "
                                  "AND scope=? AND key=?", (repository, str(run_id))).fetchone()):
                raise ControlStoreError("review preflight stop contradicts a child launch")
            receipt_id = f"review-execution-preflight-stop:{run_id}"
            prior = db.execute(
                "SELECT payload_json FROM dedupe_keys "
                "WHERE namespace='review-execution-preflight-stop' AND scope=? AND key=?",
                (repository, str(run_id)),
            ).fetchone()
            if prior is not None:
                stopped = decode_workflow(prior["payload_json"], ReviewRuntimePreflightStop)
                if stopped.admission != admission or stopped.result != result:
                    raise ControlStoreError("review preflight stop conflicts with its immutable receipt")
                return stopped
            if (row["state"] != "reserved"
                    or row["budget_state"] != "active" or row["recovery_policy"] != "spawn_pending"
                    or db.execute("SELECT 1 FROM leases WHERE run_id=?", (str(run_id),)).fetchone()
                    or db.execute("SELECT 1 FROM charges WHERE run_id=?", (str(run_id),)).fetchone()
                    or db.execute("SELECT 1 FROM dedupe_keys WHERE namespace IN "
                                  "('review-execution-launch','review-execution-return') "
                                  "AND scope=? AND key=?", (repository, str(run_id))).fetchone()):
                raise ControlStoreError("review preflight stop has contradictory launch or money evidence")
            stopped = ReviewRuntimePreflightStop(
                admission, receipt_id, result, _parse_stamp(_stamp(self._now())),
            )
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('review-execution-preflight-stop',?,?,?,?)",
                (repository, str(run_id), _stamp(stopped.stopped_at), encode_workflow(stopped)),
            )
            return stopped

    def settle_review_return(
        self, request: SettlementRequest, returned: ReturnedReviewChild,
    ) -> SettlementResult:
        """Commit review output with its exact settled charge, or neither."""
        if not isinstance(returned, ReturnedReviewChild) or str(request.run_id) != returned.run_id:
            raise ValueError("review settlement requires the returned child's run")
        with self.transaction() as db:
            self._assert_owner_active(db, returned.owner_id)
            repository, prior = self._review_return_authority(db, returned)
            settled = self._settle(db, request)
            if settled.disposition is AccountingDisposition.AVAILABLE:
                charge = db.execute(
                    "SELECT provider_receipt_id FROM charges WHERE run_id=?", (returned.run_id,),
                ).fetchone()
                if charge is None:
                    raise ControlStoreError("review return settlement lacks its actual charge")
                if not prior:
                    self._insert_review_return(db, repository, returned)
            return settled

    def settle_review_preflight_stop(
        self, returned: ReturnedReviewChild,
    ) -> SettlementResult | None:
        """Settle a verified local Codex stop as zero with its raw return."""
        if not isinstance(returned, ReturnedReviewChild):
            raise ValueError("review preflight settlement requires a typed return")
        with self.transaction() as db:
            run = db.execute(
                "SELECT b.workload_repository,b.state AS budget_state,r.state AS run_state, "
                "r.outcome,r.provider_receipt_id,r.started_at,rr.policy FROM budget_reservations b "
                "JOIN runs r USING(run_id) JOIN reservation_recovery rr USING(run_id) "
                "WHERE b.run_id=?", (returned.run_id,),
            ).fetchone()
            if run is None:
                return None
            repository = run["workload_repository"]
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys "
                "WHERE namespace='review-execution-preflight-stop' AND scope=? AND key=?",
                (repository, returned.run_id),
            ).fetchone()
            if row is None:
                return None
            stopped = decode_workflow(row["payload_json"], ReviewRuntimePreflightStop)
            if (stopped.admission.child != returned.child
                    or stopped.admission.owner_id != returned.owner_id
                    or returned.runtime is not RuntimeName.CODEX
                    or stopped.result != returned.result):
                raise ControlStoreError("review preflight settlement disagrees with its stop")
            canonical = replace(returned, returned_at=stopped.stopped_at)
            source, prior = self._review_return_authority(db, canonical)
            if source != repository:
                raise ControlStoreError("review preflight settlement changed repository")
            receipt_id = f"codex:preflight-stop:{returned.run_id}"
            charge = db.execute(
                "SELECT provider_receipt_id,amount_micros,currency,basis,usage_json, "
                "rate_card_id,rate_card_version FROM charges WHERE run_id=?",
                (returned.run_id,),
            ).fetchone()
            if prior and (
                run["run_state"] != "completed" or run["outcome"] != "settled"
                or run["budget_state"] != "settled" or charge is None
                or run["provider_receipt_id"] != receipt_id
                or tuple(charge) != (receipt_id, 0, "USD", PricingBasis.SYNTHETIC.value,
                                    None, None, None)
            ):
                raise ControlStoreError("review preflight replay disagrees with its settled charge")
            if not prior and (
                run["run_state"] != "reserved" or run["budget_state"] != "active"
                or run["policy"] != "spawn_pending" or run["started_at"] is not None
                or run["provider_receipt_id"] is not None
                or charge is not None
            ):
                raise ControlStoreError("review preflight stop lost its pending zero-settlement state")
            request = SettlementRequest(
                RunId(returned.run_id), Money.zero("USD"), PricingBasis.SYNTHETIC,
                receipt_id, None, None, None,
            )
            settled = self._settle(db, request, review_stop=True)
            if settled.disposition is not AccountingDisposition.AVAILABLE:
                raise ControlStoreError("review preflight stop cannot settle its zero charge")
            if not prior:
                self._insert_review_return(db, repository, canonical)
            return settled

    def returned_review_child(
        self, repository: str, run_id: RunId,
    ) -> ReturnedReviewChild | None:
        repository = canonical_repository(repository)
        with self.transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-return' "
                "AND scope=? AND key=?", (repository, str(run_id)),
            ).fetchone()
            if row is None:
                return None
            returned = decode_workflow(row["payload_json"], ReturnedReviewChild)
            if returned.run_id != str(run_id) or self._review_return_authority(db, returned) != (repository, True):
                raise ControlStoreError("review return lookup disagrees with its admitted run")
            return returned

    def read_review_child_evidence(
        self, repository: str, owner_id: str, round_id: str,
    ) -> tuple[ReviewChildEvidence, ...]:
        """Read one planned round's child facts from indexed, transaction-local proof.

        Non-striking or otherwise unclassified launched results remain pending
        until a specific durable disposition exists. This read grants no
        outer-round outcome.
        """
        repository = canonical_repository(repository)
        validate_workflow_identifier(round_id, "review round")
        validate_workflow_identifier(owner_id, "review owner")
        with self.transaction() as db:
            return self._read_review_child_evidence(db, repository, owner_id, round_id)

    @staticmethod
    def _read_review_child_evidence(
        db: sqlite3.Connection, repository: str, owner_id: str, round_id: str,
    ) -> tuple[ReviewChildEvidence, ...]:
        stored = load_review_preflight_record(db, repository, owner_id, round_id)
        if stored is None:
            raise ControlStoreError("review child evidence lacks its canonical preflight")
        selection, _, plan, _ = stored
        revision_row = db.execute(
            "SELECT generation,version FROM workflows WHERE repository=? AND target_kind=? AND target_number=?",
            workflow_scope(plan.round.binding.rejection.fence.key),
        ).fetchone()
        if revision_row is None:
            raise ControlStoreError("review child evidence lost its workflow aggregate")

        def revision_recorded(revision: WorkflowRevision) -> bool:
            return (revision.fence.generation < revision_row["generation"]
                    or (revision.fence.generation == revision_row["generation"]
                        and revision.version <= revision_row["version"]))

        runs = db.execute(
            "SELECT r.*,b.workload_repository,b.state AS budget_state,b.currency AS budget_currency,b.release_reason,"
            "rr.policy AS recovery_policy FROM runs r "
            "JOIN budget_reservations b USING(run_id) "
            "JOIN reservation_recovery rr USING(run_id) "
            "WHERE r.review_round_id=?", (round_id,),
        ).fetchall()
        planned = {child.child_id: child for child in plan.children}
        indexed_runs = {run["review_child_id"]: run for run in runs}
        if (len(runs) > len(plan.children) or len(indexed_runs) != len(runs)
                or any(child_id not in planned for child_id in indexed_runs)):
            raise ControlStoreError("review round contains an unplanned or duplicate paid child")
        evidence: list[ReviewChildEvidence] = []
        for child in plan.children:
            refusal_row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-refusal' "
                "AND scope=? AND key=?", (repository, _review_child_key(child)),
            ).fetchone()
            indexed = _indexed_review_child_admission(db, repository, child)
            run = indexed_runs.get(child.child_id)
            if refusal_row is not None:
                refusal = decode_workflow(refusal_row["payload_json"], ReviewChildAdmissionRefusal)
                if (indexed is not None or run is not None or refusal.child != child
                        or refusal.owner_id != owner_id
                        or refusal.claim_occurrence_id != selection.claim_occurrence_id
                        or not revision_recorded(refusal.refused_revision)
                        or refusal.receipt_id != _review_refusal_receipt_id(refusal)
                        or db.execute("SELECT 1 FROM runs WHERE run_id=?", (refusal.run_id,)).fetchone()):
                    raise ControlStoreError("review refusal conflicts with this planned child")
                evidence.append(ReviewChildRefused(child, refusal.receipt_id))
                continue
            if indexed is None:
                if run is not None:
                    raise ControlStoreError("review paid child lost its admission index")
                evidence.append(ReviewChildPending(child))
                continue
            if (run is None or run["run_id"] != indexed.run_id
                    or indexed.claim_occurrence_id != selection.claim_occurrence_id
                    or indexed.owner_id != owner_id or not revision_recorded(indexed.admitted_revision)):
                raise ControlStoreError("review child admission disagrees with its round")
            admission = SQLiteControlStore._load_review_launch_admission(db, RunId(indexed.run_id), repository, run)
            if admission != indexed:
                raise ControlStoreError("review child admission changed during evidence read")
            release_row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-release' "
                "AND scope=? AND key=?", (repository, indexed.run_id),
            ).fetchone()
            released = proven_unlaunched_run(db, RunId(indexed.run_id))
            if release_row is not None or released:
                if release_row is None or not released:
                    raise ControlStoreError("review unlaunched release lacks matching durable proof")
                proof = decode_workflow(release_row["payload_json"], ReviewUnlaunchedRelease)
                release_reason = run["release_reason"]
                if (proof.admission != indexed or not isinstance(release_reason, str)
                        or proof.reason_digest != hashlib.sha256(release_reason.encode()).hexdigest()
                        or _stamp(proof.released_at) != run["completed_at"]
                        or db.execute(
                            "SELECT 1 FROM dedupe_keys WHERE namespace IN ('review-execution-launch',"
                            "'review-execution-preflight-stop','review-execution-return') "
                            "AND scope=? AND key=?", (repository, indexed.run_id),
                        ).fetchone() is not None):
                    raise ControlStoreError("review release contradicts its admitted child")
                evidence.append(ReviewChildReleased(child, indexed.run_id, proof.receipt_id))
                continue
            stop_row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-preflight-stop' "
                "AND scope=? AND key=?", (repository, indexed.run_id),
            ).fetchone()
            if stop_row is not None:
                stopped = decode_workflow(stop_row["payload_json"], ReviewRuntimePreflightStop)
                if (stopped.admission != indexed or plan.runtime is not RuntimeName.CODEX
                        or run["started_at"] is not None
                        or db.execute(
                            "SELECT 1 FROM dedupe_keys WHERE namespace='review-execution-launch' "
                            "AND scope=? AND key=?", (repository, indexed.run_id),
                        ).fetchone() is not None
                        or db.execute("SELECT 1 FROM leases WHERE run_id=?", (indexed.run_id,)).fetchone()):
                    raise ControlStoreError("review preflight stop disagrees with planned child")
                return_row = db.execute(
                    "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-return' "
                    "AND scope=? AND key=?", (repository, indexed.run_id),
                ).fetchone()
                if return_row is None:
                    if (run["state"] != "reserved" or run["budget_state"] != "active"
                            or run["recovery_policy"] != "spawn_pending"
                            or run["provider_receipt_id"] is not None
                            or db.execute("SELECT 1 FROM charges WHERE run_id=?", (indexed.run_id,)).fetchone()):
                        raise ControlStoreError("pending review stop has contradictory accounting state")
                    evidence.append(ReviewChildPending(child))
                    continue
                returned = decode_workflow(return_row["payload_json"], ReturnedReviewChild)
                if SQLiteControlStore._review_return_authority(db, returned) != (repository, True):
                    raise ControlStoreError("review stop return disagrees with its admission")
                charge = db.execute(
                    "SELECT provider_receipt_id,amount_micros,currency,basis,usage_json,"
                    "rate_card_id,rate_card_version FROM charges WHERE run_id=?", (indexed.run_id,),
                ).fetchone()
                receipt_id = f"codex:preflight-stop:{indexed.run_id}"
                if (run["state"] != "completed" or run["outcome"] != "settled"
                        or run["budget_state"] != "settled" or run["provider_receipt_id"] != receipt_id
                        or charge is None or tuple(charge) != (receipt_id, 0, "USD", PricingBasis.SYNTHETIC.value,
                                                           None, None, None)
                        or returned.result != stopped.result or returned.returned_at != stopped.stopped_at):
                    raise ControlStoreError("review preflight stop lacks its exact zero settlement")
                evidence.append(ReviewChildSettledWithoutLaunch(
                    child, indexed.run_id, receipt_id, stopped.receipt_id,
                    ReviewPreparationFailure(ReviewPreparationKind.RUNTIME, stopped.receipt_id),
                ))
                continue
            launch_row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-launch' "
                "AND scope=? AND key=?", (repository, indexed.run_id),
            ).fetchone()
            return_row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-return' "
                "AND scope=? AND key=?", (repository, indexed.run_id),
            ).fetchone()
            if launch_row is None:
                if return_row is not None or run["started_at"] is not None or run["state"] == "running":
                    raise ControlStoreError("review child has execution evidence without guarded launch")
            else:
                launch = decode_workflow(launch_row["payload_json"], ReviewChildLaunchRegistration)
                if (launch.admission != indexed or launch.dispatch_id != run["dispatch_id"]
                        or run["started_at"] is None
                        or launch.registered_at != _parse_stamp(run["started_at"])):
                    raise ControlStoreError("review child launch disagrees with its admitted run")
                if return_row is not None:
                    returned = decode_workflow(return_row["payload_json"], ReturnedReviewChild)
                    if SQLiteControlStore._review_return_authority(db, returned) != (repository, True):
                        raise ControlStoreError("review child return disagrees with its guarded launch")
                    charge = db.execute(
                        "SELECT provider_receipt_id,amount_micros,currency,basis FROM charges WHERE run_id=?",
                        (indexed.run_id,),
                    ).fetchone()
                    if (charge is None) != (run["budget_state"] != "settled"):
                        raise ControlStoreError("review return and budget disagree with settled charge")
                    if charge is not None:
                        if (run["provider_receipt_id"] != charge["provider_receipt_id"]
                                or charge["currency"] != run["budget_currency"]
                                or type(charge["amount_micros"]) is not int or charge["amount_micros"] < 0):
                            raise ControlStoreError("review return charge disagrees with its admitted run")
                        try:
                            PricingBasis(charge["basis"])
                        except ValueError as exc:
                            raise ControlStoreError("review return has an invalid charge basis") from exc
                        if (run["state"] == "completed" and run["outcome"] in
                                ("settled", "spend_unreconciled")):
                            launched = LaunchedReviewChild(child, indexed.run_id, launch.receipt_id)
                            result = classify_normal_review_return(launched, plan.runtime, returned.result)
                            if returned.result.exit_reason is ExitReason.OPERATOR_CANCELLED:
                                intent = db.execute(
                                    "SELECT dispatch_id,kind,reason FROM run_cancellation_intents WHERE run_id=?",
                                    (indexed.run_id,),
                                ).fetchone()
                                if intent is not None:
                                    if intent["dispatch_id"] != run["dispatch_id"] or intent["kind"] != run["kind"]:
                                        raise ControlStoreError("review cancellation intent disagrees with its run")
                                    if (intent["reason"] == "shutdown_deadline_spend_unknown"
                                            and returned.result.is_error):
                                        result = ReviewChildCancelled(
                                            ReviewCancellationReason.SHUTDOWN,
                                            f"run-cancellation-intent:{indexed.run_id}",
                                        )
                            if result is not None:
                                evidence.append(ReviewChildReturned(
                                    launched, f"review-execution-return:{indexed.run_id}", result,
                                ))
                                continue
            evidence.append(ReviewChildPending(child))
        return tuple(evidence)

    @staticmethod
    def _review_return_authority(
        db: sqlite3.Connection, returned: ReturnedReviewChild,
    ) -> tuple[str, bool]:
        row = db.execute(
            "SELECT r.*,b.workload_repository,l.run_id AS leased, "
            "l.dispatch_id AS lease_dispatch_id,l.owner_id AS lease_owner_id, "
            "l.pid AS lease_pid,l.pgid AS lease_pgid, "
            "l.process_start_ticks AS lease_start_ticks, "
            "l.started_at AS lease_started_at FROM runs r "
            "JOIN budget_reservations b ON b.run_id=r.run_id "
            "LEFT JOIN leases l ON l.run_id=r.run_id WHERE r.run_id=?",
            (returned.run_id,),
        ).fetchone()
        if row is None or row["review_round_id"] is None:
            raise ControlStoreError("review return lacks its attached paid run")
        repository = row["workload_repository"]
        admission = SQLiteControlStore._load_review_launch_admission(
            db, RunId(returned.run_id), repository, row,
        )
        if (returned.child != admission.child or returned.owner_id != admission.owner_id
                or returned.run_id != admission.run_id):
            raise ControlStoreError("review return disagrees with its exact child admission")
        recorded = load_review_preflight_record(
            db, repository, returned.owner_id, returned.child.round.round_id,
        )
        if recorded is None or returned.child not in recorded[2].children or recorded[2].runtime != returned.runtime:
            raise ControlStoreError("review return disagrees with its planned runtime or child")
        launch = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-launch' "
            "AND scope=? AND key=?", (repository, returned.run_id),
        ).fetchone()
        if launch is None:
            if row["leased"] is not None or row["state"] == "running" or row["started_at"] is not None:
                raise ControlStoreError("review return lost its registered launch receipt")
        else:
            registered = decode_workflow(launch["payload_json"], ReviewChildLaunchRegistration)
            if (registered.admission != admission or registered.dispatch_id != row["dispatch_id"]
                    or registered.receipt_id != f"review-execution-launch:{returned.run_id}"
                    or row["started_at"] is None
                    or registered.registered_at != _parse_stamp(row["started_at"])):
                raise ControlStoreError("review return launch receipt disagrees with its admission")
            if row["leased"] is not None and (
                row["lease_dispatch_id"] != registered.dispatch_id
                or row["lease_owner_id"] != admission.owner_id
                or row["lease_pid"] != registered.pid
                or row["lease_pgid"] != registered.pgid
                or row["lease_start_ticks"] != registered.process_start_ticks
                or _parse_stamp(row["lease_started_at"]) != registered.registered_at
            ):
                raise ControlStoreError("review return launch receipt disagrees with its live lease")
        stop = db.execute(
            "SELECT payload_json FROM dedupe_keys "
            "WHERE namespace='review-execution-preflight-stop' AND scope=? AND key=?",
            (repository, returned.run_id),
        ).fetchone()
        if stop is not None:
            stopped = decode_workflow(stop["payload_json"], ReviewRuntimePreflightStop)
            if (stopped.admission != admission or stopped.result != returned.result
                    or launch is not None or row["started_at"] is not None or row["leased"] is not None):
                raise ControlStoreError("review return contradicts its runtime preflight stop")
        prior = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-return' "
            "AND scope=? AND key=?", (repository, returned.run_id),
        ).fetchone()
        if prior is not None:
            if decode_workflow(prior["payload_json"], ReturnedReviewChild) != returned:
                raise ControlStoreError("review return conflicts with its immutable receipt")
            return repository, True
        return repository, False

    @staticmethod
    def _insert_review_return(
        db: sqlite3.Connection, repository: str, returned: ReturnedReviewChild,
    ) -> None:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-execution-return',?,?,?,?)",
            (repository, returned.run_id, _stamp(returned.returned_at), encode_workflow(returned)),
        )

    def reconcile_review_preflight_stop(self, run_id: RunId) -> bool:
        """Complete a pending local stop after its caller disappeared."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT d.payload_json FROM dedupe_keys d "
                "JOIN budget_reservations b ON b.workload_repository=d.scope AND b.run_id=d.key "
                "WHERE d.namespace='review-execution-preflight-stop' AND d.key=?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                return False
            stopped = decode_workflow(row["payload_json"], ReviewRuntimePreflightStop)
        returned = ReturnedReviewChild(
            stopped.admission.child, str(run_id), stopped.admission.owner_id,
            RuntimeName.CODEX, stopped.result, stopped.stopped_at,
        )
        settled = self.settle_review_preflight_stop(returned)
        return settled is not None and settled.disposition is AccountingDisposition.AVAILABLE

    @staticmethod
    def _has_review_preflight_stop(db: sqlite3.Connection, run_id: RunId) -> bool:
        return db.execute(
            "SELECT 1 FROM budget_reservations b JOIN dedupe_keys d "
            "ON d.namespace='review-execution-preflight-stop' "
            "AND d.scope=b.workload_repository AND d.key=b.run_id "
            "WHERE b.run_id=?", (str(run_id),),
        ).fetchone() is not None

    def _settle(
        self, db: sqlite3.Connection, request: SettlementRequest, *, review_stop: bool = False,
    ) -> SettlementResult:
        if not review_stop and self._has_review_preflight_stop(db, request.run_id):
            return SettlementResult(
                AccountingDisposition.CONFLICT, request.amount,
                detail="review preflight stop requires its atomic zero settlement",
            )
        reservation = db.execute(
            "SELECT b.state, b.maximum_micros, b.currency, r.dispatch_id "
            "FROM budget_reservations b JOIN runs r ON r.run_id=b.run_id "
            "WHERE b.run_id=?",
            (str(request.run_id),),
        ).fetchone()
        if reservation is None:
            return SettlementResult(
                AccountingDisposition.CONFLICT,
                request.amount,
                detail="run has no reservation",
            )
        if reservation["currency"] != request.amount.currency:
            return SettlementResult(
                AccountingDisposition.CONFLICT,
                request.amount,
                detail="settlement currency differs from reservation",
            )
        existing = db.execute(
            "SELECT * FROM charges WHERE run_id=? OR provider_receipt_id=?",
            (str(request.run_id), request.provider_receipt_id),
        ).fetchone()
        encoded_usage = (
            _json(
                {
                    "input_tokens": request.usage.input_tokens,
                    "output_tokens": request.usage.output_tokens,
                    "cache_read_tokens": request.usage.cache_read_tokens,
                    "cache_write_tokens": request.usage.cache_write_tokens,
                    "source": request.usage.source.value,
                }
            )
            if request.usage is not None
            else None
        )
        if existing is not None:
            identical = (
                existing["run_id"] == str(request.run_id)
                and existing["provider_receipt_id"] == request.provider_receipt_id
                and existing["amount_micros"] == request.amount.micros
                and existing["currency"] == request.amount.currency
                and existing["basis"] == request.basis.value
                and existing["usage_json"] == encoded_usage
                and existing["rate_card_id"] == request.rate_card_id
                and existing["rate_card_version"] == request.rate_card_version
            )
            recovered_confirmation = (
                existing["run_id"] == str(request.run_id)
                and existing["basis"] == PricingBasis.RECONCILED.value
                and request.basis is not PricingBasis.RECONCILED
                and existing["amount_micros"] == request.amount.micros
                and existing["currency"] == request.amount.currency
            )
            if recovered_confirmation:
                # A dead-child sweep may settle the same final Codex
                # evidence while the live caller is normalizing its
                # already-returned result. Equal monetary authority must
                # converge without duplicating or rewriting the charge.
                run = db.execute(
                    "SELECT state, outcome FROM runs WHERE run_id=?",
                    (str(request.run_id),),
                ).fetchone()
                corrected = (
                    run is not None
                    and run["state"] == "abandoned"
                    and run["outcome"] == "dead_lease"
                )
                already_corrected = (
                    run is not None
                    and run["state"] == "completed"
                    and run["outcome"] == "settled"
                )
                if corrected:
                    db.execute(
                        "UPDATE runs SET state='completed', "
                        "outcome='settled', completed_at=? "
                        "WHERE run_id=?",
                        (_stamp(self._now()), str(request.run_id)),
                    )
                    db.execute(
                        "UPDATE reservation_recovery SET "
                        "reason='normal completion confirmed recovered "
                        "settlement', updated_at=? WHERE run_id=?",
                        (_stamp(self._now()), str(request.run_id)),
                    )
                    self._transition(
                        db,
                        "run",
                        str(request.run_id),
                        "completed",
                        {
                            "outcome": "settled",
                            "settlement": "recovery_confirmed",
                        },
                        correlation=_run_correlation(
                            request.run_id,
                            reservation["dispatch_id"],
                        ),
                    )
                if corrected or already_corrected:
                    return SettlementResult(
                        AccountingDisposition.AVAILABLE,
                        request.amount,
                        idempotent=True,
                        detail=(
                            "normal completion matched recovered settlement"
                        ),
                    )
            return SettlementResult(
                (
                    AccountingDisposition.AVAILABLE
                    if identical
                    else AccountingDisposition.CONFLICT
                ),
                request.amount,
                idempotent=identical,
                detail="" if identical else "conflicting settlement receipt",
            )
        if reservation["state"] != "active":
            return SettlementResult(
                AccountingDisposition.CONFLICT,
                request.amount,
                detail=f"reservation is already {reservation['state']}",
            )
        correlation = _run_correlation(
            request.run_id, reservation["dispatch_id"]
        )
        maximum = int(reservation["maximum_micros"])
        variance = (
            Money(request.amount.currency, request.amount.micros - maximum)
            if request.amount.micros > maximum
            else None
        )
        db.execute(
            "INSERT INTO charges(run_id, provider_receipt_id, amount_micros, "
            "currency, basis, usage_json, rate_card_id, rate_card_version, "
            "settled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(request.run_id),
                request.provider_receipt_id,
                request.amount.micros,
                request.amount.currency,
                request.basis.value,
                encoded_usage,
                request.rate_card_id,
                request.rate_card_version,
                _stamp(self._now()),
            ),
        )
        db.execute(
            "UPDATE budget_reservations SET state='settled', updated_at=? "
            "WHERE run_id=?",
            (_stamp(self._now()), str(request.run_id)),
        )
        db.execute(
            "UPDATE runs SET "
            "state=CASE WHEN state IN ('completed','cancelled','abandoned') "
            "THEN state ELSE 'completed' END, "
            "outcome=CASE WHEN state IN ('completed','cancelled','abandoned') "
            "THEN outcome ELSE 'settled' END, "
            "provider_receipt_id=?, completed_at=COALESCE(completed_at, ?) "
            "WHERE run_id=?",
            (
                request.provider_receipt_id,
                _stamp(self._now()),
                str(request.run_id),
            ),
        )
        corrected_dead_lease = False
        if request.basis is not PricingBasis.RECONCILED:
            # The normal caller can win after lease reclamation but before
            # the recovery reconciler. Its successful settlement proves
            # the accounting owner survived the child, so dead_lease is a
            # stale recovery observation rather than a terminal outcome.
            corrected_dead_lease = bool(
                db.execute(
                    "UPDATE runs SET state='completed', outcome='settled', "
                    "completed_at=? WHERE run_id=? AND state='abandoned' "
                    "AND outcome='dead_lease'",
                    (_stamp(self._now()), str(request.run_id)),
                ).rowcount
            )
            if corrected_dead_lease:
                db.execute(
                    "UPDATE reservation_recovery SET "
                    "reason='normal settlement won recovery race', "
                    "updated_at=? WHERE run_id=?",
                    (_stamp(self._now()), str(request.run_id)),
                )
        db.execute("DELETE FROM leases WHERE run_id=?", (str(request.run_id),))
        self._event(
            db,
            SpendRecorded(
                str(request.run_id),
                request.amount.micros,
                request.amount.currency,
                request.basis.value,
                request.rate_card_id,
                request.rate_card_version,
            ),
            correlation=correlation,
        )
        if variance is not None:
            self._event(
                db,
                BudgetVarianceDetected(
                    str(request.run_id),
                    maximum,
                    request.amount.micros,
                    variance.micros,
                    request.amount.currency,
                ),
                correlation=correlation,
            )
        self._transition(
            db,
            "reservation",
            str(request.run_id),
            "settled",
            {
                "amount_micros": request.amount.micros,
                "currency": request.amount.currency,
                "basis": request.basis.value,
                "provider_receipt_id": request.provider_receipt_id,
            },
            correlation=correlation,
        )
        if corrected_dead_lease:
            self._transition(
                db,
                "run",
                str(request.run_id),
                "completed",
                {
                    "outcome": "settled",
                    "settlement": "normal_after_reclaim",
                },
                correlation=correlation,
            )
        return SettlementResult(
            AccountingDisposition.AVAILABLE,
            request.amount,
            variance=variance,
        )

    def release(self, run_id: RunId, reason: str) -> ReleaseResult:
        if not reason:
            raise ValueError("reservation release reason is required")
        with self.transaction() as db, self.mutation_savepoint(db):
            return self._release(db, run_id, reason)

    def release_unlaunched(
        self, run_id: RunId, reason: str
    ) -> ReleaseResult:
        """Release only a reservation durably proven never to have run."""

        if not reason:
            raise ValueError("reservation release reason is required")
        with self.transaction() as db, self.mutation_savepoint(db):
            recovery = db.execute(
                "SELECT rr.policy, r.state AS run_state, "
                "EXISTS(SELECT 1 FROM leases l WHERE l.run_id=rr.run_id) "
                "AS has_lease "
                "FROM reservation_recovery rr JOIN runs r "
                "ON r.run_id=rr.run_id WHERE rr.run_id=?",
                (str(run_id),),
            ).fetchone()
            if (
                recovery is None
                or recovery["policy"] != "spawn_pending"
                or recovery["run_state"] not in {"reserved", "starting"}
                or bool(recovery["has_lease"])
            ):
                return ReleaseResult(
                    AccountingDisposition.CONFLICT,
                    run_id,
                    detail=(
                        "reservation lacks durable proof that paid execution "
                        "never started"
                    ),
                )
            return self._release(db, run_id, reason)

    def release_retained(
        self,
        run_id: RunId,
        reason: str,
        retained_before: datetime,
    ) -> ReleaseResult:
        """Release a completed, unmeasured run after its reconciliation grace."""

        if not reason:
            raise ValueError("retained reservation release reason is required")
        cutoff = _stamp(retained_before)
        with self.transaction() as db:
            if self._has_review_preflight_stop(db, run_id):
                return ReleaseResult(AccountingDisposition.CONFLICT, run_id,
                                     detail="review preflight stop awaits accounting capture")
            row = db.execute(
                "SELECT b.state AS reservation_state, b.release_reason, "
                "rr.policy, rr.updated_at AS recovery_updated_at, "
                "r.state AS run_state, r.outcome, "
                "EXISTS(SELECT 1 FROM leases l WHERE l.run_id=b.run_id) "
                "AS has_lease "
                "FROM budget_reservations b "
                "LEFT JOIN reservation_recovery rr ON rr.run_id=b.run_id "
                "JOIN runs r ON r.run_id=b.run_id WHERE b.run_id=?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                return ReleaseResult(
                    AccountingDisposition.CONFLICT,
                    run_id,
                    detail="run has no reservation",
                )
            if row["reservation_state"] == "released":
                identical = row["release_reason"] == reason
                return ReleaseResult(
                    (
                        AccountingDisposition.AVAILABLE
                        if identical
                        else AccountingDisposition.CONFLICT
                    ),
                    run_id,
                    idempotent=identical,
                    detail=(
                        ""
                        if identical
                        else "conflicting release reason"
                    ),
                )
            eligible = (
                row["reservation_state"] == "active"
                and row["policy"] == "reconcile_only"
                and row["run_state"] == "completed"
                and row["outcome"] == "spend_unreconciled"
                and not bool(row["has_lease"])
                and row["recovery_updated_at"] is not None
                and str(row["recovery_updated_at"]) <= cutoff
            )
            if not eligible:
                return ReleaseResult(
                    AccountingDisposition.CONFLICT,
                    run_id,
                    detail=(
                        "reservation is not a stale completed "
                        "accounting-retained run"
                    ),
                )
            now = _stamp(self._now())
            db.execute(
                "UPDATE budget_reservations SET state='released', "
                "release_reason=?, updated_at=? WHERE run_id=?",
                (reason, now, str(run_id)),
            )
            db.execute(
                "UPDATE runs SET outcome='spend_unmeasured_released' "
                "WHERE run_id=?",
                (str(run_id),),
            )
            self._transition(
                db,
                "reservation",
                str(run_id),
                "released",
                {
                    "reason": reason,
                    "accounting": "unmeasured_after_reconciliation_grace",
                },
                correlation=Correlation(run_id=str(run_id)),
            )
            return ReleaseResult(AccountingDisposition.AVAILABLE, run_id)

    def _finish_unlaunched_pr_repair(self, db, run_id: RunId) -> None:
        from .workflow_rows import persist_workflow_snapshot
        run = db.execute("SELECT continuations_json FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
        for purchase in decode_workflow(run[0], tuple):
            if isinstance(purchase, cc.IssueReviewPurchase):
                admission = self._review_repair_admission(db, WorkflowAttemptIdentity(purchase.capture.binding, run_id, purchase.capture.leg_id))
                self._apply_review_result(db, cc.ReviewRepairResult(admission, cc.ReviewCharge.NOT_LAUNCHED))
                continue
            if isinstance(purchase, cc.IssueCIPurchase):
                admission = self._ci_repair_admission(db, WorkflowAttemptIdentity(purchase.capture.binding, run_id, purchase.capture.leg_id))
                self._apply_ci_result(db, cc.CIRepairResult(admission, cc.CICharge.NOT_LAUNCHED))
                continue
            if not isinstance(purchase, cc.PRRepairPurchase) or not purchase.branch:
                continue
            admission = self._pr_repair_admission(db, WorkflowAttemptIdentity(purchase.capture.binding, run_id, purchase.capture.leg_id))
            key = admission.attempt.binding.fence.key
            receipt_key = self._continuation_receipt_key(run_id, WorkflowFamily.PR_REPAIR)
            if db.execute("SELECT 1 FROM dedupe_keys WHERE namespace='pr-repair-result' AND scope=? AND key=?", (key.repository, receipt_key)).fetchone():
                raise WorkflowAuthorityError("unlaunched repair contradicts a captured paid output")
            aggregate = load_review_workflow_row(db, key)
            selected = cc.select_pr_repair(aggregate) if aggregate is not None else None
            if isinstance(selected, cc.SelectedPRRepair) and cc.continuation_return_is_current(selected, admission):
                after = replace(aggregate, revision=WorkflowRevision(aggregate.revision.fence, aggregate.revision.version+1),
                    pr_repair=replace(selected.component, watermarks=replace(selected.component.watermarks, last_applied=admission.admission_version)))
                persist_workflow_snapshot(db, after)
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('continuation-return',?,?,?,?)",
                (key.repository, receipt_key, _stamp(self._now()), encode_workflow((admission, cc.PRRepairNotLaunched(admission)))))

    def _release(
        self,
        db: sqlite3.Connection,
        run_id: RunId,
        reason: str,
    ) -> ReleaseResult:
        reservation = db.execute(
            "SELECT state, release_reason FROM budget_reservations WHERE run_id=?",
            (str(run_id),),
        ).fetchone()
        if self._has_review_preflight_stop(db, run_id):
            return ReleaseResult(AccountingDisposition.CONFLICT, run_id,
                                 detail="review preflight stop awaits accounting capture")
        if reservation is None:
            return ReleaseResult(
                AccountingDisposition.CONFLICT,
                run_id,
                detail="run has no reservation",
            )
        if reservation["state"] == "released":
            identical = reservation["release_reason"] == reason
            return ReleaseResult(
                (
                    AccountingDisposition.AVAILABLE
                    if identical
                    else AccountingDisposition.CONFLICT
                ),
                run_id,
                idempotent=identical,
                detail="" if identical else "conflicting release reason",
            )
        if reservation["state"] != "active":
            return ReleaseResult(
                AccountingDisposition.CONFLICT,
                run_id,
                detail=f"reservation is already {reservation['state']}",
            )
        now = _stamp(self._now())
        never_launched = db.execute(
            "SELECT r.review_round_id FROM runs r JOIN reservation_recovery rr USING(run_id) "
            "WHERE r.run_id=? AND r.state IN ('reserved','starting') AND rr.policy='spawn_pending' "
            "AND r.provider_receipt_id IS NULL AND NOT EXISTS(SELECT 1 FROM charges c WHERE c.run_id=r.run_id) "
            "AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.run_id=r.run_id)", (str(run_id),),
        ).fetchone()
        review_release: ReviewUnlaunchedRelease | None = None
        repository: str | None = None
        if never_launched is not None and never_launched["review_round_id"] is not None:
            repository = db.execute(
                "SELECT workload_repository FROM budget_reservations WHERE run_id=?", (str(run_id),),
            ).fetchone()["workload_repository"]
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
            admission = self._load_review_launch_admission(db, run_id, repository, run)
            if db.execute(
                "SELECT 1 FROM dedupe_keys WHERE namespace IN ('review-execution-launch',"
                "'review-execution-preflight-stop','review-execution-return') AND scope=? AND key=?",
                (repository, str(run_id)),
            ).fetchone() is not None:
                raise ControlStoreError("unlaunched review release contradicts retained execution evidence")
            review_release = ReviewUnlaunchedRelease(
                admission, f"review-execution-release:{run_id}",
                hashlib.sha256(reason.encode()).hexdigest(), _parse_stamp(now),
            )
        db.execute(
            "UPDATE budget_reservations SET state='released', release_reason=?, "
            "updated_at=? WHERE run_id=?",
            (reason, now, str(run_id)),
        )
        db.execute(
            "UPDATE runs SET "
            "state=CASE WHEN state='abandoned' THEN 'abandoned' ELSE 'cancelled' END, "
            "outcome=CASE WHEN state='abandoned' THEN outcome ELSE ? END, "
            "completed_at=? WHERE run_id=?",
            ("unlaunched_released" if never_launched is not None else "released", now, str(run_id)),
        )
        db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
        if review_release is not None and repository is not None:
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('review-execution-release',?,?,?,?)",
                (repository, str(run_id), now, encode_workflow(review_release)),
            )
        if never_launched is not None:
            self._finish_unlaunched_pr_repair(db, run_id)
        self._transition(
            db,
            "reservation",
            str(run_id),
            "released",
            {"reason": reason},
            correlation=Correlation(run_id=str(run_id)),
        )
        return ReleaseResult(AccountingDisposition.AVAILABLE, run_id)

    def enqueue_command(self, command: LifecycleCommand) -> PendingCommand:
        with self.transaction() as db:
            return self._enqueue_command(db, command)

    def _enqueue_command(
        self, db: sqlite3.Connection, command: LifecycleCommand, *,
        registering_review_submission: bool = False,
        registering_steward_verdict: bool = False,
    ) -> PendingCommand:
        """Use the existing outbox inside either a command or workflow transaction."""
        payload = _json(command.payload)
        digest = _command_payload_digest(command, payload)
        now = _stamp(self._now())
        self._assert_owner_active(db, command.executor_identity)
        self._validate_workflow_command(
            db, command, registering_review_submission=registering_review_submission,
            registering_steward_verdict=registering_steward_verdict,
            allow_blocked_submission_retry=True,
        )
        row = db.execute(
            "SELECT * FROM lifecycle_commands WHERE idempotency_key=?",
            (command.idempotency_key,),
        ).fetchone()
        if row is not None:
            if (
                row["command_id"] != str(command.command_id)
                or row["kind"] != command.kind.value
                or row["target"] != command.target
                or row["expected_revision"] != command.expected_revision
                or row["repository"] != command.repository
                or row["executor_identity"] != command.executor_identity
                or row["payload_digest"] != digest
                or row["workflow_binding_json"] != _encoded_command_binding(command)
            ):
                raise ControlStoreError(
                    f"idempotency key {command.idempotency_key!r} conflicts"
                )
            return PendingCommand(
                _command_from_row(row),
                row["state"],
                row["attempts"],
                int(row["refusals"]),
            )
        db.execute(
            "INSERT INTO lifecycle_commands(command_id, idempotency_key, "
            "logical_key, occurrence, kind, "
            "target, expected_revision, config_version, repository, "
            "executor_identity, payload_json, "
            "payload_digest, state, created_at, updated_at, workflow_binding_json) "
            "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                str(command.command_id),
                command.idempotency_key,
                command.idempotency_key,
                command.kind.value,
                command.target,
                command.expected_revision,
                command.config_version,
                command.repository,
                command.executor_identity,
                payload,
                digest,
                now,
                now,
                _encoded_command_binding(command),
            ),
        )
        self._transition(
            db,
            "lifecycle_command",
            str(command.command_id),
            "pending",
            {
                "kind": command.kind.value,
                "target": command.target,
                "idempotency_key": command.idempotency_key,
                "expected_revision": command.expected_revision,
                "config_version": command.config_version,
            },
            correlation=_command_correlation(command),
        )
        return PendingCommand(command, "pending", 0)

    def _validate_workflow_command(
        self, db: sqlite3.Connection, command: LifecycleCommand, *,
        registering_review_submission: bool = False,
        registering_steward_verdict: bool = False,
        allow_blocked_submission_retry: bool = False,
    ) -> None:
        if registering_steward_verdict or "steward_guard" in command.payload or command.idempotency_key.startswith("steward-verdict:"):
            self._validate_steward_command(db, command, registering=registering_steward_verdict)
            return
        binding = command.workflow
        publication = command.kind in {CommandKind.PUBLISH_BRANCH, CommandKind.OPEN_PULL_REQUEST, CommandKind.UPDATE_PULL_REQUEST_BODY}
        captured_run = self._publication_command_run(db, command) if publication else None
        if binding is None:
            if publication:
                branch = command.payload.get("branch")
                issue = issue_from_branch(branch) if isinstance(branch, str) else None
                activated = db.execute(
                    "SELECT 1 FROM workflows JOIN workflow_authority_epochs USING(repository) "
                    "WHERE repository=? AND target_kind='issue' AND target_number=? AND family='resolve'",
                    (command.repository, issue),
                ).fetchone()
                if activated is not None:
                    if captured_run is None or captured_run["kind"] in _RESOLVE_PAID_KINDS:
                        raise ControlStoreError("activated resolve publication requires captured workflow authority")
            if command.target.startswith("issue:") and db.execute(
                "SELECT 1 FROM workflows JOIN workflow_authority_epochs USING(repository) "
                "WHERE repository=? AND target_kind='issue' AND target_number=? AND family='resolve'",
                (command.repository, command.target.partition(":")[2]),
            ).fetchone() is not None:
                if deployment_role(command.repository, command.executor_identity) == "fix":
                    raise ControlStoreError("activated issue command requires captured workflow authority")
            if command.kind is CommandKind.SUBMIT_REVIEW and load_family_authority(
                db, WorkflowFamily.REVIEW_REJECTIONS, command.repository,
            ) is not None:
                # ADR 0015: an activated review family has one submission path. An
                # unbound verdict would neither fence by generation nor settle a
                # baseline, so it could double-post beside a bound one.
                raise ControlStoreError("activated review submission requires captured workflow authority")
            return
        self._assert_owner_active(db, command.executor_identity)
        try:
            role = deployment_role(command.repository, command.executor_identity)
            if binding.authority.family is WorkflowFamily.REVIEW_REJECTIONS:
                if role != "review":
                    raise WorkflowAuthorityError("review rejection commands require the repository's review owner")
                # Allowlist, not a denylist: publication, issue and merge kinds
                # can never ride on review-family authority.
                if command.kind not in _REVIEW_FAMILY_COMMAND_KINDS:
                    raise WorkflowAuthorityError(f"review rejection workflow cannot enqueue {command.kind.value} commands")
                # A review relabel or dismissal without its decodable guard is
                # unrepresentable in the outbox, not merely skipped by the executor.
                review_family_op(command)
                bound_submission_op(command)
                review_snapshot = require_review_binding(db, binding, allow_gated=True)
                if (command.kind is CommandKind.SUBMIT_REVIEW
                        and isinstance(review_snapshot.execution, BlockedReviewExecutionMigration)):
                    prior = db.execute("SELECT * FROM lifecycle_commands WHERE idempotency_key=?",
                                       (command.idempotency_key,)).fetchone() if allow_blocked_submission_retry else None
                    if (prior is None or prior["state"] not in ("succeeded", "permanent_failure")
                            or _command_from_row(prior) != command):
                        raise WorkflowAuthorityError("review execution migration is blocked")
                if (command.kind is CommandKind.SUBMIT_REVIEW
                        and isinstance(review_snapshot.execution, CurrentReviewExecution)):
                    if not registering_review_submission:
                        registration = read_review_submission_registration(
                            db, command.repository, str(command.command_id),
                        )
                        if (registration is None
                                or registration.round.binding != review_snapshot.execution.binding
                                or registration.round.binding.rejection != binding
                                or registration.submission.command_id != str(command.command_id)
                                or registration.owner_id != command.executor_identity
                                or registration.command_digest != _command_payload_digest(command, _json(command.payload))
                                or registration.idempotency_key != command.idempotency_key
                                or registration.config_version != command.config_version):
                            raise WorkflowAuthorityError(
                                "active review execution submission requires atomic round registration"
                            )
                if command.target != f"pr:{binding.fence.key.target.number}":
                    raise WorkflowAuthorityError("review rejection command target disagrees with its pull request")
                return
            if role != "fix":
                raise WorkflowAuthorityError("resolve commands require the repository's fix owner")
            require_resolve_binding(db, binding, allow_gated=True)
            if command.target.startswith("issue:") and command.target != f"issue:{binding.fence.key.target.number}":
                raise WorkflowAuthorityError("issue command target disagrees with its workflow")
            if publication and captured_run is None:
                raise WorkflowAuthorityError("publication command lacks its captured run authority")
        except (ValueError, TypeError) as exc:
            raise ControlStoreError(str(exc)) from exc

    @staticmethod
    def _publication_command_run(db: sqlite3.Connection, command: LifecycleCommand) -> sqlite3.Row | None:
        run_id = command.payload.get("run_id")
        if run_id is None:
            return None
        if not isinstance(run_id, str) or not run_id:
            raise ControlStoreError("publication run identity is malformed")
        row = db.execute(
            "SELECT kind, owner_id, dispatch_id, workflow_binding_json FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None or row["owner_id"] != command.executor_identity or row["workflow_binding_json"] != _encoded_command_binding(command):
            raise ControlStoreError("publication command lacks its captured run authority")
        try:
            dispatch = json.loads(row["dispatch_id"])
        except (ValueError, TypeError) as exc:
            raise ControlStoreError("publication run metadata is malformed") from exc
        if not isinstance(dispatch, dict) or dispatch.get("repository") != command.repository or dispatch.get("role") != "fix" or dispatch.get("publication_branch") != command.payload.get("branch") or dispatch.get("cwd") != command.payload.get("cwd"):
            raise ControlStoreError("publication command disagrees with captured branch authority")
        issue = dispatch.get("issue")
        if issue is not None and (type(issue) is not int or issue <= 0 or issue_from_branch(str(command.payload.get("branch", ""))) != issue):
            raise ControlStoreError("publication branch disagrees with its captured issue")
        return row

    def enqueue_command_occurrence(
        self, command: LifecycleCommand
    ) -> PendingCommand:
        """Start a new durable occurrence of a previously satisfied effect.

        This is used only after authoritative readback proves a terminal
        command's desired state was later reversed. The generation allocation
        and insert share one transaction, preventing concurrent daemons from
        executing the same recurrence twice.
        """

        if "steward_guard" in command.payload or command.idempotency_key.startswith("steward-verdict:"):
            raise WorkflowAuthorityError("a selected steward verdict cannot acquire a new command occurrence")
        payload = _json(command.payload)
        payload_digest = _command_payload_digest(command, payload)
        now = _stamp(self._now())
        with self.transaction() as db:
            self._assert_owner_active(db, command.executor_identity)
            self._validate_workflow_command(db, command)
            existing = db.execute(
                "SELECT logical_key FROM lifecycle_commands WHERE command_id=?",
                (str(command.command_id),),
            ).fetchone()
            logical_key = (
                str(existing["logical_key"])
                if existing is not None
                else command.idempotency_key
            )
            latest = db.execute(
                "SELECT * FROM lifecycle_commands WHERE logical_key=? "
                "ORDER BY occurrence DESC LIMIT 1",
                (logical_key,),
            ).fetchone()
            if latest is not None and (command.workflow is not None or latest["workflow_binding_json"] is not None):
                self._validate_workflow_reexecution(latest)
                if db.execute(
                    "SELECT 1 FROM workflow_rearm_commands WHERE command_id=?", (latest["command_id"],),
                ).fetchone() is not None:
                    raise ControlStoreError("rearm obligations cannot acquire new command occurrences; use authorized retry or continuation")
            if latest is not None and latest["state"] in {
                "pending",
                "executing",
                "retryable_failure",
                "unknown_outcome",
            }:
                if (
                    latest["kind"] != command.kind.value
                    or latest["target"] != command.target
                    or latest["expected_revision"] != command.expected_revision
                    or latest["repository"] != command.repository
                    or latest["executor_identity"] != command.executor_identity
                    or latest["payload_digest"] != payload_digest
                    or latest["workflow_binding_json"] != _encoded_command_binding(command)
                ):
                    raise ControlStoreError(
                        f"logical command {logical_key!r} has a conflicting "
                        "active occurrence"
                    )
                return PendingCommand(
                    _command_from_row(latest),
                    str(latest["state"]),
                    int(latest["attempts"]),
                    int(latest["refusals"]),
                )
            occurrence = (
                int(latest["occurrence"]) + 1 if latest is not None else 0
            )
            occurrence_key = f"{logical_key}:occurrence:{occurrence}"
            command_id = CommandId(
                hashlib.sha256(occurrence_key.encode()).hexdigest()
            )
            repeated = LifecycleCommand(
                command_id,
                occurrence_key,
                command.kind,
                command.target,
                command.expected_revision,
                dict(command.payload),
                command.config_version,
                command.repository,
                command.executor_identity,
                command.workflow,
            )
            db.execute(
                "INSERT INTO lifecycle_commands(command_id, idempotency_key, "
                "logical_key, occurrence, kind, target, expected_revision, "
                "config_version, repository, executor_identity, payload_json, "
                "payload_digest, state, created_at, updated_at, workflow_binding_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    str(command_id),
                    occurrence_key,
                    logical_key,
                    occurrence,
                    command.kind.value,
                    command.target,
                    command.expected_revision,
                    command.config_version,
                    command.repository,
                    command.executor_identity,
                    payload,
                    payload_digest,
                    now,
                    now,
                    _encoded_command_binding(command),
                ),
            )
            self._transition(
                db,
                "lifecycle_command",
                str(command_id),
                "pending",
                {
                    "kind": command.kind.value,
                    "target": command.target,
                    "idempotency_key": occurrence_key,
                    "logical_key": logical_key,
                    "occurrence": occurrence,
                    "expected_revision": command.expected_revision,
                    "config_version": command.config_version,
                },
                correlation=_command_correlation(repeated),
            )
            return PendingCommand(repeated, "pending", 0)

    def last_published_revision(self, target: str) -> str | None:
        """The revision this deployment most recently PUBLISHED to `target`
        (newest succeeded PUBLISH_BRANCH receipt), or None when it never has.

        The publish broker's foreign-write fence (ADR 0012 §2): a remote tip
        equal to this value is our own published history — the agent may
        rewrite it; anything else was written by someone else and must be
        incorporated, not replaced."""
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT r.remote_revision FROM effect_receipts AS r "
                "JOIN lifecycle_commands AS c ON c.command_id=r.command_id "
                "WHERE c.target=? AND c.kind=? AND r.status='succeeded' "
                "ORDER BY c.created_at DESC, c.rowid DESC LIMIT 1",
                (target, CommandKind.PUBLISH_BRANCH.value),
            ).fetchone()
        if row is None or not row[0]:
            return None
        return str(row[0])

    def pending_commands(
        self,
        *,
        repository: str | None = None,
        executor_identity: str | None = None,
    ) -> tuple[PendingCommand, ...]:
        predicates = [
            "state IN ('pending','executing','retryable_failure','unknown_outcome')"
        ]
        params: list[str] = []
        if repository is not None:
            predicates.append("repository=?")
            params.append(repository)
        if executor_identity is not None:
            predicates.append("executor_identity=?")
            params.append(executor_identity)
        with closing(self._connect()) as db, db:
            rows = db.execute(
                "SELECT * FROM lifecycle_commands WHERE "
                + " AND ".join(predicates)
                + " ORDER BY created_at, command_id",
                tuple(params),
            ).fetchall()
        return tuple(
            PendingCommand(
                _command_from_row(row),
                row["state"],
                row["attempts"],
                int(row["refusals"]),
            )
            for row in rows
        )

    def command_reconciliation(self, command_id: CommandId) -> CommandReconciliation | None:
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT workflow_reconciliation_json FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
        if row is None:
            raise KeyError(str(command_id))
        return self._command_reconciliation(row)

    @staticmethod
    def _command_reconciliation(row: sqlite3.Row) -> CommandReconciliation | None:
        raw = row["workflow_reconciliation_json"]
        return None if raw is None else decode_workflow(
            raw, ActiveReconciliation | ReconciliationOnly | RetiredUncertainCommand | RetiredSettledCommand,
        )

    @staticmethod
    def _require_reconciliation_gate(
        db: sqlite3.Connection, command: LifecycleCommand,
        state: ReconciliationOnly | RetiredUncertainCommand,
    ) -> OpenGate:
        binding = command.workflow
        if binding is None:
            raise WorkflowAuthorityError("command uncertainty has no captured workflow")
        # The binding's family selects the only rows that can hold its gate.
        current = load_workflow_row(db, binding.fence.key)
        if current is None:
            raise WorkflowAuthorityError("command uncertainty workflow is absent")
        uncertainty = state.uncertainty if isinstance(state, RetiredUncertainCommand) else state
        gate = next((opened for item in current.gates
                     if (opened := item.gate if isinstance(item, RearmingGate) else item).gate_id == uncertainty.gate_id), None)
        if gate is None or not isinstance(gate.cause, (RemoteOutcomeUncertain, RetryAuthorizationRequired)) or gate.cause.command_id != command.command_id:
            raise WorkflowAuthorityError("command uncertainty has no matching visible gate")
        if isinstance(gate.cause, RemoteOutcomeUncertain) and (
            gate.cause.unresolved_since != uncertainty.history.unresolved_since
            or gate.cause.failed_reads != uncertainty.history.failed_reads
        ):
            raise WorkflowAuthorityError("command uncertainty gate disagrees with its read history")
        if isinstance(gate.cause, RetryAuthorizationRequired):
            row = db.execute("SELECT * FROM workflow_evidence WHERE evidence_id=?", (gate.cause.absence_evidence_id,)).fetchone()
            if row is None or row["kind"] != "absence" or row["command_id"] != str(command.command_id):
                raise WorkflowAuthorityError("known-absence gate lacks command evidence")
            evidence = decode_workflow(row["payload_json"], CommandAbsenceEvidence)
            if evidence.binding != binding or evidence.command_id != command.command_id:
                raise WorkflowAuthorityError("known-absence evidence belongs to another command")
        return gate

    @staticmethod
    def _require_retirement_evidence(
        db: sqlite3.Connection, command: LifecycleCommand,
        state: RetiredSettledCommand | RetiredUncertainCommand,
    ) -> None:
        if command.workflow is None:
            raise WorkflowAuthorityError("retired command has no captured workflow")
        scope = workflow_scope(command.workflow.fence.key)
        identifiers = (state.retirement_evidence_id, state.outcome_evidence_id) if isinstance(state, RetiredSettledCommand) else (state.retirement_evidence_id,)
        for index, identifier in enumerate(identifiers):
            row = db.execute("SELECT * FROM workflow_evidence WHERE evidence_id=?", (identifier,)).fetchone()
            if row is None or (row["repository"], row["target_kind"], row["target_number"]) != scope or row["command_id"] not in ((None, str(command.command_id)) if index == 0 else (str(command.command_id),)):
                raise WorkflowAuthorityError("retired command lacks scoped retirement/outcome evidence")
            if not isinstance(json.loads(row["payload_json"]), dict):
                raise WorkflowAuthorityError("retired command evidence is malformed")

    def claim_escalation_signals(
        self, repository: str, executor_identity: str, *, limit: int = 32,
    ) -> tuple[int, ...]:
        """Claim each review escalation's one operator signal; the pull requests to alert on.

        An escalation is signalled once its attention hold is verified on
        GitHub, whichever path settled that projection SUCCEEDED: the direct
        write, a scheduled or reconciliation read after a lost reply, or a
        restart's recovery. An escalation whose signal is owed without such
        a receipt (its hold found already present, its re-plans exhausted,
        its restoration contested) is signalled too. One signal per
        escalation identity (a verdict's submission, a ceiling's gate), so a
        restored or re-planned hold never re-pages. Rows written by a
        retired predecessor of this executor are this executor's to send;
        every other executor's rows are filtered before the limit, so they
        never starve these. A malformed owed record is quarantined and
        journalled once (`escalation_signal_malformed`), never re-read, and
        never blocks the well-formed rows. The journal event commits with the
        claim; the caller's alert after commit is best-effort, as every alert.
        """
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("escalation signal claim limit must be between 1 and 128")
        repository = canonical_repository(repository)
        claimed: list[int] = []
        with self.transaction() as db:
            self._assert_owner_active(db, executor_identity)
            lineage = self._owner_lineage(db, repository, executor_identity)
            owners = ",".join("?" for _ in lineage)
            rows = db.execute(
                f"SELECT * FROM lifecycle_commands c WHERE c.repository=? AND c.executor_identity IN ({owners}) "
                "AND c.kind=? AND c.state=? AND (c.idempotency_key LIKE ? OR c.idempotency_key LIKE ?) "
                "AND NOT EXISTS (SELECT 1 FROM dedupe_keys d WHERE d.namespace=? AND d.scope=c.repository "
                "AND d.key=c.command_id) ORDER BY c.created_at LIMIT ?",
                (repository, *lineage, CommandKind.RELABEL.value, EffectStatus.SUCCEEDED.value,
                 ESCALATION_PROJECTION_PREFIX + "%", HOLD_RESTORE_PREFIX + "%", ESCALATION_SIGNAL_COMMAND_NAMESPACE,
                 limit),
            ).fetchall()
            for row in rows:
                command = _command_from_row(row)
                self._claim_key(db, ESCALATION_SIGNAL_COMMAND_NAMESPACE, repository, str(command.command_id), {})
                identity = escalation_signal_identity(command)
                kind, _, number = command.target.partition(":")
                if identity is not None and kind == "pr" and number.isdigit():
                    if self._claim_key(db, ESCALATION_SIGNAL_NAMESPACE, repository, identity, {"pr": int(number)}):
                        claimed.append(int(number))
            for row in db.execute(
                f"SELECT e.scope, e.key, e.payload_json FROM dedupe_keys e WHERE e.namespace=? "
                f"AND e.scope IN ({owners}) "
                "AND NOT EXISTS (SELECT 1 FROM dedupe_keys d WHERE d.namespace=? AND d.scope=? AND d.key=e.key) "
                "AND NOT EXISTS (SELECT 1 FROM dedupe_keys q WHERE q.namespace=? AND q.scope=e.scope AND q.key=e.key) "
                "ORDER BY e.claimed_at, e.key LIMIT ?",
                (ESCALATION_SIGNAL_OWED_NAMESPACE, *lineage, ESCALATION_SIGNAL_NAMESPACE, repository,
                 ESCALATION_SIGNAL_QUARANTINE_NAMESPACE, limit),
            ).fetchall():
                parsed = _owed_escalation_signal(row["payload_json"])
                if isinstance(parsed, str):
                    self._claim_key(db, ESCALATION_SIGNAL_QUARANTINE_NAMESPACE, row["scope"], row["key"],
                                    {"error": parsed})
                    self._transition(db, "review_escalation_signal", row["key"], "escalation_signal_malformed",
                                     {"owner": row["scope"], "error": parsed})
                    continue
                number, reason = parsed
                if self._claim_key(db, ESCALATION_SIGNAL_NAMESPACE, repository, row["key"],
                                   {"pr": number, "reason": reason}):
                    claimed.append(number)
            for number in claimed:
                self._event(db, EscalationRaised("pr", number), correlation=Correlation(pr=number))
        return tuple(claimed)

    @classmethod
    def _recovery_owners(cls, db: sqlite3.Connection, owner_id: str) -> tuple[str, ...]:
        cls._assert_owner_active(db, owner_id)
        repository, separator, _ = owner_id.partition(":")
        return cls._owner_lineage(db, repository, owner_id) if separator else (owner_id,)

    @staticmethod
    def _owner_lineage(db: sqlite3.Connection, repository: str, owner_id: str) -> tuple[str, ...]:
        """This owner and every retired owner of its repository and role that it (transitively) succeeded.

        The reverse of `owns_command`'s walk: a terminal receipt keeps the
        executor that settled it, so a predecessor's unclaimed rows belong
        to its successor. Bounded like that walk.
        """
        role = deployment_role(repository, owner_id)
        lineage, frontier = [owner_id], [owner_id]
        for _ in range(64):
            if not frontier:
                break
            marks = ",".join("?" for _ in frontier)
            found = [row["owner_id"] for row in db.execute(
                f"SELECT owner_id, repository, role FROM retired_owners WHERE successor_owner_id IN ({marks}) "
                "ORDER BY owner_id", frontier,
            ) if canonical_repository(row["repository"]) == repository and row["role"] == role
                and row["owner_id"] not in lineage]
            lineage.extend(found)
            frontier = found
        return tuple(lineage)

    def _claim_key(self, db: sqlite3.Connection, namespace: str, scope: str, key: str, payload: dict) -> bool:
        return db.execute(
            "INSERT OR IGNORE INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
            (namespace, scope, key, self._now().isoformat(), json.dumps(payload, sort_keys=True)),
        ).rowcount == 1

    def unsettled_sibling_dismissals(self, command: LifecycleCommand) -> int:
        """How many dismissals its re-arm still requires that have not succeeded.

        A review re-arm's trigger projection waits on them: restoring the
        trigger while a counted review may still stand would drop the visible
        needs-human-review hold of a re-arm that can still stop (ADR 0015).
        Retired obligations never hold it; a projection outside a re-arm
        (a ceiling release) has no siblings.
        """
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM workflow_rearm_commands projection "
                "JOIN workflow_rearm_commands sibling ON sibling.request_id=projection.request_id "
                "JOIN lifecycle_commands dismissal ON dismissal.command_id=sibling.command_id "
                "WHERE projection.command_id=? AND sibling.disposition='required' AND dismissal.kind=? "
                "AND dismissal.state != ?",
                (str(command.command_id), CommandKind.DISMISS_REVIEW.value, EffectStatus.SUCCEEDED.value),
            ).fetchone()
        return int(row[0])

    def runnable_commands(
        self, *, repository: str, executor_identity: str,
        diagnose: Callable[[str], None],
    ) -> tuple[PendingCommand, ...]:
        """Visible inventory and execution eligibility are deliberately separate.

        A hold is normal state, not a failed attempt. Corrupt authority still
        reports a diagnostic without starving unrelated obligations.
        """
        selected: list[PendingCommand] = []
        diagnostics: list[str] = []
        with self.transaction() as db:
            self._assert_owner_active(db, executor_identity)
            rows = db.execute(
                "SELECT * FROM lifecycle_commands WHERE repository=? AND executor_identity=? "
                "AND state IN ('pending','executing','retryable_failure','unknown_outcome') ORDER BY created_at, command_id",
                (repository, executor_identity),
            ).fetchall()
            for row in rows:
                db.execute("SAVEPOINT runnable_command")
                try:
                    command = _command_from_row(row)
                    reconciliation = self._command_reconciliation(row)
                    if isinstance(reconciliation, (RetiredSettledCommand, RetiredUncertainCommand)):
                        self._require_retirement_evidence(db, command, reconciliation)
                    authorized_now = False
                    if isinstance(reconciliation, (ReconciliationOnly, RetiredUncertainCommand)):
                        gate = self._require_reconciliation_gate(db, command, reconciliation)
                        # A freshly accepted grant is due now, exactly once;
                        # later visits follow the bounded read schedule.
                        grant = self._executable_authorization(db, command.command_id, gate) if isinstance(
                            reconciliation, ReconciliationOnly) else None
                        # Recorded in this transaction, so a grant bypasses the
                        # schedule once however its visit ends.
                        authorized_now = grant is not None and db.execute(
                            "UPDATE workflow_retry_authorizations SET first_visit_at=? "
                            "WHERE authorization_id=? AND disposition IS NULL AND first_visit_at IS NULL",
                            (_stamp(self._now()), grant.authorization_id),
                        ).rowcount == 1
                    if reconciliation is None or isinstance(reconciliation, ActiveReconciliation):
                        if not self._workspace_projection_ready(db, command):
                            continue
                    attempt = self._return_projection_attempt(db, command)
                    if attempt is not None:
                        current = load_resolve_workflow_row(db, attempt.binding.fence.key)
                        if current is None:
                            raise WorkflowAuthorityError("returned command workflow is absent")
                        if current.binding != attempt.binding and not isinstance(reconciliation, (RetiredSettledCommand, RetiredUncertainCommand)):
                            reconciliation = self._retire_return_projection(db, row, command, current)
                        if current.binding == attempt.binding and current.gates and not isinstance(reconciliation, (ReconciliationOnly, RetiredUncertainCommand)):
                            continue
                    if reconciliation is not None and not authorized_now and not reconciliation_due(reconciliation, self._now()):
                        continue
                    selected.append(PendingCommand(command, row["state"], row["attempts"], int(row["refusals"])))
                except (ValueError, TypeError, KeyError, ControlStoreError) as exc:
                    db.execute("ROLLBACK TO runnable_command")
                    diagnostics.append(f"lifecycle command {row['command_id']} cannot be selected: {type(exc).__name__}: {exc}")
                finally:
                    db.execute("RELEASE runnable_command")
        for diagnostic in diagnostics:
            diagnose(diagnostic)
        return tuple(selected)

    def _retire_return_projection(
        self, db: sqlite3.Connection, row: sqlite3.Row,
        command: LifecycleCommand, current: WorkflowSnapshot,
    ) -> CommandReconciliation:
        binding = command.workflow
        assert binding is not None
        scope = workflow_scope(binding.fence.key)
        evidence = json.dumps({"command": str(command.command_id), "captured": encode_workflow(binding),
                               "superseded_by": encode_workflow(current.binding)}, sort_keys=True)
        retirement_id = "command-superseded:" + hashlib.sha256(evidence.encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO workflow_evidence VALUES (?, ?, ?, ?, ?, 'observation', ?)",
                   (retirement_id, *scope, str(command.command_id), evidence))
        return self._retire_workflow_command(db, row, command, current, retirement_id, create_uncertainty=True)

    @classmethod
    def _unresolved_history(cls, row: sqlite3.Row) -> ActiveReconciliation | None:
        """The one durable source of a command's unresolved read history.

        Persisted history wins; otherwise an executing or unknown command has
        been unresolved since the store stamped its current state. That
        fallback relies on no write touching `updated_at` between the
        uncertain receipt and the first persisted history; anything that
        would must persist history first.
        """
        state = cls._command_reconciliation(row)
        if isinstance(state, ActiveReconciliation):
            return state
        if isinstance(state, ReconciliationOnly):
            return state.history
        if isinstance(state, RetiredUncertainCommand):
            return state.uncertainty.history
        if row["state"] not in ("executing", "unknown_outcome"):
            return None
        since = datetime.fromisoformat(row["updated_at"])
        return ActiveReconciliation(since, since, 0)

    @classmethod
    def _schedule_unresolved(
        cls, db: sqlite3.Connection, row: sqlite3.Row, status: EffectStatus | None, now: datetime,
    ) -> None:
        """Enter or leave the bounded pre-expiry schedule with the outcome itself.

        ADR 0015: a workflow command whose outcome becomes unknown persists its
        first unresolved time, next read and read count in the same transaction
        that records the outcome, so it is never re-read on every drain. An
        already-running history keeps its start and count, including across
        a proven-absent re-execution (`retry_reconciled_command` keeps it), so
        repeated unknowns reach the age/read ceiling. A known outcome ends a
        pre-expiry history; expired or retired histories are owned by the
        reconciliation-only and retirement paths.
        """
        if row["workflow_binding_json"] is None:
            return
        prior = cls._command_reconciliation(row)
        if prior is not None and not isinstance(prior, ActiveReconciliation):
            return
        if status is EffectStatus.UNKNOWN_OUTCOME:
            encoded: str | None = encode_workflow(unknown_outcome_history(prior, now))
        else:
            encoded = None
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                   (encoded, row["command_id"]))

    @staticmethod
    def _retry_judged_inapplicable(db: sqlite3.Connection, command_id: CommandId) -> bool:
        return db.execute(
            "SELECT 1 FROM workflow_retry_authorizations WHERE command_id=? AND disposition='inapplicable'",
            (str(command_id),),
        ).fetchone() is not None

    def _retire_workflow_command(
        self, db: sqlite3.Connection, row: sqlite3.Row, command: LifecycleCommand,
        current: AnyWorkflowSnapshot, retirement_id: str, *, create_uncertainty: bool = False,
    ) -> CommandReconciliation:
        """One receipt/history boundary for rearm retirement and supersession."""
        binding = command.workflow
        if binding is None or binding.fence.key != current.binding.fence.key:
            raise WorkflowAuthorityError("retired command belongs to another workflow")
        scope = workflow_scope(binding.fence.key)
        receipt = db.execute("SELECT * FROM effect_receipts WHERE command_id=?", (str(command.command_id),)).fetchone()
        if receipt is not None and (not row["attempt_owner"] or receipt["executor_identity"] != row["attempt_owner"]):
            raise WorkflowAuthorityError("retired obligation receipt does not match its executing owner")
        if row["state"] in ("executing", "unknown_outcome"):
            prior = self._command_reconciliation(row)
            history = prior.history if isinstance(prior, ReconciliationOnly) else prior
            gate = next((opened for item in current.gates
                         if isinstance((opened := item.gate if isinstance(item, RearmingGate) else item).cause, (RemoteOutcomeUncertain, RetryAuthorizationRequired))
                         and opened.cause.command_id == command.command_id), None)
            if gate is None and not create_uncertainty:
                raise WorkflowAuthorityError("retiring an uncertain command requires its visible uncertainty gate")
            if history is None:
                unresolved = self._unresolved_history(row)
                assert unresolved is not None
                since = unresolved.unresolved_since
                if gate is not None and isinstance(gate.cause, RemoteOutcomeUncertain) and gate.cause.unresolved_since != since:
                    raise WorkflowAuthorityError("command uncertainty must retain its recorded start")
                history = ActiveReconciliation(since, max(since, self._now()), gate.cause.failed_reads if gate is not None and isinstance(gate.cause, RemoteOutcomeUncertain) else 0)
            if not isinstance(history, ActiveReconciliation):
                raise WorkflowAuthorityError("superseded command has incompatible uncertainty history")
            if isinstance(prior, ReconciliationOnly) and (gate is None or prior.gate_id != gate.gate_id):
                raise WorkflowAuthorityError("command reconciliation lacks its matching uncertainty gate")
            if gate is not None and isinstance(gate.cause, RemoteOutcomeUncertain) and (
                gate.cause.unresolved_since != history.unresolved_since or gate.cause.failed_reads != history.failed_reads
            ):
                raise WorkflowAuthorityError("retirement cannot reset command uncertainty history")
            gate_id = gate.gate_id if gate is not None else "command-uncertain:" + hashlib.sha256(str(command.command_id).encode()).hexdigest()
            state = RetiredUncertainCommand(ReconciliationOnly(history, gate_id), retirement_id)
            if gate is None:
                self._reconciliation_gate(db, binding, gate_id, OpenGate(
                    gate_id, binding.fence.generation,
                    RemoteOutcomeUncertain(command.command_id, history.unresolved_since, history.failed_reads, "superseded command outcome remains unverified"),
                    NoAuthorizedRemediation("read-only outcome reconciliation required"),
                ))
        else:
            if row["state"] == "pending" and row["attempts"] == 0 and receipt is None:
                outcome = json.dumps({"command": str(command.command_id), "observation": "never_began_locally"}, sort_keys=True)
            elif receipt is not None and receipt["status"] == row["state"] and row["state"] in ("retryable_failure", "permanent_failure", "succeeded"):
                outcome = json.dumps(dict(receipt), sort_keys=True)
            elif row["state"] == "retryable_failure" and receipt is None and (archived := [
                r["evidence_id"] for r in db.execute(
                    "SELECT evidence_id FROM workflow_evidence WHERE command_id=? AND kind='prior_receipt' "
                    "ORDER BY evidence_id", (str(command.command_id),))
            ]):
                # A trusted read proved the unknown attempt absent and archived
                # its receipt (`retry_reconciled_command`); it never settled.
                outcome = json.dumps({"command": str(command.command_id), "observation": "proven_absent_never_settled",
                                      "prior_receipts": archived}, sort_keys=True)
            else:
                raise WorkflowAuthorityError("superseded command lacks settled or uncertain outcome evidence")
            outcome_id = "command-outcome:" + hashlib.sha256(outcome.encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO workflow_evidence VALUES (?, ?, ?, ?, ?, 'observation', ?)",
                       (outcome_id, *scope, str(command.command_id), outcome))
            state = RetiredSettledCommand(retirement_id, outcome_id)
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                   (encode_workflow(state), str(command.command_id)))
        grant = self._open_authorization(db, command.command_id)
        if grant is not None:
            # A retired command can never execute, so its grant ends with it.
            self._dispose_grant(db, grant, RetryDisposition.VOID.value, "the command was retired before the grant was spent",
                                details={"evidence_id": retirement_id})
        self._transition(db, "lifecycle_command", str(command.command_id), "superseded", {"evidence_id": retirement_id})
        return state

    def _reconciliation_gate(
        self, db: sqlite3.Connection, binding: WorkflowBinding, gate_id: str, gate: OpenGate | None,
    ) -> None:
        scope = workflow_scope(binding.fence.key)
        current = load_workflow_row(db, binding.fence.key)
        if current is None:
            raise WorkflowAuthorityError("command reconciliation workflow is absent")
        existing = db.execute("SELECT * FROM workflow_gates WHERE gate_id=?", (gate_id,)).fetchone()
        if existing is not None and (existing["repository"], existing["target_kind"], existing["target_number"]) != scope:
            raise WorkflowAuthorityError("command uncertainty gate belongs to another workflow")
        retained = tuple(item for item in current.gates if (item.gate if isinstance(item, RearmingGate) else item).gate_id != gate_id)
        # Reconciliation is not exempt from the same bounded snapshot contract
        # used by operator and result transitions.
        replace(current, gates=retained + ((gate,) if gate is not None else ()))
        if gate is None:
            changed = db.execute("UPDATE workflow_gates SET state='closed' WHERE gate_id=? AND state<>'closed'", (gate_id,)).rowcount
        else:
            db.execute(
                "INSERT INTO workflow_gates VALUES (?, ?, ?, ?, ?, ?, 'open', ?) "
                "ON CONFLICT(gate_id) DO UPDATE SET opened_generation=excluded.opened_generation, cause_key=excluded.cause_key, state='open', payload_json=excluded.payload_json",
                (gate_id, *scope, gate.opened_generation, hashlib.sha256(encode_workflow(gate.cause).encode()).hexdigest(), encode_workflow(gate)),
            )
            changed = 1
        if changed:
            db.execute("UPDATE workflows SET version=version+1 WHERE repository=? AND target_kind=? AND target_number=?", scope)

    def record_reconciliation_read(
        self, command_id: CommandId, expected: ReconciliationOnly | RetiredUncertainCommand,
        *, expected_attempts: int, read_started_at: datetime, executor_identity: str,
        observed: bool | None, error: str = "", remote_object_id: str | None = None,
    ) -> bool:
        """Persist a harness read, never permission to repeat a remote mutation.

        `expected` and `expected_attempts` were observed before the read: a
        read that began before another attempt cannot settle after it. The
        read is stamped at its start (store clock), the conservative bound.
        An open retry grant accepted before the read began ends when the
        postcondition is present, and otherwise survives unavailable reads
        under the normal backoff up to its bounded ceiling (ADR 0015).
        """
        if observed is not None and not isinstance(observed, bool):
            raise ValueError("postcondition observation must be a boolean or unavailable")
        if type(expected_attempts) is not int or expected_attempts < 0:
            raise ValueError("reconciliation requires the attempt count observed before the read")
        if not isinstance(read_started_at, datetime) or read_started_at.tzinfo is None or read_started_at.utcoffset() is None:
            raise ValueError("reconciliation requires the timezone-aware start of its read")
        with self.transaction() as db:
            self._assert_owner_active(db, executor_identity)
            row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
            if row is None or row["executor_identity"] != executor_identity:
                raise WorkflowAuthorityError("command reconciliation belongs to another executor")
            command = _command_from_row(row)
            binding = command.workflow
            if binding is None:
                raise WorkflowAuthorityError("typed command reconciliation requires workflow authority")
            self._return_projection_attempt(db, command)
            state = self._command_reconciliation(row)
            if state != expected or int(row["attempts"]) != expected_attempts:
                return False  # Another read or attempt already advanced this history.
            self._require_reconciliation_gate(db, command, state)
            if isinstance(state, RetiredUncertainCommand):
                self._require_retirement_evidence(db, command, state)
            uncertainty = state.uncertainty if isinstance(state, RetiredUncertainCommand) else state
            assert isinstance(uncertainty, ReconciliationOnly)
            now = self._now()
            if not 0 <= (now - read_started_at).total_seconds() <= ABSENCE_FRESHNESS_SECONDS:
                raise ValueError("a reconciliation read must be recorded fresh and cannot start in the future")
            grant = self._open_authorization(db, command_id)
            accepted_at = self._grant_accepted_at(db, grant) if grant is not None else None
            # A grant accepted after this read began is not judged by it.
            judged = grant if grant is not None and accepted_at is not None and accepted_at <= read_started_at else None
            evidence_payload = json.dumps({"command": str(command_id), "observed_at": _stamp(read_started_at),
                                           "postcondition_present": observed, "error": error[:4096],
                                           "prior": encode_workflow(state)}, sort_keys=True)
            evidence_id = "command-read:" + hashlib.sha256(evidence_payload.encode()).hexdigest()
            scope = workflow_scope(binding.fence.key)
            progress: ReconciliationProgress | None = None
            keep_gate = False
            if observed is False:
                evidence = CommandAbsenceEvidence(evidence_id, command_id, binding, read_started_at, "current-postcondition:" + hashlib.sha256(evidence_payload.encode()).hexdigest())
                progress = absent_reconciliation(binding, command_id, state, evidence, now=now)
                kind, payload = "absence", encode_workflow(evidence)
            elif observed is None:
                progress = unavailable_reconciliation(binding, command_id, state, now=now, evidence_id=evidence_id, error=error[:4096] or "postcondition verification unavailable")
                kind, payload = "observation", evidence_payload
                if judged is not None:
                    reads = self._count_grant_unavailable(db, judged)
                    void = grant_unavailable_void(reads, accepted_at, now) if accepted_at is not None else None
                    if void is not None:
                        self._dispose_grant(db, judged, RetryDisposition.VOID.value, void, details={"evidence_id": evidence_id})
                    else:
                        # The grant stays spendable: its absence evidence keeps
                        # the gate while the read schedule backs off.
                        keep_gate = True
                        self._transition(db, "workflow_retry_authorization", judged.authorization_id, "deferred", {
                            "command_id": str(command_id), "unavailable_reads": reads,
                            "next_read_at": _stamp(progress.state.history.next_read_at) if isinstance(progress.state, ReconciliationOnly) else None,
                        })
                    judged = None
            else:
                if row["state"] not in ("executing", "unknown_outcome"):
                    raise WorkflowAuthorityError("verified success contradicts a settled command state")
                receipt = EffectReceipt(command_id, executor_identity, now, now, EffectStatus.SUCCEEDED,
                                        remote_object_id=remote_object_id)
                self._replace_effect_receipt(db, command)
                db.execute("UPDATE lifecycle_commands SET attempt_owner=? WHERE command_id=?", (executor_identity, str(command_id)))
                self._insert_effect(db, receipt, _receipt_digest(receipt))
                kind, payload = "observation", evidence_payload
                if isinstance(state, RetiredUncertainCommand):
                    resolved: CommandReconciliation | None = RetiredSettledCommand(state.retirement_evidence_id, evidence_id)
                else:
                    resolved = None
                db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                           (encode_workflow(resolved) if resolved is not None else None, str(command_id)))
                self._reconciliation_gate(db, binding, uncertainty.gate_id, None)
                # Settled: no grant, early or late, can match this command again.
                judged = grant
            db.execute("INSERT OR IGNORE INTO workflow_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (evidence_id, *scope, str(command_id), kind, payload))
            if observed is not True:
                assert progress is not None
                db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                           (encode_workflow(progress.state), str(command_id)))
                if not keep_gate:
                    self._reconciliation_gate(db, binding, uncertainty.gate_id, progress.gate)
            if judged is not None:
                self._dispose_grant(
                    db, judged, "not_needed" if observed is True else "void",
                    "the postcondition is already present" if observed is True else
                    "a newer absence observation superseded the grant before it was spent",
                    details={"evidence_id": evidence_id},
                )
            self._transition(db, "lifecycle_command", str(command_id), "reconciled_read_only", {"evidence_id": evidence_id})
            return observed is True

    def record_scheduled_read(
        self, command_id: CommandId, expected: ActiveReconciliation | None,
        *, expected_attempts: int, read_started_at: datetime, executor_identity: str,
        observed: bool | None, error: str = "", reexecution_ceiling: bool = False,
        remote_object_id: str | None = None,
    ) -> bool:
        """Persist one pre-expiry read of an unresolved workflow command (ADR 0015).

        An unavailable read backs off on the persisted schedule; at the
        configured age/read ceiling the command becomes reconciliation-only
        behind one `RemoteOutcomeUncertain` gate. Verified presence settles the
        same command. Proven absence is recorded here only at the
        re-execution ceiling (``reexecution_ceiling``), where it expires the
        command behind its uncertainty gate; below it, absence is the existing
        authority to re-execute, taken through `retry_reconciled`.
        True means this read settled the command as succeeded.
        """
        if not isinstance(reexecution_ceiling, bool) or (observed is not None and not isinstance(observed, bool)):
            raise ValueError("a scheduled read requires typed observation facts")
        if (observed is False) != reexecution_ceiling:
            raise ValueError("a scheduled read records absence only at the re-execution ceiling")
        if expected is not None and not isinstance(expected, ActiveReconciliation):
            raise ValueError("a scheduled read requires pre-expiry history")
        if type(expected_attempts) is not int or expected_attempts < 0:
            raise ValueError("reconciliation requires the attempt count observed before the read")
        if not isinstance(read_started_at, datetime) or read_started_at.tzinfo is None or read_started_at.utcoffset() is None:
            raise ValueError("reconciliation requires the timezone-aware start of its read")
        with self.transaction() as db:
            self._assert_owner_active(db, executor_identity)
            row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
            if row is None or row["executor_identity"] != executor_identity:
                raise WorkflowAuthorityError("command reconciliation belongs to another executor")
            command = _command_from_row(row)
            binding = command.workflow
            if binding is None:
                raise WorkflowAuthorityError("typed command reconciliation requires workflow authority")
            self._return_projection_attempt(db, command)
            if (row["state"] not in ("executing", "unknown_outcome")
                    or self._command_reconciliation(row) != expected or int(row["attempts"]) != expected_attempts):
                return False  # Another read or attempt already advanced this command.
            now = self._now()
            if not 0 <= (now - read_started_at).total_seconds() <= ABSENCE_FRESHNESS_SECONDS:
                raise ValueError("a reconciliation read must be recorded fresh and cannot start in the future")
            history = expected if expected is not None else self._unresolved_history(row)
            evidence_payload = json.dumps({"command": str(command_id), "observed_at": _stamp(read_started_at),
                                           "postcondition_present": observed, "error": error[:4096],
                                           "prior": encode_workflow(history)}, sort_keys=True)
            evidence_id = "command-read:" + hashlib.sha256(evidence_payload.encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO workflow_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (evidence_id, *workflow_scope(binding.fence.key), str(command_id), "observation", evidence_payload))
            if observed is True:
                receipt = EffectReceipt(command_id, executor_identity, now, now, EffectStatus.SUCCEEDED,
                                        remote_object_id=remote_object_id)
                self._replace_effect_receipt(db, command)
                db.execute("UPDATE lifecycle_commands SET attempt_owner=? WHERE command_id=?", (executor_identity, str(command_id)))
                self._insert_effect(db, receipt, _receipt_digest(receipt))
                self._transition(db, "lifecycle_command", str(command_id), "reconciled_read_only", {"evidence_id": evidence_id})
                return True
            progress = unavailable_reconciliation(
                binding, command_id, history, now=now, evidence_id=evidence_id,
                error=error[:4096] or "postcondition verification unavailable",
                reexecution_ceiling=reexecution_ceiling,
            )
            db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                       (encode_workflow(progress.state), str(command_id)))
            if progress.gate is not None:
                self._reconciliation_gate(db, binding, progress.gate.gate_id, progress.gate)
            state = progress.state
            active = state.history if isinstance(state, ReconciliationOnly) else state
            self._transition(db, "lifecycle_command", str(command_id),
                             "reexecution_ceiling" if reexecution_ceiling else
                             "reconciliation_expired" if isinstance(state, ReconciliationOnly) else "reconciliation_deferred", {
                                 "evidence_id": evidence_id,
                                 "failed_reads": active.failed_reads,
                                 "reexecutions": active.reexecutions,
                             })
            return False

    @staticmethod
    def _grant_accepted_at(db: sqlite3.Connection, grant: UnconsumedRetryAuthorization) -> datetime | None:
        value = db.execute("SELECT accepted_at FROM workflow_retry_authorizations WHERE authorization_id=?",
                           (grant.authorization_id,)).fetchone()[0]
        return datetime.fromisoformat(value) if value is not None else None

    @staticmethod
    def _count_grant_unavailable(db: sqlite3.Connection, grant: UnconsumedRetryAuthorization) -> int:
        db.execute("UPDATE workflow_retry_authorizations SET unavailable_reads=unavailable_reads+1 "
                   "WHERE authorization_id=? AND disposition IS NULL", (grant.authorization_id,))
        return int(db.execute("SELECT unavailable_reads FROM workflow_retry_authorizations WHERE authorization_id=?",
                              (grant.authorization_id,)).fetchone()[0])

    def bound_review_submission(self, command_id: CommandId) -> ReviewSubmissionReceipt | CeilingEscalation | None:
        """Harness-minted evidence of one verified, generation-bound SUBMIT_REVIEW.

        Only this store's own command row and SUCCEEDED receipt can yield it,
        never agent output: the reviewed head and patch identity come from the
        command the executor submitted, and an escalation keeps its cause so a
        ceiling decision can tell it apart. None until the receipt succeeds.
        """
        with closing(self._connect()) as db:
            return load_bound_review_submission(db, command_id)

    @staticmethod
    def _prewrite_read(row: sqlite3.Row) -> ActiveReconciliation | None:
        raw = row["prewrite_read_json"]
        return decode_workflow(raw, ActiveReconciliation) if raw is not None else None

    def prewrite_read(self, command_id: CommandId) -> ActiveReconciliation | None:
        """The bounded schedule of a command whose pre-write guard read was unavailable."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT prewrite_read_json FROM lifecycle_commands WHERE command_id=?",
                             (str(command_id),)).fetchone()
        if row is None:
            raise KeyError(str(command_id))
        return self._prewrite_read(row)

    def record_prewrite_unavailable(
        self, command_id: CommandId, expected: ActiveReconciliation | None, *,
        expected_attempts: int, read_started_at: datetime, executor_identity: str, error: str,
    ) -> bool:
        """Persist one unavailable pre-write guard read of a bound command (ADR 0012).

        The command has not written, so this is not outcome uncertainty: it
        backs off on its own schedule, is journaled, and at the ceiling opens
        a visible `EvidenceUnavailable` gate. Beginning the command (after a
        complete read) ends the schedule and closes the gate. False means
        another read or attempt already advanced the command.
        """
        if expected is not None and not isinstance(expected, ActiveReconciliation):
            raise ValueError("a pre-write read requires its prior schedule")
        if type(expected_attempts) is not int or expected_attempts < 0:
            raise ValueError("a pre-write read requires the attempt count observed before it")
        if not isinstance(read_started_at, datetime) or read_started_at.tzinfo is None or read_started_at.utcoffset() is None:
            raise ValueError("a pre-write read requires the timezone-aware start of its read")
        with self.transaction() as db:
            self._assert_owner_active(db, executor_identity)
            row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
            if row is None or row["executor_identity"] != executor_identity:
                raise WorkflowAuthorityError("pre-write read belongs to another executor")
            command = _command_from_row(row)
            binding = command.workflow
            if binding is None:
                raise WorkflowAuthorityError("a pre-write schedule requires workflow authority")
            # A stale generation cannot begin; it must not open a hold either.
            self._validate_workflow_command(db, command)
            if (row["state"] not in ("pending", "retryable_failure") or self._prewrite_read(row) != expected
                    or int(row["attempts"]) != expected_attempts):
                return False
            now = self._now()
            if not 0 <= (now - read_started_at).total_seconds() <= ABSENCE_FRESHNESS_SECONDS:
                raise ValueError("a pre-write read must be recorded fresh and cannot start in the future")
            progress = unavailable_prewrite_read(binding, command_id, expected, now=now,
                                                 error=error[:4096] or "pre-write guard read unavailable")
            history = progress.state
            assert isinstance(history, ActiveReconciliation)
            db.execute("UPDATE lifecycle_commands SET prewrite_read_json=? WHERE command_id=?",
                       (encode_workflow(history), str(command_id)))
            if progress.gate is not None:
                self._reconciliation_gate(db, binding, progress.gate.gate_id, progress.gate)
            self._transition(db, "lifecycle_command", str(command_id),
                             "prewrite_evidence_gated" if progress.gate is not None else "prewrite_evidence_deferred", {
                                 "failed_reads": history.failed_reads,
                                 "next_read_at": _stamp(history.next_read_at),
                                 "error": error[:512],
                             })
            return True

    def latest_command(self, logical_key: str) -> PendingCommand | None:
        if not logical_key:
            raise ValueError("logical command key is required")
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT * FROM lifecycle_commands WHERE logical_key=? "
                "ORDER BY occurrence DESC LIMIT 1",
                (logical_key,),
            ).fetchone()
        if row is None:
            return None
        return PendingCommand(
            _command_from_row(row),
            str(row["state"]),
            int(row["attempts"]),
            int(row["refusals"]),
        )

    def begin_command(self, command_id: CommandId) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM lifecycle_commands WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(command_id))
            command = _command_from_row(row)
            self._validate_workflow_command(db, command)
            if not self._workspace_projection_ready(db, command):
                raise ControlStoreError("rearm projection awaits verified workspace preparation")
            self._validate_return_projection(db, command)
            self._validate_workflow_reexecution(row)
            if row["state"] not in {"pending", "retryable_failure"}:
                raise ControlStoreError(
                    f"command {command_id} cannot execute from {row['state']}"
                )
            if row["state"] == "retryable_failure":
                self._replace_effect_receipt(db, command)
            self._mark_executing(db, command_id)

    def _mark_executing(self, db: sqlite3.Connection, command_id: CommandId) -> None:
        """The one attempt increment shared by ordinary and authorized execution.

        Beginning ends any pre-write evidence schedule (the executor only
        begins after a complete guard read) and closes its visible hold.
        """
        row = db.execute("SELECT workflow_binding_json, prewrite_read_json FROM lifecycle_commands WHERE command_id=?",
                         (str(command_id),)).fetchone()
        now = _stamp(self._now())
        db.execute(
            "UPDATE lifecycle_commands SET state='executing', attempts=attempts+1, "
            "updated_at=?, attempt_owner=executor_identity, prewrite_read_json=NULL WHERE command_id=?",
            (now, str(command_id)),
        )
        if row is not None and row["prewrite_read_json"] is not None and row["workflow_binding_json"] is not None:
            binding = decode_workflow(row["workflow_binding_json"], WorkflowBinding)
            self._reconciliation_gate(db, binding, prewrite_evidence_gate_id(command_id), None)
        self._transition(
            db,
            "lifecycle_command",
            str(command_id),
            "executing",
        )

    @staticmethod
    def _workspace_projection_ready(db: sqlite3.Connection, command: LifecycleCommand) -> bool:
        """Whether a re-arm obligation may execute now; other commands always may.

        Dispatches on the binding's family: resolve obligations also wait for
        their verified workspace handoff; review obligations have none.
        """
        association = db.execute(
            "SELECT request_id,disposition FROM workflow_rearm_commands WHERE command_id=?",
            (str(command.command_id),),
        ).fetchone()
        pending_type = (
            PendingReviewRearm if command.workflow is not None
            and command.workflow.authority.family is WorkflowFamily.REVIEW_REJECTIONS else PendingRearm
        )
        if association is None:
            if command.workflow is not None:
                current = load_workflow_row(db, command.workflow.fence.key)
                if current is not None and isinstance(current.intent, pending_type) and command.command_id in current.intent.required_commands:
                    raise WorkflowAuthorityError("rearm projection lacks its recorded operation association")
            return True
        if command.workflow is None:
            raise WorkflowAuthorityError("rearm projection lacks its workflow binding")
        current = require_workflow_binding(db, command.workflow, allow_gated=True)
        intent = current.intent
        if (association["disposition"] != "required" or not isinstance(intent, pending_type)
                or intent.request_id != association["request_id"]
                or command.command_id not in intent.required_commands):
            raise WorkflowAuthorityError("rearm projection no longer belongs to a pending authorized intent")
        # ADR 0015: an unresolved prior effect on this workflow could conflict
        # with the replacement projection, so it waits for reconciliation.
        if any(isinstance((g.gate if isinstance(g, RearmingGate) else g).cause, RemoteOutcomeUncertain) for g in current.gates):
            return False
        if isinstance(intent, PendingRearm):
            return load_workspace_ack(db, intent.handoff) is not None
        return True

    @staticmethod
    def _return_projection_attempt(db: sqlite3.Connection, command: LifecycleCommand) -> WorkflowAttemptIdentity | None:
        if not command.idempotency_key.startswith("resolve-return:"):
            return None
        source = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return-command' AND scope=? AND key=?",
            (command.repository, str(command.command_id)),
        ).fetchone()
        if source is None:
            raise ControlStoreError("resolve completion command lacks its paid-return provenance")
        attempt = decode_workflow(source["payload_json"], WorkflowAttemptIdentity)
        if command.workflow != attempt.binding:
            raise ControlStoreError("resolve completion command disagrees with its paid return")
        disposition = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return-disposition' AND scope=? AND key=?",
            (command.repository, str(attempt.run_id)),
        ).fetchone()
        if disposition is None:
            raise ControlStoreError("resolve completion has not been finalized")
        finalized = decode_workflow(disposition["payload_json"], FinalizedResolveReturn)
        if finalized.result.attempt != attempt or command.command_id not in finalized.commands:
            raise ControlStoreError("resolve completion is not an authorized returned-result obligation")
        return attempt

    def _validate_return_projection(self, db: sqlite3.Connection, command: LifecycleCommand) -> None:
        attempt = self._return_projection_attempt(db, command)
        if attempt is None:
            return
        # Classification and its outbox obligation may commit while another
        # gate holds. Only execution needs an unheld current generation.
        require_resolve_binding(db, attempt.binding)

    def retry_reconciled_command(self, command_id: CommandId) -> None:
        """A trusted read proved an ambiguous mutation did not land."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM lifecycle_commands WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(command_id))
            command = _command_from_row(row)
            self._validate_workflow_command(db, command)
            self._validate_workflow_reexecution(row)
            if row["state"] not in {"executing", "unknown_outcome"}:
                raise ControlStoreError(
                    f"command {command_id} cannot be reconciled from {row['state']}"
                )
            self._replace_effect_receipt(db, command)
            db.execute(
                "UPDATE lifecycle_commands SET state='retryable_failure', "
                "updated_at=? WHERE command_id=?",
                (_stamp(self._now()), str(command_id)),
            )
            # The pre-expiry history is kept and charged one re-execution: an
            # attempt that is unknown again continues the same age and read
            # count and backs off exponentially, so the unknown -> absent ->
            # re-execute cycle reaches the ADR 0015 ceiling (unknown outcomes
            # charge no refusals).
            history = self._command_reconciliation(row)
            if isinstance(history, ActiveReconciliation):
                db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                           (encode_workflow(absent_reexecution(history)), str(command_id)))
            self._transition(
                db,
                "lifecycle_command",
                str(command_id),
                "retryable_failure",
                {"reason": "authoritative verification found no effect"},
            )

    @staticmethod
    def _validate_workflow_reexecution(row: sqlite3.Row) -> None:
        raw = row["workflow_reconciliation_json"]
        if raw is not None:
            try:
                decode_workflow(raw, ActiveReconciliation)
            except ValueError as exc:
                raise ControlStoreError("workflow command is reconciliation-only or retired; execution is not authorized") from exc

    @staticmethod
    def _retry_authorization_row(db: sqlite3.Connection, authorization_id: str) -> RetryAuthorization | None:
        row = db.execute(
            "SELECT * FROM workflow_retry_authorizations WHERE authorization_id=?", (authorization_id,),
        ).fetchone()
        return None if row is None else _retry_authorization_from_row(row)

    @staticmethod
    def _open_authorization(db: sqlite3.Connection, command_id: CommandId) -> UnconsumedRetryAuthorization | None:
        # A unique partial index admits at most one open grant per command.
        row = db.execute(
            "SELECT * FROM workflow_retry_authorizations WHERE command_id=? AND disposition IS NULL",
            (str(command_id),),
        ).fetchone()
        if row is None:
            return None
        grant = _retry_authorization_from_row(row)
        assert isinstance(grant, UnconsumedRetryAuthorization)
        return grant

    def _executable_authorization(
        self, db: sqlite3.Connection, command_id: CommandId, gate: OpenGate,
    ) -> UnconsumedRetryAuthorization | None:
        """An open grant still naming the gate's current absence observation."""
        if not isinstance(gate.cause, RetryAuthorizationRequired) or gate.cause.command_id != command_id:
            return None
        grant = self._open_authorization(db, command_id)
        if grant is None or (grant.gate_id, grant.evidence_id) != (gate.gate_id, gate.cause.absence_evidence_id):
            return None
        return grant

    def retry_authorization(self, command_id: CommandId) -> UnconsumedRetryAuthorization | None:
        """The open grant awaiting the executor's one spend decision (read-only).

        It is returned even when newer evidence moved the gate on, so the
        executor's next read ends it rather than leaving it matchable forever.
        """
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
            if row is None:
                raise KeyError(str(command_id))
            state = self._command_reconciliation(row)
            if not isinstance(state, ReconciliationOnly):
                return None
            self._require_reconciliation_gate(db, _command_from_row(row), state)
            return self._open_authorization(db, command_id)

    def _retry_grant(
        self, db: sqlite3.Connection, row: sqlite3.Row | None, authorization_id: str, executor_identity: str,
    ) -> RetryAuthorization:
        self._assert_owner_active(db, executor_identity)
        if row is None or row["executor_identity"] != executor_identity:
            raise WorkflowAuthorityError("authorized retry belongs to another executor")
        stored = self._retry_authorization_row(db, authorization_id)
        if stored is None or _authorization(stored).command_id != row["command_id"]:
            raise WorkflowAuthorityError("authorized retry lacks its persisted operator grant")
        return stored

    def _dispose_grant(
        self, db: sqlite3.Connection, grant: UnconsumedRetryAuthorization, disposition: str, reason: str,
        *, attempt: int | None = None, details: dict[str, object] | None = None,
    ) -> None:
        """Every grant ends here exactly once; the schema refuses a second disposal."""
        if db.execute(
            "UPDATE workflow_retry_authorizations SET disposition=?, disposition_reason=?, disposed_at=?, "
            "consumed_attempt=? WHERE authorization_id=? AND disposition IS NULL",
            (disposition, reason[:4096], _stamp(self._now()), attempt, grant.authorization_id),
        ).rowcount != 1:
            raise WorkflowAuthorityError("retry grant was disposed concurrently")
        self._transition(db, "workflow_retry_authorization", grant.authorization_id, disposition, {
            "command_id": str(grant.command_id), "reason": reason[:4096], **(details or {}),
        })

    @staticmethod
    def _read_absence(command: LifecycleCommand, authorization_id: str, read: RetryAbsenceRead) -> CommandAbsenceEvidence:
        """Evidence names the executor's actual read: its time and observed facts."""
        assert command.workflow is not None
        facts = json.dumps({
            "command": str(command.command_id), "authorization_id": authorization_id,
            "observed_at": _stamp(read.observed_at), "postcondition_present": False,
            "target_revision": read.target_revision, "guard_holds": read.guard_holds,
        }, sort_keys=True)
        digest = hashlib.sha256(facts.encode()).hexdigest()
        return CommandAbsenceEvidence(
            "retry-absence:" + digest, command.command_id, command.workflow, read.observed_at,
            "current-postcondition:" + digest,
        )

    def _insert_retry_evidence(
        self, db: sqlite3.Connection, command: LifecycleCommand, kind: str, evidence_id: str, payload: str,
    ) -> None:
        assert command.workflow is not None
        db.execute("INSERT OR IGNORE INTO workflow_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (evidence_id, *workflow_scope(command.workflow.fence.key), str(command.command_id), kind, payload))
        stored = db.execute("SELECT kind,payload_json FROM workflow_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if stored is None or (stored["kind"], stored["payload_json"]) != (kind, payload):
            raise WorkflowAuthorityError("retry evidence identity contains different facts")

    def _attempt_uncertainty(
        self, db: sqlite3.Connection, command: LifecycleCommand, state: ReconciliationOnly, detail: str,
    ) -> None:
        """Report an authorized attempt's unverified outcome on its retained gate.

        Unresolved history is kept; the rewritten state makes every read taken
        before this point fail its compare-and-set, and the gate stops showing
        the absence the attempt already spent.
        """
        binding = command.workflow
        assert binding is not None
        history = state.history
        rotated = ReconciliationOnly(replace(history, next_read_at=max(
            self._now() + timedelta(seconds=ReconciliationPolicy().initial_delay_seconds), history.unresolved_since,
        )), state.gate_id)
        db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                   (encode_workflow(rotated), str(command.command_id)))
        self._reconciliation_gate(db, binding, state.gate_id, OpenGate(
            state.gate_id, binding.fence.generation,
            RemoteOutcomeUncertain(command.command_id, history.unresolved_since, history.failed_reads, detail),
            NoAuthorizedRemediation("read-only outcome reconciliation required"),
        ))

    def begin_authorized_retry(
        self, command_id: CommandId, authorization_id: str, *,
        expected: ReconciliationOnly, expected_attempts: int, read: RetryAbsenceRead, executor_identity: str,
    ) -> RetryBegan | RetryAlreadyConsumed | RetryDeclined | RetryStale:
        """Make the grant's one spend decision atomically (ADR 0015).

        The caller holds the shared deployment effect authority and has just
        read current absence and re-evaluated the original decision. The pure
        spend decision either consumes the grant with the attempt increment and
        receipt archival, or ends it inapplicable/void with persisted evidence;
        no refusal is left to repeat. A crash before this commits leaves the
        grant open; a crash after it leaves read-only reconciliation.
        """
        if not isinstance(expected, ReconciliationOnly) or not isinstance(read, RetryAbsenceRead):
            raise ValueError("authorized retry requires the command's reconciliation-only state and its read")
        if type(expected_attempts) is not int or expected_attempts < 0:
            raise ValueError("authorized retry requires the observed attempt count")
        validate_workflow_identifier(authorization_id, "retry authorization")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
            stored = self._retry_grant(db, row, authorization_id, executor_identity)
            if isinstance(stored, ConsumedRetryAuthorization):
                return RetryAlreadyConsumed(stored)
            if isinstance(stored, ClosedRetryAuthorization):
                return RetryDeclined(stored)
            if self._command_reconciliation(row) != expected or int(row["attempts"]) != expected_attempts:
                return RetryStale()
            command = _command_from_row(row)
            binding = command.workflow
            if binding is None:
                raise WorkflowAuthorityError("authorized retry requires captured workflow authority")
            self._require_reconciliation_gate(db, command, expected)
            if row["state"] not in ("executing", "unknown_outcome"):
                raise WorkflowAuthorityError("reconciliation-only command lacks an unresolved outcome")
            current = load_workflow_row(db, binding.fence.key)
            if current is None:
                raise WorkflowAuthorityError("authorized retry workflow is absent")
            facts = load_retry_authorization_facts(self, db, command.command_id, stored.evidence_id)
            # One grant contract for both families (ADR 0015): the review
            # executor's read re-evaluated the command's own accepted guard.
            decision = retry_spend_decision(current, stored, facts, read, now=self._now(),
                                            effect_owed=self._effect_owed(db, command, current))
            if isinstance(decision, DeclineRetry):
                return RetryDeclined(self._decline_retry(db, row, command, current, stored, expected, read, decision))
            evidence = self._read_absence(command, authorization_id, read)
            applicability = CommandRetryApplicability(
                evidence.evidence_id.replace("retry-absence:", "retry-applicability:", 1), command.command_id,
                current.binding, read.observed_at, str(row["payload_digest"]),
            )
            self._insert_retry_evidence(db, command, "absence", evidence.evidence_id, encode_workflow(evidence))
            self._insert_retry_evidence(db, command, "retry_applicability", applicability.evidence_id, encode_workflow(applicability))
            attempt = expected_attempts + 1
            self._dispose_grant(db, stored, "consumed", "authorized attempt began", attempt=attempt, details={
                "attempt": attempt, "absence_evidence_id": evidence.evidence_id,
                "applicability_evidence_id": applicability.evidence_id,
            })
            spent = int(db.execute("SELECT count(*) FROM workflow_retry_authorizations "
                                   "WHERE command_id=? AND disposition='consumed'", (str(command_id),)).fetchone()[0])
            if spent >= MAX_AUTHORIZED_ATTEMPTS:
                self._transition(db, "workflow_retry_authorization", authorization_id, "ceiling_reached", {
                    "command_id": str(command_id), "authorized_attempts": spent, "ceiling": MAX_AUTHORIZED_ATTEMPTS,
                })
            self._replace_effect_receipt(db, command)
            self._mark_executing(db, command_id)
            self._attempt_uncertainty(db, command, expected, f"authorized retry attempt {attempt} began; outcome unverified")
            return RetryBegan(ConsumedRetryAuthorization(stored, attempt))

    @staticmethod
    def _effect_owed(db: sqlite3.Connection, command: LifecycleCommand, current: AnyWorkflowSnapshot) -> bool:
        """Whether the workflow still owes a granted command's effect, read in the spend's transaction.

        Only a bound review submission can stop being owed: a newer verdict
        at the same head that landed (or was recorded) makes a re-write a
        stale second verdict (ADR 0015).
        """
        submission = bound_submission_op(command)
        if submission is None:
            return True
        binding = command.workflow
        if binding is None or not isinstance(current, ReviewWorkflowSnapshot):
            raise WorkflowAuthorityError("a bound review submission requires its review workflow")
        settled_sibling = False
        for sibling in db.execute(
            "SELECT * FROM lifecycle_commands WHERE repository=? AND target=? AND kind=? AND command_id<>? "
            "AND state='succeeded'",
            (command.repository, command.target, CommandKind.SUBMIT_REVIEW.value, str(command.command_id)),
        ).fetchall():
            other = _command_from_row(sibling)
            if other.workflow == binding and other.expected_revision == submission.head:
                settled_sibling = True
                break
        return bound_submission_owed(current, binding, submission.head, submission.patch_id,
                                     settled_sibling=settled_sibling)

    def _decline_retry(
        self, db: sqlite3.Connection, row: sqlite3.Row, command: LifecycleCommand, current: AnyWorkflowSnapshot,
        stored: UnconsumedRetryAuthorization, expected: ReconciliationOnly, read: RetryAbsenceRead,
        decision: DeclineRetry,
    ) -> ClosedRetryAuthorization:
        """End the grant unspent and persist what the read proved.

        A fresh absence rotates the gate evidence and advances the bounded read
        schedule. An inapplicable standalone command retires; an inapplicable
        re-arm obligation keeps its restriction and can never be granted again.
        """
        binding = command.workflow
        assert binding is not None
        payload = json.dumps({"command": str(command.command_id), "authorization_id": stored.authorization_id,
                              "disposition": decision.disposition.value, "reason": decision.reason[:4096],
                              "observed_at": _stamp(read.observed_at), "target_revision": read.target_revision},
                             sort_keys=True)
        refusal_id = "retry-declined:" + hashlib.sha256(payload.encode()).hexdigest()
        self._insert_retry_evidence(db, command, "retry_applicability", refusal_id, payload)
        self._dispose_grant(db, stored, decision.disposition.value, decision.reason, details={
            "evidence_id": refusal_id,
            "association": "rearm" if isinstance(stored.association, RearmObligation) else "standalone",
        })
        state: CommandReconciliation = expected
        if decision.disposition is RetryDisposition.INAPPLICABLE and isinstance(stored.association, StandaloneCommand):
            state = self._retire_workflow_command(db, row, command, current, refusal_id)
            if not isinstance(state, RetiredUncertainCommand):
                raise WorkflowAuthorityError("inapplicable retry must retire an unresolved command")
        if decision.fresh_read:
            assert isinstance(state, (ReconciliationOnly, RetiredUncertainCommand))
            evidence = self._read_absence(command, stored.authorization_id, read)
            self._insert_retry_evidence(db, command, "absence", evidence.evidence_id, encode_workflow(evidence))
            progress = absent_reconciliation(binding, command.command_id, state, evidence, now=self._now())
            db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
                       (encode_workflow(progress.state), str(command.command_id)))
            self._reconciliation_gate(db, binding, expected.gate_id, progress.gate)
        return ClosedRetryAuthorization(stored, decision.disposition, decision.reason[:4096])

    def _settle_authorized_retry(self, db: sqlite3.Connection, receipt: EffectReceipt) -> None:
        row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(receipt.command_id),)).fetchone()
        state = self._command_reconciliation(row)
        if not isinstance(state, ReconciliationOnly):
            return
        grant = db.execute(
            "SELECT authorization_id FROM workflow_retry_authorizations "
            "WHERE command_id=? AND disposition='consumed' AND consumed_attempt=?",
            (str(receipt.command_id), int(row["attempts"])),
        ).fetchone()
        if grant is None:
            return
        command = _command_from_row(row)
        assert command.workflow is not None
        if receipt.status is EffectStatus.SUCCEEDED:
            db.execute("UPDATE lifecycle_commands SET workflow_reconciliation_json=NULL WHERE command_id=?",
                       (str(receipt.command_id),))
            self._reconciliation_gate(db, command.workflow, state.gate_id, None)
        elif receipt.status is EffectStatus.UNKNOWN_OUTCOME:
            # Original expiry history is retained; any absence read during the
            # write is superseded, so a later write needs a new proof and grant.
            self._attempt_uncertainty(db, command, state, f"authorized retry attempt {int(row['attempts'])} outcome unknown")
        else:
            raise ControlStoreError("an authorized retry settles only as verified success or unknown outcome")
        self._transition(db, "workflow_retry_authorization", grant["authorization_id"],
                         "succeeded" if receipt.status is EffectStatus.SUCCEEDED else "unresolved",
                         {"command_id": str(receipt.command_id), "attempt": int(row["attempts"])})

    @staticmethod
    def _archive_workflow_receipt(db: sqlite3.Connection, command: LifecycleCommand) -> None:
        if command.workflow is None:
            return
        receipt = db.execute("SELECT * FROM effect_receipts WHERE command_id=?", (str(command.command_id),)).fetchone()
        if receipt is None:
            return
        payload = _json(dict(receipt))
        evidence_id = hashlib.sha256(f"prior-receipt:{command.command_id}:{payload}".encode()).hexdigest()
        # Scoped by the binding's own target: a pull request workflow's receipt
        # is never filed under an issue of the same number.
        db.execute(
            "INSERT OR IGNORE INTO workflow_evidence(evidence_id, repository, target_kind, target_number, command_id, kind, payload_json) "
            "VALUES (?, ?, ?, ?, ?, 'prior_receipt', ?)",
            (evidence_id, command.repository, *workflow_scope(command.workflow.fence.key)[1:], str(command.command_id), payload),
        )

    def _replace_effect_receipt(self, db: sqlite3.Connection, command: LifecycleCommand) -> None:
        """All receipt replacement paths retain migrated authority evidence."""
        if "steward_guard" in command.payload or command.idempotency_key.startswith("steward-verdict:"):
            self._validate_steward_command(db, command, settling=True)
        self._archive_workflow_receipt(db, command)
        db.execute("DELETE FROM effect_receipts WHERE command_id=?", (str(command.command_id),))

    def resolve_unknown_effect(self, receipt: EffectReceipt) -> bool:
        """Replace an ambiguous receipt after authoritative remote verification.

        True means THIS call performed the reconciliation. False means the
        command was ALREADY settled `succeeded` by another actor and this call
        is a no-op that asserts the same outcome — benign, not an error.

        The False answer exists because command enumeration and reconciliation
        cannot share a transaction: `pending_commands` returns a snapshot, and
        the reconciling reads between it and this call are remote. keifu PR
        #177 (2026-08-02): the executing process submitted the review and
        committed its succeeded receipt while a second drain — hot reload makes
        two drains overlap routinely — was mid-`has_review` on the same
        command. The second drain then verified the very effect the first had
        just settled and got "command is not awaiting reconciliation", a scary
        line for an outcome both actors agreed on. Two actors independently
        proving the same success is a benign convergence; the earlier receipt
        is authoritative (it carries the real execution window) and is kept.

        Every other non-reconcilable state still RAISES. `pending` means a
        receipt exists for a command that never executed; `retryable_failure` /
        `permanent_failure` mean a verified success contradicts a recorded
        failure; a missing row means the receipt has no command. Those are
        corruption, not convergence, and must stay loud. `succeeded` without a
        succeeded receipt is likewise corruption — it is the settled state
        without the evidence that produces it.

        Convergence is also NOT assumed when the two actors demonstrably
        produced DIFFERENT remote objects. `reconcile_async`'s "reconciling
        read" is not always read-only: the Jira workflow's is a paid
        search-then-create MCP leg, so two overlapping workflows can create
        PROJ-100 and PROJ-101 for one command. Silently answering False there
        would let the caller persist the key the authoritative receipt does
        not name, orphaning a paid duplicate with no log line. Differing
        non-null `remote_object_id`s therefore raise: that is provable
        divergence, not convergence. Either side null carries no evidence of
        disagreement (most effects never name a remote object) and stays
        benign.
        """

        if receipt.status is not EffectStatus.SUCCEEDED:
            raise ValueError("unknown effect can only resolve to succeeded here")
        with self.transaction() as db, self.mutation_savepoint(db):
            row = db.execute(
                "SELECT * FROM lifecycle_commands WHERE command_id=?",
                (str(receipt.command_id),),
            ).fetchone()
            if row is None:
                raise ControlStoreError("command is not awaiting reconciliation")
            command = _command_from_row(row)
            if "steward_guard" in command.payload:
                self._require_steward_command_source(db, command)
            state = str(row["state"])
            if state == EffectStatus.SUCCEEDED.value:
                settled = db.execute(
                    "SELECT status, remote_object_id FROM effect_receipts "
                    "WHERE command_id=?",
                    (str(receipt.command_id),),
                ).fetchone()
                if (
                    settled is None
                    or str(settled["status"]) != EffectStatus.SUCCEEDED.value
                ):
                    raise ControlStoreError(
                        "command is succeeded without a succeeded receipt"
                    )
                surviving = settled["remote_object_id"]
                if (
                    surviving
                    and receipt.remote_object_id
                    and str(surviving) != str(receipt.remote_object_id)
                ):
                    raise ControlStoreError(
                        "command already succeeded against a different remote "
                        f"object: {surviving} != {receipt.remote_object_id}"
                    )
                return False
            if state not in {"executing", EffectStatus.UNKNOWN_OUTCOME.value}:
                raise ControlStoreError("command is not awaiting reconciliation")
            command = _command_from_row(row)
            if command.workflow is not None or "steward_guard" in command.payload:
                self._assert_owner_active(db, command.executor_identity)
                if receipt.executor_identity != command.executor_identity:
                    raise ControlStoreError("workflow reconciliation receipt requires the current command owner")
                db.execute("UPDATE lifecycle_commands SET attempt_owner=? WHERE command_id=?", (receipt.executor_identity, str(receipt.command_id)))
            self._replace_effect_receipt(db, command)
            self._insert_effect(db, receipt, _receipt_digest(receipt))
            return True

    def exhaust_command(self, receipt: EffectReceipt) -> bool:
        """Retire a command that has burned its retry ceiling (ADR 0012).

        A remote write GitHub keeps refusing for a permanent reason — the
        keifu incident: a review identity whose PAT lacked Issues:Write, so
        every relabel returned 403, verified as "not applied", and was
        re-executed on every tick forever — is not retryable. Crossing the
        ceiling is the only transition into `permanent_failure` from
        `retryable_failure`, and it is a REPLACEMENT of the last failure
        receipt so the command keeps exactly one receipt.

        The lifecycle_commands row (payload included) is deliberately left in
        place: once the operator fixes the identity they must still be able to
        see what never posted. `enqueue_command_occurrence` is the existing
        primitive for deliberately re-running such an effect.

        Returns False for ANY state that is no longer `retryable_failure` —
        another daemon retired it, or the write actually landed between the
        ceiling test and this call. Neither is an error: a concurrent SUCCESS
        raising here would surface as an exception out of `execute_all` and
        charge the caller a spurious stall. The False answer means "not retired
        by me", which is exactly what a once-only escalation needs.
        """

        if receipt.status is not EffectStatus.PERMANENT_FAILURE:
            raise ValueError("an exhausted command must receipt permanent_failure")
        with self.transaction() as db, self.mutation_savepoint(db):
            row = db.execute(
                "SELECT * FROM lifecycle_commands WHERE command_id=?",
                (str(receipt.command_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(receipt.command_id))
            command = _command_from_row(row)
            if "steward_guard" in command.payload:
                self._require_steward_command_source(db, command)
            if row["state"] != EffectStatus.RETRYABLE_FAILURE.value:
                return False
            command = _command_from_row(row)
            if command.workflow is not None or "steward_guard" in command.payload:
                self._assert_owner_active(db, command.executor_identity)
                if receipt.executor_identity != command.executor_identity:
                    raise ControlStoreError("workflow exhaustion receipt requires the current command owner")
                db.execute("UPDATE lifecycle_commands SET attempt_owner=? WHERE command_id=?", (receipt.executor_identity, str(receipt.command_id)))
            if "steward_guard" in command.payload:
                captured = db.execute("SELECT remote_revision FROM effect_receipts WHERE command_id=?", (str(receipt.command_id),)).fetchone()
                retained = captured[0] if captured is not None else None
                if receipt.remote_revision is not None and receipt.remote_revision != retained:
                    raise WorkflowAuthorityError("steward exhaustion changed its captured partial identity")
                receipt = replace(receipt, remote_revision=retained)
            self._replace_effect_receipt(db, command)
            self._insert_effect(db, receipt, _receipt_digest(receipt))
            return True

    def record_effect(self, receipt: EffectReceipt) -> bool:
        digest = _receipt_digest(receipt)
        with self.transaction() as db:
            row = db.execute(
                "SELECT receipt_digest FROM effect_receipts WHERE command_id=?",
                (str(receipt.command_id),),
            ).fetchone()
            if row is not None:
                command_row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(receipt.command_id),)).fetchone()
                if command_row is not None:
                    command = _command_from_row(command_row)
                    if "steward_guard" in command.payload or command.idempotency_key.startswith("steward-verdict:"):
                        self._require_steward_command_source(db, command)
                return row["receipt_digest"] == digest
            self._insert_effect(db, receipt, digest)
            self._settle_authorized_retry(db, receipt)
            return True

    def _insert_effect(
        self, db: sqlite3.Connection, receipt: EffectReceipt, digest: str
    ) -> None:
        command_row = db.execute(
            "SELECT * FROM lifecycle_commands WHERE command_id=?", (str(receipt.command_id),),
        ).fetchone()
        if command_row is None:
            raise ControlStoreError(f"receipt {receipt.command_id} has no lifecycle command")
        command = _command_from_row(command_row)
        if "steward_guard" in command.payload or command.idempotency_key.startswith("steward-verdict:"):
            self._validate_steward_command(db, command, settling=True, refusing=receipt.status is EffectStatus.PERMANENT_FAILURE)
            if not command_row["attempt_owner"] or receipt.executor_identity != command_row["attempt_owner"]:
                raise ControlStoreError("steward effect receipt does not match its captured attempt owner")
        # Retiring/adopting a deployment changes the command executor, not the
        # identity that actually began this attempt and may receipt its result.
        if command.workflow is not None and (not command_row["attempt_owner"] or receipt.executor_identity != command_row["attempt_owner"]):
            raise ControlStoreError("workflow effect receipt does not match the captured attempt owner")
        if command.kind is CommandKind.SUBMIT_REVIEW and receipt.remote_object_id is not None:
            # Defence in depth behind the review marker: one GitHub review is
            # the receipt of at most one submission, so two verdicts can never
            # both be settled by the same review.
            holder = db.execute(
                "SELECT r.command_id FROM effect_receipts r JOIN lifecycle_commands c ON c.command_id=r.command_id "
                "WHERE r.remote_object_id=? AND r.command_id<>? AND c.kind=? AND c.repository=? LIMIT 1",
                (receipt.remote_object_id, str(receipt.command_id), CommandKind.SUBMIT_REVIEW.value, command.repository),
            ).fetchone()
            if holder is not None:
                raise ControlStoreError(
                    f"review {receipt.remote_object_id} is already the receipt of command {holder['command_id']}"
                )
        db.execute(
            "INSERT INTO effect_receipts(command_id, executor_identity, "
            "started_at, completed_at, status, remote_object_id, "
            "remote_revision, error, receipt_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(receipt.command_id),
                receipt.executor_identity,
                _stamp(receipt.started_at),
                _stamp(receipt.completed_at),
                receipt.status.value,
                receipt.remote_object_id,
                receipt.remote_revision,
                receipt.error,
                digest,
            ),
        )
        # `refusals` counts only outcomes GitHub actually refused — a readback
        # that proved the requested state absent. A SUCCEEDED receipt clears it
        # (the command is done, and a later occurrence starts clean); every
        # other status leaves it UNCHANGED. Unknown outcomes carry no
        # information about the token's permissions, so neither charging them
        # (which lets an outage retire a good command) nor clearing on them
        # (which lets a flaky network make the ceiling unreachable for a
        # permanently denied write) is correct — ADR 0012.
        now = self._now()
        db.execute(
            "UPDATE lifecycle_commands SET state=?, updated_at=?, "
            "refusals=CASE ?"
            " WHEN 'retryable_failure' THEN refusals+1"
            " WHEN 'succeeded' THEN 0"
            " ELSE refusals END "
            "WHERE command_id=?",
            (
                receipt.status.value,
                _stamp(now),
                receipt.status.value,
                str(receipt.command_id),
            ),
        )
        self._schedule_unresolved(db, command_row, receipt.status, now)
        if receipt.status is EffectStatus.SUCCEEDED:
            self._event(
                db,
                ActionApplied(
                    str(receipt.command_id),
                    command.kind.value,
                    command.target,
                    command.kind.value,
                ),
                correlation=_command_correlation(command),
            )
        else:
            self._event(
                db,
                ActionFailed(
                    str(receipt.command_id),
                    command.kind.value,
                    receipt.status.value,
                    receipt.error or "",
                ),
                correlation=_command_correlation(command),
            )
        self._transition(
            db,
            "lifecycle_command",
            str(receipt.command_id),
            receipt.status.value,
            {
                "kind": command.kind.value,
                "target": command.target,
                "executor_identity": receipt.executor_identity,
            },
            correlation=_command_correlation(command),
        )

    def effect_receipt(self, command_id: CommandId) -> EffectReceipt | None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT * FROM effect_receipts WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
        if row is None:
            return None
        return EffectReceipt(
            command_id=CommandId(row["command_id"]),
            executor_identity=row["executor_identity"],
            started_at=_parse_stamp(row["started_at"]),
            completed_at=_parse_stamp(row["completed_at"]),
            status=EffectStatus(row["status"]),
            remote_object_id=row["remote_object_id"],
            remote_revision=row["remote_revision"],
            error=row["error"],
        )

    def enqueue_event(self, event: EventEnvelope, db: sqlite3.Connection | None = None) -> None:
        encoded = encode_event_line(event)
        encoded_bytes = len((encoded + "\n").encode())
        if encoded_bytes > self._event_line_limit:
            raise ValueError(
                f"outbox event line is {encoded_bytes} bytes; "
                f"limit is {self._event_line_limit}"
            )

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR IGNORE INTO event_outbox("
                "event_id, destination, event_json, committed_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    event.producer.feed_identity,
                    encoded,
                    _stamp(self._now()),
                ),
            )

        if db is not None:
            insert(db)
            return
        with self.transaction() as connection:
            insert(connection)

    def flush_outbox(
        self,
        emit: Callable[[EventEnvelope], None],
        limit: int = 100,
        *,
        destination: str | None = None,
    ) -> int:
        """Emit committed rows in order and acknowledge each only after success."""

        if limit <= 0:
            return 0
        emitted = 0
        processed = 0
        while processed < limit:
            with closing(self._connect()) as db, db:
                row = (
                    db.execute(
                        "SELECT outbox_id, event_json FROM event_outbox "
                        "WHERE emitted_at IS NULL AND failed_at IS NULL "
                        "AND destination=? ORDER BY outbox_id LIMIT 1",
                        (destination,),
                    ).fetchone()
                    if destination is not None
                    else db.execute(
                        "SELECT outbox_id, event_json FROM event_outbox "
                        "WHERE emitted_at IS NULL AND failed_at IS NULL "
                        "ORDER BY outbox_id LIMIT 1"
                    ).fetchone()
                )
            if row is None:
                break
            processed += 1
            try:
                if len((row["event_json"] + "\n").encode()) > self._event_line_limit:
                    raise ValueError("event exceeds configured line limit")
                event = decode_event_line(row["event_json"])
            except (TypeError, ValueError) as exc:
                with self.transaction() as db:
                    db.execute(
                        "UPDATE event_outbox SET failed_at=?, failure=? "
                        "WHERE outbox_id=? AND failed_at IS NULL",
                        (
                            _stamp(self._now()),
                            str(exc)[:500],
                            row["outbox_id"],
                        ),
                    )
                continue
            emit(event)
            with self.transaction() as db:
                db.execute(
                    "UPDATE event_outbox SET emitted_at=? "
                    "WHERE outbox_id=? AND emitted_at IS NULL",
                    (_stamp(self._now()), row["outbox_id"]),
                )
            emitted += 1
        return emitted

    def backup(self, destination: str) -> None:
        target = os.path.abspath(destination)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with closing(self._connect()) as source, closing(sqlite3.connect(target)) as dest, source, dest:
            source.backup(dest)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)

    def import_legacy_spend(self, source_path: str) -> MigrationReport:
        """Import an append-only legacy ledger without resetting live caps."""

        source = Path(source_path)
        raw = source.read_bytes()
        source_digest = hashlib.sha256(raw).hexdigest()
        source_identity = str(source.resolve())
        migration_id = "legacy-spend:" + hashlib.sha256(
            f"{source_identity}:{source_digest}".encode()
        ).hexdigest()
        legacy_migration_id = f"legacy-spend:{source_digest}"
        parsed: list[tuple[str, str, int, int, datetime, str]] = []
        occurrences: dict[str, int] = {}
        rejected = 0
        for line in raw.splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("event") != "spend":
                    raise ValueError("not a spend row")
                stamp = _parse_stamp(str(row["ts"]))
                repository = row.get("repo")
                issue = row.get("issue")
                if (
                    not isinstance(repository, str)
                    or not repository.strip()
                    or repository == "<unattributed>"
                    or isinstance(issue, bool)
                    or not isinstance(issue, int)
                    or issue <= 0
                ):
                    raise ValueError("spend scope is unattributed")
                amount = Decimal(
                    str(row.get("billed_cost_usd", row["cost_usd"]))
                )
                if not amount.is_finite() or amount < 0:
                    raise ValueError("amount must be finite and nonnegative")
                micros = int(
                    (amount * Decimal(1_000_000)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                normalized_repository = canonical_repository(repository)
                model = str(row.get("model") or "<unknown>")
                normalized_stamp = _stamp(stamp)
                semantic_row = json.dumps(
                    {
                        "amount_micros": micros,
                        "issue": issue,
                        "model": model,
                        "repository": normalized_repository,
                        "settled_at": normalized_stamp,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                occurrence = occurrences.get(semantic_row, 0) + 1
                occurrences[semantic_row] = occurrence
                row_identity = hashlib.sha256(
                    f"{source_identity}:{semantic_row}:{occurrence}".encode()
                ).hexdigest()
                run_id = hashlib.sha256(
                    f"legacy-spend-row:{row_identity}".encode()
                ).hexdigest()
                parsed.append(
                    (
                        run_id,
                        normalized_repository,
                        issue,
                        micros,
                        stamp,
                        model,
                    )
                )
            except (
                InvalidOperation,
                KeyError,
                OverflowError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                rejected += 1

        if rejected:
            raise LegacySpendMigrationError(
                f"{source}: rejected {rejected} legacy spend row(s); "
                "no rows were imported"
            )

        imported = 0
        total = 0
        with self.transaction() as db:
            existing = db.execute(
                "SELECT migration_id, row_count, rejected_count, amount_micros "
                "FROM migration_imports WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if existing is None:
                existing = db.execute(
                    "SELECT migration_id, row_count, rejected_count, "
                    "amount_micros FROM migration_imports "
                    "WHERE migration_id=? AND source_path=?",
                    (legacy_migration_id, source_identity),
                ).fetchone()
            if existing is not None:
                return MigrationReport(
                    str(existing["migration_id"]),
                    source_digest,
                    int(existing["row_count"]),
                    int(existing["rejected_count"]),
                    Money("USD", int(existing["amount_micros"])),
                    True,
                )
            for (
                run_id,
                repository,
                issue,
                micros,
                stamp,
                model,
            ) in parsed:
                period_start = stamp.astimezone(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                period_end = period_start + timedelta(days=1)
                period_id = period_start.date().isoformat()
                settled_at = _stamp(stamp)
                db.execute(
                    "INSERT OR IGNORE INTO runs("
                    "run_id, dispatch_id, kind, owner_id, state, "
                    "config_version, started_at, completed_at, outcome, "
                    "provider_receipt_id) VALUES (?, ?, 'legacy-import', "
                    "'migration', 'completed', 'legacy-v1', ?, ?, 'settled', ?)",
                    (
                        run_id,
                        f"legacy:{run_id}",
                        settled_at,
                        settled_at,
                        f"legacy:{run_id}",
                    ),
                )
                if not int(db.execute("SELECT changes()").fetchone()[0]):
                    continue
                imported += 1
                total += micros
                db.execute(
                    "INSERT INTO budget_reservations("
                    "reservation_id, run_id, repository, workload_repository, "
                    "dimension, period_id, period_start, period_end, "
                    "period_timezone, currency, maximum_micros, cap_micros, "
                    "model, rate_card_id, rate_card_version, "
                    "quote_effective_at, config_version, state, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, 'repository', ?, ?, ?, "
                    "'UTC', 'USD', ?, ?, ?, 'legacy', '1', ?, 'legacy-v1', "
                    "'settled', ?, ?)",
                    (
                        f"legacy:{run_id}",
                        run_id,
                        repository,
                        repository,
                        period_id,
                        _stamp(period_start),
                        _stamp(period_end),
                        micros,
                        9_000_000_000_000_000_000,
                        model,
                        settled_at,
                        settled_at,
                        settled_at,
                    ),
                )
                scopes = (
                    (
                        repository,
                        "repository",
                        period_id,
                        period_start,
                        period_end,
                    ),
                    ("*", "global", period_id, period_start, period_end),
                    (
                        repository,
                        f"issue:{issue}",
                        f"lifetime:{issue}",
                        datetime(1970, 1, 1, tzinfo=timezone.utc),
                        datetime.max.replace(tzinfo=timezone.utc),
                    ),
                )
                for scoped_repo, dimension, scoped_period, start, end in scopes:
                    db.execute(
                        "INSERT INTO reservation_limits("
                        "run_id, repository, dimension, period_id, period_start, "
                        "period_end, period_timezone, currency, cap_micros"
                        ") VALUES (?, ?, ?, ?, ?, ?, 'UTC', 'USD', ?)",
                        (
                            run_id,
                            scoped_repo,
                            dimension,
                            scoped_period,
                            _stamp(start),
                            _stamp(end),
                            9_000_000_000_000_000_000,
                        ),
                    )
                db.execute(
                    "INSERT INTO charges(run_id, provider_receipt_id, "
                    "amount_micros, currency, basis, settled_at) "
                    "VALUES (?, ?, ?, 'USD', 'synthetic', ?)",
                    (run_id, f"legacy:{run_id}", micros, settled_at),
                )
            db.execute(
                "INSERT INTO migration_imports(migration_id, source_path, "
                "source_digest, row_count, rejected_count, amount_micros, "
                "imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    migration_id,
                    str(source.resolve()),
                    source_digest,
                    imported,
                    rejected,
                    total,
                    _stamp(self._now()),
                ),
            )
        return MigrationReport(
            migration_id,
            source_digest,
            imported,
            rejected,
            Money("USD", total),
        )

    def _balance(
        self, db: sqlite3.Connection, scope: BudgetScope
    ) -> tuple[int, int]:
        params = (
            scope.repository,
            scope.dimension,
            scope.period_id,
            scope.currency,
        )
        settled = db.execute(
            "SELECT COALESCE(SUM(c.amount_micros), 0) AS amount "
            "FROM charges c JOIN reservation_limits l ON l.run_id=c.run_id "
            "WHERE l.repository=? AND l.dimension=? AND l.period_id=? "
            "AND c.currency=?",
            params,
        ).fetchone()["amount"]
        reserved = db.execute(
            "SELECT COALESCE(SUM(r.maximum_micros), 0) AS amount "
            "FROM budget_reservations r "
            "JOIN reservation_limits l ON l.run_id=r.run_id "
            "WHERE l.repository=? AND l.dimension=? AND l.period_id=? "
            "AND l.currency=? AND r.state='active'",
            params,
        ).fetchone()["amount"]
        return int(settled), int(reserved)

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        from .sql_read_budget import BudgetedSqlConnection

        mode = "rwc" if create else "rw"
        uri = f"{Path(self.path).as_uri()}?mode={mode}"
        try:
            db = sqlite3.connect(
                uri,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                uri=True,
                factory=BudgetedSqlConnection,
            )
        except sqlite3.OperationalError as exc:
            if not create and not os.path.exists(self.path):
                raise CorruptControlStore(
                    "control-store authority database disappeared after "
                    "initialization"
                ) from exc
            raise
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db


def _authorization(value: RetryAuthorization) -> UnconsumedRetryAuthorization:
    return value if isinstance(value, UnconsumedRetryAuthorization) else value.authorization


def _retry_authorization_from_row(row: sqlite3.Row) -> RetryAuthorization:
    try:
        association = (StandaloneCommand() if row["association_kind"] == "standalone"
                       else RearmObligation(row["request_id"]))
        grant = UnconsumedRetryAuthorization(
            row["authorization_id"], CommandId(row["command_id"]), row["gate_id"], row["evidence_id"],
            decode_workflow(row["binding_json"], WorkflowBinding), association,
        )
        disposition = row["disposition"]
        if disposition is None:
            return grant
        if disposition == "consumed":
            return ConsumedRetryAuthorization(grant, int(row["consumed_attempt"]))
        return ClosedRetryAuthorization(grant, RetryDisposition(disposition), row["disposition_reason"])
    except (ValueError, TypeError) as exc:
        raise WorkflowAuthorityError("persisted retry authorization is malformed") from exc


def load_retry_authorization_facts(
    store: SQLiteControlStore, db: sqlite3.Connection, command_id: CommandId, evidence_id: str,
) -> RetryAuthorizationFacts:
    """Mint harness-owned acceptance and spend facts inside the caller's transaction.

    The same facts back acceptance, the operator view and the spend decision,
    including the store's own execution-authority check, so nothing is offered
    that the executor would have to discard.
    """
    row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
    if row is None:
        raise WorkflowAuthorityError("retry authorization names an unknown command")
    command = _command_from_row(row)
    binding = command.workflow
    if binding is None:
        raise WorkflowAuthorityError("retry authorization requires a workflow-bound command")
    evidence: CommandAbsenceEvidence | None = None
    stored = db.execute("SELECT * FROM workflow_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
    if (stored is not None and stored["kind"] == "absence" and stored["command_id"] == str(command_id)
            and (stored["repository"], stored["target_kind"], stored["target_number"]) == workflow_scope(binding.fence.key)):
        candidate = decode_workflow(stored["payload_json"], CommandAbsenceEvidence)
        evidence = candidate if candidate.command_id == command.command_id else None
    link = db.execute(
        "SELECT request_id,disposition FROM workflow_rearm_commands WHERE command_id=?", (str(command_id),),
    ).fetchone()
    current = load_workflow_row(db, binding.fence.key)
    intent = current.intent if current is not None else None
    # Review obligations carry no workspace handoff; their readiness is the
    # pending intent alone.
    workspace_ready = link is not None and (
        isinstance(intent, PendingRearm) and intent.request_id == link["request_id"]
        and load_workspace_ack(db, intent.handoff) is not None
        or isinstance(intent, PendingReviewRearm) and intent.request_id == link["request_id"]
    )
    prior = tuple(_retry_authorization_from_row(item) for item in db.execute(
        "SELECT * FROM workflow_retry_authorizations WHERE command_id=? "
        "AND (disposition IS NULL OR disposition='inapplicable' OR evidence_id=?) "
        "ORDER BY authorization_id LIMIT ?",
        (str(command_id), evidence_id, MAX_RELEVANT_AUTHORIZATIONS),
    ))
    attempted, attempts = db.execute(
        "SELECT max(disposed_at), count(*) FROM workflow_retry_authorizations "
        "WHERE command_id=? AND disposition='consumed'",
        (str(command_id),),
    ).fetchone()
    refusal: str | None = None
    db.execute("SAVEPOINT retry_execution_check")
    try:
        store._validate_workflow_command(db, command)
    except CorruptControlStore:
        raise
    except (ControlStoreError, ValueError) as exc:
        refusal = str(exc)[:4096] or type(exc).__name__
    finally:
        # The check is read-only by contract; never let it leave writes behind.
        db.execute("ROLLBACK TO retry_execution_check")
        db.execute("RELEASE retry_execution_check")
    return RetryAuthorizationFacts(
        command.command_id, binding, SQLiteControlStore._command_reconciliation(row), evidence,
        RearmObligation(link["request_id"]) if link is not None else StandaloneCommand(), prior,
        returns_resolve_result=SQLiteControlStore._return_projection_attempt(db, command) is not None,
        obligation_active=link is not None and link["disposition"] == "required",
        workspace_ready=workspace_ready,
        last_attempt_at=datetime.fromisoformat(attempted) if attempted is not None else None,
        authorized_attempts=int(attempts), execution_refusal=refusal,
    )


def load_bound_review_submission(
    db: sqlite3.Connection, command_id: CommandId,
) -> ReviewSubmissionReceipt | CeilingEscalation | None:
    """`SQLiteControlStore.bound_review_submission` inside the caller's transaction.

    The receipt identity is the GitHub review the executor matched by its
    command marker (`remote_object_id`); the store already refuses one review
    as the receipt of two submissions, so a baseline names its actual review.
    """
    row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
    if row is None:
        raise KeyError(str(command_id))
    receipt = db.execute("SELECT * FROM effect_receipts WHERE command_id=?", (str(command_id),)).fetchone()
    command = _command_from_row(row)
    binding = command.workflow
    if (command.kind is not CommandKind.SUBMIT_REVIEW or binding is None
            or binding.authority.family is not WorkflowFamily.REVIEW_REJECTIONS):
        return None
    if row["state"] != EffectStatus.SUCCEEDED.value or receipt is None or receipt["status"] != EffectStatus.SUCCEEDED.value:
        return None
    try:
        op = bound_submission_op(command)
        assert op is not None
        review = receipt["remote_object_id"]
        if not isinstance(review, str) or not review:
            raise ValueError("the receipt does not name the review it matched")
        submission = BoundReviewSubmission(command.command_id, binding)
        receipt_id = f"github-review:{review}"
        assert op.patch_id is not None
        if op.escalate_cause == "ceiling":
            return CeilingEscalation(submission, op.head, receipt_id, "ceiling", op.rule)
        # A verdict ESCALATE is the reviewer's verdict on its head: a baseline
        # that also carries the attention hold (`record_escalated_review`).
        return ReviewSubmissionReceipt(submission, op.head, op.patch_id, receipt_id,
                                       escalated=op.escalate_cause == "verdict")
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowAuthorityError(f"bound review submission evidence is malformed: {exc}") from exc


def _command_from_row(row: sqlite3.Row) -> LifecycleCommand:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise CorruptControlStore("lifecycle command payload is not an object")
    command = LifecycleCommand(
        CommandId(row["command_id"]),
        row["idempotency_key"],
        CommandKind(row["kind"]),
        row["target"],
        row["expected_revision"],
        payload,
        row["config_version"],
        row["repository"],
        row["executor_identity"],
        decode_workflow(row["workflow_binding_json"], WorkflowBinding) if row["workflow_binding_json"] is not None else None,
    )
    if ("steward_guard" in command.payload or command.idempotency_key.startswith("steward-verdict:")
            or "logical_key" in row.keys() and row["logical_key"].startswith("steward-verdict:")):
        guard = decode_workflow(command.payload.get("steward_guard"), cc.StewardCommandGuard)
        logical = f"steward-verdict:{guard.source.admission.attempt.run_id}:{guard.position}"
        if (command.idempotency_key != logical or str(command.command_id) != hashlib.sha256(logical.encode()).hexdigest()
                or _command_payload_digest(command, _json(command.payload)) != row["payload_digest"]):
            raise CorruptControlStore("steward verdict command disagrees with its immutable guard")
    return command


def _encoded_command_binding(command: LifecycleCommand) -> str | None:
    return encode_workflow(command.workflow) if command.workflow is not None else None


def _command_payload_digest(command: LifecycleCommand, payload: str) -> str:
    return _encoded_payload_digest(payload, _encoded_command_binding(command))


def _encoded_payload_digest(payload: str, binding: str | None) -> str:
    identity = payload if binding is None else payload + "\n" + binding
    return hashlib.sha256(identity.encode()).hexdigest()


def _command_correlation(command: LifecycleCommand) -> Correlation:
    kind, separator, raw_number = command.target.partition(":")
    if not separator:
        return Correlation()
    if kind == "pull-request-body":
        # Broker targets are `<kind>:<owner/repo>:<number>` — the repository
        # segment sits in the middle, so the number is the LAST field, not the
        # first one after the kind. Without this the dashboard folded every
        # body-update transition with an EMPTY correlation and the edit could
        # not be traced back to its PR or issue.
        issue = command.payload.get("issue")
        try:
            pr = int(command.target.rpartition(":")[2])
        except ValueError:
            return Correlation()
        if pr <= 0:
            return Correlation()
        return Correlation(
            pr=pr,
            issue=issue if isinstance(issue, int) and issue > 0 else None,
        )
    try:
        number = int(raw_number)
    except ValueError:
        return Correlation()
    if number <= 0:
        return Correlation()
    if kind == "issue":
        return Correlation(issue=number)
    if kind == "pr":
        return Correlation(pr=number)
    return Correlation()


def _run_correlation(run_id: RunId, dispatch_id: str) -> Correlation:
    try:
        payload = json.loads(dispatch_id)
        issue = payload.get("issue")
        number = payload.get("number")
        kind = str(payload.get("kind") or "")
    except (AttributeError, TypeError, json.JSONDecodeError):
        return Correlation(run_id=str(run_id))
    correlated_issue = (
        int(issue)
        if isinstance(issue, int) and not isinstance(issue, bool) and issue > 0
        else None
    )
    correlated_pr = (
        int(number)
        if correlated_issue is None
        and isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
        and "REVIEW" in kind
        else None
    )
    return Correlation(
        issue=correlated_issue,
        pr=correlated_pr,
        run_id=str(run_id),
    )


def _dispatch_issue(dispatch_id: str) -> int | None:
    try:
        value = json.loads(dispatch_id).get("issue")
    except (AttributeError, TypeError, json.JSONDecodeError):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _dispatch_claim_target(issue: int) -> str:
    if issue == 0:
        raise ValueError("dispatch claim target cannot be zero")
    return f"issue:{issue}" if issue > 0 else f"pr:{-issue}"


def _dispatch_claim_issue(target: str) -> int | None:
    """Inverse of `_dispatch_claim_target`; None when the row is unreadable."""
    prefix, _, number = target.partition(":")
    if prefix not in {"issue", "pr"} or not number.isdigit():
        return None
    value = int(number)
    if value <= 0:
        return None
    return value if prefix == "issue" else -value


def _receipt_digest(receipt: EffectReceipt) -> str:
    encoded = _json(
        {
            "command_id": str(receipt.command_id),
            "executor_identity": receipt.executor_identity,
            "started_at": _stamp(receipt.started_at),
            "completed_at": _stamp(receipt.completed_at),
            "status": receipt.status.value,
            "remote_object_id": receipt.remote_object_id,
            "remote_revision": receipt.remote_revision,
            "error": receipt.error,
        }
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError(f"{type(value).__name__} is not JSON-serializable control data")


def _sql_statements(source: str) -> tuple[str, ...]:
    """Split immutable migration source without `executescript` auto-commits."""

    statements: list[str] = []
    pending: list[str] = []
    for line in source.splitlines():
        pending.append(line)
        candidate = "\n".join(pending).strip()
        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            pending.clear()
    remainder = "\n".join(pending).strip()
    if remainder:
        raise IncompatibleSchema("control-store migration source is incomplete")
    return tuple(statements)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control-store timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("control-store timestamps must be timezone-aware")
    return parsed
