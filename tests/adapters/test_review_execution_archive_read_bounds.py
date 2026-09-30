"""Retained unrelated history does not enter certified reads or indexed claim proof."""

import time

from issue_agent.adapters.review_execution_archive_claim import observe_review_execution_archive_claim
from issue_agent.adapters.review_execution_checkpoint_commit import commit_review_execution_checkpoint
from issue_agent.adapters.sql_read_budget import bounded_sql_source_reads
from issue_agent.adapters.workflow_rows import load_review_workflow_row
from tests.adapters.test_review_execution_archive_evidence import _complete_source_passes
from tests.adapters.test_review_execution_checkpoint_commit import _copy_source
from tests.adapters.test_review_execution_control import rig


def _measure(store, read):
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 1
        return 0

    with store.transaction() as db:
        db.set_progress_handler(progress, 1)
        started, cpu_started = time.perf_counter(), time.process_time()
        try:
            with bounded_sql_source_reads(db, 1024 * 1024) as budget:
                result = read(db)
            elapsed, cpu = time.perf_counter() - started, time.process_time() - cpu_started
        finally:
            db.set_progress_handler(None, 0)
    return result, (budget.rows, budget.source_bytes, instructions, elapsed, cpu)


def test_certified_load_and_claim_proof_stay_bounded_across_retained_identity_growth(rig):
    store, key, plan, now = _copy_source(rig)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        assert before is not None
        commit_review_execution_checkpoint(db, key, before.revision, plan.round.sequence, now=now)
        expected = load_review_workflow_row(db, key)
    histories = []
    previous = 0
    for size in (10, 1000, 5000):
        with store.transaction() as db:
            db.executemany(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (("unrelated-history", "o/r", f"retained:{index}", now.isoformat(), "x" * 1024)
                 for index in range(previous, size)),
            )
        previous = size
        loaded, hot = _measure(store, lambda db: load_review_workflow_row(db, key))
        assert loaded == expected
        _complete_source_passes(store, key, now)
        claim, indexed = _measure(store, lambda db: observe_review_execution_archive_claim(
            db, plan.round, indexed_identities=True,
        ))
        assert claim.terminal_kind == "released"
        histories.append((size, claim.source_sha256, hot, indexed))
    assert len({item[1] for item in histories}) == 1
    for position in (2, 3):
        baseline = histories[0][position]
        for item in histories[1:]:
            current = item[position]
            assert current[0] == baseline[0]
            assert current[1] <= baseline[1] + 2048
            assert current[2] <= 2 * baseline[2] + 2048
    # Emit the reproducible profile without asserting noisy wall-clock values.
    print("retained identities | hot rows/bytes/vm/wall/cpu | claim rows/bytes/vm/wall/cpu")
    for size, _, hot, indexed in histories:
        print(size, hot, indexed)
