"""ADR 0010 reconciliation loop — observe → decide → execute → sleep.

Replaces the three near-identical bash `while true; do … sleep; done` loops
(watcher.sh / review-watcher.sh / steward.sh). A `Daemon` is parameterised by:
  * `observe()`  → WorldState (gateway reads),
  * `decide(world)` → [Action] (a pure domain decision function, bound to config),
  * an `Executor` to apply them.

`tick()` is the observe→decide→submit unit and is what tests drive. Cheap
actions execute before it returns; configured paid dispatches continue in a
bounded pool. `run_forever()` wraps it with the sleep + heartbeat (periodic alive line +
the suspend-stall warning when wall-clock elapsed >> interval — the lesson from
the laptop-suspend stall). The clock + sleep are injected so the loop is testable
without real time.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import threading
import time
import uuid

from ..adapters.github import GhUnavailable
from ..domain.actions import Action, Relabel, RunAgent
from ..domain.events import (
    DaemonStateChanged,
    GithubUnavailable,
    StallDetected,
    TelemetryEvent,
    TickGated,
)
from ..domain.recovery import RecoveryTrigger
from ..domain.supervision import (
    ClockSample,
    DaemonState,
    ExitCode,
    suspension_between,
)
from ..domain.world import WorldState
from .execute import Executor
from .world_probe import WorldUnchanged


def _ignore_log(_message: str) -> None:
    pass


DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 80.0
DEFAULT_RESERVATION_SWEEP_TICKS = 30
_SHUTDOWN_ESCALATION_RESERVE_SECONDS = 10.0
_SHUTDOWN_PREPARE_RESERVE_SECONDS = 2.0
_SHUTDOWN_FINALIZE_RESERVE_SECONDS = 5.0
_SHUTDOWN_TERMINALIZE_RESERVE_SECONDS = 3.0


def order_for_execution(actions: list[Action]) -> list[Action]:
    """Cheap, terminal actions first; agent dispatches last (stable partition).

    Historically a RunAgent blocked the tick for the whole session, so any
    merge/relabel/reap sequenced after one waited that long for a ~1s gh call.
    Background dispatch removes the ongoing blind spot, but cheap-first remains
    the atomic claim invariant: lifecycle/claim writes land before work starts.
    Observed twice on the first real
    deployment: PR #533 sat APPROVED+green across two daemon lifetimes behind
    other issues' resolves, and the first post-restart tick on 2026-07-26
    dispatched RESOLVE #500 ahead of arming merge-ready #537 — the more work
    the harness has, the longer FINISHED work sits unmerged, which is exactly
    backwards.

    Stable within each group: a dispatch's own preceding relabel (ai-ready →
    ai-working) still executes before it, and multiple dispatches keep decide's
    order. Nothing in a tick's list depends on a dispatch completing before a
    cheap action — post-run consequences live in the workflow tails, not here."""
    cheap = [a for a in actions if not isinstance(a, RunAgent)]
    dispatches = [a for a in actions if isinstance(a, RunAgent)]
    return [*cheap, *dispatches]


def dispatch_claims(actions: list[Action]) -> frozenset[Relabel]:
    """Claims that exist solely to authorize a paired paid dispatch."""

    return frozenset(
        action.requires
        for action in actions
        if isinstance(action, RunAgent) and action.requires is not None
    )


@dataclass
class Daemon:
    name: str
    observe: Callable[[], WorldState]
    decide: Callable[[WorldState], list[Action]]
    executor: Executor
    poll_seconds: int = 60
    log: Callable[[str], None] = _ignore_log
    # Optional typed telemetry sink. When set, skipped ticks become countable
    # events rather than lines in a log nobody retained.
    journal: object | None = None
    # Optional pre-observe gate (app.spend_gate). Returning a reason string
    # means "this whole tick is pointless" — checked BEFORE observe so a
    # level-triggered halt costs no GraphQL quota and writes no labels (#27).
    # None (the field, or the return) = no gate.
    tick_gate: Callable[[], str | None] | None = None
    # Optional push wake (adapters.wake.WakeSignal.pending). When set, the
    # inter-tick sleep is served in ≤1s slices and ends early the moment a
    # webhook delivery lands, which demotes `poll_seconds` from "how fast the
    # harness notices" to "the reconciler backstop". None = today's behaviour:
    # ONE sleep(poll_seconds) call, unsliced.
    wake_check: Callable[[], bool] | None = None
    # Paid dispatches may run behind the observation loop. Zero preserves the
    # synchronous behavior used by review and by existing hand-built
    # daemons; a positive value bounds the background worker pool.
    dispatch_capacity: int = 0
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
    # Decision-side path leases are acquired before lifecycle writes execute.
    # Any dispatch that never reaches the runtime must release that lease here.
    dispatch_rejected: Callable[[RunAgent], None] | None = None
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
    finalize: Callable[[], object] | None = None
    terminalize: Callable[[], object] | None = None
    _state: DaemonState = field(
        default=DaemonState.STARTING, init=False, repr=False
    )
    _gated_on: str | None = field(default=None, init=False, repr=False)
    _dispatch_pool: ThreadPoolExecutor | None = field(
        default=None, init=False, repr=False
    )
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
    _finalized: bool = field(default=False, init=False, repr=False)
    _finalize_clean: bool = field(default=True, init=False, repr=False)
    _terminalized: bool = field(default=False, init=False, repr=False)
    _terminalize_clean: bool = field(default=True, init=False, repr=False)
    _wake_recovery_pending: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.dispatch_capacity < 0:
            raise ValueError("dispatch_capacity must be >= 0")
        if self.dispatch_capacity:
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
        if self._dispatch_pool is None:
            return 1
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
        pool = self._dispatch_pool
        if pool is None:
            self.executor.execute(action)
            return True
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
            future = pool.submit(self.executor.execute, action)
            self._dispatches[future] = (action, key)
        # A Future that already completed invokes callbacks synchronously from
        # add_done_callback; register outside the lock so that fast fake/runtime
        # dispatches cannot deadlock while removing themselves.
        future.add_done_callback(self._dispatch_done)
        return True

    def _release_rejected_dispatch(self, action: RunAgent) -> None:
        if self.dispatch_rejected is None:
            return
        try:
            self.dispatch_rejected(action)
        except Exception as exc:  # noqa: BLE001 - rejection still stands
            self.log(
                f"{self.name}: rejected dispatch cleanup failed for "
                f"{action!r}: {exc}"
            )

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
        if pool is None:
            return self._finalize()
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
            # Persist cancellation intent before teardown, but do not replay
            # lifecycle commands while the dispatch that owns them can still
            # be applying or receipting the same remote effect.
            self._prepare_shutdown()
            finalize_reserve = min(
                _SHUTDOWN_FINALIZE_RESERVE_SECONDS,
                self.shutdown_timeout_seconds * 0.2,
            )
            terminalize_reserve = min(
                _SHUTDOWN_TERMINALIZE_RESERVE_SECONDS,
                self.shutdown_timeout_seconds * 0.2,
            )
            finalized = False
            terminalized = False
            if self.force_kill_dispatches is not None:
                killed = self._bounded_shutdown_call(
                    self.force_kill_dispatches,
                    "forced process teardown",
                    max_seconds=max(
                        0.0,
                        self._remaining_shutdown_time()
                        - finalize_reserve
                        - terminalize_reserve,
                    ),
                )
                if killed:
                    # A killed provider flushes its final rollout evidence while
                    # the worker unwinds. The worker must settle before either
                    # lifecycle replay or spend reconciliation: both consume
                    # state the dispatch is still authorized to write.
                    _done, unfinished = wait(
                        unfinished,
                        timeout=max(
                            0.0,
                            self._remaining_shutdown_time()
                            - finalize_reserve
                            - terminalize_reserve,
                        ),
                    )
                    if not unfinished:
                        finalized = self._finalize(
                            reserve_seconds=terminalize_reserve,
                        )
                        terminalized = (
                            self._terminalize() if finalized else False
                        )
        else:
            finalized = self._finalize()
            terminalized = (
                self._terminalize()
                if self._shutdown_terminalization_required and finalized
                else not self._shutdown_terminalization_required
            )
        pool.shutdown(wait=False, cancel_futures=cancel)
        if unfinished:
            return False
        return self._shutdown_prepare_clean and finalized and terminalized

    def _finish_shutdown(self) -> bool:
        """Finish deferred cleanup after every dispatch owner has settled."""

        finalized = self._finalize()
        terminalized = (
            self._terminalize()
            if self._shutdown_terminalization_required and finalized
            else not self._shutdown_terminalization_required
        )
        return self._shutdown_prepare_clean and finalized and terminalized

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

    def _gate_blocks_this_tick(self) -> bool:
        """True when `tick_gate` says to skip. Edge-triggered logging: a level
        that stays tripped for hours must not print a line every poll, and the
        clearing edge is the line an operator actually waits for.

        Fails closed for paid admission: an accounting read error must never be
        interpreted as zero spend. Observation and free reconciliation continue."""
        if self.tick_gate is None:
            return False
        try:
            reason = self.tick_gate()
        except Exception as exc:  # noqa: BLE001 - a broken gate ≠ a stopped daemon
            reason = f"ACCOUNTING UNAVAILABLE — {exc}"
        if reason is None:
            if self._gated_on is not None:
                self.log(f"{self.name}: resuming — {self._gated_on} no longer applies")
                self._gated_on = None
            return False
        if reason != self._gated_on:
            self.log(f"{self.name}: {reason}")
            self._gated_on = reason
            if self.journal is not None:
                self.journal.emit(TelemetryEvent(TickGated(reason)))
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
        # Lifecycle writes are the state machine's acknowledgements. Replay them
        # before a world read, reaper decision, or fresh paid dispatch; bounded
        # unverifiable entries are quarantined so unrelated work can continue.
        # A spend gate must not strand the write that explains already-spent work.
        if not self.executor.replay_pending():
            self.log(
                f"{self.name}: pending lifecycle actions remain — "
                "skipping observe/dispatch this tick"
            )
            return []
        if scheduler_progress is not None:
            scheduler_progress()
        paid_blocked = self._gate_blocks_this_tick()
        try:
            world = self.observe()
            if scheduler_progress is not None:
                scheduler_progress()
            actions = self.decide(world)
        except WorldUnchanged:
            # The probe proved this tick would observe the world the last full
            # build already observed; re-deciding it re-derives the same actions.
            # Deliberately SILENT — a line every quiet poll is exactly the
            # heartbeat noise the probe exists to remove, and run_forever's ♥
            # already proves liveness.
            return []
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
        blocked_claims = dispatch_claims(actions) if paid_blocked else frozenset()
        prepare_cleanup = getattr(self.executor, "prepare_cleanup", None)
        if prepare_cleanup is not None:
            try:
                actions = prepare_cleanup(actions)
            except ValueError as error:
                self.log(f"{self.name}: cleanup recipe refused before effects: {error}")
                return []
        ordered = order_for_execution(actions)
        taken: list[Action] = []
        completed_claims: set[Relabel] = set()
        for action in ordered:
            try:
                if isinstance(action, Relabel) and action in blocked_claims:
                    continue
                if isinstance(action, RunAgent):
                    if (
                        action.requires is not None
                        and action.requires not in completed_claims
                    ):
                        self._release_rejected_dispatch(action)
                        self.log(
                            f"{self.name}: dispatch {action.kind.name} "
                            f"#{action.number} withheld — claim did not complete"
                        )
                        continue
                    if paid_blocked:
                        self._release_rejected_dispatch(action)
                        self.log(
                            f"{self.name}: paid dispatch {action.kind.name} "
                            f"#{action.number} blocked; free tick work completed"
                        )
                        continue
                    if self._dispatch_pool is None:
                        # Preserve the synchronous tick contract: the decision
                        # was taken even when its execution raises and is crash-
                        # isolated below (convergence accounting counts paid
                        # attempts, including pre-result deaths).
                        taken.append(action)
                        self.executor.execute(action)
                    elif self._submit_dispatch(action):
                        taken.append(action)
                    else:
                        self._release_rejected_dispatch(action)
                        self.log(
                            f"{self.name}: dispatch {action.kind.name} "
                            f"#{action.number} not submitted — slot full, "
                            "duplicate target, or recovery/reload draining"
                        )
                else:
                    taken.append(action)
                    self.executor.execute(action)
                    if isinstance(action, Relabel):
                        completed_claims.add(action)
            except Exception as exc:  # noqa: BLE001 - one bad action ≠ dead daemon
                if isinstance(action, RunAgent) and self._dispatch_pool is not None:
                    # Includes pool.submit failures: the runtime never acquired
                    # ownership, so its finally cannot release decision leases.
                    self._release_rejected_dispatch(action)
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
        mode = (
            f"webhook wake + reconcile {self.poll_seconds}s"
            if self.wake_check is not None
            else f"poll {self.poll_seconds}s"
        )
        self.log(f"{self.name}: watching ({mode})")
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
                active_elapsed = cur_active - last_active
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

    def _finalize(self, *, reserve_seconds: float = 0.0) -> bool:
        if self._finalized:
            return self._finalize_clean
        self._finalized = True
        if self.finalize is None:
            return True
        if self._shutdown_deadline is None:
            self._shutdown_deadline = (
                self.monotonic() + self.shutdown_timeout_seconds
            )
        self._finalize_clean = self._bounded_shutdown_call(
            self.finalize,
            "shutdown reconciliation",
            max_seconds=max(
                0.0,
                self._remaining_shutdown_time() - reserve_seconds,
            ),
        )
        return self._finalize_clean

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
        """Sleep one poll interval, cut short by a push wake if one is wired.

        A notifier slices even quiet/retry-after waits to keep a systemd
        watchdog alive. A wake check uses ≤1s slices and returns on delivery.
        Without either callback this remains one sleep call for compatibility."""
        interval = max(
            self.poll_seconds,
            int(self._retry_after_seconds or 0),
        )
        self._retry_after_seconds = None
        if self.wake_check is None and scheduler_progress is None:
            sleep(interval)
            return
        wake_check = self.wake_check
        remaining = interval
        while remaining > 0:
            step_limit = 1 if wake_check is not None else 30
            step = min(step_limit, remaining)
            sleep(step)
            remaining -= step
            if scheduler_progress is not None:
                scheduler_progress()
            if wake_check is None:
                continue
            try:
                woken = wake_check()
            except Exception as exc:  # noqa: BLE001 - a broken wake ≠ a dead daemon
                self.log(f"{self.name}: wake check failed (polling on): {exc}")
                # Disable probing for this interval, but keep the remainder
                # sliced so the service watchdog still sees scheduler liveness.
                wake_check = None
                continue
            if woken:
                return
