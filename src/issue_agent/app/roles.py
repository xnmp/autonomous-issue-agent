"""Assemble the two role daemons from config + adapters.

Each `build_*_daemon` binds:
  * observe  → gateway.build_world (the GitHub slice the role needs),
  * decide   → the bound pure decision function (decide_fix / decide_bound_review),
  * executor → applies the leaf Actions; its `run_agent` drives the (async)
               AgentDispatcher for RunAgent actions.

The dispatcher is async (SDK query is a coroutine). Each paid fix leg owns a
bounded background worker and event loop; observation and lifecycle writes
remain on the daemon thread. Review keeps synchronous dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from ..adapters.agent_runner import AgentRunner
from ..adapters.control_store import ControlStoreError, LegacySpendMigrationError, SQLiteControlStore
from ..adapters.control_path import control_store_path
from ..adapters.effects import GitHubEffectExecutor
from ..adapters.accounting_runtime import AccountingRuntime, issue_budget_scope
from ..adapters.github import GitHubGateway
from ..adapters.journal import (
    JournalingRuntime,
    Journal,
    default_ledger_path,
)
from ..adapters.store import Store
from ..adapters.spend_recovery import CodexSpendReconciler, spend_evidence_dir
from ..adapters.worktree import ReviewCheckouts, Worktrees
from ..adapters.review_effects import ReviewReads
from ..adapters.review_execution_control import SQLiteReviewExecutionControl
from ..adapters.review_workflow_control import review_family_activated
from ..adapters.workflow_rows import WorkflowAuthorityError

from ..domain.fix import breaker_key
from ..domain.state import Harness
from ..domain.review_dispatch_record import (
    AdmissionInFlight, AdmissionOutcome, AdmissionWithheld, WithheldReason,
)
from ..domain.labels import Label
from ..domain.workflow_identity import WorkflowWriter
from ..domain.review import (
    BoundReviewState,
    FixHold,
    IdentityMode,
    ReviewConfig,
    decide_bound_review,
    fix_hold_ceiling_seconds,
    hold_review_dispatch,
)
from .planners.tasks import REVIEW_DIMENSIONS, FixTaskConfig, ReviewTaskConfig
from ..domain.actions import Relabel, RunAgent, Target
from ..adapters.runtime_plan import AgentRuntime
from ..domain.effect_retry import DEFAULT_EFFECT_RETRY_CEILING
from ..domain.agent_task import RuntimeName
from ..domain.contracts import BudgetScope, Money
from ..domain.pricing import (
    BudgetPolicy,
    ModelRates,
    RateTable,
    UnknownModelPolicy,
    usd_per_million_to_micros,
    with_default_model_rates,
    default_cache_read_rate,
    default_cache_write_rate,
)
from ..domain.recovery import RecoveryTrigger
from ..domain.events import (
    TelemetryEvent,
    TelemetryWarning,
)
from ..domain.ports import CommandRecovery, SpendReconciler, StaleReservationReconciler
from .config import (
    CodexRuntimeConfig,
    HarnessConfig,
    ReviewRuntimeConfig,
    deployment_identity_for_target,
    owner_identity_for_target,
    prior_deployment_identities,
    configured_prior_owner_ids,
)
from .dispatch import AgentDispatcher, DispatchConfig
from .execute import Executor
from .review_runtime import (
    ActiveReviewRuntime,
    ReviewDispatchPlan,
    configured_review_control,
    current_review_failures,
)
from ..adapters.act import GitHubAct
from .fix import FixFacts, FixLegs, FixSettings, decide_with, dispatch_item, lease_slots
from .loop import Daemon
from .recovery import NoWorktreeRecovery, RecoveryCoordinator, RecoveryReport


def _spend_evidence_dirs(cfg: HarnessConfig) -> tuple[str, ...]:
    return tuple(
        spend_evidence_dir(os.path.abspath(path))
        for path in dict.fromkeys(
            (cfg.state_dir, cfg.review_state_dir)
        )
        if path
    )


def _role_state_dir(cfg: HarnessConfig, role: str) -> str:
    return {
        "fix": cfg.state_dir,
        "review": cfg.review_state_dir,
    }[role]


def _deployment_identity_for_target(
    repository: str,
    target_dir: str,
    role: str,
) -> str:
    return deployment_identity_for_target(repository, target_dir, role)


def _deployment_identity_for_repository(
    cfg: HarnessConfig, role: str, repository: str
) -> str:
    return _deployment_identity_for_target(
        repository,
        cfg.target_dir,
        role,
    )


def _deployment_identity(cfg: HarnessConfig, role: str) -> str:
    return _deployment_identity_for_repository(cfg, role, cfg.repo)


def process_supervision_state_dir(
    cfg: HarnessConfig,
    role: str,
    state_dir: str | None = None,
) -> str:
    """Private process registry for one ``(repo, checkout, role)`` deployment."""

    root = state_dir if state_dir is not None else _role_state_dir(cfg, role)
    if not root:
        raise ValueError("process supervisor state directory is required")
    return os.path.join(
        root,
        "process-supervision",
        _deployment_identity(cfg, role),
    )


def prior_process_supervision_state_dirs(
    cfg: HarnessConfig,
    role: str,
    state_dir: str | None = None,
) -> tuple[str, ...]:
    """Registries this explicit repoint is authorized to reap at boot."""

    root = state_dir if state_dir is not None else _role_state_dir(cfg, role)
    if not root:
        raise ValueError("process supervisor state directory is required")
    return tuple(
        os.path.join(root, "process-supervision", deployment)
        for _repository, deployment in prior_deployment_identities(cfg, role)
    )


def _owner_identity(cfg: HarnessConfig, role: str) -> str:
    return owner_identity_for_target(cfg.repo, cfg.target_dir, role)


def _control_store(
    cfg: HarnessConfig,
    state_dir: str,
    role: str,
) -> SQLiteControlStore:
    """Open the one accounting/control authority shared by all role daemons."""

    control = SQLiteControlStore(_control_store_path(cfg, state_dir))
    control.initialize()
    prior_owners = configured_prior_owner_ids(cfg, role)
    control.adopt_legacy_owner(
        cfg.repo,
        role,
        _owner_identity(cfg, role),
        prior_owner_ids=prior_owners,
    )
    control.bind_active_owner(_owner_identity(cfg, role))
    return control


def _control_store_path(cfg: HarnessConfig, state_dir: str) -> str:
    return control_store_path(
        (cfg.state_dir, cfg.review_state_dir),
        fallback_state_dir=state_dir,
        authority_root=cfg.control_authority_root,
    )


def migrate_legacy_spend(cfg: HarnessConfig, state_dir: str) -> int:
    """Import all rotated legacy ledgers before the first paid admission."""

    control = SQLiteControlStore(_control_store_path(cfg, state_dir))
    control.initialize()
    current = Path(default_ledger_path())
    candidates = (
        tuple(sorted(current.parent.glob(f"{current.stem}*{current.suffix}")))
        if current.parent.is_dir()
        else ()
    )
    imported = 0
    for path in candidates:
        report = control.import_legacy_spend(str(path))
        if report.rejected:
            raise LegacySpendMigrationError(
                f"{path}: rejected {report.rejected} legacy spend row(s); "
                "repair or quarantine the ledger before paid work can start"
            )
        if not report.idempotent:
            imported += report.imported
    return imported


@dataclass
class _LateReservationReconciler:
    """Bind admission to recovery despite their composition order.

    Accounting wraps the runtime before the recovery coordinator exists, so the
    reconciler admission calls on a budget trip is bound afterwards. Until then
    it is inert, which degrades admission to the pre-keifu-#34 behaviour of
    escalating immediately rather than inventing headroom.
    """

    delegate: StaleReservationReconciler | None = None

    def bind(self, delegate: StaleReservationReconciler) -> None:
        self.delegate = delegate

    def reconcile_stale_reservations(self) -> int:
        if self.delegate is None:
            return 0
        return self.delegate.reconcile_stale_reservations()


@dataclass(frozen=True)
class _SweepReservationReconciler:
    """On-demand reservation recovery, identical to the periodic sweep.

    Reusing the periodic plan is deliberate: it reclaims dead leases and sweeps
    reservations without reaping processes, so an admission-time call can never
    disturb a live paid child. Release eligibility stays entirely with the
    existing recovery rules.
    """

    control: SQLiteControlStore
    owner_id: str
    sweep: Callable[[RecoveryTrigger], RecoveryReport]

    def reconcile_stale_reservations(self) -> int:
        # Counted by differencing active reservations rather than reading the
        # report's spend_unknown set: a run whose spend the sweep settled
        # exactly also returns headroom, but reports nothing unknown.
        before = frozenset(self.control.active_reservation_runs(self.owner_id))
        self.sweep(RecoveryTrigger.PERIODIC)
        after = frozenset(self.control.active_reservation_runs(self.owner_id))
        return len(before - after)


def _with_accounting(
    runtime: AgentRuntime,
    control: SQLiteControlStore,
    cfg: HarnessConfig,
    role: str,
    runtime_name: str,
    reconciler: _LateReservationReconciler | None = None,
) -> AccountingRuntime:
    money = (
        lambda value: Money.from_major("USD", value)
        if value > 0
        else None
    )
    return AccountingRuntime(
        runtime,
        control,
        cfg.repo,
        role,
        money(cfg.daily_spend_cap_usd),
        money(cfg.global_daily_spend_cap_usd),
        money(cfg.issue_spend_cap_usd),
        cfg.config_version,
        runtime_name,
        _budget_policy(cfg),
        _rate_table(cfg, runtime_name),
        spend_evidence_dir(_role_state_dir(cfg, role)),
        owner_id=_owner_identity(cfg, role),
        require_dispatch_claim=True,
        reservation_reconciler=reconciler,
    )


def _budget_policy(cfg: HarnessConfig) -> BudgetPolicy:
    def money(value: float) -> Money:
        return Money.from_major("USD", value)

    limits: dict[tuple[str, str], Money] = {}
    for kind in ("RESOLVE", "ADDRESS_REVIEW", "FIX_CI", "REBASE"):
        limits[("fix", kind)] = money(cfg.fix_budget_usd)
    for kind in ("REVIEW", "RE_REVIEW"):
        limits[("review", kind)] = money(cfg.review_budget_usd)
    limits[("review", "REVIEW_GATE")] = money(cfg.review_gate_budget_usd)
    for key, _focus in REVIEW_DIMENSIONS:
        normalized = key.upper().replace("-", "_")
        limits[("review", f"REVIEW_{normalized}")] = money(
            cfg.review_dimension_budget_usd
        )
        limits[("review", f"VALIDATE_{normalized}")] = money(
            cfg.review_validation_budget_usd
        )
    return BudgetPolicy(limits)


def _rate_table(
    cfg: HarnessConfig, runtime_name: str
) -> RateTable | None:
    try:
        fallback = ModelRates(
            usd_per_million_to_micros(cfg.codex_cost_per_mtok_in),
            usd_per_million_to_micros(cfg.codex_cost_per_mtok_out),
            usd_per_million_to_micros(cfg.codex_cost_per_mtok_in / 10),
            usd_per_million_to_micros(cfg.codex_cost_per_mtok_in),
        )
        cached = dict(cfg.codex_cost_per_mtok_cached_in)
        rates = {
            model: ModelRates(
                usd_per_million_to_micros(rate_in),
                usd_per_million_to_micros(rate_out),
                usd_per_million_to_micros(
                    cached.get(model, default_cache_read_rate(model, rate_in))
                ),
                usd_per_million_to_micros(default_cache_write_rate(model, rate_in)),
            )
            for model, rate_in, rate_out in (
                with_default_model_rates(cfg.codex_cost_per_mtok)
                if runtime_name == RuntimeName.CODEX.value
                else cfg.codex_cost_per_mtok
            )
        }
    except ValueError:
        return None
    # Shipped defaults can change without an overlay edit. Settlement receipts
    # must identify the effective prices, not just the operator's input snapshot.
    version = hashlib.sha256(json.dumps({
        "config": cfg.config_version,
        "rates": {model.strip().casefold(): asdict(value) for model, value in rates.items()},
        "fallback": asdict(fallback),
    }, sort_keys=True).encode()).hexdigest()[:16]
    return RateTable(
        (
            "codex-config"
            if runtime_name == RuntimeName.CODEX.value
            else f"{runtime_name}-synthetic-config"
        ),
        version,
        "USD",
        rates,
        UnknownModelPolicy.USE_FALLBACK,
        fallback,
    )


@dataclass(frozen=True)
class EffectRecovery:
    actions: GitHubEffectExecutor
    reconcile_workflow: Callable[[], int] | None = None

    def drain_pending_commands(self) -> int:
        recovered = self.reconcile_workflow() if self.reconcile_workflow is not None else 0
        return recovered + self.actions.drain_pending_commands()


def _effects(
    gateway: GitHubGateway,
    control: SQLiteControlStore,
    *,
    repository: str,
    identity: str,
    config_version: str,
    retry_ceiling: int = DEFAULT_EFFECT_RETRY_CEILING,
    journal: Journal | None = None,
    alert_cmd: str = "",
    log: Callable[[str], None] | None = None,
) -> EffectRecovery:
    executor = GitHubEffectExecutor(
        gateway,
        control,
        repository=repository,
        identity=identity,
        config_version=config_version,
        retry_ceiling=retry_ceiling,
        journal=journal,
        alert_cmd=alert_cmd,
        log=log,
    )
    return EffectRecovery(executor)


@dataclass(frozen=True)
class _RecoveryHooks:
    reconcile: Callable[[RecoveryTrigger], object]


def _recovery(
    state_dir: str,
    cfg: HarnessConfig,
    control: SQLiteControlStore,
    effects: CommandRecovery,
    role: str,
    worktrees: Worktrees | None = None,
    *,
    owner_id: str,
    reconciler: _LateReservationReconciler | None = None,
) -> _RecoveryHooks:
    from ..adapters.process_supervisor import ProcessSupervisor

    effective_state_dir = state_dir or os.path.dirname(control.path)
    coordinator = RecoveryCoordinator(
        ProcessSupervisor(
            process_supervision_state_dir(
                cfg,
                role,
                effective_state_dir,
            )
        ),
        control,
        worktrees or NoWorktreeRecovery(),
        effects,
        CodexSpendReconciler(control, _spend_evidence_dirs(cfg)),
        owner_id,
    )
    if reconciler is not None:
        reconciler.bind(
            _SweepReservationReconciler(control, owner_id, coordinator.reconcile)
        )

    return _RecoveryHooks(coordinator.reconcile)


def _force_kill(
    cfg: HarnessConfig,
    role: str,
    state_dir: str,
) -> Callable[[], object]:
    from ..adapters.process_supervisor import ProcessSupervisor

    supervisor = ProcessSupervisor(
        process_supervision_state_dir(cfg, role, state_dir)
    )
    return supervisor.force_kill_all


def _shutdown_finalize(
    control: SQLiteControlStore,
    effects: CommandRecovery,
    *,
    owner_id: str,
) -> Callable[[], object]:
    """Persist remaining ownership and flush effects before forced teardown."""

    def finalize() -> object:
        control.record_owned_cancellation_intents(
            owner_id, "shutdown_deadline_spend_unknown"
        )
        return effects.drain_pending_commands()

    return finalize


def _shutdown_prepare(
    control: SQLiteControlStore,
    *,
    owner_id: str,
) -> Callable[[], object]:
    def prepare() -> object:
        return control.record_owned_cancellation_intents(
            owner_id, "shutdown_deadline_spend_unknown"
        )

    return prepare


def _shutdown_terminalize(
    control: SQLiteControlStore,
    spend: SpendReconciler,
    *,
    owner_id: str,
    state_dir: str,
    role: str,
) -> Callable[[], object]:
    """Reconcile killed paid work and persist any unresolved handovers."""

    from ..adapters.handover import write_shutdown_handover

    reason = "shutdown_deadline_spend_unknown"

    def terminalize() -> object:
        intents = control.record_owned_cancellation_intents(owner_id, reason)
        for intent in intents:
            spend.reconcile(intent.run_id)
        written = 0
        for intent in control.pending_cancellation_intents(owner_id):
            receipt = write_shutdown_handover(
                state_dir=state_dir,
                role=role,
                issue=intent.issue_number,
                run_id=str(intent.run_id),
                kind=intent.kind,
                reason=intent.reason,
            )
            if receipt is None:
                continue
            control.mark_cancellation_handover_written(intent.run_id)
            written += 1
        return written

    return terminalize


class CancellableAsyncDispatch:
    """Run one dispatch coroutine per worker thread with cooperative shutdown.

    Each worker owns its event loop, while the daemon main thread retains a
    thread-safe cancellation handle. Cancelling the root task propagates into
    the async agent runner, whose subprocess cleanup terminates its process
    group before the worker exits.
    """

    def __init__(
        self,
        dispatch: Callable[[RunAgent], Awaitable[None]],
        finished: Callable[[RunAgent], None] | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._finished = finished
        self._lock = threading.Lock()
        self._active: dict[
            int,
            tuple[
                asyncio.AbstractEventLoop,
                asyncio.Task[None],
                threading.Event,
            ],
        ] = {}
        # Sticky: cancellation may win the race before a submitted pool worker
        # reaches __call__. Every later registration observes this edge too.
        self._shutdown = threading.Event()

    def _new_cancel_event(self) -> threading.Event:
        return threading.Event()

    async def _run(
        self, action: RunAgent, cancel_requested: threading.Event
    ) -> None:
        task = asyncio.create_task(self._dispatch(action))
        try:
            while not task.done():
                if cancel_requested.is_set():
                    task.cancel()
                # call_soon_threadsafe is the normal shutdown path. The bounded
                # wait is a portability fallback for event loops whose self-pipe
                # wake is unavailable during teardown.
                await asyncio.wait((task,), timeout=1.0)
            await task
        except asyncio.CancelledError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise

    def __call__(self, action: RunAgent) -> None:
        loop = asyncio.new_event_loop()
        cancel_requested = self._new_cancel_event()
        task = loop.create_task(self._run(action, cancel_requested))
        token = id(task)
        with self._lock:
            self._active[token] = (loop, task, cancel_requested)
            if self._shutdown.is_set():
                cancel_requested.set()
                task.cancel()
        try:
            loop.run_until_complete(task)
        finally:
            with self._lock:
                self._active.pop(token, None)
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            if self._finished is not None:
                self._finished(action)

    def cancel_all(self) -> None:
        with self._lock:
            self._shutdown.set()
            active = tuple(self._active.values())
        for loop, task, cancel_requested in active:
            cancel_requested.set()
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # The worker may have completed and closed its loop after the
                # snapshot. Its future is already settling; continue cancelling
                # the remaining workers.
                continue


class DispatchReservations:
    """Atomic issue leases shared across threads and deployments: one paid
    dispatch per target (Q13 deleted the path-overlap holds)."""

    def __init__(
        self,
        control: SQLiteControlStore | None = None,
        *,
        repository: str = "",
        role: str = "",
        owner_id: str = "",
    ) -> None:
        self._lock = threading.Lock()
        self._actions: dict[RunAgent, int] = {}
        self._control = control
        self._repository = repository
        self._role = role
        self._owner_id = owner_id
        if control is not None and (
            not repository or not role or not owner_id
        ):
            raise ValueError(
                "durable dispatch claims require repository, role, and owner"
            )

    def reserve(self, action: RunAgent, issue: int) -> bool:
        with self._lock:
            if issue in self._actions.values():
                return False
            if self._control is not None and not self._control.claim_dispatch(
                self._repository,
                self._role,
                issue,
                action.kind.name,
                self._owner_id,
            ):
                return False
            self._actions[action] = issue
            return True

    def finish(self, action: RunAgent) -> None:
        with self._lock:
            issue = self._actions.pop(action, None)
            if issue is not None:
                if self._control is not None:
                    self._control.release_dispatch_claim(
                        self._repository,
                        self._role,
                        issue,
                        self._owner_id,
                    )

    def issue_for(self, action: RunAgent) -> int | None:
        with self._lock:
            return self._actions.get(action)

    def running(self, number: int) -> bool:
        """Whether a dispatch this process reserved for target number `number` still holds its reservation."""
        with self._lock:
            return any(action.number == number for action in self._actions)


def _log(name: str):
    import sys

    def log(msg: str) -> None:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()  # unbuffered: a crash right after must not eat the line

    return log


def _onoff(enabled: bool) -> str:
    return "on" if enabled else "off"


def review_capability_line(*, can_approve: bool, adopt_unlabeled: bool) -> str:
    """One boot line for the reviewer. `approve=off` is solo identity mode: the
    reviewer still reviews and comments, but the verdict never becomes a GitHub
    approval, so nothing it does can merge anything."""
    return (
        "review: capabilities: review=on "
        f"approve={_onoff(can_approve)} adopt_unlabeled={_onoff(adopt_unlabeled)}"
    )


def _journal(store: Store) -> Journal:
    """The role's plain JSONL journal, beside its other local state."""

    return Journal(os.path.join(store.state_dir, "journal.jsonl"))


