"""Generic daemon loop — observe → decide → execute → sleep, with heartbeat.

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
from datetime import datetime, timedelta, timezone
import threading
import time

from ..adapters.github import GhUnavailable
from ..adapters.journal import read_events_tail, stalled_run_annotations
from ..domain.actions import Action, RunAgent
from ..domain.world import WorldState
from .execute import Executor
from .world_probe import WorldUnchanged


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


@dataclass
class Daemon:
    name: str
    observe: Callable[[], WorldState]
    decide: Callable[[WorldState], list[Action]]
    executor: Executor
    poll_seconds: int = 60
    log: Callable[[str], None] = lambda msg: None
    # Optional JSONL journal (adapters.journal.Journal). Default None keeps every
    # existing construction working; when set, the skipped-tick arm becomes a
    # countable event rather than a line in a log nobody kept.
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
    # synchronous behavior used by review/steward and by existing hand-built
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
    # Decision-side path leases are acquired before lifecycle writes execute.
    # Any dispatch that never reaches the runtime must release that lease here.
    dispatch_rejected: Callable[[RunAgent], None] | None = None
    _gated_on: str | None = field(default=None, init=False, repr=False)
    _dispatch_pool: ThreadPoolExecutor | None = field(
        default=None, init=False, repr=False
    )
    _dispatch_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _dispatches: dict[Future[None], tuple[RunAgent, Hashable]] = field(
        default_factory=dict, init=False, repr=False
    )
    _accept_dispatches: bool = field(default=True, init=False, repr=False)
    _dispatch_closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.dispatch_capacity < 0:
            raise ValueError("dispatch_capacity must be >= 0")
        if self.dispatch_capacity:
            self._dispatch_pool = ThreadPoolExecutor(
                max_workers=self.dispatch_capacity,
                thread_name_prefix=f"{self.name}-dispatch",
            )

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

        A pending hot reload closes admission while existing work drains, so
        the domain never emits a claim relabel for work this process will not
        start.
        """
        if self._dispatch_pool is None:
            return 1
        with self._dispatch_lock:
            if not self._accept_dispatches or self._dispatch_closed:
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

    def shutdown_dispatches(self, *, cancel: bool) -> None:
        """Close admission, optionally cancel active async work, and join.

        ThreadPoolExecutor workers are non-daemon. Joining them explicitly is
        therefore part of the daemon's shutdown contract, not merely resource
        tidying: without cancellation Ctrl-C leaves the child agent session
        alive and interpreter shutdown waits for its worker indefinitely.
        """
        pool = self._dispatch_pool
        if pool is None:
            return
        with self._dispatch_lock:
            if self._dispatch_closed:
                return
            self._dispatch_closed = True
            self._accept_dispatches = False
        if cancel and self.cancel_active_dispatches is not None:
            try:
                self.cancel_active_dispatches()
            except Exception as exc:  # noqa: BLE001 - shutdown must still join
                self.log(f"{self.name}: dispatch cancellation failed: {exc}")
        pool.shutdown(wait=True, cancel_futures=cancel)

    def _gate_blocks_this_tick(self) -> bool:
        """True when `tick_gate` says to skip. Edge-triggered logging: a level
        that stays tripped for hours must not print a line every poll, and the
        clearing edge is the line an operator actually waits for.

        Fails OPEN: a gate that raises must not wedge the daemon, and every
        breaker it front-runs is still enforced at dispatch time."""
        if self.tick_gate is None:
            return False
        try:
            reason = self.tick_gate()
        except Exception as exc:  # noqa: BLE001 - a broken gate ≠ a stopped daemon
            self.log(f"{self.name}: tick gate failed (proceeding): {exc}")
            return False
        if reason is None:
            if self._gated_on is not None:
                self.log(f"{self.name}: resuming — {self._gated_on} no longer applies")
                self._gated_on = None
            return False
        if reason != self._gated_on:
            self.log(f"{self.name}: {reason}")
            self._gated_on = reason
            if self.journal is not None:
                self.journal.emit("tick_gated", reason=reason)
        return True

    def tick(self) -> list[Action]:
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
        if self._gate_blocks_this_tick():
            return []
        try:
            world = self.observe()
            actions = self.decide(world)
        except WorldUnchanged:
            # The probe proved this tick would observe the world the last full
            # build already observed; re-deciding it re-derives the same actions.
            # Deliberately SILENT — a line every quiet poll is exactly the
            # heartbeat noise the probe exists to remove, and run_forever's ♥
            # already proves liveness.
            return []
        except GhUnavailable as exc:
            self.log(
                f"{self.name}: world build failed: GitHub {exc.headline} — "
                f"skipping tick, no actions taken ({exc.detail})"
            )
            # Journalled too: a run of these is the difference between "the
            # harness did nothing because there was nothing to do" and "the
            # harness could not see" — indistinguishable from actions alone.
            if self.journal is not None:
                self.journal.emit("gh_unavailable", kind=exc.kind, detail=exc.detail)
            return []
        except Exception as exc:  # noqa: BLE001 - crash-isolate a poll
            self.log(f"{self.name}: poll pass failed (continuing): {exc}")
            return []
        ordered = order_for_execution(actions)
        taken: list[Action] = []
        for action in ordered:
            try:
                if isinstance(action, RunAgent):
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
                            "duplicate target, or hot reload draining"
                        )
                else:
                    taken.append(action)
                    self.executor.execute(action)
            except Exception as exc:  # noqa: BLE001 - one bad action ≠ dead daemon
                if isinstance(action, RunAgent) and self._dispatch_pool is not None:
                    # Includes pool.submit failures: the runtime never acquired
                    # ownership, so its finally cannot release decision leases.
                    self._release_rejected_dispatch(action)
                self.log(f"{self.name}: action {action!r} failed: {exc}")
        return taken

    def run_forever(
        self,
        sleep: Callable[[int], None],
        now: Callable[[], int],
        heartbeat_seconds: int = 900,
        max_ticks: int | None = None,
        reload_check: Callable[[], bool] | None = None,
        active_now: Callable[[], float] = time.monotonic,
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
        last = now()
        last_active = active_now()
        last_logged = last
        ticks = 0
        reload_pending = False
        try:
            while max_ticks is None or ticks < max_ticks:
                cur = now()
                cur_active = active_now()
                elapsed = cur - last
                active_elapsed = cur_active - last_active
                previous = last
                last = cur
                last_active = cur_active
                # A late wall heartbeat is a stall only when wall time advanced
                # beyond monotonic active time. Synchronous paid legs routinely
                # exceed the wall threshold and must not generate incident records
                # or replay lifecycle state merely because they were productive.
                suspended = max(0, round(elapsed - active_elapsed))
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
                    observed_from = datetime.fromtimestamp(
                        previous, tz=timezone.utc
                    )
                    reconcile_from = min(
                        observed_from,
                        stall_started - timedelta(seconds=self.poll_seconds),
                    )
                    if self.journal is not None:
                        self.journal.emit(
                            "stall",
                            started_at=stall_started.isoformat(),
                            woke_at=woke_at.isoformat(),
                            elapsed_seconds=elapsed,
                            suspended_seconds=suspended,
                            stall_clock_offset_seconds=stall_clock_offset,
                        )
                        path = getattr(self.journal, "path", "")
                        if path and suspended > 0:
                            for annotation in stalled_run_annotations(
                                read_events_tail(path),
                                observed_from,
                                woke_at,
                                suspended,
                            ):
                                self.journal.emit(
                                    "agent_run_stall",
                                    **annotation,
                                    stall_started_at=stall_started.isoformat(),
                                    stall_clock_offset_seconds=stall_clock_offset,
                                    observed_from=observed_from.isoformat(),
                                    woke_at=woke_at.isoformat(),
                                    detected_suspended_seconds=suspended,
                                )
                    self.executor.reconcile_recent_actions(
                        reconcile_from,
                        woke_at,
                    )
                    last_logged = cur
                elif cur - last_logged >= heartbeat_seconds:
                    self.log(f"{self.name}: ♥ alive (poll {self.poll_seconds}s)")
                    last_logged = cur
                self.tick()
                ticks += 1
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
                self._sleep_interval(sleep)
            self.shutdown_dispatches(cancel=False)
        except BaseException:
            self.shutdown_dispatches(cancel=True)
            raise

    def _sleep_interval(self, sleep: Callable[[int], None]) -> None:
        """Sleep one poll interval, cut short by a push wake if one is wired.

        Without `wake_check` this is exactly the old `sleep(poll_seconds)` — one
        call, so a fake sleep still sees the interval it was asked for. With it,
        the same total is served in ≤1s slices and abandoned on the first
        delivery. A wake_check that raises is treated as "no wake": losing push
        latency must never wedge the reconciler."""
        if self.wake_check is None:
            sleep(self.poll_seconds)
            return
        remaining = self.poll_seconds
        while remaining > 0:
            step = 1 if remaining > 1 else remaining
            sleep(step)
            remaining -= step
            try:
                woken = self.wake_check()
            except Exception as exc:  # noqa: BLE001 - a broken wake ≠ a dead daemon
                self.log(f"{self.name}: wake check failed (polling on): {exc}")
                # Serve out the REST of the interval unsliced: returning here
                # would tick after ~1s, and a persistently-broken check would
                # turn the slow reconciler into a 1s hot loop against the API.
                if remaining > 0:
                    sleep(remaining)
                return
            if woken:
                return
