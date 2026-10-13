from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import sqlite3
from zoneinfo import ZoneInfo

from issue_agent.adapters.control_store import (
    AccountingDisposition,
    BudgetLimit,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)
from issue_agent.app.recovery import RecoveryCoordinator
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.domain.contracts import (
    BudgetScope,
    ChargeOrigin,
    Money,
    PricingBasis,
    RunId,
)
from issue_agent.domain.recovery import RecoveryTrigger


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

    def leases(self):
        return (_Lease(RunId("live")),)

    def release_unlaunched(self, run_id, reason):
        if run_id not in self.unlaunched:
            return _Release(_Disposition("conflict"))
        self.released.append((run_id, reason))
        return _Release()

    def settle_terminal_estimate(self, run_id, reason):
        return False

    def release_retained(self, _run_id, _reason, _retained_before):
        return _Release(_Disposition("conflict"))



class _Spend:
    def __init__(self, settled=()):
        self.settled = frozenset(settled)
        self.seen = []

    def reconcile(self, run_id):
        self.seen.append(run_id)
        return run_id in self.settled


class _Worktrees:
    def prune_validate(self):
        return 2




def test_recovery_reconciles_dead_reservation_before_unknown_release():
    control = _Control()
    spend = _Spend((RunId("recovered"),))

    report = RecoveryCoordinator(
        _Supervisor(), control, _Worktrees(),
        spend, "o/r:fix"
    ).reconcile(RecoveryTrigger.BOOT)

    assert report.killed_orphans == ()
    assert report.reclaimed_leases == (RunId("recovered"),)
    assert report.validated_worktrees == 2
    assert report.spend_unknown == ()
    assert spend.seen == [RunId("recovered")]
    assert control.released == []


def test_recovery_never_releases_indeterminate_spend_after_execution():
    control = _Control()
    spend = _Spend()

    report = RecoveryCoordinator(
        _Supervisor(), control, _Worktrees(),
        spend, "o/r:fix"
    ).reconcile(RecoveryTrigger.BOOT)

    assert report.spend_unknown == ()
    assert control.released == []
    assert RunId("live") not in spend.seen


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


    class Worktrees:
        def prune_validate(self):
            calls.append("validate_worktrees")
            return 0


    RecoveryCoordinator(
        Supervisor(),
        Control(),
        Worktrees(),
        _Spend(),
        "o/r:fix",
    ).reconcile(RecoveryTrigger.BOOT)

    assert calls == [
        "reclaim_leases",
        "validate_worktrees",
        "reconstruct_spend",
    ]


def test_wake_and_periodic_triggers_execute_only_their_declared_arms():
    calls = []

    class Supervisor:
        def recover_orphans(self):
            calls.append("reap_all")
            return ()

        def recover_abandoned_orphans(self):
            calls.append("reap_abandoned")
            return ()

    class Control(_Control):
        def reclaim_dead_leases(self, _owner_id, _is_alive):
            calls.append("reclaim_leases")
            return ()

        def leases(self):
            calls.append("read_leases")
            return ()

        def stale_active_reservation_runs(self, _owner_id, _created_before):
            calls.append("read_stale_reservations")
            return ()


    class Worktrees:
        def prune_validate(self):
            calls.append("validate_worktrees")
            return 0


    coordinator = RecoveryCoordinator(
        Supervisor(),
        Control(),
        Worktrees(),
        _Spend(),
        "o/r:fix",
    )

    coordinator.reconcile(RecoveryTrigger.WAKE)
    assert calls == [
        "reap_abandoned",
        "reclaim_leases",
        "validate_worktrees",
        "read_leases",
        "read_stale_reservations",
    ]

    calls.clear()
    coordinator.reconcile(RecoveryTrigger.PERIODIC)
    assert calls == [
        "reclaim_leases",
        "read_leases",
        "read_stale_reservations",
    ]


NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
OWNER = "o/r:fix"


