"""One role's daemon: observe -> derive -> policy -> act, then sleep.

Each role reads the same GitHub observation and the same local facts (every
ended leg of the repository, from either role, plus the live claims and
leases of both), derives every item's state with `domain.state.derive`, maps
it to Actions with `domain.policy.actions`, and keeps only its own: the
review role runs `RunAgent(REVIEW)`, the fix role everything else. Both
deriving from the same facts is what keeps `needs-human` single-writer and a
leg of one role visible to the other (it holds its item busy and breaks the
other act's attempt run).

`Daemon.tick()` is the observe -> decide -> submit unit and is what tests
drive. Cheap actions execute before it returns; paid dispatches continue in a
bounded pool. `run_forever()` wraps it with the wait and heartbeat (a periodic
alive line plus the suspend-stall warning when wall-clock elapsed far exceeds
the interval). The wait ends early on a wake trigger (a GitHub change the tick
made, a leg of this role leaving the pool, an item the other role freed in the
control store); the poll interval stays the ceiling that picks up GitHub-side
events. The clock and sleep are injected so the loop is testable without real
time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, NamedTuple, Protocol
from zoneinfo import ZoneInfo

from ..adapters.accounting_runtime import AccountingRuntime, AdmissionCaps, issue_budget_scope
from ..adapters.act import reported_gaps
from ..adapters.atomic_file import atomic_write
from ..adapters.control_path import control_store_path
from ..adapters.control_store import ControlStoreError, LegRun, SQLiteControlStore
from ..adapters.github import GhUnavailable
from ..adapters.journal import Journal, JournalingRuntime
from ..adapters.leg_sandbox import LegSandbox
from ..adapters.runtime_plan import AgentRuntime, RuntimePlan
from ..adapters.spend_recovery import CodexSpendReconciler, spend_evidence_dir
from ..domain.agent_task import RuntimeName
from ..domain.contracts import BudgetScope, Money
from ..domain.actions import ISSUE_ACTS, Action, AgentKind, RunAgent
from ..domain.attempt import cut_off_counts
from ..domain.backoff import SESSION_LIMIT, LegEnd, dispatched_kind
from ..domain.evidence import recorded_keys
from ..domain.leg import Leg
from ..domain.outcome import CUT_OFF_ENDS, MACHINE_ENDS, AgentResult, LegClass, pre_admission_refusal
from ..domain.gaps import parse_harness_gap
from ..domain.policy import question_text
from ..domain.ports import SpendReconciler
from ..domain.recovery import RecoveryTrigger
from ..domain.refs import harness_branch_issue
from ..domain.state import DISPATCHABLE, DerivedState, Harness, ItemKey, Local, derive, issue_key, latest_steward, pr_key
from ..domain.trust import Untrusted, as_untrusted
from ..domain.routing import parse_tier
from ..domain.triage import parse_triage, steward_from_record
from ..domain.supervision import (
    ClockSample,
    DaemonState,
    ExitCode,
    suspension_between,
)
from ..domain.world import WorldState
from .config import MAX_DISPATCH_CONCURRENCY, HarnessConfig, budget_policy, owner_identity_for_target, rate_table, supervision_dir
from .live_config import LiveConfig
from .recovery import RecoveryCoordinator

def run_item(run: LegRun) -> ItemKey | None:
    """The item a recorded leg belongs to: its issue for STEWARD, TRIAGE and RESOLVE,
    its PR for PR-stage legs. Pre-5b kinds (RESUME, PR_RESUME, ROUTE) belong
    to no act: under the cutover rule those legs simply stop counting."""
    kind = dispatched_kind(run.kind)
    if kind in ISSUE_ACTS and run.issue > 0:
        return issue_key(run.issue)
    if kind in DISPATCHABLE and kind not in ISSUE_ACTS and run.number > 0:
        return pr_key(run.number)
    return None


def leg_end(run: LegRun, earlier: Sequence[LegEnd] = (), log: Callable[[str], None] | None = None) -> LegEnd | None:
    """A recorded leg as derive's `LegEnd`; its item's `earlier` ends (oldest
    first) decide whether a cut-off end is an attempt (`domain.attempt`). A run
    without a readable `leg` receipt never returned (died, reclaimed, refused
    before launch): a machine end."""
    kind = dispatched_kind(run.kind)
    if kind not in DISPATCHABLE or run.completed_at.utcoffset() is None:
        return None
    receipt = run.receipt or {}
    runtime = runtime if isinstance(runtime := receipt.get("runtime"), str) and runtime in _RUNTIMES else ""
    # Legacy: receipts before #441 marked a cap `capped` (on a free resume's machine end, and on
    # an agent end); `end` names every cut-off since. Exit: delete the `capped` read once no open
    # item holds a receipt written before #441.
    cut_off = isinstance(end := receipt.get("end"), str) and end in CUT_OFF_ENDS or receipt.get("capped") is True
    leg = next((c for c in (LegClass.AGENT, LegClass.ASKED) if receipt.get("leg") == c.value), LegClass.MACHINE)
    if leg is LegClass.MACHINE and cut_off and cut_off_counts(kind, runtime, earlier):
        leg = LegClass.AGENT
    started = run.started_at
    if started is not None and (started.utcoffset() is None or started > run.completed_at):
        started = None
    question = question_text(receipt.get("question")) if leg is LegClass.ASKED else None
    ran = leg is not LegClass.MACHINE
    triage = parse_triage(receipt.get("triage")) if kind is AgentKind.TRIAGE and ran else None
    raw = receipt.get("steward")
    steward = steward_from_record(raw, run.issue) if kind is AgentKind.STEWARD and ran else None
    if log is not None and steward is not None and steward != steward_from_record(raw):
        log(f"issue #{run.issue}: a recorded steward claim names the issue itself; dropped")
    evidence = recorded_keys(receipt.get("evidence")) if kind is AgentKind.FIX_CI and ran else frozenset()
    end_head = receipt.get("end_head")
    session = receipt.get("session")
    return LegEnd(kind, run.completed_at, leg is LegClass.MACHINE, run.head, question=question, started_at=started,
                  triage=triage, steward=steward, tier=parse_tier(receipt.get("tier")), evidence=evidence,
                  end_head=end_head if ran and isinstance(end_head, str) and len(end_head) <= 64 else "",
                  capped=leg is LegClass.AGENT and cut_off,
                  harness_gap=ran and parse_harness_gap(receipt.get("harness_issue")) is not None,
                  leg_id=run.leg_id[:64], runtime=runtime,
                  end=end if not ran and isinstance(end, str) and end in MACHINE_ENDS else "",
                  session=session if isinstance(session, str) and len(session) <= SESSION_LIMIT else "")


def leg_ends(runs: Iterable[LegRun], log: Callable[[str], None] | None = None) -> tuple[tuple[LegRun, LegEnd], ...]:
    """Each run of an item's act with its `LegEnd`, in end order (ties keep
    the recorded order): every item's legs classified by the one rule."""
    out: list[tuple[LegRun, LegEnd]] = []
    earlier: dict[ItemKey, list[LegEnd]] = {}
    for run in sorted(runs, key=lambda r: r.completed_at):
        key = run_item(run)
        if key is not None and (end := leg_end(run, earlier.get(key, ()), log)) is not None:
            earlier.setdefault(key, []).append(end)
            out.append((run, end))
    return tuple(out)


