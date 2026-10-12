"""ADR 0002's reserve-before-run accounting boundary."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import contextvars
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import InvalidOperation
from zoneinfo import ZoneInfo

from .runtime_plan import AgentRuntime, RuntimePlan
from ..domain import continuation_components as cc
from ..domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
    RunId,
    TokenUsage,
    UsageSource,
    WorkflowAttemptIdentity,
    daily_budget_scope,
)
from ..domain.outcome import (
    AgentResult,
    ExitReason,
    DAILY_HEADROOM_STATUS,
    MAX_BUDGET_STATUS,
    RETURN_REFUSED_STATUS,
    budget_exhausted,
    infra_class_failure,
    address_review_actions,
    resumable_session,
)
from ..domain.ports import StaleReservationReconciler
from ..domain.pricing import BudgetPolicy, RateTable, price
from ..domain.run_usage import MeasuredRunUsage, EstimatedRunUsage, price_run_usage, total_usage
from ..domain.refs import is_governed_branch
from ..domain.workflow_identity import IssueRef, WorkflowBinding, WorkflowFamily, WorkflowKey, WorkflowWriter
from ..domain.workflow import ResolveSession, ResolveStage
from ..domain.resolve_return import ReturnedResolve
from .workflow_control import SQLiteWorkflowControl
from .workflow_codec import WorkflowEncodingError, encode_workflow
from ..domain.agent_task import RuntimeName, run_input_fingerprint
from ..domain.review_execution import ReviewChildIdentity, review_child_budget_leg
from ..domain.review_return import ReturnedReviewChild, review_returned_envelope
from .control_store import (
    AccountingDisposition,
    BudgetLimit,
    ReservationRequest,
    ReservationResult,
    SQLiteControlStore,
    SettlementRequest,
)

_UNLIMITED_ACCOUNTING_MICROS = 9_000_000_000_000_000_000


def _return_text(value: object) -> str:
    if not isinstance(value, str):
        return "" if value is None else f"[invalid provider text: {type(value).__name__}]"
    normalized = value[:4001].replace("\x00", "\\0")
    # Bound encoded bytes, not code points: astral text expands twelvefold in
    # the ASCII durable codec and must not break settlement after paid work.
    if len(json.dumps(normalized, ensure_ascii=True)) <= 4000:
        return normalized
    low, high = 0, min(len(normalized), 3980)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(normalized[:middle], ensure_ascii=True)) <= 3980:
            low = middle
        else:
            high = middle - 1
    return normalized[:low] + "\n[truncated]"


def _normalize_resolve_result(result: AgentResult) -> AgentResult:
    malformed = type(result.is_error) is not bool or any(
        value is not None and not isinstance(value, str)
        for value in (result.status, result.summary, result.needs, result.envelope_subtype, result.session_id)
    ) or (result.exit_reason is not None and not isinstance(result.exit_reason, ExitReason))
    session = result.session_id if isinstance(result.session_id, str) else ""
    if session and (len(session) > 512 or not session.strip() or any(ord(char) < 32 for char in session)):
        session = ""
        malformed = True
    return replace(
        result, is_error=malformed or result.is_error is not False,
        status=_return_text(result.status)[:512],
        summary=_return_text(result.summary), needs=_return_text(result.needs),
        envelope_subtype=_return_text(result.envelope_subtype)[:512],
        session_id=session,
        exit_reason=result.exit_reason if isinstance(result.exit_reason, ExitReason) else None,
    )


_FIX_PUBLICATION_KINDS = frozenset(
    {
        "RESOLVE",
        "RESUME",
        "PR_RESUME",
        "ADDRESS_REVIEW",
        "FIX_CI",
        "REBASE",
    }
)


@dataclass(frozen=True)
class AccountingRuntime(AgentRuntime):
    inner: AgentRuntime
    store: SQLiteControlStore
    repository: str
    role: str
    repository_cap: Money | None
    global_cap: Money | None
    issue_cap: Money | None
    config_version: str
    runtime_name: str
    budget_policy: BudgetPolicy
    rate_table: RateTable | None = None
    spend_evidence_dir: str = ""
    event_log_path: str = ""
    event_deployment: str = ""
    event_role: str = ""
    event_line_limit: int = 0
    owner_id: str = ""
    github_host: str = "github.com"
    approved_base_branch: str = "dev"
    require_dispatch_claim: bool = False
    reservation_reconciler: StaleReservationReconciler | None = None
    admit: Callable[[ReservationRequest], ReservationResult] | None = None
    _context: contextvars.ContextVar[
        tuple[int | None, str, int, int | None, WorkflowBinding | None, ReviewChildIdentity | None]
    ] = field(
        default_factory=lambda: contextvars.ContextVar(
            f"accounting_context_{uuid.uuid4().hex}",
            default=(None, "", 0, None, None, None),
        ),
        init=False,
        repr=False,
        compare=False,
    )
    _identity: contextvars.ContextVar[tuple[str, str]] = field(
        default_factory=lambda: contextvars.ContextVar(
            f"accounting_identity_{uuid.uuid4().hex}", default=("", "")
        ),
        init=False,
        repr=False,
        compare=False,
    )

    def set_context(
        self,
        role: str,
        kind: str,
        number: int,
        issue: int | None,
        claim_target: int | None = None,
        *,
        workflow: WorkflowBinding | None = None,
        review_child: ReviewChildIdentity | None = None,
    ) -> None:
        if workflow is not None and not isinstance(workflow, WorkflowBinding):
            raise ValueError("accounting context requires a validated workflow binding")
        if review_child is not None and (
            not isinstance(review_child, ReviewChildIdentity)
            or role != "review"
            or self.role != "review"
            or kind not in {"REVIEW", "RE_REVIEW"}
            or workflow != review_child.round.binding.rejection
        ):
            raise ValueError("accounting review child requires its bound review context")
        if review_child is not None:
            RuntimeName(self.runtime_name)
        self._context.set((issue, kind, number, claim_target, workflow, review_child))

    @property
    def workflow_binding(self) -> WorkflowBinding | None:
        return self._context.get()[4]

    def _reserve_reclaiming_dead_headroom(
        self,
        request: ReservationRequest,
    ) -> ReservationResult:
        """Recover terminal reservations before conceding a cap denial.

        Boot and admission share settlement authority. ADR 0012 allows one
        sweep/retry for exhaustion; it cannot bypass live leases or repair
        unavailable/corrupt SQLite authority and identity conflicts.
        """

        admit = self.admit or self.store.reserve
        reservation = admit(request)
        if (
            reservation.disposition is not AccountingDisposition.EXHAUSTED
            or self.reservation_reconciler is None
        ):
            return reservation
        try:
            released = (
                self.reservation_reconciler.reconcile_stale_reservations()
            )
        except Exception as exc:  # noqa: BLE001 - escalate exactly as before
            self._journal_reconciled(
                request,
                reservation,
                0,
                False,
                f"reconciliation unavailable: {exc}",
            )
            return reservation
        retried = admit(request)
        self._journal_reconciled(
            request,
            reservation,
            released,
            retried.disposition is AccountingDisposition.AVAILABLE,
            reservation.detail,
        )
        return retried

    def _journal_reconciled(
        self,
        request: ReservationRequest,
        exhausted: ReservationResult,
        released: int,
        admitted: bool,
        reason: str,
    ) -> None:
        # The journalled scope names the limit that tripped, not the primary
        # one: a leg carrying repository, global, and issue caps can be denied
        # by any of them, and an operator reads this to decide which cap moves.
        try:
            self.store.record_admission_reconciled(
                request.run_id,
                exhausted.tripped_dimension or request.scope.dimension,
                released,
                admitted,
                reason,
            )
        except Exception:  # noqa: BLE001 - telemetry never fails a dispatch
            pass

    def set_run_identity(self, run_id: str, leg_id: str) -> None:
        if not run_id or not leg_id:
            raise ValueError("accounting run and leg identities are required")
        self._identity.set((run_id, leg_id))

    def _refused(self, detail: str) -> AgentResult:
        self._record_refused_leg()
        return _return_refused(detail)

    def _record_refused_leg(self) -> None:
        """Record a fix refusal before admission as an ordinary zero-cost run.

        A refusal buys nothing and mints no paid run, so without this row the
        fix backoff never saw it and the item re-dispatched every poll with a
        claim relabel each time (#262, tauri PR #1019). Cancelled, released and
        receipt-less, the row reads as a machine end and nothing else counts it.
        """
        issue, kind, *_ = self._context.get()
        if self.role != "fix" or issue is None or not kind:
            return
        run_id = RunId(uuid.uuid4().hex)
        dispatch_id = json.dumps({"repository": self.repository, "role": self.role, "kind": kind, "issue": issue},
                                 sort_keys=True, separators=(",", ":"))
        try:
            self.store.start_run(run_id, dispatch_id, kind, self.owner_id or f"{self.repository}:{self.role}",
                                 self.config_version)
            self.store.complete_run(run_id, state="cancelled", outcome="admission_refused")
        except Exception:  # noqa: BLE001 - the refusal itself must still return
            pass

    async def run(self, task: RuntimePlan) -> AgentResult:
        if task.runtime is not None and task.runtime.value != self.runtime_name:
            return self._refused("selected provider differs from accounting runtime")
        now = datetime.now(timezone.utc)
        issue, kind, number, claim_target, workflow, review_child = self._context.get()
        if review_child is not None and task.resolve_inputs is not None:
            return self._refused("review child cannot carry resolve inputs")
        if task.resolve_inputs is not None:
            if workflow is None or self.role != "fix" or kind not in ("RESOLVE", "RESUME"):
                return self._refused("returned-result context does not match the admitted workflow")
            # Bound persisted input before paying. Provider prose is normalized
            # separately below, leaving room for its receipt and provenance.
            try:
                encoded_inputs = encode_workflow(task.resolve_inputs)
            except WorkflowEncodingError:
                return self._refused("resolve completion inputs exceed durable receipt capacity")
            if len(encoded_inputs.encode()) > 100000:
                return self._refused("resolve completion inputs exceed durable receipt capacity")
        # ADR 0008: paid admission binds the decision-side target unchanged;
        # review convergence remains PR-owned even when spend is issue-scoped.
        target = (
            claim_target
            if claim_target is not None
            else _breaker_key(kind, number, issue)
        )
        retry_key = -number if self.role == "review" else target
        if (
            self.runtime_name == "codex"
            and self.rate_table is None
            and self._caps_armed()
        ):
            return _blocked("codex pricing is unavailable")
        issue_less_review = self.role == "review" and number > 0
        if (
            self.issue_cap is not None
            and issue is None
            and not issue_less_review
        ):
            return _blocked("issue spend scope is unavailable")
        limits = list(self._limits(now, issue))
        primary, *additional = limits
        budget_kind = review_child_budget_leg(review_child, kind or "") if review_child is not None else kind or ""
        try:
            maximum = self.budget_policy.maximum_for(
                self.role, budget_kind
            )
        except KeyError as exc:
            return _blocked(str(exc))
        declared = Money.from_major("USD", task.max_budget_usd)
        if declared != maximum:
            return _blocked(
                f"task budget {declared.micros} disagrees with policy "
                f"{maximum.micros} for {self.role}/{kind}"
            )
        correlated_run_id, _leg_id = self._identity.get()
        run_id = RunId(correlated_run_id or uuid.uuid4().hex)
        continuations = ()
        if task.issue_reply is not None:
            selected = task.issue_reply
            if isinstance(selected, cc.LinkedPRIssueReplyInput):
                if (self.role != "fix" or kind != "PR_RESUME" or number != selected.pr.number
                        or issue != selected.selection.binding.fence.key.target.number
                        or task.pr_repair_input is None or workflow != task.pr_repair_input.selection.binding
                        or workflow.fence.key.target != selected.pr):
                    return self._refused("linked PR issue reply requires its exact issue/PR context")
            elif self.role != "fix" or kind != "RESUME" or workflow is None or workflow.fence != selected.selection.revision.fence:
                return self._refused("selected issue reply requires its captured resolve context")
            capture = cc.ContinuationCapture(selected.selection.binding, selected.selection.revision, _leg_id or str(run_id))
            continuations = ((cc.IssueReplyPurchase(capture, selected.reply_at) if isinstance(selected, cc.LinkedPRIssueReplyInput)
                              else cc.IssueRecoveryPurchase(capture, selected.reply_at, selected.branch, 2**63 - 1)),)
        if task.pr_repair_input is not None:
            selected = task.pr_repair_input
            linked_reply = isinstance(task.issue_reply, cc.LinkedPRIssueReplyInput)
            if (self.role != "fix" or kind not in {"FIX_CI", "ADDRESS_REVIEW", "REBASE", "PR_RESUME"} or workflow != selected.selection.binding
                    or number != selected.selection.binding.fence.key.target.number or issue is None
                    or selected.runtime.value != self.runtime_name or continuations and not linked_reply
                    or kind == "ADDRESS_REVIEW" and selected.review is None
                    or kind != "ADDRESS_REVIEW" and selected.review is not None
                    or kind == "REBASE" and selected.rebase is None
                    or kind != "REBASE" and selected.rebase is not None
                    or kind == "PR_RESUME" and (selected.reply is None and not linked_reply
                                                or selected.reply is not None and linked_reply)
                    or kind != "PR_RESUME" and selected.reply is not None
                    or kind != "FIX_CI" and selected.ci is not None):
                return self._refused("selected repair input requires its exact PR, runtime and family context")
            session = selected.session
            if (task.resume or "") != (session.handle if session is not None else ""):
                return self._refused("repair launch handle differs from selected continuity")
            capture = cc.ContinuationCapture(selected.selection.binding, selected.selection.revision, _leg_id or str(run_id))
            continuations = (cc.PRRepairPurchase(capture, reply_at=selected.reply.reply_at if selected.reply is not None else "",
                                                 allowance=(cc.RepairAllowance.INFRA if kind == "PR_RESUME" else cc.RepairAllowance.REVIEW if selected.review is not None else cc.RepairAllowance.REBASE if selected.rebase is not None else cc.RepairAllowance.CI), ceiling=2**63-1,
                                                 branch=selected.branch, handoff=selected.handoff, advance_resume=selected.review is not None or kind == "PR_RESUME", head=selected.head),) + continuations
            if selected.ci is not None:
                ci = selected.ci
                if issue != ci.selection.binding.fence.key.target.number:
                    return self._refused("CI allowance belongs to another linked issue")
                capture = cc.ContinuationCapture(ci.selection.binding, ci.selection.revision, _leg_id or str(run_id))
                continuations += (cc.IssueCIPurchase(capture, ci.pr, ci.head, ci.ceiling),)
            if selected.review is not None:
                review = selected.review
                if issue != review.selection.binding.fence.key.target.number:
                    return self._refused("review allowance belongs to another linked issue")
                capture = cc.ContinuationCapture(review.selection.binding, review.selection.revision, _leg_id or str(run_id))
                continuations += (cc.IssueReviewPurchase(capture, review.pr, review.head, review.review_at, review.ceiling),)
            if selected.rebase is not None:
                rebase = selected.rebase
                if issue != rebase.selection.binding.fence.key.target.number:
                    return self._refused("rebase allowance belongs to another linked issue")
                capture = cc.ContinuationCapture(rebase.selection.binding, rebase.selection.revision, _leg_id or str(run_id))
                continuations += (cc.IssueRebasePurchase(capture, rebase.pr, rebase.head, rebase.branch, rebase.ceiling),)
        if (self.role == "fix" and issue is not None
                and kind in {"ROUTE", "RESOLVE", "RESUME", "PR_RESUME", "FIX_CI", "ADDRESS_REVIEW", "REBASE"}
                and not any(p.capture.binding.authority.family is WorkflowFamily.ISSUE_RECOVERY for p in continuations)):
            selected_infra = self.store.read_issue_recovery(WorkflowKey(self.repository, IssueRef(issue)))
            if not isinstance(selected_infra, cc.SelectedIssueRecovery):
                return self._refused("fix infrastructure episode requires migrated issue recovery authority")
            continuations += (cc.IssueInfraPurchase(cc.ContinuationCapture(
                selected_infra.binding, selected_infra.revision, _leg_id or str(run_id))),)
        publication = _fix_publication_context(self.role, task.cwd)
        if (
            self.role == "fix"
            and kind in _FIX_PUBLICATION_KINDS
            and publication is None
        ):
            self._record_refused_leg()
            return AgentResult(
                is_error=True,
                status="publication_authority_unavailable",
                envelope_subtype="publication_authority_unavailable",
                summary=(
                    "durable publication authority is unavailable for the "
                    "fix worktree"
                ),
                exit_reason=ExitReason.AGENT_ERROR,
            )
        if task.pr_repair_input is not None and publication.branch != task.pr_repair_input.branch:
            repair = task.pr_repair_input
            raise cc.PublicationIdentityMismatch(cc.PublicationIdentityEscalationCapture(
                repair.selection.binding, repair.head, repair.branch, issue, publication.branch,
                f"Harness refused paid repair: checkout branch `{publication.branch}` differs from current PR branch `{repair.branch}` at `{repair.head}`. Correct the worktree/publication identity before resuming.",
                "ai-blocked" if kind == "PR_RESUME" else "ai-pr-open",
            ))
        dispatch_id = json.dumps(
            {
                "repository": self.repository,
                "role": self.role,
                "kind": kind or "unknown",
                "leg_id": _leg_id or str(run_id),
                "runtime": self.runtime_name,
                "effort": task.effort.value if task.effort is not None else None,
                "input_fingerprint": run_input_fingerprint(task.intent),
                "resolve_inputs": encode_workflow(task.resolve_inputs) if task.resolve_inputs is not None else None,
                **({"issue_reply": encode_workflow((task.issue_reply.reply_at, task.issue_reply.body,
                    task.issue_reply.branch if isinstance(task.issue_reply, cc.IssueReplyInput) else task.issue_reply.pr.number))}
                   if task.issue_reply is not None else {}),
                "number": number,
                "issue": issue,
                **({"ci_head": task.pr_repair_input.ci.head} if task.pr_repair_input is not None and task.pr_repair_input.ci is not None else {}),
                **({"review_input": encode_workflow((task.pr_repair_input.review.head, task.pr_repair_input.review.review_at))}
                   if task.pr_repair_input is not None and task.pr_repair_input.review is not None else {}),
                **({"rebase_input": encode_workflow((task.pr_repair_input.rebase.head, task.pr_repair_input.rebase.branch))}
                   if task.pr_repair_input is not None and task.pr_repair_input.rebase is not None else {}),
                **({"pr_reply": encode_workflow((task.pr_repair_input.reply.reply_at, task.pr_repair_input.reply.body))}
                   if task.pr_repair_input is not None and task.pr_repair_input.reply is not None else {}),
                "breaker_key": retry_key,
                "claim_target": target,
                "cwd": (
                    str(Path(task.cwd).resolve())
                    if task.cwd is not None
                    else ""
                ),
                "publication_branch": (
                    publication.branch if publication is not None else ""
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        reserved_model = task.model or "<runtime-default>"
        request = ReservationRequest(
            ReservationId(uuid.uuid4().hex),
            run_id,
            dispatch_id,
            primary.scope,
            PriceQuote(
                reserved_model,
                "task-budget",
                self.config_version,
                now,
                maximum,
            ),
            primary.cap,
            self.config_version,
            self.repository,
            tuple(additional),
            kind or "paid",
            self.owner_id or f"{self.repository}:{self.role}",
            workflow=workflow,
            review_child=review_child,
            continuations=continuations,
        )
        # Paid child ownership is recorded by the runtime's ProcessSupervisor.
        # The daemon itself must not be registered as the reservation lease:
        # doing so makes a transport failure look live until daemon restart.
        reservation = self._reserve_reclaiming_dead_headroom(request)
        if review_child is not None and reservation.disposition is AccountingDisposition.EXHAUSTED:
            try:
                refusal = self.store.record_review_admission_refusal(request)
            except Exception as exc:  # noqa: BLE001 - uncertain authority cannot become a budget verdict
                return self._refused(f"review child refusal authority unavailable: {exc}")
            if refusal is None:
                return self._refused("review child budget or authority changed before final refusal")
            reservation = ReservationResult(
                AccountingDisposition.EXHAUSTED, None,
                Money(refusal.currency, refusal.committed_micros),
                Money(refusal.currency, refusal.requested_micros),
                Money(refusal.currency, refusal.cap_micros),
                f"final planned-child refusal receipt {refusal.receipt_id}",
                refusal.limit_dimension,
            )
        if reservation.disposition is not AccountingDisposition.AVAILABLE:
            if workflow is not None and reservation.disposition is not AccountingDisposition.EXHAUSTED:
                return self._refused(reservation.detail or reservation.disposition.value)
            # Time restores daily headroom, so that refusal waits label-free
            # (DAILY_HEADROOM_STATUS); a lifetime issue cap needs a decision.
            daily = (reservation.disposition is AccountingDisposition.EXHAUSTED
                     and (reservation.tripped_dimension or primary.scope.dimension) in {"repository", "global"})
            status = DAILY_HEADROOM_STATUS if daily else MAX_BUDGET_STATUS
            return AgentResult(
                is_error=True,
                status=status,
                envelope_subtype=status,
                summary=(
                    "paid admission blocked: "
                    f"{reservation.disposition.value} ({reservation.detail})"
                ),
                exit_reason=ExitReason.BUDGET_EXHAUSTED,
            )
        if self.require_dispatch_claim and not self.store.bind_dispatch_claim(
            self.repository,
            self.role,
            target,
            request.owner_id,
            run_id,
        ):
            released = self.store.release(
                run_id,
                "dispatch_claim_binding_unavailable",
            )
            return AgentResult(
                is_error=True,
                status="dispatch_claim_unavailable",
                envelope_subtype="dispatch_claim_unavailable",
                summary=(
                    "paid admission cancelled: its global target/path claim "
                    f"could not be bound ({released.disposition.value})"
                ),
                exit_reason=ExitReason.AGENT_ERROR,
            )
        publication_owner = (
            self.owner_id or f"{self.repository}:{self.role}"
        )
        authority = {
            "ISSUE_AGENT_RUN_ID": str(run_id),
            "ISSUE_AGENT_CONFIG_VERSION": self.config_version,
            **({
                "ISSUE_AGENT_RATE_CARD_ID": self.rate_table.rate_card_id,
                "ISSUE_AGENT_RATE_CARD_VERSION": self.rate_table.version,
            } if self.rate_table is not None else {}),
            "ISSUE_AGENT_CONTROL_DB": self.store.path,
            "ISSUE_AGENT_REPOSITORY": self.repository,
            "ISSUE_AGENT_OWNER_ID": request.owner_id,
            **({"ISSUE_AGENT_REVIEW_CHILD": "1"} if review_child is not None else {}),
            **(
                {
                    "ISSUE_AGENT_EVENT_LOG": self.event_log_path,
                    "ISSUE_AGENT_EVENT_DEPLOYMENT": self.event_deployment,
                    "ISSUE_AGENT_EVENT_ROLE": self.event_role,
                    "ISSUE_AGENT_EVENT_LINE_LIMIT": str(
                        self.event_line_limit
                    ),
                }
                if self.event_log_path
                else {}
            ),
        }
        publication_authority: dict[str, str] = {}
        if publication is not None:
            publication_authority = {
                    "ISSUE_AGENT_PUBLICATION_OWNER": publication_owner,
                    "ISSUE_AGENT_GITHUB_HOST": self.github_host,
                    "ISSUE_AGENT_BASE_BRANCH": self.approved_base_branch,
                    "ISSUE_AGENT_PUBLICATION_CWD": publication.cwd,
                    "ISSUE_AGENT_PUBLICATION_BRANCH": publication.branch,
                    "ISSUE_AGENT_PUBLICATION_ISSUE": str(issue or 0),
                    "ISSUE_AGENT_PUBLICATION_KIND": kind,
                    "ISSUE_AGENT_GIT_BIN": publication.git_binary,
                    "ISSUE_AGENT_GH_BIN": publication.gh_binary,
                    "ISSUE_AGENT_EXEC_PATH": publication.executable_path,
                }
        authority = {**authority, **publication_authority}
        authority_bundle = (
            base64.urlsafe_b64encode(
                json.dumps(
                    authority,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).decode()
            if publication_authority
            else ""
        )
        runtime_task = replace(
            task,
            env={
                **task.env,
                **authority,
                **(
                    {
                        "ISSUE_AGENT_AUTHORITY_BUNDLE": authority_bundle,
                    }
                    if publication_authority
                    else {}
                ),
            },
        )
        try:
            result = await self.inner.run(runtime_task)
        except BaseException:
            self._recover_or_release(run_id)
            raise
        if self.role == "fix" and not infra_class_failure(result):
            # Only a normal return is recorded: every other ended fix run, with
            # no receipt, is a machine end to the backoff.
            self.store.record_fix_infra_return(run_id,
                WorkflowWriter(self.repository, "fix", self.owner_id or f"{self.repository}:fix"))
        result = replace(
            result,
            workflow_attempt=(
                WorkflowAttemptIdentity(workflow, run_id, _leg_id or str(run_id))
                if workflow is not None else None
            ),
        )
        returned = None
        if task.resolve_inputs is not None:
            result = _normalize_resolve_result(result)
            assert result.workflow_attempt is not None
            session = None
            if resumable_session(result):
                session = ResolveSession(result.workflow_attempt, result.session_id, self.runtime_name, reserved_model)
            returned = ReturnedResolve(
                result.workflow_attempt, ResolveStage(kind), task.resolve_inputs,
                result.is_error,
                result.status, result.summary, result.needs, result.envelope_subtype,
                result.exit_reason, session, datetime.now(timezone.utc),
            )
        review_return = None
        if review_child is not None:
            try:
                review_return = ReturnedReviewChild(
                    review_child, str(run_id), request.owner_id, RuntimeName(self.runtime_name),
                    review_returned_envelope(result), datetime.now(timezone.utc),
                )
                encode_workflow(review_return)
            except (ValueError, WorkflowEncodingError) as exc:
                self.store.record_reservation_retained(
                    run_id, "review returned output cannot be durably captured",
                )
                raise RuntimeError("review returned output cannot be durably captured") from exc
        return self._settle_result(
            run_id, task.model or result.model or reserved_model, result,
            returned=returned, returned_review=review_return,
            repair_input=task.pr_repair_input, attempt=result.workflow_attempt,
        )

    def _settle_result(
        self, run_id: RunId, model: str | None, result: AgentResult, *,
        returned: ReturnedResolve | None = None,
        returned_review: ReturnedReviewChild | None = None,
        repair_input: cc.PRRepairInput | None = None,
        attempt: WorkflowAttemptIdentity | None = None,
    ) -> AgentResult:
        if returned_review is not None:
            stopped = self.store.settle_review_preflight_stop(returned_review)
            if stopped is not None:
                if stopped.disposition is not AccountingDisposition.AVAILABLE:
                    raise RuntimeError("review preflight stop settlement failed closed")
                return result
        observed = result.run_usage
        gap = observed.detail if isinstance(observed, EstimatedRunUsage) else ""
        usage = None
        amount = Money.zero("USD")
        basis = PricingBasis.ESTIMATE
        card = None
        try:
            if isinstance(observed, (MeasuredRunUsage, EstimatedRunUsage)):
                usage = total_usage(observed.samples)
                card = observed.rates or self.rate_table
                amount = price_run_usage(observed, card)
                if isinstance(observed, MeasuredRunUsage) and all(s.reported_cost is not None for s in observed.samples):
                    basis = PricingBasis.PROVIDER_REPORTED
                elif isinstance(observed, MeasuredRunUsage) and card is not None and all(
                    card.priceable(s.model) for s in observed.samples
                ):
                    basis = PricingBasis.CONFIGURED_RATE_CARD
                else:
                    gap = gap or "some request models have no configured rates"
            else:
                try:
                    provider_amount = Money.from_major("USD", result.cost_usd)
                    if provider_amount.micros > 2**63 - 1:
                        raise ValueError("provider dollars exceed SQLite's money bound")
                except (ValueError, TypeError, ArithmeticError):
                    provider_amount = Money.zero()
                    gap = "provider dollars malformed; using usable token evidence"
                for raw in (result.usage, result.stream_usage):
                    try:
                        candidate = _token_usage(raw, self.runtime_name)
                    except (ValueError, TypeError, ArithmeticError):
                        candidate = None
                        gap = "some token evidence malformed; using the remaining evidence"
                    if candidate is not None and candidate.trusted_for_accounting:
                        usage = candidate
                        if usage.total_tokens:
                            break
                if self.runtime_name != "codex" and provider_amount.micros > 0:
                    amount = provider_amount
                    basis = PricingBasis.PROVIDER_REPORTED
                elif usage is not None and usage.trusted_for_accounting and self.rate_table is not None and model:
                    amount = price(usage, self.rate_table.rates_for(model), self.rate_table.currency)
                    card = self.rate_table
                    gap = "request receipts unavailable; streamed usage priced at configured rates"
                elif provider_amount.micros > 0 and not (isinstance(result.usage, dict) and result.usage.get("source") not in {"provider", "rollout", "reconciled"}):
                    amount = provider_amount
                    gap = "only the runtime's priced usage estimate is available"
                else:
                    usage = None
                    gap = "request receipts and priceable usage unavailable; interrupted requests charged zero"
        except (ValueError, TypeError, KeyError, ArithmeticError):
            gap = "request accounting evidence is malformed or unpriceable; unusable requests charged zero"
        if amount.micros > 2**63 - 1:
            amount = Money.zero("USD")
            basis = PricingBasis.ESTIMATE
            gap = "request prices exceed SQLite's money bound; unusable requests charged zero"
        request = SettlementRequest(
            run_id, amount, basis,
            f"{self.runtime_name}:{result.session_id or 'no-session'}:{run_id}",
            usage, card.rate_card_id if card else None, card.version if card else None,
            gap,
        )
        if repair_input is not None:
            assert attempt is not None
            admission = self.store.pr_repair_admission(attempt)
            runtime = RuntimeName(self.runtime_name)
            actual_model = cc.RepairModel(admission, runtime, result.model or model or "<runtime-default>")
            session = (cc.RepairSession(admission, result.session_id, runtime, actual_model.model)
                       if resumable_session(result) else None)
            state = replace(repair_input.selection.component.state, branch=repair_input.branch,
                            session=session, actual_model=actual_model)
            writer = WorkflowWriter(self.repository, self.role, self.owner_id or f"{self.repository}:{self.role}")
            ci = (cc.CIRepairResult(self.store.ci_repair_admission(attempt),
                cc.CICharge.NOT_ATTEMPT if budget_exhausted(result.exit_reason) or infra_class_failure(result) else cc.CICharge.ATTEMPT)
                if repair_input.ci is not None else None)
            review = (cc.ReviewRepairResult(self.store.review_repair_admission(attempt),
                cc.ReviewCharge.ATTEMPT if address_review_actions(repair_input.review.pr.number, result).bump_retry else cc.ReviewCharge.NOT_ATTEMPT)
                if repair_input.review is not None else None)
            rebase = (cc.RebaseRepairResult(self.store.rebase_repair_admission(attempt),
                cc.RebaseCharge.NOT_ATTEMPT if budget_exhausted(result.exit_reason) or infra_class_failure(result) else cc.RebaseCharge.ATTEMPT)
                if repair_input.rebase is not None else None)
            settlement = self.store.settle_pr_repair_result(cc.PRRepairResult(admission, state, request, ci, review, rebase), writer)
        elif returned_review is not None:
            settlement = self.store.settle_review_return(request, returned_review)
        elif returned is None:
            settlement = self.store.settle(request)
        else:
            writer = WorkflowWriter(self.repository, self.role, self.owner_id or f"{self.repository}:{self.role}")
            # Capturing evidence does not classify or consume the resolve ceiling.
            settlement = SQLiteWorkflowControl(self.store, writer, resolve_ceiling=0).settle_return(request, returned)
        if settlement.disposition is not AccountingDisposition.AVAILABLE:
            raise RuntimeError(
                f"accounting settlement failed closed: {settlement.detail}"
            )
        if self.spend_evidence_dir:
            from .codex_runner import remove_spend_evidence

            remove_spend_evidence(self.spend_evidence_dir, run_id)
        return replace(result, cost_usd=amount.micros / 1_000_000, pricing_basis=basis)

    def _caps_armed(self) -> bool:
        return any(
            cap is not None
            for cap in (
                self.repository_cap,
                self.global_cap,
                self.issue_cap,
            )
        )

    def _recover_or_release(self, run_id: RunId) -> None:
        """Give every returned failure path a terminal accounting outcome."""

        reconciled = False
        try:
            if self.runtime_name == "codex" and self.spend_evidence_dir:
                from .spend_recovery import CodexSpendReconciler

                reconciled = CodexSpendReconciler(
                    self.store, (self.spend_evidence_dir,)
                ).reconcile(run_id)
            forced_shutdown = (
                self.runtime_name == "codex"
                and self.store.has_cancellation_intent(run_id)
            )
            if not reconciled and not forced_shutdown:
                released = self.store.release_unlaunched(
                    run_id, "runtime_exception_before_launch"
                )
                if (
                    released.disposition
                    is not AccountingDisposition.AVAILABLE
                ):
                    self.store.record_reservation_retained(
                        run_id,
                        "runtime failed after paid execution may have started",
                    )
        except Exception:
            # Preserve the causal runtime/normalization/settlement exception.
            # An unavailable authority remains fail-closed and boot recovery
            # retries the still-active reservation.
            pass

    def _limits(
        self, now: datetime, issue: int | None
    ) -> tuple[BudgetLimit, ...]:
        limits: list[BudgetLimit] = []
        if self.repository_cap is not None:
            limits.append(
                BudgetLimit(
                    _daily_scope(
                        self.repository, "repository", self.repository_cap.currency, now
                    ),
                    self.repository_cap,
                )
            )
        if self.global_cap is not None:
            limits.append(
                BudgetLimit(
                    _daily_scope("*", "global", self.global_cap.currency, now),
                    self.global_cap,
                )
            )
        if self.issue_cap is not None and issue is not None:
            limits.append(
                BudgetLimit(
                    _issue_scope(
                        self.repository, issue, self.issue_cap.currency, now
                    ),
                    self.issue_cap,
                )
            )
        if not limits:
            limits.append(
                BudgetLimit(
                    _daily_scope(self.repository, "repository", "USD", now),
                    Money("USD", _UNLIMITED_ACCOUNTING_MICROS),
                )
            )
        return tuple(limits)


@dataclass(frozen=True)
class _FixPublicationContext:
    cwd: str
    branch: str
    git_binary: str
    gh_binary: str
    executable_path: str


def _fix_publication_context(
    role: str, cwd: str | None
) -> _FixPublicationContext | None:
    if role != "fix" or cwd is None:
        return None
    git_binary = shutil.which("git")
    gh_binary = shutil.which("gh")
    resolved_cwd = str(Path(cwd).resolve())
    if not git_binary or not gh_binary:
        return None
    try:
        result = subprocess.run(
            (
                str(Path(git_binary).resolve()),
                "-C",
                resolved_cwd,
                "branch",
                "--show-current",
            ),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = result.stdout.strip()
    if result.returncode != 0 or not is_governed_branch(branch):
        return None
    resolved_git = str(Path(git_binary).resolve())
    resolved_gh = str(Path(gh_binary).resolve())
    executable_path = os.pathsep.join(
        dict.fromkeys(
            (
                str(Path(resolved_git).parent),
                str(Path(resolved_gh).parent),
                "/usr/bin",
                "/bin",
            )
        )
    )
    return _FixPublicationContext(
        resolved_cwd,
        branch,
        resolved_git,
        resolved_gh,
        executable_path,
    )


def _daily_scope(
    repository: str, dimension: str, currency: str, now: datetime
) -> BudgetScope:
    return daily_budget_scope(
        repository, dimension, currency, now, ZoneInfo("UTC")
    )


def _issue_scope(
    repository: str, issue: int, currency: str, now: datetime
) -> BudgetScope:
    start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return BudgetScope(
        repository,
        f"issue:{issue}",
        currency,
        f"lifetime:{issue}",
        start,
        datetime.max.replace(tzinfo=timezone.utc),
        ZoneInfo("UTC"),
    )


def _return_refused(detail: str) -> AgentResult:
    # Receipt authority is a harness precondition, not evidence of exhausted
    # money or a failed issue implementation: a machine failure, never an
    # attempt. It buys nothing; `_record_refused_leg` spaces its retry.
    return AgentResult(
        is_error=True,
        status=RETURN_REFUSED_STATUS,
        envelope_subtype=RETURN_REFUSED_STATUS,
        summary=f"paid admission refused: {detail}",
        exit_reason=ExitReason.RUNTIME_UNAVAILABLE,
    )


def _blocked(detail: str) -> AgentResult:
    return AgentResult(
        is_error=True,
        status=MAX_BUDGET_STATUS,
        envelope_subtype=MAX_BUDGET_STATUS,
        summary=f"paid admission blocked: {detail}",
        exit_reason=ExitReason.BUDGET_EXHAUSTED,
    )


_PR_KINDS = frozenset(
    {"PR_RESUME", "ADDRESS_REVIEW", "FIX_CI", "REBASE", "REVIEW", "RE_REVIEW"}
)


def _breaker_key(kind: str, number: int, issue: int | None) -> int:
    if issue is not None:
        return issue
    return -number if kind in _PR_KINDS else number


def _token_usage(raw: object, runtime: str) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    usage_keys = {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
    }
    if not usage_keys.intersection(raw):
        return None
    counters = tuple(raw.get(key, 0) for key in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "total_tokens",
    ))
    if any(type(value) is not int or not 0 <= value <= 10**12 for value in counters):
        raise ValueError("token counters are malformed or outside the supported bound")
    input_total, output, cache_read, cache_write, total_only = counters
    if runtime == "codex" and cache_read + cache_write > input_total:
        raise ValueError("cache counters exceed inclusive input")
    if total_only > 0 and not any(
        (
            input_total,
            output,
            cache_read,
            cache_write,
        )
    ):
        return None
    # Codex reports cached input as a subset of input.  Normalize to disjoint
    # counters exactly once at this adapter boundary.
    disjoint_input = (
        max(0, input_total - cache_read - cache_write)
        if runtime == "codex"
        else input_total
    )
    if runtime == "codex":
        try:
            source = UsageSource(str(raw.get("source") or ""))
        except ValueError:
            source = UsageSource.STDERR
    else:
        source = UsageSource.PROVIDER
    return TokenUsage(
        disjoint_input,
        output,
        cache_read,
        cache_write,
        source,
    )
