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


def _probe(sigs, clock, ttl=300.0):
    """A probe whose fetch pops canned signatures (a callable raises instead)."""
    feed = list(sigs)

    def fetch():
        nxt = feed.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return WorldProbe(fetch, ttl_seconds=ttl, clock=clock)


def _observe(probe, builds, world="w"):
    def build():
        builds.append(1)
        return world

    return probe.gate(build)


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