def _spend_cfg(cfg: HarnessConfig, role: str) -> dict:
    """Paid-run attribution; SQLite owns all admission decisions."""
    return {
        "role": role,
        "repo": cfg.repo,
    }


def _control_spend_gate(
    cfg: HarnessConfig, control: SQLiteControlStore, leg_usd: float
) -> Callable[[], str | None] | None:
    """Fast-path daily admission over the same authority as reservation.

    Paid dispatch waits while one leg's reservation no longer fits a cap, so a
    cap below a single leg is reported here rather than refused at admission.
    """

    caps = tuple(
        (repository, dimension, cap)
        for repository, dimension, cap in (
            (cfg.repo, "repository", cfg.daily_spend_cap_usd),
            ("*", "global", cfg.global_daily_spend_cap_usd),
        )
        if cap > 0
    )
    if not caps:
        return None
    reservation = Money.from_major("USD", leg_usd)

    def gate() -> str | None:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for repository, dimension, cap_usd in caps:
            scope = BudgetScope(
                repository,
                dimension,
                "USD",
                start.date().isoformat(),
                start,
                start + timedelta(days=1),
                ZoneInfo("UTC"),
            )
            snapshot = control.snapshot(scope)
            if snapshot.disposition.value != "available":
                return (
                    "ACCOUNTING "
                    f"{snapshot.disposition.value.upper()} — {snapshot.detail}"
                )
            cap = Money.from_major("USD", cap_usd)
            if snapshot.committed.micros + reservation.micros > cap.micros:
                return (
                    f"{dimension} daily spend cap reached: "
                    f"${snapshot.committed.micros / 1_000_000:.2f} spent, "
                    f"${leg_usd:.2f} leg / ${cap_usd:.2f}"
                )
        return None

    return gate


