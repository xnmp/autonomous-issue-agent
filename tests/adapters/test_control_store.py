"""The 3-table control store: money, leases, idempotency, and the one-way cutover."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters import control_store
from issue_agent.adapters.control_store import (
    AccountingDisposition,
    BudgetLimit,
    ControlStoreError,
    CorruptControlStore,
    CutoverRefused,
    IncompatibleSchema,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
    read_settled_spend,
)
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.domain.contracts import (
    BudgetScope,
    ChargeOrigin,
    Money,
    PricingBasis,
    RunId,
    TokenUsage,
    daily_budget_scope,
)

NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
REPO = "o/r"
OWNER = "o/r:fix:deployment"
LEGACY_SCHEMA = Path(__file__).parent / "fixtures" / "legacy_control_v46.sql"
# The live store's version at the cutover: no workload_repository column on
# reservations and no charge origin yet.
LEGACY_V41_SCHEMA = Path(__file__).parent / "fixtures" / "legacy_control_v41.sql"


def _store(tmp_path: Path, now=lambda: NOW) -> SQLiteControlStore:
    store = SQLiteControlStore(str(tmp_path / "state" / "control.db"), now=now)
    store.initialize()
    return store


def _daily(repository: str = REPO, dimension: str = "repository") -> BudgetScope:
    return daily_budget_scope(repository, dimension, "USD", NOW, ZoneInfo("UTC"))


def _issue_scope(issue: int) -> BudgetScope:
    return BudgetScope(
        REPO, f"issue:{issue}", "USD", f"lifetime:{issue}",
        datetime(1970, 1, 1, tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc), ZoneInfo("UTC"),
    )


def _request(
    index: int, maximum: int = 60, cap: int = 100, *, issue: int | None = None,
    lease_target: int | None = None, limits: tuple[BudgetLimit, ...] | None = None,
    owner: str = OWNER, repository: str = REPO,
) -> ReservationRequest:
    return ReservationRequest(
        RunId(f"run-{index}"), repository, "fix", "RESOLVE", owner, Money("USD", maximum),
        limits or (BudgetLimit(_daily(repository), Money("USD", cap)),),
        lease_target if lease_target is not None else index,
        issue=issue if issue is not None else index, number=index, runtime="claude",
    )


def _reserve(store: SQLiteControlStore, *args, **changes):
    """Lease the request's item as its owner, then reserve: a paid leg's path."""
    request = _request(*args, **changes)
    store.lease(request.repository, request.role, request.lease_target, request.owner_id)
    return store.reserve(request)


def _settlement(run: str, micros: int, receipt: str | None = None, **changes) -> SettlementRequest:
    return SettlementRequest(
        RunId(run), Money("USD", micros), changes.pop("basis", PricingBasis.PROVIDER_REPORTED),
        receipt or f"receipt-{run}", changes.pop("usage", None), None, None, **changes,
    )


def _leg(store: SQLiteControlStore, run: str) -> sqlite3.Row:
    with closing(sqlite3.connect(store.path)) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM legs WHERE leg_id=?", (run,)).fetchone()


def _dead(_pid: int, _ticks: int) -> bool:
    return False


def _launch(store: SQLiteControlStore, index: int, *, issue: int | None = None) -> RunId:
    """Lease, reserve and register one leg's process: a running paid leg."""
    target = issue if issue is not None else index
    assert store.lease(REPO, "fix", target, OWNER)
    assert store.reserve(_request(index, cap=10_000, issue=target, lease_target=target)).admitted
    run = RunId(f"run-{index}")
    store.register_process_lease(run, expected_owner_id=OWNER, pid=4242, pgid=4242, process_start_ticks=7)
    return run


# -- opening -------------------------------------------------------------------


def test_a_fresh_store_has_three_tables_a_schema_version_and_owner_only_permissions(tmp_path):
    store = _store(tmp_path)

    with closing(sqlite3.connect(store.path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"legs", "leases", "idempotency"}
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(store.path)).st_mode & 0o777 == 0o700
    store.initialize()  # reopening an existing store is idempotent


@pytest.mark.parametrize("evidence", ["opened", "-wal", "-shm", ".authority", ".retired-v41-20261013T000000Z"])
def test_a_missing_store_with_evidence_of_prior_use_is_refused_not_recreated(tmp_path, evidence):
    # Recreating it would reset every daily and lifetime cap to zero, and a
    # leftover -wal would be replayed into the "fresh" file.
    path = tmp_path / "state" / "control.db"
    if evidence == "opened":
        _store(tmp_path)
        path.unlink()
    else:
        path.parent.mkdir(mode=0o700)
        Path(f"{path}{evidence}").write_bytes(b"left behind")

    with pytest.raises(CorruptControlStore, match="missing but was used before") as refused:
        SQLiteControlStore(str(path)).initialize()

    assert str(refused.value).count(".initialize.lock" if evidence == "opened" else evidence) >= 1
    if evidence.startswith(".retired-v"):
        # The retired file is the only record of pre-cutover spend.
        assert f"rename {path}{evidence} back to {path}" in str(refused.value)
        assert "Do not delete it" in str(refused.value)
        assert "delete the listed files" not in str(refused.value)
    else:
        assert "restore control.db" in str(refused.value)
    assert not path.exists()