# A receipt's runtime reaches a public gap issue: only a known name passes.
_RUNTIMES = frozenset(r.value for r in RuntimeName)


def steward_criteria(runs: Iterable[LegRun]) -> Untrusted | None:
    """The acceptance criteria of the issue's latest agent-ended STEWARD leg
    as derive reads it, as a prompt list:
    agent text the steward's parser laundered, so still untrusted."""
    ends = tuple(end for _, end in leg_ends(runs) if end.kind is AgentKind.STEWARD)
    found = latest_steward(ends)
    criteria = found.steward.acceptance_criteria if found is not None and found.steward is not None else ()
    return as_untrusted("\n".join(f"- {c}" for c in criteria)) if criteria else None


def local_legs(runs: Iterable[LegRun], log: Callable[[str], None] | None = None) -> dict[ItemKey, tuple[LegEnd, ...]]:
    out: dict[ItemKey, list[LegEnd]] = {}
    for run, end in leg_ends(runs, log):
        if (key := run_item(run)) is not None:
            out.setdefault(key, []).append(end)
    return {key: tuple(ends) for key, ends in out.items()}


def dispatch_item(action: RunAgent) -> ItemKey:
    return issue_key(action.number) if action.kind in ISSUE_ACTS else pr_key(action.number)


def leased_items(active: Iterable[RunAgent], held_issues: Iterable[int]) -> frozenset[ItemKey]:
    """Items with a leg in flight: this process's dispatches, plus every issue
    either role (in any process, or an orphaned child) holds a live claim or
    lease on."""
    return frozenset(dispatch_item(a) for a in active) | frozenset(
        issue_key(n) for n in held_issues if type(n) is int and n > 0
    )


def item_issues(world: WorldState) -> frozenset[int]:
    return frozenset(i.number for i in world.issues if i.number > 0) | frozenset(
        n for p in world.prs if (n := harness_branch_issue(p.head_ref_name)) is not None
    )


