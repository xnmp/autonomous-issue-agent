from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import pytest
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.continuation_run_import import collect_continuation_runs
from issue_agent.adapters.resolve_activation import ResolveActivationRefused
from issue_agent.domain.workflow_identity import WorkflowFamily, IssueRef
from tests.adapters.test_control_store import _request, _store, NOW

FIX=(WorkflowFamily.ISSUE_RECOVERY,WorkflowFamily.PR_REPAIR)


def reserve(store,index=1,*,repository='o/r',role='fix'):
    payload=dict(repository=repository,role=role,kind='RESOLVE' if role=='fix' else 'TRIAGE',number=7,issue=7,breaker_key=7)
    request=replace(_request(index,maximum=1,cap=100),owner_id=repository+':'+role+':owner',
        workload_repository=repository,scope=replace(_request(index).scope,repository=repository),
        dispatch_id=json.dumps(payload),kind=payload['kind'])
    assert store.reserve(request).disposition.value=="available"
    return request


def snapshot(store):
    with store.transaction() as db:return collect_continuation_runs(db,'o/r',FIX)


def test_real_reservation_attribution_is_read_only_and_digest_changes_for_evidence(tmp_path):
    store=_store(tmp_path);request=reserve(store)
    first=snapshot(store)
    assert len(first.runs)==1 and first.runs[0].key.target==IssueRef(7)
    assert snapshot(store)==first
    with store.transaction() as db:
        assert db.execute('SELECT workflow_binding_json FROM runs').fetchone()[0] is None
        db.execute("UPDATE runs SET outcome='dead_lease' WHERE run_id=?",(request.run_id,))
    assert snapshot(store).source_digest!=first.source_digest
    reserve(store,2)
    assert len(snapshot(store).runs)==2 and snapshot(store).source_digest!=first.source_digest
    with store.transaction() as db:
        for table in ('reservation_recovery','reservation_limits','budget_reservations','runs'):
            db.execute('DELETE FROM '+table+' WHERE run_id=?',(request.run_id,))
    assert snapshot(store).source_digest!=first.source_digest


def test_known_foreign_and_other_role_evidence_is_isolated(tmp_path):
    store=_store(tmp_path);reserve(store)
    first=snapshot(store)
    reserve(store,2,repository='x/y');reserve(store,3,role='steward')
    assert snapshot(store)==first


@pytest.mark.parametrize('sql',["UPDATE runs SET owner_id='migration'", "UPDATE runs SET kind='GROOM'",
 "UPDATE runs SET dispatch_id='null'", "UPDATE runs SET owner_id='x/y:fix:owner'",
 "UPDATE budget_reservations SET workload_repository='x/y'",
 "UPDATE budget_reservations SET repository='x/y'",
 "UPDATE runs SET dispatch_id='{}'", "UPDATE runs SET owner_id='unknown'",
 "UPDATE runs SET dispatch_id='{" + '"repository":"o/r","repository":"x/y"' + "}'"])
def test_ambiguous_or_conflicting_sql_evidence_refuses_import(tmp_path,sql):
    store=_store(tmp_path);reserve(store)
    with store.transaction() as db:db.execute(sql)
    with pytest.raises(ResolveActivationRefused):snapshot(store)


def imported(store,tmp_path):
    ledger=tmp_path/'spend.jsonl'
    ledger.write_text(json.dumps(dict(event='spend',ts=NOW.isoformat(),repo='o/r',issue=7,cost_usd=1.25,model='legacy'))+'\n')
    assert store.import_legacy_spend(str(ledger)).imported==1


def test_exact_public_legacy_import_is_passive_but_committed_in_snapshot(tmp_path):
    store=_store(tmp_path);empty=snapshot(store)
    imported(store,tmp_path)
    result=snapshot(store)
    assert result.runs==() and result.source_digest!=empty.source_digest
    assert snapshot(store)==result


@pytest.mark.parametrize('sql',["UPDATE charges SET basis='rate_card'", "DELETE FROM charges",
 "UPDATE runs SET outcome='dead_lease'", "UPDATE budget_reservations SET state='released'",
 "UPDATE runs SET dispatch_id='legacy:impostor'", "UPDATE charges SET amount_micros=1",
 "DELETE FROM reservation_limits WHERE dimension='global'"])
def test_passive_accounting_impostors_do_not_authorize_import(tmp_path,sql):
    store=_store(tmp_path);imported(store,tmp_path)
    with store.transaction() as db:db.execute(sql)
    with pytest.raises(ResolveActivationRefused):snapshot(store)


def test_oversize_dispatch_and_row_universe_fail_closed(tmp_path,monkeypatch):
    store=_store(tmp_path);request=reserve(store)
    with store.transaction() as db:db.execute('UPDATE runs SET dispatch_id=?',('x'*65537,))
    with pytest.raises(ResolveActivationRefused):snapshot(store)
    with store.transaction() as db:db.execute('UPDATE runs SET dispatch_id=?',(request.dispatch_id,))
    reserve(store,2);reserve(store,3)
    monkeypatch.setattr('issue_agent.adapters.continuation_run_import.MAX_IMPORT_TARGETS',1)
    with pytest.raises(ResolveActivationRefused):snapshot(store)


