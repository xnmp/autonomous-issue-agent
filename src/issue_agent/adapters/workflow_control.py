"""Bound, generation-fenced SQLite workflow transitions; ADR 0015.

This adapter joins the existing control-store transaction and command outbox.
It never initializes authority, imports legacy state, or performs remote I/O.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta

from ..domain.contracts import EffectStatus, PricingBasis, WorkflowAttemptIdentity
from ..domain import continuation_components as cc
from ..domain.actions import Target
from ..domain.labels import Label
from ..domain.events import ControlTransitionCommitted, Correlation
from ..domain.actions import ClearIssueState, Relabel, RemoveWorktree
from ..domain.contracts import CommandId
from ..domain.outcome import budget_exhausted, infra_class_failure
from ..domain.resolve_rearm import (
    AbsentResolveControl, InactiveResolveControl, PendingRetry, ResolveControlRead, ResolveControlView,
    PendingResolveRearm, RearmObservation, UnavailableResolveRearm,
    decide_rearm_reconciliation, defer_rearm_observation,
    schedule_rearm_observation, update_rearm_observation,
)
from ..domain.resolve_return import (
    FinalizedResolveReturn, PendingResolveReturn, ResolveCleanupCompleted, ReturnedResolve, RetiredResolveReturn, UnavailableResolveReturn,
)
from ..domain.resolve_split import (
    AdjudicationInput, ContractBaseline, ContractResetCompleted, ContractResetIntent,
    ContractRevision, NeedsAdjudication, ReauthorInput, ReauthorResetIntent, ResetIntent,
    SplitContext, SplitImplement, split_context, validate_contract_rounds,
)
from ..domain.resolve_workspace import (
    EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared, NoPriorResolveAdmission,
    ObservedResolveBase, PriorResolveAdmission, ResolveWorkspaceObserved,
)
from ..domain.workflow import (
    ActiveReconciliation, ReconciliationOnly, RetiredSettledCommand, RetiredUncertainCommand, AuthoredResolve, AuthorizeAbsentRetry, RearmObligation, RetryAuthorizationRequired, AuthorizedOperatorCommand, CancelRearm, CancelledRearm, CompletedRearm,
    ContinueGrantedRearm, HeldProjection, LinkedPrPresent, NoLinkedPr, ObservedIssue, ObservedTransition, OpenGate, OperatorActor,
    PendingRearm, RearmingGate, RearmIntent, RemoteOutcomeUncertain,
    RequestRearm, ResolveAdmission, ResolveAttemptResult, ResolveRearmAcceptance, ResolveRearmPreflight, ResolveResultKind, ResolveStage,
    StopReason, SupersededRearm, WorkflowAbsent, WorkflowChanged, WorkflowCommitted,
    WorkflowConflict, WorkflowMigrationRequired, WorkflowRead, WorkflowSnapshot, WorkflowUnavailable,
    WorkflowUnchanged, WorkflowRejected, WorkflowRejection,
)
from ..domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowRevision,
    WorkflowWriter, validate_workflow_identifier, validate_workflow_integer,
)
from ..domain.workflow_policy import (
    apply_return_cleanup, clear_return_split, decide_return, defer_return_observation, initial_resolve_workflow,
    cancel_resolve_rearm, reauthor_baseline, record_resolve_attempt, resolve_return_actions, return_gate_id,
    update_return_observation, validate_resolve_transition, split_return_continuation,
)
from ..domain.workflow_retry import RetryAuthorizationFacts, authorize_absent_retry, minted_authorization, retry_authorization_available
from .control_store import (
    load_retry_authorization_facts, AccountingDisposition, ControlStoreError, SettlementRequest, SettlementResult, SQLiteControlStore,
    _command_from_row, _stamp, admitted_adjudications, latest_work_admission,
    proven_unlaunched_run, require_workspace_predecessor, reset_predecessor_admission,
    target_has_work_admissions, workflow_run_scope_bounds,
)
from .effects import command_for_action
from .deployment_authority import DeploymentAuthority, deployment_role, deployment_effect_barrier
from .workflow_codec import decode_workflow, encode_workflow
from .workflow_commit import (
    insert_pending_rearm, intent_state, opened_gate, owns_command, persist_retry_authorization, record_commit,
    record_stop_evidence,
    replay_or_conflict, retire_rearm_obligation, transition_receipt, update_rearm_state,
)
from .workflow_rows import (
    load_issue_aggregate, load_review_workflow_row, load_family_authority,
    WORKSPACE_ACK_NAMESPACE, WorkflowAuthorityError, WorkflowRevisionConflict, load_resolve_workflow_row,
    load_resolve_authority, load_workspace_ack, persist_workflow_snapshot, workflow_scope, insert_workflow_component,
)


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

    def fix_admission_refusals(self, selected: cc.SelectedIssueRecovery) -> int:
        """Pre-admission refusals in this recovery episode since the issue's last
        admitted run (#262)."""
        key = selected.binding.fence.key
        self._scope(key)
        return self._control.fix_admission_refusals(self.writer.repository, key.target.number, selected.episode_scope)

    def reconcile_pr_repair_returns(self) -> int:
        return self._control.reconcile_pr_repair_returns(self.writer)

    def pr_repair_handoff(self, issue: int, pr, branch: str, runtime) -> cc.RepairHandoff | None:
        with self._control.transaction() as db:
            return self._pr_repair_handoff(db, issue, pr, branch, runtime)

    def _pr_repair_handoff(self, db, issue: int, pr, branch: str, runtime) -> cc.RepairHandoff | None:
        self._pr_repair_scope(WorkflowKey(self.writer.repository, pr))
        key = WorkflowKey(self.writer.repository, IssueRef(issue))
        self._control._assert_owner_active(db, self.writer.owner_id)
        if load_family_authority(db, WorkflowFamily.RESOLVE, key.repository) is None:
            return None
        state = load_issue_aggregate(db, key)
        if not isinstance(state, WorkflowSnapshot):
            return None
        session = state.episode.continuation.session
        if session is None or session.runtime != runtime.value:
            return None
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-return' AND scope=? AND key=?",
            (key.repository, str(session.source.run_id))).fetchone()
        if raw is None:
            raise WorkflowAuthorityError("repair handoff lacks its exact returned resolve receipt")
        returned = decode_workflow(raw[0], ReturnedResolve)
        self._return_prior(db, returned)
        done = self._return_disposition(db, returned)
        run = db.execute("SELECT dispatch_id FROM runs WHERE run_id=?", (str(session.source.run_id),)).fetchone()
        dispatch = json.loads(run[0]) if run is not None else {}
        if (returned.attempt != session.source or returned.session != session or session.source.binding != state.binding
                or not isinstance(done, FinalizedResolveReturn) or not isinstance(done.linkage, LinkedPrPresent)
                or done.linkage.pr != pr.number or dispatch.get('publication_branch') != branch
                or dispatch.get('issue') != issue):
            raise WorkflowAuthorityError("repair handoff has unsupported source, branch or classification")
        if not isinstance(state.issue_recovery, cc.ActiveIssueRecovery):
            raise WorkflowAuthorityError("repair handoff has unknown issue resume/fork history")
        return cc.RepairHandoff(session.source, session.session_id, runtime, session.model, branch, pr, state.issue_recovery.state.resumes)
    def _cleanup_selection(self, db, key: WorkflowKey, merged: bool, source: cc.PRRepairCleanup):
        self._pr_repair_scope(key)
        if not isinstance(source, cc.PRRepairCleanup) or source.binding.fence.key != key or source.merged != merged:
            raise WorkflowAuthorityError("PR cleanup requires its exact selected source")
        self._control._assert_owner_active(db, self.writer.owner_id)
        state = load_review_workflow_row(db, key)
        if state is None or not isinstance(state.pr_repair, cc.ActivePRRepair):
            raise WorkflowAuthorityError("PR cleanup is inactive or migration blocked")
        component = state.pr_repair
        selected = cc.select_pr_repair(state)
        if (not isinstance(selected, cc.SelectedPRRepair)
                or source.binding != selected.binding or source.revision != state.revision or source.merged != merged):
            raise WorkflowAuthorityError("PR cleanup source revision is no longer current")
        self._control._validate_pr_repair_sources(db, selected)
        if any(isinstance(remedy, (cc.OwedCIRemediation, cc.OwedPRRecoveryEffect)) for remedy in (
                component.state.ci_rerun, component.state.ci_cancel,
                component.state.branch_refresh, component.state.pending_ci_escalation)):
            raise WorkflowAuthorityError("repair cleanup retains unsettled remote remediation evidence")
        if (component.watermarks.last_admitted > component.watermarks.last_applied
                and component.watermarks.last_admitted_fence == state.revision.fence):
            raise WorkflowAuthorityError("repair cleanup requires its selected paid output settled")
        return state, component

    def validate_pr_repair_cleanup(self, key: WorkflowKey, merged: bool, source: cc.PRRepairCleanup) -> None:
        with self._control.transaction() as db:
            self._cleanup_selection(db, key, merged, source)

    def cleanup_pr_repair(self, key: WorkflowKey, merged: bool, source: cc.PRRepairCleanup) -> None:
        with self._control.transaction() as db, self._control.mutation_savepoint(db):
            state, component = self._cleanup_selection(db, key, merged, source)
            continuity = cc.retire_pr_repair_continuity(component.state, merged)
            if continuity != component.state:
                after = replace(state, revision=WorkflowRevision(state.revision.fence, state.revision.version + 1),
                    pr_repair=replace(component, state=continuity))
                persist_workflow_snapshot(db, after)

    def _pr_repair_scope(self, key: WorkflowKey) -> None:
        from ..domain.workflow_identity import PullRequestRef
        if not isinstance(key, WorkflowKey) or key.repository != self.writer.repository or not isinstance(key.target, PullRequestRef):
            raise WorkflowAuthorityError("PR repair is outside the bound writer scope")

    def read_pr_repair(self, key: WorkflowKey) -> cc.PRRepairSelection:
        self._pr_repair_scope(key)
        return self._control.read_pr_repair(key)

    def read_issue_recovery(self, key: WorkflowKey) -> cc.IssueRecoverySelection:
        self._scope(key)
        return self._control.read_issue_recovery(key)

    def reset_ci_episode(self, source: cc.CIEpisodeReset) -> bool:
        self._scope(source.binding.fence.key)
        return self._control.reset_ci_episode(source, self.writer)

    def reset_rebase_allowance(self, source: cc.RebaseAllowanceReset) -> bool:
        self._scope(source.binding.fence.key)
        return self._control.reset_rebase_allowance(source, self.writer)

    def consume_blank_pr_reply(self, binding: WorkflowBinding, reply_at: str, body: str) -> bool:
        from .workflow_rows import load_review_workflow_row, persist_workflow_snapshot
        if (not isinstance(binding, WorkflowBinding) or binding.authority.family is not WorkflowFamily.PR_REPAIR
                or not cc.reply_advances(reply_at, "") or not isinstance(body, str)
                or len(body.encode()) > 65536 or body.strip() or self.writer.role != "fix"):
            raise WorkflowAuthorityError("unpaid PR reply requires its exact authority, timestamp and blank content")
        self._pr_repair_scope(binding.fence.key)
        receipt_key = hashlib.sha256(encode_workflow((binding.fence.key, reply_at)).encode()).hexdigest()
        request = encode_workflow((binding, reply_at, body))
        with self._control.transaction() as db, self._control.mutation_savepoint(db):
            self._control._assert_owner_active(db, self.writer.owner_id)
            receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='pr-reply-noop' AND scope=? AND key=?",
                                 (self.writer.repository, receipt_key)).fetchone()
            if receipt is not None:
                if receipt[0] != request:
                    raise WorkflowAuthorityError("blank PR reply conflicts with its original consumption")
                return False
            aggregate = load_review_workflow_row(db, binding.fence.key)
            selected = cc.select_pr_repair(aggregate) if aggregate is not None else None
            if not isinstance(selected, cc.SelectedPRRepair) or selected.binding != binding:
                raise WorkflowAuthorityError("blank PR reply requires current PR repair authority")
            assert aggregate is not None
            self._control._validate_pr_repair_sources(db, selected)
            marker = selected.component.state.consumed_reply_at
            if marker == reply_at:
                raise WorkflowAuthorityError("blank PR reply lost its exact consumption receipt")
            if not cc.reply_advances(reply_at, marker):
                return False
            component = replace(selected.component, state=replace(selected.component.state, consumed_reply_at=reply_at,
                                                                   blank_reply=cc.PRBlankReplySource(binding, reply_at)))
            persist_workflow_snapshot(db, replace(aggregate, pr_repair=component,
                revision=replace(aggregate.revision, version=aggregate.revision.version+1)))
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('pr-reply-noop',?,?,?,?)",
                       (self.writer.repository, receipt_key, self._control._now().isoformat(), request))
            return True

    def load_workflow(self, key: WorkflowKey) -> WorkflowRead:
        self._scope(key)
        try:
            with closing(self._control._connect()) as db:
                db.execute("BEGIN")
                if load_resolve_authority(db, key.repository) is None:
                    return WorkflowMigrationRequired(key)
                from ..domain.workflow import IssueWorkflowShell
                snapshot = load_issue_aggregate(db, key)
                if isinstance(snapshot, IssueWorkflowShell):
                    return (WorkflowMigrationRequired(key) if snapshot.revision.fence.generation != 0
                            or isinstance(snapshot.issue_recovery, cc.BlockedIssueRecovery)
                            or isinstance(snapshot.steward, cc.BlockedSteward) else WorkflowAbsent(key))
                return snapshot if snapshot is not None else WorkflowAbsent(key)
        except sqlite3.OperationalError as exc:
            return WorkflowUnavailable(key, f"workflow read unavailable: {exc}")

    def consume_blank_issue_reply(self, binding: WorkflowBinding, reply_at: str, body: str) -> bool:
        if not isinstance(binding, WorkflowBinding) or not cc.reply_advances(reply_at, ""):
            raise WorkflowAuthorityError("blank reply requires its exact binding and timestamp")
        self._scope(binding.fence.key)
        if not isinstance(body, str) or len(body.encode()) > 65536 or body.strip():
            raise WorkflowAuthorityError("unpaid reply consumption requires blank content")
        receipt_key = hashlib.sha256(encode_workflow((binding.fence.key, reply_at)).encode()).hexdigest()
        command = command_for_action(Relabel(Target.ISSUE, binding.fence.key.target.number, Label.AI_WORKING, Label.AI_BLOCKED, workflow=binding), repository=self.writer.repository, executor_identity=self.writer.owner_id)
        command = replace(command, command_id=CommandId(receipt_key), idempotency_key="issue-reply-noop:" + receipt_key)
        request = encode_workflow((binding, reply_at, command.executor_identity, json.dumps(dict(command.payload), sort_keys=True, separators=(",", ":"))))
        with self._control.transaction() as db, self._control.mutation_savepoint(db):
            self._control._assert_owner_active(db, self.writer.owner_id)
            receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='issue-reply-noop' AND scope=? AND key=?", (self.writer.repository, receipt_key)).fetchone()
            if receipt is not None:
                if receipt[0] != request:
                    raise WorkflowAuthorityError("blank reply conflicts with its original consumption")
                selected_command = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=? AND typeof(payload_json)='text' AND length(CAST(payload_json AS BLOB))<=262144", (receipt_key,)).fetchone()
                if selected_command is None or _command_from_row(selected_command) != command:
                    raise WorkflowAuthorityError("blank reply lost its exact original projection command")
                self._control._require_issue_reply_projection(db, binding.fence.key, reply_at, allow_unresolved=True)
                return True
            if db.execute("SELECT 1 FROM lifecycle_commands WHERE command_id=?", (receipt_key,)).fetchone() is not None:
                raise WorkflowAuthorityError("blank reply lost its selected consumption receipt")
            prior = load_issue_aggregate(db, binding.fence.key)
            if not isinstance(prior, WorkflowSnapshot) or prior.binding != binding or HeldProjection(prior).held:
                raise WorkflowAuthorityError("blank reply requires current unheld resolve authority")
            selected = cc.select_issue_recovery(prior)
            if not isinstance(selected, cc.SelectedIssueRecovery) or selected.component.gates:
                raise WorkflowAuthorityError("blank reply continuation is inactive, blocked or held")
            if not cc.reply_advances(reply_at, selected.component.state.consumed_reply_at):
                return False
            self._control._require_issue_reply_projection(db, binding.fence.key, selected.component.state.consumed_reply_at)
            component = replace(selected.component, state=replace(selected.component.state, consumed_reply_at=reply_at))
            after = replace(prior, issue_recovery=component, revision=replace(prior.revision, version=prior.revision.version + 1))
            self._persist_snapshot(db, after)
            self._control._enqueue_command(db, command)
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('issue-reply-noop',?,?,?,?)", (self.writer.repository, receipt_key, self._control._now().isoformat(), request))
            if self._control._event_factory is not None:
                event = ControlTransitionCommitted("issue_reply", receipt_key, "consumed_blank", {"reply_at": reply_at, "command_id": str(command.command_id)})
                self._control.enqueue_event(self._control._event_factory(event, Correlation(issue=binding.fence.key.target.number)), db)
            return True

    def resolve_control_view(self, key: WorkflowKey, *, request_id: str | None = None) -> ResolveControlRead:
        """One read snapshot; inspection never schedules recovery or initializes state."""
        self._scope(key)
        if request_id is not None:
            validate_workflow_identifier(request_id, "requested rearm")
        try:
            with closing(self._control._connect()) as db:
                db.execute("BEGIN")
                self._control._assert_owner_active(db, self.writer.owner_id)
                authority = load_resolve_authority(db, self.writer.repository)
                if authority is None:
                    return InactiveResolveControl(key)
                snapshot = load_resolve_workflow_row(db, key)
                if snapshot is None:
                    return AbsentResolveControl(key, authority)
                selected_id = request_id or (snapshot.intent.request_id if snapshot.intent is not None else None)
                requested = self._lookup_rearm(db, key, selected_id) if selected_id is not None else None
                observation = self._rearm_observation(db, snapshot) if isinstance(snapshot.intent, PendingRearm) else None
                return ResolveControlView(snapshot, selected_id, requested, observation,
                                          self._retry_authorizable(db, snapshot), self._retry_pending(db, snapshot))
        except (sqlite3.Error, ValueError, TypeError) as exc:
            reason = (str(exc) or type(exc).__name__).replace("\x00", "")[:16384]
            return WorkflowUnavailable(key, reason)

    def ensure_observed_workflow(self, observation: ObservedIssue) -> WorkflowCommitted:
        if not isinstance(observation, ObservedIssue):
            raise ValueError("workflow discovery requires a verified present issue")
        self._scope(observation.key)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            prior = load_issue_aggregate(db, observation.key)
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
            if isinstance(prior, WorkflowSnapshot):
                return WorkflowCommitted(prior, idempotent=True)
            scope = workflow_scope(observation.key)
            from ..domain.workflow import IssueWorkflowShell
            from .workflow_rows import encode_workflow_episodes, initialize_new_continuations
            if isinstance(prior, IssueWorkflowShell):
                if (prior.revision.fence.generation != 0 or isinstance(prior.issue_recovery, cc.BlockedIssueRecovery)
                        or isinstance(prior.steward, cc.BlockedSteward)):
                    raise WorkflowAuthorityError("historical issue migration hold prevents primary discovery")
                snapshot = replace(snapshot, revision=replace(prior.revision, version=prior.revision.version + 1),
                                   issue_recovery=prior.issue_recovery, steward=prior.steward)
                db.execute("UPDATE workflows SET version=?,episode_json=? WHERE repository=? AND target_kind=? AND target_number=?",
                           (snapshot.revision.version, encode_workflow_episodes(snapshot), *scope))
            else:
                db.execute("INSERT INTO workflows VALUES (?, ?, ?, 0, 0, ?)",
                           (*scope, encode_workflow_episodes(snapshot)))
            snapshot = initialize_new_continuations(db, snapshot)
            if not isinstance(snapshot, WorkflowSnapshot):
                raise WorkflowAuthorityError("issue discovery lost its resolve snapshot")
            db.execute("UPDATE workflows SET episode_json=? WHERE repository=? AND target_kind=? AND target_number=?",
                       (encode_workflow_episodes(snapshot), *scope))
            insert_workflow_component(db, observation.key, authority, snapshot.episode.episode_id)
            db.execute(
                "INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                (snapshot.episode.episode_id, *scope, encode_workflow(observation)),
            )
            self._control._transition(db, "workflow", encode_workflow(observation.key), "discovered",
                                      {"generation": snapshot.revision.fence.generation,
                                       "version": snapshot.revision.version, "epoch": authority.epoch})
            return WorkflowCommitted(snapshot)

    def lookup_rearm(self, key: WorkflowKey, request_id: str) -> RearmIntent | None:
        self._scope(key)
        validate_workflow_identifier(request_id, "request")
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            load_resolve_workflow_row(db, key)
            return self._lookup_rearm(db, key, request_id)

    @staticmethod
    def _lookup_rearm(db: sqlite3.Connection, key: WorkflowKey, request_id: str) -> RearmIntent | None:
        row = db.execute(
            "SELECT * FROM workflow_rearms WHERE request_id=? AND repository=? "
            "AND target_kind=? AND target_number=?", (request_id, *workflow_scope(key)),
        ).fetchone()
        if row is None:
            return None
        intent = decode_workflow(row["intent_json"], PendingRearm | CompletedRearm | CancelledRearm | SupersededRearm)
        if intent.request_id != request_id or intent.gate_id != row["gate_id"] or intent.to_generation != row["to_generation"] or row["state"] != intent_state(intent):
            raise WorkflowAuthorityError("re-arm history disagrees with its typed payload")
        return intent

    def accepted_rearm(
        self, key: WorkflowKey, request_id: str,
    ) -> AuthorizedOperatorCommand | None:
        self._scope(key)
        validate_workflow_identifier(request_id, "request")
        with closing(self._control._connect()) as db:
            db.execute("BEGIN")
            load_resolve_workflow_row(db, key)
            return self._accepted_rearm(db, key, request_id)

    def _accepted_rearm(
        self, db: sqlite3.Connection, key: WorkflowKey, request_id: str,
    ) -> AuthorizedOperatorCommand | None:
        row = db.execute(
            "SELECT * FROM workflow_rearms WHERE request_id=?", (request_id,),
        ).fetchone()
        if row is None:
            return None
        if (row["repository"], row["target_kind"], row["target_number"]) != workflow_scope(key):
            raise WorkflowAuthorityError(
                "re-arm request identity belongs to another workflow"
            )
        try:
            audit = decode_workflow(row["actor_json"], AuthorizedOperatorCommand)
            intent = decode_workflow(
                row["intent_json"],
                PendingRearm | CompletedRearm | CancelledRearm | SupersededRearm,
            )
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("invalid accepted re-arm audit") from exc
        command = audit.command
        if (
            not isinstance(command, (RequestRearm, ContinueGrantedRearm))
            or command.request_id != request_id
            or command.expected.fence.key != key
            or audit.preflight is None
            or encode_workflow(audit) != row["actor_json"]
            or intent.request_id != request_id
            or intent.gate_id != row["gate_id"]
            or intent.to_generation != row["to_generation"]
            or row["state"] != intent_state(intent)
            or row["request_digest"] != hashlib.sha256(
                encode_workflow((audit.actor.subject, command)).encode()
            ).hexdigest()
        ):
            raise WorkflowAuthorityError(
                "accepted re-arm audit disagrees with its durable identity"
            )
        return audit

    def _rearm_observation(
        self, db: sqlite3.Connection, state: WorkflowSnapshot,
    ) -> RearmObservation:
        intent = state.intent
        if not isinstance(intent, PendingRearm):
            raise WorkflowAuthorityError("re-arm observation requires a pending intent")
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-rearm-observation' "
            "AND scope=? AND key=?",
            (self.writer.repository, intent.request_id),
        ).fetchone()
        if row is None:
            raise WorkflowAuthorityError("pending re-arm lacks observation scheduling evidence")
        try:
            observation = decode_workflow(row["payload_json"], RearmObservation)
            PendingResolveRearm(state, observation)
            return observation
        except (ValueError, TypeError) as exc:
            raise WorkflowAuthorityError("invalid pending re-arm observation") from exc

    def _schedule_rearm_audit(
        self, db: sqlite3.Connection, request_id: str, at: datetime,
    ) -> None:
        changed = db.execute(
            "UPDATE dedupe_keys SET claimed_at=? WHERE namespace='workflow-rearm-observation' "
            "AND scope=? AND key=?",
            (at.isoformat(), self.writer.repository, request_id),
        ).rowcount
        if not changed:
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('workflow-rearm-observation',?,?,?,NULL)",
                (self.writer.repository, request_id, at.isoformat()),
            )

    def pending_rearms(
        self, *, limit: int = 32,
    ) -> tuple[PendingResolveRearm | UnavailableResolveRearm, ...]:
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("re-arm reconciliation limit must be between 1 and 128")
        pending: list[PendingResolveRearm | UnavailableResolveRearm] = []
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            now = self._control._now()
            rows = db.execute(
                "SELECT rearm.request_id,rearm.target_kind,rearm.target_number "
                "FROM workflow_rearms rearm INDEXED BY workflow_one_pending_rearm "
                "LEFT JOIN dedupe_keys observation "
                "ON observation.namespace='workflow-rearm-observation' "
                "AND observation.scope=rearm.repository AND observation.key=rearm.request_id "
                "WHERE rearm.repository=? AND rearm.target_kind='issue' AND rearm.state='pending' "
                "AND COALESCE(julianday(observation.claimed_at),0)<=julianday(?) "
                "ORDER BY rearm.target_kind,rearm.target_number,rearm.request_id LIMIT ?",
                (self.writer.repository, now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                db.execute("SAVEPOINT pending_rearm")
                try:
                    if row["target_kind"] != "issue":
                        raise WorkflowAuthorityError("pending resolve re-arm has a non-issue target")
                    key = WorkflowKey(self.writer.repository, IssueRef(row["target_number"]))
                    state = load_resolve_workflow_row(db, key)
                    if state is None or not isinstance(state.intent, PendingRearm):
                        raise WorkflowAuthorityError("pending re-arm index lacks current workflow authority")
                    if state.intent.request_id != row["request_id"]:
                        raise WorkflowAuthorityError("pending re-arm index disagrees with current intent")
                    if self._accepted_rearm(db, key, row["request_id"]) is None:
                        raise WorkflowAuthorityError("pending re-arm lacks accepted operator audit")
                    observation = self._rearm_observation(db, state)
                    if observation.history.next_read_at <= now:
                        pending.append(PendingResolveRearm(state, observation))
                except (ValueError, TypeError, KeyError) as exc:
                    db.execute("ROLLBACK TO pending_rearm")
                    next_read = now + timedelta(hours=1)
                    raw_key = row["request_id"]
                    reason = (str(exc) or type(exc).__name__).replace("\x00", "\\0")[:16384]
                    try:
                        validate_workflow_identifier(raw_key, "re-arm request index")
                        record_key = raw_key
                    except ValueError:
                        record_key = "invalid-index:" + hashlib.sha256(
                            repr(raw_key).encode("utf-8", "backslashreplace")
                        ).hexdigest()
                        reason = "Invalid re-arm request index; " + reason[:16000]
                    self._schedule_rearm_audit(db, raw_key, next_read)
                    self._control._transition(
                        db, "workflow_rearm", record_key, "authority_unavailable",
                        {"reason": reason, "next_read_at": next_read.isoformat()},
                    )
                    pending.append(UnavailableResolveRearm(record_key, reason, next_read))
                finally:
                    db.execute("RELEASE pending_rearm")
        return tuple(pending)

    def defer_rearm(self, expected: WorkflowRevision, error: str) -> None:
        if not isinstance(expected, WorkflowRevision):
            raise ValueError("re-arm deferral requires a typed revision")
        self._scope(expected.fence.key)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            state = load_resolve_workflow_row(db, expected.fence.key)
            if state is None or state.revision != expected or not isinstance(state.intent, PendingRearm):
                raise WorkflowAuthorityError("re-arm deferral requires exact pending authority")
            previous = self._rearm_observation(db, state)
            now = self._control._now()
            deferred = defer_rearm_observation(previous, error, now=now)
            after = update_rearm_observation(state, deferred, now=now)
            changed = db.execute(
                "UPDATE dedupe_keys SET claimed_at=?,payload_json=? "
                "WHERE namespace='workflow-rearm-observation' AND scope=? AND key=?",
                (deferred.history.next_read_at.isoformat(), encode_workflow(deferred),
                 self.writer.repository, state.intent.request_id),
            ).rowcount
            if changed != 1:
                raise WorkflowAuthorityError("re-arm observation schedule changed before deferral")
            if after != state:
                self._persist_snapshot(db, after)
            self._control._transition(
                db, "workflow_rearm", state.intent.request_id, "observation_deferred",
                {"failed_reads": deferred.history.failed_reads,
                 "next_read_at": deferred.history.next_read_at.isoformat()},
            )

    def _rearm_command_facts(
        self, db: sqlite3.Connection, state: WorkflowSnapshot,
    ) -> tuple[tuple[CommandId, ...], tuple[CommandId, ...], tuple[RemoteOutcomeUncertain, ...]]:
        intent = state.intent
        if not isinstance(intent, PendingRearm):
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
            raise WorkflowAuthorityError("pending re-arm commands disagree with required obligations")
        verified: list[CommandId] = []
        rejected: list[CommandId] = []
        uncertainty: list[RemoteOutcomeUncertain] = []
        for row in rows:
            command = _command_from_row(row)
            if (
                row["disposition"] != "required"
                or command.workflow != state.binding
                or not owns_command(db, self.writer, command.executor_identity)
            ):
                raise WorkflowAuthorityError("pending re-arm command belongs to another authority")
            receipt_status = row["receipt_status"]
            reconciliation = self._control._command_reconciliation(row)
            if isinstance(reconciliation, (RetiredUncertainCommand, RetiredSettledCommand)):
                raise WorkflowAuthorityError("a pending re-arm obligation cannot already be retired")
            if isinstance(reconciliation, ReconciliationOnly):
                # Expired uncertainty keeps its own visible gate, so it is not
                # re-reported; a grant judged inapplicable is a verified
                # refusal of the obligation (ADR 0015 changed guards).
                if row["state"] not in ("executing", "unknown_outcome"):
                    raise WorkflowAuthorityError("reconciliation-only obligation lacks an unresolved outcome")
                self._control._require_reconciliation_gate(db, command, reconciliation)
                if self._control._retry_judged_inapplicable(db, command.command_id):
                    rejected.append(command.command_id)
                continue
            if receipt_status is None:
                if row["state"] not in ("pending", "executing"):
                    raise WorkflowAuthorityError("settled re-arm command lacks its exact receipt")
                history = self._control._unresolved_history(row)
                if history is not None:
                    uncertainty.append(RemoteOutcomeUncertain(
                        command.command_id, history.unresolved_since,
                        history.failed_reads, "command outcome remains unresolved",
                    ))
                continue
            try:
                status = EffectStatus(receipt_status)
                started = datetime.fromisoformat(row["receipt_started"])
                completed = datetime.fromisoformat(row["receipt_completed"])
            except (ValueError, TypeError) as exc:
                raise WorkflowAuthorityError("re-arm command has malformed receipt evidence") from exc
            if (
                row["state"] != status.value
                or not row["attempt_owner"]
                or row["receipt_executor"] != row["attempt_owner"]
                or completed < started
            ):
                raise WorkflowAuthorityError("re-arm command receipt disagrees with its execution")
            if status is EffectStatus.SUCCEEDED:
                verified.append(command.command_id)
            elif status is EffectStatus.PERMANENT_FAILURE:
                rejected.append(command.command_id)
            elif status is EffectStatus.UNKNOWN_OUTCOME:
                history = self._control._unresolved_history(row)
                assert history is not None
                uncertainty.append(RemoteOutcomeUncertain(
                    command.command_id, history.unresolved_since, history.failed_reads,
                    row["receipt_error"] or "command outcome remains unresolved",
                ))
            elif reconciliation is not None:
                raise WorkflowAuthorityError("settled command retained incompatible uncertainty")
        return tuple(verified), tuple(rejected), tuple(uncertainty)

    @staticmethod
    def _rearm_evidence_payload(
        proposal: WorkflowChanged, issue: ObservedIssue,
        linkage: NoLinkedPr | LinkedPrPresent, external_holds: tuple[str, ...],
    ) -> ResolveRearmPreflight | ObservedIssue | LinkedPrPresent:
        intent = proposal.snapshot.intent
        if isinstance(intent, CompletedRearm):
            if not isinstance(linkage, NoLinkedPr):
                raise WorkflowAuthorityError("completed re-arm requires verified absent linkage")
            return ResolveRearmPreflight(issue, linkage, external_holds)
        if isinstance(intent, SupersededRearm) and intent.reason is StopReason.DECISION_CHANGED and isinstance(linkage, LinkedPrPresent):
            return linkage
        if isinstance(intent, SupersededRearm) and intent.reason is StopReason.DECISION_CHANGED and isinstance(linkage, NoLinkedPr):
            return ResolveRearmPreflight(issue, linkage, external_holds)
        return issue

    def reconcile_rearm(
        self, expected: WorkflowRevision, issue: ObservedIssue,
        linkage: NoLinkedPr | LinkedPrPresent, *, external_holds: tuple[str, ...] = (),
        workspace_verified: EpisodeWorkspacePrepared | None = None,
    ) -> WorkflowCommitted | WorkflowConflict:
        if not isinstance(expected, WorkflowRevision) or not isinstance(issue, ObservedIssue) or not isinstance(linkage, (NoLinkedPr, LinkedPrPresent)):
            raise ValueError("re-arm reconciliation requires typed authority and observations")
        self._scope(expected.fence.key)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            prior = load_resolve_workflow_row(db, expected.fence.key)
            if prior is None or prior.revision != expected or not isinstance(prior.intent, PendingRearm):
                raise WorkflowAuthorityError("re-arm reconciliation requires exact pending authority")
            previous = self._rearm_observation(db, prior)
            audit = self._accepted_rearm(db, expected.fence.key, prior.intent.request_id)
            if audit is None or not isinstance(audit.preflight, (ResolveRearmPreflight, ResolveRearmAcceptance)):
                raise WorkflowAuthorityError("pending re-arm lacks its accepted decision facts")
            verified, rejected, uncertainty = self._rearm_command_facts(db, prior)
            stored_workspace = load_workspace_ack(db, prior.intent.handoff)
            if workspace_verified is not None and (
                not isinstance(workspace_verified, EpisodeWorkspacePrepared)
                or workspace_verified != stored_workspace
            ):
                raise WorkflowAuthorityError("re-arm reconciliation lacks exact verified workspace evidence")
            now = self._control._now()
            working = prior
            if workspace_verified is not None:
                working = update_rearm_observation(prior, None, now=now)
                if working != prior:
                    self._persist_snapshot(db, working)
            evidence_id = "rearm-observation:" + hashlib.sha256(encode_workflow((
                prior.intent.request_id, issue, linkage, external_holds,
            )).encode()).hexdigest()
            proposal = decide_rearm_reconciliation(
                working, audit.preflight, issue, linkage,
                verified_commands=verified, rejected_commands=rejected,
                uncertainty=uncertainty, workspace=workspace_verified,
                evidence_id=evidence_id, now=now, external_holds=external_holds,
            )
            if isinstance(proposal, WorkflowRejected):
                return WorkflowConflict(working.revision, proposal.reason)
            if isinstance(proposal, WorkflowChanged):
                payload = self._rearm_evidence_payload(proposal, issue, linkage, external_holds)
                scope = workflow_scope(expected.fence.key)
                existing = db.execute(
                    "SELECT payload_json FROM workflow_evidence WHERE evidence_id=?", (evidence_id,),
                ).fetchone()
                encoded = encode_workflow(payload)
                if existing is not None and existing["payload_json"] != encoded:
                    raise WorkflowAuthorityError("re-arm evidence identity contains different facts")
                if existing is None:
                    db.execute(
                        "INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                        (evidence_id, *scope, encoded),
                    )
                return self._commit_workflow(
                    db, working.revision, (working.authority,), proposal,
                )
            assert isinstance(proposal, WorkflowUnchanged)
            scheduled = schedule_rearm_observation(previous, now=now)
            retained = previous if previous.last_error and workspace_verified is None else scheduled
            changed = db.execute(
                "UPDATE dedupe_keys SET claimed_at=?,payload_json=? "
                "WHERE namespace='workflow-rearm-observation' AND scope=? AND key=?",
                (scheduled.history.next_read_at.isoformat(), encode_workflow(retained),
                 self.writer.repository, prior.intent.request_id),
            ).rowcount
            if changed != 1:
                raise WorkflowAuthorityError("re-arm observation schedule changed during reconciliation")
            return WorkflowCommitted(working)

    def cancel_rearm(
        self, command: CancelRearm, actor: OperatorActor, *, binding: WorkflowBinding | None = None,
    ) -> WorkflowCommitted | WorkflowConflict:
        if not isinstance(command, CancelRearm) or not isinstance(actor, OperatorActor):
            raise ValueError("re-arm cancellation requires typed command and actor")
        self._scope(command.expected.fence.key)
        with self._rearm_barriers():
            with self._control.transaction() as db:
                self._control._assert_owner_active(db, self.writer.owner_id)
                prior = load_resolve_workflow_row(db, command.expected.fence.key)
                if prior is None:
                    raise WorkflowAuthorityError("re-arm cancellation requires workflow authority")
                if binding is not None and (not isinstance(binding, WorkflowBinding) or binding.fence != command.expected.fence):
                    raise WorkflowAuthorityError("cancellation binding disagrees with its expected revision")
                receipt = db.execute(
                    "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-cancellation' "
                    "AND scope=? AND key=?",
                    (self.writer.repository, command.cancellation_id),
                ).fetchone()
                if receipt is not None:
                    try:
                        accepted = json.loads(receipt["payload_json"])
                        accepted_audit = accepted.get("audit") if isinstance(accepted, dict) else None
                        if not isinstance(accepted_audit, str):
                            raise ValueError("cancellation receipt lacks its accepted audit")
                        audit = decode_workflow(accepted_audit, AuthorizedOperatorCommand)
                    except (ValueError, TypeError) as exc:
                        raise WorkflowAuthorityError("invalid accepted cancellation audit") from exc
                    if audit.command == command and audit.actor.subject == actor.subject:
                        intent = self._lookup_rearm(db, command.expected.fence.key, command.request_id)
                        if binding is not None and (intent is None or (binding.authority, binding.episode_id) != (intent.handoff.destination.authority, intent.handoff.destination.episode_id)):
                            return WorkflowConflict(prior.revision, "cancellation replay names another family epoch or episode")
                        return WorkflowCommitted(prior, idempotent=True)
                    return WorkflowConflict(
                        prior.revision,
                        "durable workflow identity was reused with different content",
                    )
                if prior.revision != command.expected or not isinstance(prior.intent, PendingRearm):
                    return WorkflowConflict(prior.revision, "re-arm cancellation requires exact pending authority")
                if binding is not None and prior.binding != binding:
                    return WorkflowConflict(prior.revision, "cancellation family epoch or episode changed")
                verified, _, uncertainty = self._rearm_command_facts(db, prior)
                proposal = cancel_resolve_rearm(
                    prior, command, actor, verified_commands=verified, uncertainty=uncertainty,
                )
                if isinstance(proposal, WorkflowRejected):
                    return WorkflowConflict(prior.revision, proposal.reason)
                if not isinstance(proposal, WorkflowChanged):
                    return WorkflowCommitted(prior, idempotent=True)
                return self._commit_workflow(
                    db, prior.revision, (prior.authority,), proposal,
                )

    def _retry_facts(self, db: sqlite3.Connection, command: AuthorizeAbsentRetry) -> RetryAuthorizationFacts:
        facts = load_retry_authorization_facts(self._control, db, command.command_id, command.evidence_id)
        row = db.execute("SELECT executor_identity FROM lifecycle_commands WHERE command_id=?", (str(command.command_id),)).fetchone()
        if facts.command_binding.fence.key != command.expected.fence.key or not owns_command(db, self.writer, row["executor_identity"]):
            raise WorkflowAuthorityError("retry authorization names a command outside this fix owner's workflow")
        return facts

    def _retry_authorizable(self, db: sqlite3.Connection, snapshot: WorkflowSnapshot) -> frozenset[str]:
        available: set[str] = set()
        for gate in snapshot.gates:
            if not isinstance(gate, OpenGate) or not isinstance(gate.cause, RetryAuthorizationRequired):
                continue
            try:
                facts = load_retry_authorization_facts(self._control, db, gate.cause.command_id, gate.cause.absence_evidence_id)
                row = db.execute("SELECT executor_identity FROM lifecycle_commands WHERE command_id=?", (str(gate.cause.command_id),)).fetchone()
                owned = facts.command_binding.fence.key == snapshot.revision.fence.key and owns_command(db, self.writer, row["executor_identity"])
            except (WorkflowAuthorityError, ControlStoreError, ValueError, TypeError):
                continue  # Unreadable authority is never offered as an action.
            if owned and retry_authorization_available(snapshot, gate, facts):
                available.add(gate.gate_id)
        return frozenset(available)

    @staticmethod
    def _retry_pending(db: sqlite3.Connection, snapshot: WorkflowSnapshot) -> tuple[PendingRetry, ...]:
        gates = {(g.gate if isinstance(g, RearmingGate) else g).gate_id for g in snapshot.gates}
        pending = []
        for row in db.execute(
            "SELECT g.authorization_id, g.gate_id, g.command_id FROM workflow_retry_authorizations g "
            "JOIN workflow_gates w USING(gate_id) WHERE g.disposition IS NULL "
            "AND w.repository=? AND w.target_kind=? AND w.target_number=? ORDER BY g.gate_id",
            workflow_scope(snapshot.revision.fence.key),
        ):
            if row["gate_id"] in gates:
                pending.append(PendingRetry(row["gate_id"], row["authorization_id"], row["command_id"]))
        return tuple(pending)

    def _persist_retry_authorization(
        self, db: sqlite3.Connection, transition: WorkflowChanged, facts: RetryAuthorizationFacts,
    ) -> None:
        persist_retry_authorization(db, self._control, transition, facts)

    def authorize_retry(
        self, command: AuthorizeAbsentRetry, actor: OperatorActor, *, binding: WorkflowBinding | None = None,
    ) -> WorkflowCommitted | WorkflowConflict | WorkflowRejected:
        """Persist one operator grant under every resolve deployment barrier."""
        if not isinstance(command, AuthorizeAbsentRetry) or not isinstance(actor, OperatorActor):
            raise ValueError("retry authorization requires typed command and actor")
        if binding is not None and (not isinstance(binding, WorkflowBinding) or binding.fence != command.expected.fence):
            raise WorkflowAuthorityError("retry binding disagrees with its expected revision")
        self._scope(command.expected.fence.key)
        with self._rearm_barriers():
            with self._control.transaction() as db:
                self._control._assert_owner_active(db, self.writer.owner_id)
                prior = load_resolve_workflow_row(db, command.expected.fence.key)
                if prior is None:
                    raise WorkflowAuthorityError("retry authorization requires workflow authority")
                stored = db.execute(
                    "SELECT binding_json, actor_json FROM workflow_retry_authorizations WHERE authorization_id=?",
                    (command.authorization_id,),
                ).fetchone()
                if stored is not None:
                    try:
                        accepted = decode_workflow(stored["actor_json"], AuthorizedOperatorCommand)
                        granted = decode_workflow(stored["binding_json"], WorkflowBinding)
                    except (ValueError, TypeError) as exc:
                        raise WorkflowAuthorityError("invalid accepted retry authorization audit") from exc
                    if accepted.command == command and accepted.actor.subject == actor.subject and (
                        binding is None or binding == granted
                    ):
                        return WorkflowCommitted(prior, idempotent=True)
                    return WorkflowConflict(prior.revision, "authorization identity was reused with different content or operator")
                if prior.revision != command.expected:
                    return WorkflowConflict(prior.revision, "workflow changed before retry authorization")
                if binding is not None and prior.binding != binding:
                    return WorkflowConflict(prior.revision, "retry authorization family epoch or episode changed")
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
                return self._commit_workflow(db, prior.revision, (prior.authority,), proposal)

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
        prior = load_resolve_workflow_row(db, returned.attempt.binding.fence.key)
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
                if not prior.gates or any(opened_gate(g).gate_id != return_gate_id(returned) for g in prior.gates):
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
            remaining = replace(prior, gates=tuple(g for g in prior.gates if opened_gate(g).gate_id != return_gate_id(returned)))
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
            after = replace(after, gates=tuple(g for g in after.gates if opened_gate(g).gate_id != return_gate_id(returned)),
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
        state = load_resolve_workflow_row(db, binding.fence.key)
        if state is None or state.binding != binding or state.gates:
            raise WorkflowAuthorityError("re-author reset requires current unheld authority")
        continuation = state.episode.continuation
        if isinstance(continuation, AuthoredResolve) and continuation.author_admission.input_fingerprint == input_fingerprint:
            raise WorkflowAuthorityError("applicable author inputs do not require replacement")
        workspace_predecessor = require_workspace_predecessor(db, state)
        if (workspace_predecessor is not None
                and workspace_predecessor.attempt.binding.episode_id != binding.episode_id):
            return None
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
        state = load_resolve_workflow_row(db, intent.binding.fence.key)
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
    def _rearm_barriers(self):
        """Workspace I/O requires all resolve-adjacent deployments quiescent."""
        with ExitStack() as barriers:
            for role in ("fix", "review", "steward"):
                barriers.enter_context(deployment_effect_barrier(
                    self._control.path, self.writer.repository, role,
                ))
            yield

    def _assert_target_quiescent(self, db: sqlite3.Connection, key: WorkflowKey) -> None:
        selected = load_issue_aggregate(db, key)
        if selected is not None and isinstance(selected.steward, cc.ActiveSteward) and (
                selected.steward.state.triage.pending_delivery or selected.steward.state.groom.pending_delivery):
            raise WorkflowAuthorityError("target has an owed steward delivery")
        if isinstance(selected, WorkflowSnapshot) and isinstance(selected.issue_recovery, cc.ActiveIssueRecovery):
            self._control._require_issue_reply_projection(db, key, selected.issue_recovery.state.consumed_reply_at)
        lower, upper = workflow_run_scope_bounds(key)
        active = db.execute(
            "SELECT 1 FROM runs r LEFT JOIN leases l USING(run_id) "
            "LEFT JOIN budget_reservations b USING(run_id) "
            "WHERE r.workflow_episode_key>=? AND r.workflow_episode_key<? "
            "AND (r.state IN ('reserved','starting','running') OR l.run_id IS NOT NULL "
            "OR b.state='active') LIMIT 1",
            (lower, upper),
        ).fetchone()
        if active is not None:
            raise WorkflowAuthorityError(
                "workspace authority requires drained runs, leases and reservations"
            )
        pending_return = db.execute(
            "SELECT 1 FROM runs r INDEXED BY runs_workflow_scope CROSS JOIN dedupe_keys captured "
            "ON captured.namespace='workflow-return' AND captured.scope=? "
            "AND captured.key=r.run_id LEFT JOIN dedupe_keys finalized "
            "ON finalized.namespace='workflow-return-disposition' "
            "AND finalized.scope=? AND finalized.key=r.run_id "
            "WHERE r.workflow_episode_key>=? AND r.workflow_episode_key<? "
            "AND finalized.key IS NULL LIMIT 1",
            (self.writer.repository, self.writer.repository, lower, upper),
        ).fetchone()
        if pending_return is not None:
            raise WorkflowAuthorityError(
                "workspace authority requires every captured paid return finalized"
            )
        # Match TEXT affinity on both sides so SQLite probes the partial index;
        # an uncast JSON expression makes this join scan retained publications.
        publication = db.execute(
            "SELECT 1 FROM runs r INDEXED BY runs_workflow_scope "
            "CROSS JOIN lifecycle_commands c INDEXED BY lifecycle_publication_runs "
            "ON r.run_id=CAST(json_extract(c.payload_json,'$.run_id') AS TEXT) "
            "WHERE c.repository=? AND c.kind IN ('publish_branch','open_pull_request','update_pull_request_body') "
            "AND c.state NOT IN ('succeeded','permanent_failure') "
            "AND r.workflow_episode_key>=? AND r.workflow_episode_key<? LIMIT 1",
            (self.writer.repository, lower, upper),
        ).fetchone()
        if publication is not None:
            raise WorkflowAuthorityError(
                "workspace authority requires every publication effect resolved"
            )

    def _workspace_predecessor(
        self, db: sqlite3.Connection, state: WorkflowSnapshot,
        binding: WorkflowBinding, branch: str,
    ) -> NoPriorResolveAdmission | PriorResolveAdmission:
        latest = latest_work_admission(db, binding)
        if latest is None:
            if target_has_work_admissions(db, binding.fence.key):
                raise WorkflowAuthorityError(
                    "workspace predecessor pointer is missing from retained admissions"
                )
            return NoPriorResolveAdmission(binding)
        if latest.revision.version > state.revision.version:
            raise WorkflowAuthorityError(
                "workspace predecessor belongs to a future workflow revision"
            )
        row = db.execute(
            "SELECT dispatch_id FROM runs WHERE run_id=?",
            (str(latest.attempt.run_id),),
        ).fetchone()
        try:
            metadata = json.loads(row["dispatch_id"]) if row is not None else None
        except (ValueError, TypeError):
            metadata = None
        if not isinstance(metadata, dict) or metadata.get("publication_branch") != branch:
            raise WorkflowAuthorityError(
                "workspace branch lacks exact admitted publication provenance"
            )
        return PriorResolveAdmission(latest.attempt, latest.revision.version)

    def _assert_workspace_predecessor(
        self, db: sqlite3.Connection, state: WorkflowSnapshot,
        handoff: EpisodeWorkspaceHandoff,
    ) -> None:
        if self._workspace_predecessor(db, state, handoff.source, handoff.branch) != handoff.predecessor:
            raise WorkflowAuthorityError(
                "workspace handoff predecessor changed after observation"
            )

    def _pending_handoff(
        self, db: sqlite3.Connection, expected: WorkflowRevision, handoff_id: str,
    ) -> tuple[WorkflowSnapshot, EpisodeWorkspaceHandoff]:
        self._scope(expected.fence.key)
        validate_workflow_identifier(handoff_id, "workspace handoff")
        self._control._assert_owner_active(db, self.writer.owner_id)
        state = load_resolve_workflow_row(db, expected.fence.key)
        if state is None or state.revision != expected or not isinstance(state.intent, PendingRearm):
            raise WorkflowAuthorityError(
                "workspace preparation requires the exact current pending re-arm"
            )
        handoff = state.intent.handoff
        if handoff.handoff_id != handoff_id:
            raise WorkflowAuthorityError("workspace preparation names another handoff")
        self._assert_target_quiescent(db, expected.fence.key)
        self._assert_workspace_predecessor(db, state, handoff)
        return state, handoff

    @contextmanager
    def rearm_authority(self, expected: WorkflowRevision, *, binding: WorkflowBinding | None = None):
        """Fence remote observation; callers perform no SQLite work while yielded."""
        if not isinstance(expected, WorkflowRevision):
            raise ValueError("re-arm authority requires a typed revision")
        if binding is not None and (not isinstance(binding, WorkflowBinding) or binding.fence != expected.fence):
            raise ValueError("re-arm binding disagrees with its expected revision")
        self._scope(expected.fence.key)
        with self._rearm_barriers():
            with self._control.transaction() as db:
                self._control._assert_owner_active(db, self.writer.owner_id)
                state = load_resolve_workflow_row(db, expected.fence.key)
                if state is None:
                    raise WorkflowAuthorityError(
                        "re-arm authority requires the exact current workflow revision"
                    )
                if state.revision != expected:
                    raise WorkflowRevisionConflict(state.revision, "re-arm workflow revision changed")
                if binding is not None and state.binding != binding:
                    raise WorkflowRevisionConflict(state.revision, "re-arm family epoch or episode changed")
                self._assert_target_quiescent(db, expected.fence.key)
            yield state

    def observe_workspace(
        self, binding: WorkflowBinding, base: ObservedResolveBase, branch: str,
    ) -> ResolveWorkspaceObserved:
        """Mint observed workspace evidence while the caller holds rearm_authority."""
        if not isinstance(binding, WorkflowBinding) or not isinstance(base, ObservedResolveBase):
            raise ValueError("workspace observation requires typed binding and base")
        self._scope(binding.fence.key)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            state = load_resolve_workflow_row(db, binding.fence.key)
            if state is None or state.binding != binding:
                raise WorkflowAuthorityError(
                    "workspace observation requires the exact current workflow binding"
                )
            self._assert_target_quiescent(db, binding.fence.key)
            predecessor = self._workspace_predecessor(db, state, binding, branch)
            return ResolveWorkspaceObserved(binding, predecessor, branch, base)

    @contextmanager
    def workspace_handoff_authority(
        self, handoff_id: str, expected: WorkflowRevision,
    ):
        """Fence preparation and yield outside SQL; callers then perform local Git I/O."""
        if not isinstance(expected, WorkflowRevision):
            raise ValueError("workspace preparation requires a typed revision")
        with self._rearm_barriers():
            with self._control.transaction() as db:
                _, handoff = self._pending_handoff(db, expected, handoff_id)
                acknowledgement = load_workspace_ack(db, handoff)
            yield acknowledgement

    def complete_workspace_handoff(
        self, completed: EpisodeWorkspacePrepared,
    ) -> EpisodeWorkspacePrepared:
        """Persist verified preparation while workspace_handoff_authority remains held."""
        if not isinstance(completed, EpisodeWorkspacePrepared):
            raise ValueError("workspace completion requires verified preparation evidence")
        handoff = completed.handoff
        self._scope(handoff.destination.fence.key)
        with self._control.transaction() as db:
            self._control._assert_owner_active(db, self.writer.owner_id)
            state = load_resolve_workflow_row(db, handoff.destination.fence.key)
            if state is None or not isinstance(state.intent, PendingRearm) or state.intent.handoff != handoff:
                raise WorkflowAuthorityError(
                    "workspace completion requires its exact current pending re-arm"
                )
            self._assert_target_quiescent(db, handoff.destination.fence.key)
            self._assert_workspace_predecessor(db, state, handoff)
            prior = load_workspace_ack(db, handoff)
            if prior is not None:
                if prior.handoff != completed.handoff or prior.result != completed.result:
                    raise WorkflowAuthorityError(
                        "workspace completion disagrees with retained evidence"
                    )
                return prior
            if completed.performed_under != state.binding:
                raise WorkflowAuthorityError(
                    "workspace completion was not verified under current authority"
                )
            if not 0 <= (self._control._now() - completed.observed_at).total_seconds() <= 300:
                raise WorkflowAuthorityError("workspace completion evidence is not fresh")
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (?,?,?,?,?)",
                (WORKSPACE_ACK_NAMESPACE, self.writer.repository, handoff.handoff_id,
                 completed.observed_at.isoformat(), encode_workflow(completed)),
            )
            self._control._transition(
                db, "workflow_workspace", handoff.handoff_id, "verified",
                {"archive_ref": completed.result.archive_ref,
                 "generation": completed.performed_under.fence.generation},
            )
            return completed

    def prepared_workspace(
        self, expected: WorkflowRevision,
    ) -> EpisodeWorkspacePrepared | None:
        """Read exact preparation after caller-owned Git verification under rearm_authority."""
        if not isinstance(expected, WorkflowRevision):
            raise ValueError("prepared workspace lookup requires a typed revision")
        self._scope(expected.fence.key)
        with self._control.transaction() as db:
            state = load_resolve_workflow_row(db, expected.fence.key)
            if state is None or state.revision != expected or not isinstance(state.intent, PendingRearm):
                raise WorkflowAuthorityError(
                    "prepared workspace lookup requires exact pending authority"
                )
            self._control._assert_owner_active(db, self.writer.owner_id)
            self._assert_target_quiescent(db, expected.fence.key)
            self._assert_workspace_predecessor(db, state, state.intent.handoff)
            return load_workspace_ack(db, state.intent.handoff)

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
            prior = load_resolve_workflow_row(db, key)
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
        with self._control.transaction() as db:
            return self._commit_workflow(db, expected, authorities, transition)

    def _commit_workflow(
        self, db: sqlite3.Connection, expected: WorkflowRevision, authorities: tuple[FamilyAuthority, ...],
        transition: WorkflowChanged,
    ) -> WorkflowCommitted | WorkflowConflict:
        if not isinstance(expected, WorkflowRevision) or not isinstance(transition, WorkflowChanged):
            raise ValueError("commit requires a typed revision and proposed transition")
        self._scope(expected.fence.key)
        if transition.expected != expected:
            raise WorkflowAuthorityError("commit and transition expect different workflow revisions")
        if not isinstance(authorities, tuple) or authorities != (transition.snapshot.authority,):
            raise WorkflowAuthorityError("resolve commit requires exactly its captured family authority")
        encoded = transition_receipt(transition)
        self._control._assert_owner_active(db, self.writer.owner_id)
        prior = load_resolve_workflow_row(db, expected.fence.key)
        if prior is None:
            raise WorkflowAuthorityError("workflow must be explicitly imported before mutation")
        if authorities != (prior.authority,):
            raise WorkflowAuthorityError("workflow family activation epoch changed")
        if isinstance(transition.audit, ResolveAttemptResult) and db.execute(
            "SELECT 1 FROM dedupe_keys WHERE namespace='workflow-return' AND scope=? AND key=?",
            (self.writer.repository, str(transition.audit.attempt.run_id)),
        ).fetchone() is not None:
            return WorkflowConflict(prior.revision, "captured paid return must use observation finalization")
        replayed = replay_or_conflict(db, self.writer.repository, prior, transition, encoded)
        if replayed is not None:
            return replayed
        if transition.snapshot.binding != prior.binding:
            if isinstance(prior.steward, cc.ActiveSteward) and (
                    prior.steward.state.triage.pending_delivery or prior.steward.state.groom.pending_delivery):
                raise WorkflowAuthorityError("generation change has an owed steward delivery")
            if isinstance(prior.issue_recovery, cc.ActiveIssueRecovery):
                self._control._require_issue_reply_projection(db, prior.binding.fence.key, prior.issue_recovery.state.consumed_reply_at)
        observations = self._validate_evidence(db, prior, transition)
        admission = self._validate_attempt_run(db, transition.audit) if isinstance(transition.audit, ResolveAttemptResult) else None
        accepted_preflight = None
        if isinstance(prior.intent, PendingRearm):
            row = db.execute("SELECT actor_json FROM workflow_rearms WHERE request_id=?", (prior.intent.request_id,)).fetchone()
            if row is None:
                raise WorkflowAuthorityError("pending intent lacks its accepted audit")
            accepted_preflight = decode_workflow(row["actor_json"], AuthorizedOperatorCommand).preflight
        retry_facts = self._retry_facts(db, transition.audit.command) if isinstance(transition.audit, AuthorizedOperatorCommand) and isinstance(transition.audit.command, AuthorizeAbsentRetry) else None
        validate_resolve_transition(prior, transition, ceiling=self._resolve_ceiling, now=self._control._now(), observations=observations, accepted_preflight=accepted_preflight, admission=admission, retry_facts=retry_facts)
        if transition.snapshot.binding != prior.binding:
            # A new generation/episode starts the pre-admission refusal count
            # over (#262). Deleted whatever the row holds, so a corrupt one
            # (which holds the issue) is recoverable by the operator's re-arm.
            db.execute(
                "DELETE FROM level_state WHERE namespace=? AND scope=? AND key=?",
                (self._control._REFUSAL_NAMESPACE, self.writer.repository, str(expected.fence.key.target.number)),
            )
        self._persist(db, prior, transition, encoded)
        if retry_facts is not None:
            self._persist_retry_authorization(db, transition, retry_facts)
        return record_commit(db, self._control, self.writer.repository, transition, encoded)

    def _validate_evidence(
        self, db: sqlite3.Connection, prior: WorkflowSnapshot, proposal: WorkflowChanged,
    ) -> tuple[ResolveRearmPreflight | ObservedIssue | LinkedPrPresent, ...]:
        audit = proposal.audit
        scope = workflow_scope(prior.revision.fence.key)
        if isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.command, (RequestRearm, ContinueGrantedRearm)):
            self._assert_target_quiescent(db, prior.revision.fence.key)
            if isinstance(audit.command, RequestRearm):
                if not isinstance(audit.preflight, ResolveRearmAcceptance):
                    raise WorkflowAuthorityError(
                        "re-arm request lacks its accepted workspace observation"
                    )
                observed = audit.preflight.workspace
                if observed.source != prior.binding:
                    raise WorkflowAuthorityError(
                        "workspace observation no longer names the current source binding"
                    )
                if self._workspace_predecessor(db, prior, observed.source, observed.branch) != observed.predecessor:
                    raise WorkflowAuthorityError(
                        "workspace observation predecessor changed before acceptance"
                    )
            else:
                if not isinstance(prior.intent, (CancelledRearm, SupersededRearm)):
                    raise WorkflowAuthorityError(
                        "continued re-arm lacks its stopped workspace handoff"
                    )
                self._assert_workspace_predecessor(db, prior, prior.intent.handoff)
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
        if isinstance(intent, CompletedRearm):
            acknowledgement = load_workspace_ack(db, intent.handoff)
            if acknowledgement is None or acknowledgement != intent.workspace:
                raise WorkflowAuthorityError(
                    "re-arm completion lacks its exact persisted workspace acknowledgement"
                )
        if isinstance(intent, (CompletedRearm, CancelledRearm, SupersededRearm)):
            if isinstance(prior.intent, PendingRearm):
                verified, _, _ = self._rearm_command_facts(db, prior)
                if set(intent.verified_commands) != set(verified):
                    raise WorkflowAuthorityError("re-arm disposition must retain every successful required receipt")
            for command_id in intent.verified_commands:
                row = db.execute(
                    "SELECT lifecycle_commands.state, lifecycle_commands.executor_identity, lifecycle_commands.attempt_owner, "
                    "lifecycle_commands.workflow_binding_json, effect_receipts.status, effect_receipts.executor_identity AS receipt_owner FROM lifecycle_commands "
                    "JOIN effect_receipts USING(command_id) WHERE command_id=?", (str(command_id),),
                ).fetchone()
                if row is None or row["state"] != EffectStatus.SUCCEEDED.value or row["status"] != EffectStatus.SUCCEEDED.value or not owns_command(db, self.writer, row["executor_identity"]) or not row["attempt_owner"] or row["receipt_owner"] != row["attempt_owner"] or decode_workflow(row["workflow_binding_json"], WorkflowBinding) != prior.binding:
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
                if row is not None and owns_command(db, self.writer, row["executor_identity"]):
                    rejected = True
                elif command_id in self._rearm_command_facts(db, prior)[1]:
                    rejected = True  # A grant judged the obligation inapplicable.
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

    def _persist_snapshot(self, db: sqlite3.Connection, after: WorkflowSnapshot) -> None:
        persist_workflow_snapshot(db, after)

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
                now = self._control._now()
                insert_pending_rearm(db, scope, intent, proposal.audit)
                seed = RearmObservation(
                    intent.request_id, after.binding, ActiveReconciliation(now, now, 0),
                )
                observation = schedule_rearm_observation(seed, now=now)
                db.execute(
                    "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                    "VALUES ('workflow-rearm-observation',?,?,?,?)",
                    (self.writer.repository, intent.request_id,
                     observation.history.next_read_at.isoformat(), encode_workflow(observation)),
                )
            else:
                update_rearm_state(db, intent)
        for command in proposal.commands:
            self._control._enqueue_command(db, command)
            assert isinstance(intent, PendingRearm)
            db.execute(
                "INSERT INTO workflow_rearm_commands VALUES (?, ?, ?, 'required', ?)",
                (intent.request_id, "ensure_lifecycle_projection", str(command.command_id), encode_workflow(proposal.audit)),
            )
        if isinstance(intent, (CancelledRearm, SupersededRearm)) and intent != prior.intent:
            retirement_id = record_stop_evidence(db, scope, proposal)
            for command_id in intent.retired_obligations:
                retire_rearm_obligation(db, intent, str(command_id))
                self._retire_command(db, after, str(command_id), retirement_id)

    def _retire_command(
        self, db: sqlite3.Connection, snapshot: WorkflowSnapshot,
        command_id: str, retirement_id: str,
    ) -> None:
        row = db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            raise WorkflowAuthorityError("retired obligation has no durable command")
        if not owns_command(db, self.writer, row["executor_identity"]) or decode_workflow(row["workflow_binding_json"], WorkflowBinding) != snapshot.binding:
            raise WorkflowAuthorityError("retired obligation belongs to a different workflow owner")
        self._control._retire_workflow_command(
            db, row, _command_from_row(row), snapshot, retirement_id,
        )
