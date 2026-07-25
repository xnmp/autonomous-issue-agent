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
from ..domain.actions import Action
from ..domain.world import WorldState
from .execute import Executor


@dataclass
class Daemon:
    name: str
    observe: Callable[[], WorldState]
    decide: Callable[[WorldState], list[Action]]
    executor: Executor
    poll_seconds: int = 60
    log: Callable[[str], None] = lambda msg: None

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
            return []
        except Exception as exc:  # noqa: BLE001 - crash-isolate a poll
            self.log(f"{self.name}: poll pass failed (continuing): {exc}")
            return []
        for action in actions:
            try:
                self.executor.execute(action)
            except Exception as exc:  # noqa: BLE001 - one bad action ≠ dead daemon
                self.log(f"{self.name}: action {action!r} failed: {exc}")
        return actions

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
