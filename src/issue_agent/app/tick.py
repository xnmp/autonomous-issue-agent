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
bounded pool. `run_forever()` wraps it with the sleep and heartbeat (a periodic
alive line plus the suspend-stall warning when wall-clock elapsed far exceeds
the interval). The clock and sleep are injected so the loop is testable
without real time.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Hashable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from ..adapters.accounting_runtime import LEG_ROLES
from ..adapters.control_store import LegRun
from ..adapters.github import GhUnavailable
from ..adapters.runtime_plan import AgentRuntime
from ..domain.actions import Action, AgentKind, RunAgent
from ..domain.backoff import LegEnd, dispatched_kind
from ..domain.events import (
    DaemonStateChanged,
    GithubUnavailable,
    StallDetected,
    TelemetryEvent,
)
from ..domain.leg import Leg
from ..domain.outcome import AgentResult, LegClass, pre_admission_refusal
from ..domain.policy import question_text
from ..domain.ports import EventSink
from ..domain.recovery import RecoveryTrigger
from ..domain.refs import harness_branch_issue
from ..domain.state import DISPATCHABLE, DerivedState, Harness, ItemKey, Local, derive, issue_key, pr_key
from ..domain.supervision import (
    ClockSample,
    DaemonState,
    ExitCode,
    suspension_between,
)
from ..domain.world import WorldState
from .planners.run import PlannedRun, compose_runtime_plan

def run_item(run: LegRun) -> ItemKey | None:
    """The item a recorded leg belongs to: its issue for RESOLVE, its PR for
    PR-stage legs. Pre-5b kinds (RESUME, PR_RESUME, ROUTE) belong to no act:
    under the cutover rule those legs simply stop counting."""
    kind = dispatched_kind(run.kind)
    if kind is AgentKind.RESOLVE and run.issue > 0:
        return issue_key(run.issue)
    if kind in DISPATCHABLE and kind is not AgentKind.RESOLVE and run.number > 0:
        return pr_key(run.number)
    return None


def leg_class(run: LegRun) -> LegClass:
    """A leg's recorded class; no readable receipt is a machine end."""
    raw = run.receipt.get("leg") if run.receipt is not None else None
    return next((c for c in (LegClass.AGENT, LegClass.ASKED) if raw == c.value), LegClass.MACHINE)


def leg_end(run: LegRun) -> LegEnd | None:
    """A recorded leg as derive's `LegEnd`. A run without a `leg` receipt
    never returned (died, reclaimed, refused before launch): a machine end."""
    kind = dispatched_kind(run.kind)
    if kind not in DISPATCHABLE or run.completed_at.utcoffset() is None:
        return None
    leg = leg_class(run)
    started = run.started_at
    if started is not None and (started.utcoffset() is None or started > run.completed_at):
        started = None
    question = question_text((run.receipt or {}).get("question")) if leg is LegClass.ASKED else None
    return LegEnd(kind, run.completed_at, leg is LegClass.MACHINE, run.head, question=question, started_at=started)


def local_legs(runs: Iterable[LegRun]) -> dict[ItemKey, tuple[LegEnd, ...]]:
    out: dict[ItemKey, list[LegEnd]] = {}
    for run in runs:
        key, end = run_item(run), leg_end(run)
        if key is not None and end is not None:
            out.setdefault(key, []).append(end)
    return {key: tuple(sorted(ends, key=lambda e: e.ended_at)) for key, ends in out.items()}


def dispatch_item(action: RunAgent) -> ItemKey:
    return issue_key(action.number) if action.kind is AgentKind.RESOLVE else pr_key(action.number)


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

    def held_issues(self) -> set[int]:
        repo = self.repository
        held = {c.target_key for role in LEG_ROLES for c in self.control.active_dispatch_claims(repo, role)}
        for lease in self.control.leases():
            try:
                dispatch = json.loads(lease.dispatch_id)
            except (TypeError, ValueError):
                continue
            if isinstance(dispatch, dict) and dispatch.get("role") in LEG_ROLES and dispatch.get("repository") == repo:
                held.add(dispatch.get("issue"))
        return held

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
            legs=local_legs(runs),
            leases=leased_items(active, self.held_issues()),
            headroom=capped is not None and self.headroom(),
            lifetime_capped=capped or frozenset(),
        )