def _build_codex_runner(
    cfg: HarnessConfig,
    *,
    default_model: str | None,
    sandbox: str,
    inject_system_prompt: bool,
    resume_on_retry: bool,
    leg_role: str,
    log,
    state_dir: str = "",
):
    """Build either role's Codex adapter from the single runtime config.

    Fix and review deliberately differ only in their capability boundary: fixes
    need the git-capable sandbox, while a review is read-only. Keeping every
    transport, rollout-pricing, and budget option here prevents a new Codex
    accounting control reaching one paid role while silently skipping the other.
    `leg_role` names the role's process-supervision state.
    """
    from ..adapters.codex_runner import CodexProvider, CodexRunner
    from ..adapters.process_supervisor import ProcessSupervisor

    runtime = cfg.codex_runtime
    return CodexRunner(
        codex_bin=runtime.binary,
        provider=CodexProvider(
            provider_id=runtime.provider_id,
            name=runtime.provider_name,
            base_url=runtime.base_url,
            env_key=runtime.env_key,
            wire_api=runtime.wire_api,
        ),
        sandbox=sandbox,
        resume_on_retry=resume_on_retry,
        reasoning_effort=runtime.reasoning_effort,
        inject_system_prompt=inject_system_prompt,
        log=log,
        debug=os.environ.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False"),
        idle_timeout=(runtime.idle_timeout if runtime.idle_timeout > 0 else None),
        max_runtime=(runtime.max_runtime if runtime.max_runtime > 0 else None),
        transport_retries=runtime.transport_retries,
        backoff_base=runtime.backoff_base,
        rate_limit_backoff_base=runtime.rate_limit_backoff_base,
        max_rate_limit_wait=runtime.max_rate_limit_wait,
        max_rate_limit_retries=runtime.max_rate_limit_retries,
        cost_per_mtok_in=runtime.cost_per_mtok_in,
        cost_per_mtok_out=runtime.cost_per_mtok_out,
        cost_per_mtok=runtime.cost_per_mtok,
        cost_per_mtok_cached_in=runtime.cost_per_mtok_cached_in,
        enforce_budget=runtime.enforce_budget,
        process_supervisor=(
            ProcessSupervisor(
                process_supervision_state_dir(cfg, leg_role, state_dir)
            )
            if state_dir
            else None
        ),
        spend_evidence_dir=spend_evidence_dir(state_dir),
        rate_table=_rate_table(cfg, RuntimeName.CODEX.value),
        **({"default_model": default_model} if default_model else {}),
    )


