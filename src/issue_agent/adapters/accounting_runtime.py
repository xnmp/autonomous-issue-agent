"""ADR 0002's reserve-before-run accounting boundary."""

from __future__ import annotations

import uuid
import contextvars
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .runtime_plan import AgentRuntime, RuntimePlan
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
    classify_leg,
    resumable_session,
)
from ..domain.ports import StaleReservationReconciler
from ..domain.pricing import BudgetPolicy, RateTable, price
from ..domain.run_usage import MeasuredRunUsage, EstimatedRunUsage, price_run_usage, total_usage
from ..domain.workflow_identity import WorkflowBinding
from .workflow_codec import WorkflowEncodingError, encode_workflow
from ..domain.agent_task import RuntimeName, run_input_fingerprint
from ..domain.review_execution import ReviewChildIdentity, review_child_budget_leg
from ..domain.review_return import ReturnedReviewChild, review_returned_envelope
from .control_store import (
    FIX_LEG_NAMESPACE,
    AccountingDisposition,
    BudgetLimit,
    ReservationRequest,
    ReservationResult,
    SQLiteControlStore,
    SettlementRequest,
)

_UNLIMITED_ACCOUNTING_MICROS = 9_000_000_000_000_000_000
_SUMMARY_LIMIT = 20000


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
    owner_id: str = ""
    require_dispatch_claim: bool = False
    reservation_reconciler: StaleReservationReconciler | None = None
    _context: contextvars.ContextVar[
        tuple[int | None, str, int, int | None, WorkflowBinding | None, ReviewChildIdentity | None, str]
    ] = field(
        default_factory=lambda: contextvars.ContextVar(
            f"accounting_context_{uuid.uuid4().hex}",
            default=(None, "", 0, None, None, None, ""),
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
        head: str = "",
    ) -> None:
        if not isinstance(head, str):
            raise ValueError("accounting context head must be text")
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
        self._context.set((issue, kind, number, claim_target, workflow, review_child, head))

    def _reserve_reclaiming_dead_headroom(
        self,
        request: ReservationRequest,
    ) -> ReservationResult:
        """Recover terminal reservations before conceding a cap denial.

        Boot and admission share settlement authority. ADR 0012 allows one
        sweep/retry for exhaustion; it cannot bypass live leases or repair
        unavailable/corrupt SQLite authority and identity conflicts.
        """

        reservation = self.store.reserve(request)
        if (
            reservation.disposition is not AccountingDisposition.EXHAUSTED
            or self.reservation_reconciler is None
        ):
            return reservation
        try:
            self.reservation_reconciler.reconcile_stale_reservations()
        except Exception:  # noqa: BLE001 - escalate exactly as before
            return reservation
        return self.store.reserve(request)


    def set_run_identity(self, run_id: str, leg_id: str) -> None:
        if not run_id or not leg_id:
            raise ValueError("accounting run and leg identities are required")
        self._identity.set((run_id, leg_id))

    def _refused(self, detail: str) -> AgentResult:
        self.record_refused_leg()
        return _return_refused(detail)

    def record_refused_leg(self) -> None:
        """Record a fix refusal before admission as an ordinary zero-cost run.

        A refusal buys nothing and mints no paid run, so without this row the
        fix backoff never saw it and the item re-dispatched every poll with a
        claim relabel each time (#262, tauri PR #1019). Cancelled, released and
        receipt-less, the row reads as a machine end and nothing else counts it.
        """
        issue, kind, number, *_, head = self._context.get()
        if self.role != "fix" or issue is None or not kind:
            return
        run_id = RunId(uuid.uuid4().hex)
        dispatch_id = json.dumps({"repository": self.repository, "role": self.role, "kind": kind, "issue": issue,
                                  "number": number, "head": head},
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
        issue, kind, number, claim_target, workflow, review_child, head = self._context.get()
        if self.role == "fix" and (workflow is not None or review_child is not None):
            return self._refused("a fix leg is a plain leg: no workflow binding")
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
            return self._blocked("codex pricing is unavailable")
        issue_less_review = self.role == "review" and number > 0
        if (
            self.issue_cap is not None
            and issue is None
            and not issue_less_review
        ):
            return self._blocked("issue spend scope is unavailable")
        limits = list(self._limits(now, issue))
        primary, *additional = limits
        budget_kind = review_child_budget_leg(review_child, kind or "") if review_child is not None else kind or ""
        try:
            maximum = self.budget_policy.maximum_for(
                self.role, budget_kind
            )
        except KeyError as exc:
            return self._blocked(str(exc))
        declared = Money.from_major("USD", task.max_budget_usd)
        if declared != maximum:
            return self._blocked(
                f"task budget {declared.micros} disagrees with policy "
                f"{maximum.micros} for {self.role}/{kind}"
            )
        correlated_run_id, _leg_id = self._identity.get()
        run_id = RunId(correlated_run_id or uuid.uuid4().hex)
        dispatch_id = json.dumps(
            {
                "repository": self.repository,
                "role": self.role,
                "kind": kind or "unknown",
                "leg_id": _leg_id or str(run_id),
                "runtime": self.runtime_name,
                "effort": task.effort.value if task.effort is not None else None,
                "input_fingerprint": run_input_fingerprint(task.intent),
                "number": number,
                "issue": issue,
                # The PR head the leg was decided on: attempt counting keys
                # REVIEW legs to it (refactor decision Q21).
                "head": head,
                "breaker_key": retry_key,
                "claim_target": target,
                "cwd": (
                    str(Path(task.cwd).resolve())
                    if task.cwd is not None
                    else ""
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
            # A fix refusal buys nothing; its zero-cost row spaces the retry.
            self.record_refused_leg()
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
        }
        runtime_task = replace(task, env={**task.env, **authority})
        try:
            result = await self.inner.run(runtime_task)
        except BaseException:
            self._recover_or_release(run_id)
            raise
        if self.role == "fix":
            # The leg's class, recorded before settlement: a fix run that ends
            # without this receipt (died, reclaimed, refused) is a machine end.
            self.store.receipt(FIX_LEG_NAMESPACE, self.repository, str(run_id), {
                "leg": classify_leg(result, resumed=bool(task.resume)).value,
                "session": resumable_session(result),
                "runtime": self.runtime_name,
                # The PR body the harness opens a RESOLVE's branch with (Q11).
                "summary": (result.summary or "")[:_SUMMARY_LIMIT],
            })
        result = replace(
            result,
            workflow_attempt=(
                WorkflowAttemptIdentity(workflow, run_id, _leg_id or str(run_id))
                if workflow is not None else None
            ),
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
            returned_review=review_return,
        )

    def _blocked(self, detail: str) -> AgentResult:
        self.record_refused_leg()
        return _blocked(detail)

    def _settle_result(
        self, run_id: RunId, model: str | None, result: AgentResult, *,
        returned_review: ReturnedReviewChild | None = None,
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
        if returned_review is not None:
            settlement = self.store.settle_review_return(request, returned_review)
        else:
            settlement = self.store.settle(request)
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
                    issue_budget_scope(self.repository, issue, self.issue_cap.currency),
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


def _daily_scope(
    repository: str, dimension: str, currency: str, now: datetime
) -> BudgetScope:
    return daily_budget_scope(
        repository, dimension, currency, now, ZoneInfo("UTC")
    )


def issue_budget_scope(repository: str, issue: int, currency: str) -> BudgetScope:
    """An issue's lifetime spend scope (the per-issue cap's dimension)."""
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
    # attempt. It buys nothing; `record_refused_leg` spaces its retry.
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