Choose = Callable[[WorldState, Mapping[ItemKey, DerivedState], int], Iterable[Action]]


def decide_with(
    role: str,
    facts: Callable[[WorldState], Local],
    harness: Harness,
    choose: Choose,
    slots: Callable[[], int],
    log: Callable[[str], None],
) -> Callable[[WorldState], list[Action]]:
    """derive once, then the role's `choose` keeps its Actions (with at most
    `slots` paid dispatches)."""
    def decide(world: WorldState) -> list[Action]:
        chosen = list(choose(world, derive(world, facts(world), harness), slots()))
        for action in chosen:
            log(f"{role}: {action!r}")
        return chosen
    return decide


def lease_slots(daemon_ref: Mapping[str, object]) -> Callable[[], int]:
    def slots() -> int:
        daemon = daemon_ref.get("daemon")
        return int(getattr(daemon, "available_dispatch_slots", 0)) if daemon is not None else 0
    return slots


def _ignore_log(_message: str) -> None:
    pass


class ActionExecutor(Protocol):
    """What the loop needs from a role's executor."""

    def execute(self, action: Action) -> None: ...


DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 80.0
DEFAULT_RESERVATION_SWEEP_TICKS = 30
_SHUTDOWN_ESCALATION_RESERVE_SECONDS = 10.0
_SHUTDOWN_PREPARE_RESERVE_SECONDS = 2.0
_SHUTDOWN_TERMINALIZE_RESERVE_SECONDS = 3.0


def order_for_execution(actions: list[Action]) -> list[Action]:
    """Cheap actions first; agent dispatches last (stable partition).

    A merge or relabel must never wait behind paid work: observed twice on the
    first real deployment, PR #533 sat APPROVED+green across two daemon
    lifetimes behind other issues' resolves, and the first post-restart tick
    on 2026-07-26 dispatched RESOLVE #500 ahead of merging #537. Stable within
    each group, so dispatches keep decide's order."""
    cheap = [a for a in actions if not isinstance(a, RunAgent)]
    dispatches = [a for a in actions if isinstance(a, RunAgent)]
    return [*cheap, *dispatches]


