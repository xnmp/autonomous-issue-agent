"""ADR 0010 change probe for the per-tick world build.

Every tick each daemon rebuilds its whole world through `gh` list/view calls —
GraphQL-backed and ~14 points a build, which exhausted the shared review/steward
token's 5,000-point pool on 2026-07-26 (both daemons at POLL_SECONDS=20 is ~360
builds/hour). Almost all of those builds observe a world identical to the last
one.

The gate asks a 1-REST-point question first — "what is the newest `updated_at`
across all issues and PRs?" (`GitHubGateway.change_signature`) — and skips the
tick when the answer matches the signature committed at the last full build.
Every state this harness acts on is a GitHub WRITE (labels, comments, reviews,
pushes, merges), and every one of those bumps some issue/PR `updated_at`, so the
daemons' own actions invalidate the signature by construction.

What the signature is blind to: CI check-run completion and mergeable/
mergeStateStatus flips, which GitHub recomputes without touching `updated_at`.
The reviewer reads neither (it ignores checks), so it is gated
unconditionally. The FIX
daemon consumes both (CI-fix dispatch, auto-rebase) — but only when there is
something in flight to consume them FROM. `probe_when` expresses exactly that:
an optional predicate over the LAST fully-built world which decides whether the
cheap signature is a sound stand-in for the next build. main.py passes
`domain.world.fix_probe_safe` for the fixer, so quiet ticks (no `ai-working`
issue, no open `ai-pr-open` PR) cost 1 REST point instead of a ~14-point world,
while any in-flight work restores full builds until it lands. The TTL
reconciliation build below is defence in depth on top of that analysis, not a
substitute for it.

`read_signals` closes the remaining hole (#163): "an open harness PR exists" was
the fixer's whole in-flight test, so the fixer NEVER probed while a PR waited on
CI — ~180 full GraphQL builds an hour, the largest single term in the 2026-08-02
pool exhaustion. The blind spot there is only the CI and mergeability of ≤3
heads, and both are readable over cheap REST. This hook re-reads them each tick;
`fix_probe_safe` compares the reading against the one committed with the last
full build and gates only when NOTHING moved. The extra reads run only when the
last world actually had in-flight heads.

Failure posture, in one line each:
  * probe_when(last world, signals) is False → build, WITHOUT probing (a wasted
    REST call on top of the GraphQL build is strictly worse than not probing).
  * read_signals returns None (a failed blind-spot read) → the predicate sees
    "not read" → build. It raising GhUnavailable propagates like any observation.
  * probe raises GhUnavailable (rate limit / network / 5xx) → propagates, the
    daemon's existing skip-tick arm answers; nothing here retries.
  * probe returns "" (benign gh failure, empty repo) → build — fail OPEN; a
    broken probe must degrade to today's behaviour, never to permanent sleep.
  * signature changed / first tick / TTL expired → build.
  * build itself fails → signature NOT committed, so the next tick builds again.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from collections.abc import Sequence

from ..domain.world import (
    HeadRef,
    HeadSignal,
    ProbeSignals,
    WorldState,
    fix_probe_safe,
    probe_signal_heads,
)


def fix_probe_hooks(
    has_active_dispatch: Callable[[], bool],
    read_heads: Callable[[Sequence[HeadRef]], tuple[HeadSignal, ...] | None],
) -> tuple[
    Callable[[WorldState, ProbeSignals], bool],
    Callable[[WorldState], tuple[HeadSignal, ...] | None],
]:
    """The fixer's `(probe_when, read_signals)` pair.

    Adds the one in-flight fact no GitHub read describes: a PAID DISPATCH. A leg
    can finish, die or be cancelled without touching any issue or PR, so a tick
    is never skippable while one is running — and the blind-spot reading is not
    bought either, since the answer could not gate the tick anyway.

    Composed here rather than inline in `main.py` so the guard is testable
    without standing up a daemon."""

    def probe_when(world: WorldState, signals: ProbeSignals) -> bool:
        if has_active_dispatch():
            return False
        return fix_probe_safe(world, signals)

    def read_signals(world: WorldState) -> tuple[HeadSignal, ...] | None:
        if has_active_dispatch():
            return None
        return read_heads(probe_signal_heads(world))

    return probe_when, read_signals


class WorldUnchanged(Exception):
    """The probe proved the world identical to the last full build. Not an
    error: the tick simply has nothing new to observe."""


@dataclass
class WorldProbe:
    """Wraps an `observe` callable with the signature check. One instance per
    daemon process; state is in-memory only (a restart just builds once)."""

    fetch_signature: Callable[[], str]
    # Reconciliation ceiling: a full build at least this often, whatever the
    # signature says. Bounds the staleness of anything the signature cannot see.
    ttl_seconds: float = 300.0
    clock: Callable[[], float] = time.time
    # Predicate over the LAST fully-built world: "was that world a shape in which
    # the signature is a sound stand-in for a build?". None = always probe (the
    # review wiring). False for the last world = build unconditionally,
    # and don't even spend the REST point on a probe whose answer can't be used.
    probe_when: Callable[[WorldState, ProbeSignals], bool] | None = None
    # Re-reads what the signature cannot see (CI + mergeability of the last
    # world's in-flight heads) over a DIFFERENT, cheap pool. Returning None means
    # the read failed; the predicate then fails toward a build. Not called at all
    # when the last world has no such heads, so an idle fixer stays at one point.
    read_signals: Callable[[WorldState], tuple[HeadSignal, ...] | None] | None = None
    _sig: str | None = field(default=None, init=False)
    _last_full: float | None = field(default=None, init=False)
    _last_world: WorldState | None = field(default=None, init=False)
    _signals: tuple[HeadSignal, ...] | None = field(default=None, init=False)

    def gate(self, build: Callable[[], WorldState]) -> Callable[[], WorldState]:
        def observe() -> WorldState:
            # With no predicate the gate is unconditional (review: its
            # first tick probes, finds no committed signature, and builds). With
            # one, a world must exist to judge — and it must pass.
            current: tuple[HeadSignal, ...] | None = None
            if self._last_world is not None and self.read_signals is not None:
                # GhUnavailable propagates like every other observation failure.
                current = self.read_signals(self._last_world)
            gated = self.probe_when is None or (
                self._last_world is not None
                and self.probe_when(
                    self._last_world, ProbeSignals(current, self._signals)
                )
            )
            if gated:
                fresh = (
                    self._last_full is not None
                    and self.clock() - self._last_full < self.ttl_seconds
                )
                sig = self.fetch_signature()  # GhUnavailable propagates to the tick
                if fresh and sig and sig == self._sig:
                    raise WorldUnchanged()
            else:
                sig = ""
            world = build()
            # Commit only AFTER a successful build: committing at probe time
            # would let a failed build strand the daemon behind a signature it
            # never actually observed. An unprobed build keeps the older `_sig`:
            # it still means "no updated_at moved since it was fetched", which is
            # a strictly stronger claim than one taken at this build's time, so a
            # later match remains proof that this build's world still holds.
            self._sig = sig or self._sig
            self._last_full = self.clock()
            self._last_world = world
            # Committed from the reading taken BEFORE this build, deliberately:
            # a blind-spot flip inside the read→build window then shows up as a
            # DIFFERENCE on the next tick (one redundant build) instead of being
            # absorbed into the committed value and skipped until the TTL. A
            # failed reading commits None, which the predicate reads as "not
            # read" and answers with a build.
            self._signals = current
            return world

        return observe