def test_a_stale_wal_is_never_replayed_into_a_recreated_store(tmp_path):
    store = _store(tmp_path)
    assert store.claim_key("fix-comment:o/r:needs-human:7") == "new"
    with closing(sqlite3.connect(store.path)) as holder:  # keep the -wal alive past the delete
        holder.execute("PRAGMA journal_mode=WAL").fetchone()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO idempotency(key, state, claimed_at) VALUES ('only-in-wal', 'done', 'x')")
        holder.commit()
        os.unlink(store.path)
        with pytest.raises(CorruptControlStore, match="-wal"):
            SQLiteControlStore(store.path).initialize()
    assert not os.path.exists(store.path)


@pytest.mark.parametrize("contents", [b"", b"not a database at all, just bytes" * 200])
def test_an_empty_or_damaged_file_is_refused_rather_than_replaced(tmp_path, contents):
    path = tmp_path / "control.db"
    path.write_bytes(contents)

    with pytest.raises(CorruptControlStore):
        SQLiteControlStore(str(path)).initialize()
    assert path.read_bytes() == contents


def test_a_newer_or_foreign_schema_is_refused(tmp_path):
    newer = tmp_path / "newer.db"
    with closing(sqlite3.connect(newer)) as db:
        db.execute("CREATE TABLE legs (leg_id TEXT)")
        db.execute("PRAGMA user_version=2")
    foreign = tmp_path / "foreign.db"
    with closing(sqlite3.connect(foreign)) as db:
        db.execute("CREATE TABLE something_else (x)")

    for path in (newer, foreign):
        with pytest.raises(IncompatibleSchema):
            SQLiteControlStore(str(path)).initialize()


def _initialize_in_process(path: str, start, results) -> None:
    start.wait(timeout=5)
    try:
        SQLiteControlStore(path).initialize()
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        results.put(f"{type(exc).__name__}: {exc}")
    else:
        results.put("ok")


def test_both_role_daemons_booting_at_once_create_one_store(tmp_path):
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    path = str(tmp_path / "control.db")
    workers = [context.Process(target=_initialize_in_process, args=(path, start, results)) for _ in range(4)]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=30)

    assert sorted(results.get(timeout=5) for _ in workers) == ["ok"] * 4
    assert not list(tmp_path.glob("control.db.new-*"))


# -- money -------------------------------------------------------------------


def test_reservations_admit_until_the_cap_and_name_the_limit_that_tripped(tmp_path):
    store = _store(tmp_path)
    issue_limit = BudgetLimit(_issue_scope(7), Money("USD", 100))
    limits = (BudgetLimit(_daily(), Money("USD", 1_000)), issue_limit)

    first = _reserve(store, 1, issue=7, limits=limits)
    second = _reserve(store, 2, issue=7, limits=limits)

    assert first.admitted
    assert second.disposition is AccountingDisposition.EXHAUSTED
    assert second.tripped_dimension == "issue:7"
    assert store.snapshot(_issue_scope(7)).reserved == Money("USD", 60)


def test_the_global_cap_sums_every_repository_and_the_daily_cap_only_its_own(tmp_path):
    store = _store(tmp_path)
    global_limit = BudgetLimit(_daily("*", "global"), Money("USD", 100))

    assert _reserve(store, 1, limits=(BudgetLimit(_daily("a/one"), Money("USD", 60)), global_limit),
                                  repository="a/one", owner="a/one:fix:d").admitted
    blocked = _reserve(store, 2, limits=(BudgetLimit(_daily("b/two"), Money("USD", 60)), global_limit),
                                     repository="b/two", owner="b/two:fix:d")

    assert blocked.tripped_dimension == "global"
    assert store.snapshot(_daily("a/one")).committed == Money("USD", 60)
    assert store.snapshot(_daily("b/two")).committed == Money("USD", 0)


def test_yesterdays_spend_does_not_count_against_todays_cap(tmp_path):
    clock = [NOW - timedelta(days=1)]
    store = _store(tmp_path, now=lambda: clock[0])
    assert _reserve(store, 1, cap=100).admitted
    assert store.settle(_settlement("run-1", 90)).disposition is AccountingDisposition.AVAILABLE
    clock[0] = NOW

    assert _reserve(store, 2, cap=100).admitted
    assert store.snapshot(_daily()).committed == Money("USD", 60)


