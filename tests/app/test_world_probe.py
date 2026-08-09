"""The change-probe gate must only ever SAVE work, never hide it.

Motivating incident (2026-07-26, xnmp/tauri-explorer): review + steward share
one token, and full world builds every 20s exhausted its 5,000-point GraphQL
pool — both daemons blind for the rest of the hour. The probe replaces most
builds with a 1-point REST signature check. These tests pin the safety
contracts: every ambiguous outcome (first tick, probe failure, TTL expiry,
failed build) must fall back to a full build — the probe may only skip when it
POSITIVELY proved nothing changed.
"""
from __future__ import annotations

import pytest

from issue_agent.adapters.github import GhUnavailable
from issue_agent.app.world_probe import WorldProbe, WorldUnchanged


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _probe(sigs, clock, ttl=300.0, probe_when=None, fetches=None, read_signals=None):
    """A probe whose fetch pops canned signatures (a callable raises instead)."""
    feed = list(sigs)

    def fetch():
        if fetches is not None:
            fetches.append(1)
        nxt = feed.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return WorldProbe(
        fetch,
        ttl_seconds=ttl,
        clock=clock,
        probe_when=probe_when,
        read_signals=read_signals,
    )


def _observe(probe, builds, world="w"):
    def build():
        builds.append(1)
        return world

    return probe.gate(build)


def _observe_seq(probe, builds, worlds):
    """gate() over a build that returns the next canned world each time (the last
    one sticks), so a test can drive `probe_when` through changing world shapes."""
    feed = list(worlds)

    def build():
        builds.append(1)
        return feed.pop(0) if len(feed) > 1 else feed[0]

    return probe.gate(build)


IDLE, BUSY = "idle", "busy"
# Stands in for domain.world.fix_probe_safe, which reads the world AND the
# blind-spot signals the gate re-read this tick (#163).
_idle_only = (lambda w, _signals: w == IDLE)


def test_first_tick_always_builds_and_unchanged_ticks_then_skip():
    clock, builds = Clock(), []
    observe = _observe(_probe(["5@t1", "5@t1", "5@t1"], clock), builds)
    assert observe() == "w"          # first tick: no signature yet → build
    with pytest.raises(WorldUnchanged):
        observe()                    # same signature → proven unchanged
    with pytest.raises(WorldUnchanged):
        observe()
    assert len(builds) == 1


def test_a_changed_signature_triggers_a_build():
    clock, builds = Clock(), []
    observe = _observe(_probe(["5@t1", "5@t2", "5@t2"], clock), builds)
    observe()
    observe()                        # updated_at moved → build again
    with pytest.raises(WorldUnchanged):
        observe()                    # then quiet again
    assert len(builds) == 2


def test_ttl_expiry_forces_a_reconciliation_build_despite_unchanged_signature():
    """The signature is blind to CI/mergeability flips (and Jira-side edits);
    the TTL bounds how stale a gated daemon can possibly run."""
    clock, builds = Clock(), []
    observe = _observe(_probe(["5@t1", "5@t1"], clock, ttl=300.0), builds)
    observe()
    clock.t += 301
    observe()                        # unchanged sig, but the ceiling passed
    assert len(builds) == 2


def test_a_benignly_failed_probe_builds_instead_of_sleeping():
    """"" is gh's fail-soft answer (and an empty repo). If the probe query is
    ever broken, the daemon must degrade to today's build-every-tick behaviour
    — permanent skip would be a silent outage."""
    clock, builds = Clock(), []
    observe = _observe(_probe(["5@t1", "", ""], clock), builds)
    observe()
    observe()
    observe()
    assert len(builds) == 3


def test_probe_infra_failure_propagates_as_gh_unavailable():
    """Rate limit during the probe = the existing skip-tick arm's business; the
    gate must neither swallow it nor let it corrupt the committed signature."""
    clock, builds = Clock(), []
    probe = _probe(["5@t1", GhUnavailable("rate-limit", "x"), "5@t1"], clock)
    observe = _observe(probe, builds)
    observe()
    with pytest.raises(GhUnavailable):
        observe()
    with pytest.raises(WorldUnchanged):
        observe()                    # recovered probe, same sig → still skips
    assert len(builds) == 1


# ── probe_when: the conditionally-gated fixer (#3) ───────────────────────────
# The signature can't see CI/mergeability flips, which is why the fixer used to
# build unconditionally. Those signals only exist while something is in flight,
# so the fixer now passes a predicate over the LAST built world: idle-shaped →
# probe like review/steward; anything in flight → full build every tick.
def test_predicate_first_tick_builds_without_spending_a_probe():
    """No world has been observed yet, so the predicate has nothing to judge:
    build — and don't burn a REST point on an answer that can't be used."""
    clock, builds, fetches = Clock(), [], []
    observe = _observe_seq(
        _probe([], clock, probe_when=_idle_only, fetches=fetches), builds, [IDLE]
    )
    assert observe() == IDLE
    assert (len(builds), fetches) == (1, [])