def test_foreign_older_dispatch_and_migration_canary_do_not_block_selected_role(tmp_path):
    store=_store(tmp_path);reserve(store);baseline=snapshot(store)
    reserve(store,2,repository='x/y')
    with store.transaction() as db:
        db.execute("UPDATE runs SET owner_id='x/y:migration',kind='migration-canary',dispatch_id='old-canary' WHERE run_id='run-2'")
    assert snapshot(store)==baseline
    with store.transaction() as db:
        db.execute('UPDATE runs SET dispatch_id=? WHERE run_id=?',
                   (json.dumps(dict(repository='o/r',role='migration',kind='migration-canary')),'run-2'))
    with pytest.raises(ResolveActivationRefused):snapshot(store)


@pytest.mark.parametrize('sql',["UPDATE budget_reservations SET model='another-model'",
 "UPDATE reservation_limits SET cap_micros=99", "UPDATE budget_reservations SET release_reason='later'"])
def test_related_accounting_evidence_changes_source_identity(tmp_path,sql):
    store=_store(tmp_path);reserve(store);before=snapshot(store)
    with store.transaction() as db:db.execute(sql)
    assert snapshot(store).source_digest!=before.source_digest


@pytest.mark.parametrize('sql',["UPDATE budget_reservations SET cap_micros=100",
 "UPDATE reservation_limits SET period_timezone='UTC+1'",
 "UPDATE budget_reservations SET quote_effective_at='later'",
 "UPDATE reservation_limits SET period_id='other-period'",
 "UPDATE reservation_limits SET period_start='2026-01-01T00:00:00.000Z'",
 "UPDATE reservation_limits SET period_end='2026-01-02T00:00:00.000Z'"])
def test_passive_import_needs_exact_frozen_import_metadata(tmp_path,sql):
    store=_store(tmp_path);imported(store,tmp_path)
    with store.transaction() as db:db.execute(sql)
    with pytest.raises(ResolveActivationRefused):snapshot(store)


def test_passive_import_requires_canonical_aware_utc_stamps_even_when_all_match(tmp_path):
    store=_store(tmp_path);imported(store,tmp_path)
    with store.transaction() as db:
        db.execute("UPDATE runs SET started_at=substr(started_at,1,length(started_at)-1),completed_at=substr(completed_at,1,length(completed_at)-1)")
        db.execute("UPDATE charges SET settled_at=substr(settled_at,1,length(settled_at)-1)")
        db.execute("UPDATE budget_reservations SET quote_effective_at=substr(quote_effective_at,1,length(quote_effective_at)-1),created_at=substr(created_at,1,length(created_at)-1),updated_at=substr(updated_at,1,length(updated_at)-1)")
    with pytest.raises(ResolveActivationRefused):snapshot(store)


def test_older_missing_repository_keeps_history_and_accounting_read_only(tmp_path):
    store = _store(tmp_path)
    request = reserve(store)
    with store.transaction() as db:
        payload = json.loads(request.dispatch_id)
        del payload['repository']
        db.execute('UPDATE runs SET dispatch_id=? WHERE run_id=?', (json.dumps(payload), request.run_id))
        before = {table: list(db.execute('SELECT * FROM ' + table))
                  for table in ('runs', 'budget_reservations', 'reservation_limits', 'charges')}
    result = snapshot(store)
    assert [(run.run_id, run.key.repository, run.key.target) for run in result.runs] == [
        (request.run_id, 'o/r', IssueRef(7)),
    ]
    with store.transaction() as db:
        assert {table: list(db.execute('SELECT * FROM ' + table)) for table in before} == before


@pytest.mark.parametrize('damage', ['no_reservation', 'foreign_workload', 'foreign_owner', 'wrong_kind', 'missing_role'])
def test_missing_dispatch_repository_does_not_hide_conflicting_or_missing_evidence(tmp_path, damage):
    store = _store(tmp_path)
    request = reserve(store)
    payload = json.loads(request.dispatch_id)
    del payload['repository']
    with store.transaction() as db:
        if damage == 'no_reservation':
            db.execute('DELETE FROM reservation_recovery')
            db.execute('DELETE FROM reservation_limits')
            db.execute('DELETE FROM budget_reservations')
        elif damage == 'foreign_workload':
            db.execute("UPDATE budget_reservations SET workload_repository='x/y'")
        elif damage == 'foreign_owner':
            db.execute("UPDATE runs SET owner_id='x/y:fix:owner'")
        elif damage == 'wrong_kind':
            payload['kind'] = 'GROOM'
        else:
            del payload['role']
        db.execute('UPDATE runs SET dispatch_id=? WHERE run_id=?', (json.dumps(payload), request.run_id))
    with pytest.raises(ResolveActivationRefused):
        snapshot(store)