@dataclass(frozen=True)
class _FixRuntimeBinding:
    runner: AgentRuntime
    prepare_cwd: Callable[[str], None] | None = None


def _bind_claude_fix_runtime(
    _cfg: HarnessConfig,
    _runtime_config: CodexRuntimeConfig,
    runner: AgentRuntime,
) -> _FixRuntimeBinding:
    from ..adapters.repository_guidance import RepositoryGuidanceRuntime

    return _FixRuntimeBinding(
        RepositoryGuidanceRuntime(runner, log=_log("fix"))
    )


def _bind_codex_fix_runtime(
    cfg: HarnessConfig,
    runtime_config: CodexRuntimeConfig,
    _runner: AgentRuntime,
) -> _FixRuntimeBinding:
    from ..adapters.codex_home import CodexHomeRuntime, write_codex_home

    log = _log("fix")
    runner = _build_codex_runner(
        cfg,
        default_model=runtime_config.model or None,
        sandbox=runtime_config.sandbox,
        inject_system_prompt=False,
        resume_on_retry=True,
        leg_role="fix",
        log=log,
        state_dir=cfg.state_dir,
    )
    reason = runner.preflight()
    log(
        f"codex: NOT READY at startup — {reason}"
        if reason is not None
        else "codex: runtime ready (bin + key present)"
    )
    prepare_cwd = partial(
        write_codex_home,
        agents_md=cfg.agent_md,
        state_dir=cfg.state_dir,
        auth_file=runner.oauth_auth_file(),
    )
    return _FixRuntimeBinding(
        CodexHomeRuntime(runner, prepare_cwd),
        prepare_cwd,
    )