class Facts:
    """The local facts derive needs, read from SQLite each tick. A failed run
    or lease read raises: the daemon skips the tick rather than deciding
    blind. A failed lifetime-spend read is no headroom instead: paid legs wait
    silently and free work (merge, open, refresh, stops) goes on."""

    def __init__(
        self,
        control,
        repository: str,
        *,
        active: Callable[[], Iterable[RunAgent]],
        headroom: Callable[[], bool],
        lifetime_capped: Callable[[int], bool],
        log: Callable[[str], None],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.control = control
        self.repository = repository
        self.active = active
        self.headroom = headroom
        self.lifetime_capped = lifetime_capped
        self.log = log
        self.now = now

    def held_issues(self) -> frozenset[int]:
        return self.control.held_targets(self.repository)

    def __call__(self, world: WorldState) -> Local:
        # `active` is sampled before the run read, so a leg ending between the
        # two reads is busy (still active) rather than free with its end
        # unseen. A leg that ended after GitHub was read is seen here but its
        # push may not be in `world`: derive holds it busy (end > world.now).
        active = tuple(self.active())
        issues = item_issues(world)
        runs = self.control.leg_runs(self.repository, issues)
        try:
            capped: frozenset[int] | None = frozenset(n for n in issues if self.lifetime_capped(n))
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            self.log(f"lifetime spend unreadable, paid legs wait this tick: {exc}")
            capped = None
        return Local(
            now=self.now(),
            legs=local_legs(runs, self.log),
            leases=leased_items(active, self.held_issues()),
            headroom=capped is not None and self.headroom(),
            lifetime_capped=capped or frozenset(),
            reported=reported_gaps(self.control),
        )


class Slots(NamedTuple):
    """Free dispatch capacity: `heavy` for acts that hold a slot, `light` for
    those that do not (STEWARD, TRIAGE), which have their own ceiling."""

    heavy: int
    light: int


Choose = Callable[[WorldState, Mapping[ItemKey, DerivedState], Slots, Local], Iterable[Action]]


def decide_with(
    role: str,
    facts: Callable[[WorldState], Local],
    harness: Harness,
    choose: Choose,
    slots: Callable[[], Slots],
    log: Callable[[str], None],
) -> Callable[[WorldState], list[Action]]:
    """derive once, then the role's `choose` keeps its Actions (with at most
    `slots` paid dispatches)."""
    def decide(world: WorldState) -> list[Action]:
        local = facts(world)
        chosen = list(choose(world, derive(world, local, harness), slots(), local))
        for action in chosen:
            log(f"{role}: {action!r}")
        return chosen
    return decide


def _ignore_log(_message: str) -> None:
    pass


class ActionExecutor(Protocol):
    """What the loop needs from a role's executor."""

    def execute(self, action: Action) -> None: ...


DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 80.0
# The inter-tick wait checks its wake triggers once a second, and ends no
# sooner than the gap after it began: one GitHub observation per 5 s however
# busy the triggers are (the GraphQL budget is shared with the sibling role and
# the dashboard). The poll interval is the ceiling. A trigger is an event whose
# effects have already landed, so none waits for anything to settle.
WAKE_CHECK_SECONDS = 1
WAKE_MIN_GAP_SECONDS = 5
# Requeue after mutation (controller-runtime): the next act in the lifecycle
# waits on the tick's own GitHub change (#1096: 83 s from the triage comment
# to the RESOLVE dispatch).
RETICK = "re-tick after a GitHub change"
# A leg of this role left the pool: its slot and item are free to the next tick.
LEG_ENDED = "a leg of this role ended"
# The other role handed back a claim, after its leg's GitHub effects landed.
ITEM_FREED = "the other role freed an item"
DEFAULT_RESERVATION_SWEEP_POLLS = 30
_SHUTDOWN_ESCALATION_RESERVE_SECONDS = 10.0
_SHUTDOWN_PREPARE_RESERVE_SECONDS = 2.0
_SHUTDOWN_TERMINALIZE_RESERVE_SECONDS = 3.0
_ALLOWED_TRANSITIONS = {
    DaemonState.STARTING: {DaemonState.RUNNING, DaemonState.DRAINING},
    DaemonState.RUNNING: {DaemonState.DRAINING},
    DaemonState.DRAINING: {DaemonState.STOPPED},
    DaemonState.STOPPED: set(),
}


def order_for_execution(actions: list[Action]) -> list[Action]:
    """Cheap actions first, dispatches last, each group in decide's order. A
    merge must never wait behind paid work (PR #533 sat APPROVED and green
    across two daemon lifetimes behind other issues' resolves)."""
    return [a for a in actions if not isinstance(a, RunAgent)] + [a for a in actions if isinstance(a, RunAgent)]


@dataclass
class Daemon:
    name: str
    observe: Callable[[], WorldState]
    decide: Callable[[WorldState], list[Action]]
    executor: ActionExecutor
    poll_seconds: int = 60
    log: Callable[[str], None] = _ignore_log
    # Paid dispatches run behind the observation loop, at most this many at
    # once; read at each use (FIX_DISPATCH_CONCURRENCY is a live knob).
    dispatch_capacity: Callable[[], int] = lambda: 1
    # Duplicate suppression: one in-flight dispatch per key.
    dispatch_key: Callable[[RunAgent], Hashable] = lambda action: (action.kind, action.number)
    # Cancels the asyncio tasks under the worker threads before the pool joins.
    cancel_active_dispatches: Callable[[], None] | None = None
    force_kill_dispatches: Callable[[], object] | None = None
    heartbeat_path: str = ""
    # Total shutdown deadline including forced teardown and reconciliation:
    # the systemd hard stop is 90s, leaving 10s for interpreter exit.
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    # Injected so deadline expiry is a decision in tests, not a race.
    monotonic: Callable[[], float] = time.monotonic
    reconcile: Callable[[RecoveryTrigger], object] | None = None
    # Elapsed (monotonic) time in poll intervals: early wakes add ticks, not sweeps.
    reservation_sweep_polls: int = DEFAULT_RESERVATION_SWEEP_POLLS
    # Opened for one inter-tick wait (`StoreWake.watch`): its probe reports
    # whether the other role freed an item since the tick began.
    watch_store: Callable[[], AbstractContextManager[Callable[[], bool]]] | None = None
    prepare_shutdown: Callable[[], object] | None = None
    terminalize: Callable[[], object] | None = None
    _state: DaemonState = field(default=DaemonState.STARTING, init=False, repr=False)
    _dispatch_pool: ThreadPoolExecutor = field(init=False, repr=False)
    # Guards `_dispatches`; notified when a finished dispatch leaves it.
    _dispatch_lock: threading.Condition = field(default_factory=lambda: threading.Condition(threading.Lock()),
                                                init=False, repr=False)
    _shutdown_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _dispatches: dict[Future[None], tuple[RunAgent, Hashable]] = field(default_factory=dict, init=False, repr=False)
    # Dispatches that have left `_dispatches`; the wait compares it with the tick's.
    _dispatches_ended: int = field(default=0, init=False, repr=False)
    # A hot reload waits for paid work to drain (`_admitting`).
    _reload_pending: bool = field(default=False, init=False, repr=False)
    _dispatch_closed: bool = field(default=False, init=False, repr=False)
    _retry_after_seconds: float | None = field(default=None, init=False, repr=False)
    _shutdown_deadline: float | None = field(default=None, init=False, repr=False)
    _requires_hard_exit: bool = field(default=False, init=False, repr=False)
    _shutdown_prepared: bool = field(default=False, init=False, repr=False)
    _shutdown_prepare_clean: bool = field(default=True, init=False, repr=False)
    _shutdown_terminalization_required: bool = field(default=False, init=False, repr=False)
    _terminalized: bool = field(default=False, init=False, repr=False)
    _terminalize_clean: bool = field(default=True, init=False, repr=False)
    _wake_recovery_pending: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # Heavy legs fill `dispatch_capacity`; light ones (STEWARD, TRIAGE) get as many
        # more, their ceiling. The pool is sized for the largest capacity the
        # config admits (threads start lazily), so a queued light leg can never
        # hide in it whatever the capacity becomes.
        self._dispatch_pool = ThreadPoolExecutor(max_workers=2 * MAX_DISPATCH_CONCURRENCY,
                                                 thread_name_prefix=f"{self.name}-dispatch")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be positive")
        if self.reservation_sweep_polls <= 0:
            raise ValueError("reservation sweep cadence must be positive")

    @property
    def state(self) -> DaemonState:
        return self._state

    @property
    def requires_hard_exit(self) -> bool:
        """Whether bounded cleanup left threads that can block interpreter exit."""
        return self._requires_hard_exit

    def _transition(self, state: DaemonState, reason: str = "") -> None:
        if state is self._state:
            return
        if state not in _ALLOWED_TRANSITIONS[self._state]:
            raise RuntimeError(f"invalid daemon transition {self._state.value} -> {state.value}")
        previous, self._state = self._state, state
        self.log(f"{self.name}: state {previous.value} -> {state.value}" + (f" ({reason})" if reason else ""))

    def _write_heartbeat(self) -> None:
        if not self.heartbeat_path:
            return
        try:
            if parent := os.path.dirname(self.heartbeat_path):
                os.makedirs(parent, mode=0o700, exist_ok=True)
            payload = {"pid": os.getpid(), "state": self._state.value, "reload_pending": self._reload_pending,
                       "in_flight": [{"kind": a.kind.name, "number": a.number} for a in self.active_dispatches],
                       "updated_at": datetime.now(timezone.utc).isoformat()}
            atomic_write(self.heartbeat_path, json.dumps(payload, sort_keys=True))
        except OSError as exc:
            self.log(f"{self.name}: heartbeat write failed: {exc}")

    @property
    def active_dispatches(self) -> tuple[RunAgent, ...]:
        return tuple(action for action, _key in self.active_dispatch_items)

    @property
    def active_dispatch_items(self) -> tuple[tuple[RunAgent, Hashable], ...]:
        with self._dispatch_lock:
            return tuple(self._dispatches.values())

    def _admitting(self, *, heavy: bool) -> bool:
        # Caller holds _dispatch_lock. Shutdown or pending recovery closes
        # admission. A pending hot reload holds heavy legs while they drain;
        # light ones (STEWARD, TRIAGE: their own small budget and timeout)
        # start only while a heavy leg is still in flight, so the reload
        # waits at most one light-leg timeout past the last heavy one (the
        # light lane runs in parallel), never a stream of them (#1105 sat
        # 45 min behind #1078's FIX_CI).
        if self._dispatch_closed or self._wake_recovery_pending:
            return False
        return not self._reload_pending or (not heavy and self._in_flight(heavy=True) > 0)

    @property
    def available_dispatch_slots(self) -> int:
        """Capacity visible to decide, so it never chooses work this process
        will not start."""
        capacity = self.dispatch_capacity()
        with self._dispatch_lock:
            return max(0, capacity - self._in_flight(heavy=True)) if self._admitting(heavy=True) else 0

    def _in_flight(self, *, heavy: bool) -> int:
        # Caller holds _dispatch_lock.
        return sum(a.kind.holds_dispatch_slot is heavy for a, _k in self._dispatches.values())

    @property
    def available_light_slots(self) -> int:
        capacity = self.dispatch_capacity()
        with self._dispatch_lock:
            return max(0, capacity - self._in_flight(heavy=False)) if self._admitting(heavy=False) else 0

    def _dispatch_done(self, future: Future[None]) -> None:
        with self._dispatch_lock:
            entry = self._dispatches.pop(future, None)
            if entry is not None:
                self._dispatches_ended += 1
            self._dispatch_lock.notify_all()
        if entry is None:
            return
        if future.cancelled():
            self.log(f"{self.name}: action {entry[0]!r} was cancelled")
        elif (exc := future.exception()) is not None:
            self.log(f"{self.name}: action {entry[0]!r} failed: {exc}")

    def _submit_dispatch(self, action: RunAgent) -> bool:
        key = self.dispatch_key(action)
        capacity = self.dispatch_capacity()
        with self._dispatch_lock:
            if (not self._admitting(heavy=action.kind.holds_dispatch_slot)
                    or self._in_flight(heavy=action.kind.holds_dispatch_slot) >= capacity
                    or key in {k for _a, k in self._dispatches.values()}):
                return False
            future = self._dispatch_pool.submit(self.executor.execute, action)
            self._dispatches[future] = (action, key)
        # Outside the lock: a future that already completed runs its callback
        # synchronously, which takes the lock to remove itself.
        future.add_done_callback(self._dispatch_done)
        return True

    def wait_for_dispatches(self) -> None:
        """Wait until the current set has left the in-flight set, its slots and
        keys free, without closing admission (tests). A future wakes its
        waiters before it runs `_dispatch_done`, so waiting on the futures
        alone could return while the drain still counted them."""
        with self._dispatch_lock:
            pending = tuple(self._dispatches)
            self._dispatch_lock.wait_for(lambda: self._dispatches.keys().isdisjoint(pending))

    def shutdown_dispatches(self, *, cancel: bool) -> bool:
        """Close admission, optionally cancel active async work, and join. The
        pool's workers are non-daemon threads: without cancellation Ctrl-C
        leaves the agent session alive and interpreter exit waits on it."""
        with self._shutdown_lock:
            return self._shutdown_dispatches(cancel=cancel)

    def _shutdown_dispatches(self, *, cancel: bool) -> bool:
        pool = self._dispatch_pool
        self._shutdown_terminalization_required |= cancel
        if self._shutdown_deadline is None:
            self._shutdown_deadline = self.monotonic() + self.shutdown_timeout_seconds
        with self._dispatch_lock:
            if self._dispatch_closed:
                return False if self._dispatches else self._finish_shutdown()
            self._dispatch_closed = True
        if cancel:
            self._prepare_shutdown()
            if self.cancel_active_dispatches is not None:
                try:
                    self.cancel_active_dispatches()
                except Exception as exc:  # noqa: BLE001 - shutdown must still join
                    self.log(f"{self.name}: dispatch cancellation failed: {exc}")
        with self._dispatch_lock:
            pending = tuple(self._dispatches)
        reserve = min(_SHUTDOWN_ESCALATION_RESERVE_SECONDS, self.shutdown_timeout_seconds * 0.5)
        _done, unfinished = wait(pending, timeout=max(0.0, self._remaining_shutdown_time() - reserve))
        if unfinished:
            self.log(f"{self.name}: shutdown deadline expired with {len(unfinished)} dispatch(es) still active")
            self._shutdown_terminalization_required = True
            self._prepare_shutdown()  # persist cancellation intent before teardown
            terminalize_reserve = min(_SHUTDOWN_TERMINALIZE_RESERVE_SECONDS, self.shutdown_timeout_seconds * 0.2)
            terminalized = False
            if self.force_kill_dispatches is not None and self._bounded_shutdown_call(
                    self.force_kill_dispatches, "forced process teardown",
                    max_seconds=max(0.0, self._remaining_shutdown_time() - terminalize_reserve)):
                # A killed provider flushes its final rollout evidence while the
                # worker unwinds; the worker settles before spend reconciliation.
                _done, unfinished = wait(unfinished, timeout=max(
                    0.0, self._remaining_shutdown_time() - terminalize_reserve))
                if not unfinished:
                    terminalized = self._terminalize()
        else:
            terminalized = self._terminalize() if self._shutdown_terminalization_required else True
        pool.shutdown(wait=False, cancel_futures=cancel)
        return not unfinished and self._shutdown_prepare_clean and terminalized

    def _finish_shutdown(self) -> bool:
        terminalized = self._terminalize() if self._shutdown_terminalization_required else True
        return self._shutdown_prepare_clean and terminalized

    def _remaining_shutdown_time(self) -> float:
        if self._shutdown_deadline is None:
            return self.shutdown_timeout_seconds
        return max(0.0, self._shutdown_deadline - self.monotonic())

    def _prepare_shutdown(self) -> bool:
        if self._shutdown_prepared:
            return self._shutdown_prepare_clean
        self._shutdown_prepared = True
        if self.prepare_shutdown is None:
            return True
        reserve = min(_SHUTDOWN_PREPARE_RESERVE_SECONDS, self.shutdown_timeout_seconds * 0.2)
        self._shutdown_prepare_clean = self._bounded_shutdown_call(
            self.prepare_shutdown, "shutdown intent persistence",
            max_seconds=max(0.0, self._remaining_shutdown_time() - reserve))
        return self._shutdown_prepare_clean

    def _terminalize(self) -> bool:
        if self._terminalized:
            return self._terminalize_clean
        self._terminalized = True
        if self.terminalize is None:
            return True
        self._terminalize_clean = self._bounded_shutdown_call(
            self.terminalize, "durable run terminalization", max_seconds=self._remaining_shutdown_time())
        return self._terminalize_clean

    def _bounded_shutdown_call(self, callback: Callable[[], object], description: str, *,
                               max_seconds: float | None = None) -> bool:
        budget = self._remaining_shutdown_time()
        if max_seconds is not None:
            budget = min(budget, max_seconds)
        if budget <= 0:
            self.log(f"{self.name}: no shutdown budget remains for {description}")
            return False
        completed = threading.Event()
        failures: list[BaseException] = []

        def invoke() -> None:
            try:
                callback()
            except BaseException as exc:  # noqa: BLE001 - report after join
                failures.append(exc)
            finally:
                completed.set()

        threading.Thread(target=invoke, name=f"{self.name}-shutdown-{description.replace(' ', '-')}",
                         daemon=True).start()
        completed.wait(timeout=budget)
        if not completed.is_set():
            self.log(f"{self.name}: {description} exceeded the shutdown deadline")
            return False
        if failures:
            self.log(f"{self.name}: {description} failed: {failures[0]}")
            return False
        return True

    def _reconcile_wake(self) -> None:
        """Wake recovery runs only once live dispatch owners have settled."""
        if self.reconcile is None:
            return
        if self.active_dispatches:
            if not self._wake_recovery_pending:
                self.log(f"{self.name}: wake recovery deferred until active dispatches settle")
            self._wake_recovery_pending = True
            return
        self.reconcile(RecoveryTrigger.WAKE)
        self._wake_recovery_pending = False

    def tick(self, scheduler_progress: Callable[[], None] | None = None) -> list[Action]:
        """One observe→decide→execute pass; returns the actions taken (a failed
        GitHub write is not taken). Never
        raises: a failed pass logs and yields []. GitHub being unavailable is
        an operating condition, not a bug: nothing is decided, one loud line,
        and the next poll is the retry (spaced by any Retry-After)."""
        def progress() -> None:
            if scheduler_progress is not None:
                scheduler_progress()

        progress()
        try:
            world = self.observe()
            progress()
            actions = self.decide(world)
        except GhUnavailable as exc:
            # Set even without a Retry-After: a back-off interval is never woken early.
            self._retry_after_seconds = exc.retry_after_s or 0.0
            self.log(f"{self.name}: world build failed: GitHub {exc.headline} — skipping tick, no actions taken "
                     f"({exc.detail})")
            return []
        except Exception as exc:  # noqa: BLE001 - crash-isolate a poll
            self.log(f"{self.name}: poll pass failed (continuing): {exc}")
            return []
        taken: list[Action] = []
        for action in order_for_execution(actions):
            try:
                if not isinstance(action, RunAgent):
                    self.executor.execute(action)
                    taken.append(action)
                elif self._submit_dispatch(action):
                    taken.append(action)
                else:
                    self.log(f"{self.name}: dispatch {action.kind.name} #{action.number} not submitted — slot "
                             "full, duplicate target, or recovery/reload draining")
            except GhUnavailable as exc:
                # A refused write backs off like a refused read: no re-tick, no early wake.
                self._retry_after_seconds = exc.retry_after_s or 0.0
                self.log(f"{self.name}: action {action!r} failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - one bad action ≠ dead daemon
                self.log(f"{self.name}: action {action!r} failed: {exc}")
            finally:
                progress()
        return taken

    def run_forever(
        self,
        sleep: Callable[[int], None],
        now: Callable[[], int],
        heartbeat_seconds: int = 900,
        max_ticks: int | None = None,
        reload_check: Callable[[], bool] | None = None,
        active_now: Callable[[], float] = time.monotonic,
        scheduler_progress: Callable[[], None] | None = None,
    ) -> None:
        """tick→sleep with a heartbeat; `max_ticks` bounds it for tests. On a
        positive `reload_check` admission closes, observation continues while
        paid work drains, then the process exits RELOAD_EXIT_CODE."""
        self.log(f"{self.name}: watching (poll {self.poll_seconds}s)")
        self._transition(DaemonState.RUNNING)
        last, last_active = now(), active_now()
        last_logged, last_swept, ticks = last, last_active, 0
        own_wakes = True
        try:
            while max_ticks is None or ticks < max_ticks:
                cur, cur_active = now(), active_now()
                if self._wake_recovery_pending:
                    self._reconcile_wake()
                elapsed = cur - last
                # A late tick is a stall only when wall time outran monotonic
                # time: a long synchronous leg is productive, not suspended.
                suspended = round(suspension_between(ClockSample(last, last_active),
                                                     ClockSample(cur, cur_active)).suspended_seconds)
                last, last_active = cur, cur_active
                if elapsed >= self.poll_seconds * 4 and suspended > 0:
                    self.log(f"{self.name}: ♥ woke after {elapsed}s (expected ~{self.poll_seconds}s, {suspended}s "
                             "suspended) — likely suspended/stalled; catching up")
                    self._reconcile_wake()
                    last_logged = cur
                elif cur - last_logged >= heartbeat_seconds:
                    self.log(f"{self.name}: ♥ alive (poll {self.poll_seconds}s)")
                    last_logged = cur
                ended = self._dispatches_ended
                taken = self.tick(scheduler_progress)
                self._write_heartbeat()
                if scheduler_progress is not None:
                    scheduler_progress()
                ticks += 1
                if (self.reconcile is not None
                        and cur_active - last_swept >= self.reservation_sweep_polls * self.poll_seconds):
                    last_swept = cur_active
                    try:
                        self.reconcile(RecoveryTrigger.PERIODIC)
                    except Exception as exc:
                        self.log(f"{self.name}: reservation sweep failed (continuing): {exc}")
                reload_requested = reload_check is not None and reload_check()
                if reload_requested and self.active_dispatches and not self._reload_pending:
                    self.log(f"{self.name}: hot-reload requested — draining {len(self.active_dispatches)} paid "
                             "dispatch(es) while observation continues; light legs start only while a heavy one "
                             "drains")
                    with self._dispatch_lock:
                        self._reload_pending = True
                    self._write_heartbeat()
                if (reload_requested or self._reload_pending) and not self.active_dispatches:
                    from ..adapters.reload import RELOAD_EXIT_CODE

                    self.log(f"{self.name}: hot-reload — harness source changed; restarting with no session in flight")
                    raise SystemExit(RELOAD_EXIT_CODE)
                reason = self._sleep_interval(
                    sleep, scheduler_progress, ended=ended, own_wakes=own_wakes,
                    retick=any(not isinstance(action, RunAgent) for action in taken))
                # A tick woken by its own trigger is never woken by its own
                # trigger again: a mutation every tick repeats (an idempotent
                # re-post, a merge GitHub has not taken yet) or a dispatch that
                # ends at once every tick (a skip before its claim, a refused
                # read in the worker: no leg recorded, so no back-off) costs
                # at most one extra observe per interval, never a loop.
                own_wakes = reason not in (RETICK, LEG_ENDED)
                if reason:
                    self.log(f"{self.name}: woke: {reason}")
            self._transition(DaemonState.DRAINING, "loop completed")
            clean = self.shutdown_dispatches(cancel=False)
            self._transition(DaemonState.STOPPED)
            self._write_heartbeat()
            if not clean:
                self._requires_hard_exit = True
                raise SystemExit(ExitCode.TRANSIENT)
        except BaseException as exc:
            if self._state not in {DaemonState.DRAINING, DaemonState.STOPPED}:
                self._transition(DaemonState.DRAINING, "shutdown requested")
            clean = True
            if self._state is not DaemonState.STOPPED:
                clean = self.shutdown_dispatches(cancel=True)
                self._transition(DaemonState.STOPPED)
            self._write_heartbeat()
            if not clean:
                self._requires_hard_exit = True
                raise SystemExit(ExitCode.TRANSIENT) from exc
            raise

    def _sleep_interval(self, sleep: Callable[[int], None], scheduler_progress: Callable[[], None] | None = None,
                        *, retick: bool, ended: int, own_wakes: bool) -> str | None:
        """One poll interval, checked once a second (feeding the watchdog);
        returns the wake trigger that ended it early: the tick's own GitHub
        change (`retick`) or a dispatch leaving the pool since `ended` was
        read at the tick's start (both only with `own_wakes`), or an item the
        other role freed. After GitHub refused a tick the back-off runs in
        full: no trigger may turn it into another request."""
        wakeable = self._retry_after_seconds is None
        own_wakes = own_wakes and wakeable
        interval = max(self.poll_seconds, int(self._retry_after_seconds or 0))
        self._retry_after_seconds = None
        reason, elapsed = (RETICK if retick and own_wakes else None), 0
        # Opened even for a back-off, so the claims it frees are spent on it.
        with (self.watch_store or _nothing_freed)() as freed_elsewhere:
            while elapsed < interval:
                step = min(WAKE_CHECK_SECONDS, interval - elapsed)
                sleep(step)
                elapsed += step
                if scheduler_progress is not None:
                    scheduler_progress()
                if not wakeable:
                    continue
                reason = (reason or (LEG_ENDED if own_wakes and self._dispatches_ended != ended else None)
                          or (ITEM_FREED if freed_elsewhere() else None))
                if reason is not None and elapsed >= WAKE_MIN_GAP_SECONDS:
                    return reason
        return None


def _nothing_freed() -> AbstractContextManager[Callable[[], bool]]:
    return contextlib.nullcontext(lambda: False)


async def run_leg(
    runner: AgentRuntime,
    log: Callable[[str], None],
    leg: Leg,
    task: RuntimePlan,
    *,
    scope: AbstractContextManager[object] | None = None,
) -> AgentResult:
    """The single path for executing and logging an agent leg."""
    target = leg.number or 0
    label = leg.kind.casefold().replace("_", "-")
    log(f"agent #{target}: {label} — running")
    with scope or contextlib.nullcontext():
        result = await runner.run(task)
    status = result.status or ("error" if result.is_error else "ok")
    log(
        f"agent #{target}: {label} — done "
        f"(status={status}{f', ${result.cost_usd:.2f}' if result.cost_usd else ''}"
        # The refusal's summary names the missing authority (#262).
        f"{f', {result.summary[:300]}' if pre_admission_refusal(result) else ''})"
    )
    return result


# ── composition: both roles assemble through `compose_role` ─────────────────


def stderr_log(_role: str) -> Callable[[str], None]:
    def log(msg: str) -> None:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()  # a crash right after must not eat the line
    return log


class CancellableAsyncDispatch:
    """Run one dispatch coroutine per worker thread with cooperative shutdown.

    Each worker owns its event loop; the daemon thread keeps a thread-safe
    cancellation handle. Cancelling the root task propagates into the async
    runner, whose subprocess cleanup ends its process group before the worker
    exits.
    """

    def __init__(self, dispatch: Callable[[RunAgent], Awaitable[None]]) -> None:
        self._dispatch = dispatch
        self._lock = threading.Lock()
        self._active: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Task[None], threading.Event]] = {}
        # Sticky: cancellation may win the race before a submitted worker
        # reaches __call__; every later registration observes it too.
        self._shutdown = threading.Event()

    def _new_cancel_event(self) -> threading.Event:
        return threading.Event()

    async def _run(self, action: RunAgent, cancel_requested: threading.Event) -> None:
        task = asyncio.create_task(self._dispatch(action))
        try:
            while not task.done():
                if cancel_requested.is_set():
                    task.cancel()
                # call_soon_threadsafe is the normal path; the bounded wait is a
                # fallback for loops whose self-pipe wake is gone in teardown.
                await asyncio.wait((task,), timeout=1.0)
            await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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

    def cancel_all(self) -> None:
        with self._lock:
            self._shutdown.set()
            active = tuple(self._active.values())
        for loop, task, cancel_requested in active:
            cancel_requested.set()
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                continue  # the worker closed its loop after the snapshot