def migration_canary(store, version):
    from zoneinfo import ZoneInfo
    from issue_agent.adapters.control_store import ReservationRequest, SettlementRequest
    from issue_agent.domain.contracts import Money, PriceQuote, PricingBasis, ReservationId, RunId, daily_budget_scope

    source = 'a' * 16
    suffix = source + ('-' + 'b' * 16 if version == 3 else '') + '-o/r'
    run_id = RunId('migration-canary-' + (f'v{version}-' if version > 1 else '') + suffix)
    now = store.now()
    scope = daily_budget_scope('o/r', 'repository' if version == 1 else 'migration-canary:' + source,
                               'USD', now, ZoneInfo('UTC'))
    request = ReservationRequest(ReservationId('reservation-' + run_id), run_id, 'dispatch-' + run_id,
        scope, PriceQuote('migration-canary', 'migration-canary', '1', now, Money('USD', 1)),
        Money('USD', 9_000_000_000_000_000_000), 'migration-canary-v1', 'o/r',
        kind='migration-canary', owner_id='o/r:migration')
    assert store.reserve(request).admitted
    assert store.settle(SettlementRequest(run_id, Money('USD', 1), PricingBasis.SYNTHETIC,
        'receipt-' + run_id, None, None, None)).disposition.value == 'available'
    return run_id


@pytest.mark.parametrize('version', [1, 2, 3])
def test_exact_migration_verifier_probe_is_passive_read_only_accounting_evidence(tmp_path, version):
    store = _store(tmp_path)
    empty = snapshot(store)
    migration_canary(store, version)
    with store.transaction() as db:
        before = {table: list(db.execute('SELECT * FROM ' + table)) for table in
                  ('runs', 'budget_reservations', 'reservation_limits', 'charges')}
    selected = snapshot(store)
    assert selected.runs == () and selected.source_digest != empty.source_digest
    with store.transaction() as db:
        assert {table: list(db.execute('SELECT * FROM ' + table)) for table in before} == before
        db.execute("UPDATE runs SET completed_at=strftime('%Y-%m-%dT%H:%M:%fZ',completed_at,'+0.001 seconds')")
    assert snapshot(store).source_digest != selected.source_digest


@pytest.mark.parametrize('version', [1, 2, 3])
def test_migration_verifier_probe_is_recognised_when_the_clock_advances_between_writes(tmp_path, version):
    # Native CI caught settlement stamps straddling a millisecond boundary; the
    # verifier then refused its own probe as unattributable history (#249).
    instant = [datetime(2026, 10, 10, 11, 33, 34, tzinfo=timezone.utc)]

    def ticking_clock():
        instant[0] += timedelta(milliseconds=1)
        return instant[0]

    store = SQLiteControlStore(str(tmp_path / 'control.db'), now=ticking_clock)
    store.initialize()
    migration_canary(store, version)

    assert snapshot(store).runs == ()
    with store.transaction() as db:
        settled = db.execute('SELECT settled_at FROM charges').fetchone()[0]
        updated = db.execute('SELECT updated_at FROM budget_reservations').fetchone()[0]
    assert settled == updated


@pytest.mark.parametrize('sql', [
    "UPDATE runs SET owner_id='o/r:fix:owner'", "UPDATE runs SET dispatch_id='other'",
    "UPDATE runs SET outcome='dead_lease'", "UPDATE runs SET started_at=completed_at",
    "UPDATE runs SET config_version='other'", "UPDATE runs SET provider_receipt_id='other'",
    "UPDATE charges SET amount_micros=2",
    "UPDATE charges SET basis='rate_card'", "UPDATE charges SET usage_json='{}'",
    "UPDATE charges SET rate_card_id='other'", "DELETE FROM charges",
    "UPDATE budget_reservations SET state='released'", "UPDATE budget_reservations SET model='other'",
    "UPDATE budget_reservations SET workload_repository='x/y'", "UPDATE budget_reservations SET maximum_micros=2",
    "UPDATE budget_reservations SET release_reason='other'", "UPDATE reservation_limits SET cap_micros=99",
    "UPDATE budget_reservations SET dimension='migration-canary:bbbbbbbbbbbbbbbb'",
    "DELETE FROM reservation_limits", "UPDATE runs SET completed_at='2026-07-29T00:00:00.000Z'",
    "UPDATE budget_reservations SET quote_effective_at=substr(quote_effective_at,1,length(quote_effective_at)-1)",
])
def test_migration_verifier_impostors_never_authorize_activation(tmp_path, sql):
    store = _store(tmp_path)
    migration_canary(store, 3)
    with store.transaction() as db:
        db.execute(sql)
    with pytest.raises(ResolveActivationRefused):
        snapshot(store)