def _bind_glm_fix_runtime(
    cfg: HarnessConfig,
    _runtime_config: CodexRuntimeConfig,
    runner: AgentRuntime,
) -> _FixRuntimeBinding:
    from ..adapters.claude_profile import (
        EnvInjectingRuntime,
        claude_profile_env,
        endpoint_host,
    )

    log = _log("fix")
    try:
        environment = claude_profile_env(cfg.glm_settings_file)
    except ValueError as exc:
        log(f"glm: NOT READY at startup — {exc}")
        return _FixRuntimeBinding(runner)
    log(
        f"glm: runtime ready (endpoint {endpoint_host(environment)}, "
        f"profile {environment['CLAUDE_CONFIG_DIR']})"
    )
    from ..adapters.repository_guidance import RepositoryGuidanceRuntime

    return _FixRuntimeBinding(
        RepositoryGuidanceRuntime(
            EnvInjectingRuntime(runner, env=environment),
            log=log,
        )
    )


_FIX_RUNTIME_REGISTRY: Mapping[
    RuntimeName,
    Callable[
        [HarnessConfig, CodexRuntimeConfig, AgentRuntime],
        _FixRuntimeBinding,
    ],
] = {
    RuntimeName.CLAUDE: _bind_claude_fix_runtime,
    RuntimeName.CODEX: _bind_codex_fix_runtime,
    RuntimeName.GLM: _bind_glm_fix_runtime,
}

if frozenset(_FIX_RUNTIME_REGISTRY) != frozenset(RuntimeName):
    raise RuntimeError("fix runtime registry is not exhaustive")


@dataclass(frozen=True)
class _ReviewRuntimeBinding:
    runner: AgentRuntime
    execution_cwd: str = ""
    prepare_cwd: Callable[[str], None] | None = None
    runtime_preflight: Callable[[], str | None] | None = None


def _claude_review_runtime_preflight() -> None:
    """Claude has no local static preflight; changing runtimes resolves a prior Codex setup hold."""
    return None


def _bind_claude_review_runtime(
    cfg: HarnessConfig,
    _runtime_config: ReviewRuntimeConfig,
    runner: AgentRuntime,
) -> _ReviewRuntimeBinding:
    from ..adapters.repository_guidance import RepositoryGuidanceRuntime

    # execution_cwd is what arms the pinned per-PR review checkout (roles
    # builds ReviewCheckouts off it) — without it Claude review legs read
    # whatever the operator's live checkout happens to contain (0d2e295 M1).
    return _ReviewRuntimeBinding(
        RepositoryGuidanceRuntime(runner, log=_log("review")),
        execution_cwd=cfg.target_dir,
        runtime_preflight=_claude_review_runtime_preflight,
    )


def _bind_codex_review_runtime(
    cfg: HarnessConfig,
    runtime_config: ReviewRuntimeConfig,
    _runner: AgentRuntime,
) -> _ReviewRuntimeBinding:
    from ..adapters.codex_home import CodexHomeRuntime, write_codex_home

    log = _log("review")
    runner = _build_codex_runner(
        cfg,
        default_model=runtime_config.model,
        sandbox="read-only",
        inject_system_prompt=True,
        resume_on_retry=False,
        leg_role="review",
        log=log,
        state_dir=cfg.review_state_dir,
    )
    reason = runner.preflight()
    log(
        f"codex: NOT READY at startup — {reason}"
        if reason is not None
        else "codex: runtime ready (bin + key present)"
    )
    execution_cwd = cfg.target_dir
    if not execution_cwd:
        log(
            "codex: NOT READY at startup — REVIEWER_RUNTIME=codex requires "
            "TARGET_DIR so its read-only leg has an explicit checkout"
        )
        return _ReviewRuntimeBinding(
            runner,
            runtime_preflight=lambda: "REVIEWER_RUNTIME=codex requires TARGET_DIR",
        )
    prepare_cwd = partial(
        write_codex_home,
        state_dir=cfg.review_state_dir,
        auth_file=runner.oauth_auth_file(),
    )
    return _ReviewRuntimeBinding(
        CodexHomeRuntime(runner, prepare_cwd),
        execution_cwd,
        prepare_cwd,
        runner.preflight,
    )


_REVIEW_RUNTIME_REGISTRY: Mapping[
    RuntimeName,
    Callable[
        [HarnessConfig, ReviewRuntimeConfig, AgentRuntime],
        _ReviewRuntimeBinding,
    ],
] = {
    RuntimeName.CLAUDE: _bind_claude_review_runtime,
    RuntimeName.CODEX: _bind_codex_review_runtime,
}

_SUPPORTED_REVIEW_RUNTIMES = frozenset(
    {RuntimeName.CLAUDE, RuntimeName.CODEX}
)
if frozenset(_REVIEW_RUNTIME_REGISTRY) != _SUPPORTED_REVIEW_RUNTIMES:
    raise RuntimeError("review runtime registry is not exhaustive")


def _lifetime_capped(cfg: HarnessConfig, control: SQLiteControlStore) -> Callable[[int], bool]:
    """Whether an issue can no longer afford one more fix leg under its
    lifetime cap: the same arithmetic paid admission applies."""
    if cfg.issue_spend_cap_usd <= 0:
        return lambda _issue: False
    cap = Money.from_major("USD", cfg.issue_spend_cap_usd)
    leg = Money.from_major("USD", cfg.fix_budget_usd)

    def capped(issue: int) -> bool:
        snapshot = control.snapshot(issue_budget_scope(cfg.repo, issue, "USD"))
        if snapshot.disposition.value != "available":
            # FixFacts reads a failed read as no headroom: paid legs wait, free work goes on.
            raise ControlStoreError(f"issue #{issue} spend is unreadable: {snapshot.detail}")
        return snapshot.committed.micros + leg.micros > cap.micros

    return capped


def _headroom(gate: Callable[[], str | None] | None) -> Callable[[], bool]:
    """Daily/global headroom for one more leg. An unreadable ledger is no
    headroom: free work continues, paid work waits silently (Q7)."""

    def headroom() -> bool:
        if gate is None:
            return True
        try:
            return gate() is None
        except Exception:  # noqa: BLE001 - see docstring
            return False

    return headroom


class _NoCommands:
    """The fix role queues no durable commands: every write is idempotent."""

    def drain_pending_commands(self) -> int:
        return 0


@dataclass(frozen=True)
class FixDaemonComposition:
    """The fix role's composed authorities.

    Exposed so acceptance tests drive the exact production wiring (facts,
    act, legs, recovery) instead of a hand-copied replica that can drift.
    """

    daemon: Daemon
    control: SQLiteControlStore
    journal: Journal
    act: GitHubAct
    legs: FixLegs
    recovery: _RecoveryHooks
    owner_id: str


def build_fix_daemon(
    cfg: HarnessConfig,
    gateway: GitHubGateway,
    store: Store,
    worktrees: Worktrees,
    runner: AgentRunner,
) -> Daemon:
    return compose_fix_daemon(cfg, gateway, store, worktrees, runner).daemon