def test_in_flight_world_rebuilds_fully_and_never_probes():
    """A world with work in flight is exactly the world whose CI/mergeability
    the signature cannot see — full build, no probe, every tick."""
    clock, builds, fetches = Clock(), [], []
    observe = _observe_seq(
        _probe([], clock, probe_when=_idle_only, fetches=fetches), builds, [BUSY]
    )
    observe()
    observe()
    observe()
    assert (len(builds), fetches) == (3, [])


def test_idle_world_is_gated_like_review_and_steward():
    """Once an idle world is on record the cheap signature takes over. Tick 2
    still builds (tick 1 never fetched, so no signature is committed yet); from
    then on an unchanged signature skips the ~14-point build."""
    clock, builds, fetches = Clock(), [], []
    observe = _observe_seq(
        _probe(["5@t1", "5@t1"], clock, probe_when=_idle_only, fetches=fetches),
        builds, [IDLE],
    )
    observe()                        # first tick: build, no probe
    observe()                        # idle on record → probe, nothing committed yet
    with pytest.raises(WorldUnchanged):
        observe()                    # same signature → proven unchanged
    assert (len(builds), len(fetches)) == (2, 2)


def test_a_changed_signature_still_builds_for_a_gated_fixer():
    clock, builds = Clock(), []
    observe = _observe_seq(
        _probe(["5@t1", "5@t2"], clock, probe_when=_idle_only), builds, [IDLE]
    )
    observe()                        # first tick: build
    observe()                        # probe, no committed signature → build
    observe()                        # updated_at moved (new ai-ready label) → build
    assert len(builds) == 3


def test_going_busy_then_idle_reuses_the_last_committed_signature():
    """Unprobed (in-flight) builds keep the older signature: it still means "no
    updated_at has moved since it was fetched", so a later match is proof the
    newer build's world holds too — one build, not two, on the way back to idle."""
    clock, builds, fetches = Clock(), [], []
    observe = _observe_seq(
        _probe(["5@t1", "5@t2", "5@t2"], clock, probe_when=_idle_only, fetches=fetches),
        builds, [IDLE, BUSY, IDLE],
    )
    observe()                        # 1: build (no probe) → idle world
    observe()                        # 2: probe 5@t1, nothing committed → build → busy
    observe()                        # 3: busy on record → build (no probe) → idle
    observe()                        # 4: idle again → probe 5@t2 ≠ 5@t1 → build
    with pytest.raises(WorldUnchanged):
        observe()                    # 5: 5@t2 committed and unchanged → skip
    assert (len(builds), len(fetches)) == (4, 3)


def test_no_predicate_leaves_review_and_steward_probing_from_tick_one():
    """Anti-regression twin of the tests above: without `probe_when` the gate is
    unconditional, so the very first tick spends the probe (and skips at tick 2)."""
    clock, builds, fetches = Clock(), [], []
    observe = _observe(_probe(["5@t1", "5@t1"], clock, fetches=fetches), builds)
    observe()
    with pytest.raises(WorldUnchanged):
        observe()
    assert (len(builds), len(fetches)) == (1, 2)


def test_a_failed_build_does_not_commit_the_new_signature():
    """Committing at probe time would strand the daemon: sig recorded, world
    never observed, every later tick 'unchanged'."""
    clock = Clock()
    calls = []
    probe = _probe(["5@t1", "5@t1"], clock)

    def build():
        calls.append(1)
        raise GhUnavailable("server", "boom")

    observe = probe.gate(build)
    with pytest.raises(GhUnavailable):
        observe()
    with pytest.raises(GhUnavailable):
        observe()                    # same sig again, but nothing was committed
    assert len(calls) == 2


# ── read_signals: folding the CI/mergeability blind spot into the gate (#163) ─
# The fixer used to refuse to probe whenever ANY open harness PR existed, so a
# PR waiting on CI cost a full GraphQL build every tick. The gate now re-reads
# that blind spot cheaply and hands both readings to the predicate, which gates
# only when nothing moved.
def _stable_when_unmoved(_world, signals):
    """Stand-in for fix_probe_safe's in-flight arm: gate only on two readings
    that both exist and agree."""
    return (
        signals.current is not None
        and signals.previous is not None
        and signals.current == signals.previous
    )


def test_unchanged_blind_spot_readings_let_an_in_flight_world_skip():
    clock, builds, reads = Clock(), [], []

    def read(world):
        reads.append(world)
        return ("ci=green",)

    observe = _observe_seq(
        _probe(["5@t1", "5@t1", "5@t1"], clock,
               probe_when=_stable_when_unmoved, read_signals=read),
        builds, [BUSY],
    )
    observe()                        # 1: no world yet → build, nothing read
    observe()                        # 2: first reading, none committed → build
    observe()                        # 3: readings agree → probe, no signature yet
    with pytest.raises(WorldUnchanged):
        observe()                    # 4: reading AND signature both unchanged
    assert (len(builds), reads) == (3, [BUSY, BUSY, BUSY])