def test_concurrent_reservations_admit_exactly_one_for_the_final_capacity(tmp_path):
    store = _store(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: _reserve(store, index).disposition, range(1, 9)))

    assert results.count(AccountingDisposition.AVAILABLE) == 1
    assert results.count(AccountingDisposition.EXHAUSTED) == 7


def test_a_replayed_reservation_is_idempotent_and_a_different_one_conflicts(tmp_path):
    store = _store(tmp_path)
    assert _reserve(store, 1).admitted

    assert _reserve(store, 1).disposition is AccountingDisposition.AVAILABLE
    assert _reserve(store, 1, maximum=50).disposition is AccountingDisposition.CONFLICT
    assert store.snapshot(_daily()).reserved == Money("USD", 60)


def test_settlement_is_once_idempotent_and_a_conflicting_receipt_is_refused(tmp_path):
    store = _store(tmp_path)
    _reserve(store, 1)
    _reserve(store, 2, cap=1_000)
    usage = TokenUsage(10, 5, 0, 0)

    first = store.settle(_settlement("run-1", 75, usage=usage))
    replay = store.settle(_settlement("run-1", 75, usage=usage))
    changed = store.settle(_settlement("run-1", 10))
    stolen = store.settle(_settlement("run-2", 10, receipt="receipt-run-1"))

    assert first.disposition is AccountingDisposition.AVAILABLE
    assert replay.idempotent
    assert changed.disposition is AccountingDisposition.CONFLICT
    assert stolen.disposition is AccountingDisposition.CONFLICT
    snapshot = store.snapshot(_daily())
    assert (snapshot.settled, snapshot.reserved) == (Money("USD", 75), Money("USD", 60))


def test_a_normal_return_corrects_a_recovery_estimate_but_not_the_reverse(tmp_path):
    store = _store(tmp_path)
    _reserve(store, 1, cap=1_000)
    _reserve(store, 2, cap=1_000)
    estimate = dict(basis=PricingBasis.ESTIMATE, origin=ChargeOrigin.RECOVERY_ESTIMATE)

    assert store.settle(_settlement("run-1", 0, "estimate-1", **estimate)).disposition.value == "available"
    assert store.settle(_settlement("run-1", 40)).disposition.value == "available"
    assert store.settle(_settlement("run-2", 30)).disposition.value == "available"
    late = store.settle(_settlement("run-2", 0, "estimate-2", **estimate))

    assert late.disposition is AccountingDisposition.CONFLICT
    assert store.snapshot(_daily()).settled == Money("USD", 70)


def test_release_returns_capacity_charges_nothing_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    _reserve(store, 1)

    released = store.release_unlaunched(RunId("run-1"), "unlaunched")
    replay = store.release_unlaunched(RunId("run-1"), "unlaunched")
    other_reason = store.release_unlaunched(RunId("run-1"), "different")

    assert released.disposition is AccountingDisposition.AVAILABLE
    assert replay.idempotent
    assert other_reason.disposition is AccountingDisposition.CONFLICT
    assert store.snapshot(_daily()).committed == Money("USD", 0)
    assert store.settle(_settlement("run-1", 5)).disposition is AccountingDisposition.CONFLICT
    assert _reserve(store, 2).admitted


def test_only_a_never_launched_leg_is_released_as_unlaunched(tmp_path):
    store = _store(tmp_path)
    _reserve(store, 1)
    running = _launch(store, 2)

    assert store.release_unlaunched(RunId("run-1"), "unlaunched").disposition is AccountingDisposition.AVAILABLE
    refused = store.release_unlaunched(running, "unlaunched")

    assert refused.disposition is AccountingDisposition.CONFLICT
    assert _leg(store, "run-2")["state"] == "running"


def test_an_interrupted_leg_is_settled_at_zero_only_once_its_process_is_gone(tmp_path):
    store = _store(tmp_path)
    run = _launch(store, 1)
    # Out of scope: another repository's and another role's leases (whose pids
    # this daemon may not even see in its PID namespace) are never reclaimed.
    store.lease("x/y", "fix", 3, "x/y:fix:d")
    store.lease(REPO, "review", 4, f"{REPO}:review:d")

    assert not store.settle_terminal_estimate(run, "no evidence")  # live lease, no end
    assert store.reclaim_dead_leases(OWNER, _dead) == (run,)
    assert sorted((lease.repository, lease.role) for lease in store.leases()) == [(REPO, "review"), ("x/y", "fix")]
    assert store.settle_terminal_estimate(run, "no evidence")

    leg = _leg(store, "run-1")
    assert (leg["state"], leg["charged_micros"], leg["basis"], leg["origin"]) == (
        "ended", 0, "estimate", "recovery_estimate",
    )
    assert store.snapshot(_daily()).committed == Money("USD", 0)