@dataclass
class Daemon:
    name: str
    observe: Callable[[], WorldState]
    decide: Callable[[WorldState], list[Action]]
    executor: ActionExecutor
    poll_seconds: int = 60
    log: Callable[[str], None] = _ignore_log
    # Optional typed telemetry sink. When set, skipped ticks become countable
    # events rather than lines in a log nobody retained.
    journal: EventSink | None = None
    # Paid dispatches run behind the observation loop in a worker pool of
    # this many threads.
    dispatch_capacity: int = 1
    # Stable identity for duplicate suppression. Fix wiring maps every issue-
    # and PR-keyed leg to its underlying issue; the generic fallback prevents
    # an identical RunAgent from being submitted twice.
    dispatch_key: Callable[[RunAgent], Hashable] = (
        lambda action: (action.kind, action.number)
    )
    # Fix wiring owns the asyncio tasks underneath executor worker threads.
    # This hook cancels them on operator/fatal shutdown before the pool joins.
    cancel_active_dispatches: Callable[[], None] | None = None
    force_kill_dispatches: Callable[[], object] | None = None
    heartbeat_path: str = ""
    # Total application deadline, including forced teardown and reconciliation.
    # The systemd hard stop is 90s, leaving 10s for interpreter/supervisor exit.
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    # Clock for the shutdown deadline. Tests inject a controlled clock so
    # deadline expiry is a decision, not a race against thread scheduling.
    monotonic: Callable[[], float] = time.monotonic
    # ADR 0006: callers emit semantic triggers; the coordinator selects arms.
    reconcile: Callable[[RecoveryTrigger], object] | None = None
    reservation_sweep_ticks: int = DEFAULT_RESERVATION_SWEEP_TICKS
    prepare_shutdown: Callable[[], object] | None = None
    terminalize: Callable[[], object] | None = None
    _state: DaemonState = field(
        default=DaemonState.STARTING, init=False, repr=False
    )
    _dispatch_pool: ThreadPoolExecutor = field(init=False, repr=False)
    _dispatch_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _shutdown_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _dispatches: dict[Future[None], tuple[RunAgent, Hashable]] = field(
        default_factory=dict, init=False, repr=False
    )
    _accept_dispatches: bool = field(default=True, init=False, repr=False)
    _dispatch_closed: bool = field(default=False, init=False, repr=False)
    _retry_after_seconds: float | None = field(default=None, init=False, repr=False)
    _shutdown_deadline: float | None = field(default=None, init=False, repr=False)
    _requires_hard_exit: bool = field(default=False, init=False, repr=False)
    _shutdown_prepared: bool = field(default=False, init=False, repr=False)
    _shutdown_prepare_clean: bool = field(default=True, init=False, repr=False)
    _shutdown_terminalization_required: bool = field(
        default=False, init=False, repr=False
    )
    _terminalized: bool = field(default=False, init=False, repr=False)
    _terminalize_clean: bool = field(default=True, init=False, repr=False)
    _wake_recovery_pending: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.dispatch_capacity < 1:
            raise ValueError("dispatch_capacity must be >= 1")
        self._dispatch_pool = ThreadPoolExecutor(
            max_workers=self.dispatch_capacity,
            thread_name_prefix=f"{self.name}-dispatch",
        )
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be positive")
        if self.reservation_sweep_ticks <= 0:
            raise ValueError("reservation sweep cadence must be positive")

    @property
    def state(self) -> DaemonState:
        return self._state

    @property
    def requires_hard_exit(self) -> bool:
        """Whether bounded cleanup left threads that can block interpreter exit."""

        return self._requires_hard_exit

    def _transition(self, state: DaemonState, reason: str = "") -> None:
        allowed = {
            DaemonState.STARTING: {DaemonState.RUNNING, DaemonState.DRAINING},
            DaemonState.RUNNING: {DaemonState.DRAINING},
            DaemonState.DRAINING: {DaemonState.STOPPED},
            DaemonState.STOPPED: set(),
        }
        if state is self._state:
            return
        if state not in allowed[self._state]:
            raise RuntimeError(
                f"invalid daemon transition {self._state.value} -> {state.value}"
            )
        previous = self._state
        self._state = state
        self.log(
            f"{self.name}: state {previous.value} -> {state.value}"
            + (f" ({reason})" if reason else "")
        )
        if self.journal is not None:
            self.journal.emit(
                TelemetryEvent(
                    DaemonStateChanged(previous.value, state.value, reason)
                )
            )

    def _write_heartbeat(self) -> None:
        if not self.heartbeat_path:
            return
        parent = os.path.dirname(self.heartbeat_path)
        try:
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            temporary = f"{self.heartbeat_path}.{os.getpid()}.tmp"
            payload = {
                "pid": os.getpid(),
                "state": self._state.value,
                "in_flight": [
                    {"kind": action.kind.name, "number": action.number}
                    for action in self.active_dispatches
                ],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.heartbeat_path)
        except OSError as exc:
            self.log(f"{self.name}: heartbeat write failed: {exc}")

    @property
    def active_dispatches(self) -> tuple[RunAgent, ...]:
        """A stable snapshot of paid work currently in flight."""
        return tuple(action for action, _key in self.active_dispatch_items)

    @property
    def active_dispatch_items(self) -> tuple[tuple[RunAgent, Hashable], ...]:
        """Actions plus the stable issue identity resolved at submission."""
        with self._dispatch_lock:
            return tuple(self._dispatches.values())

    @property
    def available_dispatch_slots(self) -> int:
        """Capacity visible to the decision layer.

        Pending recovery or hot reload closes admission while existing work
        drains, so the domain never emits a claim relabel for work this process
        will not start.
        """
        with self._dispatch_lock:
            if (
                not self._accept_dispatches
                or self._dispatch_closed
                or self._wake_recovery_pending
            ):
                return 0
            return max(0, self.dispatch_capacity - len(self._dispatches))

    def _dispatch_done(self, future: Future[None]) -> None:
        with self._dispatch_lock:
            entry = self._dispatches.pop(future, None)
        if entry is None:
            return
        action, _key = entry
        if future.cancelled():
            self.log(f"{self.name}: action {action!r} was cancelled")
            return
        exc = future.exception()
        if exc is not None:
            self.log(f"{self.name}: action {action!r} failed: {exc}")

    def _submit_dispatch(self, action: RunAgent) -> bool:
        key = self.dispatch_key(action)
        with self._dispatch_lock:
            active_keys = {active_key for _action, active_key in self._dispatches.values()}
            if (
                not self._accept_dispatches
                or self._dispatch_closed
                or self._wake_recovery_pending
                or len(self._dispatches) >= self.dispatch_capacity
                or key in active_keys
            ):
                return False
            future = self._dispatch_pool.submit(self.executor.execute, action)
            self._dispatches[future] = (action, key)
        # A Future that already completed invokes callbacks synchronously from
        # add_done_callback; register outside the lock so that fast fake/runtime
        # dispatches cannot deadlock while removing themselves.
        future.add_done_callback(self._dispatch_done)
        return True

    def wait_for_dispatches(self) -> None:
        """Wait for the current bounded set without closing future admission."""
        with self._dispatch_lock:
            pending = tuple(self._dispatches)
        if pending:
            wait(pending)

    def shutdown_dispatches(self, *, cancel: bool) -> bool:
        """Close admission, optionally cancel active async work, and join.

        ThreadPoolExecutor workers are non-daemon. Joining them explicitly is
        therefore part of the daemon's shutdown contract, not merely resource
        tidying: without cancellation Ctrl-C leaves the child agent session
        alive and interpreter shutdown waits for its worker indefinitely.
        """
        with self._shutdown_lock:
            return self._shutdown_dispatches(cancel=cancel)

    def _shutdown_dispatches(self, *, cancel: bool) -> bool:
        pool = self._dispatch_pool
        self._shutdown_terminalization_required |= cancel
        if self._shutdown_deadline is None:
            self._shutdown_deadline = (
                self.monotonic() + self.shutdown_timeout_seconds
            )
        with self._dispatch_lock:
            if self._dispatch_closed:
                pending = tuple(self._dispatches)
                if pending:
                    return False
                return self._finish_shutdown()
            self._dispatch_closed = True
            self._accept_dispatches = False
        if cancel:
            self._prepare_shutdown()
        if cancel and self.cancel_active_dispatches is not None:
            try:
                self.cancel_active_dispatches()
            except Exception as exc:  # noqa: BLE001 - shutdown must still join
                self.log(f"{self.name}: dispatch cancellation failed: {exc}")
        with self._dispatch_lock:
            pending = tuple(self._dispatches)
        graceful_budget = max(
            0.0,
            self._remaining_shutdown_time()
            - min(
                _SHUTDOWN_ESCALATION_RESERVE_SECONDS,
                self.shutdown_timeout_seconds * 0.5,
            ),
        )
        _done, unfinished = wait(pending, timeout=graceful_budget)
        if unfinished:
            self.log(
                f"{self.name}: shutdown deadline expired with "
                f"{len(unfinished)} dispatch(es) still active"
            )
            self._shutdown_terminalization_required = True
            # Persist cancellation intent before teardown.
            self._prepare_shutdown()
            terminalize_reserve = min(
                _SHUTDOWN_TERMINALIZE_RESERVE_SECONDS,
                self.shutdown_timeout_seconds * 0.2,
            )
            terminalized = False
            if self.force_kill_dispatches is not None:
                killed = self._bounded_shutdown_call(
                    self.force_kill_dispatches,
                    "forced process teardown",
                    max_seconds=max(
                        0.0,
                        self._remaining_shutdown_time() - terminalize_reserve,
                    ),
                )
                if killed:
                    # A killed provider flushes its final rollout evidence while
                    # the worker unwinds. The worker must settle before spend
                    # reconciliation: it consumes state the dispatch is still
                    # authorized to write.
                    _done, unfinished = wait(
                        unfinished,
                        timeout=max(
                            0.0,
                            self._remaining_shutdown_time() - terminalize_reserve,
                        ),
                    )
                    if not unfinished:
                        terminalized = self._terminalize()
        else:
            terminalized = (
                self._terminalize()
                if self._shutdown_terminalization_required
                else True
            )
        pool.shutdown(wait=False, cancel_futures=cancel)
        if unfinished:
            return False
        return self._shutdown_prepare_clean and terminalized

    def _finish_shutdown(self) -> bool:
        """Finish deferred cleanup after every dispatch owner has settled."""

        terminalized = (
            self._terminalize()
            if self._shutdown_terminalization_required
            else True
        )
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
        self._shutdown_prepare_clean = self._bounded_shutdown_call(
            self.prepare_shutdown,
            "shutdown intent persistence",
            max_seconds=max(
                0.0,
                self._remaining_shutdown_time()
                - min(
                    _SHUTDOWN_PREPARE_RESERVE_SECONDS,
                    self.shutdown_timeout_seconds * 0.2,
                ),
            ),
        )
        return self._shutdown_prepare_clean

    def _bounded_shutdown_call(
        self,
        callback: Callable[[], object],
        description: str,
        *,
        max_seconds: float | None = None,
    ) -> bool:
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

        worker = threading.Thread(
            target=invoke,
            name=f"{self.name}-shutdown-{description.replace(' ', '-')}",
            daemon=True,
        )
        worker.start()
        completed.wait(timeout=budget)
        if not completed.is_set():
            self.log(f"{self.name}: {description} exceeded the shutdown deadline")
            return False
        if failures:
            self.log(f"{self.name}: {description} failed: {failures[0]}")
            return False
        return True

    def _reconcile_wake(self) -> None:
        """Run wake recovery only after live dispatch owners have settled."""

        if self.reconcile is None:
            return
        if self.active_dispatches:
            if not self._wake_recovery_pending:
                self.log(
                    f"{self.name}: wake recovery deferred until active "
                    "dispatches settle"
                )
            self._wake_recovery_pending = True
            return
        self.reconcile(RecoveryTrigger.WAKE)
        self._wake_recovery_pending = False

    def tick(
        self,
        scheduler_progress: Callable[[], None] | None = None,
    ) -> list[Action]:
        """One observe→decide→execute pass. Returns the actions taken (for tests
        / logging). Never raises out: a failed pass logs and yields [] so the
        daemon loop continues (the bash `poll_once || log` contract).

        `GhUnavailable` (rate limit / network / 5xx) gets its own arm: it is not
        a bug to debug from a stack-ish dump but an expected operating condition
        with exactly one correct response — observe NOTHING, decide NOTHING,
        execute NOTHING, say so in one loud line. There is deliberately no retry
        or backoff here: the poll loop IS the retry, and a rate limit that
        outlives one interval will simply announce itself again."""
        if scheduler_progress is not None:
            scheduler_progress()
        try:
            world = self.observe()
            if scheduler_progress is not None:
                scheduler_progress()
            actions = self.decide(world)
        except GhUnavailable as exc:
            self._retry_after_seconds = exc.retry_after_s
            self.log(
                f"{self.name}: world build failed: GitHub {exc.headline} — "
                f"skipping tick, no actions taken ({exc.detail})"
            )
            # Journalled too: a run of these is the difference between "the
            # harness did nothing because there was nothing to do" and "the
            # harness could not see" — indistinguishable from actions alone.
            if self.journal is not None:
                self.journal.emit(
                    TelemetryEvent(
                        GithubUnavailable(
                            exc.kind,
                            exc.detail,
                            (
                                int(exc.retry_after_s * 1000)
                                if exc.retry_after_s is not None
                                else None
                            ),
                        )
                    )
                )
            return []
        except Exception as exc:  # noqa: BLE001 - crash-isolate a poll
            self.log(f"{self.name}: poll pass failed (continuing): {exc}")
            return []
        taken: list[Action] = []
        for action in order_for_execution(actions):
            try:
                if isinstance(action, RunAgent):
                    if self._submit_dispatch(action):
                        taken.append(action)
                    else:
                        self.log(
                            f"{self.name}: dispatch {action.kind.name} "
                            f"#{action.number} not submitted — slot full, "
                            "duplicate target, or recovery/reload draining"
                        )
                else:
                    taken.append(action)
                    self.executor.execute(action)
            except Exception as exc:  # noqa: BLE001 - one bad action ≠ dead daemon
                self.log(f"{self.name}: action {action!r} failed: {exc}")
            finally:
                if scheduler_progress is not None:
                    scheduler_progress()
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
        """Loop tick→sleep with a heartbeat. `now`/`sleep` injected (testable).
        `max_ticks` bounds the loop for tests; None = forever.

        `reload_check` (hot reload): consulted between ticks. A concurrent fixer
        closes admission on the first positive result, keeps observing while its
        paid work drains, then exits with RELOAD_EXIT_CODE for point-at."""
        self.log(f"{self.name}: watching (poll {self.poll_seconds}s)")
        self._transition(DaemonState.RUNNING)
        last = now()
        last_active = active_now()
        last_logged = last
        ticks = 0
        reload_pending = False
        try:
            while max_ticks is None or ticks < max_ticks:
                cur = now()
                cur_active = active_now()
                if self._wake_recovery_pending:
                    self._reconcile_wake()
                elapsed = cur - last
                previous = last
                previous_active = last_active
                last = cur
                last_active = cur_active
                # A late wall heartbeat is a stall only when wall time advanced
                # beyond monotonic active time. Synchronous paid legs routinely
                # exceed the wall threshold and must not generate incident records
                # or replay lifecycle state merely because they were productive.
                suspended = round(
                    suspension_between(
                        ClockSample(previous, previous_active),
                        ClockSample(cur, cur_active),
                    ).suspended_seconds
                )
                if elapsed >= self.poll_seconds * 4 and suspended > 0:
                    stall_started = datetime.fromtimestamp(
                        cur - suspended, tz=timezone.utc
                    )
                    woke_at = datetime.fromtimestamp(cur, tz=timezone.utc)
                    # wall-minus-monotonic is stable across processes on one boot
                    # and advances by the suspension amount. Unlike a wall-time
                    # start estimate, it is not shifted by work after resume, so
                    # dashboard readers can identify one box stall across roles.
                    stall_clock_offset = round(cur - cur_active, 3)
                    self.log(
                        f"{self.name}: ♥ woke after {elapsed}s "
                        f"(expected ~{self.poll_seconds}s) "
                        "— likely suspended/stalled; catching up"
                    )
                    if self.journal is not None:
                        self.journal.emit(
                            TelemetryEvent(
                                StallDetected(
                                    (
                                        f"{self.name}:"
                                        f"{stall_clock_offset:g}:"
                                        f"{uuid.uuid4().hex}"
                                    ),
                                    stall_started.isoformat(),
                                    woke_at.isoformat(),
                                    int(suspended * 1000),
                                    int(elapsed * 1000),
                                    int(stall_clock_offset * 1000),
                                )
                            )
                        )
                    self._reconcile_wake()
                    last_logged = cur
                elif cur - last_logged >= heartbeat_seconds:
                    self.log(f"{self.name}: ♥ alive (poll {self.poll_seconds}s)")
                    last_logged = cur
                self.tick(scheduler_progress)
                self._write_heartbeat()
                if scheduler_progress is not None:
                    scheduler_progress()
                ticks += 1
                if (
                    self.reconcile is not None
                    and ticks % self.reservation_sweep_ticks == 0
                ):
                    try:
                        self.reconcile(RecoveryTrigger.PERIODIC)
                    except Exception as exc:
                        self.log(
                            f"{self.name}: reservation sweep failed "
                            f"(continuing): {exc}"
                        )
                reload_requested = reload_check is not None and reload_check()
                if reload_requested and self.active_dispatches:
                    if not reload_pending:
                        self.log(
                            f"{self.name}: hot-reload requested — draining "
                            f"{len(self.active_dispatches)} paid dispatch(es) while "
                            "observation continues"
                        )
                    reload_pending = True
                    with self._dispatch_lock:
                        self._accept_dispatches = False
                if (reload_requested or reload_pending) and not self.active_dispatches:
                    from ..adapters.reload import RELOAD_EXIT_CODE

                    self.log(
                        f"{self.name}: hot-reload — harness source changed; "
                        "restarting with no session in flight"
                    )
                    raise SystemExit(RELOAD_EXIT_CODE)
                self._sleep_interval(sleep, scheduler_progress)
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

    def _terminalize(self) -> bool:
        if self._terminalized:
            return self._terminalize_clean
        self._terminalized = True
        if self.terminalize is None:
            return True
        self._terminalize_clean = self._bounded_shutdown_call(
            self.terminalize,
            "durable run terminalization",
            max_seconds=self._remaining_shutdown_time(),
        )
        return self._terminalize_clean

    def _sleep_interval(
        self,
        sleep: Callable[[int], None],
        scheduler_progress: Callable[[], None] | None = None,
    ) -> None:
        """Sleep one poll interval. A notifier slices the wait to keep a
        systemd watchdog alive; without one this is one sleep call."""
        interval = max(
            self.poll_seconds,
            int(self._retry_after_seconds or 0),
        )
        self._retry_after_seconds = None
        if scheduler_progress is None:
            sleep(interval)
            return
        remaining = interval
        while remaining > 0:
            step = min(30, remaining)
            sleep(step)
            remaining -= step
            scheduler_progress()


async def run_leg(
    runner: AgentRuntime,
    log: Callable[[str], None],
    leg: Leg,
    task: PlannedRun,
    *,
    scope: AbstractContextManager[object] | None = None,
) -> AgentResult:
    """The single path for executing and logging an agent leg."""
    target = leg.number or 0
    label = leg.kind.casefold().replace("_", "-")
    log(f"agent #{target}: {label} — running")
    with scope or contextlib.nullcontext():
        result = await runner.run(compose_runtime_plan(task))
    status = result.status or ("error" if result.is_error else "ok")
    log(
        f"agent #{target}: {label} — done "
        f"(status={status}{f', ${result.cost_usd:.2f}' if result.cost_usd else ''}"
        # The refusal's summary names the missing authority (#262).
        f"{f', {result.summary[:300]}' if pre_admission_refusal(result) else ''})"
    )
    return result



__all__ = [
    "ActionExecutor", "Choose", "Daemon", "Facts", "decide_with", "dispatch_item", "item_issues",
    "lease_slots", "leased_items", "leg_class", "leg_end", "local_legs", "order_for_execution",
    "run_item", "run_leg",
]