def control_store(cfg: HarnessConfig, role: str) -> SQLiteControlStore:
    """The one accounting/control authority shared by both roles. The first
    open after the 3-table cutover retires a legacy store; `CutoverRefused`
    stops the boot while that store still holds unsettled money."""
    control = SQLiteControlStore(control_store_path(
        (cfg.state_dir, cfg.review_state_dir), fallback_state_dir=cfg.state_dir_for(role),
        authority_root=cfg.control_authority_root))
    control.initialize()
    return control


def owner_identity(cfg: HarnessConfig, role: str) -> str:
    return owner_identity_for_target(cfg.repo, cfg.target_dir, role)


def spend_reconciler(cfg: HarnessConfig, control: SQLiteControlStore) -> CodexSpendReconciler:
    dirs = dict.fromkeys(os.path.abspath(path) for path in (cfg.state_dir, cfg.review_state_dir) if path)
    return CodexSpendReconciler(control, tuple(spend_evidence_dir(path) for path in dirs))


def headroom(cfg: HarnessConfig, control, budget_usd: float) -> Callable[[], bool]:
    """Daily repository/global headroom for one more leg of `budget_usd`
    (public: the dashboard derives with it too), over the authority
    reservation uses. A cap below one leg makes paid work wait here instead of
    being refused at admission (decision #7); an unreadable ledger is no
    headroom: free work goes on, paid work waits (Q7)."""
    caps = tuple((repository, dimension, Money.from_major("USD", cap)) for repository, dimension, cap in (
        (cfg.repo, "repository", cfg.daily_spend_cap_usd), ("*", "global", cfg.global_daily_spend_cap_usd))
        if cap > 0)
    leg = Money.from_major("USD", budget_usd)

    def fits() -> bool:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            for repository, dimension, cap in caps:
                snapshot = control.snapshot(BudgetScope(
                    repository, dimension, "USD", start.date().isoformat(), start,
                    start + timedelta(days=1), ZoneInfo("UTC")))
                if (snapshot.disposition.value != "available"
                        or snapshot.committed.micros + leg.micros > cap.micros):
                    return False
        except Exception:  # noqa: BLE001 - see docstring
            return False
        return True

    return fits