def test_retained_spend_stays_reserved_until_recovery_and_reports_its_end(tmp_path):
    store = _store(tmp_path)
    run = _launch(store, 1)

    assert store.record_reservation_retained(run, "runtime failed after launch")
    assert store.stale_active_reservation_runs(OWNER, NOW - timedelta(hours=1)) == (run,)
    assert store.run_completed_at(run) == NOW.timestamp()
    assert store.snapshot(_daily()).reserved == Money("USD", 60)
    assert store.leases() == ()
    assert store.release_unlaunched(run, "unmeasured").disposition is AccountingDisposition.CONFLICT
    assert store.settle_terminal_estimate(run, "unmeasured")
    assert store.snapshot(_daily()).committed == Money("USD", 0)


def test_shutdown_marks_this_deployments_unsettled_legs_without_releasing_them(tmp_path):
    store = _store(tmp_path)
    run = _launch(store, 1, issue=9)
    _reserve(store, 2, cap=1_000, owner="o/r:fix:other")
    ended = _launch(store, 3)
    assert store.record_reservation_retained(ended, "runtime failed after launch")  # ended before shutdown

    intents = store.record_owned_cancellation_intents(OWNER, "shutdown_deadline_spend_unknown")

    assert [(i.run_id, i.kind, i.issue_number, i.reason) for i in intents] == [
        (run, "RESOLVE", 9, "shutdown_deadline_spend_unknown"),
    ]
    assert store.has_cancellation_intent(run)
    assert not store.has_cancellation_intent(RunId("run-2"))
    assert not store.has_cancellation_intent(ended)
    # Once the cancelled leg ends unsettled, it is still shutdown's to hand over.
    assert store.record_reservation_retained(run, "killed")
    assert not store.has_cancellation_intent(run)
    assert [i.run_id for i in store.record_owned_cancellation_intents(OWNER, "shutdown_deadline_spend_unknown")] == [run]
    assert store.snapshot(_daily()).reserved == Money("USD", 180)


@pytest.mark.parametrize("damage", ["garbage", "deleted"])
def test_an_unreadable_authority_refuses_paid_admission(tmp_path, damage):
    store = _store(tmp_path)
    if damage == "garbage":
        Path(store.path).write_bytes(b"\0garbage" * 4096)
    else:
        os.unlink(store.path)

    result = store.reserve(_request(1))

    assert not result.admitted
    assert result.disposition in (AccountingDisposition.CORRUPT, AccountingDisposition.UNAVAILABLE)
    assert not os.path.exists(store.path) or damage == "garbage"


# -- legs ----------------------------------------------------------------------


def test_ended_legs_carry_their_returned_class_session_and_result(tmp_path):
    store = _store(tmp_path)
    run = _launch(store, 1, issue=5)
    assert store.leg_runs(REPO, (5,)) == ()  # not ended yet

    assert store.record_result(run, "asked", session="s-1", result={"question": "which?"})
    assert not store.record_result(run, "agent")
    store.settle(_settlement("run-1", 10))
    store.record_unpaid_leg(RunId("refused"), repository=REPO, role="fix", kind="FIX_CI", owner_id=OWNER,
                            issue=5, number=12, head="abc", runtime="claude")

    runs = sorted(store.leg_runs(REPO, (5, 6, -1)), key=lambda run: run.kind)
    assert [(r.kind, r.number, r.issue, r.head) for r in runs] == [("FIX_CI", 12, 5, "abc"), ("RESOLVE", 1, 5, "")]
    assert runs[0].receipt is None
    assert runs[1].receipt == {"question": "which?", "leg": "asked", "session": "s-1", "runtime": "claude"}
    assert runs[1].started_at is not None


def test_leg_reads_are_bounded_for_huge_issue_lists(tmp_path):
    store = _store(tmp_path)
    store.record_unpaid_leg(RunId("r"), repository=REPO, role="review", kind="REVIEW", owner_id=OWNER,
                            issue=1_999, number=3, head="h", runtime="codex")

    assert [run.issue for run in store.leg_runs(REPO, range(1, 3_000))] == [1_999]


# -- leases ----------------------------------------------------------------------


def test_one_role_holds_one_item_and_the_other_role_may_hold_it_too(tmp_path):
    store = _store(tmp_path)

    assert store.lease(REPO, "fix", 7, OWNER)
    assert not store.lease(REPO, "fix", 7, "o/r:fix:other")
    assert store.lease(REPO, "review", 7, "o/r:review:d")
    assert store.lease(REPO, "fix", 8, OWNER)
    assert store.held_targets(REPO) == frozenset({7, 8})
    assert store.unlease(REPO, "fix", 7, OWNER)
    assert store.lease(REPO, "fix", 7, "o/r:fix:other")


