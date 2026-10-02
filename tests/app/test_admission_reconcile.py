"""keifu #34: a budget trip must reconcile dead headroom before escalating.

2026-08-02: three fix legs died within seconds on a broken codex binary having
spent nothing measurable. Each conservatively retained its reservation, so the
next resolve was denied against phantom headroom in 23ms, labelled
`needs-budget-review`, and waited for a human -- with $0 of real spend. A later
reconciliation pass released every one of those reservations.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.control_store import (
    Lease,
    ReservationRequest,
    SettlementRequest,
    SQLiteControlStore,
)
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.app.recovery import RecoveryCoordinator
from issue_agent.app.roles import (
    _LateReservationReconciler,
    _SweepReservationReconciler,
)
from issue_agent.domain.contracts import (
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
    RunId,
)
from issue_agent.domain.events import (
    AdmissionReconciled,
    EventEnvelope,
    Producer,
    new_event_id,
)
from issue_agent.domain.outcome import (
    MAX_BUDGET_STATUS,
    AgentResult,
    ExitReason,
)
from issue_agent.domain.pricing import (
    BudgetPolicy,
    ModelRates,
    RateTable,
    UnknownModelPolicy,
)

from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery

REPOSITORY = "o/r"
ISSUE = 34
OWNER = "o/r:fix"
LEG_MAXIMUM = Money("USD", 2_000_000)
GRACE = timedelta(minutes=2)


@pytest.fixture(autouse=True)
def _publication_authority(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        accounting_runtime,
        "_fix_publication_context",
        lambda role, cwd: (
            accounting_runtime._FixPublicationContext(
                str(Path(cwd or tmp_path).resolve()),
                f"fix/{ISSUE}-broken-binary",
                "/usr/bin/git",
                "/usr/bin/gh",
                "/usr/bin:/bin",
            )
            if role == "fix"
            else None
        ),
    )


class _Runtime:
    """Records whether the paid provider was ever actually reached."""

    def __init__(self):
        self.calls = 0

    async def run(self, _task):
        self.calls += 1
        return AgentResult(
            False,
            status="ok",
            cost_usd=1.0,
            session_id="receipt",
            usage={"input_tokens": 100, "output_tokens": 10},
        )


class _Supervisor:
    def recover_orphans(self):
        raise AssertionError("admission recovery used the destructive reaper")

    def recover_abandoned_orphans(self):
        return ()


class _Worktrees:
    def prune_validate(self) -> int:
        return 0


class _Commands:
    def drain_pending_commands(self) -> int:
        return 0

    def pending_commands(self):
        return ()


class _NoTrustedUsage:
    """The provider died before emitting usage, so spend stays indeterminate."""

    def reconcile(self, _run_id) -> bool:
        return False


class _Abandonments:
    def __init__(self):
        self.calls = []

    def abandon_run(self, run_id, reason) -> bool:
        self.calls.append((run_id, reason))
        return True


class _CountingReconciler:
    """Observes the ADR 0012 ceiling on reconcile-then-retry."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def reconcile_stale_reservations(self) -> int:
        self.calls += 1
        return self.inner.reconcile_stale_reservations()


class _Exploding:
    def reconcile_stale_reservations(self) -> int:
        raise RuntimeError("control store unreadable")


def _rates() -> RateTable:
    return RateTable(
        "test",
        "v1",
        "USD",
        {},
        UnknownModelPolicy.USE_FALLBACK,
        ModelRates(5_000_000, 30_000_000, 500_000, 5_000_000),
    )


