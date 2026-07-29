"""Generic daemon loop — observe → decide → execute → sleep, with heartbeat.

Replaces the three near-identical bash `while true; do … sleep; done` loops
(watcher.sh / review-watcher.sh / steward.sh). A `Daemon` is parameterised by:
  * `observe()`  → WorldState (gateway reads),
  * `decide(world)` → [Action] (a pure domain decision function, bound to config),
  * an `Executor` to apply them.

`tick()` is the pure-sequenced unit (observe→decide→execute) and is what tests
drive; `run_forever()` wraps it with the sleep + heartbeat (periodic alive line +
the suspend-stall warning when wall-clock elapsed >> interval — the lesson from
the laptop-suspend stall). The clock + sleep are injected so the loop is testable
without real time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..adapters.github import GhUnavailable
from ..domain.actions import Action, RunAgent
from ..domain.world import WorldState
from .execute import Executor
from .world_probe import WorldUnchanged


def order_for_execution(actions: list[Action]) -> list[Action]:
    """Cheap, terminal actions first; agent dispatches last (stable partition).

    A RunAgent blocks the tick for the whole session (dispatch is synchronous
    by design — up to the wall cap), so any merge/relabel/reap sequenced after
    one waits that long for a ~1s gh call. Observed twice on the first real
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
    _gated_on: str | None = field(default=None, init=False, repr=False)

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
        for action in ordered:
            try:
                self.executor.execute(action)
            except Exception as exc:  # noqa: BLE001 - one bad action ≠ dead daemon
                self.log(f"{self.name}: action {action!r} failed: {exc}")
        return ordered

    def run_forever(
        self,
        sleep: Callable[[int], None],
        now: Callable[[], int],
        heartbeat_seconds: int = 900,
        max_ticks: int | None = None,
        reload_check: Callable[[], bool] | None = None,
    ) -> None:
        """Loop tick→sleep with a heartbeat. `now`/`sleep` injected (testable).
        `max_ticks` bounds the loop for tests; None = forever.

        `reload_check` (hot reload): consulted BETWEEN ticks — the one point
        where no agent session can be in flight (dispatch is synchronous inside
        tick), so exiting here never kills paid work. True → exit with
        RELOAD_EXIT_CODE for point-at's relaunch loop."""
        mode = (
            f"webhook wake + reconcile {self.poll_seconds}s"
            if self.wake_check is not None
            else f"poll {self.poll_seconds}s"
        )
        self.log(f"{self.name}: watching ({mode})")
        last = now()
        last_logged = last
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            cur = now()
            elapsed = cur - last
            last = cur
            if elapsed > self.poll_seconds * 4:
                self.log(
                    f"{self.name}: ♥ woke after {elapsed}s (expected ~{self.poll_seconds}s) "
                    "— likely suspended/stalled; catching up"
                )
                last_logged = cur
            elif cur - last_logged >= heartbeat_seconds:
                self.log(f"{self.name}: ♥ alive (poll {self.poll_seconds}s)")
                last_logged = cur
            self.tick()
            ticks += 1
            if reload_check is not None and reload_check():
                from ..adapters.reload import RELOAD_EXIT_CODE

                self.log(
                    f"{self.name}: hot-reload — harness source changed; "
                    f"restarting between ticks (no session in flight)"
                )
                raise SystemExit(RELOAD_EXIT_CODE)
            self._sleep_interval(sleep)

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