def test_a_dead_holders_lease_is_taken_over_unless_its_leg_still_runs(tmp_path, monkeypatch):
    store = _store(tmp_path)
    assert store.lease(REPO, "fix", 1, OWNER)
    run = _launch(store, 2)
    monkeypatch.setattr(ProcessSupervisor, "matches_identity", classmethod(lambda cls, pid, ticks: False))

    assert store.lease(REPO, "fix", 1, "o/r:fix:other")  # holder gone, nothing bound
    assert not store.lease(REPO, "fix", 2, "o/r:fix:other")  # its running leg holds it
    assert store.held_targets(REPO) == frozenset({2})  # every holder process reads as gone
    assert _leg(store, str(run))["state"] == "running"


def test_a_spawned_process_takes_over_its_legs_lease(tmp_path, monkeypatch):
    store = _store(tmp_path)
    run = _launch(store, 1)
    lease = store.leases()[0]
    assert (lease.run_id, lease.pid, lease.pgid, lease.process_start_ticks) == (run, 4242, 4242, 7)
    assert _leg(store, "run-1")["state"] == "running"

    store.register_process_lease(run, expected_owner_id=OWNER, pid=4242, pgid=4242, process_start_ticks=7)
    with pytest.raises(ControlStoreError):
        store.register_process_lease(run, expected_owner_id="o/r:fix:other", pid=5, pgid=5, process_start_ticks=1)
    # A leg is one launch: a second process never takes over a running leg,
    # even once the first group is gone.
    monkeypatch.setattr(ProcessSupervisor, "matches_identity", classmethod(lambda cls, pid, ticks: False))
    with pytest.raises(ControlStoreError, match="conflicting"):
        store.register_process_lease(run, expected_owner_id=OWNER, pid=5, pgid=5, process_start_ticks=1)
    assert store.leases()[0].pid == 4242


def test_a_process_cannot_register_for_an_unleased_or_finished_leg(tmp_path):
    store = _store(tmp_path)
    _reserve(store, 1)
    assert store.reclaim_dead_leases(OWNER, _dead) == ()  # its daemon died: run-1 holds no lease
    run = _launch(store, 2)
    store.settle(_settlement(str(run), 1))

    for leg in (RunId("run-1"), run, RunId("missing")):
        with pytest.raises(ControlStoreError):
            store.register_process_lease(leg, expected_owner_id=OWNER, pid=9, pgid=9, process_start_ticks=9)


def test_reclaim_probes_liveness_outside_the_write_transaction(tmp_path):
    store = _store(tmp_path)
    _launch(store, 1)

    def probe(_pid: int, _ticks: int) -> bool:
        with closing(sqlite3.connect(store.path, timeout=0)) as other:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        return True

    assert store.reclaim_dead_leases(OWNER, probe) == ()


def test_an_unsettled_leg_keeps_its_lease_until_its_process_is_gone(tmp_path):
    # A leg killed at shutdown may leave usage readable only after it is
    # reaped: unleasing it must not open the way to a zero settlement.
    store = _store(tmp_path)
    run = _launch(store, 1)
    assert store.lease(REPO, "fix", 2, OWNER)

    assert not store.unlease(REPO, "fix", 1, OWNER)
    assert store.unlease(REPO, "fix", 2, OWNER)  # nothing bound: released

    assert [lease.run_id for lease in store.leases()] == [run]
    assert _leg(store, str(run))["ended_at"] is None
    assert not store.settle_terminal_estimate(run, "no evidence")
    assert store.reclaim_dead_leases(OWNER, _dead) == (run,)
    assert store.settle_terminal_estimate(run, "no evidence")
    assert store.unlease(REPO, "fix", 1, OWNER) is False  # reclaim already dropped it


def test_unleasing_a_never_launched_leg_releases_its_reservation_with_the_lease(tmp_path):
    # The runtime's own release can fail on a transient SQLite error; the
    # claim's release must not then keep the reservation and the daemon-pid
    # lease until the daemon restarts.
    store = _store(tmp_path)
    assert _reserve(store, 1, issue=7, lease_target=7).admitted

    assert store.unlease(REPO, "fix", 7, OWNER)

    leg = _leg(store, "run-1")
    assert (leg["state"], leg["end_reason"], leg["charged_micros"]) == ("released", "unlaunched_reservation", None)
    assert leg["ended_at"] is not None
    assert store.leases() == ()
    assert store.held_targets(REPO) == frozenset()
    assert store.snapshot(_daily()).committed == Money("USD", 0)
    assert store.lease(REPO, "fix", 7, "o/r:fix:other")


# -- idempotency -------------------------------------------------------------------


