"""ADR 0002's reserve-before-run accounting boundary."""

from __future__ import annotations

from collections.abc import Mapping
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
    PricingBasis,
    RunId,
    TokenUsage,
    UsageSource,
    daily_budget_scope,
)
from ..domain.outcome import (
    AgentResult,
    ExitReason,
    DAILY_HEADROOM_STATUS,
    MAX_BUDGET_STATUS,
    RETURN_REFUSED_STATUS,
    LegClass,
    classify_leg,
    resumable_session,
)
from ..domain.policy import question_text
from ..domain.review import parse_verdict
from ..domain.ports import StaleReservationReconciler
from ..domain.pricing import BudgetPolicy, RateTable, price
from ..domain.run_usage import MeasuredRunUsage, EstimatedRunUsage, price_run_usage, total_usage
from .control_store import (
    AccountingDisposition,
    BudgetLimit,
    ReservationRequest,
    ReservationResult,
    SQLiteControlStore,
    SettlementRequest,
)

LEG_ROLES = frozenset({"fix", "review"})
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
    reservation_reconciler: StaleReservationReconciler | None = None
    _context: contextvars.ContextVar[
        tuple[int | None, str, int, int | None, str, bool]
    ] = field(
        default_factory=lambda: contextvars.ContextVar(
            f"accounting_context_{uuid.uuid4().hex}",
            default=(None, "", 0, None, "", False),
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
        head: str = "",
        resumed: bool = False,
    ) -> None:
        if not isinstance(head, str) or type(resumed) is not bool:
            raise ValueError("accounting context head must be text and resumed an exact bool")
        self._context.set((issue, kind, number, claim_target, head, resumed))

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

    def record_refused_leg(self, attempt: Mapping[str, object] | None = None) -> None:
        """Record a refusal before admission as an ordinary zero-cost run.

        A refusal buys nothing and mints no paid run, so without this row the
        backoff never saw it and the item re-dispatched every poll (#262,
        tauri PR #1019). Cancelled, released and receipt-less, the row reads
        as a machine end and nothing else counts it.

        `attempt` is for an end that retrying cannot change (GitHub refused
        the review a leg produced; the head has nothing to review): it is
        written as an agent leg's receipt, so the item's attempt cap stops it
        at `needs-human` instead of a free retry looping forever.
        """
        issue, kind, number, *_, head, _resumed = self._context.get()
        if self.role not in LEG_ROLES or issue is None or not kind:
            return
        try:
            self.store.record_unpaid_leg(
                RunId(uuid.uuid4().hex), repository=self.repository, role=self.role, kind=kind,
                owner_id=self._owner(), issue=issue, number=number, head=head, runtime=self.runtime_name,
                leg_class=LegClass.AGENT.value if attempt is not None else None, result=attempt,
            )
        except Exception:  # noqa: BLE001 - the refusal itself must still return
            pass

    async def run(self, task: RuntimePlan) -> AgentResult:
        if task.runtime is not None and task.runtime.value != self.runtime_name:
            return self._refused("selected provider differs from accounting runtime")
        now = datetime.now(timezone.utc)
        issue, kind, number, claim_target, head, resumed = self._context.get()
        # ADR 0008: paid admission binds the decision-side target unchanged.
        target = (
            claim_target
            if claim_target is not None
            else _default_claim_target(kind, number, issue)
        )
        if (
            self.runtime_name == "codex"
            and self.rate_table is None
            and self._caps_armed()
        ):
            return self._blocked("codex pricing is unavailable")
        if self.issue_cap is not None and issue is None:
            return self._blocked("issue spend scope is unavailable")
        limits = list(self._limits(now, issue))
        primary = limits[0]
        budget_kind = kind or ""
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
        reserved_model = task.model or "<runtime-default>"
        request = ReservationRequest(
            run_id,
            self.repository,
            self.role,
            kind or "paid",
            self._owner(),
            maximum,
            tuple(limits),
            lease_target=target,
            issue=issue,
            number=number,
            # The PR head the leg was decided on: attempt counting keys REVIEW
            # legs to it (refactor decision Q21).
            head=head,
            runtime=self.runtime_name,
            model=reserved_model,
        )
        # Paid child ownership is recorded by the runtime's ProcessSupervisor.
        # The daemon itself must not be registered as the leg's process:
        # doing so makes a transport failure look live until daemon restart.
        reservation = self._reserve_reclaiming_dead_headroom(request)
        if reservation.disposition is AccountingDisposition.CONFLICT:
            self.record_refused_leg()
            return AgentResult(
                is_error=True,
                status="dispatch_claim_unavailable",
                envelope_subtype="dispatch_claim_unavailable",
                summary=f"paid admission refused: {reservation.detail}",
                exit_reason=ExitReason.AGENT_ERROR,
            )
        if reservation.disposition is not AccountingDisposition.AVAILABLE:
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
        }
        runtime_task = replace(task, env={**task.env, **authority})
        try:
            result = await self.inner.run(runtime_task)
        except BaseException:
            self._recover_or_release(run_id)
            raise
        if self.role in LEG_ROLES:
            # The leg's class, recorded before settlement: a run that ends
            # without this receipt (died, reclaimed, refused) is a machine end.
            leg = classify_leg(result, resumed=resumed)
            verdict = parse_verdict(result.structured) if kind == "REVIEW" and not result.is_error else None
            self.store.record_result(run_id, leg.value, session=resumable_session(result) or "", result={
                # The PR body the harness opens a RESOLVE's branch with (Q11).
                "summary": (result.summary or "")[:_SUMMARY_LIMIT],
                # The question the QUESTION stop comment embeds: recorded with
                # the leg so no separate post can lose it.
                **({"question": question_text(result.needs)} if leg is LegClass.ASKED else {}),
                # A review's parsed verdict: the harness submits it (and can
                # resubmit it after a crash) without buying the review again.
                **({"verdict": {"event": verdict.event, "body": verdict.body}} if verdict is not None else {}),
            })
        return self._settle_result(run_id, task.model or result.model or reserved_model, result)

    def _owner(self) -> str:
        return self.owner_id or f"{self.repository}:{self.role}"

    def _blocked(self, detail: str) -> AgentResult:
        self.record_refused_leg()
        return _blocked(detail)

    def _settle_result(self, run_id: RunId, model: str | None, result: AgentResult) -> AgentResult:
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
    {"ADDRESS_REVIEW", "FIX_CI", "REBASE", "REVIEW"}
)


def _default_claim_target(kind: str, number: int, issue: int | None) -> int:
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
