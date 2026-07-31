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
The reviewer and the steward read neither (the reviewer ignores checks; the
steward never fetches PRs at all), so they are gated unconditionally. The FIX
daemon consumes both (CI-fix dispatch, auto-rebase) — but only when there is
something in flight to consume them FROM. `probe_when` expresses exactly that:
an optional predicate over the LAST fully-built world which decides whether the
cheap signature is a sound stand-in for the next build. main.py passes
`domain.world.fix_probe_safe` for the fixer, so quiet ticks (no `ai-working`
issue, no open `ai-pr-open` PR) cost 1 REST point instead of a ~14-point world,
while any in-flight work restores full builds until it lands. The TTL
reconciliation build below is defence in depth on top of that analysis, not a
substitute for it.

Failure posture, in one line each:
  * probe_when(last world) is False → build, WITHOUT probing (a wasted REST call
    on top of the GraphQL build is strictly worse than not probing at all).
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

from ..domain.world import WorldState


class WorldUnchanged(Exception):
    """The probe proved the world identical to the last full build. Not an
    error: the tick simply has nothing new to observe."""


@dataclass
class WorldProbe:
    """Wraps an `observe` callable with the signature check. One instance per
    daemon process; state is in-memory only (a restart just builds once)."""

    fetch_signature: Callable[[], str]
    # Reconciliation ceiling: a full build at least this often, whatever the
    # signature says. Bounds the staleness of anything the signature cannot see
    # (and, when Jira sync is on, of Jira-side edits the steward mirrors).
    ttl_seconds: float = 300.0
    clock: Callable[[], float] = time.time
    # Predicate over the LAST fully-built world: "was that world a shape in which
    # the signature is a sound stand-in for a build?". None = always probe (the
    # review/steward wiring). False for the last world = build unconditionally,
    # and don't even spend the REST point on a probe whose answer can't be used.
    probe_when: Callable[[WorldState], bool] | None = None
    _sig: str | None = field(default=None, init=False)
    _last_full: float | None = field(default=None, init=False)
    _last_world: WorldState | None = field(default=None, init=False)

    def gate(self, build: Callable[[], WorldState]) -> Callable[[], WorldState]:
        def observe() -> WorldState:
            # With no predicate the gate is unconditional (review/steward: their
            # first tick probes, finds no committed signature, and builds). With
            # one, a world must exist to judge — and it must pass.
            gated = self.probe_when is None or (
                self._last_world is not None and self.probe_when(self._last_world)
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
            return world

        return observe
