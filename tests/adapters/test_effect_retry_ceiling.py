"""ADR 0012: durable lifecycle commands must have a retry CEILING.

The keifu deployment (2026-08-01) ran a review identity whose fine-grained PAT
lacked Issues: Write. A bare 403 is deliberately not an infra failure, so every
`gh issue edit --add-label` returned "", the readback said "not applied", the
command was receipted `retryable_failure`, and the next tick's drain executed
the identical denied write again — forever, silently.

These tests pin the BEHAVIOUR of the bound, not its bookkeeping: a permanently
refused write stops being attempted, says so exactly once, and stays queryable;
a write that succeeds inside the ceiling is unaffected; and an INFRA failure —
which is genuinely transient and already has its own reconciliation path — is
never retired by it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.effects import GitHubEffectExecutor, command_for_action
from issue_agent.adapters.github import GhUnavailable
from issue_agent.domain.actions import PostComment, Relabel, Target
from issue_agent.domain.contracts import EffectStatus
from issue_agent.domain.effect_retry import (
    DEFAULT_EFFECT_RETRY_CEILING,
    effect_ceiling_reached,
)
from issue_agent.domain.events import LevelChanged
from issue_agent.domain.labels import Label


class DeniedWriteGateway:
    """A GitHub whose writes are refused the way a scope-less PAT refuses them:
    the mutation returns normally (gh exits non-zero, the adapter fail-softs to
    "") and the readback shows nothing changed."""

    def __init__(self, *, infra: bool = False):
        self.calls: list[tuple] = []
        self.infra = infra
        self.issue_revision = "issue-rev-1"

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))
        if self.infra:
            raise GhUnavailable("rate-limit", "API rate limit exceeded")

    def comment(self, kind, number, body):
        self.calls.append(("comment", kind, number, body))
        if self.infra:
            raise GhUnavailable("network", "error connecting to api.github.com")

    def has_label(self, kind, number, label):
        return False

    def has_comment(self, kind, number, body):
        return False

    def get_issue(self, issue):
        del issue
        return SimpleNamespace(updated_at=self.issue_revision)

    def get_pr(self, pr, *, mergeability=True, checks=True):
        del pr, mergeability, checks
        return SimpleNamespace(head_ref_oid="head-1")


class FlakyGateway(DeniedWriteGateway):
    """Refuses or dies according to a script: each entry is "refuse" (the write
    returns and the readback shows nothing) or "infra" (GhUnavailable)."""

    def __init__(self, script: list[str]):
        super().__init__()
        self.script = list(script)

    def _next(self) -> str:
        return self.script.pop(0) if self.script else "refuse"

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))
        if self._next() == "infra":
            raise GhUnavailable("server", "HTTP 503 gateway unavailable")


class EventuallyWritableGateway(DeniedWriteGateway):
    """Refuses `refusals` writes, then behaves."""

    def __init__(self, refusals: int):
        super().__init__()
        self.refusals = refusals
        self.labels: set[tuple] = set()

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))
        if self.refusals > 0:
            self.refusals -= 1
            return
        if add:
            self.labels.add((kind, number, add))

    def has_label(self, kind, number, label):
        return (kind, number, label) in self.labels


class RecordingJournal:
    def __init__(self):
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


def _executor(tmp_path, gateway, *, ceiling: int = 3, journal=None, log=None):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    return (
        GitHubEffectExecutor(
            gateway,
            store,
            repository="o/r",
            identity="o/r:steward",
            retry_ceiling=ceiling,
            journal=journal,
            log=log,
        ),
        store,
    )


def _relabel() -> Relabel:
    return Relabel(Target.ISSUE, 7, None, Label.AI_TRIAGE)


def _command_id(action, revision="issue-rev-1"):
    return command_for_action(
        action,
        expected_revision=revision,
        repository="o/r",
        executor_identity="o/r:steward",
    ).command_id


def _levels(journal) -> list[LevelChanged]:
    return [e.payload for e in journal.events if isinstance(e.payload, LevelChanged)]


# ── the ceiling ─────────────────────────────────────────────────────────────
def test_permanently_refused_write_stops_being_replayed_at_the_ceiling(tmp_path):
    gateway = DeniedWriteGateway()
    journal = RecordingJournal()
    lines: list[str] = []
    executor, store = _executor(
        tmp_path, gateway, ceiling=3, journal=journal, log=lines.append
    )

    executor.execute(_relabel())
    for _ in range(10):
        executor.drain_pending_commands()

    # Exactly `ceiling` attempts were bought, not one per tick forever.
    assert len(gateway.calls) == 3
    # And the command is out of the replay set for good.
    assert store.pending_commands() == ()


def test_retired_command_escalates_exactly_once_and_keeps_its_content(tmp_path):
    action = PostComment(Target.ISSUE, 7, "the verdict nobody ever saw")
    gateway = DeniedWriteGateway()
    journal = RecordingJournal()
    lines: list[str] = []
    executor, store = _executor(
        tmp_path, gateway, ceiling=2, journal=journal, log=lines.append
    )

    executor.execute(action)
    for _ in range(5):
        executor.drain_pending_commands()

    levels = _levels(journal)
    assert len(levels) == 1
    assert (levels[0].state, levels[0].reason) == ("exhausted", "retry_ceiling")
    assert levels[0].attributes["target"] == "issue:7"
    # The operator line has to name the write and point at the likely cause.
    escalation = "\n".join(lines)
    assert "issue:7" in escalation and "doctor" in escalation.lower()
    assert "Issues" in escalation

    # The content must survive: on a fixed token an operator has to be able to
    # see what never posted.
    command_id = _command_id(action)
    receipt = store.effect_receipt(command_id)
    assert receipt is not None
    assert receipt.status is EffectStatus.PERMANENT_FAILURE
    retired = store.latest_command(f"github:{command_id}")
    assert retired is not None
    assert retired.state == EffectStatus.PERMANENT_FAILURE.value
    assert retired.command.payload["body"] == "the verdict nobody ever saw"


def test_write_that_lands_inside_the_ceiling_clears_normally(tmp_path):
    gateway = EventuallyWritableGateway(refusals=2)
    journal = RecordingJournal()
    executor, store = _executor(tmp_path, gateway, ceiling=5, journal=journal)

    executor.execute(_relabel())
    executor.drain_pending_commands()
    executor.drain_pending_commands()

    assert ("issue", 7, "ai-triage") in gateway.labels
    assert store.pending_commands() == ()
    assert _levels(journal) == []
    assert (
        store.effect_receipt(_command_id(_relabel())).status
        is EffectStatus.SUCCEEDED
    )


def test_infra_failures_never_burn_the_ceiling(tmp_path):
    """A rate limit or a dead network is not a refusal. Those receipt
    `unknown_outcome` and are reconciled; retiring them would turn a transient
    outage into a permanently dropped lifecycle write."""
    gateway = DeniedWriteGateway(infra=True)
    journal = RecordingJournal()
    executor, store = _executor(tmp_path, gateway, ceiling=2, journal=journal)

    with pytest.raises(GhUnavailable):
        executor.execute(_relabel())
    for _ in range(6):
        executor.drain_pending_commands()

    pending = store.pending_commands()
    assert len(pending) == 1
    assert pending[0].state == EffectStatus.UNKNOWN_OUTCOME.value
    assert pending[0].attempts > 2  # past the ceiling, still not retired
    assert _levels(journal) == []


def test_drain_logs_the_command_it_could_not_process(tmp_path):
    """The bare `except Exception: continue` made a drain that processed
    nothing indistinguishable from a clean one."""
    gateway = DeniedWriteGateway(infra=True)
    lines: list[str] = []
    executor, _store = _executor(tmp_path, gateway, ceiling=5, log=lines.append)

    with pytest.raises(GhUnavailable):
        executor.execute(_relabel())
    executor.drain_pending_commands()

    assert any("could not be drained" in line for line in lines)
    assert any("GhUnavailable" in line for line in lines)


def test_other_commands_still_drain_after_one_is_retired(tmp_path):
    gateway = EventuallyWritableGateway(refusals=0)
    executor, store = _executor(tmp_path, gateway, ceiling=1)
    # A command that can never verify, alongside one that can.
    gateway.refusals = 10**6
    executor.execute(_relabel())
    gateway.refusals = 0
    executor.execute(Relabel(Target.ISSUE, 8, None, Label.AI_TRIAGE))

    executor.drain_pending_commands()

    assert ("issue", 8, "ai-triage") in gateway.labels
    assert store.pending_commands() == ()


# ── refusals, not attempts (adversarial review, round 1) ────────────────────
def _drain(executor, times: int) -> None:
    for _ in range(times):
        try:
            executor.drain_pending_commands()
        except GhUnavailable:
            pass


def test_an_outage_does_not_pre_burn_the_ceiling(tmp_path):
    """`attempts` counts every execution `begin_command` starts, including the
    ones an outage interrupted, and `retry_reconciled_command` does not reset
    it. Counting attempts meant ~10 ticks of GitHub degradation retired a
    command on its FIRST genuine refusal — a lifecycle write silently dropped
    because the network was bad ten minutes ago."""
    gateway = FlakyGateway(["infra"] * 9 + ["refuse"])
    journal = RecordingJournal()
    executor, store = _executor(tmp_path, gateway, ceiling=3, journal=journal)

    with pytest.raises(GhUnavailable):
        executor.execute(_relabel())
    _drain(executor, 9)  # 9 outage ticks in total, then exactly one refusal

    pending = store.pending_commands()
    assert len(pending) == 1, "one genuine refusal must not retire the command"
    assert pending[0].attempts >= 9
    assert pending[0].refusals == 1
    assert _levels(journal) == []


def test_refusals_interleaved_with_outages_still_reach_the_ceiling(tmp_path):
    """The converse: an intermittent network must not make the ceiling
    unreachable for a write GitHub keeps refusing."""
    gateway = FlakyGateway(["refuse", "infra", "refuse", "infra", "refuse"])
    journal = RecordingJournal()
    executor, store = _executor(tmp_path, gateway, ceiling=3, journal=journal)

    executor.execute(_relabel())
    _drain(executor, 8)

    assert store.pending_commands() == ()
    levels = _levels(journal)
    assert len(levels) == 1
    assert levels[0].attributes["refusals"] == 3
    assert levels[0].attributes["attempts"] > 3


def test_a_success_clears_the_refusal_count(tmp_path):
    gateway = EventuallyWritableGateway(refusals=2)
    executor, store = _executor(tmp_path, gateway, ceiling=5)

    executor.execute(_relabel())
    executor.drain_pending_commands()
    executor.drain_pending_commands()

    latest = store.latest_command(f"github:{_command_id(_relabel())}")
    assert latest is not None
    assert latest.state == EffectStatus.SUCCEEDED.value
    assert latest.refusals == 0


def test_a_concurrent_success_is_not_an_error(tmp_path):
    """`exhaust_command` must answer "not retired by me" rather than raise:
    an exception here unwinds through execute_all and charges the caller a
    spurious stall."""
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    gateway = EventuallyWritableGateway(refusals=0)
    executor = GitHubEffectExecutor(
        gateway, store, repository="o/r", identity="o/r:steward", retry_ceiling=1
    )
    executor.execute(_relabel())  # succeeds

    command = command_for_action(
        _relabel(),
        expected_revision="issue-rev-1",
        repository="o/r",
        executor_identity="o/r:steward",
    )
    from issue_agent.domain.contracts import EffectReceipt

    now = datetime.now(timezone.utc)
    retired = store.exhaust_command(
        EffectReceipt(
            command.command_id,
            "o/r:steward",
            now,
            now,
            EffectStatus.PERMANENT_FAILURE,
            error="ceiling",
        )
    )
    assert retired is False
    assert (
        store.latest_command(f"github:{command.command_id}").state
        == EffectStatus.SUCCEEDED.value
    )


# ── harness-owned signals skip the optimistic revision guard ────────────────
def test_a_harness_signal_still_delivers_after_the_issue_moved(tmp_path):
    """An escalation explains why the harness stopped. A human commenting in
    the same tick moves `updatedAt`; discarding the escalation for that reason
    would leave the issue silently stalled."""
    gateway = EventuallyWritableGateway(refusals=1)
    executor, store = _executor(tmp_path, gateway, ceiling=5)
    escalation = Relabel(
        Target.ISSUE, 7, None, Label.NEEDS_HUMAN_REVIEW, None, True
    )

    executor.execute(escalation)  # refused once → queued
    gateway.issue_revision = "issue-rev-2"  # the issue moved underneath it
    executor.drain_pending_commands()

    assert ("issue", 7, "needs-human-review") in gateway.labels
    assert store.pending_commands() == ()


def test_an_ordinary_write_still_fails_closed_when_the_issue_moved(tmp_path):
    """The anti-vacuity twin: without the harness_signal flag the optimistic
    guard must still discard a stale write."""
    gateway = EventuallyWritableGateway(refusals=1)
    executor, store = _executor(tmp_path, gateway, ceiling=5)

    executor.execute(_relabel())
    gateway.issue_revision = "issue-rev-2"
    executor.drain_pending_commands()

    assert ("issue", 7, "ai-triage") not in gateway.labels
    assert store.pending_commands() == ()  # retired as permanent_failure


# ── the pure predicate ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "state,attempts,ceiling,expected",
    [
        ("retryable_failure", 9, 10, False),
        ("retryable_failure", 10, 10, True),
        ("retryable_failure", 10**9, 10, True),
        ("retryable_failure", 0, 10, False),
        # Only a verified refusal counts.
        ("unknown_outcome", 99, 10, False),
        ("executing", 99, 10, False),
        ("pending", 99, 10, False),
        ("succeeded", 99, 10, False),
        ("", 99, 10, False),
        # A non-positive ceiling means "no bound" — config rejects it as input.
        ("retryable_failure", 99, 0, False),
        ("retryable_failure", 99, -1, False),
    ],
)
def test_effect_ceiling_reached_boundaries(state, attempts, ceiling, expected):
    assert effect_ceiling_reached(state, attempts, ceiling) is expected


def test_default_ceiling_is_positive():
    assert DEFAULT_EFFECT_RETRY_CEILING > 0