def compose_fix_daemon(
    cfg: HarnessConfig,
    gateway: GitHubGateway,
    store: Store,
    worktrees: Worktrees,
    runner: AgentRunner,
    *,
    log: Callable[[str], None] | None = None,
) -> FixDaemonComposition:
    """observe -> derive -> policy -> act, with paid legs in a bounded pool."""
    log = log or _log("fix")
    journal = _journal(store)
    fix_runtime = cfg.fix_runtime
    binding = _FIX_RUNTIME_REGISTRY[cfg.fixer_runtime](cfg, cfg.codex_runtime, runner)
    control = _control_store(cfg, cfg.state_dir, "fix")
    owner_id = _owner_identity(cfg, "fix")
    settings = FixSettings(
        repository=cfg.repo,
        auto_merge=cfg.auto_merge,
        base_branch=cfg.base_branch,
        runtime=cfg.fixer_runtime.value,
        owner_id=owner_id,
        harness=Harness(fix_bot=fix_runtime.bot, review_bot=cfg.review_bot),
    )
    reconciler = _LateReservationReconciler()
    accounting = _with_accounting(
        binding.runner, control, cfg, "fix", cfg.fixer_runtime.value, reconciler,
    )
    runtime = JournalingRuntime(
        accounting, journal, repo=cfg.repo, role="fix",
        runtime=cfg.fixer_runtime.value, state_dir=cfg.state_dir,
    )
    daemon_ref: dict[str, Daemon] = {}
    legs = FixLegs(
        gateway, control, settings,
        runtime=runtime,
        accounting=accounting,
        worktrees=worktrees,
        task_config=FixTaskConfig(
            cfg.repo, cfg.base_branch, cfg.agent_md, fix_runtime.budget_usd,
            fix_runtime.model, fix_runtime.effort, runtime=cfg.fixer_runtime,
        ),
        prepare_cwd=binding.prepare_cwd,
        log=log,
    )
    dispatch = CancellableAsyncDispatch(legs.run)
    act = GitHubAct(
        gateway, control,
        repository=cfg.repo,
        base_branch=cfg.base_branch,
        merge_method=cfg.merge_method,
        close_issue_on_merge=cfg.close_issue_on_merge,
        marker=settings.harness.marker,
        dispatch=dispatch, remove_worktree=worktrees.remove, log=log,
    )
    facts = FixFacts(
        control, settings,
        active=lambda: daemon_ref["daemon"].active_dispatches if "daemon" in daemon_ref else (),
        headroom=_headroom(_control_spend_gate(cfg, control, fix_runtime.budget_usd)),
        lifetime_capped=_lifetime_capped(cfg, control),
    )
    recovery = _recovery(
        cfg.state_dir, cfg, control, _NoCommands(), "fix", worktrees,
        owner_id=owner_id, reconciler=reconciler,
    )
    daemon = Daemon(
        "fix",
        lambda: gateway.build_fix_world(cfg.base_branch),
        decide_with(facts, settings, lease_slots(daemon_ref), log),
        act,
        cfg.tick_interval_seconds,
        log,
        journal=journal,
        dispatch_capacity=cfg.fix_dispatch_concurrency,
        dispatch_key=lambda action: dispatch_item(action),
        cancel_active_dispatches=dispatch.cancel_all,
        force_kill_dispatches=_force_kill(cfg, "fix", cfg.state_dir),
        heartbeat_path=os.path.join(cfg.state_dir, "heartbeat.json"),
        reconcile=recovery.reconcile,
        prepare_shutdown=_shutdown_prepare(control, owner_id=owner_id),
        finalize=_shutdown_finalize(control, _NoCommands(), owner_id=owner_id),
        terminalize=_shutdown_terminalize(
            control,
            CodexSpendReconciler(control, _spend_evidence_dirs(cfg)),
            owner_id=owner_id,
            state_dir=cfg.state_dir,
            role="fix",
        ),
    )
    daemon_ref["daemon"] = daemon
    log(f"fix: capabilities: resolve=on merge={'on' if cfg.auto_merge else 'off'}")
    return FixDaemonComposition(daemon, control, journal, act, legs, recovery, owner_id)


def _review_unproductive_rounds(
    control: SQLiteControlStore, repository: str, world, errors: dict[int, str],
) -> dict[int, int]:
    """Current SQL allowance per target; unreadable authority withholds admission."""
    counts = {}
    for pr in world.prs:
        try:
            counts[pr.number] = current_review_failures(control, repository, pr.number)
        except (WorkflowAuthorityError, ControlStoreError, sqlite3.Error, OSError) as exc:
            errors[pr.number] = str(exc)
            counts[pr.number] = 0
    return counts


def _fix_holds(control, repository: str, world, now: datetime) -> dict[int, FixHold]:
    """PRs a live fix-role claim currently owns, keyed by PR number (#176).

    Both roles derive a claim target from `breaker_key`, so the fixer's claim on
    a PR (or on the issue that PR closes) is addressable from the review side
    without any new state. Two open PRs closing one issue therefore share a
    target and defer together — the same coarseness the dispatch reservation
    itself has, deliberately not a second, finer notion of "same work".

    A store that cannot answer is treated as no holds: this reader only
    withholds paid work, and the submit-time head fence still protects
    correctness, so it must never be the thing that stops review.
    """
    try:
        claims = control.active_dispatch_claims(repository, "fix")
    except Exception:  # noqa: BLE001 - see docstring: fail open to dispatch
        return {}
    by_target = {claim.target_key: claim for claim in claims}
    holds: dict[int, FixHold] = {}
    for pr in world.prs:
        claim = by_target.get(breaker_key(pr))
        if claim is None:
            continue
        holds[pr.number] = FixHold(
            pr.number,
            claim.claim_key,
            max(0.0, (now - claim.claimed_at).total_seconds()),
        )
    return holds


@dataclass(frozen=True)
class ReviewDaemonComposition:
    """The review role's composed authorities (see `FixDaemonComposition`).

    `runtime` is None only when the gateway cannot read pull request reviews;
    an activated review family then holds every paid review rather than
    falling back to the legacy path.
    """

    daemon: Daemon
    control: SQLiteControlStore
    journal: Journal
    executor: Executor
    effects: EffectRecovery
    dispatcher: AgentDispatcher
    runtime: ActiveReviewRuntime | None
    recovery: _RecoveryHooks
    owner_id: str


def build_review_daemon(
    cfg: HarnessConfig, gateway: GitHubGateway, store: Store, runner: AgentRuntime
) -> Daemon:
    return compose_review_daemon(cfg, gateway, store, runner).daemon