def _store(tmp_path, stamped_at: datetime):
    """A control store whose writes are stamped at ``stamped_at``.

    Reservations planted in the past are what make them eligible for the
    existing reconciliation grace, exactly as a leg that died minutes ago is.
    """

    store = SQLiteControlStore(
        str(tmp_path / "control.db"), now=lambda: stamped_at
    )
    store.initialize()
    install_workflow(store, with_issue_recovery(resolve_snapshot(ISSUE)))
    published: list[EventEnvelope] = []
    producer = Producer("deployment", REPOSITORY, "fix", 123, "boot")
    sequence = [0]

    def factory(payload, correlation):
        index = sequence[0]
        sequence[0] += 1
        stamp = stamped_at + timedelta(milliseconds=index)
        return EventEnvelope(
            payload,
            new_event_id(stamp),
            index,
            stamp,
            stamp,
            index,
            producer,
            correlation,
        )

    store.bind_event_outbox(factory, published.append)
    return store, published


def _reconciled(published) -> list[AdmissionReconciled]:
    return [
        event.payload
        for event in published
        if isinstance(event.payload, AdmissionReconciled)
    ]


def _scope(now: datetime):
    return accounting_runtime._issue_scope(REPOSITORY, ISSUE, "USD", now)


def _reserve(store, scope, run_id: str, cap: Money, at: datetime, lease=None):
    identifier = RunId(run_id)
    result = store.reserve(
        ReservationRequest(
            ReservationId(f"{run_id}-reservation"),
            identifier,
            f"dispatch-{run_id}",
            scope,
            PriceQuote("gpt", "card", "v1", at, LEG_MAXIMUM),
            cap,
            "config",
            REPOSITORY,
            owner_id=OWNER,
        ),
        lease=lease,
    )
    assert result.admitted, result.detail
    return identifier


def _dead_leg(store, scope, run_id: str, cap: Money, at: datetime) -> RunId:
    """A leg that died usage-blind and retained its reservation."""

    identifier = _reserve(store, scope, run_id, cap, at)
    assert store.record_reservation_retained(
        identifier, "trusted usage and provider charge unavailable"
    )
    return identifier


def _live_leg(store, scope, run_id: str, cap: Money, at: datetime) -> RunId:
    """A leg that is still running and still owns a verifiable lease."""

    identity = ProcessSupervisor.process_identity(os.getpid())
    assert identity is not None
    return _reserve(
        store,
        scope,
        run_id,
        cap,
        at,
        lease=Lease(
            RunId(run_id),
            f"dispatch-{run_id}",
            OWNER,
            identity.pid,
            os.getpgid(identity.pid),
            at,
            identity.start_ticks,
        ),
    )


def _sweeper(store, now: datetime):
    coordinator = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Commands(),
        _NoTrustedUsage(),
        _Abandonments(),
        OWNER,
        reservation_grace=GRACE,
        now=lambda: now,
    )
    return _SweepReservationReconciler(store, OWNER, coordinator.reconcile)


def _admission(store, runtime, cap: Money, reconciler):
    late = None
    if reconciler is not None:
        late = _LateReservationReconciler()
        late.bind(reconciler)
    wrapper = AccountingRuntime(
        runtime,
        store,
        REPOSITORY,
        "fix",
        None,
        None,
        cap,
        "cfg",
        "codex",
        BudgetPolicy({("fix", "RESOLVE"): LEG_MAXIMUM}),
        _rates(),
        owner_id=OWNER,
        reservation_reconciler=late,
    )
    wrapper.set_context("fix", "RESOLVE", ISSUE, ISSUE, workflow=resolve_snapshot(ISSUE).binding)
    return wrapper


def _resolve(wrapper):
    return asyncio.run(
        wrapper.run(
            RuntimePlan("resolve", model="gpt-test", max_budget_usd=2)
        )
    )