def lifetime_capped(cfg: HarnessConfig, control) -> Callable[[int], bool]:
    """Whether an issue can no longer afford one more leg under its lifetime
    cap, priced at the dearer role's leg: fix alone writes the stop, so both
    roles must agree or a review nobody can afford is stopped by nobody."""
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


# The checkout this code runs from: never visible to a leg (config/ holds the bot tokens).
_THIS_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def leg_sandbox(cfg: HarnessConfig, role: str, env: Mapping[str, str] | None = None) -> LegSandbox:
    """The role's leg sandbox (#413). A fix leg writes its checkout and holds
    FIX_BOT_TOKEN, whatever account owns it; a review leg reads and holds no
    credential: the act layer keeps the bot tokens."""
    env = os.environ if env is None else env
    fix = role == "fix"
    return LegSandbox(
        home=env.get("HOME") or os.path.expanduser("~"),
        repository=cfg.target_dir or os.getcwd(),
        writable=fix,
        credential=env.get("FIX_BOT_TOKEN") if fix else None,
        protected=(_THIS_CHECKOUT, cfg.harness_root, cfg.state_dir, cfg.review_state_dir,
                   cfg.control_authority_root),
    )


def bind_runtime(
    cfg: HarnessConfig, role: str, runtime: RuntimeName, runner: AgentRuntime, log: Callable[[str], None],
) -> tuple[AgentRuntime, Callable[[str], object] | None]:
    """One of the role's runtimes and the per-checkout preparation its legs
    run first. Claude runs the passed SDK runner with repository guidance.
    Codex builds its own runner: fixes get the git-capable sandbox, reviews
    are read-only and carry the system prompt inline."""
    from ..adapters.repository_guidance import RepositoryGuidanceRuntime

    if runtime is RuntimeName.CLAUDE:
        return RepositoryGuidanceRuntime(runner, log=log), None
    from ..adapters.codex_home import CodexHomeRuntime, write_codex_home
    from ..adapters.codex_runner import CodexProvider, CodexRunner
    from ..adapters.process_supervisor import ProcessSupervisor

    fix = role == "fix"
    state_dir = cfg.state_dir_for(role)
    # No default model: every leg carries its live route's model, and a route
    # without one means the runtime's own default (a boot FIX_MODEL baked in
    # here outlived its unset; ADR 0007 amendment).
    codex = CodexRunner(
        codex_bin=cfg.codex_bin,
        provider=CodexProvider(provider_id=cfg.codex_provider_id, name=cfg.codex_provider_name,
                               base_url=cfg.codex_base_url, env_key=cfg.codex_env_key,
                               wire_api=cfg.codex_wire_api),
        sandbox=cfg.codex_sandbox if fix else "read-only",
        reasoning_effort=cfg.codex_reasoning_effort,
        inject_system_prompt=not fix,
        log=log,
        debug=os.environ.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False"),
        idle_timeout=cfg.agent_idle_timeout if cfg.agent_idle_timeout > 0 else None,
        max_runtime=cfg.agent_max_runtime if cfg.agent_max_runtime > 0 else None,
        process_supervisor=ProcessSupervisor(supervision_dir(cfg, role)),
        spend_evidence_dir=spend_evidence_dir(state_dir),
        rate_table=rate_table(cfg, RuntimeName.CODEX.value),
        leg_sandbox=leg_sandbox(cfg, role),
    )
    reason = codex.preflight()
    log(f"codex: NOT READY at startup — {reason}" if reason is not None
        else "codex: runtime ready (bin + key present)")
    prepare = partial(write_codex_home, agents_md=cfg.agent_md if fix else "", state_dir=state_dir,
                      auth_file=codex.oauth_auth_file())
    return CodexHomeRuntime(codex, prepare), prepare