def _reserve(store, run_id, *, now=NOW, runtime="", issue=1):
    """Lease an issue as the daemon and reserve one paid leg bound to it."""
    scope = BudgetScope(
        "o/r", "repository", "USD", now.date().isoformat(),
        now.replace(hour=0), now.replace(hour=0) + timedelta(days=1), ZoneInfo("UTC"),
    )
    assert store.lease("o/r", "fix", issue, OWNER)
    assert store.reserve(ReservationRequest(
        run_id, "o/r", "fix", "RESOLVE", OWNER, Money("USD", 10),
        (BudgetLimit(scope, Money("USD", 100)),), issue, issue=issue, runtime=runtime,
    )).admitted
    return scope


def _leg(store, run_id, columns):
    with closing(sqlite3.connect(store.path)) as db:
        return db.execute(f"SELECT {columns} FROM legs WHERE leg_id=?", (str(run_id),)).fetchone()


def test_dead_lease_with_recovered_spend_keeps_one_abandoned_terminal_outcome(
    tmp_path, monkeypatch
):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    run_id = RunId("crashed")
    _reserve(store, run_id)
    store.register_process_lease(
        run_id, expected_owner_id=OWNER, pid=999_999, pgid=999_999, process_start_ticks=1,
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
                    origin=ChargeOrigin.RECOVERY_ESTIMATE,
                )
            )
            return result.disposition is AccountingDisposition.AVAILABLE

    monkeypatch.setattr(
        "issue_agent.app.recovery._pid_alive", lambda _pid, _start: False
    )
    report = RecoveryCoordinator(
        _Supervisor(), store, _Worktrees(),
        Reconciler(), OWNER
    ).reconcile(RecoveryTrigger.BOOT)

    assert report.reclaimed_leases == (run_id,)
    assert _leg(store, run_id, "state, end_reason, provider_receipt_id, charged_micros") == (
        "ended", "dead_lease", "provider-receipt", 7,
    )
    assert store.leases() == ()


def test_suspend_recovery_preserves_live_owned_lease_and_settlement(
    tmp_path,
):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    identity = ProcessSupervisor.process_identity(os.getpid())
    assert identity is not None
    run_id = RunId("live-owned")
    _reserve(store, run_id)
    store.register_process_lease(
        run_id, expected_owner_id=OWNER, pid=identity.pid,
        pgid=os.getpgid(identity.pid), process_start_ticks=identity.start_ticks,
    )

    class SafeSupervisor:
        def recover_orphans(self):
            raise AssertionError("suspend recovery used the destructive reaper")

        def recover_abandoned_orphans(self):
            return ()

    report = RecoveryCoordinator(
        SafeSupervisor(),
        store,
        _Worktrees(),
        _Spend(),
        OWNER,
        now=lambda: NOW + timedelta(hours=1),
    ).reconcile(RecoveryTrigger.WAKE)

    assert report.reclaimed_leases == ()
    assert store.active_reservation_runs(OWNER) == (run_id,)
    assert tuple(lease.run_id for lease in store.leases(OWNER)) == (run_id,)
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


def test_periodic_sweep_settles_completed_usage_blind_reservation_without_grace(
    tmp_path,
):
    current = [NOW + timedelta(minutes=1)]
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    run_id = RunId("usage-blind")
    scope = _reserve(store, run_id)
    assert store.record_reservation_retained(
        run_id, "trusted usage and provider charge unavailable"
    )
    coordinator = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Spend(),
        OWNER,
        reservation_grace=timedelta(minutes=2),
        now=lambda: current[0],
    )

    assert coordinator.reconcile(RecoveryTrigger.PERIODIC).spend_unknown == ()
    assert store.active_reservation_runs(OWNER) == ()
    assert _leg(store, run_id, "charged_micros, basis") == (0, "estimate")

    current[0] = NOW + timedelta(minutes=3)
    report = coordinator.reconcile(RecoveryTrigger.PERIODIC)

    assert report.spend_unknown == ()
    assert store.active_reservation_runs(OWNER) == ()
    assert store.snapshot(scope).reserved == Money.zero("USD")
    assert _leg(store, run_id, "state, end_reason, charged_micros") == (
        "ended", "trusted usage and provider charge unavailable", 0,
    )