def compose_review_daemon(
    cfg: HarnessConfig, gateway: GitHubGateway, store: Store, runner: AgentRuntime,
    *, log: Callable[[str], None] | None = None,
) -> ReviewDaemonComposition:
    say: Callable[[str], None] = log or _log("review")
    repo_cfg = cfg.repository
    review_runtime = cfg.review_runtime
    journal = _journal(store)
    rcfg = ReviewConfig(
        review_bot=review_runtime.bot, identity_mode=repo_cfg.identity_mode,
        trigger_label=Label(review_runtime.trigger_label),
        adopt_unlabeled=review_runtime.adopt_unlabeled,
        # getattr, not a plain attribute read: the env knob
        # (MAX_UNPRODUCTIVE_REVIEW_ROUNDS) is wired into ReviewRuntimeConfig
        # separately from this ceiling. Until it lands the domain default
        # applies — a missing knob must never mean "no ceiling", which is the
        # unbounded state P0R-3 describes.
        max_unproductive_rounds=review_runtime.max_unproductive_rounds,
        # The SAME value the submission side applies (dcfg.max_review_rejections
        # below), so the decide side's abandoned-at-ceiling arm can never
        # disagree with the breaker that created the state it is healing.
        max_rejections=review_runtime.max_rejections,
    )
    dcfg = DispatchConfig(
        base_branch=repo_cfg.base_branch,
        review=ReviewTaskConfig(
            repo_cfg.repo, repo_cfg.base_branch, review_runtime.prompt,
            review_runtime.budget_usd, review_runtime.model,
            gate_model=review_runtime.gate_model,
            validation_model=review_runtime.validation_model,
            gate_budget_usd=review_runtime.gate_budget_usd,
            dimension_budget_usd=review_runtime.dimension_budget_usd,
            validation_budget_usd=review_runtime.validation_budget_usd,
            scout_model=review_runtime.scout_model,
        ),
        max_review_rejections=review_runtime.max_rejections,
        review_bot=review_runtime.bot,
        reviewer_runtime=review_runtime.runtime,
        review_trigger_label=review_runtime.trigger_label,
        review_mode=review_runtime.mode,
        log=say,
        **_spend_cfg(cfg, "review"),
    )
    runtime_binding = _REVIEW_RUNTIME_REGISTRY[review_runtime.runtime](
        cfg, review_runtime, runner
    )
    review_runner = runtime_binding.runner
    review_execution_cwd = runtime_binding.execution_cwd
    review_prepare_cwd = runtime_binding.prepare_cwd

    # Reviews must read the code the verdict binds. A pinned per-PR detached
    # worktree replaces the operator's live checkout as execution cwd whenever
    # a target checkout exists (keifu #165: stale-checkout reviews produced
    # phantom REQUEST_CHANGES rounds under fresh SHAs).
    review_checkout = None
    review_checkout_cleanup = None
    if review_execution_cwd:
        _review_checkouts = ReviewCheckouts(
            review_execution_cwd, worktree_root=cfg.worktree_root
        )
        review_checkout = _review_checkouts.ensure
        review_checkout_cleanup = _review_checkouts.remove

    dcfg = replace(
        dcfg,
        review_execution_cwd=review_execution_cwd,
        review_prepare_cwd=review_prepare_cwd,
        review_checkout=review_checkout,
        review_checkout_cleanup=review_checkout_cleanup,
    )

    # Reviewer has no worktree/issue side; pass a no-op Worktrees-like None usage.
    control = _control_store(cfg, cfg.review_state_dir, "review")
    owner_id = _owner_identity(cfg, "review")
    effect_executor = _effects(
        gateway,
        control,
        repository=cfg.repo,
        identity=owner_id,
        config_version=cfg.config_version,
        retry_ceiling=cfg.effect_retry_ceiling,
        journal=journal,
        alert_cmd=cfg.alert_cmd,
        log=say,
    )
    executor = Executor(
        gateway,
        log=say,
        journal=journal,
        alert_cmd=cfg.alert_cmd,
        effect_executor=effect_executor.actions,
    )
    reservations = DispatchReservations(
        control,
        repository=cfg.repo,
        role="review",
        owner_id=owner_id,
    )
    runtime = _active_review_runtime(
        cfg, rcfg, control, gateway, store, effect_executor.actions, owner_id, say,
        runtime_preflight=runtime_binding.runtime_preflight,
    )
    if runtime is not None:
        executor.bind_workflow_reconciliation(runtime.reconcile)
        effect_executor = replace(effect_executor, reconcile_workflow=runtime.reconcile)
    review_reconciler = _LateReservationReconciler()
    dispatcher = AgentDispatcher(
        dcfg, gateway, store,
        JournalingRuntime(
            _with_accounting(
                review_runner,
                control,
                cfg,
                "review",
                review_runtime.runtime.value,
                review_reconciler,
            ),
            journal,
            repo=cfg.repo,
            role="review",
            runtime=review_runtime.runtime.value,
            state_dir=cfg.review_state_dir,
        ),
        executor,
        journal=journal,
        dispatch_claim_target=reservations.issue_for,
        review_authority=runtime,
    )
    dispatch_runtime = CancellableAsyncDispatch(
        dispatcher.dispatch,
        finished=reservations.finish,
    )
    executor.bind_dispatch(dispatch_runtime)

    migration_reported_at: datetime | None = None

    def decide_actions(world) -> tuple[list, ReviewDispatchPlan | None]:
        nonlocal migration_reported_at
        try:
            active = review_family_activated(control, cfg.repo)
        except WorkflowAuthorityError as exc:
            # Unknown activation: neither path may buy a review this tick.
            say(f"review dispatch held: {exc}")
            return [], None
        if not active:
            now = datetime.now(timezone.utc)
            if migration_reported_at is None or now - migration_reported_at >= timedelta(minutes=15):
                migration_reported_at = now
                say("review dispatch held: review_migration_required")
                if journal is not None:
                    journal.emit(TelemetryEvent(TelemetryWarning(
                        "review_migration_required", "review family requires explicit activation",
                        {"repository": cfg.repo},
                    )))
            return [], None
        migration_reported_at = None
        if runtime is None:
            say("review dispatch held: the review family is active but this "
                "gateway cannot read pull request reviews")
            return [], None
        # The breaker counters come first: the plan records the admission
        # decision `decide_bound_review` applies with exactly these counts.
        counter_errors: dict[int, str] = {}
        unproductive = _review_unproductive_rounds(
            control, cfg.repo, world, counter_errors
        )
        plan = runtime.plan(world, rcfg, unproductive, counter_errors)
        errors: dict[int, str] = {**plan.errors, **counter_errors}
        for target, reason in errors.items():
            say(f"PR #{target} held: {reason}")
        for target, reason in plan.held.items():
            say(f"PR #{target} review held by its workflow: {reason}")
        return decide_bound_review(world, rcfg, BoundReviewState(
            owed=plan.owed, unproductive_rounds=unproductive, control_errors=errors,
        )), plan

    def decide(world):
        actions, review_plan = decide_actions(world)
        admissions: dict[int, AdmissionOutcome] = {}
        acquired: list[RunAgent] = []
        try:
            admitted = _admit(world, actions, admissions, acquired)
        except BaseException:
            # Nothing reaches the executor when admission fails midway. Only
            # this batch's claims are ours to release; running work keeps its own.
            for action in acquired:
                try:
                    reservations.finish(action)
                except Exception as exc:  # noqa: BLE001 - attempt every release
                    say(f"review admission cleanup failed for PR #{action.number}: {exc}")
            raise
        if review_plan is not None and runtime is not None:
            # After admission, so an owed review it did not start is recorded
            # as withheld or running. `record_plan` never raises: the record
            # is the operator view's projection, never dispatch authority.
            runtime.record_plan(review_plan, admissions)
        return admitted

    def _admit(world, actions, admissions: dict[int, AdmissionOutcome], acquired: list[RunAgent]) -> list:
        plan = hold_review_dispatch(
            actions,
            _fix_holds(control, cfg.repo, world, datetime.now(timezone.utc)),
            ceiling_seconds=fix_hold_ceiling_seconds(cfg.agent_max_runtime),
        )
        for hold in plan.deferred:
            admissions[hold.pr] = AdmissionWithheld(
                WithheldReason.FIX_CLAIM, f"a fix run still holds this pull request's issue ({int(hold.held_seconds)}s)")
        actions = list(plan.actions)
        prs = {pr.number: pr for pr in world.prs}
        rejected_prs: set[int] = set()
        for action in actions:
            if not isinstance(action, RunAgent):
                continue
            pr = prs.get(action.number)
            # ADR 0008: the repository-global claim and paid admission must use
            # one target identity. Branch linkage is authoritative when GitHub
            # omits a closing issue on a non-default-base PR.
            issue = breaker_key(pr) if pr is not None else -action.number
            if reservations.reserve(action, issue):
                acquired.append(action)
                continue
            rejected_prs.add(action.number)
            if reservations.running(action.number):
                # This pull request's own review still holds the target: it is
                # the owed review, running, not a competing claim.
                admissions[action.number] = AdmissionInFlight()
                continue
            admissions[action.number] = AdmissionWithheld(WithheldReason.RESERVED, (
                f"target {issue} is already reserved (another pull request on the same issue here, or another "
                "deployment's claim)"))
            say(
                f"dispatch {action.kind.name} PR #{action.number}: "
                f"deferred — target {issue} is already reserved (another PR "
                "on the same issue here, or another deployment's claim)"
            )
        if not rejected_prs:
            return actions
        return [
            action
            for action in actions
            if not (
                isinstance(action, RunAgent)
                and action.number in rejected_prs
            )
            and not (
                isinstance(action, Relabel)
                and action.target is Target.PR
                and action.number in rejected_prs
            )
        ]

    say(review_capability_line(
        can_approve=rcfg.identity_mode == IdentityMode.THREE,
        adopt_unlabeled=rcfg.adopt_unlabeled,
    ))
    recovery = _recovery(
        cfg.review_state_dir,
        cfg,
        control,
        effect_executor,
        "review",
        owner_id=owner_id,
        reconciler=review_reconciler,
    )
    if isinstance(gateway, ReviewReads):
        execution_recovery = SQLiteReviewExecutionControl(
            control, WorkflowWriter(cfg.repo, "review", owner_id), gateway,
            preparation_control=runtime.control if runtime is not None else None,
        )
        base_reconcile = recovery.reconcile
        blocked_reported_at: datetime | None = None

        def reconcile_review(trigger: RecoveryTrigger) -> RecoveryReport:
            nonlocal blocked_reported_at
            report = base_reconcile(trigger)
            sweep = execution_recovery.reconcile_review_rounds()
            if sweep.sealed:
                say(f"review recovery: fenced {len(sweep.sealed)} abandoned parent round(s)")
            if sweep.finalized:
                say(f"review recovery: finalized {len(sweep.finalized)} parent round(s)")
            if sweep.blocked:
                now = datetime.now(timezone.utc)
                if blocked_reported_at is None or now - blocked_reported_at >= timedelta(minutes=15):
                    blocked_reported_at = now
                    detail = "; ".join(f"{round_id}: {reason}" for round_id, reason in sweep.blocked[:3])
                    say(f"review recovery: {len(sweep.blocked)} parent round(s) could not be recovered: {detail}")
            return report

        recovery = _RecoveryHooks(reconcile_review)
    else:
        say("review execution recovery unavailable: the gateway cannot read pull request reviews")

    daemon = Daemon("review", gateway.build_review_world, decide, executor,
                  cfg.tick_interval_seconds, say, journal=journal,
                  tick_gate=_control_spend_gate(cfg, control, cfg.review_budget_usd),
                  dispatch_capacity=1,
                  dispatch_key=lambda action: ("pr", action.number),
                  cancel_active_dispatches=dispatch_runtime.cancel_all,
                  dispatch_rejected=reservations.finish,
                  force_kill_dispatches=_force_kill(
                      cfg,
                      "review",
                      cfg.review_state_dir or store.state_dir
                  ),
                  heartbeat_path=os.path.join(
                      cfg.review_state_dir, "heartbeat.json"
                  ),
                  reconcile=recovery.reconcile,
                  prepare_shutdown=_shutdown_prepare(
                      control,
                      owner_id=owner_id,
                  ),
                  finalize=_shutdown_finalize(
                      control,
                      effect_executor,
                      owner_id=owner_id,
                  ),
                  terminalize=_shutdown_terminalize(
                      control,
                      CodexSpendReconciler(
                          control, _spend_evidence_dirs(cfg)
                      ),
                      owner_id=owner_id,
                      state_dir=cfg.review_state_dir,
                      role="review",
                  ))
    return ReviewDaemonComposition(
        daemon, control, journal, executor, effect_executor, dispatcher, runtime,
        recovery, owner_id,
    )


def _active_review_runtime(
    cfg: HarnessConfig, rcfg: ReviewConfig, control: SQLiteControlStore, gateway,
    store: Store, effects: GitHubEffectExecutor, owner_id: str, log,
    *, runtime_preflight: Callable[[], str | None] | None = None,
) -> ActiveReviewRuntime | None:
    """The review family's runtime, or None when the gateway cannot read reviews."""
    if not isinstance(gateway, ReviewReads):
        return None
    # `rcfg` is derived from `cfg.review_runtime`, the same values the shared
    # composition reads, so the operator transport enforces this daemon's rule.
    workflow = configured_review_control(
        cfg, control, gateway, store, owner_id, runtime_preflight=runtime_preflight,
    )
    return ActiveReviewRuntime(
        workflow, effects, gateway, review_bot=rcfg.review_bot, log=log,
        mode=cfg.review_runtime.mode, runtime=cfg.review_runtime.runtime,
        failure_ceiling=cfg.review_runtime.max_unproductive_rounds,
    )