def test_a_claimed_comment_key_survives_a_crash_and_is_read_back(tmp_path):
    store = _store(tmp_path)
    assert store.claim_key("fix-comment:o/r:needs-human:7") == "new"

    reopened = SQLiteControlStore(store.path)  # the process died before confirming
    reopened.initialize()
    assert reopened.claim_key("fix-comment:o/r:needs-human:7") == "claimed"
    reopened.confirm_key("fix-comment:o/r:needs-human:7")
    assert SQLiteControlStore(store.path).claim_key("fix-comment:o/r:needs-human:7") == "done"


# -- read model ----------------------------------------------------------------------


def test_the_settled_spend_read_model_groups_charges_by_repository(tmp_path):
    store = _store(tmp_path)
    _reserve(store, 1)
    _reserve(store, 2, repository="a/b", owner="a/b:fix:d")
    _reserve(store, 3, cap=1_000)
    store.settle(_settlement("run-1", 40))
    store.settle(_settlement("run-2", 25))
    start = NOW.replace(hour=0)

    spend = read_settled_spend(store.path, start, start + timedelta(days=1))

    assert spend.total == Money("USD", 65)
    assert spend.for_repository("O/R") == Money("USD", 40)
    assert spend.for_repository("never/ran") == Money("USD", 0)


def test_the_read_model_never_creates_or_cuts_over_a_store(tmp_path):
    start = NOW.replace(hour=0)
    missing = tmp_path / "missing.db"
    legacy = _legacy(tmp_path / "legacy.db")
    before = legacy.read_bytes()

    assert read_settled_spend(str(missing), start, start + timedelta(days=1)).total is None
    assert read_settled_spend(str(legacy), start, start + timedelta(days=1)).total is None
    assert not missing.exists()
    assert legacy.read_bytes() == before


# -- cutover -------------------------------------------------------------------------


def _legacy(
    path: Path, *, runs: tuple[tuple, ...] = (), comments: tuple[tuple, ...] = (), schema: Path = LEGACY_SCHEMA,
) -> Path:
    """A legacy store (v46 unless told otherwise). Each run: (run_id,
    run_state, reservation_state, charged_micros | None, issue, created_at, lease?)."""
    v41 = schema == LEGACY_V41_SCHEMA
    with closing(sqlite3.connect(path)) as db:
        db.executescript(schema.read_text())
        db.execute("PRAGMA journal_mode=WAL")
        for run_id, run_state, reservation_state, charged, issue, created_at, lease in runs:
            dispatch = (
                f'{{"issue":{issue},"kind":"RESOLVE","number":{issue},"repository":"{REPO}",'
                '"role":"fix","runtime":"codex"}'
            )
            db.execute(
                "INSERT INTO runs(run_id, dispatch_id, kind, owner_id, state, config_version, started_at, "
                "completed_at, outcome, provider_receipt_id) VALUES (?, ?, 'RESOLVE', ?, ?, 'v', ?, ?, ?, ?)",
                (run_id, dispatch, OWNER, run_state, created_at,
                 created_at if run_state == "completed" else None,
                 "settled" if charged is not None else None,
                 f"receipt-{run_id}" if charged is not None else None),
            )
            day = created_at[:10]
            workload = "" if v41 else "workload_repository, "
            db.execute(
                f"INSERT INTO budget_reservations(reservation_id, run_id, repository, {workload}"
                "dimension, period_id, period_start, period_end, period_timezone, currency, maximum_micros, "
                "cap_micros, model, rate_card_id, rate_card_version, quote_effective_at, config_version, "
                f"state, created_at, updated_at) VALUES (?, ?, ?, {'' if v41 else '?, '}'repository', ?, ?, ?, "
                "'UTC', 'USD', 500, 1000, 'gpt', 'card', 'v', ?, 'v', ?, ?, ?)",
                (f"res-{run_id}", run_id, REPO, *(() if v41 else (REPO,)), day, f"{day}T00:00:00.000Z",
                 f"{day}T23:59:59.999Z", created_at, reservation_state, created_at, created_at),
            )
            db.execute(
                "INSERT INTO reservation_limits(run_id, repository, dimension, period_id, period_start, "
                "period_end, period_timezone, currency, cap_micros) VALUES (?, ?, ?, ?, ?, ?, 'UTC', 'USD', 10000)",
                (run_id, REPO, f"issue:{issue}", f"lifetime:{issue}", "1970-01-01T00:00:00.000Z",
                 "9999-12-31T23:59:59.999Z"),
            )
            db.execute(
                "INSERT INTO reservation_recovery(run_id, policy, updated_at) VALUES (?, 'running', ?)",
                (run_id, created_at),
            )
            if charged is not None:
                db.execute(
                    "INSERT INTO charges(run_id, provider_receipt_id, amount_micros, currency, basis, settled_at"
                    f"{'' if v41 else ', origin'}) VALUES (?, ?, ?, 'USD', 'provider_reported', ?"
                    f"{'' if v41 else ", 'normal_return'"})",
                    (run_id, f"receipt-{run_id}", charged, created_at),
                )
            if lease:
                db.execute(
                    "INSERT INTO leases(run_id, dispatch_id, owner_id, pid, pgid, process_start_ticks, started_at) "
                    "VALUES (?, ?, ?, 1, 1, 1, ?)", (run_id, dispatch, OWNER, created_at),
                )
        for scope, key, payload in comments:
            db.execute(
                "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, payload_json) "
                "VALUES (?, ?, ?, '2026-07-01T00:00:00.000Z', ?)",
                ("fix-comment", scope, key, payload),
            )
        db.execute(
            "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, payload_json) "
            "VALUES ('workflow-admission', 'o/r', 'k', '2026-07-01T00:00:00.000Z', '{}')"
        )
        db.commit()
    return path


