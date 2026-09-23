"""Bound, generation-fenced SQLite workflow transitions; ADR 0015.

This adapter joins the existing control-store transaction and command outbox.
It never initializes authority, imports legacy state, or performs remote I/O.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta

from ..domain.contracts import EffectStatus, PricingBasis, WorkflowAttemptIdentity
from ..domain.actions import ClearIssueState, Relabel, RemoveWorktree
from ..domain.contracts import CommandId
from ..domain.outcome import budget_exhausted, infra_class_failure
from ..domain.resolve_return import (
    FinalizedResolveReturn, PendingResolveReturn, ResolveCleanupCompleted, ReturnedResolve, RetiredResolveReturn, UnavailableResolveReturn,
)
from ..domain.resolve_split import (
    AdjudicationInput, ContractBaseline, ContractResetCompleted, ContractResetIntent,
    ContractRevision, NeedsAdjudication, ReauthorInput, ReauthorResetIntent, ResetIntent,
    SplitContext, SplitImplement, split_context, validate_contract_rounds,
)
from ..domain.workflow import (
    AuthoredResolve, AuthorizedOperatorCommand, CancelRearm, CancelledRearm, CompletedRearm,
    ContinueGrantedRearm, HeldProjection, LinkedPrPresent, NoLinkedPr, ObservedIssue, ObservedTransition, OpenGate,
    PendingRearm, RearmingGate, RearmIntent, RemoteOutcomeUncertain,
    RequestRearm, ResolveAdmission, ResolveAttemptResult, ResolveRearmPreflight, ResolveResultKind, ResolveStage,
    StopReason, SupersededRearm, WorkflowAbsent, WorkflowChanged, WorkflowCommitted,
    WorkflowConflict, WorkflowRead, WorkflowSnapshot, WorkflowUnavailable,
    WorkflowUnchanged, WorkflowRejected,
)
from ..domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision,
    WorkflowWriter, validate_workflow_identifier, validate_workflow_integer,
)
from ..domain.workflow_policy import (
    apply_return_cleanup, clear_return_split, decide_return, defer_return_observation, initial_resolve_workflow,
    reauthor_baseline, record_resolve_attempt, resolve_return_actions, return_gate_id,
    update_return_observation, validate_resolve_transition, split_return_continuation,
)
from .control_store import AccountingDisposition, SettlementRequest, SettlementResult, SQLiteControlStore, _command_from_row, admitted_adjudications, latest_work_admission, proven_unlaunched_run, reset_predecessor_admission
from .effects import command_for_action
from .deployment_authority import DeploymentAuthority, deployment_role, deployment_effect_barrier
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_rows import WorkflowAuthorityError, load_workflow_row, workflow_scope


def _opened(gate: OpenGate | RearmingGate) -> OpenGate:
    return gate.gate if isinstance(gate, RearmingGate) else gate


def _accepted_proposal(proposal: WorkflowChanged) -> str:
    """Retain accepted facts and plan; these are not the operator's identity."""
    snapshot = replace(proposal.snapshot, gates=tuple(sorted(proposal.snapshot.gates, key=lambda g: _opened(g).gate_id)))
    return json.dumps({
        "mutation": proposal.mutation.value,
        "expected": encode_workflow(proposal.expected),
        "snapshot": encode_workflow(snapshot),
        "audit": encode_workflow(proposal.audit),
        "commands": [{
            "id": c.command_id, "idempotency": c.idempotency_key,
            "kind": c.kind.value, "target": c.target,
            "revision": c.expected_revision, "payload": dict(c.payload),
            "config": c.config_version, "repository": c.repository,
            "owner": c.executor_identity, "workflow": encode_workflow(c.workflow),
        } for c in proposal.commands],
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _same_receipt(raw: str, proposal: WorkflowChanged, encoded: str) -> bool:
    if not isinstance(proposal.audit, AuthorizedOperatorCommand):
        return raw == encoded
    try:
        accepted = json.loads(raw)
        if not isinstance(accepted, dict):
            raise ValueError("accepted workflow audit must be an object")
        accepted_audit = accepted.get("audit")
        if not isinstance(accepted_audit, str):
            raise ValueError("accepted workflow audit is missing its typed payload")
        audit = decode_workflow(accepted_audit, AuthorizedOperatorCommand)
    except (ValueError, TypeError) as exc:
        raise WorkflowAuthorityError("invalid accepted operator audit") from exc
    # Authentication is established before this bound port is called. Replay
    # belongs to the same authenticated subject and exact operator command;
    # refreshed observations, channel provenance and regenerated plans do not
    # authorize a second write or replace the original accepted audit.
    return audit.actor.subject == proposal.audit.actor.subject and audit.command == proposal.audit.command


def _dedupe_key(proposal: WorkflowChanged) -> tuple[str, str]:
    audit = proposal.audit
    if isinstance(audit, ResolveAttemptResult):
        # A run is one paid leg. The full payload also binds the leg identity;
        # changing just that identity cannot buy another allowance charge.
        return "workflow-result", str(audit.attempt.run_id)
    if isinstance(audit, AuthorizedOperatorCommand):
        command = audit.command
        if isinstance(command, (RequestRearm, ContinueGrantedRearm)):
            return "workflow-request", command.request_id
        if isinstance(command, CancelRearm):
            return "workflow-cancellation", command.cancellation_id
    return "workflow-transition", hashlib.sha256(encode_workflow(proposal.expected).encode()).hexdigest()


class SQLiteWorkflowControl:
    def __init__(
        self, control: SQLiteControlStore, writer: WorkflowWriter, *, resolve_ceiling: int, max_contract_rounds: int = 2,
    ) -> None:
        if not isinstance(control, SQLiteControlStore) or not isinstance(writer, WorkflowWriter):
            raise ValueError("workflow control requires the existing store and a typed writer")
        if writer.role != "fix" or deployment_role(writer.repository, writer.owner_id) != writer.role:
            raise ValueError("resolve workflow authority requires the repository's fix owner")
        validate_workflow_integer(resolve_ceiling, "resolve ceiling")
        validate_contract_rounds(max_contract_rounds)
        self._control = control
        self._writer = writer
        self._resolve_ceiling = resolve_ceiling
        self._max_contract_rounds = max_contract_rounds

    @property
    def writer(self) -> WorkflowWriter:
        return self._writer

    def _scope(self, key: WorkflowKey) -> None:
        if not isinstance(key, WorkflowKey) or key.repository != self.writer.repository or not isinstance(key.target, IssueRef):
            raise WorkflowAuthorityError("workflow is outside the bound resolve writer scope")

    def load_workflow(self, key: WorkflowKey) -> WorkflowRead:
        self._scope(key)
        try:
            with closing(self._control._connect()) as db:
                db.execute("BEGIN")
                snapshot = load_workflow_row(db, key)
                return snapshot if snapshot is not None else WorkflowAbsent(key)
        except sqlite3.OperationalError as exc:
            return WorkflowUnavailable(key, f"workflow read unavailable: {exc}")

    def ensure_observed_workflow(self, observation: ObservedIssue) -> WorkflowCommitted:
        if not isinstance(observation, ObservedIssue):
            raise ValueError("workflow discovery requires a verified present issue")
        self._scope(observation.key)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            prior = load_workflow_row(db, observation.key)
            activation = db.execute(
                "SELECT epoch FROM workflow_authority_epochs WHERE repository=? AND family=?",
                (self.writer.repository, WorkflowFamily.RESOLVE.value),
            ).fetchone()
            if activation is None:
                raise WorkflowAuthorityError("resolve workflow migration is required")
            try:
                authority = FamilyAuthority(WorkflowFamily.RESOLVE, activation["epoch"])
            except ValueError as exc:
                raise WorkflowAuthorityError("resolve activation epoch is invalid") from exc
            snapshot = initial_resolve_workflow(observation, authority, now=self._control._now())
            if prior is not None:
                return WorkflowCommitted(prior, idempotent=True)
            scope = workflow_scope(observation.key)
            db.execute(
                "INSERT INTO workflows VALUES (?, ?, ?, 0, 0, ?)",
                (*scope, encode_workflow((authority, snapshot.episode))),
            )
            db.execute(
                "INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                (snapshot.episode.episode_id, *scope, encode_workflow(observation)),
            )
            self._control._transition(db, "workflow", encode_workflow(observation.key), "discovered",
                                      {"generation": 0, "version": 0, "epoch": authority.epoch})
            return WorkflowCommitted(snapshot)

    def lookup_rearm(self, key: WorkflowKey, request_id: str) -> RearmIntent | None:
        self._scope(key)
        validate_workflow_identifier(request_id, "request")
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            load_workflow_row(db, key)
            row = db.execute(
                "SELECT * FROM workflow_rearms WHERE request_id=? AND repository=? "
                "AND target_kind=? AND target_number=?", (request_id, *workflow_scope(key)),
            ).fetchone()
            if row is None:
                return None
            intent = decode_workflow(row["intent_json"], PendingRearm | CompletedRearm | CancelledRearm | SupersededRearm)
            if intent.request_id != request_id or intent.gate_id != row["gate_id"] or intent.to_generation != row["to_generation"] or row["state"] != _intent_state(intent):
                raise WorkflowAuthorityError("re-arm history disagrees with its typed payload")
            return intent

    def held_projection(self, key: WorkflowKey) -> HeldProjection:
        state = self.load_workflow(key)
        if not isinstance(state, WorkflowSnapshot):
            raise WorkflowAuthorityError("held projection requires available workflow authority")
        return HeldProjection(state)

    @staticmethod
    def _return_proof(returned: ReturnedResolve) -> ResolveAttemptResult:
        result = returned.result()
        kind = (ResolveResultKind.BUDGET_REFUSAL if budget_exhausted(result.exit_reason) else
                ResolveResultKind.INFRA_FAILURE if infra_class_failure(result) else ResolveResultKind.STAGE_PROGRESSED)
        # This value only selects the settlement validation contract. It is
        # never recorded as a provisional paid classification.
        return ResolveAttemptResult(returned.attempt, kind, stage=returned.stage, session=returned.session)

    def _return_prior(self, db: sqlite3.Connection, returned: ReturnedResolve) -> tuple[WorkflowSnapshot, ResolveAdmission]:
        self._scope(returned.attempt.binding.fence.key)
        self._control._assert_owner_active(db, self.writer.owner_id)
        prior = load_workflow_row(db, returned.attempt.binding.fence.key)
        if prior is None:
            raise WorkflowAuthorityError("paid return requires existing workflow authority")
        admission = self._validate_attempt_run(db, self._return_proof(returned))
        if returned.split_input is not None:
            encoded = encode_workflow(returned.split_input)
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            row = db.execute("SELECT dispatch_id FROM runs WHERE run_id=?", (str(returned.attempt.run_id),)).fetchone()
            metadata = json.loads(row["dispatch_id"])
            selection = split_context(returned.split_input).selection
            reservation = db.execute("SELECT model FROM budget_reservations WHERE run_id=?", (str(returned.attempt.run_id),)).fetchone()
            if admission.split_fingerprint != fingerprint or metadata.get("split_fingerprint") != fingerprint or metadata.get("split_input") != encoded:
                raise WorkflowAuthorityError("returned split context disagrees with its admitted input")
            if metadata.get("resolve_inputs") != encode_workflow(returned.inputs):
                raise WorkflowAuthorityError("returned split issue facts disagree with the admitted task")
            if metadata.get("runtime") != selection.runtime or metadata.get("effort") != (selection.effort.value if selection.effort is not None else None) or reservation is None or reservation["model"] != selection.model:
                raise WorkflowAuthorityError("returned split selection disagrees with actual admitted runtime/model/effort")
            if isinstance(returned.split_input, (SplitImplement, AdjudicationInput)):
                source = returned.split_input if isinstance(returned.split_input, SplitImplement) else returned.split_input.petition
                predecessor = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return-disposition' AND scope=? AND key=?", (self.writer.repository, str(source.source.run_id))).fetchone()
                if predecessor is None:
                    raise WorkflowAuthorityError("split input lacks its finalized predecessor")
                finalized = decode_workflow(predecessor["payload_json"], FinalizedResolveReturn)
                if finalized.result.attempt != source.source or finalized.result.split != source or source.admission_version >= admission.revision.version:
                    raise WorkflowAuthorityError("split input does not match its earlier finalized predecessor")
            if isinstance(returned.split_input, ReauthorInput):
                intent = returned.split_input.reset.intent
                assert isinstance(intent, ReauthorResetIntent)
                source, source_admission, _ = self._finalized_source(db, intent.source)
                if source_admission.revision.version >= admission.revision.version or intent.binding != returned.attempt.binding or intent.input_fingerprint != admission.input_fingerprint:
                    raise WorkflowAuthorityError("replacement author disagrees with its earlier reset predecessor")
                if self._source_baseline(db, source) != intent.baseline:
                    raise WorkflowAuthorityError("replacement author reset used a different safe baseline")
            if isinstance(returned.split_input, (AdjudicationInput, ReauthorInput)):
                ack = returned.split_input.reset
                stored = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-contract-reset-completed' AND scope=? AND key=?", (self.writer.repository, ack.intent.intent_id)).fetchone()
                if stored is None or stored["payload_json"] != encode_workflow(ack):
                    raise WorkflowAuthorityError("split input lacks its durable verified reset")
        elif admission.split_fingerprint is not None:
            raise WorkflowAuthorityError("returned result omitted its admitted split input")
        if admission.revision.version > prior.revision.version or returned.returned_at > self._control._now():
            raise WorkflowAuthorityError("paid return or admission is newer than current authority")
        return prior, admission

    def _return_row(self, db: sqlite3.Connection, namespace: str, returned: ReturnedResolve):
        return db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                          (namespace, self.writer.repository, str(returned.attempt.run_id))).fetchone()

    def _return_disposition(self, db: sqlite3.Connection, returned: ReturnedResolve) -> FinalizedResolveReturn | RetiredResolveReturn | None:
        row = self._return_row(db, "workflow-return-disposition", returned)
        if row is None:
            return None
        disposition = decode_workflow(row["payload_json"], FinalizedResolveReturn | RetiredResolveReturn)
        attempt = disposition.result.attempt if isinstance(disposition, FinalizedResolveReturn) else disposition.attempt
        if attempt != returned.attempt:
            raise WorkflowAuthorityError("paid return disposition belongs to another attempt")
        if isinstance(disposition, FinalizedResolveReturn):
            result = self._return_row(db, "workflow-result", returned)
            if result is None or result["payload_json"] != encode_workflow(disposition.result) or disposition.result.stage is not returned.stage:
                raise WorkflowAuthorityError("paid return disposition disagrees with final classification")
            for command_id in disposition.commands:
                command = db.execute("SELECT repository,workflow_binding_json FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()
                if command is None or command["repository"] != self.writer.repository or decode_workflow(command["workflow_binding_json"], WorkflowBinding) != returned.attempt.binding:
                    raise WorkflowAuthorityError("paid return disposition lacks its bound completion command")
                provenance = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return-command' AND scope=? AND key=?",
                                        (self.writer.repository, str(command_id))).fetchone()
                if provenance is None or decode_workflow(provenance["payload_json"], WorkflowAttemptIdentity) != returned.attempt:
                    raise WorkflowAuthorityError("paid return command lacks its admitted occurrence provenance")
            if not returned.is_error and (returned.status in ("pushed", "done") or returned.split_input is not None) and disposition.issue.issue.state == "closed" and isinstance(disposition.linkage, NoLinkedPr):
                cleanup_row = self._return_row(db, "workflow-return-cleanup", returned)
                if cleanup_row is None:
                    raise WorkflowAuthorityError("terminal returned result lacks its cleanup acknowledgement")
                cleanup = decode_workflow(cleanup_row["payload_json"], ResolveCleanupCompleted)
                if cleanup != ResolveCleanupCompleted(returned.attempt, disposition.issue, disposition.linkage) or disposition.result.session is not None:
                    raise WorkflowAuthorityError("terminal returned result disagrees with its cleanup acknowledgement")
        return disposition

    def _insert_return_record(self, db: sqlite3.Connection, namespace: str, returned: ReturnedResolve, payload: str, *, at: datetime | None = None) -> None:
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   (namespace, self.writer.repository, str(returned.attempt.run_id),
                    (at or self._control._now()).isoformat(), payload))

    def _retire_return(self, db: sqlite3.Connection, prior: WorkflowSnapshot, returned: ReturnedResolve) -> WorkflowSnapshot:
        after = update_return_observation(prior, returned, None)
        self._persist_snapshot(db, after)
        self._insert_return_record(db, "workflow-return-disposition", returned,
                                   encode_workflow(RetiredResolveReturn(returned.attempt, prior.binding)))
        self._control._transition(db, "workflow_return", str(returned.attempt.run_id), "retired",
                                  {"generation": prior.revision.fence.generation})
        return after

    def capture_return(self, returned: ReturnedResolve) -> WorkflowCommitted | WorkflowConflict:
        with self._control.transaction() as db:
            return self._capture_return(db, returned)

    def _capture_return(self, db: sqlite3.Connection, returned: ReturnedResolve) -> WorkflowCommitted | WorkflowConflict:
        if not isinstance(returned, ReturnedResolve):
            raise ValueError("capture requires a typed paid return")
        encoded = encode_workflow(returned)
        prior, _ = self._return_prior(db, returned)
        existing = self._return_row(db, "workflow-return", returned)
        if existing is not None:
            if existing["payload_json"] != encoded:
                return WorkflowConflict(prior.revision, "paid return identity already contains different evidence")
            self._return_disposition(db, returned)
            return WorkflowCommitted(prior, idempotent=True)
        if self._return_row(db, "workflow-result", returned) is not None:
            return WorkflowConflict(prior.revision, "paid run was already classified without this return receipt")
        self._insert_return_record(db, "workflow-return", returned, encoded)
        if prior.binding != returned.attempt.binding:
            return WorkflowCommitted(self._retire_return(db, prior, returned))
        pending = PendingResolveReturn(returned, self._control._now())
        self._insert_return_record(db, "workflow-return-observation", returned, encode_workflow(pending), at=pending.next_read_at)
        after = update_return_observation(prior, returned, pending)
        self._persist_snapshot(db, after)
        self._control._transition(db, "workflow_return", str(returned.attempt.run_id), "captured",
                                  {"generation": after.revision.fence.generation})
        return WorkflowCommitted(after)

    def settle_return(self, request: SettlementRequest, returned: ReturnedResolve) -> SettlementResult:
        """Money and recoverable paid output share the existing settlement commit."""
        if not isinstance(request, SettlementRequest) or not isinstance(returned, ReturnedResolve) or request.run_id != returned.attempt.run_id:
            raise ValueError("atomic settlement requires this paid return's run")
        self._scope(returned.attempt.binding.fence.key)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            settled = self._control._settle(db, request)
            if settled.disposition is AccountingDisposition.AVAILABLE:
                captured = self._capture_return(db, returned)
                if isinstance(captured, WorkflowConflict):
                    raise WorkflowAuthorityError(captured.reason)
            return settled

    def returned_result(self, attempt: WorkflowAttemptIdentity) -> ReturnedResolve | None:
        if not isinstance(attempt, WorkflowAttemptIdentity):
            raise ValueError("returned result lookup requires a captured attempt")
        self._scope(attempt.binding.fence.key)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return' AND scope=? AND key=?",
                             (self.writer.repository, str(attempt.run_id))).fetchone()
            if row is None:
                return None
            returned = decode_workflow(row["payload_json"], ReturnedResolve)
            if returned.attempt != attempt:
                raise WorkflowAuthorityError("returned result lookup disagrees with the admitted attempt")
            self._return_prior(db, returned)
            self._return_disposition(db, returned)
            return returned

    def pending_returns(self, *, limit: int = 32) -> tuple[PendingResolveReturn | UnavailableResolveReturn, ...]:
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("return reconciliation limit must be between 1 and 128")
        pending: list[PendingResolveReturn | UnavailableResolveReturn] = []
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            now = self._control._now()
            # Operational recovery trusts atomic terminal records and scheduling
            # indexes; this is not an audit of arbitrary at-rest DB tampering.
            # Direct return lookup/capture/finalize still validate dispositions.
            rows = db.execute(
                "SELECT raw.key AS run_id, raw.payload_json AS returned_json, observation.payload_json AS observation_json "
                "FROM dedupe_keys raw LEFT JOIN dedupe_keys observation ON observation.namespace='workflow-return-observation' "
                "AND observation.scope=raw.scope AND observation.key=raw.key LEFT JOIN runs ON runs.run_id=raw.key "
                "LEFT JOIN dedupe_keys done ON done.namespace='workflow-return-disposition' AND done.scope=raw.scope AND done.key=raw.key "
                "WHERE raw.namespace='workflow-return' AND raw.scope=? AND (runs.owner_id=? OR runs.run_id IS NULL) "
                "AND done.key IS NULL "
                "AND COALESCE(julianday(raw.claimed_at), 0)<=julianday(?) "
                "AND COALESCE(julianday(observation.claimed_at), 0)<=julianday(?) "
                "ORDER BY julianday(raw.claimed_at), raw.key LIMIT ?",
                (self.writer.repository, self.writer.owner_id, now.isoformat(), now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                db.execute("SAVEPOINT returned_observation")
                try:
                    returned = decode_workflow(row["returned_json"], ReturnedResolve)
                    if str(returned.attempt.run_id) != row["run_id"]:
                        raise WorkflowAuthorityError("paid return index disagrees with its admitted run")
                    item = decode_workflow(row["observation_json"], PendingResolveReturn)
                    if item.returned != returned:
                        raise WorkflowAuthorityError("pending observation disagrees with immutable paid return")
                    prior, _ = self._return_prior(db, returned)
                    if prior.binding != returned.attempt.binding:
                        self._retire_return(db, prior, returned)
                    elif item.next_read_at <= now:
                        pending.append(item)
                except (ValueError, TypeError, KeyError) as exc:
                    db.execute("ROLLBACK TO returned_observation")
                    next_read = now + timedelta(hours=1)
                    reason = (str(exc) or type(exc).__name__).replace("\x00", "\\0")[:16384]
                    record_key = row["run_id"]
                    try:
                        validate_workflow_identifier(record_key, "paid return index")
                    except ValueError:
                        record_key = "invalid-index:" + hashlib.sha256(repr(record_key).encode("utf-8", "backslashreplace")).hexdigest()
                        reason = "Invalid paid return index; " + reason[:16000]
                    self._schedule_return_audit(db, row["run_id"], next_read)
                    self._control._transition(db, "workflow_return", record_key, "authority_unavailable",
                                              {"reason": reason, "next_read_at": next_read.isoformat()})
                    pending.append(UnavailableResolveReturn(record_key, reason, next_read))
                finally:
                    db.execute("RELEASE returned_observation")
        return tuple(pending)

    def _schedule_return_audit(self, db: sqlite3.Connection, run_id: str, at: datetime) -> None:
        # The raw payload and its returned_at remain immutable. This existing
        # index doubles as a fair scan cursor: corrupt prefixes cannot
        # monopolize a bounded recovery batch, including after process restart.
        db.execute("UPDATE dedupe_keys SET claimed_at=? WHERE namespace='workflow-return' AND scope=? AND key=?",
                   (at.isoformat(), self.writer.repository, run_id))

    def defer_return(self, returned: ReturnedResolve, error: str) -> None:
        with self._control.transaction() as db:
            prior, _ = self._return_prior(db, returned)
            self._require_return(db, returned)
            if self._return_disposition(db, returned) is not None:
                return
            if prior.binding != returned.attempt.binding:
                self._retire_return(db, prior, returned)
                return
            row = self._return_row(db, "workflow-return-observation", returned)
            if row is None:
                raise WorkflowAuthorityError("paid return lacks observation scheduling evidence")
            previous = decode_workflow(row["payload_json"], PendingResolveReturn)
            if previous.returned != returned:
                raise WorkflowAuthorityError("paid return observation payload changed")
            pending = defer_return_observation(previous, error, now=self._control._now())
            db.execute("UPDATE dedupe_keys SET claimed_at=?,payload_json=? WHERE namespace='workflow-return-observation' AND scope=? AND key=?",
                       (pending.next_read_at.isoformat(), encode_workflow(pending), self.writer.repository, str(returned.attempt.run_id)))
            self._persist_snapshot(db, update_return_observation(prior, returned, pending))
            self._control._transition(db, "workflow_return", str(returned.attempt.run_id), "observation_deferred",
                                      {"failed_reads": pending.failed_reads, "next_read_at": pending.next_read_at.isoformat()})

    def _require_return(self, db: sqlite3.Connection, returned: ReturnedResolve) -> None:
        row = self._return_row(db, "workflow-return", returned)
        if row is None or row["payload_json"] != encode_workflow(returned):
            raise WorkflowAuthorityError("completion requires its exact immutable paid return")

    @contextmanager
    def return_effect_authority(self, returned: ReturnedResolve):
        with DeploymentAuthority(self._control, self.writer.repository, self.writer.role, self.writer.owner_id).effect():
            with self._control.transaction() as db:
                prior, _ = self._return_prior(db, returned)
                self._require_return(db, returned)
                if self._return_disposition(db, returned) is not None or prior.binding != returned.attempt.binding:
                    raise WorkflowAuthorityError("returned local effects require current unfinished authority")
                if not prior.gates or any(_opened(g).gate_id != return_gate_id(returned) for g in prior.gates):
                    raise WorkflowAuthorityError("returned local effects are held by another cause")
            # Acceptance/owner retirement requires the exclusive deployment
            # barrier; the shared barrier spans I/O but no SQL transaction does.
            yield

    def finalize_return(self, returned: ReturnedResolve, issue: ObservedIssue, linkage: NoLinkedPr | LinkedPrPresent,
                        *, cleanup: ResolveCleanupCompleted | None = None, contract_revision: ContractRevision | None = None) -> WorkflowCommitted | WorkflowConflict:
        with self._control.transaction() as db:
            prior, admission = self._return_prior(db, returned)
            self._require_return(db, returned)
            if self._return_disposition(db, returned) is not None:
                return WorkflowCommitted(prior, idempotent=True)
            if prior.binding != returned.attempt.binding:
                return WorkflowCommitted(self._retire_return(db, prior, returned))
            adjudications = admitted_adjudications(db, prior.binding)
            classified, actions = decide_return(returned, issue, linkage, now=self._control._now(),
                                               adjudications=adjudications,
                                               adjudication_ceiling=self._max_contract_rounds)
            remaining = replace(prior, gates=tuple(g for g in prior.gates if _opened(g).gate_id != return_gate_id(returned)))
            if any(not isinstance(action, Relabel) for action in actions):
                if remaining.gates:
                    return WorkflowConflict(prior.revision, "another workflow gate still holds local cleanup")
                expected_cleanup = (RemoveWorktree(issue.issue.number), ClearIssueState(pr=None, issue=issue.issue.number, merged=True))
                if actions != expected_cleanup or not isinstance(cleanup, ResolveCleanupCompleted) or cleanup.attempt != returned.attempt or cleanup.issue != issue or cleanup.linkage != linkage:
                    return WorkflowConflict(prior.revision, "closed-issue local cleanup requires durable completion evidence")
                actions = ()
            elif cleanup is not None:
                return WorkflowConflict(prior.revision, "unexpected cleanup acknowledgement for remote completion")
            if contract_revision is not None:
                if not isinstance(contract_revision, ContractRevision) or returned.stage not in (ResolveStage.AUTHOR, ResolveStage.ADJUDICATE) or returned.split_input is None or contract_revision.branch != split_context(returned.split_input).branch:
                    return WorkflowConflict(prior.revision, "recovered contract revision does not match the returned branch")
            try:
                cursor = None if cleanup is not None or issue.issue.state == "closed" or isinstance(linkage, LinkedPrPresent) else split_return_continuation(returned, admission, classified, issue=issue, contract_revision=contract_revision, adjudications=adjudications, adjudication_ceiling=self._max_contract_rounds)
            except ValueError as exc:
                return WorkflowConflict(prior.revision, str(exc))
            classified = replace(classified, split=cursor)
            if self._return_row(db, "workflow-result", returned) is not None:
                return WorkflowConflict(prior.revision, "paid return already has an independently recorded classification")
            self._validate_attempt_run(db, classified)
            proposal = record_resolve_attempt(prior, classified, ceiling=self._resolve_ceiling, admission=admission)
            if isinstance(proposal, WorkflowRejected):
                return WorkflowConflict(prior.revision, proposal.reason)
            after = proposal.snapshot
            # Capture did not advance the continuation watermark, so the final
            # blocked RESUME can still clear this exact run's returned session.
            after = replace(after, gates=tuple(g for g in after.gates if _opened(g).gate_id != return_gate_id(returned)),
                            revision=replace(prior.revision, version=prior.revision.version + 1))
            if issue.issue.state == "closed" or isinstance(linkage, LinkedPrPresent) or (
                returned.stage is ResolveStage.ADJUDICATE and classified.kind is ResolveResultKind.INFRA_FAILURE and cursor is None
            ):
                after = clear_return_split(after, admission)
            if cleanup is not None:
                after = apply_return_cleanup(after, cleanup)
            self._persist_snapshot(db, after)
            commands = []
            for index, action in enumerate(resolve_return_actions(after, actions, prior=prior)):
                command = command_for_action(action, repository=self.writer.repository, executor_identity=self.writer.owner_id)
                occurrence = hashlib.sha256(f"{returned.attempt.run_id}\0{index}\0{command.command_id}".encode()).hexdigest()
                command = replace(command, command_id=CommandId(occurrence), idempotency_key=f"resolve-return:{occurrence}")
                self._control._enqueue_command(db, command)
                db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-return-command',?,?,?,?)",
                           (self.writer.repository, str(command.command_id), self._control._now().isoformat(), encode_workflow(returned.attempt)))
                commands.append(command.command_id)
            self._insert_return_record(db, "workflow-result", returned, encode_workflow(classified))
            self._insert_return_record(db, "workflow-return-disposition", returned,
                                       encode_workflow(FinalizedResolveReturn(classified, issue, linkage, tuple(commands))))
            if cleanup is not None:
                self._insert_return_record(db, "workflow-return-cleanup", returned, encode_workflow(cleanup))
            if contract_revision is not None:
                self._insert_return_record(db, "workflow-return-contract", returned, encode_workflow(ContractBaseline(returned.attempt, contract_revision)))
            self._control._transition(db, "workflow_return", str(returned.attempt.run_id), "finalized",
                                      {"classification": classified.kind.value, "commands": commands})
            return WorkflowCommitted(after)

    def _finalized_source(self, db: sqlite3.Connection, attempt: WorkflowAttemptIdentity) -> tuple[ReturnedResolve, ResolveAdmission, FinalizedResolveReturn]:
        self._scope(attempt.binding.fence.key)
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return' AND scope=? AND key=?", (self.writer.repository, str(attempt.run_id))).fetchone()
        if row is None:
            raise WorkflowAuthorityError("reset predecessor lacks its immutable paid return")
        returned = decode_workflow(row["payload_json"], ReturnedResolve)
        if returned.attempt != attempt:
            raise WorkflowAuthorityError("reset predecessor belongs to another attempt")
        admission = self._validate_attempt_run(db, self._return_proof(returned))
        run = db.execute("SELECT dispatch_id FROM runs WHERE run_id=?", (str(attempt.run_id),)).fetchone()
        metadata = json.loads(run["dispatch_id"])
        encoded = encode_workflow(returned.split_input)
        if returned.split_input is None or admission.split_fingerprint != hashlib.sha256(encoded.encode()).hexdigest() or metadata.get("split_input") != encoded or metadata.get("split_fingerprint") != admission.split_fingerprint or metadata.get("resolve_inputs") != encode_workflow(returned.inputs):
            raise WorkflowAuthorityError("reset predecessor disagrees with its immutable admitted input")
        disposition = self._return_disposition(db, returned)
        if not isinstance(disposition, FinalizedResolveReturn):
            raise WorkflowAuthorityError("reset predecessor requires a finalized paid return")
        return returned, admission, disposition

    def _source_baseline(self, db: sqlite3.Connection, returned: ReturnedResolve) -> ContractBaseline | None:
        disposition = self._return_disposition(db, returned)
        if not isinstance(disposition, FinalizedResolveReturn):
            raise WorkflowAuthorityError("reset baseline requires finalized predecessor evidence")
        row = self._return_row(db, "workflow-return-contract", returned)
        recovered = decode_workflow(row["payload_json"], ContractBaseline) if row is not None else None
        try:
            return reauthor_baseline(returned, disposition, recovered=recovered)
        except ValueError as exc:
            raise WorkflowAuthorityError(str(exc)) from exc

    def _reauthor_reset_intent(self, db: sqlite3.Connection, binding: WorkflowBinding, input_fingerprint: str) -> ReauthorResetIntent | None:
        if not isinstance(input_fingerprint, str) or len(input_fingerprint) != 64 or any(char not in "0123456789abcdef" for char in input_fingerprint):
            raise ValueError("re-author reset requires the replacement input SHA-256 digest")
        self._scope(binding.fence.key)
        self._control._assert_owner_active(db, self.writer.owner_id)
        state = load_workflow_row(db, binding.fence.key)
        if state is None or state.binding != binding or state.gates:
            raise WorkflowAuthorityError("re-author reset requires current unheld authority")
        continuation = state.episode.continuation
        if isinstance(continuation, AuthoredResolve) and continuation.author_admission.input_fingerprint == input_fingerprint:
            raise WorkflowAuthorityError("applicable author inputs do not require replacement")
        latest = reset_predecessor_admission(db, binding)
        if latest is None:
            if isinstance(continuation, AuthoredResolve):
                raise WorkflowAuthorityError("authored checkpoint has no admitted predecessor")
            return None
        if (latest.attempt.binding.fence.key, latest.attempt.binding.authority, latest.attempt.binding.episode_id) != (binding.fence.key, binding.authority, binding.episode_id):
            raise WorkflowAuthorityError("re-author predecessor belongs to another episode")
        if proven_unlaunched_run(db, latest.attempt.run_id):
            run = db.execute("SELECT dispatch_id FROM runs WHERE run_id=?", (str(latest.attempt.run_id),)).fetchone()
            metadata = json.loads(run["dispatch_id"])
            raw = metadata.get("split_input")
            if latest.stage is ResolveStage.AUTHOR and not isinstance(continuation, AuthoredResolve) and isinstance(raw, str) and latest.split_fingerprint == hashlib.sha256(raw.encode()).hexdigest() and metadata.get("split_fingerprint") == latest.split_fingerprint and isinstance(decode_workflow(raw, SplitContext), SplitContext):
                return None
            raise WorkflowAuthorityError("unlaunched reset source lacks a safe admitted predecessor")
        returned, admission, _ = self._finalized_source(db, latest.attempt)
        if admission != latest:
            raise WorkflowAuthorityError("re-author reset source disagrees with latest admitted authority")
        if db.execute("SELECT 1 FROM leases WHERE run_id=?", (str(latest.attempt.run_id),)).fetchone() is not None:
            raise WorkflowAuthorityError("re-author reset is blocked by an active process lease")
        baseline = self._source_baseline(db, returned)
        if baseline is None:
            if isinstance(continuation, AuthoredResolve):
                raise WorkflowAuthorityError("existing author checkpoint cannot discard baseline provenance")
            return None
        return ReauthorResetIntent(binding, latest.attempt, baseline, input_fingerprint)

    def reauthor_reset_intent(self, binding: WorkflowBinding, input_fingerprint: str) -> ReauthorResetIntent | None:
        with self._control.transaction() as db:
            return self._reauthor_reset_intent(db, binding, input_fingerprint)

    def _reset_state(self, db: sqlite3.Connection, intent: ResetIntent) -> WorkflowSnapshot:
        if not isinstance(intent, (ContractResetIntent, ReauthorResetIntent)):
            raise ValueError("contract reset requires a typed intent")
        self._scope(intent.binding.fence.key)
        self._control._assert_owner_active(db, self.writer.owner_id)
        state = load_workflow_row(db, intent.binding.fence.key)
        if state is None or state.binding != intent.binding or state.gates:
            raise WorkflowAuthorityError("contract reset requires current unheld authority")
        if isinstance(intent, ReauthorResetIntent):
            if self._reauthor_reset_intent(db, intent.binding, intent.input_fingerprint) != intent:
                raise WorkflowAuthorityError("re-author reset disagrees with current predecessor or baseline")
            return state
        continuation = state.episode.continuation
        if not isinstance(continuation, AuthoredResolve) or continuation.split != intent.petition:
            raise WorkflowAuthorityError("contract reset no longer owns the current petition")
        if admitted_adjudications(db, intent.binding) >= self._max_contract_rounds:
            raise WorkflowAuthorityError("contract adjudication allowance is exhausted")
        latest = latest_work_admission(db, intent.binding)
        if latest is not None and latest.stage is ResolveStage.ADJUDICATE and proven_unlaunched_run(db, latest.attempt.run_id):
            run = db.execute("SELECT dispatch_id FROM runs WHERE run_id=?", (str(latest.attempt.run_id),)).fetchone()
            supplied = decode_workflow(json.loads(run["dispatch_id"])["split_input"], AdjudicationInput)
            ack = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-contract-reset-completed' AND scope=? AND key=?", (self.writer.repository, intent.intent_id)).fetchone()
            if supplied.reset.intent == intent and ack is not None and decode_workflow(ack["payload_json"], ContractResetCompleted) == supplied.reset:
                return state
        if latest is None or latest.attempt != intent.petition.source:
            raise WorkflowAuthorityError("contract reset is blocked by a later active or returned admission")
        row = db.execute("SELECT runs.state, leases.run_id AS leased FROM runs LEFT JOIN leases USING(run_id) WHERE runs.run_id=?", (str(latest.attempt.run_id),)).fetchone()
        if row is None or row["state"] not in ("completed", "cancelled", "abandoned") or row["leased"] is not None:
            raise WorkflowAuthorityError("contract reset is blocked by an active run or process lease")
        done = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return-disposition' AND scope=? AND key=?", (self.writer.repository, str(latest.attempt.run_id))).fetchone()
        if done is None:
            raise WorkflowAuthorityError("contract reset requires its finalized paid predecessor")
        disposition = decode_workflow(done["payload_json"], FinalizedResolveReturn)
        if disposition.result.attempt != latest.attempt or disposition.result.split != intent.petition:
            raise WorkflowAuthorityError("contract reset predecessor does not own this petition")
        return state

    @contextmanager
    def contract_reset_authority(self, intent: ResetIntent):
        with deployment_effect_barrier(self._control.path, self.writer.repository, self.writer.role):
            with self._control.transaction() as db:
                self._reset_state(db, intent)
                encoded = encode_workflow(intent)
                row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-contract-reset' AND scope=? AND key=?", (self.writer.repository, intent.intent_id)).fetchone()
                if row is not None and row["payload_json"] != encoded:
                    raise WorkflowAuthorityError("contract reset identity already contains different intent")
                if row is None:
                    db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-contract-reset',?,?,?,?)", (self.writer.repository, intent.intent_id, self._control._now().isoformat(), encoded))
                done = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-contract-reset-completed' AND scope=? AND key=?", (self.writer.repository, intent.intent_id)).fetchone()
                acknowledgement = decode_workflow(done["payload_json"], ContractResetCompleted) if done is not None else None
                if acknowledgement is not None and acknowledgement.intent != intent:
                    raise WorkflowAuthorityError("contract reset acknowledgement belongs to different intent")
            # The exclusive barrier serializes reset callers; a replay with
            # retained acknowledgement must not mutate the worktree again.
            yield acknowledgement

    def complete_contract_reset(self, acknowledgement: ContractResetCompleted) -> ContractResetCompleted:
        if not isinstance(acknowledgement, ContractResetCompleted):
            raise ValueError("contract reset completion requires verified evidence")
        intent = acknowledgement.intent
        with self._control.transaction() as db:
            self._reset_state(db, intent)
            if not 0 <= (self._control._now() - acknowledgement.observed_at).total_seconds() <= 300:
                raise WorkflowAuthorityError("contract reset completion must be fresh")
            row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-contract-reset' AND scope=? AND key=?", (self.writer.repository, intent.intent_id)).fetchone()
            if row is None or row["payload_json"] != encode_workflow(intent):
                raise WorkflowAuthorityError("contract reset completion lacks its durable intent")
            prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-contract-reset-completed' AND scope=? AND key=?", (self.writer.repository, intent.intent_id)).fetchone()
            if prior is not None:
                stored = decode_workflow(prior["payload_json"], ContractResetCompleted)
                if stored.intent != intent or stored.result != acknowledgement.result:
                    raise WorkflowAuthorityError("contract reset completion disagrees with retained evidence")
                return stored
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-contract-reset-completed',?,?,?,?)", (self.writer.repository, intent.intent_id, acknowledgement.observed_at.isoformat(), encode_workflow(acknowledgement)))
            self._control._transition(db, "workflow_contract_reset", intent.intent_id, "verified", {"archive_ref": acknowledgement.result.archive_ref})
            return acknowledgement

    def record_attempt(self, result: ResolveAttemptResult) -> WorkflowCommitted | WorkflowConflict:
        """Every paid classification has one immutable audit, even without a strike."""
        if not isinstance(result, ResolveAttemptResult):
            raise ValueError("resolve attempt recording requires a typed result")
        key = result.attempt.binding.fence.key
        self._scope(key)
        encoded = encode_workflow(result)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            prior = load_workflow_row(db, key)
            if prior is None:
                raise WorkflowAuthorityError("workflow must be explicitly imported before recording results")
            admission = self._validate_attempt_run(db, result)
            if admission.split_fingerprint is not None:
                return WorkflowConflict(prior.revision, "split admission must use atomic return capture and observation finalization")
            if db.execute("SELECT 1 FROM dedupe_keys WHERE namespace='workflow-return' AND scope=? AND key=?",
                          (self.writer.repository, str(result.attempt.run_id))).fetchone() is not None:
                return WorkflowConflict(prior.revision, "captured paid return must use observation finalization")
            receipt = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-result' AND scope=? AND key=?",
                (self.writer.repository, str(result.attempt.run_id)),
            ).fetchone()
            if receipt is not None:
                if receipt["payload_json"] != encoded:
                    return WorkflowConflict(prior.revision, "paid run already has a different recorded classification")
                return WorkflowCommitted(prior, idempotent=True)
            proposal = record_resolve_attempt(prior, result, ceiling=self._resolve_ceiling, admission=admission)
            if isinstance(proposal, WorkflowRejected):
                return WorkflowConflict(prior.revision, proposal.reason)
            if isinstance(proposal, WorkflowChanged):
                validate_resolve_transition(prior, proposal, ceiling=self._resolve_ceiling, now=self._control._now(), admission=admission)
                self._persist(db, prior, proposal, encoded)
                after = proposal.snapshot
            else:
                assert isinstance(proposal, WorkflowUnchanged)
                after = prior
            db.execute(
                "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, payload_json) "
                "VALUES ('workflow-result', ?, ?, ?, ?)",
                (self.writer.repository, str(result.attempt.run_id), self._control._now().isoformat(), encoded),
            )
            self._control._transition(db, "workflow_attempt", str(result.attempt.run_id), result.kind.value,
                                      {"generation": result.attempt.binding.fence.generation,
                                       "current_generation": after.revision.fence.generation})
            return WorkflowCommitted(after)

    def commit_workflow(
        self, expected: WorkflowRevision, authorities: tuple[FamilyAuthority, ...],
        transition: WorkflowChanged,
    ) -> WorkflowCommitted | WorkflowConflict:
        if not isinstance(expected, WorkflowRevision) or not isinstance(transition, WorkflowChanged):
            raise ValueError("commit requires a typed revision and proposed transition")
        self._scope(expected.fence.key)
        if transition.expected != expected:
            raise WorkflowAuthorityError("commit and transition expect different workflow revisions")
        if not isinstance(authorities, tuple) or authorities != (transition.snapshot.authority,):
            raise WorkflowAuthorityError("resolve commit requires exactly its captured family authority")
        encoded = encode_workflow(transition.audit) if isinstance(transition.audit, ResolveAttemptResult) else _accepted_proposal(transition)
        namespace, dedupe = _dedupe_key(transition)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            prior = load_workflow_row(db, expected.fence.key)
            if prior is None:
                raise WorkflowAuthorityError("workflow must be explicitly imported before mutation")
            if authorities != (prior.authority,):
                raise WorkflowAuthorityError("workflow family activation epoch changed")
            if isinstance(transition.audit, ResolveAttemptResult) and db.execute(
                "SELECT 1 FROM dedupe_keys WHERE namespace='workflow-return' AND scope=? AND key=?",
                (self.writer.repository, str(transition.audit.attempt.run_id)),
            ).fetchone() is not None:
                return WorkflowConflict(prior.revision, "captured paid return must use observation finalization")
            receipt = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (namespace, self.writer.repository, dedupe),
            ).fetchone()
            if receipt is not None:
                if not _same_receipt(receipt["payload_json"], transition, encoded):
                    return WorkflowConflict(prior.revision, "durable workflow identity was reused with different content")
                return WorkflowCommitted(prior, idempotent=True)
            if prior.revision != expected:
                return WorkflowConflict(prior.revision, "workflow revision changed before commit")
            observations = self._validate_evidence(db, prior, transition)
            admission = self._validate_attempt_run(db, transition.audit) if isinstance(transition.audit, ResolveAttemptResult) else None
            accepted_preflight = None
            if isinstance(prior.intent, PendingRearm):
                row = db.execute("SELECT actor_json FROM workflow_rearms WHERE request_id=?", (prior.intent.request_id,)).fetchone()
                if row is None:
                    raise WorkflowAuthorityError("pending intent lacks its accepted audit")
                accepted_preflight = decode_workflow(row["actor_json"], AuthorizedOperatorCommand).preflight
            validate_resolve_transition(prior, transition, ceiling=self._resolve_ceiling, now=self._control._now(), observations=observations, accepted_preflight=accepted_preflight, admission=admission)
            self._persist(db, prior, transition, encoded)
            db.execute(
                "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (namespace, self.writer.repository, dedupe, self._control._now().isoformat(), encoded),
            )
            self._control._transition(
                db, "workflow", encode_workflow(expected.fence.key), transition.mutation.value,
                {"generation": transition.snapshot.revision.fence.generation,
                 "version": transition.snapshot.revision.version},
            )
            return WorkflowCommitted(transition.snapshot)

    def _validate_evidence(
        self, db: sqlite3.Connection, prior: WorkflowSnapshot, proposal: WorkflowChanged,
    ) -> tuple[ResolveRearmPreflight | ObservedIssue | LinkedPrPresent, ...]:
        audit = proposal.audit
        scope = workflow_scope(prior.revision.fence.key)
        if isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, (RequestRearm, ContinueGrantedRearm)):
            for row in db.execute(
                "SELECT runs.workflow_binding_json, runs.dispatch_id FROM runs LEFT JOIN leases USING(run_id) "
                "WHERE (runs.state IN ('reserved', 'starting', 'running') OR leases.run_id IS NOT NULL) "
                "AND (runs.owner_id=? OR substr(runs.owner_id, 1, ?)=?)",
                (self.writer.repository, len(self.writer.repository) + 1, self.writer.repository + ":"),
            ):
                if row["workflow_binding_json"] is not None:
                    binding = decode_workflow(row["workflow_binding_json"], WorkflowBinding)
                    if binding.fence.key == prior.revision.fence.key:
                        raise WorkflowAuthorityError("workflow has an active admitted run or process lease")
                else:
                    # Legacy live runs are fenced by the same repository/issue
                    # metadata; migration must not conceal an undrained writer.
                    try:
                        metadata = json.loads(row["dispatch_id"])
                    except (ValueError, TypeError):
                        raise WorkflowAuthorityError("active run lacks verifiable workflow scope") from None
                    if not isinstance(metadata, dict):
                        raise WorkflowAuthorityError("active run lacks verifiable workflow scope")
                    if metadata.get("repository") == scope[0] and (metadata.get("issue") == scope[2] or metadata.get("number") == scope[2]):
                        raise WorkflowAuthorityError("workflow has an undrained legacy run")
        observations: list[ResolveRearmPreflight | ObservedIssue | LinkedPrPresent] = []
        if isinstance(audit, ObservedTransition):
            for evidence_id in audit.evidence_ids:
                row = db.execute(
                    "SELECT kind, payload_json FROM workflow_evidence WHERE evidence_id=? AND repository=? AND target_kind=? AND target_number=?",
                    (evidence_id, *scope),
                ).fetchone()
                if row is None:
                    raise WorkflowAuthorityError("observed transition lacks its durable harness evidence")
                if isinstance(proposal.snapshot.intent, (CompletedRearm, SupersededRearm)):
                    if row["kind"] != "observation":
                        raise WorkflowAuthorityError("workflow progression requires an observation evidence variant")
                    observations.append(decode_workflow(row["payload_json"], ResolveRearmPreflight | ObservedIssue | LinkedPrPresent))
        intent = proposal.snapshot.intent
        if isinstance(intent, (CompletedRearm, CancelledRearm, SupersededRearm)):
            for command_id in intent.verified_commands:
                row = db.execute(
                    "SELECT lifecycle_commands.state, lifecycle_commands.executor_identity, lifecycle_commands.attempt_owner, "
                    "lifecycle_commands.workflow_binding_json, effect_receipts.status, effect_receipts.executor_identity AS receipt_owner FROM lifecycle_commands "
                    "JOIN effect_receipts USING(command_id) WHERE command_id=?", (str(command_id),),
                ).fetchone()
                if row is None or row["state"] != EffectStatus.SUCCEEDED.value or row["status"] != EffectStatus.SUCCEEDED.value or not self._owns_command(db, row["executor_identity"]) or not row["attempt_owner"] or row["receipt_owner"] != row["attempt_owner"] or decode_workflow(row["workflow_binding_json"], WorkflowBinding) != prior.binding:
                    raise WorkflowAuthorityError("re-arm verification lacks a successful durable command receipt")
        if isinstance(intent, SupersededRearm) and intent.reason is StopReason.REQUIRED_EFFECT_REJECTED:
            rejected = False
            for command_id in intent.retired_obligations:
                row = db.execute(
                "SELECT lifecycle_commands.executor_identity FROM lifecycle_commands JOIN effect_receipts USING(command_id) "
                "WHERE command_id=? AND state='permanent_failure' AND status='permanent_failure' "
                "AND attempt_owner IS NOT NULL AND effect_receipts.executor_identity=attempt_owner AND workflow_binding_json=?",
                (str(command_id), encode_workflow(prior.binding)),
                ).fetchone()
                if row is not None and self._owns_command(db, row["executor_identity"]):
                    rejected = True
            if not rejected:
                raise WorkflowAuthorityError("effect-rejected supersession requires an exact required command rejection")
        for command in proposal.commands:
            if command.executor_identity != self.writer.owner_id:
                raise WorkflowAuthorityError("re-arm command does not belong to the accepting fix owner")
        return tuple(observations)

    def _validate_attempt_run(self, db: sqlite3.Connection, result: ResolveAttemptResult) -> ResolveAdmission:
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (str(result.attempt.run_id),)).fetchone()
        if row is None or row["owner_id"] != self.writer.owner_id or row["workflow_binding_json"] is None:
            raise WorkflowAuthorityError("resolve result has no owning admitted run")
        binding = decode_workflow(row["workflow_binding_json"], WorkflowBinding)
        try:
            metadata = json.loads(row["dispatch_id"])
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("resolve run has malformed attempt metadata") from exc
        if binding != result.attempt.binding or not isinstance(metadata, dict) or metadata.get("leg_id") != result.attempt.leg_id:
            raise WorkflowAuthorityError("resolve attempt disagrees with its admitted run")
        if row["kind"] != result.stage.value or metadata.get("kind") != result.stage.value:
            raise WorkflowAuthorityError("resolve result stage disagrees with its admitted run")
        if row["state"] not in ("completed", "cancelled", "abandoned"):
            raise WorkflowAuthorityError("resolve result requires a terminal admitted run")
        receipt = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND scope=? AND key=?",
            (self.writer.repository, str(result.attempt.run_id)),
        ).fetchone()
        if receipt is None:
            raise WorkflowAuthorityError("resolve result lacks its durable admission receipt")
        admission = decode_workflow(receipt["payload_json"], ResolveAdmission)
        if admission.attempt != result.attempt or admission.stage is not result.stage:
            raise WorkflowAuthorityError("resolve result disagrees with its durable admission receipt")
        if admission.input_fingerprint != metadata.get("input_fingerprint"):
            raise WorkflowAuthorityError("resolve admission disagrees with its actual task input fingerprint")
        if result.kind not in (ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL) or result.session is not None:
            reservation = db.execute(
                "SELECT workload_repository, model, state, provider_receipt_id, basis "
                "FROM budget_reservations JOIN charges USING(run_id) WHERE run_id=?", (str(result.attempt.run_id),),
            ).fetchone()
            runtime = metadata.get("runtime")
            receipt_id = row["provider_receipt_id"]
            normal_receipt = (
                isinstance(runtime, str) and bool(runtime)
                and isinstance(receipt_id, str)
                and receipt_id.startswith(f"{runtime}:")
                and receipt_id.endswith(f":{result.attempt.run_id}")
                and len(receipt_id) > len(runtime) + len(result.attempt.run_id) + 2
            )
            if (
                row["state"] != "completed" or reservation is None or reservation["state"] != "settled"
                or reservation["workload_repository"] != self.writer.repository
                or not normal_receipt or reservation["provider_receipt_id"] != receipt_id
                or reservation["basis"] not in {basis.value for basis in PricingBasis if basis is not PricingBasis.RECONCILED}
            ):
                raise WorkflowAuthorityError("resolve progress lacks normal executed settlement provenance")
        else:
            reservation = None
        if result.session is not None:
            session = result.session
            if (
                session.session_id == "no-session"
                or row["provider_receipt_id"] != f"{session.runtime}:{session.session_id}:{result.attempt.run_id}"
                or metadata.get("runtime") != session.runtime
                or reservation is None or reservation["model"] != session.model
            ):
                raise WorkflowAuthorityError("resolve session lacks exact provider and selected-model provenance")
        return admission

    def _owns_command(self, db: sqlite3.Connection, owner: str) -> bool:
        """Terminal receipts retain their executor through audited adoption."""
        seen: set[str] = set()
        for _ in range(64):
            if owner == self.writer.owner_id:
                return True
            if owner in seen:
                return False
            seen.add(owner)
            row = db.execute(
                "SELECT successor_owner_id, repository, role FROM retired_owners WHERE owner_id=?", (owner,),
            ).fetchone()
            if row is None or row["repository"] != self.writer.repository or row["role"] != self.writer.role:
                return False
            owner = row["successor_owner_id"]
        return False

    def _persist_snapshot(self, db: sqlite3.Connection, after: WorkflowSnapshot) -> None:
        scope = workflow_scope(after.revision.fence.key)
        db.execute(
            "UPDATE workflows SET generation=?, version=?, episode_json=? "
            "WHERE repository=? AND target_kind=? AND target_number=?",
            (after.revision.fence.generation, after.revision.version,
             encode_workflow((after.authority, after.episode)), *scope),
        )
        # Closed causes remain forensic history, never a runtime file fallback.
        db.execute(
            "UPDATE workflow_gates SET state='closed' WHERE repository=? AND target_kind=? AND target_number=?",
            scope,
        )
        for gate in after.gates:
            opened = _opened(gate)
            cause_key = hashlib.sha256(encode_workflow(opened.cause).encode()).hexdigest()
            existing = db.execute("SELECT repository, target_kind, target_number FROM workflow_gates WHERE gate_id=?", (opened.gate_id,)).fetchone()
            if existing is not None and tuple(existing) != scope:
                raise WorkflowAuthorityError("gate identity belongs to another workflow")
            db.execute(
                "INSERT INTO workflow_gates VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(gate_id) DO UPDATE SET state=excluded.state, payload_json=excluded.payload_json",
                (opened.gate_id, *scope, opened.opened_generation, cause_key,
                 "rearming" if isinstance(gate, RearmingGate) else "open", encode_workflow(gate)),
            )
    def _persist(
        self, db: sqlite3.Connection, prior: WorkflowSnapshot,
        proposal: WorkflowChanged, encoded: str,
    ) -> None:
        after = proposal.snapshot
        self._persist_snapshot(db, after)
        scope = workflow_scope(after.revision.fence.key)
        intent = after.intent
        if intent is not None and intent != prior.intent:
            if isinstance(intent, PendingRearm):
                assert isinstance(proposal.audit, AuthorizedOperatorCommand)
                db.execute(
                    "INSERT INTO workflow_rearms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (intent.request_id, *scope, intent.gate_id, intent.from_generation, intent.to_generation,
                     "pending", hashlib.sha256(encode_workflow((proposal.audit.actor.subject, proposal.audit.command)).encode()).hexdigest(),
                     encode_workflow(proposal.audit), encode_workflow(intent)),
                )
            else:
                db.execute(
                    "UPDATE workflow_rearms SET state=?, intent_json=? WHERE request_id=?",
                    (_intent_state(intent), encode_workflow(intent), intent.request_id),
                )
        for command in proposal.commands:
            self._control._enqueue_command(db, command)
            assert isinstance(intent, PendingRearm)
            db.execute(
                "INSERT INTO workflow_rearm_commands VALUES (?, ?, ?, 'required', ?)",
                (intent.request_id, "ensure_lifecycle_projection", str(command.command_id), encode_workflow(proposal.audit)),
            )
        if isinstance(intent, (CancelledRearm, SupersededRearm)) and intent != prior.intent:
            retirement_id = "workflow-stop:" + hashlib.sha256(encode_workflow(proposal.audit).encode()).hexdigest()
            db.execute(
                "INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                (retirement_id, *scope, encode_workflow(proposal.audit)),
            )
            for command_id in intent.retired_obligations:
                db.execute(
                    "UPDATE workflow_rearm_commands SET disposition='retired', disposition_evidence_json=? "
                    "WHERE request_id=? AND command_id=?",
                    (encode_workflow(intent), intent.request_id, str(command_id)),
                )
                self._retire_command(db, after, str(command_id), retirement_id)

    def _retire_command(
        self, db: sqlite3.Connection, snapshot: WorkflowSnapshot,
        command_id: str, retirement_id: str,
    ) -> None:
        row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            raise WorkflowAuthorityError("retired obligation has no durable command")
        if not self._owns_command(db, row["executor_identity"]) or decode_workflow(row["workflow_binding_json"], WorkflowBinding) != snapshot.binding:
            raise WorkflowAuthorityError("retired obligation belongs to a different workflow owner")
        self._control._retire_workflow_command(
            db, row, _command_from_row(row), snapshot, retirement_id,
        )


def _intent_state(intent: RearmIntent) -> str:
    return {
        PendingRearm: "pending", CompletedRearm: "completed",
        CancelledRearm: "cancelled", SupersededRearm: "superseded",
    }[type(intent)]
