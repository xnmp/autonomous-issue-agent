"""Bound, generation-fenced SQLite workflow transitions; ADR 0015.

This adapter joins the existing control-store transaction and command outbox.
It never initializes authority, imports legacy state, or performs remote I/O.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime

from ..domain.contracts import EffectStatus, PricingBasis
from ..domain.workflow import (
    ActiveReconciliation, AuthorizedOperatorCommand, CancelRearm, CancelledRearm, CompletedRearm,
    ContinueGrantedRearm, HeldProjection, LinkedPrPresent, ObservedIssue, ObservedTransition, OpenGate,
    PendingRearm, RearmingGate, RearmIntent, ReconciliationOnly, RemoteOutcomeUncertain,
    RequestRearm, ResolveAdmission, ResolveAttemptResult, ResolveRearmPreflight, ResolveResultKind, RetiredSettledCommand, RetiredUncertainCommand,
    StopReason, SupersededRearm, WorkflowAbsent, WorkflowChanged, WorkflowCommitted,
    WorkflowConflict, WorkflowRead, WorkflowSnapshot, WorkflowUnavailable,
    WorkflowUnchanged, WorkflowRejected,
)
from ..domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision,
    WorkflowWriter, validate_workflow_identifier, validate_workflow_integer,
)
from ..domain.workflow_policy import initial_resolve_workflow, record_resolve_attempt, validate_resolve_transition
from .control_store import SQLiteControlStore
from .deployment_authority import deployment_role
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
        self, control: SQLiteControlStore, writer: WorkflowWriter, *, resolve_ceiling: int,
    ) -> None:
        if not isinstance(control, SQLiteControlStore) or not isinstance(writer, WorkflowWriter):
            raise ValueError("workflow control requires the existing store and a typed writer")
        if writer.role != "fix" or deployment_role(writer.repository, writer.owner_id) != writer.role:
            raise ValueError("resolve workflow authority requires the repository's fix owner")
        validate_workflow_integer(resolve_ceiling, "resolve ceiling")
        self._control = control
        self._writer = writer
        self._resolve_ceiling = resolve_ceiling

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

    def _persist(
        self, db: sqlite3.Connection, prior: WorkflowSnapshot,
        proposal: WorkflowChanged, encoded: str,
    ) -> None:
        after = proposal.snapshot
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
        receipt = db.execute("SELECT * FROM effect_receipts WHERE command_id=?", (command_id,)).fetchone()
        if receipt is not None and (not row["attempt_owner"] or receipt["executor_identity"] != row["attempt_owner"]):
            raise WorkflowAuthorityError("retired obligation receipt does not match its executing owner")
        scope = workflow_scope(snapshot.revision.fence.key)
        if row["state"] in ("executing", "unknown_outcome"):
            gate = next((g for g in snapshot.gates if isinstance(cause := _opened(g).cause, RemoteOutcomeUncertain) and str(cause.command_id) == command_id), None)
            if gate is None:
                raise WorkflowAuthorityError("retiring an uncertain command requires its visible uncertainty gate")
            cause = _opened(gate).cause
            assert isinstance(cause, RemoteOutcomeUncertain)
            raw = row["workflow_reconciliation_json"]
            if raw is None:
                since = datetime.fromisoformat(row["updated_at"])
                if cause.unresolved_since != since:
                    raise WorkflowAuthorityError("command uncertainty must retain its recorded start")
                history = ActiveReconciliation(since, max(since, self._control._now()), cause.failed_reads)
            else:
                previous = decode_workflow(raw, ActiveReconciliation | ReconciliationOnly)
                history = previous.history if isinstance(previous, ReconciliationOnly) else previous
                if cause.unresolved_since != history.unresolved_since or cause.failed_reads != history.failed_reads:
                    raise WorkflowAuthorityError("retirement cannot reset command uncertainty history")
            disposition = RetiredUncertainCommand(ReconciliationOnly(history, _opened(gate).gate_id), retirement_id)
        else:
            if row["state"] == "pending" and row["attempts"] == 0 and receipt is None:
                evidence = json.dumps({"command_id": command_id, "state": "pending", "attempts": 0, "observation": "never_began_locally"}, sort_keys=True)
            elif receipt is not None and row["state"] in ("succeeded", "retryable_failure", "permanent_failure") and receipt["status"] == row["state"]:
                # A verified refusal settles current uncertainty, not a claim
                # that earlier partial effects or paid work never happened.
                evidence = json.dumps(dict(receipt), sort_keys=True)
            else:
                raise WorkflowAuthorityError("command retirement lacks a settled outcome or uncertainty gate")
            outcome_id = "retired-outcome:" + hashlib.sha256(evidence.encode()).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO workflow_evidence VALUES (?, ?, ?, ?, ?, 'observation', ?)",
                (outcome_id, *scope, command_id, evidence),
            )
            disposition = RetiredSettledCommand(retirement_id, outcome_id)
        db.execute(
            "UPDATE lifecycle_commands SET workflow_reconciliation_json=? WHERE command_id=?",
            (encode_workflow(disposition), command_id),
        )


def _intent_state(intent: RearmIntent) -> str:
    return {
        PendingRearm: "pending", CompletedRearm: "completed",
        CancelledRearm: "cancelled", SupersededRearm: "superseded",
    }[type(intent)]