TODAY = "2026-07-30T09:00:00.000Z"
SETTLED = (
    ("today", "completed", "settled", 300, 7, TODAY, False),
    ("earlier", "completed", "settled", 200, 7, "2026-07-01T09:00:00.000Z", False),
    ("other-issue", "completed", "settled", 50, 8, TODAY, False),
    ("released", "cancelled", "released", None, 7, TODAY, False),
)


def test_cutover_carries_settled_spend_into_the_caps_and_retires_the_legacy_file(tmp_path):
    path = _legacy(tmp_path / "control.db", runs=SETTLED,
                   comments=(("o/r", "needs-human:7", '"done"'), ("o/r", "needs-human:8", '"claimed"')))
    legacy_bytes_digest = hashlib.sha256(path.read_bytes()).hexdigest()

    store = SQLiteControlStore(str(path), now=lambda: NOW)
    store.initialize()

    retired = list(tmp_path.glob("control.db.retired-v46-*"))
    assert [p.name for p in retired] == ["control.db.retired-v46-20260730T120000Z"]
    with closing(sqlite3.connect(retired[0])) as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 3
    assert hashlib.sha256(path.read_bytes()).hexdigest() != legacy_bytes_digest
    assert store.snapshot(_daily()).settled == Money("USD", 350)
    assert store.snapshot(_daily("*", "global")).settled == Money("USD", 350)
    assert store.snapshot(_issue_scope(7)).settled == Money("USD", 500)
    capped = store.reserve(_request(1, maximum=60, issue=7,
                                    limits=(BudgetLimit(_issue_scope(7), Money("USD", 520)),)))
    assert capped.tripped_dimension == "issue:7"
    # Money only: attempts restart, so carried legs are no legs to derive.
    assert store.leg_runs(REPO, (7, 8)) == ()
    assert store.claim_key("fix-comment:o/r:needs-human:7") == "done"
    assert store.claim_key("fix-comment:o/r:needs-human:8") == "claimed"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 2
    store.initialize()  # the cutover runs once
    assert len(list(tmp_path.glob("control.db.retired-*"))) == 1


def test_a_v41_store_cuts_over_with_its_spend_in_the_caps(tmp_path):
    path = _legacy(tmp_path / "control.db", runs=SETTLED, schema=LEGACY_V41_SCHEMA)

    store = SQLiteControlStore(str(path), now=lambda: NOW)
    store.initialize()

    assert [p.name for p in tmp_path.glob("control.db.retired-*")] == ["control.db.retired-v41-20260730T120000Z"]
    assert store.snapshot(_daily()).settled == Money("USD", 350)
    assert store.snapshot(_daily("*", "global")).settled == Money("USD", 350)
    assert store.snapshot(_issue_scope(7)).settled == Money("USD", 500)
    with closing(sqlite3.connect(path)) as db:
        assert set(db.execute("SELECT repository, origin FROM legs")) == {(REPO, "normal_return")}


def test_a_lost_store_restored_from_its_retired_file_keeps_the_pre_cutover_spend(tmp_path):
    path = _legacy(tmp_path / "control.db", runs=SETTLED)
    SQLiteControlStore(str(path), now=lambda: NOW).initialize()
    path.unlink()

    with pytest.raises(CorruptControlStore, match="Do not delete it"):
        SQLiteControlStore(str(path), now=lambda: NOW).initialize()
    for stale in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if stale.exists():
            stale.rename(tmp_path / f"aside{stale.name[-4:]}")
    (retired,) = tmp_path.glob("control.db.retired-v46-*")
    retired.rename(path)

    store = SQLiteControlStore(str(path), now=lambda: NOW)
    store.initialize()
    assert store.snapshot(_daily()).settled == Money("USD", 350)
    assert store.snapshot(_issue_scope(7)).settled == Money("USD", 500)


