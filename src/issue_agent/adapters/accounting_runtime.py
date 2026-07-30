"""Agent runtime decorator enforcing reserve-before-run accounting."""

from __future__ import annotations

import uuid
import contextvars
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
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
    daily_budget_scope,
)
from ..domain.outcome import AgentResult, ExitReason, MAX_BUDGET_STATUS
from ..domain.pricing import BudgetPolicy, RateTable, price
from .control_store import (
    AccountingDisposition,
    BudgetLimit,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)

_UNLIMITED_ACCOUNTING_MICROS = 9_000_000_000_000_000_000


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
    _context: contextvars.ContextVar[tuple[int | None, str, int]] = field(
        default_factory=lambda: contextvars.ContextVar(
            f"accounting_context_{uuid.uuid4().hex}", default=(None, "", 0)
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
        self, role: str, kind: str, number: int, issue: int | None
    ) -> None:
        self._context.set((issue, kind, number))

    def set_run_identity(self, run_id: str, leg_id: str) -> None:
        if not run_id or not leg_id:
            raise ValueError("accounting run and leg identities are required")
        self._identity.set((run_id, leg_id))

    async def run(self, task: RuntimePlan) -> AgentResult:
        now = datetime.now(timezone.utc)
        issue, kind, number = self._context.get()
        if self.runtime_name == "codex" and self.rate_table is None:
            return _blocked("codex pricing is unavailable")
        if self.issue_cap is not None and issue is None:
            return _blocked("issue spend scope is unavailable")
        limits = list(self._limits(now, issue))
        primary, *additional = limits
        try:
            maximum = self.budget_policy.maximum_for(
                self.role, kind or ""
            )
        except KeyError as exc:
            return _blocked(str(exc))
        declared = Money(
            "USD",
            max(
                0,
                int(
                    (Decimal(str(task.max_budget_usd)) * 1_000_000).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                ),
            ),
        )
        if declared != maximum:
            return _blocked(
                f"task budget {declared.micros} disagrees with policy "
                f"{maximum.micros} for {self.role}/{kind}"
            )
        correlated_run_id, _leg_id = self._identity.get()
        run_id = RunId(correlated_run_id or uuid.uuid4().hex)
        dispatch_id = json.dumps(
            {
                "role": self.role,
                "kind": kind or "unknown",
                "number": number,
                "issue": issue,
                "breaker_key": _breaker_key(kind, number, issue),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        request = ReservationRequest(
            ReservationId(uuid.uuid4().hex),
            run_id,
            dispatch_id,
            primary.scope,
            PriceQuote(
                task.model or "<runtime-default>",
                "task-budget",
                self.config_version,
                now,
                maximum,
            ),
            primary.cap,
            self.config_version,
            tuple(additional),
            kind or "paid",
            f"{self.repository}:{self.role}",
        )
        # Paid child ownership is recorded by the runtime's ProcessSupervisor.
        # The daemon itself must not be registered as the reservation lease:
        # doing so makes a transport failure look live until daemon restart.
        reservation = self.store.reserve(request)
        if reservation.disposition is not AccountingDisposition.AVAILABLE:
            return AgentResult(
                is_error=True,
                status=MAX_BUDGET_STATUS,
                envelope_subtype=MAX_BUDGET_STATUS,
                summary=(
                    "paid admission blocked: "
                    f"{reservation.disposition.value} ({reservation.detail})"
                ),
                exit_reason=ExitReason.BUDGET_EXHAUSTED,
            )
        runtime_task = replace(
            task,
            env={
                **task.env,
                "ISSUE_AGENT_RUN_ID": str(run_id),
                "ISSUE_AGENT_CONFIG_VERSION": self.config_version,
                "ISSUE_AGENT_CONTROL_DB": self.store.path,
                "ISSUE_AGENT_REPOSITORY": self.repository,
                "ISSUE_AGENT_DURABLE_PUBLISH": "1",
                "ISSUE_AGENT_PUBLICATION_OWNER": (
                    f"{self.repository}:{self.role}"
                ),
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
            },
        )
        try:
            result = await self.inner.run(runtime_task)
            return self._settle_result(
                run_id, task.model or result.model, result
            )
        except BaseException:
            self._recover_or_release(run_id)
            raise

    def _settle_result(
        self, run_id: RunId, model: str | None, result: AgentResult
    ) -> AgentResult:
        usage = _token_usage(result.usage, self.runtime_name)
        provider_amount = Money(
            "USD",
            int(
                (Decimal(str(result.cost_usd)) * 1_000_000).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            ),
        )
        priced_by_rate_card = (
            self.rate_table is not None
            and usage is not None
            and usage.trusted_for_accounting
            and not (
                self.runtime_name != "codex"
                and provider_amount.micros > 0
            )
        )
        if priced_by_rate_card:
            if not model:
                raise RuntimeError("configured-rate settlement needs a model")
            amount = price(
                usage,
                self.rate_table.rates_for(model),
                self.rate_table.currency,
            )
            basis = (
                PricingBasis.CONFIGURED_RATE_CARD
                if self.runtime_name == "codex"
                else PricingBasis.SYNTHETIC
            )
        elif (
            self.runtime_name == "codex"
            and self.rate_table is not None
            and provider_amount.micros > 0
        ):
            # Older Codex stderr exposes only ``total_tokens``. CodexRunner has
            # already conservatively priced that total at the configured higher
            # rate, but TokenUsage cannot represent an unsplit total. Preserve
            # the configured-price receipt instead of converting an observable
            # non-zero run into a zero charge.
            amount = provider_amount
            basis = PricingBasis.CONFIGURED_RATE_CARD
        else:
            amount = provider_amount
            basis = (
                PricingBasis.SYNTHETIC
                if provider_amount.micros <= 0
                else PricingBasis.PROVIDER_REPORTED
            )
        settlement = self.store.settle(
            SettlementRequest(
                run_id,
                amount,
                basis,
                result.session_id or f"run:{run_id}",
                usage,
                self.rate_table.rate_card_id
                if (
                    priced_by_rate_card
                    or basis is PricingBasis.CONFIGURED_RATE_CARD
                )
                and self.rate_table is not None
                else None,
                self.rate_table.version
                if (
                    priced_by_rate_card
                    or basis is PricingBasis.CONFIGURED_RATE_CARD
                )
                and self.rate_table is not None
                else None,
            )
        )
        if settlement.disposition is not AccountingDisposition.AVAILABLE:
            raise RuntimeError(
                f"accounting settlement failed closed: {settlement.detail}"
            )
        if self.spend_evidence_dir:
            from .codex_runner import remove_spend_evidence

            remove_spend_evidence(self.spend_evidence_dir, run_id)
        return result

    def _recover_or_release(self, run_id: RunId) -> None:
        """Give every returned failure path a terminal accounting outcome."""

        reconciled = False
        try:
            if self.runtime_name == "codex" and self.spend_evidence_dir:
                from .spend_recovery import CodexSpendReconciler

                reconciled = CodexSpendReconciler(
                    self.store, (self.spend_evidence_dir,)
                ).reconcile(run_id)
            if not reconciled:
                # The inner runtime has completed supervised teardown before this
                # path. Hard daemon death cannot enter here and still leaves the
                # active reservation for boot recovery.
                self.store.release(
                    run_id, "runtime_exception_spend_unknown"
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
    input_total = int(raw.get("input_tokens", 0) or 0)
    cache_read = int(raw.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(raw.get("cache_creation_input_tokens", 0) or 0)
    if min(input_total, cache_read, cache_write) < 0:
        return None
    total_only = int(raw.get("total_tokens", 0) or 0)
    if total_only > 0 and not any(
        (
            input_total,
            int(raw.get("output_tokens", 0) or 0),
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
    return TokenUsage(
        disjoint_input,
        int(raw.get("output_tokens", 0) or 0),
        cache_read,
        cache_write,
        UsageSource.ROLLOUT if runtime == "codex" else UsageSource.PROVIDER,
    )
