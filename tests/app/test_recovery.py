from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import sqlite3
from zoneinfo import ZoneInfo

from issue_agent.adapters.control_store import (
    AccountingDisposition,
    Lease,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)
from issue_agent.app.recovery import RecoveryCoordinator
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
    RunId,
)


@dataclass(frozen=True)
class _Disposition:
    value: str = "available"


@dataclass(frozen=True)
class _Release:
    disposition: _Disposition = _Disposition()


@dataclass(frozen=True)
class _Lease:
    run_id: RunId


class _Supervisor:
    def recover_orphans(self):
        return (RunId("orphan"),)

    def recover_abandoned_orphans(self):
        return ()


class _Control:
    def __init__(self, unlaunched=()):
        self.released = []
        self.unlaunched = frozenset(unlaunched)

    def reclaim_dead_leases(self, _owner_id, _is_alive):
        return (RunId("recovered"),)

    def active_reservation_runs(self, _owner_id):
        return (RunId("recovered"), RunId("live"))

    def stale_active_reservation_runs(self, _owner_id, _created_before):
        return (RunId("recovered"), RunId("live"))

    def leases(self, _owner_id):
        return (_Lease(RunId("live")),)

    def release_unlaunched(self, run_id, reason):
        if run_id not in self.unlaunched:
            return _Release(_Disposition("conflict"))
        self.released.append((run_id, reason))
        return _Release()

    def pending_commands(self):
        return ("pending",)


class _Spend:
    def __init__(self, settled=()):
        self.settled = frozenset(settled)
        self.seen = []

    def reconcile(self, run_id):
        self.seen.append(run_id)
        return run_id in self.settled


class _Abandonments:
    def __init__(self):
        self.calls = []

    def abandon_run(self, run_id, reason):
        self.calls.append((run_id, reason))
        return True


class _Worktrees:
    def prune_validate(self):
        return 2


class _Commands:
    def drain_pending_commands(self):
        return 1


def test_recovery_reconciles_dead_reservation_before_unknown_release():
    control = _Control()
    spend = _Spend((RunId("recovered"),))
    abandonments = _Abandonments()

    report = RecoveryCoordinator(
        _Supervisor(), control, _Worktrees(), _Commands(),
        spend, abandonments, "o/r:fix"
    ).recover()

    assert report.killed_orphans == (RunId("orphan"),)
    assert report.reclaimed_leases == (RunId("recovered"),)
    assert report.validated_worktrees == 2
    assert report.recovered_commands == 1
    assert report.spend_unknown == ()
    assert report.pending_commands == 1
    assert spend.seen == [RunId("recovered")]
    assert control.released == []
    assert abandonments.calls == [
        (RunId("recovered"), "process ownership lost")
    ]


def test_recovery_never_releases_indeterminate_spend_after_execution():
    control = _Control()
    spend = _Spend()
    abandonments = _Abandonments()

    report = RecoveryCoordinator(
        _Supervisor(), control, _Worktrees(), _Commands(),
        spend, abandonments, "o/r:fix"
    ).recover()

    assert report.spend_unknown == ()
    assert control.released == []
    assert RunId("live") not in spend.seen
    assert abandonments.calls == [
        (RunId("recovered"), "process ownership lost")
    ]


def test_recovery_completes_when_abandonment_telemetry_raises():
    control = _Control()

    class BrokenTelemetry:
        def abandon_run(self, _run_id, _reason):
            raise OSError("disk full")

    report = RecoveryCoordinator(
        _Supervisor(),
        control,
        _Worktrees(),
        _Commands(),
        _Spend(),
        BrokenTelemetry(),
        "o/r:fix",
    ).recover()

    assert report.spend_unknown == ()
    assert control.released == []


def test_recovery_runs_the_fixed_crash_consistency_sequence():
    calls = []

    class Supervisor:
        def recover_orphans(self):
            calls.append("kill_orphans")
            return ()

        def recover_abandoned_orphans(self):
            calls.append("kill_abandoned_orphans")
            return ()

    class Control(_Control):
        def reclaim_dead_leases(self, _owner_id, _is_alive):
            calls.append("reclaim_leases")
            return ()

        def active_reservation_runs(self, _owner_id):
            calls.append("reconstruct_spend")
            return ()

        def stale_active_reservation_runs(self, _owner_id, _created_before):
            calls.append("reconstruct_spend")
            return ()

        def pending_commands(self):
            calls.append("inspect_pending")
            return ()

    class Worktrees:
        def prune_validate(self):
            calls.append("validate_worktrees")
            return 0

    class Commands:
        def drain_pending_commands(self):
            calls.append("drain_commands")
            return 0

    class Abandonments:
        def abandon_run(self, run_id, reason):
            calls.append("correct_abandonment")
            return True

    RecoveryCoordinator(
        Supervisor(),
        Control(),
        Worktrees(),
        Commands(),
        _Spend(),
        Abandonments(),
        "o/r:fix",
    ).recover()

    assert calls == [
        "kill_orphans",
        "reclaim_leases",
        "validate_worktrees",
        "drain_commands",
        "reconstruct_spend",
        "inspect_pending",
    ]


