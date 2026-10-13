"""Assemble the two role daemons from config + adapters.

Each `compose_*_daemon` binds the same tick (`tick.py`):
  * observe  → `gateway.build_world` (one GraphQL observation),
  * decide   → derive + policy, keeping the role's own Actions,
  * executor → `adapters.act.GitHubAct`; `RunAgent` goes to the role's legs.

Each paid leg owns a bounded background worker and event loop; observation
and GitHub writes remain on the daemon thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from zoneinfo import ZoneInfo

from ..adapters.act import GitHubAct
from ..adapters.agent_runner import AgentRunner
from ..adapters.accounting_runtime import AccountingRuntime, issue_budget_scope
from ..adapters.control_path import control_store_path
from ..adapters.control_store import ControlStoreError, SQLiteControlStore
from ..adapters.github import GitHubGateway
from ..adapters.journal import Journal, JournalingRuntime
from ..adapters.runtime_plan import AgentRuntime
from ..adapters.spend_recovery import CodexSpendReconciler, spend_evidence_dir
from ..adapters.store import Store
from ..adapters.worktree import ReviewCheckouts, Worktrees
from ..domain.actions import RunAgent
from ..domain.agent_task import RuntimeName
from ..domain.contracts import BudgetScope, Money
from ..domain.ports import SpendReconciler, StaleReservationReconciler
from ..domain.recovery import RecoveryTrigger
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
from ..domain.state import Harness
from ..domain.identity import login_eq
from .config import HarnessConfig, deployment_identity_for_target, owner_identity_for_target
from .fix import FixLegs, FixSettings, fix_choose
from .tick import Daemon
from .planners.tasks import FixTaskConfig, ReviewTaskConfig
from .recovery import NoWorktreeRecovery, RecoveryCoordinator, RecoveryReport
from .review import ReviewAct, ReviewLegs, ReviewSettings, review_choose
from .tick import Facts, decide_with, dispatch_item, lease_slots

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


def _owner_identity(cfg: HarnessConfig, role: str) -> str:
    return owner_identity_for_target(cfg.repo, cfg.target_dir, role)


def _control_store(cfg: HarnessConfig, state_dir: str) -> SQLiteControlStore:
    """Open the one accounting/control authority shared by all role daemons.

    The first open after the 3-table cutover release retires a legacy store
    (see `control_store._cut_over`); `CutoverRefused` stops the boot while
    that store still holds unsettled money.
    """

    control = SQLiteControlStore(
        control_store_path(
            (cfg.state_dir, cfg.review_state_dir),
            fallback_state_dir=state_dir,
            authority_root=cfg.control_authority_root,
        )
    )
    control.initialize()
    return control


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
        reservation_reconciler=reconciler,
    )


def _budget_policy(cfg: HarnessConfig) -> BudgetPolicy:
    def money(value: float) -> Money:
        return Money.from_major("USD", value)

    limits: dict[tuple[str, str], Money] = {}
    for kind in ("RESOLVE", "ADDRESS_REVIEW", "FIX_CI", "REBASE"):
        limits[("fix", kind)] = money(cfg.fix_budget_usd)
    limits[("review", "REVIEW")] = money(cfg.review_budget_usd)
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
class _RecoveryHooks:
    reconcile: Callable[[RecoveryTrigger], object]


def _recovery(
    state_dir: str,
    cfg: HarnessConfig,
    control: SQLiteControlStore,
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
        written = 0
        for intent in intents:
            spend.reconcile(intent.run_id)
            # One file per issue: rewriting it at a later shutdown is idempotent.
            receipt = write_shutdown_handover(
                state_dir=state_dir,
                role=role,
                issue=intent.issue_number,
                run_id=str(intent.run_id),
                kind=intent.kind,
                reason=intent.reason,
            )
            written += receipt is not None
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


def _log(name: str):
    import sys

    def log(msg: str) -> None:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()  # unbuffered: a crash right after must not eat the line

    return log


def _journal(store: Store) -> Journal:
    """The role's plain JSONL journal, beside its other local state."""

    return Journal(os.path.join(store.state_dir, "journal.jsonl"))


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

    runtime = cfg
    return CodexRunner(
        codex_bin=runtime.codex_bin,
        provider=CodexProvider(
            provider_id=runtime.codex_provider_id,
            name=runtime.codex_provider_name,
            base_url=runtime.codex_base_url,
            env_key=runtime.codex_env_key,
            wire_api=runtime.codex_wire_api,
        ),
        sandbox=sandbox,
        resume_on_retry=resume_on_retry,
        reasoning_effort=runtime.codex_reasoning_effort,
        inject_system_prompt=inject_system_prompt,
        log=log,
        debug=os.environ.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False"),
        idle_timeout=(runtime.agent_idle_timeout if runtime.agent_idle_timeout > 0 else None),
        max_runtime=(runtime.agent_max_runtime if runtime.agent_max_runtime > 0 else None),
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
    _runtime_config: HarnessConfig,
    runner: AgentRuntime,
) -> _FixRuntimeBinding:
    from ..adapters.repository_guidance import RepositoryGuidanceRuntime

    return _FixRuntimeBinding(
        RepositoryGuidanceRuntime(runner, log=_log("fix"))
    )