def test_periodic_sweep_releases_only_stale_unlaunched_reservation(
    tmp_path, monkeypatch
):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    run_id = RunId("never-launched")
    _reserve(store, run_id)
    # The daemon that reserved died before spawning: its claim is dead.
    monkeypatch.setattr(
        "issue_agent.app.recovery._pid_alive", lambda _pid, _start: False
    )
    report = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Spend(),
        OWNER,
        reservation_grace=timedelta(minutes=2),
        now=lambda: NOW + timedelta(minutes=3),
    ).reconcile(RecoveryTrigger.PERIODIC)

    assert report.spend_unknown == (run_id,)
    assert store.active_reservation_runs(OWNER) == ()
    assert _leg(store, run_id, "state, end_reason, charged_micros") == (
        "released", "unlaunched_reservation", None,
    )


def test_a_stale_reserved_leg_holding_its_live_lease_is_never_released(tmp_path):
    # A leg is reserved only under its lease, so a spawn that may still
    # register is always visible to the sweep as live work.
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    run_id = RunId("spawn-race")
    _reserve(store, run_id)

    report = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Spend(),
        OWNER,
        reservation_grace=timedelta(minutes=2),
        now=lambda: NOW + timedelta(minutes=3),
    ).reconcile(RecoveryTrigger.PERIODIC)

    assert report.spend_unknown == ()
    assert store.active_reservation_runs(OWNER) == (run_id,)
    assert tuple(lease.run_id for lease in store.leases(OWNER)) == (run_id,)
    store.register_process_lease(
        run_id, expected_owner_id=OWNER, pid=os.getpid(), pgid=os.getpgid(0),
        process_start_ticks=ProcessSupervisor.process_identity(os.getpid()).start_ticks,
    )
    assert _leg(store, run_id, "state") == ("running",)


def test_boot_settles_an_unmeasured_codex_hold_once_and_preserves_money(tmp_path):
    from issue_agent.adapters.spend_recovery import CodexSpendReconciler

    now = datetime.now(timezone.utc)
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: now)
    store.initialize()
    run_id = RunId("9fdbdc47801a41849dc212b941ef6b8f")
    scope = _reserve(store, run_id, now=now, runtime="codex")
    assert store.record_reservation_retained(run_id, "unfinished turns")
    coordinator = RecoveryCoordinator(_Supervisor(), store, _Worktrees(),
        CodexSpendReconciler(store, (str(tmp_path / "missing-evidence"),)), OWNER,
        now=lambda: now)
    assert coordinator.reconcile(RecoveryTrigger.BOOT).spend_unknown == ()
    assert store.active_reservation_runs(OWNER) == ()
    assert store.snapshot(scope).reserved == Money.zero()
    ledger = _leg(store, run_id, "leg_id, charged_micros, basis, settled_at")
    assert coordinator.reconcile(RecoveryTrigger.BOOT).spend_unknown == ()
    assert _leg(store, run_id, "leg_id, charged_micros, basis, settled_at") == ledger
    assert _leg(store, run_id, "state, charged_micros, basis") == ("ended", 0, "estimate")


def test_generic_estimate_recovery_preserves_real_abandonment(tmp_path):
    from tests.adapters.test_spend_recovery import _reserved_store, _end_run_at, END
    store, _scope, run_id = _reserved_store(tmp_path, runtime="claude")
    _end_run_at(store, run_id, END)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE legs SET end_reason='dead_lease' WHERE leg_id=?", (str(run_id),))
        db.commit()
    assert store.settle_terminal_estimate(run_id, "provider evidence unavailable")
    assert store.active_reservation_runs(OWNER) == ()
    assert _leg(store, run_id, "state, end_reason, charged_micros, basis") == (
        "ended", "dead_lease", 0, "estimate",
    )