def test_dead_lease_with_recovered_spend_keeps_one_abandoned_terminal_outcome(
    tmp_path, monkeypatch
):
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    store = SQLiteControlStore(
        str(tmp_path / "control.db"), now=lambda: now
    )
    store.initialize()
    run_id = RunId("crashed")
    scope = BudgetScope(
        "o/r",
        "daily",
        "USD",
        "2026-07-30",
        now.replace(hour=0),
        now.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    request = ReservationRequest(
        ReservationId("reservation"),
        run_id,
        "dispatch",
        scope,
        PriceQuote("gpt", "card", "v1", now, Money("USD", 10)),
        Money("USD", 100),
        "config",
        "o/r",
        owner_id="o/r:fix",
    )
    store.reserve(
        request,
        lease=Lease(run_id, "dispatch", "owner", 999_999, 999_999, now),
    )

    class Reconciler:
        def reconcile(self, recovered_run_id):
            result = store.settle(
                SettlementRequest(
                    recovered_run_id,
                    Money("USD", 7),
                    PricingBasis.RECONCILED,
                    "provider-receipt",
                    None,
                    None,
                    None,
                )
            )
            return result.disposition is AccountingDisposition.AVAILABLE

    abandonments = _Abandonments()
    monkeypatch.setattr(
        "issue_agent.app.recovery._pid_alive", lambda _pid, _start: False
    )
    report = RecoveryCoordinator(
        _Supervisor(), store, _Worktrees(), _Commands(),
        Reconciler(), abandonments, "o/r:fix"
    ).recover()

    with sqlite3.connect(store.path) as db:
        state, outcome, receipt = db.execute(
            "SELECT state, outcome, provider_receipt_id FROM runs WHERE run_id=?",
            (str(run_id),),
        ).fetchone()
    assert report.reclaimed_leases == (run_id,)
    assert (state, outcome, receipt) == (
        "abandoned",
        "dead_lease",
        "provider-receipt",
    )
    assert abandonments.calls == [
        (run_id, "process ownership lost")
    ]


def test_suspend_recovery_preserves_live_owned_lease_and_settlement(
    tmp_path,
):
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    store = SQLiteControlStore(
        str(tmp_path / "control.db"), now=lambda: now
    )
    store.initialize()
    identity = ProcessSupervisor.process_identity(os.getpid())
    assert identity is not None
    run_id = RunId("live-owned")
    scope = BudgetScope(
        "o/r",
        "daily",
        "USD",
        "2026-07-30",
        now.replace(hour=0),
        now.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    request = ReservationRequest(
        ReservationId("live-reservation"),
        run_id,
        "dispatch",
        scope,
        PriceQuote("gpt", "card", "v1", now, Money("USD", 10)),
        Money("USD", 100),
        "config",
        "o/r",
        owner_id="o/r:fix",
    )
    store.reserve(
        request,
        lease=Lease(
            run_id,
            "dispatch",
            "o/r:fix",
            identity.pid,
            os.getpgid(identity.pid),
            now,
            identity.start_ticks,
        ),
    )

    class SafeSupervisor:
        def recover_orphans(self):
            raise AssertionError("suspend recovery used the destructive reaper")

        def recover_abandoned_orphans(self):
            return ()

    abandonments = _Abandonments()
    report = RecoveryCoordinator(
        SafeSupervisor(),
        store,
        _Worktrees(),
        _Commands(),
        _Spend(),
        abandonments,
        "o/r:fix",
        now=lambda: now + timedelta(hours=1),
    ).recover_after_suspend()

    assert report.reclaimed_leases == ()
    assert store.active_reservation_runs("o/r:fix") == (run_id,)
    assert tuple(lease.run_id for lease in store.leases("o/r:fix")) == (
        run_id,
    )
    settled = store.settle(
        SettlementRequest(
            run_id,
            Money("USD", 7),
            PricingBasis.RECONCILED,
            "live-provider-receipt",
            None,
            None,
            None,
        )
    )
    assert settled.disposition is AccountingDisposition.AVAILABLE
    assert abandonments.calls == []


def test_periodic_sweep_retains_usage_blind_reservation_after_grace(
    tmp_path,
):
    reserved_at = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    current = [reserved_at + timedelta(minutes=1)]
    store = SQLiteControlStore(
        str(tmp_path / "control.db"), now=lambda: reserved_at
    )
    store.initialize()
    run_id = RunId("usage-blind")
    scope = BudgetScope(
        "o/r",
        "daily",
        "USD",
        "2026-07-30",
        reserved_at.replace(hour=0),
        reserved_at.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    store.reserve(
        ReservationRequest(
            ReservationId("usage-blind-reservation"),
            run_id,
            "dispatch",
            scope,
            PriceQuote(
                "gpt", "card", "v1", reserved_at, Money("USD", 10)
            ),
            Money("USD", 100),
            "config",
            "o/r",
            owner_id="o/r:fix",
        )
    )
    assert store.record_reservation_retained(
        run_id, "trusted usage and provider charge unavailable"
    )
    coordinator = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Commands(),
        _Spend(),
        _Abandonments(),
        "o/r:fix",
        reservation_grace=timedelta(minutes=2),
        now=lambda: current[0],
    )

    assert coordinator.sweep_reservations().spend_unknown == ()
    assert store.active_reservation_runs("o/r:fix") == (run_id,)

    current[0] = reserved_at + timedelta(minutes=3)
    report = coordinator.sweep_reservations()

    assert report.spend_unknown == ()
    assert store.active_reservation_runs("o/r:fix") == (run_id,)
    with sqlite3.connect(store.path) as db:
        policy, state, outcome = db.execute(
            "SELECT rr.policy, r.state, r.outcome "
            "FROM reservation_recovery rr JOIN runs r "
            "ON r.run_id=rr.run_id WHERE rr.run_id=?",
            (str(run_id),),
        ).fetchone()
    assert (policy, state, outcome) == (
        "reconcile_only",
        "completed",
        "spend_unreconciled",
    )


def test_periodic_sweep_releases_only_stale_unlaunched_reservation(
    tmp_path,
):
    reserved_at = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    store = SQLiteControlStore(
        str(tmp_path / "control.db"), now=lambda: reserved_at
    )
    store.initialize()
    run_id = RunId("never-launched")
    scope = BudgetScope(
        "o/r",
        "daily",
        "USD",
        "2026-07-30",
        reserved_at.replace(hour=0),
        reserved_at.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    store.reserve(
        ReservationRequest(
            ReservationId("never-launched-reservation"),
            run_id,
            "dispatch",
            scope,
            PriceQuote(
                "gpt", "card", "v1", reserved_at, Money("USD", 10)
            ),
            Money("USD", 100),
            "config",
            "o/r",
            owner_id="o/r:fix",
        )
    )
    report = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Commands(),
        _Spend(),
        _Abandonments(),
        "o/r:fix",
        reservation_grace=timedelta(minutes=2),
        now=lambda: reserved_at + timedelta(minutes=3),
    ).sweep_reservations()

    assert report.spend_unknown == (run_id,)
    assert store.active_reservation_runs("o/r:fix") == ()
    with sqlite3.connect(store.path) as db:
        state, reason = db.execute(
            "SELECT state, release_reason FROM budget_reservations "
            "WHERE run_id=?",
            (str(run_id),),
        ).fetchone()
    assert (state, reason) == ("released", "unlaunched_reservation")


def test_spawn_registration_wins_race_with_periodic_release(tmp_path):
    reserved_at = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    store = SQLiteControlStore(
        str(tmp_path / "control.db"), now=lambda: reserved_at
    )
    store.initialize()
    run_id = RunId("spawn-race")
    scope = BudgetScope(
        "o/r",
        "daily",
        "USD",
        "2026-07-30",
        reserved_at.replace(hour=0),
        reserved_at.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    store.reserve(
        ReservationRequest(
            ReservationId("spawn-race-reservation"),
            run_id,
            "dispatch",
            scope,
            PriceQuote(
                "gpt", "card", "v1", reserved_at, Money("USD", 10)
            ),
            Money("USD", 100),
            "config",
            "o/r",
            owner_id="o/r:fix",
        )
    )

    class RegisteringReconciler:
        def reconcile(self, candidate):
            store.register_lease(
                Lease(
                    candidate,
                    "dispatch",
                    "o/r:fix",
                    123,
                    123,
                    reserved_at,
                    1,
                )
            )
            return False

    report = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Commands(),
        RegisteringReconciler(),
        _Abandonments(),
        "o/r:fix",
        reservation_grace=timedelta(minutes=2),
        now=lambda: reserved_at + timedelta(minutes=3),
    ).sweep_reservations()

    assert report.spend_unknown == ()
    assert store.active_reservation_runs("o/r:fix") == (run_id,)
    assert tuple(lease.run_id for lease in store.leases("o/r:fix")) == (
        run_id,
    )
