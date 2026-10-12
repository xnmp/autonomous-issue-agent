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
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from ..adapters.agent_runner import AgentRunner
from ..adapters.agent_publish import AgentPublicationBroker
from ..adapters.control_store import (
    ControlStoreError,
    LegacySpendMigrationError,
    ReservationRequest,
    ReservationResult,
    SQLiteControlStore,
)
from ..adapters.control_path import control_store_path
from ..adapters.effects import GitHubEffectExecutor
from ..adapters.accounting_runtime import AccountingRuntime
from ..adapters.environment import boot_manifest
from ..adapters.event_log import EventLog
from ..adapters.typed_journal import TypedJournal
from ..adapters.github import GitHubGateway, Identity
from ..adapters.journal import (
    JournalingRuntime,
    LegacyJournalLocator,
    default_ledger_path,
)
from ..adapters.store import Store
from ..adapters.spend_recovery import CodexSpendReconciler, spend_evidence_dir
from ..adapters.wake import WakeSignal, wake_path
from ..adapters.worktree import ReviewCheckouts, Worktrees
from ..adapters.workflow_control import SQLiteWorkflowControl
from ..adapters.review_effects import ReviewReads
from ..adapters.review_execution_control import SQLiteReviewExecutionControl
from ..adapters.review_workflow_control import review_family_activated
from ..adapters.workflow_rows import WorkflowAuthorityError

from ..domain.fix import (
    FixConfig,
    FixState,
    InFlightFix,
    breaker_key,
    ci_flakes,
    CiStallStep,
    ci_remediations,
    ci_remediation_marker,
    decide_fix,
    duplicate_pr_guards,
    issue_holds,
    automerge_in_scope,
    merge_preconditions_met,
)
from ..domain.review import REVIEW_APPROVED
from ..domain.review_dispatch_record import (
    AdmissionInFlight, AdmissionOutcome, AdmissionWithheld, WithheldReason,
)
from ..domain.labels import Label
from ..domain.ci import CIState, ci_state, parse_timestamp
from ..domain import continuation_components as cc
from ..domain.workflow import (
    ObservedIssue, WorkflowAbsent, WorkflowMigrationRequired, WorkflowRead, WorkflowSnapshot, WorkflowUnavailable,
)
from ..domain.workflow_identity import IssueRef, PullRequestRef, WorkflowKey, WorkflowWriter
from ..domain.workflow_ports import WorkflowControl
from ..domain.lifecycle import pr_lifecycle
from ..domain import review as review_dom
from ..domain.identity import login_eq
from ..domain.review import (
    DEFAULT_MAX_UNPRODUCTIVE_ROUNDS,
    BoundReviewState,
    FixHold,
    IdentityMode,
    ReviewConfig,
    decide_bound_review,
    fix_hold_ceiling_seconds,
    hold_review_dispatch,
)
from .planners.tasks import REVIEW_DIMENSIONS, FixTaskConfig, ReviewTaskConfig
from ..domain.actions import AgentKind, NoOp, Relabel, ResetCiRetries, RunAgent, Target
from ..adapters.runtime_plan import AgentRuntime
from ..domain.effect_retry import DEFAULT_EFFECT_RETRY_CEILING
from ..domain.agent_task import RuntimeName
from ..domain.contracts import BudgetScope, Money, EffectStatus
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
    CiFlakeDetected,
    CiPendingDeadline,
    Correlation,
    DuplicatePrPrevented,
    LevelChanged,
    PathPrediction,
    Producer,
    RecurrenceDetected,
    TelemetryEvent,
    TelemetryWarning,
)
from ..domain.ports import (
    WorkflowControlError,
    CommandRecovery,
    EventSink,
    IssueStateReasonReads,
    LevelState,
    PredictionReader,
    RecentMergesReads,
    SpendReconciler,
    StaleReservationReconciler,
)
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
    ActiveReviewRuntime, ReviewDispatchPlan, configured_review_control,
    review_guidance_reader, current_review_failures,
)
from .workflows.resolve_rearm import ResolveRearmWorkflow
from .loop import Daemon
from .world_reporter import WorldReporter
from .recovery import NoWorktreeRecovery, RecoveryCoordinator, RecoveryReport


def _ignore_world(_world: object) -> None:
    return None


def _spend_evidence_dirs(cfg: HarnessConfig) -> tuple[str, ...]:
    return tuple(
        spend_evidence_dir(os.path.abspath(path))
        for path in dict.fromkeys(
            (cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir)
        )
        if path
    )


def _role_state_dir(cfg: HarnessConfig, role: str) -> str:
    return {
        "fix": cfg.state_dir,
        "review": cfg.review_state_dir,
        "steward": cfg.steward_state_dir,
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
    journal: TypedJournal,
) -> SQLiteControlStore:
    """Open the one accounting/control authority shared by all role daemons."""

    control = SQLiteControlStore(_control_store_path(cfg, state_dir))
    control.initialize()
    role = journal.events.producer.role
    prior_deployments = prior_deployment_identities(cfg, role)
    prior_owners = configured_prior_owner_ids(cfg, role)
    control.adopt_legacy_owner(
        cfg.repo,
        role,
        _owner_identity(cfg, role),
        prior_owner_ids=prior_owners,
    )
    control.bind_active_owner(_owner_identity(cfg, role))
    control.bind_event_outbox(
        journal.events.prepare,
        journal.events.append_committed,
        line_limit=journal.events.line_limit,
        destination=journal.events.producer.feed_identity,
    )
    for prior_repository, prior_deployment in prior_deployments:
        control.adopt_projection_feed(
            prior_deployment=prior_deployment,
            current_deployment=journal.events.producer.deployment,
            repository=cfg.repo,
            role=role,
            prior_destination=f"{prior_deployment}:{prior_repository}:{role}",
            current_destination=journal.events.producer.feed_identity,
        )
    return control


def _control_store_path(cfg: HarnessConfig, state_dir: str) -> str:
    return control_store_path(
        (cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir),
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
    journal: TypedJournal,
    reconciler: _LateReservationReconciler | None = None,
    *,
    admit: Callable[[ReservationRequest], ReservationResult] | None = None,
) -> AgentRuntime:
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
        journal.events.path,
        journal.events.producer.deployment,
        journal.events.producer.role,
        journal.events.line_limit,
        owner_id=_owner_identity(cfg, role),
        github_host=os.environ.get("GH_HOST", "github.com"),
        approved_base_branch=cfg.base_branch,
        require_dispatch_claim=True,
        reservation_reconciler=reconciler,
        max_contract_rounds=cfg.max_contract_rounds,
        admit=admit,
    )


def _budget_policy(cfg: HarnessConfig) -> BudgetPolicy:
    def money(value: float) -> Money:
        return Money.from_major("USD", value)

    limits: dict[tuple[str, str], Money] = {}
    for kind in (
        "RESOLVE",
        "RESUME",
        "PR_RESUME",
        "ADDRESS_REVIEW",
        "FIX_CI",
        "REBASE",
        "AUTHOR",
        "IMPLEMENT",
        "ADJUDICATE",
    ):
        limits[("fix", kind)] = money(cfg.fix_budget_usd)
    limits[("fix", "ROUTE")] = money(cfg.router_budget_usd)
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
    publications: AgentPublicationBroker
    reconcile_workflow: Callable[[], int] | None = None

    def drain_pending_commands(self) -> int:
        recovered = self.reconcile_workflow() if self.reconcile_workflow is not None else 0
        return (
            recovered + self.actions.drain_pending_commands()
            + self.publications.drain_pending_commands()
        )


def _effects(
    gateway: GitHubGateway,
    control: SQLiteControlStore,
    *,
    repository: str,
    identity: str,
    config_version: str,
    base_branch: str,
    fix_config: FixConfig = FixConfig(fix_bot=""),
    retry_ceiling: int = DEFAULT_EFFECT_RETRY_CEILING,
    journal: TypedJournal | None = None,
    alert_cmd: str = "",
    log: Callable[[str], None] | None = None,
) -> EffectRecovery:
    executor = GitHubEffectExecutor(
        gateway,
        control,
        repository=repository,
        identity=identity,
        config_version=config_version,
        fix_config=fix_config,
        retry_ceiling=retry_ceiling,
        journal=journal,
        alert_cmd=alert_cmd,
        log=log,
    )
    publications = AgentPublicationBroker(
        control,
        repository=repository,
        base_branch=base_branch,
        host=os.environ.get("GH_HOST", "github.com"),
        config_version=config_version,
        identity=f"{identity}:publication",
        owner=identity,
    )
    return EffectRecovery(executor, publications)


@dataclass(frozen=True)
class _RecoveryHooks:
    reconcile: Callable[[RecoveryTrigger], object]


