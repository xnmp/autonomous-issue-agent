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
from dataclasses import dataclass

from ..adapters.github import GhUnavailable
from ..domain.actions import Action, RunAgent
from ..domain.world import WorldState
from .execute import Executor


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
        try:
            world = self.observe()
            actions = self.decide(world)
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
    ) -> None:
        """Loop tick→sleep with a heartbeat. `now`/`sleep` injected (testable).
        `max_ticks` bounds the loop for tests; None = forever."""
        self.log(f"{self.name}: watching (poll {self.poll_seconds}s)")
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
            sleep(self.poll_seconds)
