"""Agent runtime decorator enforcing reserve-before-run accounting."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import contextvars
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
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
from ..domain.refs import is_governed_branch
from .control_store import (
    AccountingDisposition,
    BudgetLimit,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)

_UNLIMITED_ACCOUNTING_MICROS = 9_000_000_000_000_000_000
_FIX_PUBLICATION_KINDS = frozenset(
    {
        "RESOLVE",
        "RESUME",
        "PR_RESUME",
        "ADDRESS_REVIEW",
        "FIX_CI",
        "REBASE",
        "AUTHOR",
        "IMPLEMENT",
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
        try:
            maximum = self.budget_policy.maximum_for(
                self.role, kind or ""
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
        publication = _fix_publication_context(self.role, task.cwd)
        if (
            self.role == "fix"
            and kind in _FIX_PUBLICATION_KINDS
            and publication is None
        ):
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
        dispatch_id = json.dumps(
            {
                "repository": self.repository,
                "role": self.role,
                "kind": kind or "unknown",
                "number": number,
                "issue": issue,
                "breaker_key": _breaker_key(kind, number, issue),
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
            self.repository,
            tuple(additional),
            kind or "paid",
            self.owner_id or f"{self.repository}:{self.role}",
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
        if self.require_dispatch_claim and not self.store.bind_dispatch_claim(
            self.repository,
            self.role,
            _breaker_key(kind or "", number, issue),
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
            "ISSUE_AGENT_CONTROL_DB": self.store.path,
            "ISSUE_AGENT_REPOSITORY": self.repository,
            "ISSUE_AGENT_OWNER_ID": request.owner_id,
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
                        "ISSUE_AGENT_DURABLE_PUBLISH": "1",
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
        # Settlement/normalization failures retain the reservation. Releasing
        # here would turn an unavailable accounting authority into new budget
        # headroom even though the paid runtime already completed.
        return self._settle_result(
            run_id, task.model or result.model, result
        )

    def _settle_result(
        self, run_id: RunId, model: str | None, result: AgentResult
    ) -> AgentResult:
        usage = _token_usage(result.usage, self.runtime_name)
        provider_amount = Money.from_major("USD", result.cost_usd)
        codex_source = (
            str(result.usage.get("source") or "")
            if isinstance(result.usage, dict)
            else ""
        )
        untrusted_codex_usage = (
            self.runtime_name == "codex"
            and isinstance(result.usage, dict)
            and codex_source
            not in {
                UsageSource.PROVIDER.value,
                UsageSource.ROLLOUT.value,
                UsageSource.RECONCILED.value,
            }
        )
        if untrusted_codex_usage:
            # Codex stderr is agent-controlled output, not a provider receipt.
            # Preserve it in telemetry, but do not let either its counters or
            # a legacy cost derived from them release budget authority.
            usage = None
            provider_amount = Money.zero("USD")
        used_stream_fallback = False
        if (
            (usage is None or usage.total_tokens == 0)
            and provider_amount.micros <= 0
        ):
            # A dropped final provider envelope is not evidence of a free run.
            # The stream accumulator can over-count repeated cache counters, but
            # it is observed adapter telemetry and therefore a conservative
            # fail-closed fallback while a monetary cap is armed.
            stream_usage = _token_usage(
                result.stream_usage, self.runtime_name
            )
            if stream_usage is not None and stream_usage.total_tokens > 0:
                usage = stream_usage
                used_stream_fallback = True
        if (
            usage is None
            and provider_amount.micros <= 0
            and self._caps_armed()
        ):
            if self.runtime_name == "codex" and self.spend_evidence_dir:
                from .spend_recovery import CodexSpendReconciler

                CodexSpendReconciler(
                    self.store, (self.spend_evidence_dir,)
                ).reconcile(run_id)
            # Retain the full reservation when an armed rollout has neither a
            # provider charge nor trusted usage. Recovery can reconcile Codex
            # evidence; every other runtime stays visibly fail-closed instead
            # of silently admitting unlimited zero-dollar work.
            self.store.record_reservation_retained(
                run_id, "trusted usage and provider charge unavailable"
            )
            return result
        if (
            usage is not None
            and usage.total_tokens > 0
            and provider_amount.micros <= 0
            and self._caps_armed()
            and (self.rate_table is None or not model)
        ):
            # Observed work with no usable pricing authority cannot safely
            # become a zero-dollar charge. Keep the maximum reservation.
            self.store.record_reservation_retained(
                run_id, "pricing authority unavailable"
            )
            return result
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
            if used_stream_fallback and self.runtime_name == "codex":
                basis = PricingBasis.CONFIGURED_RATE_CARD
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
                (
                    f"{self.runtime_name}:"
                    f"{result.session_id or 'no-session'}:{run_id}"
                ),
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
    if runtime == "codex":
        try:
            source = UsageSource(str(raw.get("source") or ""))
        except ValueError:
            source = UsageSource.STDERR
    else:
        source = UsageSource.PROVIDER
    return TokenUsage(
        disjoint_input,
        int(raw.get("output_tokens", 0) or 0),
        cache_read,
        cache_write,
        source,
    )