def test_phantom_reservations_are_reclaimed_instead_of_escalated(tmp_path):
    """The keifu #34 incident: three dead legs must not block the next one."""

    now = datetime.now(timezone.utc)
    # Past the reconciliation grace: the state the issue sits in from the
    # tick after the deaths onward, and the state in which the pre-change
    # harness still refused to dispatch because the label is human-terminal.
    died_at = now - timedelta(minutes=10)
    store, published = _store(tmp_path, died_at)
    scope = _scope(now)
    # MAX_BUDGET_USD leaves room for exactly three concurrent legs.
    cap = Money("USD", 6_000_000)
    dead = tuple(
        _dead_leg(store, scope, f"broken-binary-{index}", cap, died_at)
        for index in range(3)
    )
    assert store.snapshot(scope).reserved == Money("USD", 6_000_000)

    runtime = _Runtime()
    reconciler = _CountingReconciler(_sweeper(store, now))
    result = _resolve(_admission(store, runtime, cap, reconciler))

    assert result.status == "ok"
    assert result.exit_reason is not ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1, "the leg must run, not wait for a human"
    assert reconciler.calls == 1
    active = store.active_reservation_runs(OWNER)
    assert all(run_id not in active for run_id in dead)

    (event,) = _reconciled(published)
    assert event.admitted is True
    assert event.released_runs == 3
    assert event.scope == f"issue:{ISSUE}"


def test_reconciliation_never_releases_a_still_running_leg(tmp_path):
    now = datetime.now(timezone.utc)
    started_at = now - timedelta(minutes=10)
    store, _published = _store(tmp_path, started_at)
    scope = _scope(now)
    cap = Money("USD", 4_000_000)
    dead = _dead_leg(store, scope, "broken-binary", cap, started_at)
    alive = _live_leg(store, scope, "still-running", cap, started_at)

    runtime = _Runtime()
    result = _resolve(_admission(store, runtime, cap, _sweeper(store, now)))

    active = store.active_reservation_runs(OWNER)
    assert alive in active, "a live leg's reservation is never phantom"
    assert dead not in active
    # Only the dead leg's headroom returned, which is enough for one leg.
    assert result.status == "ok"
    assert runtime.calls == 1


def test_a_genuine_cap_exhaustion_still_escalates_to_a_human(tmp_path):
    """Nothing releasable means the pre-incident escalation is unchanged."""

    now = datetime.now(timezone.utc)
    store, published = _store(tmp_path, now)
    scope = _scope(now)
    cap = Money("USD", 4_000_000)
    # Retained just now: inside the reconciliation grace, so recovery still
    # refuses to release them and admission must concede.
    _dead_leg(store, scope, "just-died-a", cap, now)
    _dead_leg(store, scope, "just-died-b", cap, now)

    runtime = _Runtime()
    reconciler = _CountingReconciler(_sweeper(store, now))
    result = _resolve(_admission(store, runtime, cap, reconciler))

    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert result.status == MAX_BUDGET_STATUS
    assert result.envelope_subtype == MAX_BUDGET_STATUS
    assert runtime.calls == 0
    assert len(store.active_reservation_runs(OWNER)) == 2

    (event,) = _reconciled(published)
    assert event.admitted is False
    assert event.released_runs == 0


def test_admission_reconciles_at_most_once_per_dispatch(tmp_path):
    """ADR 0012 ceiling: one reconcile, one retry, no loop."""

    now = datetime.now(timezone.utc)
    store, _published = _store(tmp_path, now)
    scope = _scope(now)
    cap = Money("USD", 2_000_000)
    _dead_leg(store, scope, "just-died", cap, now)

    runtime = _Runtime()
    reconciler = _CountingReconciler(_sweeper(store, now))
    wrapper = _admission(store, runtime, cap, reconciler)

    assert _resolve(wrapper).exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert reconciler.calls == 1

    # A second denial in the same tick escalates after its own single attempt;
    # it neither loops inside one dispatch nor compounds across dispatches.
    assert _resolve(wrapper).exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert reconciler.calls == 2
    assert runtime.calls == 0


def test_unreadable_reconciliation_escalates_without_admitting(tmp_path):
    now = datetime.now(timezone.utc)
    store, published = _store(tmp_path, now)
    cap = Money("USD", 2_000_000)
    _dead_leg(store, _scope(now), "just-died", cap, now)

    runtime = _Runtime()
    result = _resolve(_admission(store, runtime, cap, _Exploding()))

    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 0
    (event,) = _reconciled(published)
    assert event.admitted is False
    assert "reconciliation unavailable" in event.reason