def test_a_cutover_that_would_move_a_charge_out_of_a_cap_scope_is_refused(tmp_path):
    path = _legacy(tmp_path / "control.db", runs=SETTLED)
    with closing(sqlite3.connect(path)) as db:  # the leg's dispatch says issue 7, its lifetime cap was issue 9
        db.execute("UPDATE reservation_limits SET dimension='issue:9' WHERE run_id='earlier'")
        db.commit()

    with pytest.raises(CorruptControlStore, match=r"drop 1 charge\(s\).*earlier from o/r issue:9"):
        SQLiteControlStore(str(path)).initialize()

    # Only the journal mode changed (the previous release turns WAL back on).
    _assert_legacy_intact(path)
    assert not list(tmp_path.glob("control.db.retired-*"))
    assert not list(tmp_path.glob("control.db.cutover-*"))


def test_a_legacy_store_without_a_recorded_version_is_refused_as_corrupt(tmp_path):
    path = _legacy(tmp_path / "control.db", runs=SETTLED)
    with closing(sqlite3.connect(path)) as db:
        db.execute("DELETE FROM schema_migrations")
        db.commit()

    with pytest.raises(CorruptControlStore, match="no schema version"):
        SQLiteControlStore(str(path)).initialize()


@pytest.mark.parametrize("unsettled", [
    ("live", "running", "active", None, 7, TODAY, True),
    ("reserved", "reserved", "active", None, 7, TODAY, False),
    ("unswept", "abandoned", "active", None, 7, TODAY, False),
])
def test_cutover_is_refused_while_legacy_money_is_unsettled(tmp_path, unsettled):
    path = _legacy(tmp_path / "control.db", runs=(*SETTLED, unsettled))
    before = path.read_bytes()

    with pytest.raises(CutoverRefused, match=r"1 unsettled legacy run\(s\) \(" + unsettled[0]):
        SQLiteControlStore(str(path)).initialize()

    assert path.read_bytes() == before
    assert not list(tmp_path.glob("control.db.retired-*"))
    assert not list(tmp_path.glob("control.db.cutover-*"))


def test_cutover_is_refused_while_the_previous_release_holds_the_store_open(tmp_path):
    path = _legacy(tmp_path / "control.db", runs=SETTLED)
    with closing(sqlite3.connect(path)) as holder:
        holder.execute("SELECT COUNT(*) FROM runs").fetchone()
        with pytest.raises(CutoverRefused, match="holds"):
            SQLiteControlStore(str(path), busy_timeout_ms=100).initialize()
    assert not list(tmp_path.glob("control.db.retired-*"))


def _assert_legacy_intact(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*), SUM(amount_micros) FROM charges").fetchone() == (3, 550)


def _crash(*_args, **_kwargs):
    raise OSError("simulated crash")


@pytest.mark.parametrize("step", ["copy", "verify", "retire", "swap"])
def test_a_crash_at_any_cutover_step_leaves_the_legacy_store_and_a_retry_completes(tmp_path, monkeypatch, step):
    path = _legacy(tmp_path / "control.db", runs=SETTLED)
    if step == "copy":
        def partial_copy(new, _legacy):
            new.execute("INSERT INTO idempotency(key, state, claimed_at) VALUES ('half', 'done', 'x')")
            raise OSError("simulated crash")
        monkeypatch.setattr(control_store, "_carry", partial_copy)
    elif step == "verify":
        original = control_store._carry
        def short_copy(new, legacy):
            original(new, legacy)
            new.execute("DELETE FROM legs WHERE leg_id='earlier'")
        monkeypatch.setattr(control_store, "_carry", short_copy)
    elif step == "retire":
        monkeypatch.setattr(control_store.os, "link", _crash)
    else:
        monkeypatch.setattr(control_store.os, "replace", _crash)

    with pytest.raises((OSError, CorruptControlStore)):
        SQLiteControlStore(str(path), now=lambda: NOW).initialize()

    _assert_legacy_intact(path)
    assert not list(tmp_path.glob("control.db.cutover-*"))
    monkeypatch.undo()
    store = SQLiteControlStore(str(path), now=lambda: NOW)
    store.initialize()
    assert store.snapshot(_issue_scope(7)).settled == Money("USD", 500)
    retired = list(tmp_path.glob("control.db.retired-*"))
    assert len(retired) == 1
    _assert_legacy_intact(retired[0])


def test_a_hard_kill_mid_copy_leaves_a_stray_file_the_retry_removes(tmp_path):
    path = _legacy(tmp_path / "control.db", runs=SETTLED)
    stray = tmp_path / "control.db.cutover-deadbeef"
    stray.write_bytes(b"half a database")

    SQLiteControlStore(str(path), now=lambda: NOW).initialize()

    assert not stray.exists()
    assert len(list(tmp_path.glob("control.db.retired-*"))) == 1