def _bind_codex_fix_runtime(
    cfg: HarnessConfig,
    runtime_config: HarnessConfig,
    _runner: AgentRuntime,
) -> _FixRuntimeBinding:
    from ..adapters.codex_home import CodexHomeRuntime, write_codex_home

    log = _log("fix")
    runner = _build_codex_runner(
        cfg,
        default_model=runtime_config.codex_model or None,
        sandbox=runtime_config.codex_sandbox,
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


_FIX_RUNTIME_REGISTRY: Mapping[
    RuntimeName,
    Callable[
        [HarnessConfig, HarnessConfig, AgentRuntime],
        _FixRuntimeBinding,
    ],
] = {
    RuntimeName.CLAUDE: _bind_claude_fix_runtime,
    RuntimeName.CODEX: _bind_codex_fix_runtime,
}

if frozenset(_FIX_RUNTIME_REGISTRY) != frozenset(RuntimeName):
    raise RuntimeError("fix runtime registry is not exhaustive")


@dataclass(frozen=True)
class _ReviewRuntimeBinding:
    runner: AgentRuntime
    prepare_cwd: Callable[[str], None] | None = None


def _bind_claude_review_runtime(
    cfg: HarnessConfig,
    _runtime_config: HarnessConfig,
    runner: AgentRuntime,
) -> _ReviewRuntimeBinding:
    from ..adapters.repository_guidance import RepositoryGuidanceRuntime

    return _ReviewRuntimeBinding(RepositoryGuidanceRuntime(runner, log=_log("review")))


def _bind_codex_review_runtime(
    cfg: HarnessConfig,
    runtime_config: HarnessConfig,
    _runner: AgentRuntime,
) -> _ReviewRuntimeBinding:
    from ..adapters.codex_home import CodexHomeRuntime, write_codex_home

    log = _log("review")
    runner = _build_codex_runner(
        cfg,
        default_model=runtime_config.review_model,
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
    prepare_cwd = partial(
        write_codex_home,
        state_dir=cfg.review_state_dir,
        auth_file=runner.oauth_auth_file(),
    )
    return _ReviewRuntimeBinding(CodexHomeRuntime(runner, prepare_cwd), prepare_cwd)


_REVIEW_RUNTIME_REGISTRY: Mapping[
    RuntimeName,
    Callable[
        [HarnessConfig, HarnessConfig, AgentRuntime],
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
    """Whether an issue can no longer afford one more leg under its lifetime
    cap: the arithmetic paid admission applies, for the dearer of the two
    roles' legs. Both roles must agree, since the stop it leads to is written
    by fix alone: with each role's own leg price, a review the cap cannot
    afford would be stopped by nobody and run by nobody."""
    if cfg.issue_spend_cap_usd <= 0:
        return lambda _issue: False
    cap = Money.from_major("USD", cfg.issue_spend_cap_usd)
    leg = Money.from_major("USD", max(cfg.fix_budget_usd, cfg.review_budget_usd))

    def capped(issue: int) -> bool:
        snapshot = control.snapshot(issue_budget_scope(cfg.repo, issue, "USD"))
        if snapshot.disposition.value != "available":
            # `Facts` reads a failed read as no headroom: paid legs wait, free work goes on.
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
    binding = _FIX_RUNTIME_REGISTRY[cfg.fixer_runtime](cfg, cfg, runner)
    control = _control_store(cfg, cfg.state_dir)
    owner_id = _owner_identity(cfg, "fix")
    settings = FixSettings(
        repository=cfg.repo,
        auto_merge=cfg.auto_merge,
        base_branch=cfg.base_branch,
        runtime=cfg.fixer_runtime.value,
        owner_id=owner_id,
        harness=Harness(fix_bot=cfg.fix_bot, review_bot=cfg.review_bot),
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
            cfg.repo, cfg.base_branch, cfg.agent_md, cfg.fix_budget_usd,
            cfg.fix_model, cfg.fix_effort, runtime=cfg.fixer_runtime,
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
        marker=settings.harness.marker,
        dispatch=dispatch, remove_worktree=worktrees.remove, log=log,
    )
    facts = Facts(
        control, cfg.repo,
        active=lambda: daemon_ref["daemon"].active_dispatches if "daemon" in daemon_ref else (),
        headroom=_headroom(_control_spend_gate(cfg, control, cfg.fix_budget_usd)),
        lifetime_capped=_lifetime_capped(cfg, control),
        log=log,
    )
    recovery = _recovery(
        cfg.state_dir, cfg, control, "fix", worktrees,
        owner_id=owner_id, reconciler=reconciler,
    )
    daemon = Daemon(
        "fix",
        lambda: gateway.build_world(cfg.base_branch),
        decide_with("fix", facts, settings.harness, fix_choose(settings, log), lease_slots(daemon_ref), log),
        act,
        cfg.poll_seconds,
        log,
        journal=journal,
        dispatch_capacity=cfg.fix_dispatch_concurrency,
        dispatch_key=lambda action: dispatch_item(action),
        cancel_active_dispatches=dispatch.cancel_all,
        force_kill_dispatches=_force_kill(cfg, "fix", cfg.state_dir),
        heartbeat_path=os.path.join(cfg.state_dir, "heartbeat.json"),
        reconcile=recovery.reconcile,
        prepare_shutdown=_shutdown_prepare(control, owner_id=owner_id),
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


@dataclass(frozen=True)
class ReviewDaemonComposition:
    """The review role's composed authorities (see `FixDaemonComposition`)."""

    daemon: Daemon
    control: SQLiteControlStore
    journal: Journal
    act: ReviewAct
    legs: ReviewLegs
    recovery: _RecoveryHooks
    owner_id: str


def build_review_daemon(
    cfg: HarnessConfig, gateway: GitHubGateway, store: Store, runner: AgentRuntime
) -> Daemon:
    return compose_review_daemon(cfg, gateway, store, runner).daemon


def compose_review_daemon(
    cfg: HarnessConfig, gateway: GitHubGateway, store: Store, runner: AgentRuntime,
    *, checkouts=None, log: Callable[[str], None] | None = None,
) -> ReviewDaemonComposition:
    """observe -> derive -> policy -> act, keeping only REVIEW dispatches.

    `checkouts` pins each leg to its head; by default a detached per-PR
    worktree of TARGET_DIR (keifu #165: reviews of the operator's live
    checkout produced phantom change requests under fresh SHAs).

    Two misconfigurations no retry can fix refuse to start (ValueError, so
    `main` exits INVALID_CONFIG): no TARGET_DIR to pin reviews to, and a
    review credential that is not REVIEW_BOT. The latter posted a duplicate
    review on every retry, since the read-back looked for the wrong login."""
    log = log or _log("review")
    if checkouts is None:
        if not cfg.target_dir:
            raise ValueError("the review role needs TARGET_DIR: each review reads a checkout pinned to its head")
        checkouts = ReviewCheckouts(cfg.target_dir, worktree_root=cfg.worktree_root)
    acting_as = gateway.viewer_login()
    if not login_eq(acting_as, cfg.review_bot):
        raise ValueError(
            f"REVIEW_BOT is {cfg.review_bot!r} but the review credential acts as "
            f"{acting_as or 'nobody GitHub would name'!r}: set REVIEW_BOT to the "
            "credential's login, or give the review role REVIEW_BOT's credential"
        )
    journal = _journal(store)
    binding = _REVIEW_RUNTIME_REGISTRY[cfg.reviewer_runtime](cfg, cfg, runner)
    control = _control_store(cfg, cfg.review_state_dir)
    owner_id = _owner_identity(cfg, "review")
    settings = ReviewSettings(
        repository=cfg.repo, owner_id=owner_id,
        harness=Harness(fix_bot=cfg.fix_bot, review_bot=cfg.review_bot),
    )
    reconciler = _LateReservationReconciler()
    accounting = _with_accounting(
        binding.runner, control, cfg, "review", cfg.reviewer_runtime.value, reconciler,
    )
    runtime = JournalingRuntime(
        accounting, journal, repo=cfg.repo, role="review",
        runtime=cfg.reviewer_runtime.value, state_dir=cfg.review_state_dir,
    )
    daemon_ref: dict[str, Daemon] = {}
    legs = ReviewLegs(
        gateway, control, settings,
        runtime=runtime,
        accounting=accounting,
        checkouts=checkouts,
        prepare_cwd=binding.prepare_cwd,
        task_config=ReviewTaskConfig(
            cfg.repo, cfg.base_branch, cfg.review_prompt, cfg.review_budget_usd, cfg.review_model,
        ),
        log=log,
    )
    dispatch = CancellableAsyncDispatch(legs.run)
    act = ReviewAct(dispatch)
    facts = Facts(
        control, cfg.repo,
        active=lambda: daemon_ref["daemon"].active_dispatches if "daemon" in daemon_ref else (),
        headroom=_headroom(_control_spend_gate(cfg, control, cfg.review_budget_usd)),
        lifetime_capped=_lifetime_capped(cfg, control),
        log=log,
    )
    recovery = _recovery(
        cfg.review_state_dir, cfg, control, "review",
        owner_id=owner_id, reconciler=reconciler,
    )
    daemon = Daemon(
        "review",
        lambda: gateway.build_world(cfg.base_branch),
        decide_with("review", facts, settings.harness, review_choose(settings.harness), lease_slots(daemon_ref), log),
        act,
        cfg.poll_seconds,
        log,
        journal=journal,
        dispatch_capacity=1,
        dispatch_key=lambda action: dispatch_item(action),
        cancel_active_dispatches=dispatch.cancel_all,
        force_kill_dispatches=_force_kill(cfg, "review", cfg.review_state_dir or store.state_dir),
        heartbeat_path=os.path.join(cfg.review_state_dir, "heartbeat.json"),
        reconcile=recovery.reconcile,
        prepare_shutdown=_shutdown_prepare(control, owner_id=owner_id),
        terminalize=_shutdown_terminalize(
            control,
            CodexSpendReconciler(control, _spend_evidence_dirs(cfg)),
            owner_id=owner_id,
            state_dir=cfg.review_state_dir,
            role="review",
        ),
    )
    daemon_ref["daemon"] = daemon
    log(f"review: capabilities: review=on runtime={cfg.reviewer_runtime.value}")
    return ReviewDaemonComposition(daemon, control, journal, act, legs, recovery, owner_id)