def test_admission_without_a_reconciler_escalates_exactly_as_before(tmp_path):
    # An unbound shim is inert, so a role composed before recovery exists
    # degrades to the pre-incident behaviour rather than inventing headroom.
    assert _LateReservationReconciler().reconcile_stale_reservations() == 0

    now = datetime.now(timezone.utc)
    store, published = _store(tmp_path, now)
    cap = Money("USD", 2_000_000)
    _dead_leg(store, _scope(now), "just-died", cap, now)

    runtime = _Runtime()
    result = _resolve(_admission(store, runtime, cap, None))

    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 0
    assert _reconciled(published) == []


def test_an_admitted_reservation_never_calls_reconciliation(tmp_path):
    now = datetime.now(timezone.utc)
    store, published = _store(tmp_path, now)

    runtime = _Runtime()
    reconciler = _CountingReconciler(_sweeper(store, now))
    result = _resolve(
        _admission(store, runtime, Money("USD", 50_000_000), reconciler)
    )

    assert result.status == "ok"
    assert reconciler.calls == 0
    assert _reconciled(published) == []


class _SettlesExactly:
    """Reconciliation that recovers a small, exact charge for a dead run."""

    def __init__(self, store, amount: Money):
        self.store = store
        self.amount = amount

    def reconcile(self, run_id) -> bool:
        self.store.settle(
            SettlementRequest(
                run_id,
                self.amount,
                PricingBasis.RECONCILED,
                f"recovered-{run_id}",
                None,
                None,
                None,
            )
        )
        return True


def test_headroom_freed_by_exact_settlement_is_counted_and_admitted(tmp_path):
    """A settled run frees headroom without ever reporting unknown spend."""

    now = datetime.now(timezone.utc)
    died_at = now - timedelta(minutes=10)
    store, published = _store(tmp_path, died_at)
    scope = _scope(now)
    # Room for one leg plus the small recovered charge, so admission turns on
    # the reservation being converted to its real, much smaller cost.
    cap = Money("USD", 2_100_000)
    _dead_leg(store, scope, "died-with-receipt", cap, died_at)

    coordinator = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Commands(),
        _SettlesExactly(store, Money("USD", 10_000)),
        _Abandonments(),
        OWNER,
        reservation_grace=GRACE,
        now=lambda: now,
    )
    reconciler = _SweepReservationReconciler(
        store, OWNER, coordinator.reconcile
    )

    runtime = _Runtime()
    result = _resolve(_admission(store, runtime, cap, reconciler))

    assert result.status == "ok"
    assert runtime.calls == 1
    (event,) = _reconciled(published)
    assert event.admitted is True
    # The sweep reported no unknown spend, yet a reservation did go away.
    assert event.released_runs == 1


def test_the_journalled_scope_names_the_cap_that_actually_tripped(tmp_path):
    """An operator reads this to know which cap to move."""

    now = datetime.now(timezone.utc)
    store, published = _store(tmp_path, now)
    issue_cap = Money("USD", 2_000_000)
    _dead_leg(store, _scope(now), "just-died", issue_cap, now)

    runtime = _Runtime()
    late = _LateReservationReconciler()
    late.bind(_sweeper(store, now))
    wrapper = AccountingRuntime(
        runtime,
        store,
        REPOSITORY,
        "fix",
        # A generous daily repository cap is the primary scope; the tight
        # per-issue MAX_BUDGET_USD is the limit that actually denies the leg.
        Money("USD", 500_000_000),
        None,
        issue_cap,
        "cfg",
        "codex",
        BudgetPolicy({("fix", "RESOLVE"): LEG_MAXIMUM}),
        _rates(),
        owner_id=OWNER,
        reservation_reconciler=late,
    )
    wrapper.set_context("fix", "RESOLVE", ISSUE, ISSUE, workflow=resolve_snapshot(ISSUE).binding)

    result = _resolve(wrapper)

    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    (event,) = _reconciled(published)
    assert event.scope == f"issue:{ISSUE}", "not the primary repository scope"
    assert event.admitted is False
