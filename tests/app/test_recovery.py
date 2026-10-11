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
    ChargeOrigin,
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
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

    def leases(self, _owner_id):
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
    ).reconcile(RecoveryTrigger.BOOT)

    assert report.killed_orphans == ()
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
    ).reconcile(RecoveryTrigger.BOOT)

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
    ).reconcile(RecoveryTrigger.BOOT)

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
    ).reconcile(RecoveryTrigger.BOOT)

    assert calls == [
        "reclaim_leases",
        "validate_worktrees",
        "drain_commands",
        "reconstruct_spend",
        "inspect_pending",
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

        def leases(self, _owner_id):
            calls.append("read_leases")
            return ()

        def stale_active_reservation_runs(self, _owner_id, _created_before):
            calls.append("read_stale_reservations")
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

    coordinator = RecoveryCoordinator(
        Supervisor(),
        Control(),
        Worktrees(),
        Commands(),
        _Spend(),
        _Abandonments(),
        "o/r:fix",
    )

    coordinator.reconcile(RecoveryTrigger.WAKE)
    assert calls == [
        "reap_abandoned",
        "reclaim_leases",
        "validate_worktrees",
        "drain_commands",
        "read_leases",
        "read_stale_reservations",
        "inspect_pending",
    ]

    calls.clear()
    coordinator.reconcile(RecoveryTrigger.PERIODIC)
    assert calls == [
        "reclaim_leases",
        "read_leases",
        "read_stale_reservations",
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
        lease=Lease(run_id, "dispatch", request.owner_id, 999_999, 999_999, now),
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

    abandonments = _Abandonments()
    monkeypatch.setattr(
        "issue_agent.app.recovery._pid_alive", lambda _pid, _start: False
    )
    report = RecoveryCoordinator(
        _Supervisor(), store, _Worktrees(), _Commands(),
        Reconciler(), abandonments, "o/r:fix"
    ).reconcile(RecoveryTrigger.BOOT)

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
    ).reconcile(RecoveryTrigger.WAKE)

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


def test_periodic_sweep_settles_completed_usage_blind_reservation_without_grace(
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
    abandonments = _Abandonments()
    coordinator = RecoveryCoordinator(
        _Supervisor(),
        store,
        _Worktrees(),
        _Commands(),
        _Spend(),
        abandonments,
        "o/r:fix",
        reservation_grace=timedelta(minutes=2),
        now=lambda: current[0],
    )

    assert coordinator.reconcile(RecoveryTrigger.PERIODIC).spend_unknown == ()
    assert store.active_reservation_runs("o/r:fix") == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges WHERE run_id=?",
                                (str(run_id),)).fetchone()) == (0, "estimate")

    current[0] = reserved_at + timedelta(minutes=3)
    report = coordinator.reconcile(RecoveryTrigger.PERIODIC)

    assert report.spend_unknown == ()
    assert store.active_reservation_runs("o/r:fix") == ()
    assert store.snapshot(scope).reserved == Money.zero("USD")
    assert abandonments.calls == []
    with sqlite3.connect(store.path) as db:
        policy, reservation_state, reason, run_state, outcome = db.execute(
            "SELECT rr.policy, b.state, b.release_reason, r.state, r.outcome "
            "FROM reservation_recovery rr "
            "JOIN budget_reservations b ON b.run_id=rr.run_id "
            "JOIN runs r ON r.run_id=rr.run_id WHERE rr.run_id=?",
            (str(run_id),),
        ).fetchone()
    assert (
        policy,
        reservation_state,
        reason,
        run_state,
        outcome,
    ) == (
        "reconcile_only",
        "settled",
        None,
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
    ).reconcile(RecoveryTrigger.PERIODIC)

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
    ).reconcile(RecoveryTrigger.PERIODIC)

    assert report.spend_unknown == ()
    assert store.active_reservation_runs("o/r:fix") == (run_id,)
    assert tuple(lease.run_id for lease in store.leases("o/r:fix")) == (
        run_id,
    )


def test_boot_settles_legacy_accounting_hold_once_and_preserves_money_and_journal(tmp_path):
    import json
    from issue_agent.adapters.spend_recovery import CodexSpendReconciler
    from issue_agent.adapters.event_log import EventLog
    from issue_agent.domain.events import Producer

    now = datetime.now(timezone.utc)
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: now)
    store.initialize()
    log_path = tmp_path / "events.jsonl"
    log = EventLog(str(log_path), Producer("deployment", "o/r", "fix", 100, "boot"))
    store.bind_event_outbox(log.prepare, log.append_committed)
    scope = BudgetScope("o/r", "repository", "USD", now.date().isoformat(),
                        now.replace(hour=0), now.replace(hour=0) + timedelta(days=1), ZoneInfo("UTC"))
    run_id = RunId("9fdbdc47801a41849dc212b941ef6b8f")
    assert store.reserve(ReservationRequest(ReservationId("legacy"), run_id,
        json.dumps({"runtime": "codex"}), scope,
        PriceQuote("sol", "card", "v1", now, Money("USD", 50_000_000)), Money("USD", 100_000_000),
        "cfg", "o/r", owner_id="o/r:fix")).admitted
    with store.transaction() as db:
        db.execute("UPDATE runs SET state='completed',outcome='accounting_unavailable',"
                   "started_at=?,completed_at=? WHERE run_id=?", (now.isoformat(), now.isoformat(), str(run_id)))
        db.execute("UPDATE reservation_recovery SET policy='reconcile_only',reason='unfinished turns' WHERE run_id=?",
                   (str(run_id),))
    coordinator = RecoveryCoordinator(_Supervisor(), store, _Worktrees(), _Commands(),
        CodexSpendReconciler(store, (str(tmp_path / "missing-evidence"),)), _Abandonments(), "o/r:fix",
        now=lambda: now)
    assert coordinator.reconcile(RecoveryTrigger.BOOT).spend_unknown == ()
    assert store.active_reservation_runs() == ()
    assert store.snapshot(scope).reserved == Money.zero()
    journal = log_path.read_text()
    assert 'Codex recovery evidence is missing' in journal
    assert coordinator.reconcile(RecoveryTrigger.BOOT).spend_unknown == ()
    assert log_path.read_text() == journal
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")
        assert tuple(db.execute("SELECT state,outcome FROM runs").fetchone()) == ("completed", "settled")


def test_generic_estimate_recovery_preserves_real_abandonment(tmp_path):
    from tests.adapters.test_spend_recovery import _reserved_store, _end_run_at, END
    store, _scope, run_id = _reserved_store(tmp_path, runtime="claude")
    _end_run_at(store, run_id, END)
    with store.transaction() as db:
        db.execute("UPDATE runs SET state='abandoned',outcome='dead_lease' WHERE run_id=?", (str(run_id),))
    assert store.settle_terminal_estimate(run_id, "provider evidence unavailable")
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT state,outcome FROM runs").fetchone()) == ("abandoned", "dead_lease")
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")