def test_a_moved_blind_spot_reading_rebuilds_even_on_an_unchanged_signature():
    """CI completion moves no updated_at — this reading is the only witness."""
    clock, builds = Clock(), []
    feed = [("ci=pending",), ("ci=pending",), ("ci=green",)]
    observe = _observe_seq(
        _probe(["5@t1", "5@t1", "5@t1"], clock,
               probe_when=_stable_when_unmoved,
               read_signals=lambda _w: feed.pop(0)),
        builds, [BUSY],
    )
    observe()
    observe()
    observe()                        # green ≠ pending → full build despite 5@t1
    assert len(builds) == 3


def test_an_unreadable_blind_spot_builds_instead_of_skipping():
    """Fail toward observation: a failed read is NOT "nothing changed"."""
    clock, builds = Clock(), []
    observe = _observe_seq(
        _probe(["5@t1", "5@t1", "5@t1"], clock,
               probe_when=_stable_when_unmoved, read_signals=lambda _w: None),
        builds, [BUSY],
    )
    observe()
    observe()
    observe()
    assert len(builds) == 3


def test_the_first_tick_reads_no_blind_spot_at_all():
    """Nothing has been observed, so there are no in-flight heads to read."""
    clock, builds, reads = Clock(), [], []
    observe = _observe_seq(
        _probe([], clock, probe_when=_stable_when_unmoved,
               read_signals=lambda w: reads.append(w) or ()),
        builds, [BUSY],
    )
    observe()
    assert (len(builds), reads) == (1, [])


def test_the_committed_reading_is_the_one_taken_before_the_build():
    """A CI flip inside the read→build window must cost one redundant build, not
    a skipped tick: the value committed alongside a world is the PRE-build read,
    so the next tick sees a difference rather than absorbing the flip."""
    clock, builds, seen = Clock(), [], []
    feed = [("t1",), ("t2",), ("t2",)]

    def probe_when(_world, signals):
        seen.append((signals.current, signals.previous))
        return _stable_when_unmoved(_world, signals)

    observe = _observe_seq(
        _probe(["5@t1", "5@t1", "5@t1"], clock,
               probe_when=probe_when, read_signals=lambda _w: feed.pop(0)),
        builds, [BUSY],
    )
    observe()                        # build 1 commits nothing (no world yet)
    observe()                        # reads ("t1",) → commits it with build 2
    observe()                        # reads ("t2",) vs committed ("t1",) → build 3
    assert seen == [(("t1",), None), (("t2",), ("t1",))]
    assert len(builds) == 3


# ── fix_probe_hooks: the paid-dispatch guard (#163) ──────────────────────────
# A running leg can finish, die or be cancelled without touching any issue or
# PR, so no GitHub reading can speak for it: while one is in flight the tick
# must build, and the reading must not even be bought.
from issue_agent.app.world_probe import fix_probe_hooks  # noqa: E402
from issue_agent.domain.labels import Label  # noqa: E402
from issue_agent.domain.world import (  # noqa: E402
    Check,
    HeadRef,
    HeadSignal,
    PRView,
    ProbeSignals,
    WorldState,
)


def _in_flight_world():
    return WorldState(
        prs=(
            PRView(
                number=7,
                state="OPEN",
                labels=frozenset({Label.AI_PR_OPEN}),
                checks=(Check(name="ci", state="COMPLETED", bucket="pass"),),
            ),
        )
    )


def test_a_running_leg_forces_a_build_and_buys_no_reading():
    reads = []
    probe_when, read_signals = fix_probe_hooks(
        lambda: True, lambda heads: reads.append(heads) or ()
    )
    world = _in_flight_world()
    signal = (HeadSignal(number=7, ci="same"),)
    assert read_signals(world) is None
    assert probe_when(world, ProbeSignals(signal, signal)) is False
    assert reads == []


def test_with_no_leg_running_the_reading_is_taken_for_the_in_flight_heads():
    reads = []

    def read(heads):
        reads.append(tuple(heads))
        return (HeadSignal(number=7, ci="same"),)

    probe_when, read_signals = fix_probe_hooks(lambda: False, read)
    world = _in_flight_world()
    current = read_signals(world)
    assert reads == [(HeadRef(7),)]
    assert probe_when(world, ProbeSignals(current, current)) is True
    assert probe_when(world, ProbeSignals(current, None)) is False


def test_an_idle_world_buys_no_reading_but_still_gates():
    reads = []
    probe_when, read_signals = fix_probe_hooks(
        lambda: False, lambda heads: reads.append(heads) or ()
    )
    idle = WorldState()
    assert read_signals(idle) == ()
    assert reads == [()]  # the adapter is asked for nothing
    assert probe_when(idle, ProbeSignals((), ())) is True