@dataclass(frozen=True)
class Role:
    """A role's composed authorities. Acceptance tests drive this exact
    production wiring instead of a hand-copied replica that can drift."""

    daemon: Daemon
    control: SQLiteControlStore
    journal: Journal
    act: ActionExecutor
    legs: Any
    recovery: RecoveryCoordinator
    owner_id: str


_SHUTDOWN_REASON = "shutdown_deadline_spend_unknown"


def shutdown_hooks(
    control: SQLiteControlStore, spend: SpendReconciler, *, owner_id: str,
) -> tuple[Callable[[], object], Callable[[], int]]:
    """(prepare, terminalize): record cancellation intents before teardown;
    after it, reconcile killed paid work. The intents in SQLite are the
    receipts. Recording an intent releases nothing: capacity stays reserved
    until the killed run's spend is reconciled (or boot recovery settles it)."""

    def prepare() -> object:
        return control.record_owned_cancellation_intents(owner_id, _SHUTDOWN_REASON)

    def terminalize() -> int:
        intents = control.record_owned_cancellation_intents(owner_id, _SHUTDOWN_REASON)
        for intent in intents:
            spend.reconcile(intent.run_id)
        return len(intents)

    return prepare, terminalize


def compose_role(
    cfg: HarnessConfig,
    role: str,
    gateway,
    runner: AgentRuntime,
    *,
    make_legs: Callable[..., Any],
    make_act: Callable[[CancellableAsyncDispatch, SQLiteControlStore], ActionExecutor],
    choose: Choose,
    worktrees=None,
    capacity: Callable[[HarnessConfig], int] = lambda _cfg: 1,
    live: LiveConfig | None = None,
    log: Callable[[str], None],
) -> Role:
    """observe -> derive -> policy -> act, with paid legs in a bounded pool.

    Money order: the recovery coordinator exists before accounting, so an
    admission that trips a cap can sweep stale reservations (keifu #34);
    every leg reserves before spawn and settles once through the accounting
    runtime; shutdown records cancellation intents, then reconciles killed
    work.

    `live` (ADR 0007 amendment): each observation refreshes it, and the
    tick's facts, capacity and pickup, and each leg's caps, read its current
    snapshot; `cfg` (the boot snapshot) supplies everything else."""
    live = live or LiveConfig(cfg)
    from ..adapters.process_supervisor import ProcessSupervisor
    from ..adapters.runtime_switch import RuntimeSwitch

    state_dir = cfg.state_dir_for(role)
    default = cfg.fixer_runtime if role == "fix" else cfg.reviewer_runtime
    # Legs route across runtimes (FIX_MODEL_<TIER>, TRIAGE_MODEL, STEWARD_MODEL).
    names = sorted(cfg.runtimes(role), key=lambda n: n.value)
    journal = Journal(os.path.join(state_dir, "journal.jsonl"))
    control = control_store(cfg, role)
    owner_id = owner_identity(cfg, role)
    supervisor = ProcessSupervisor(supervision_dir(cfg, role))
    spend = spend_reconciler(cfg, control)
    recovery = RecoveryCoordinator(supervisor, control, spend, owner_id)

    def cap(value: float) -> Money | None:
        return Money.from_major("USD", value) if value > 0 else None

    def admission() -> AdmissionCaps:
        current = live.current
        return AdmissionCaps(cap(current.daily_spend_cap_usd), cap(current.global_daily_spend_cap_usd),
                             cap(current.issue_spend_cap_usd), current.config_version)

    posture = leg_sandbox(cfg, role).posture
    chains: dict[RuntimeName, AgentRuntime] = {}
    accountings: dict[RuntimeName, AccountingRuntime] = {}
    prepares: dict[RuntimeName, Callable[[str], object] | None] = {}
    for name in names:
        bound, prepares[name] = bind_runtime(cfg, role, name, runner, log)
        accountings[name] = AccountingRuntime(
            bound, control, cfg.repo, role, admission, name.value, budget_policy(cfg),
            rate_table(cfg, name.value), spend_evidence_dir(state_dir), owner_id=owner_id,
            reservation_reconciler=recovery,
            remote_head=worktrees.remote_head if worktrees is not None else None,
        )
        chains[name] = JournalingRuntime(accountings[name], journal, repo=cfg.repo, role=role, runtime=name.value,
                                         sandbox=posture)

    def prepare_cwd(cwd: str, runtime: RuntimeName | None = None) -> None:
        if (prepare := prepares.get(runtime or default)) is not None:
            prepare(cwd)

    legs = make_legs(runtime=RuntimeSwitch(chains, default), accounting=accountings[default], control=control,
                     owner_id=owner_id, prepare_cwd=prepare_cwd)
    dispatch = CancellableAsyncDispatch(legs.run)
    act = make_act(dispatch, control)

    prepare_shutdown, terminalize = shutdown_hooks(control, spend, owner_id=owner_id)

    # Only the fix role files base-CI issues, so only it reads base CI; the
    # review role's token pool is the rate-limited one.
    def observe() -> WorldState:
        current = live.refresh()
        if role == "fix":
            return gateway.build_world(cfg.base_branch, filed_by=cfg.fix_bot, pickup=current.pickup)
        return gateway.build_world(cfg.base_branch)

    daemon = Daemon(
        role, observe, lambda _world: [], act, cfg.poll_seconds, log,
        dispatch_capacity=lambda: capacity(live.current),
        dispatch_key=dispatch_item,
        cancel_active_dispatches=dispatch.cancel_all,
        force_kill_dispatches=supervisor.force_kill_all,
        heartbeat_path=os.path.join(state_dir, "heartbeat.json"),
        reconcile=recovery.reconcile,
        prepare_shutdown=prepare_shutdown,
        terminalize=terminalize,
    )
    leg_usd = cfg.fix_budget_usd if role == "fix" else cfg.review_budget_usd

    def facts(world: WorldState) -> Local:
        current = live.current
        return Facts(control, cfg.repo, active=lambda: daemon.active_dispatches,
                     headroom=headroom(current, control, leg_usd), lifetime_capped=lifetime_capped(current, control),
                     log=log)(world)

    harness = Harness(fix_bot=cfg.fix_bot, review_bot=cfg.review_bot)
    daemon.decide = decide_with(role, facts, harness, choose, lambda: Slots(daemon.available_dispatch_slots, daemon.available_light_slots), log)
    return Role(daemon, control, journal, act, legs, recovery, owner_id)