def _recovery(
    state_dir: str,
    cfg: HarnessConfig,
    control: SQLiteControlStore,
    effects: EffectRecovery,
    journal: TypedJournal,
    worktrees: Worktrees | None = None,
    *,
    owner_id: str,
    reconciler: _LateReservationReconciler | None = None,
) -> _RecoveryHooks:
    from ..adapters.process_supervisor import ProcessSupervisor

    effective_state_dir = state_dir or os.path.dirname(control.path)
    role = journal.events.producer.role
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
        journal,
        owner_id,
    )

    def reconcile(trigger: RecoveryTrigger) -> RecoveryReport:
        report = coordinator.reconcile(trigger)
        control.flush_bound_outbox()
        return report

    if reconciler is not None:
        reconciler.bind(
            _SweepReservationReconciler(control, owner_id, reconcile)
        )

    return _RecoveryHooks(reconcile)


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
    retire_projection_feed: Callable[[], object],
) -> Callable[[], object]:
    """Persist remaining ownership and flush effects before forced teardown."""

    def finalize() -> object:
        control.record_owned_cancellation_intents(
            owner_id, "shutdown_deadline_spend_unknown"
        )
        drained = effects.drain_pending_commands()
        retire_projection_feed()
        control.flush_bound_outbox()
        return drained

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
        control.flush_bound_outbox()
        return written

    return terminalize


def _reported_world(
    observe: Callable,
    control: SQLiteControlStore,
    cfg: HarnessConfig,
    role: str,
    log: Callable[[str], None],
) -> Callable:
    deployment = _deployment_identity(cfg, role)
    return WorldReporter(control, deployment, log).wrap(observe)


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


class DispatchPathReservations:
    """Atomic issue/path leases shared across threads and deployments."""

    def __init__(
        self,
        control: SQLiteControlStore | None = None,
        *,
        repository: str = "",
        role: str = "",
        owner_id: str = "",
    ) -> None:
        self._lock = threading.Lock()
        self._paths: dict[int, frozenset[str]] = {}
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

    def _conflicts(self, issue: int, paths: frozenset[str]) -> bool:
        return any(
            other_issue != issue
            and (
                not paths
                or not other_paths
                or bool(paths & other_paths)
            )
            for other_issue, other_paths in self._paths.items()
        )

    def reserve(
        self, action: RunAgent, issue: int, paths: frozenset[str]
    ) -> bool:
        with self._lock:
            if issue in self._paths or self._conflicts(issue, paths):
                return False
            if self._control is not None and not self._control.claim_dispatch(
                self._repository,
                self._role,
                issue,
                action.kind.name,
                self._owner_id,
                paths,
            ):
                return False
            self._paths[issue] = paths
            self._actions[action] = issue
            return True

    def expand(self, issue: int, paths: frozenset[str]) -> bool:
        """Atomically add router predictions; one colliding router loses."""
        with self._lock:
            expanded = self._paths.get(issue, frozenset()) | paths
            if self._conflicts(issue, expanded):
                return False
            if (
                self._control is not None
                and not self._control.expand_dispatch_claim(
                    self._repository,
                    self._role,
                    issue,
                    self._owner_id,
                    paths,
                )
            ):
                return False
            self._paths[issue] = expanded
            return True

    def finish(self, action: RunAgent) -> None:
        with self._lock:
            issue = self._actions.pop(action, None)
            if issue is not None:
                self._paths.pop(issue, None)
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

    def snapshot(self) -> tuple[InFlightFix, ...]:
        with self._lock:
            return tuple(
                InFlightFix(issue, paths, exclusive=not paths)
                for issue, paths in self._paths.items()
            )


def _log(name: str):
    import sys

    def log(msg: str) -> None:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()  # unbuffered: a crash right after must not eat the line

    return log


def _onoff(enabled: bool) -> str:
    return "on" if enabled else "off"


def fix_capability_line(*, auto_merge: bool, auto_rebase: bool,
                        escalate_on_evidence: bool) -> str:
    """One boot line saying what the fixer can DO in this configuration."""
    return (
        "fix: capabilities: resolve=on "
        f"auto_merge={_onoff(auto_merge)} auto_rebase={_onoff(auto_rebase)} "
        f"escalate_on_evidence={_onoff(escalate_on_evidence)} "
    )


def review_capability_line(*, can_approve: bool, adopt_unlabeled: bool) -> str:
    """One boot line for the reviewer. `approve=off` is solo identity mode: the
    reviewer still reviews and comments, but the verdict never becomes a GitHub
    approval, so nothing it does can merge anything."""
    return (
        "review: capabilities: review=on "
        f"approve={_onoff(can_approve)} adopt_unlabeled={_onoff(adopt_unlabeled)}"
    )


def _journal(store: Store) -> LegacyJournalLocator:
    """Locate historical JSONL while live telemetry writes only schema v2."""

    return LegacyJournalLocator(os.path.join(store.state_dir, "journal.jsonl"))


def _typed_journal(
    legacy: LegacyJournalLocator,
    cfg: HarnessConfig,
    role: str,
    state_dir: str,
) -> TypedJournal:
    """Attach the canonical v2 sink without changing the historical test seam."""

    deployment = _deployment_identity(cfg, role)
    events = EventLog(
        os.path.join(state_dir, "events-v2.jsonl"),
        Producer(
            deployment,
            cfg.repo,
            role,
            os.getpid(),
            uuid.uuid4().hex,
        ),
    )
    return TypedJournal(legacy.path, events)


def _wake_check(cfg: HarnessConfig, store: Store):
    """The role's push wake, or None when WEBHOOK_WAKE is off (poll only).

    Watched beside the role's own state, like the journal: the forwarder touches
    one wake file per role so a delivery wakes all three independently.

    Interaction with the world probe: a webhook-shortened tick lands inside the
    probe's TTL, and check_suite/status deliveries move NO issue/PR updated_at —
    change_signature reads unchanged and the tick observes nothing. That is fine
    for review (it does not read checks). For the FIXER the load-bearing
    invariant is now `WorldProbe.read_signals`: since #163 the fixer DOES gate
    with an ai-pr-open PR open, and a CI-completion wake turns into a build only
    because that hook re-reads the head's CI directly and `fix_probe_safe`
    refuses to gate on a changed reading. Remove or weaken that reading and
    CI-completion wakes silently become no-ops until the TTL expires."""
    if not cfg.webhook_wake:
        return None
    return WakeSignal(wake_path(store.state_dir)).pending


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
    need the git-capable sandbox plus a worktree-installed governor, while a
    review is read-only. Keeping every transport, rollout-pricing, and budget
    option here prevents a new Codex accounting control reaching one paid role
    while silently skipping the other.

    `leg_role` states which side of that boundary the leg is on, in the leg's own
    process environment. The governor needs it because read-only-ness is declared
    by a generated hook script at the reviewer's cwd, and a fixer worktree nested
    under that cwd resolves to the same script (#113). The dispatching runner is
    the only place that knows the role for certain, so it says so here.
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
        env={"ISSUE_AGENT_LEG_ROLE": leg_role},
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
    from ..adapters.codex_hook import HookPreparingRuntime, write_hook_config
    from ..adapters.codex_runner import (
        codex_budget_not_enforced_warning,
        codex_cost_blindspot_warning,
    )

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
    blindspot = codex_cost_blindspot_warning(
        cost_per_mtok_in=runtime_config.cost_per_mtok_in,
        cost_per_mtok_out=runtime_config.cost_per_mtok_out,
        issue_spend_cap_usd=cfg.issue_spend_cap_usd,
        daily_spend_cap_usd=cfg.daily_spend_cap_usd,
        max_runtime=runtime_config.max_runtime,
        unpriced_cached_models=runtime_config.unpriced_cached_models,
    )
    if blindspot is not None:
        log(blindspot)
    budget_notice = codex_budget_not_enforced_warning(
        fix_budget_usd=cfg.fix_budget_usd,
        max_runtime=runtime_config.max_runtime,
        enforced=runtime_config.enforce_budget,
        priced=runtime_config.budget_priceable,
        pricing_gap=runtime_config.budget_pricing_gap,
    )
    if budget_notice is not None:
        log(budget_notice)
    prepare_cwd = partial(
        write_hook_config,
        agents_md=cfg.agent_md,
        base_branch=cfg.base_branch,
        repository=cfg.repo,
        edit_check_cmd=cfg.edit_check_cmd,
        log=log,
        state_dir=cfg.state_dir,
        auth_file=runner.oauth_auth_file(),
    )
    return _FixRuntimeBinding(
        HookPreparingRuntime(runner, prepare_cwd),
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
    from ..adapters.codex_hook import HookPreparingRuntime, write_hook_config
    from ..adapters.codex_runner import (
        codex_budget_not_enforced_warning,
        codex_cost_blindspot_warning,
    )

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
    blindspot = codex_cost_blindspot_warning(
        cost_per_mtok_in=cfg.codex_runtime.cost_per_mtok_in,
        cost_per_mtok_out=cfg.codex_runtime.cost_per_mtok_out,
        issue_spend_cap_usd=cfg.issue_spend_cap_usd,
        daily_spend_cap_usd=cfg.daily_spend_cap_usd,
        max_runtime=cfg.codex_runtime.max_runtime,
        runtime_name="REVIEWER_RUNTIME",
        unpriced_cached_models=cfg.codex_runtime.unpriced_cached_models,
    )
    if blindspot is not None:
        log(blindspot)
    budget_notice = codex_budget_not_enforced_warning(
        fix_budget_usd=runtime_config.budget_usd,
        max_runtime=cfg.codex_runtime.max_runtime,
        enforced=cfg.codex_runtime.enforce_budget,
        priced=cfg.codex_runtime.budget_priceable,
        pricing_gap=cfg.codex_runtime.budget_pricing_gap,
        budget_name="REVIEW_BUDGET_USD",
        runtime_name="REVIEWER_RUNTIME",
    )
    if budget_notice is not None:
        log(budget_notice)
    execution_cwd = cfg.target_dir
    if not execution_cwd:
        log(
            "codex: NOT READY at startup — REVIEWER_RUNTIME=codex requires "
            "TARGET_DIR so its read-only governor has an explicit checkout"
        )
        return _ReviewRuntimeBinding(
            runner,
            runtime_preflight=lambda: "REVIEWER_RUNTIME=codex requires TARGET_DIR",
        )
    prepare_cwd = partial(
        write_hook_config,
        review_only=True,
        base_branch=cfg.base_branch,
        repository=cfg.repo,
        log=log,
        state_dir=cfg.review_state_dir,
        auth_file=runner.oauth_auth_file(),
    )
    return _ReviewRuntimeBinding(
        HookPreparingRuntime(runner, prepare_cwd),
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


def _inject_predicted_paths(store: Store, world):
    """Attach persisted router predictions to this tick's GitHub issue views.

    GitHub is the source of truth for labels and PR diffs, while predictions are
    local, advisory routing state. Joining them here keeps the domain decision
    pure and makes any unreadable state indistinguishable from no prediction.
    """
    if not isinstance(store, PredictionReader):
        return world
    issues = []
    changed = False
    for issue in world.issues:
        try:
            predicted = store.get_predicted_paths(issue.number)
        except Exception:  # noqa: BLE001 — a hint must never stop a fix tick
            predicted = frozenset()
        combined = issue.predicted_paths | frozenset(predicted or ())
        changed = changed or combined != issue.predicted_paths
        issues.append(replace(issue, predicted_paths=combined))
    return replace(world, issues=tuple(issues)) if changed else world


def _prediction_outcome_reporter(
    journal: EventSink | None, repo: str, store: Store, log, control=None
):
    """Journal each terminal PR's predicted-versus-actual files exactly once.

    This runs before cleanup clears the per-issue prediction state. The event is
    intentionally raw sets plus their intersection: operators can calculate hit
    rate and inspect false positives without another GitHub read.
    """
    seen: set[str] = set()

    def report(world) -> None:
        if not isinstance(store, PredictionReader):
            return
        for pr in world.prs:
            if pr.state not in ("MERGED", "CLOSED") or pr.closing_issue is None:
                continue
            # `changed_files` UNREAD is not "no files changed": the terminal-PR
            # fast path (#163) builds a view without them, and this claim is
            # durable and deduped, so a false 0/N would permanently corrupt the
            # predicted-path hit rate (adversarial review F4).
            if not pr.content_known:
                continue
            try:
                predicted = frozenset(
                    store.get_predicted_paths(pr.closing_issue) or ()
                )
            except Exception:  # noqa: BLE001 — observability is fail-open
                continue
            if not predicted:
                continue
            key = f"path-prediction:{repo}:{pr.closing_issue}:{pr.number}"
            if key in seen:
                continue
            if control is not None and not control.claim("path-prediction", repo, key):
                continue
            seen.add(key)
            matched = predicted & pr.changed_files
            log(
                f"PATH PREDICTION: issue #{pr.closing_issue} / PR #{pr.number} — "
                f"{len(matched)}/{len(predicted)} predicted files changed"
            )
            if journal is not None:
                journal.emit(
                    TelemetryEvent(
                        PathPrediction(
                            tuple(sorted(predicted)),
                            tuple(sorted(pr.changed_files)),
                            tuple(sorted(matched)),
                            key,
                        ),
                        Correlation(issue=pr.closing_issue, pr=pr.number),
                    )
                )

    return report


def _control_value(errors, key, reader, default, *, error_key=None):
    try:
        return reader(key)
    except WorkflowControlError as exc:
        errors[key if error_key is None else error_key] = str(exc)
        return default


def _fix_state(
    store: Store,
    world,
    inflight: tuple[InFlightFix, ...] = (),
    *,
    workflow_control: WorkflowControl | None = None,
    available_runtimes: frozenset[str] = frozenset(),
) -> FixState:
    """Read the per-issue/PR orchestration state the fix decision needs."""
    errors: dict[int, str] = {}

    def read(key, reader, default, *, error_key=None):
        return _control_value(errors, key, reader, default, error_key=error_key)

    available_runtimes = available_runtimes or frozenset({store.runtime})
    pr_issues = {breaker_key(p) for p in world.prs if breaker_key(p) > 0}
    sessions = frozenset()
    resumed = {}
    pr_resumed = {}
    pr_reply_errors = {}
    # Retry counters are keyed by breaker_key(pr): the linked issue number, or the
    # NEGATED PR number when a PR has no resolvable issue (Jira-keyed branch, no
    # closing-ref). Must match the workflow's increment key exactly, else the
    # breaker reads 0 forever and the retry loop never escalates (was PR #112).
    keys = {breaker_key(p) for p in world.prs}
    ci_retries: dict[int, int] = {}
    ci_resets: dict[int, cc.CIEpisodeReset] = {}
    # Full (review_at, count) tuple: the domain compares the stored timestamp
    # against the CURRENT review's submitted_at so a superseding review reads
    # as count 0 (see FixState.review_retries).
    review_retries = {}
    review_errors = {}
    rebase_retries = {}
    resolve_workflows: dict[int, WorkflowRead] = {}
    if workflow_control is not None:
        observed_at = parse_timestamp(world.now)
        for issue in world.issues:
            key = WorkflowKey(workflow_control.writer.repository, IssueRef(issue.number))
            try:
                workflow = workflow_control.load_workflow(key)
                if isinstance(workflow, WorkflowAbsent):
                    if observed_at is None:
                        workflow = WorkflowUnavailable(key, "workflow discovery requires a timestamped issue observation")
                    else:
                        workflow = workflow_control.ensure_observed_workflow(
                            ObservedIssue(key, issue, observed_at)
                        ).snapshot
                resolve_workflows[issue.number] = workflow
            except (ValueError, sqlite3.Error) as exc:
                resolve_workflows[issue.number] = WorkflowUnavailable(key, str(exc))
        # A PR may remain observable when its issue fetch failed. Existing SQL
        # handoff evidence can be read, but absence cannot initialize an unseen
        # issue or authorize an issue-side effect.
        for number in pr_issues - resolve_workflows.keys():
            key = WorkflowKey(workflow_control.writer.repository, IssueRef(number))
            try:
                resolve_workflows[number] = workflow_control.load_workflow(key)
            except (ValueError, sqlite3.Error) as exc:
                resolve_workflows[number] = WorkflowUnavailable(key, str(exc))
    sessions = sessions | frozenset(
        number for number in pr_issues
        if isinstance(workflow := resolve_workflows.get(number), WorkflowSnapshot)
        and workflow.episode.continuation.session is not None
        and workflow.episode.continuation.session.runtime in available_runtimes
    )
    for pr in world.prs:
        number = breaker_key(pr)
        try:
            repair = workflow_control.read_pr_repair(WorkflowKey(workflow_control.writer.repository, PullRequestRef(pr.number))) if workflow_control is not None else None
            if not isinstance(repair, cc.SelectedPRRepair):
                raise ValueError("PR repair continuity is inactive or migration blocked")
            pr_resumed[pr.number] = repair.component.state.consumed_reply_at
            current_session = repair.component.state.session
            if current_session is not None and current_session.runtime.value in available_runtimes and repair.component.state.branch == pr.head_ref_name:
                sessions = sessions | {number}
        except (ValueError, sqlite3.Error) as exc:
            pr_reply_errors[pr.number] = str(exc)
        try:
            if workflow_control is None or number <= 0:
                raise ValueError("CI issue recovery authority is unavailable")
            selected = workflow_control.read_issue_recovery(WorkflowKey(workflow_control.writer.repository, IssueRef(number)))
            if not isinstance(selected, cc.SelectedIssueRecovery):
                raise ValueError("CI issue recovery authority is inactive or migration blocked")
            ci_retries[number] = selected.component.state.ci_failures
            rebase_retries[number] = selected.component.state.rebase_failures
            review_debt = selected.component.state.review_failures
            if review_debt.count and not review_debt.review_at:
                review_errors[number] = "review retry debt has no verified qualifying timestamp"
            elif review_debt.pr is not None and review_debt.pr != PullRequestRef(pr.number):
                review_errors[number] = "review retry debt belongs to another linked PR"
            else:
                review_retries[number] = (review_debt.review_at, review_debt.count)
            if selected.component.state.ci_failures:
                repair = workflow_control.read_pr_repair(WorkflowKey(workflow_control.writer.repository, PullRequestRef(pr.number)))
                if isinstance(repair, cc.SelectedPRRepair) and pr.head_ref_oid:
                    ci_resets[pr.number] = cc.CIEpisodeReset(selected.binding, selected.revision, repair.revision.fence, pr.head_ref_oid)
        except (ValueError, sqlite3.Error) as exc:
            errors[number] = str(exc)
    leg_ends: dict[int, tuple] = {}
    leg_history_errors: dict[int, str] = {}
    for number in {issue.number for issue in world.issues} | pr_issues:
        try:
            if workflow_control is None:
                raise ValueError("fix leg history is unavailable")
            selected_issue = workflow_control.read_issue_recovery(WorkflowKey(workflow_control.writer.repository, IssueRef(number)))
            if not isinstance(selected_issue, cc.SelectedIssueRecovery):
                raise ValueError("issue recovery authority is inactive or migration blocked")
            leg_ends[number] = workflow_control.fix_leg_ends(number)
        except (ValueError, sqlite3.Error, ControlStoreError) as exc:
            # Holds THIS issue only (fail closed); the tick logs it as
            # "fix leg history <n> held".
            leg_history_errors[number] = str(exc)
    # #38: the head SHA each PR's ONE free CI re-run was spent on. PR-keyed (not
    # breaker-keyed) because a re-run is an act on a PR's check runs, and a PR
    # with no resolvable issue has check runs like any other.
    ci_rerun_head = {}
    ci_cancel_head = {}
    ci_remediations = {}
    ci_remediation_errors = {}
    pending_ci_escalations = set()
    cleanup_sources = {}
    for pr in world.prs:
        try:
            if workflow_control is None:
                raise ValueError("CI remediation PR repair authority is unavailable")
            selected = workflow_control.read_pr_repair(WorkflowKey(workflow_control.writer.repository, PullRequestRef(pr.number)))
            if not isinstance(selected, cc.SelectedPRRepair):
                raise ValueError("CI remediation PR repair authority is inactive or migration blocked")
            if pr.state in {"CLOSED", "MERGED"}:
                cleanup_sources[pr.number] = cc.PRRepairCleanup(selected.binding, selected.revision, pr.head_ref_oid, pr.state == "MERGED")
            rerun, cancel = selected.component.state.ci_rerun, selected.component.state.ci_cancel
            ci_remediations[pr.number] = (rerun, cancel)
            ci_rerun_head[pr.number] = ci_remediation_marker(rerun)
            ci_cancel_head[pr.number] = ci_remediation_marker(cancel)
            escalation = selected.component.state.pending_ci_escalation
            if isinstance(escalation, cc.SettledPRRecoveryEffect) and escalation.receipt.status is EffectStatus.SUCCEEDED:
                escalation_capture = escalation.owed.capture
                pending_ci_escalations.add((pr.number, escalation_capture.decision_head
                    if isinstance(escalation_capture, cc.PendingCIEscalationCapture) else escalation_capture.head))
        except (ValueError, sqlite3.Error) as exc:
            ci_remediation_errors[pr.number] = str(exc)
    ci_pending_escalated = frozenset(pending_ci_escalations)
    return FixState(
        control_errors=errors,
        sessions=sessions, resumed_at=resumed, pr_resumed_at=pr_resumed,
        pr_reply_control_errors=pr_reply_errors,
        ci_retries=ci_retries, ci_resets=ci_resets, review_retries=review_retries,
        review_control_errors=review_errors,
        rebase_retries=rebase_retries, resolve_workflows=resolve_workflows,
        leg_ends=leg_ends, leg_history_errors=leg_history_errors, ci_rerun_head=ci_rerun_head,
        ci_cancel_head=ci_cancel_head,
        ci_remediations=ci_remediations, ci_remediation_errors=ci_remediation_errors,
        ci_pending_escalated=ci_pending_escalated,
        cleanup_sources=cleanup_sources,
        inflight=inflight,
    )


def _hold_reporter(
    journal: EventSink | None,
    repo: str,
    log,
    issue_is_blocked: Callable[[int], bool] | None = None,
    control: LevelState | None = None,
):
    """Report defer begin/clear transitions without steady-state tick noise.

    `pick_ready_issue` skips an ai-ready issue whose files overlap an in-flight
    PR, or that has an open GitHub dependency — silently, which is
    indistinguishable from a fixer that is simply broken (harness #6: a hold on
    #522 cost ~30 minutes of live investigation before anyone could tell which
    it was).

    The schema-v2 world projection is the durable source for dashboard levels.
    This reporter keeps only process-local edge memory; telemetry is never read
    to reconstruct orchestration state.
    """
    active: dict[tuple[int, str, int | None, int | None], dict] | None = None

    def key_for(hold) -> tuple[int, str, int | None, int | None]:
        return (hold.issue, hold.reason, hold.blocker_pr, hold.blocker_issue)

    def fields_for(key):
        issue, reason, blocker_pr, blocker_issue = key
        if reason == "human":
            return {
                "issue": issue,
                "reason": reason,
                "blocker": "human",
            }
        return {
            "issue": issue,
            "reason": reason,
            "blocker_pr": blocker_pr,
            "blocker_issue": blocker_issue,
        }

    def stored_key(key) -> str:
        issue, reason, blocker_pr, blocker_issue = key
        return f"{issue}:{reason}:{blocker_pr or ''}:{blocker_issue or ''}"

    def key_from_fields(fields) -> tuple[int, str, int | None, int | None]:
        return (
            int(fields["issue"]),
            str(fields["reason"]),
            fields.get("blocker_pr"),
            fields.get("blocker_issue"),
        )

    def waiting_label(key) -> str:
        issue, reason, blocker_pr, blocker_issue = key
        if reason == "human":
            return f"issue #{issue} waiting on human input"
        if blocker_pr is not None:
            return f"issue #{issue} waiting on PR #{blocker_pr}"
        if blocker_issue is not None:
            if reason == "blocker":
                return f"issue #{issue} blocked by #{blocker_issue}"
            return f"issue #{issue} waiting on #{blocker_issue}"
        return f"issue #{issue} waiting on an open dependency"

    def report(world, inflight=()) -> dict[int, str]:
        nonlocal active
        if active is None:
            if control is not None:
                active = {
                    key_from_fields(fields): dict(fields)
                    for fields in control.load_level_state("holds", repo).values()
                }
            else:
                active = {}

        current = {
            key_for(hold): hold for hold in issue_holds(world, tuple(inflight))
        }
        current.update({
            (issue.number, "human", None, None): issue
            for issue in world.open_issues()
            if issue.has(Label.AI_BLOCKED)
        })
        observed_issue_numbers = {issue.number for issue in world.issues}
        for key, event in active.items():
            if key[1] != "human" or key[0] in observed_issue_numbers:
                continue
            if issue_is_blocked is None:
                current[key] = event
                continue
            try:
                still_blocked = issue_is_blocked(key[0])
            except Exception as exc:  # noqa: BLE001 - unread is not an unblock
                log(f"issue #{key[0]} human-wait probe failed: {exc}")
                current[key] = event
            else:
                if still_blocked:
                    current[key] = event
        for key in current.keys() - active.keys():
            message = waiting_label(key)
            log(f"DEFERRED: {message}; dispatches once that clears")
            if journal is not None:
                fields = fields_for(key)
                journal.emit(
                    TelemetryEvent(
                        LevelChanged(
                            f"hold:{stored_key(key)}",
                            "waiting",
                            str(fields["reason"]),
                            fields,
                        ),
                        Correlation(issue=key[0]),
                    )
                )
            active[key] = fields_for(key)

        for key in active.keys() - current.keys():
            message = waiting_label(key)
            log(
                f"{message.replace(' waiting on ', ' no longer waiting on ')}; "
                "eligible for dispatch"
            )
            if journal is not None:
                fields = fields_for(key)
                journal.emit(
                    TelemetryEvent(
                        LevelChanged(
                            f"hold:{stored_key(key)}",
                            "cleared",
                            str(fields["reason"]),
                            fields,
                        ),
                        Correlation(issue=key[0]),
                    )
                )
            active.pop(key, None)
        if control is not None:
            control.replace_level_state(
                "holds",
                repo,
                {stored_key(key): fields for key, fields in active.items()},
            )
        reasons: dict[int, str] = {}
        for key in current:
            reason = waiting_label(key).removeprefix(f"issue #{key[0]} ")
            reasons[key[0]] = "; ".join(filter(None, (reasons.get(key[0]), reason)))
        return reasons

    return report


def _ci_stall_reporter(
    journal: EventSink | None, repo: str, fix_cfg: FixConfig, log, control=None
):
    """Build the per-poll reporter for a CI pending-deadline breach (#30).

    The escalation itself is `decide_fix`'s (a comment + needs-human-review);
    this is the machine-readable half. Without it a wedged check leaves NO
    trace: a pending PR dispatches no leg, so no retry counter moves and no
    `agent_run` is written, and "the PR simply never moves and nothing anywhere
    says why" was the whole complaint.

    Same two-channel shape as `_hold_reporter`, and reads the SAME pure
    derivation `decide_fix` escalates on, so the journal cannot disagree with
    the action. The LOG line fires every poll it is derivable; the JOURNAL line
    fires once per (PR, head SHA) — a re-push that wedges again is a new stall,
    the same stuck run is not. In practice the escalation relabel takes the PR
    out of the fixer's set on the first breach, so this dedup is the backstop
    for the poll where that relabel fails, and for the daemon restarts a hot
    reload produces.
    """
    seen: set[str] = set()

    def report(world, state: FixState) -> None:
        for remedy in ci_remediations(world, fix_cfg, state):
            stall = remedy.stall
            if stall.checks or stall.seconds:
                checks = ", ".join(stall.checks) or "unnamed"
                log(
                    f"CI STALL: PR #{stall.pr} has been pending {stall.seconds}s "
                    f"({checks}) \u2014 pending deadline breached; "
                    f"{remedy.step.value}: {remedy.detail}"
                )
            else:
                # The cancel-aftermath arm carries no pending clock and no
                # pinning checks; reporting it as "pending 0s" would describe a
                # state that never existed.
                log(
                    f"CI CANCEL AFTERMATH: PR #{stall.pr} ({stall.head[:7]}) "
                    f"reads green after a cancel \u2014 {remedy.step.value}"
                )
            # The STEP is part of the key: cancel, the wait it opens and the
            # escalation that bounds it are three distinct facts about one head,
            # and ADR 0012 wants the wait journalled \u2014 once, not every poll.
            key = f"ci-pending:{repo}:{stall.pr}:{stall.head}:{remedy.step.value}"
            if key in seen:
                continue
            if control is not None and not control.claim("ci-pending", repo, key):
                continue
            seen.add(key)
            if journal is None:
                continue
            correlation = Correlation(issue=remedy.issue or None, pr=stall.pr)
            if remedy.step is not CiStallStep.ESCALATE:
                # `ci_pending_deadline` counts ESCALATIONS — escalation rate is
                # a named power guardrail, and a rate counted from duplicated
                # events is not a rate. A wedge the harness heals itself must
                # therefore NOT emit one, or the change would appear to worsen
                # the very guardrail it protects (adversarial review S4). The
                # self-healing steps get their own code instead.
                journal.emit(
                    TelemetryEvent(
                        TelemetryWarning(
                            "ci_stall_step",
                            f"stalled CI: {remedy.step.value}",
                            {
                                "head": stall.head,
                                "seconds": stall.seconds,
                                "step": remedy.step.value,
                                "detail": remedy.detail,
                                "run_ids": list(remedy.run_ids),
                                "block_key": key,
                            },
                        ),
                        correlation,
                    )
                )
                continue
            journal.emit(
                TelemetryEvent(
                    CiPendingDeadline(
                        stall.head,
                        stall.seconds,
                        tuple(stall.checks),
                        key,
                    ),
                    correlation,
                )
            )

    return report


def _duplicate_pr_reporter(
    journal: EventSink | None, repo: str, fix_cfg: FixConfig, log, control=None
):
    """Build the per-poll reporter for the duplicate-PR guard (#39).

    The refusal itself is `decide_fix`'s (a comment + ai-ready → ai-blocked);
    this is the machine-readable half, and it reads the SAME pure derivation the
    decision does, so the journal cannot disagree with the action.

    The metric #39 moves is "wasted duplicate resolves" — one in five issues on
    the night it was found — and these events ARE the count of resolves not
    bought. Two channels, as everywhere else here: the LOG line every poll the
    condition holds (normally exactly one, since the relabel clears it), the
    JOURNAL line once per (issue, PR).
    """
    seen: set[str] = set()

    def report(world) -> None:
        for dup in duplicate_pr_guards(world, fix_cfg):
            log(
                f"DUPLICATE PR: issue #{dup.issue} is ai-ready but open PR "
                f"#{dup.pr} already closes it — skipping the resolve and "
                "moving the issue to ai-blocked"
            )
            key = f"duplicate-pr:{repo}:{dup.issue}:{dup.pr}"
            if key in seen:
                continue
            if control is not None and not control.claim("duplicate-pr", repo, key):
                continue
            seen.add(key)
            if journal is None:
                continue
            journal.emit(
                TelemetryEvent(
                    DuplicatePrPrevented(dup.author, key),
                    Correlation(issue=dup.issue, pr=dup.pr),
                )
            )

    return report


def _stall_reporter(journal: EventSink | None, store: Store, log):
    """No silent forever: any watched target whose observable signature has
    not changed for a whole stall threshold emits an escalating
    `target_stalled` TelemetryWarning — regardless of WHY it is stuck.

    2026-08-01's run produced three unrelated stall shapes (CI
    merge deadlock, receipt-dead-ended visual resolve, uncounted review
    deferrals); each per-path fix is necessary, but this watchdog is the
    boundary measurement that catches the classes nobody has met yet.
    Telemetry only — it never mutates lifecycle, so a false positive costs a
    journal line, never a blocked leg. One emit per (target, level) per
    process; a restart re-announces the current level once, which is the
    correct behaviour for an operator attaching to a fresh log."""
    from ..domain.liveness import detect_stalls

    emitted: set[tuple[str, int]] = set()

    def report(world, holds: Mapping[int, str] | None = None) -> None:
        seen: dict[str, tuple[str, str]] = {}
        for pr in world.prs:
            if pr.state == "OPEN":
                sig, since = store.get_stall_marker("pr", pr.number)
                if sig:
                    seen[f"pr:{pr.number}"] = (sig, since)
        for issue in world.issues:
            if issue.state == "open":
                sig, since = store.get_stall_marker("issue", issue.number)
                if sig:
                    seen[f"issue:{issue.number}"] = (sig, since)
        stalls, next_seen = detect_stalls(world, seen)
        for key, marker in next_seen.items():
            if seen.get(key) != marker:
                kind, _, num = key.partition(":")
                store.set_stall_marker(kind, int(num), marker[0], marker[1])
        for stall in stalls:
            dedupe = (f"{stall.kind}:{stall.number}", stall.level)
            if dedupe in emitted:
                continue
            emitted.add(dedupe)
            minutes = int(stall.age_seconds // 60)
            reason = holds.get(stall.number) if stall.kind == "issue" and isinstance(holds, Mapping) else None
            suffix = (
                f"deferred: {reason}" if isinstance(reason, str) and reason.strip()
                else "nothing is moving it"
            )
            log(
                f"STALL: {stall.kind} #{stall.number} unchanged for "
                f"{minutes} min (level {stall.level}) — {suffix}"
            )
            if journal is None:
                continue
            try:
                journal.emit(
                    TelemetryEvent(
                        TelemetryWarning(
                            "target_stalled",
                            f"{stall.kind} #{stall.number} unchanged for "
                            f"{minutes} min",
                            {
                                "kind": stall.kind,
                                "level": stall.level,
                                "age_seconds": int(stall.age_seconds),
                            },
                        ),
                        Correlation(
                            pr=stall.number if stall.kind == "pr" else None,
                            issue=(
                                stall.number
                                if stall.kind == "issue"
                                else None
                            ),
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — telemetry must not block the tick
                log(f"stall for {stall.kind} #{stall.number} not journalled ({exc})")

    return report


def _ci_flake_reporter(
    journal: EventSink | None, repo: str, fix_cfg: FixConfig, log, control=None
):
    """Build the per-poll reporter for a CONFIRMED flake (#38).

    `ci_rerun` (written by the Executor) says a free re-run fired; this says it
    WORKED — the checks that would have bought a $13–29 FIX_CI leg went green on
    a re-run of the identical commit. That is only observable one CI cycle after
    the fact, from world state, which is why it is a reporter over
    `domain.fix.ci_flakes` rather than an event some code path emits inline.

    Counting these against `ci_rerun` gives the flake rate the issue's metric
    needs (repair-leg share of spend), so the JOURNAL line is deduped per
    (PR, head) — the derivation stays true for every poll until the PR merges.
    """
    seen: set[str] = set()

    def report(world, state) -> None:
        for flake in ci_flakes(world, fix_cfg, state):
            log(
                f"CI FLAKE: PR #{flake.pr} went green on a re-run of the same "
                f"commit ({flake.head[:7]}) — no CI-fix leg was dispatched"
            )
            key = f"ci-flake:{repo}:{flake.pr}:{flake.head}"
            if key in seen:
                continue
            if control is not None and not control.claim("ci-flake", repo, key):
                continue
            seen.add(key)
            if journal is None:
                continue
            journal.emit(
                TelemetryEvent(
                    CiFlakeDetected(flake.head, key),
                    Correlation(pr=flake.pr),
                )
            )

    return report


def _recurrence_reporter(
    journal: EventSink | None, gateway, repo: str, fix_bot: str, log, control=None
):
    """Build the per-poll reporter for LITERAL reopens (harness #10, kind=reopen).

    A merged PR is not ground truth that the problem is fixed. The truest
    "losing power" signal is a resolved issue that comes back — and it is
    invisible to review, which is why it gets recorded here rather than inferred
    from the existing counters.

    **Why `stateReason`, not "open again".** The harness merges to a non-default
    base branch, so GitHub never auto-closes the referenced issue; the harness
    closes it itself, and only when `close_issue_on_merge` is set. An ai-done
    issue that is open therefore means either "a human reopened it" or "the close
    has not happened / is not configured" — a distinction worth one gh call,
    because the second case is every ai-done issue on a default deployment and
    would journal a recurrence for each of them, forever.

    Same two-channel shape as `_hold_reporter`: the LOG line fires every pass
    (free, and answers "why is this labelled recurrence?" from any single poll),
    the JOURNAL line once per issue — a reopen is a LEVEL that persists until
    someone closes the issue again, and the recurrence RATE is computed by
    counting these events, so duplicates would corrupt the metric rather than
    merely add noise.

    Cost: ZERO extra gh calls in the steady state. `issue_state_reason` is asked
    only about issues that are BOTH open and ai-done — a set that is empty on a
    healthy repo. A gateway without the method (a fake, a legacy adapter) yields
    an inert reporter rather than an AttributeError out of the decide closure.
    """
    if not isinstance(gateway, IssueStateReasonReads):
        return _ignore_world
    seen: set[str] = set()

    def report(world) -> None:
        reopened = [
            i.number for i in world.issues
            if i.state == "open" and i.has(Label.AI_DONE)
            and gateway.issue_state_reason(i.number) == "reopened"
        ]
        if not reopened:
            return
        # Only now is the merged list worth a call: it exists solely to name the
        # PR that shipped the thing that came back.
        shipped: dict[int, int] = {}
        if isinstance(gateway, RecentMergesReads) and fix_bot:
            try:
                shipped = {
                    m.issue: m.pr
                    for m in (gateway.recently_merged_issues(fix_bot) or ())
                }
            except Exception as exc:  # noqa: BLE001 — enrichment, never fatal
                log(f"recurrence: could not read merged PRs ({exc})")
        for number in reopened:
            log(
                f"RECURRENCE: issue #{number} was resolved and is open again "
                "(reopened)"
            )
            key = f"recurrence:{repo}:{number}:reopen"
            if key in seen:
                continue
            if control is not None and not control.claim("recurrence", repo, key):
                continue
            seen.add(key)
            if journal is None:
                continue
            # of_issue == issue: a reopen is an issue recurring as ITSELF, which
            # keeps one event shape for both kinds so the metric reads them
            # together. No label and no comment here — the reopened issue is
            # already the visible artifact, and the harness must never write to
            # an issue a human just deliberately reopened.
            journal.emit(
                TelemetryEvent(
                    RecurrenceDetected("reopen", number, key),
                    Correlation(
                        issue=number,
                        pr=(shipped.get(number) or None),
                    ),
                )
            )

    return report


@dataclass(frozen=True)
class FixDaemonComposition:
    """The fix role's composed authorities.

    Exposed so acceptance tests drive the exact production wiring (drain and
    recovery bindings, spend gate, worktree removal, outbox flush) instead of a
    hand-copied replica that can drift from it.
    """

    daemon: Daemon
    control: SQLiteControlStore
    journal: TypedJournal
    executor: Executor
    effects: EffectRecovery
    dispatcher: AgentDispatcher
    workflow_control: SQLiteWorkflowControl
    rearm: ResolveRearmWorkflow
    recovery: _RecoveryHooks
    owner_id: str


def build_fix_daemon(
    cfg: HarnessConfig,
    gateway: GitHubGateway,
    store: Store,
    worktrees: Worktrees,
    runner: AgentRunner,
    fixer_busy=lambda: False,
) -> Daemon:
    return compose_fix_daemon(cfg, gateway, store, worktrees, runner, fixer_busy).daemon


def compose_fix_daemon(
    cfg: HarnessConfig,
    gateway: GitHubGateway,
    store: Store,
    worktrees: Worktrees,
    runner: AgentRunner,
    fixer_busy=lambda: False,
    *,
    log: Callable[[str], None] | None = None,
) -> FixDaemonComposition:
    log = log or _log("fix")
    repo_cfg = cfg.repository
    fix_runtime = cfg.fix_runtime
    codex_runtime = cfg.codex_runtime
    fix_cfg = FixConfig(
        fix_bot=fix_runtime.bot,
        identity_mode=repo_cfg.identity_mode,
        auto_merge=cfg.auto_merge,
        merge_method=cfg.merge_method,
        ci_pending_max_seconds=cfg.ci_pending_max_seconds,
        duplicate_pr_guard=cfg.duplicate_pr_guard,
        ci_free_rerun=cfg.ci_free_rerun,
        close_issue_on_merge=cfg.close_issue_on_merge,
        max_ci_retries=fix_runtime.max_ci_retries,
        max_review_retries=fix_runtime.max_review_retries,
        auto_rebase_conflicts=fix_runtime.auto_rebase_conflicts,
        max_rebase_retries=fix_runtime.max_rebase_retries,
        max_resolve_retries=fix_runtime.max_resolve_retries,
    )
    journal = _typed_journal(_journal(store), cfg, "fix", store.state_dir)
    dcfg = DispatchConfig(
        base_branch=repo_cfg.base_branch,
        fix=FixTaskConfig(
            repo_cfg.repo, repo_cfg.base_branch, cfg.agent_md,
            fix_runtime.budget_usd, fix_runtime.model, fix_runtime.effort,
            router_budget_usd=cfg.router_budget_usd,
            # Harness-attested, probed once per daemon process (ADR 0012
            # provenance: the boot probe, never PR content). "" when the probe
            # could not run → the prompts say nothing rather than attest a
            # capability that may not hold.
            environment_manifest=boot_manifest(cfg.evidence_stack),
            runtime=cfg.fixer_runtime,
        ),
        max_review_rejections=cfg.max_review_rejections,
        author_tests_split=fix_runtime.author_tests_split,
        max_contract_rounds=fix_runtime.max_contract_rounds,
        fix_mcp_config=fix_runtime.mcp_config,
        harness_root=cfg.runtime_paths.harness_root,
        log=log,
        fix_bot=fix_runtime.bot,
        fixer_runtime=cfg.fixer_runtime,
        fix_model_routes=cfg.fix_model_routes,
        available_fix_runtimes=cfg.fix_runtimes,
        escalate_on_evidence=cfg.escalate_on_evidence,
        max_ci_retries=fix_runtime.max_ci_retries,
        max_review_retries=fix_runtime.max_review_retries,
        max_rebase_retries=fix_runtime.max_rebase_retries,
        **_spend_cfg(cfg, "fix"),
    )
    runtime_bindings = {
        provider: _FIX_RUNTIME_REGISTRY[provider](cfg, codex_runtime, runner)
        for provider in sorted(cfg.fix_runtimes, key=lambda provider: provider.value)
    }
    prepare_cwd = runtime_bindings[cfg.fixer_runtime].prepare_cwd if not cfg.fix_model_routes else None

    control = _control_store(cfg, cfg.state_dir, journal)
    dcfg = replace(dcfg, operator_guidance=review_guidance_reader(control, cfg.repo))
    owner_id = _owner_identity(cfg, "fix")
    effect_executor = _effects(
        gateway,
        control,
        repository=cfg.repo,
        identity=owner_id,
        config_version=cfg.config_version,
        fix_config=fix_cfg,
        base_branch=cfg.base_branch,
        retry_ceiling=cfg.effect_retry_ceiling,
        journal=journal,
        alert_cmd=cfg.alert_cmd,
        log=log,
    )
    workflow_control = SQLiteWorkflowControl(
        control, WorkflowWriter(cfg.repo, "fix", owner_id),
        resolve_ceiling=cfg.max_resolve_retries,
        max_contract_rounds=fix_runtime.max_contract_rounds,
    )
    def validate_cleanup_fix_state(action):
        if action.pr is not None:
            if action.source is None:
                raise ValueError("PR cleanup requires selected SQL recovery provenance")
            current = gateway.get_pr(action.pr)
            if (current.state != ("MERGED" if action.merged else "CLOSED")
                    or current.head_ref_oid != action.source.head):
                raise ValueError("PR cleanup terminal/head observation changed")
            workflow_control.validate_pr_repair_cleanup(WorkflowKey(cfg.repo, PullRequestRef(action.pr)), action.merged, action.source)

    def cleanup_fix_state(action):
        validate_cleanup_fix_state(action)
        if action.pr is not None:
            workflow_control.cleanup_pr_repair(WorkflowKey(cfg.repo, PullRequestRef(action.pr)), action.merged, action.source)
        store.cleanup_pr(action.pr, action.issue, action.merged)

    def reset_ci_episode(action: ResetCiRetries) -> None:
        source = action.source
        if source is None:
            raise ValueError("CI reset lacks its selected green observation")
        pr = gateway.get_pr(source.pr_fence.key.target.number)
        current_pr = gateway.pr_for_issue(action.issue, require_unique=True)
        if (breaker_key(pr) != action.issue or pr.head_ref_oid != source.head
                or pr.state not in {"OPEN", "MERGED"}
                or (pr.state != "MERGED" and ci_state(pr.ci) is not CIState.OK)
                or current_pr != (pr.number if pr.state == "OPEN" else None)):
            raise ValueError("CI reset no longer matches settled green PR/head linkage")
        if workflow_control.reset_ci_episode(source):
            store.clear_ci_history(action.issue)

    executor = Executor(
        gateway, remove_worktree=worktrees.remove, log=log,
        clear_issue_state=cleanup_fix_state,
        validate_cleanup=validate_cleanup_fix_state,
        reset_ci_retries=reset_ci_episode,
        journal=journal, alert_cmd=cfg.alert_cmd,
        effect_executor=effect_executor.actions,
    )
    # Wrapped LAST, so the decorator sees whichever runtime actually ran (Claude or
    # the Codex swap above) and the ledger is runtime-agnostic.
    fix_reconciler = _LateReservationReconciler()
    from ..adapters.workspace_admission import WorkspaceAdmission

    from ..adapters.runtime_selection import SelectingRuntime

    governed_runtimes = {
        provider: _with_accounting(
            binding.runner, control, cfg, "fix", provider.value, journal,
            fix_reconciler,
            admit=WorkspaceAdmission(control, worktrees, os.environ.get("GH_HOST", "github.com")),
        ) for provider, binding in runtime_bindings.items()
    }
    fix_runner = (SelectingRuntime(governed_runtimes, cfg.fixer_runtime)
                  if cfg.fix_model_routes else governed_runtimes[cfg.fixer_runtime])
    fix_runner = JournalingRuntime(
        fix_runner,
        journal,
        repo=cfg.repo,
        role="fix",
        runtime=cfg.fixer_runtime.value,
        state_dir=cfg.state_dir,
    )
    reservations = DispatchPathReservations(
        control,
        repository=cfg.repo,
        role="fix",
        owner_id=owner_id,
    )
    dispatcher = AgentDispatcher(
        dcfg, gateway, store, worktrees, fix_runner, executor,
        prepare_cwd=prepare_cwd, journal=journal,
        reserve_fix_paths=reservations.expand,
        dispatch_claim_target=reservations.issue_for,
        workflow_control=workflow_control,
    )
    from .resolve_control import compose_resolve_rearm

    rearm_workflow = compose_resolve_rearm(
        cfg, control, gateway, worktrees, store,
        now=lambda: datetime.now(timezone.utc), log=log,
        approved_host=os.environ.get("GH_HOST", "github.com"),
    )

    def reconcile_workflows() -> int:
        # Both startup/wake recovery and every command drain use these same
        # authorities, including when paid admission or world polling is held.
        return dispatcher.fix_workflow.reconcile_returns() + rearm_workflow.reconcile()

    executor.bind_workflow_reconciliation(reconcile_workflows)
    effect_executor = replace(effect_executor, reconcile_workflow=reconcile_workflows)
    dispatch_runtime = CancellableAsyncDispatch(
        dispatcher.dispatch,
        finished=reservations.finish,
    )
    executor.bind_dispatch(dispatch_runtime)

    report_holds = _hold_reporter(
        journal,
        cfg.repo,
        log,
        issue_is_blocked=lambda issue: gateway.get_issue(issue).has(
            Label.AI_BLOCKED
        ),
        control=control,
    )
    report_prediction_outcomes = _prediction_outcome_reporter(
        journal, cfg.repo, store, log, control
    )
    report_ci_stalls = _ci_stall_reporter(
        journal, cfg.repo, fix_cfg, log, control
    )
    report_duplicates = _duplicate_pr_reporter(
        journal, cfg.repo, fix_cfg, log, control
    )
    report_ci_flakes = _ci_flake_reporter(
        journal, cfg.repo, fix_cfg, log, control
    )
    report_stalls = _stall_reporter(journal, store, log)
    daemon_ref: dict[str, Daemon] = {}

    issue_kinds = frozenset({AgentKind.RESOLVE, AgentKind.RESUME})

    def dispatch_key(action: RunAgent):
        # `decide` reserves every action from the observed world before tick()
        # submits it. Reuse that exact identity; a second live GitHub lookup can
        # disagree with a world-probe snapshot and defeat issue deduplication.
        issue = reservations.issue_for(action)
        if issue is None:
            return (action.kind, action.number)
        return (
            ("issue", issue)
            if issue > 0
            else ("pr", action.number)
        )

    def action_reservation(
        action: RunAgent, world
    ) -> tuple[int, frozenset[str]]:
        issues = {issue.number: issue for issue in world.issues}
        prs = {pr.number: pr for pr in world.prs}
        if action.kind in issue_kinds:
            issue = action.number
            view = issues.get(issue)
            paths = (
                view.declared_paths | view.predicted_paths
                if view is not None
                else frozenset()
            )
            return issue, paths
        pr = prs.get(action.number)
        issue = breaker_key(pr) if pr is not None else -action.number
        view = issues.get(issue)
        paths = (pr.changed_files if pr is not None else frozenset()) | (
            view.declared_paths | view.predicted_paths
            if view is not None
            else frozenset()
        )
        return issue, paths

    # Sampled by observe() immediately BEFORE the world fetch. A leg that
    # finishes mid-fetch releases its reservation while the already-fetched
    # world still shows its ai-working label; a post-fetch snapshot then reads
    # "nothing active" against a stale world and reap_actions repairs an issue
    # that was never orphaned (keifu #18/#25 dual-label race, 2026-08-02 —
    # cost a duplicate route leg). Merging the pre-fetch sample fails toward
    # "busy", which defers work by one poll instead of duplicating it.
    pre_fetch_inflight: list[tuple[InFlightFix, ...]] = []

    def decide(world):
        # Report BEFORE deciding: if decide_fix raises, the tick still leaves a
        # record of what it was holding, which is the state hardest to diagnose.
        # ONE FixState per tick, shared with the flake reporter — it is the same
        # state decide_fix decides on, so the two can never describe different
        # worlds (and the store reads happen once, not twice).
        world = _inject_predicted_paths(store, world)
        held = {f.issue: f for f in pre_fetch_inflight[0]} if pre_fetch_inflight else {}
        held.update({f.issue: f for f in reservations.snapshot()})
        inflight = tuple(held.values())
        state = _fix_state(
            store,
            world,
            inflight,
            workflow_control=workflow_control,
            available_runtimes=frozenset(provider.value for provider in cfg.fix_runtimes),
        )
        open_prs = {pr.number for pr in world.prs if pr.state == "OPEN"}
        open_targets = {issue.number for issue in world.open_issues()} | {
            breaker_key(pr) for pr in world.prs if pr.number in open_prs
        }
        for prefix, errors in (
            ("workflow", state.control_errors),
            ("address-review", state.review_control_errors),
            ("fix leg history", state.leg_history_errors),
        ):
            for target, reason in errors.items():
                if target in open_targets:
                    log(f"{prefix} {target} held: {reason}")
        for target, reason in state.pr_reply_control_errors.items():
            if target in open_prs:
                log(f"PR reply {target} held: {reason}")
        migration_required = sorted(
            target for target, workflow in state.resolve_workflows.items()
            if target in open_targets and isinstance(workflow, WorkflowMigrationRequired)
        )
        if migration_required:
            log(f"resolve workflows held ({len(migration_required)}): resolve_migration_required")
        for target, workflow in state.resolve_workflows.items():
            if target in open_targets and isinstance(workflow, WorkflowUnavailable):
                log(f"resolve workflow {target} held: {workflow.reason}")
        holds = report_holds(world, inflight)
        report_prediction_outcomes(world)
        report_ci_stalls(world, state)
        report_duplicates(world)
        report_ci_flakes(world, state)
        report_stalls(world, holds)
        daemon = daemon_ref.get("daemon")
        externally_busy = fixer_busy()
        slots = (
            daemon.available_dispatch_slots
            if daemon is not None and not externally_busy
            else 0
        )
        decided = decide_fix(
            world,
            fix_cfg,
            state,
            fixer_busy=bool(inflight) or externally_busy,
            dispatch_slots=slots,
        )
        rejected: list[RunAgent] = []
        rejected_issues: set[int] = set()
        for action in decided:
            if isinstance(action, NoOp) and action.reason:
                log(action.reason)
            if not isinstance(action, RunAgent):
                continue
            issue, paths = action_reservation(action, world)
            if not reservations.reserve(action, issue, paths):
                rejected.append(action)
                rejected_issues.add(issue)
                log(
                    f"dispatch {action.kind.name} #{action.number}: deferred — "
                    "path reservation changed during decision"
                )
        if not rejected:
            return decided
        return [
            action
            for action in decided
            if action not in rejected
            and not (
                isinstance(action, Relabel)
                and action.target is Target.ISSUE
                and action.number in rejected_issues
                and action.add is Label.AI_WORKING
            )
        ]

    log(fix_capability_line(
        auto_merge=fix_cfg.auto_merge,
        auto_rebase=fix_cfg.auto_rebase_conflicts,
        escalate_on_evidence=cfg.escalate_on_evidence,
    ))
    recovery = _recovery(
        cfg.state_dir,
        cfg,
        control,
        effect_executor,
        journal,
        worktrees,
        owner_id=owner_id,
        reconciler=fix_reconciler,
    )
    reported_world = _reported_world(
        gateway.build_world, control, cfg, "fix", log
    )

    def observe_with_prefetch_reservations():
        pre_fetch_inflight[:] = [reservations.snapshot()]
        return reported_world()

    daemon = Daemon(
        "fix",
        observe_with_prefetch_reservations,
        decide,
        executor,
        cfg.tick_interval_seconds,
        log,
        journal=journal,
        tick_gate=_control_spend_gate(cfg, control, cfg.fix_budget_usd),
        wake_check=_wake_check(cfg, store),
        dispatch_capacity=cfg.fix_dispatch_concurrency,
        dispatch_key=dispatch_key,
        cancel_active_dispatches=dispatch_runtime.cancel_all,
        force_kill_dispatches=_force_kill(cfg, "fix", cfg.state_dir),
        dispatch_rejected=reservations.finish,
        heartbeat_path=os.path.join(cfg.state_dir, "heartbeat.json"),
        reconcile=recovery.reconcile,
        prepare_shutdown=_shutdown_prepare(
            control,
            owner_id=owner_id,
        ),
        finalize=_shutdown_finalize(
            control,
            effect_executor,
            owner_id=owner_id,
            retire_projection_feed=lambda: control.retire_projection_feed(
                deployment=_deployment_identity(cfg, "fix"),
                repository=cfg.repo,
                role="fix",
            ),
        ),
        terminalize=_shutdown_terminalize(
            control,
            CodexSpendReconciler(control, _spend_evidence_dirs(cfg)),
            owner_id=owner_id,
            state_dir=cfg.state_dir,
            role="fix",
        ),
    )
    daemon_ref["daemon"] = daemon
    return FixDaemonComposition(
        daemon, control, journal, executor, effect_executor, dispatcher,
        workflow_control, rearm_workflow, recovery, owner_id,
    )
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


# The deferral is an OPEN level; both ways out of it are CLOSING levels the
# projection's `_open_levels` recognises. "forced" would read as a level still
# open, which is the opposite of what a forced dispatch means.
_HOLD_OPEN = "waiting"
_HOLD_FORCED = "stopped"
_HOLD_RELEASED = "cleared"


def _fix_hold_reporter(journal: EventSink | None, repo: str, log, control=None):
    """Journal review deferrals at most once per (PR, blocking claim) (#176).

    Same shape as the fixer's hold reporter: the emitted set is persisted as
    level state, so a deferral spanning many ticks is one event and survives a
    restart. Telemetry only — every control-store interaction here is contained,
    because a journalling failure must not take down the review tick that
    `_fix_holds` went out of its way to keep dispatching.
    """
    active: dict[str, str] | None = None

    def _load() -> dict[str, str]:
        if control is None:
            return {}
        try:
            return {
                key: str(fields.get("state") or "")
                for key, fields in control.load_level_state(
                    "review_fix_hold", repo
                ).items()
            }
        except Exception:  # noqa: BLE001 - see docstring: telemetry only
            return {}

    def _emit(key: str, state: str, pr: int, attributes) -> None:
        if journal is None:
            return
        journal.emit(
            TelemetryEvent(
                LevelChanged(
                    f"review_fix_hold:{key}", state, "fix_leg_active", attributes
                ),
                Correlation(pr=pr),
            )
        )

    def report(deferred, forced) -> None:
        nonlocal active
        if active is None:
            active = _load()
        held = {
            f"{hold.pr}:{hold.claim_key}": (state, hold)
            for state, holds in ((_HOLD_OPEN, deferred), (_HOLD_FORCED, forced))
            for hold in holds
        }
        current = {key: state for key, (state, _hold) in held.items()}
        for key, (state, hold) in held.items():
            if active.get(key) == state:
                continue
            if state == _HOLD_OPEN:
                log(
                    f"DEFERRED: review of PR #{hold.pr} waits on fix claim "
                    f"{hold.claim_key}; dispatches once that leg finishes"
                )
            else:
                log(
                    f"review of PR #{hold.pr} dispatched despite fix claim "
                    f"{hold.claim_key} ({hold.held_seconds:.0f}s): deferral "
                    "ceiling reached"
                )
            _emit(
                key,
                state,
                hold.pr,
                {
                    "blocker_pr": hold.pr,
                    "blocking_claim": hold.claim_key,
                    "held_seconds": round(hold.held_seconds, 3),
                },
            )
        for key, previous in active.items():
            if key in current or previous == _HOLD_FORCED:
                continue  # a forced hold already emitted its closing level
            pr = int(key.split(":", 1)[0])
            log(f"review of PR #{pr} no longer waits on a fix leg")
            _emit(key, _HOLD_RELEASED, pr, {"blocker_pr": pr})
        if current == active:
            return  # the common case: no write transaction on a quiet poll
        if control is not None:
            try:
                control.replace_level_state(
                    "review_fix_hold",
                    repo,
                    {key: {"state": state} for key, state in current.items()},
                )
            except Exception:  # noqa: BLE001 - see docstring: telemetry only
                return  # keep memory on the last persisted set and retry next tick
        active = dict(current)

    return report


@dataclass(frozen=True)
class ReviewDaemonComposition:
    """The review role's composed authorities (see `FixDaemonComposition`).

    `runtime` is None only when the gateway cannot read pull request reviews;
    an activated review family then holds every paid review rather than
    falling back to the legacy path.
    """

    daemon: Daemon
    control: SQLiteControlStore
    journal: TypedJournal
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
    journal = _typed_journal(_journal(store), cfg, "review", store.state_dir)
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
            # Same harness-attested manifest the fixer carries, so both sides
            # of a review round argue from one set of facts about the machine
            # (2026-08-08 tauri #613/#614).
            environment_manifest=boot_manifest(cfg.evidence_stack),
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
    control = _control_store(cfg, cfg.review_state_dir, journal)
    owner_id = _owner_identity(cfg, "review")
    effect_executor = _effects(
        gateway,
        control,
        repository=cfg.repo,
        identity=owner_id,
        config_version=cfg.config_version,
        base_branch=cfg.base_branch,
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
    reservations = DispatchPathReservations(
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
        dcfg, gateway, store, None,
        JournalingRuntime(
            _with_accounting(
                review_runner,
                control,
                cfg,
                "review",
                review_runtime.runtime.value,
                journal,
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

    report_fix_holds = _fix_hold_reporter(
        journal, cfg.repo, say, control
    )
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
        report_fix_holds(plan.deferred, plan.forced)
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
            if reservations.reserve(
                action,
                issue,
                frozenset({f"pr:{action.number}"}),
            ):
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
        journal,
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

    daemon = Daemon("review", _reported_world(
                      gateway.build_review_world, control, cfg, "review",
                      say
                  ), decide, executor,
                  cfg.tick_interval_seconds, say, journal=journal,
                  tick_gate=_control_spend_gate(cfg, control, cfg.review_budget_usd),
                  wake_check=_wake_check(cfg, store),
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
                      retire_projection_feed=lambda: (
                          control.retire_projection_feed(
                              deployment=_deployment_identity(cfg, "review"),
                              repository=cfg.repo,
                              role="review",
                          )
                      ),
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
