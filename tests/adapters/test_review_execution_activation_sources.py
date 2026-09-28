"""Review execution legacy file and owner-run inventories are bounded and read-only."""

import sqlite3
from contextlib import closing

import pytest

import issue_agent.adapters.review_execution_activation as review_activation
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_activation import (
    MAX_RUN_SOURCE_PAGE, MAX_SUBMISSION_SOURCE_PAGE, read_legacy_execution_roots, read_legacy_execution_state,
    read_complete_legacy_review_runs, read_legacy_review_run_page, read_legacy_review_submission_page,
    revalidate_complete_legacy_review_runs,
    read_legacy_review_submission_receipt_page, MAX_SUBMISSION_RECEIPT_SOURCE_PAGE,
    read_legacy_review_owner_lineage,
    read_legacy_review_round_page, MAX_REVIEW_ROUND_SOURCE_PAGE,
    read_complete_legacy_review_rounds, revalidate_complete_legacy_review_rounds,
    read_legacy_review_outcome_receipt_page, MAX_OUTCOME_RECEIPT_SOURCE_PAGE,
    read_complete_legacy_review_outcome_receipts, revalidate_complete_legacy_review_outcome_receipts,
)
from issue_agent.domain.resolve_activation import (
    MAX_LEGACY_FILE_BYTES, LegacyAbsent, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry,
)
from issue_agent.domain.review_execution_activation import (
    LegacyExecutionCounterRecord, LegacyExecutionPRRecord, LegacyExecutionRunRecord, LegacyReviewRunState,
    UnreadableLegacyExecutionRun, LegacyReviewSubmissionSource, UnreadableLegacyReviewSubmission,
    LegacyReviewSubmissionReceiptSource, UnreadableLegacyReviewSubmissionReceipt,
    LegacyReviewOwnerLineage, UnreadableLegacyReviewOwnerLineage,
    CompleteLegacyReviewRuns, BlockedLegacyReviewRuns,
    LegacyReviewRoundSource, UnreadableLegacyReviewRound, MAX_LEGACY_ROUND_JSON_BYTES,
    CompleteLegacyReviewRounds, BlockedLegacyReviewRounds,
    LEGACY_REVIEW_OUTCOME_RECEIPT_NAMESPACES, MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES,
    LegacyReviewOutcomeReceiptPage, LegacyReviewOutcomeReceiptSource, UnreadableLegacyReviewOutcomeReceipt,
    BlockedLegacyReviewOutcomeReceiptPage,
    CompleteLegacyReviewOutcomeReceipts, BlockedLegacyReviewOutcomeReceipts,
)
from tests.adapters.test_review_execution_control import rig
from tests.adapters.test_review_productive_settlement import confirmed_copy, workflow_for


def test_inventory_keeps_both_counter_namespaces_and_runtime_session_bytes(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pr-7.session").write_text("codex:session-1")
    (state / "pr-7.round").write_text("4")
    (state / "pr-8.round").write_text("2")
    (state / "issue-99.infra-deaths").write_text("1")
    (state / "issue--7.infra-deaths").write_text("3")
    (state / "pr-7.last-sha").write_text("separate rejection source")
    (state / "issue-99.resolve-retries").write_text("separate resolve source")
    before = {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in state.iterdir()}

    entries = read_legacy_execution_state(str(state))

    assert {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in state.iterdir()} == before
    prs = {entry.pr: entry for entry in entries if isinstance(entry, LegacyExecutionPRRecord)}
    counters = {entry.key: entry for entry in entries if isinstance(entry, LegacyExecutionCounterRecord)}
    assert set(prs) == {7, 8}
    assert isinstance(prs[7].session, LegacyPresent) and prs[7].session.text == "codex:session-1"
    assert isinstance(prs[7].round, LegacyPresent) and prs[7].round.text == "4"
    assert prs[8].session == LegacyAbsent()
    assert set(counters) == {-7, 99}
    assert isinstance(counters[-7].infra_deaths, LegacyPresent) and counters[-7].infra_deaths.text == "3"
    assert isinstance(counters[99].infra_deaths, LegacyPresent) and counters[99].infra_deaths.text == "1"


def test_unreadable_files_remain_distinct_from_absent_files(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pr-7.session").write_bytes(b"\xff")
    (state / "pr-8.session").write_bytes(b"x" * (MAX_LEGACY_FILE_BYTES + 1))
    (state / "pr-9.round").mkdir()
    (state / "pr-10.round").symlink_to(state / "pr-7.session")
    (state / "issue--10.infra-deaths").write_bytes(b"\xff")

    entries = read_legacy_execution_state(str(state))
    prs = {entry.pr: entry for entry in entries if isinstance(entry, LegacyExecutionPRRecord)}
    counters = {entry.key: entry for entry in entries if isinstance(entry, LegacyExecutionCounterRecord)}
    assert prs[7].session == LegacyUnreadable("not valid UTF-8")
    assert isinstance(prs[8].session, LegacyUnreadable) and "exceeds" in prs[8].session.reason
    assert prs[9].round == LegacyUnreadable("not a regular file")
    assert prs[10].round == LegacyUnreadable("symbolic link")
    assert counters[-10].infra_deaths == LegacyUnreadable("not valid UTF-8")
    assert prs[7].round == LegacyAbsent()


def test_malformed_legacy_names_remain_visible_and_other_names_are_ignored(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    malformed = (
        "pr-.session", "pr-0.round", "pr-07.session", "pr--7.round", "pr-9223372036854775808.session",
        "issue-.infra-deaths", "issue-0.infra-deaths", "issue--0.infra-deaths",
        "issue-+7.infra-deaths", "issue-07.infra-deaths", "issue--07.infra-deaths",
        "issue-9223372036854775808.infra-deaths", "issue--9223372036854775808.infra-deaths",
    )
    for name in malformed:
        (state / name).write_text("1")
    (state / "issue-7.infra-deaths.tmp").write_text("incomplete write")
    (state / "pr-7.review-retries").write_text("other family")

    entries = read_legacy_execution_state(str(state))

    assert {entry.name for entry in entries if isinstance(entry, UnmappedLegacyEntry)} == set(malformed)
    assert all(isinstance(entry, UnmappedLegacyEntry) for entry in entries)


def test_roots_keep_cross_root_duplicates_but_remove_physical_alias(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "pr-7.session").write_text("codex:old")
    (second / "pr-7.session").write_text("codex:new")
    (first / "issue--7.infra-deaths").write_text("1")
    (second / "issue--7.infra-deaths").write_text("2")
    (tmp_path / "alias").symlink_to(first)

    entries = read_legacy_execution_roots((("first", str(first)), ("alias", str(tmp_path / "alias")),
                                           ("second", str(second))))

    assert [(entry.root, entry.pr) for entry in entries if isinstance(entry, LegacyExecutionPRRecord)] == [
        ("first", 7), ("second", 7),
    ]
    assert [(entry.root, entry.key) for entry in entries if isinstance(entry, LegacyExecutionCounterRecord)] == [
        ("first", -7), ("second", -7),
    ]


def test_missing_and_non_directory_roots_are_explicit(tmp_path):
    assert read_legacy_execution_state(str(tmp_path / "missing")) == ()
    bad = tmp_path / "bad"
    bad.write_text("not a directory")
    (entry,) = read_legacy_execution_state(str(bad))
    assert isinstance(entry, UnmappedLegacyEntry)
    assert entry.root == "review" and "unreadable" in entry.reason


OWNER = "o/r:review:owner"


def test_round_inventory_includes_negative_zero_and_positive_rowids_without_writes(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        for rowid, sequence in ((-1, 2), (0, 3)):
            db.execute(
                "INSERT INTO review_execution_rounds "
                "(rowid,round_id,repository,target_kind,target_number,execution_epoch,execution_episode,"
                "generation,sequence,claim_occurrence_id,plan_json,planning_json,closure_json,outcome_json) "
                "SELECT ?,?,repository,target_kind,target_number,execution_epoch,execution_episode,"
                "generation,?,claim_occurrence_id,plan_json,planning_json,closure_json,outcome_json "
                "FROM review_execution_rounds WHERE round_id=?",
                (rowid, f"inventory-round-{sequence}", sequence, plan.round.round_id),
            )
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        first = read_legacy_review_round_page(db, "o/r", after_rowid=-2)
        assert db.total_changes == before
        db.rollback()

    assert [entry.source_rowid for entry in first.entries] == [-1, 0, 1]
    assert not first.has_more
    assert all(isinstance(entry, LegacyReviewRoundSource) for entry in first.entries)
    assert dict(first.entries[-1].fields)["round_id"] == plan.round.round_id
    assert first.entries[-1].source_digest == LegacyReviewRoundSource(
        -9, first.entries[-1].fields,
    ).source_digest


@pytest.mark.parametrize("damage", ["oversized", "invalid_utf8", "blob_repository"])
def test_round_inventory_keeps_unreadable_rows_visible(rig, damage):
    store, _, _, selection, control = rig
    control.preflight(selection)
    if damage == "blob_repository":
        with closing(store._connect()) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN")
            db.execute("UPDATE review_execution_rounds SET repository=?", (b"o/r",))
            db.commit()
    else:
        with store.transaction() as db:
            if damage == "oversized":
                db.execute("UPDATE review_execution_rounds SET plan_json=?",
                           ("x" * (MAX_LEGACY_ROUND_JSON_BYTES + 1),))
            else:
                db.execute("UPDATE review_execution_rounds SET planning_json=CAST(x'FF' AS TEXT)")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        page = read_legacy_review_round_page(db, "o/r")
        assert db.total_changes == before
        db.rollback()

    assert len(page.entries) == 1
    assert isinstance(page.entries[0], UnreadableLegacyReviewRound)
    assert not page.has_more


def test_round_inventory_pages_at_bounded_row_count(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        for index in range(2, MAX_REVIEW_ROUND_SOURCE_PAGE + 3):
            db.execute(
                "INSERT INTO review_execution_rounds "
                "(round_id,repository,target_kind,target_number,execution_epoch,execution_episode,"
                "generation,sequence,claim_occurrence_id,plan_json,planning_json) "
                "SELECT ?,repository,target_kind,target_number,execution_epoch,execution_episode,"
                "generation,?,claim_occurrence_id,plan_json,planning_json "
                "FROM review_execution_rounds WHERE round_id=?",
                (f"inventory-round-{index}", index, plan.round.round_id),
            )
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        first = read_legacy_review_round_page(db, "o/r")
        second = read_legacy_review_round_page(db, "o/r", after_rowid=first.next_rowid)
        complete = read_complete_legacy_review_rounds(db, "o/r")
        db.rollback()

    assert len(first.entries) == MAX_REVIEW_ROUND_SOURCE_PAGE and first.has_more
    assert len(second.entries) == 2 and not second.has_more
    assert first.next_rowid < second.entries[0].source_rowid
    assert isinstance(complete, CompleteLegacyReviewRounds)
    assert len(complete.rounds) == MAX_REVIEW_ROUND_SOURCE_PAGE + 2


def test_round_inventory_pages_before_exceeding_retained_byte_budget(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO review_execution_rounds "
            "(round_id,repository,target_kind,target_number,execution_epoch,execution_episode,"
            "generation,sequence,claim_occurrence_id,plan_json,planning_json) "
            "SELECT 'second-inventory-round',repository,target_kind,target_number,execution_epoch,"
            "execution_episode,generation,2,claim_occurrence_id,plan_json,planning_json "
            "FROM review_execution_rounds WHERE round_id=?", (plan.round.round_id,),
        )
        large = "x" * MAX_LEGACY_ROUND_JSON_BYTES
        db.execute("UPDATE review_execution_rounds SET plan_json=?,planning_json=?,closure_json=?,outcome_json=?",
                   (large, large, large, large))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        first = read_legacy_review_round_page(db, "o/r")
        second = read_legacy_review_round_page(db, "o/r", after_rowid=first.next_rowid)
        db.rollback()

    assert len(first.entries) == 1 and first.has_more
    assert len(second.entries) == 1 and not second.has_more
    assert isinstance(first.entries[0], LegacyReviewRoundSource)
    assert isinstance(second.entries[0], LegacyReviewRoundSource)


def test_complete_round_inventory_revalidates_same_sql_sources_without_writes(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        observed = read_complete_legacy_review_rounds(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewRounds)
        revalidated = revalidate_complete_legacy_review_rounds(db, observed)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(revalidated, CompleteLegacyReviewRounds)
    assert tuple(dict(source.fields)["round_id"] for source in revalidated.rounds) == (plan.round.round_id,)
    assert revalidated.source_digest == observed.source_digest


def test_complete_round_inventory_rejects_partial_or_changed_observation(rig):
    store, _, _, selection, control = rig
    control.preflight(selection)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_rounds(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewRounds)
        partial = CompleteLegacyReviewRounds("o/r", ())
        assert isinstance(revalidate_complete_legacy_review_rounds(db, partial), BlockedLegacyReviewRounds)
        db.rollback()
    with store.transaction() as db:
        db.execute("UPDATE review_execution_rounds SET plan_json='{}'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = revalidate_complete_legacy_review_rounds(db, observed)
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewRounds)
    assert "changed" in result.reason


def test_complete_round_inventory_blocks_unreadable_source(rig):
    store, _, _, selection, control = rig
    control.preflight(selection)
    with store.transaction() as db:
        db.execute("UPDATE review_execution_rounds SET plan_json=?",
                   ("x" * (MAX_LEGACY_ROUND_JSON_BYTES + 1),))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = read_complete_legacy_review_rounds(db, "o/r")
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewRounds)
    assert result.source_rowid is not None


def test_complete_round_inventory_bounds_unrelated_table_rows(rig, monkeypatch):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with closing(store._connect()) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for index in (2, 3):
            db.execute(
                "INSERT INTO review_execution_rounds "
                "(round_id,repository,target_kind,target_number,execution_epoch,execution_episode,"
                "generation,sequence,claim_occurrence_id,plan_json,planning_json) "
                "SELECT ?,?,target_kind,target_number,execution_epoch,execution_episode,"
                "generation,?,claim_occurrence_id,plan_json,planning_json "
                "FROM review_execution_rounds WHERE round_id=?",
                (f"foreign-round-{index}", "other/repo", index, plan.round.round_id),
            )
        db.commit()
    monkeypatch.setattr(review_activation, "MAX_LEGACY_ROUND_SCAN_ROWS", 2)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = read_complete_legacy_review_rounds(db, "o/r")
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewRounds)
    assert "total scan bound" in result.reason


def test_outcome_receipt_inventory_reads_real_settlement_without_writes(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        page = read_legacy_review_outcome_receipt_page(db, "o/r")
        assert db.total_changes == before
        db.rollback()

    assert len(page.entries) == 1 and not page.has_more
    source = page.entries[0]
    assert isinstance(source, LegacyReviewOutcomeReceiptSource)
    assert dict(source.fields)["namespace"] == "review-round-outcome"
    assert dict(source.fields)["key"] == plan.round.round_id
    assert source.source_digest == LegacyReviewOutcomeReceiptSource(-7, source.fields).source_digest


def test_outcome_receipt_inventory_keeps_all_namespace_and_cursor_sources(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    namespaces = tuple(sorted(LEGACY_REVIEW_OUTCOME_RECEIPT_NAMESPACES))
    with store.transaction() as db:
        for index, namespace in enumerate(namespaces):
            db.execute("INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,?,?,?,?,?)",
                       (index - 2 if index < 3 else None, namespace, "o/r",
                        f"{plan.round.round_id}:{index}", "2026-09-28T00:00:00Z", "{}"))
        for index in range(MAX_OUTCOME_RECEIPT_SOURCE_PAGE):
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,?,?,?,?)",
                       (namespaces[0], "o/r", f"extra-{index}", "2026-09-28T00:00:00Z", "{}"))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        first = read_legacy_review_outcome_receipt_page(db, "o/r")
        second = read_legacy_review_outcome_receipt_page(db, "o/r", after_rowid=first.next_rowid)
        complete = read_complete_legacy_review_outcome_receipts(db, "o/r")
        db.rollback()

    assert len(first.entries) == MAX_OUTCOME_RECEIPT_SOURCE_PAGE and first.has_more
    assert len(second.entries) == len(namespaces) and not second.has_more
    all_entries = (*first.entries, *second.entries)
    assert all(isinstance(entry, LegacyReviewOutcomeReceiptSource) for entry in all_entries)
    assert {dict(entry.fields)["namespace"] for entry in all_entries} == set(namespaces)
    assert {entry.source_rowid for entry in all_entries} >= {-2, -1, 0}
    assert isinstance(complete, CompleteLegacyReviewOutcomeReceipts)
    assert len(complete.receipts) == len(all_entries)


@pytest.mark.parametrize("damage", ["oversized", "null_payload", "blob_scope", "blob_namespace"])
def test_outcome_receipt_inventory_keeps_malformed_sources_visible(rig, damage):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        if damage == "oversized":
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-outcome'",
                       ("x" * (MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES + 1),))
        elif damage == "null_payload":
            db.execute("UPDATE dedupe_keys SET payload_json=NULL WHERE namespace='review-round-outcome'")
        elif damage == "blob_scope":
            db.execute("UPDATE dedupe_keys SET scope=? WHERE namespace='review-round-outcome'", (b"o/r",))
        else:
            db.execute("UPDATE dedupe_keys SET namespace=? WHERE namespace='review-round-outcome'",
                       (b"review-round-outcome",))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        page = read_legacy_review_outcome_receipt_page(db, "o/r")
        db.rollback()

    assert len(page.entries) == 1
    assert isinstance(page.entries[0], UnreadableLegacyReviewOutcomeReceipt)


def test_outcome_receipt_page_bounds_unrelated_row_scans(monkeypatch):
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE dedupe_keys (namespace TEXT,scope TEXT,key TEXT,claimed_at TEXT,"
                   "reusable_after TEXT,payload_json TEXT,PRIMARY KEY(namespace,scope,key))")
        for index in range(3):
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES ('unrelated','other/repo',?,'now','{}')", (str(index),))
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                   "VALUES ('review-round-outcome','o/r','round-1','now','{}')")
        db.commit()
        db.execute("BEGIN")
        monkeypatch.setattr(review_activation, "MAX_OUTCOME_RECEIPT_SCAN_ROWS", 3)
        blocked = read_legacy_review_outcome_receipt_page(db, "o/r")
        monkeypatch.setattr(review_activation, "MAX_OUTCOME_RECEIPT_SCAN_ROWS", 4)
        page = read_legacy_review_outcome_receipt_page(db, "o/r")
        db.rollback()

    assert isinstance(blocked, BlockedLegacyReviewOutcomeReceiptPage)
    assert "total scan bound" in blocked.reason
    assert isinstance(page, LegacyReviewOutcomeReceiptPage)
    assert len(page.entries) == 1
    assert isinstance(page.entries[0], LegacyReviewOutcomeReceiptSource)


def test_complete_outcome_receipts_revalidate_real_settlement_without_writes(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        observed = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewOutcomeReceipts)
        revalidated = revalidate_complete_legacy_review_outcome_receipts(db, observed)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(revalidated, CompleteLegacyReviewOutcomeReceipts)
    assert revalidated.source_digest == observed.source_digest
    assert [(dict(source.fields)["namespace"], dict(source.fields)["key"])
            for source in revalidated.receipts] == [("review-round-outcome", plan.round.round_id)]


def test_complete_outcome_receipts_reject_partial_and_changed_sources(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewOutcomeReceipts)
        partial = CompleteLegacyReviewOutcomeReceipts("o/r", ())
        assert isinstance(revalidate_complete_legacy_review_outcome_receipts(db, partial),
                          BlockedLegacyReviewOutcomeReceipts)
        db.rollback()
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-outcome'",
                   ('{"changed":true}',))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = revalidate_complete_legacy_review_outcome_receipts(db, observed)
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewOutcomeReceipts)
    assert "changed" in result.reason


def test_complete_outcome_receipts_blocks_unreadable_source(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=NULL WHERE namespace='review-round-outcome'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = read_complete_legacy_review_outcome_receipts(db, "o/r")
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewOutcomeReceipts)
    assert result.source_rowid is not None


def test_complete_outcome_receipts_bounds_shared_table_and_retained_bytes(monkeypatch):
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE dedupe_keys (namespace TEXT,scope TEXT,key TEXT,claimed_at TEXT,"
                   "reusable_after TEXT,payload_json TEXT,PRIMARY KEY(namespace,scope,key))")
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                   "VALUES ('unrelated','other/repo','one','now','{}')")
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                   "VALUES ('review-round-outcome','o/r','round-1','now','{}')")
        db.commit()
        db.execute("BEGIN")
        monkeypatch.setattr(review_activation, "MAX_OUTCOME_RECEIPT_SCAN_ROWS", 1)
        scan_blocked = read_complete_legacy_review_outcome_receipts(db, "o/r")
        monkeypatch.setattr(review_activation, "MAX_OUTCOME_RECEIPT_SCAN_ROWS", 2)
        monkeypatch.setattr(review_activation, "MAX_LEGACY_OUTCOME_RECEIPT_INVENTORY_BYTES", 1)
        bytes_blocked = read_complete_legacy_review_outcome_receipts(db, "o/r")
        db.rollback()

    assert isinstance(scan_blocked, BlockedLegacyReviewOutcomeReceipts)
    assert "total scan bound" in scan_blocked.reason
    assert isinstance(bytes_blocked, BlockedLegacyReviewOutcomeReceipts)
    assert "total bound" in bytes_blocked.reason


def _control(tmp_path):
    control = SQLiteControlStore(str(tmp_path / "control.sqlite"))
    control.initialize()
    return control


def _run(db, number, *, owner=OWNER, dispatch=None, state="abandoned", outcome="died"):
    db.execute(
        "INSERT INTO runs(run_id,dispatch_id,kind,owner_id,state,config_version,started_at,completed_at,outcome) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"run-{number}", dispatch if dispatch is not None else f'{{"breaker_key":{-number}}}',
         "REVIEW", owner, state, "cfg", "start", "finish", outcome),
    )


def test_run_scan_retains_abandoned_completed_and_unattributed_dispatches(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _run(db, 7)
        _run(db, 8, state="completed", outcome="returned")
        _run(db, 9, dispatch="opaque historical id")
        _run(db, 10, owner="another:review:owner")
        db.commit()
        db.execute("BEGIN")
        before = db.total_changes
        page = read_legacy_review_run_page(db, OWNER)
        assert db.total_changes == before
        db.rollback()

    assert not page.has_more
    assert [entry.run_id for entry in page.entries if isinstance(entry, LegacyExecutionRunRecord)] == [
        "run-7", "run-8", "run-9",
    ]
    assert page.entries[0].state is LegacyReviewRunState.ABANDONED
    assert page.entries[1].state is LegacyReviewRunState.COMPLETED
    assert page.entries[2].dispatch_id == "opaque historical id"
    assert page.entries[0].source_digest != page.entries[1].source_digest


def test_run_scan_pages_without_skipping_or_repeating_rows(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        db.executemany(
            "INSERT INTO runs(run_id,dispatch_id,kind,owner_id,state,config_version) VALUES (?,?,?,?,?,?)",
            ((f"run-{number}", str(number), "REVIEW", OWNER, "abandoned", "cfg")
             for number in range(MAX_RUN_SOURCE_PAGE + 3)),
        )
        db.commit()
        db.execute("BEGIN")
        first = read_legacy_review_run_page(db, OWNER)
        second = read_legacy_review_run_page(db, OWNER, after_rowid=first.next_rowid)
        db.rollback()

    assert len(first.entries) == MAX_RUN_SOURCE_PAGE and first.has_more
    assert len(second.entries) == 3 and not second.has_more
    assert len({entry.run_id for entry in (*first.entries, *second.entries) if isinstance(entry, LegacyExecutionRunRecord)}) == MAX_RUN_SOURCE_PAGE + 3
    assert first.next_rowid < second.entries[0].source_rowid


def test_run_scan_reports_oversized_and_malformed_rows_without_loading_them_as_facts(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _run(db, 7, outcome="x" * 65537)
        _run(db, 8, dispatch="")
        _run(db, 9, dispatch="invalid JSON remains raw")
        _run(db, 10)
        db.execute("UPDATE runs SET dispatch_id=CAST(x'ff' AS TEXT) WHERE run_id='run-10'")
        db.commit()
        db.execute("BEGIN")
        page = read_legacy_review_run_page(db, OWNER)
        db.rollback()

    assert isinstance(page.entries[0], UnreadableLegacyExecutionRun)
    assert "exceeds" in page.entries[0].reason
    assert isinstance(page.entries[1], UnreadableLegacyExecutionRun)
    assert isinstance(page.entries[2], LegacyExecutionRunRecord)
    assert page.entries[2].dispatch_id == "invalid JSON remains raw"
    assert isinstance(page.entries[3], UnreadableLegacyExecutionRun)


def test_run_scan_requires_explicit_snapshot_and_valid_cursor(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            read_legacy_review_run_page(db, OWNER)
        db.execute("BEGIN")
        with pytest.raises(ValueError, match="cursor"):
            read_legacy_review_run_page(db, OWNER, after_rowid=2**63)
        db.rollback()


def test_run_scan_includes_explicit_negative_and_zero_sqlite_rowids(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        for rowid in (-7, 0, 3):
            db.execute(
                "INSERT INTO runs(rowid,run_id,dispatch_id,kind,owner_id,state,config_version) "
                "VALUES (?,?,?,?,?,?,?)",
                (rowid, f"run-{rowid}", str(rowid), "REVIEW", OWNER, "abandoned", "cfg"),
            )
        db.commit()
        db.execute("BEGIN")
        first = read_legacy_review_run_page(db, OWNER)
        after_negative = read_legacy_review_run_page(db, OWNER, after_rowid=-7)
        db.rollback()

    assert [entry.source_rowid for entry in first.entries] == [-7, 0, 3]
    assert [entry.source_rowid for entry in after_negative.entries] == [0, 3]


def _submission(db, number, *, repository="o/r", kind="submit_review", receipt=True, rowid=None):
    db.execute(
        "INSERT INTO lifecycle_commands(rowid,command_id,idempotency_key,logical_key,kind,target,config_version,"
        "repository,executor_identity,payload_json,payload_digest,state,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rowid, f"cmd-{number}", f"idem-{number}", f"review:{number}", kind, f"pr:{number}", "cfg",
         repository, OWNER, "{}", "a" * 64, "succeeded", "created", "updated"),
    )
    if receipt:
        db.execute(
            "INSERT INTO effect_receipts(command_id,executor_identity,started_at,completed_at,status,"
            "remote_object_id,receipt_digest) VALUES (?,?,?,?,?,?,?)",
            (f"cmd-{number}", OWNER, "started", "completed", "succeeded", f"review-{number}", "b" * 64),
        )


def test_submission_scan_keeps_command_and_effect_receipt_or_explicit_absence(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission(db, 7)
        _submission(db, 8, receipt=False)
        _submission(db, 9, repository="other/r")
        _submission(db, 10, kind="relabel")
        db.execute("BEGIN")
        before = db.total_changes
        page = read_legacy_review_submission_page(db, "o/r")
        assert db.total_changes == before
        db.rollback()

    assert not page.has_more
    assert [dict(entry.command_fields)["command_id"] for entry in page.entries] == ["cmd-7", "cmd-8"]
    assert isinstance(page.entries[0], LegacyReviewSubmissionSource)
    assert dict(page.entries[0].receipt_fields)["remote_object_id"] == "review-7"
    assert page.entries[1].receipt_fields is None


def test_submission_scan_pages_negative_and_zero_rowids(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission(db, 1, rowid=-3)
        _submission(db, 2, rowid=0)
        for number in range(3, MAX_SUBMISSION_SOURCE_PAGE + 4):
            _submission(db, number)
        db.execute("BEGIN")
        first = read_legacy_review_submission_page(db, "o/r")
        second = read_legacy_review_submission_page(db, "o/r", after_rowid=first.next_rowid)
        db.rollback()

    assert first.entries[0].source_rowid == -3 and first.entries[1].source_rowid == 0
    assert len(first.entries) == MAX_SUBMISSION_SOURCE_PAGE and first.has_more
    assert len(second.entries) == 3 and not second.has_more


def test_submission_scan_marks_oversized_or_invalid_utf8_as_unreadable(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission(db, 1)
        _submission(db, 2)
        _submission(db, 3)
        db.execute("UPDATE lifecycle_commands SET payload_json=? WHERE command_id='cmd-1'", ("x" * 65537,))
        db.execute("UPDATE effect_receipts SET error=CAST(x'ff' AS TEXT) WHERE command_id='cmd-2'")
        db.execute("UPDATE lifecycle_commands SET payload_json='malformed JSON stays raw' WHERE command_id='cmd-3'")
        db.execute("BEGIN")
        page = read_legacy_review_submission_page(db, "o/r")
        db.rollback()

    assert isinstance(page.entries[0], UnreadableLegacyReviewSubmission)
    assert isinstance(page.entries[1], UnreadableLegacyReviewSubmission)
    assert isinstance(page.entries[2], LegacyReviewSubmissionSource)
    assert dict(page.entries[2].command_fields)["payload_json"] == "malformed JSON stays raw"


def test_submission_scan_requires_explicit_snapshot_and_canonical_scope(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            read_legacy_review_submission_page(db, "o/r")
        db.execute("BEGIN")
        with pytest.raises(ValueError, match="repository"):
            read_legacy_review_submission_page(db, "Other/R")
        db.rollback()


def _submission_receipt(db, number, *, namespace="review-round-submission-index", scope="o/r", rowid=None):
    db.execute(
        "INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?,?)",
        (rowid, namespace, scope, f"key-{number}", "claimed", f"payload-{number}"),
    )


def test_submission_receipt_scan_keeps_all_three_sources_and_raw_payloads(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        for number, namespace in enumerate(("review-round-submission", "review-round-submission-index",
                                            "review-recorded-submission"), 1):
            _submission_receipt(db, number, namespace=namespace)
        _submission_receipt(db, 4, namespace="other")
        _submission_receipt(db, 5, scope="other/r")
        db.execute("BEGIN")
        before = db.total_changes
        page = read_legacy_review_submission_receipt_page(db, "o/r")
        assert db.total_changes == before
        db.rollback()

    assert not page.has_more
    assert len(page.entries) == 3
    assert all(isinstance(entry, LegacyReviewSubmissionReceiptSource) for entry in page.entries)
    assert [dict(entry.fields)["payload_json"] for entry in page.entries] == [
        "payload-1", "payload-2", "payload-3",
    ]


def test_submission_receipt_scan_pages_negative_and_zero_rowids(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission_receipt(db, 1, rowid=-3)
        _submission_receipt(db, 2, rowid=0)
        for number in range(3, MAX_SUBMISSION_RECEIPT_SOURCE_PAGE + 4):
            _submission_receipt(db, number)
        db.execute("BEGIN")
        first = read_legacy_review_submission_receipt_page(db, "o/r")
        second = read_legacy_review_submission_receipt_page(db, "o/r", after_rowid=first.next_rowid)
        db.rollback()

    assert [entry.source_rowid for entry in first.entries[:2]] == [-3, 0]
    assert len(first.entries) == MAX_SUBMISSION_RECEIPT_SOURCE_PAGE and first.has_more
    assert len(second.entries) == 3 and not second.has_more


def test_submission_receipt_scan_marks_bad_source_and_requires_snapshot(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission_receipt(db, 1)
        _submission_receipt(db, 2)
        _submission_receipt(db, 3)
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE key='key-1'", ("x" * 65537,))
        db.execute("UPDATE dedupe_keys SET claimed_at=CAST(x'ff' AS TEXT) WHERE key='key-2'")
        db.execute("UPDATE dedupe_keys SET payload_json=NULL WHERE key='key-3'")
        with pytest.raises(ValueError, match="transaction"):
            read_legacy_review_submission_receipt_page(db, "o/r")
        db.execute("BEGIN")
        with pytest.raises(ValueError, match="repository"):
            read_legacy_review_submission_receipt_page(db, "Other/R")
        page = read_legacy_review_submission_receipt_page(db, "o/r")
        db.rollback()

    assert isinstance(page.entries[0], UnreadableLegacyReviewSubmissionReceipt)
    assert isinstance(page.entries[1], UnreadableLegacyReviewSubmissionReceipt)
    assert isinstance(page.entries[2], LegacyReviewSubmissionReceiptSource)
    assert dict(page.entries[2].fields)["payload_json"] is None


def _retired_review_owner(db, owner, successor, *, repository="o/r", role="review", rowid=None):
    db.execute(
        "INSERT INTO retired_owners(rowid,owner_id,successor_owner_id,repository,role,retired_at) "
        "VALUES (?,?,?,?,?,?)",
        (rowid, owner, successor, repository, role, "retired"),
    )


def test_owner_lineage_includes_configured_and_transitive_retired_owners_without_writes(tmp_path):
    control = _control(tmp_path)
    current = "o/r:review:new"
    configured = "old-alias:review:configured"
    with closing(control._connect()) as db:
        _run(db, 97, owner="o/r:review")
        _retired_review_owner(db, "o/r:review:old", current)
        _retired_review_owner(db, "o/r:review:oldest", "o/r:review:old")
        _retired_review_owner(db, "old-alias:review:configured-child", configured)
        _retired_review_owner(db, "o/r:fix:foreign", "o/r:fix:current", role="fix")
        db.execute("BEGIN")
        before = db.total_changes
        source = read_legacy_review_owner_lineage(db, "o/r", current,
                                                   configured_prior_owners=(configured,))
        implicit_runs = read_legacy_review_run_page(db, "o/r:review")
        assert db.total_changes == before
        db.rollback()

    assert isinstance(source, LegacyReviewOwnerLineage)
    assert set(source.owners) == {current, "o/r:review", configured, "o/r:review:old",
                                  "o/r:review:oldest", "old-alias:review:configured-child"}
    assert len(source.links) == 3
    assert [entry.run_id for entry in implicit_runs.entries] == ["run-97"]


def test_owner_lineage_includes_implicit_legacy_owner_without_config(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        db.execute("BEGIN")
        source = read_legacy_review_owner_lineage(db, "o/r", "o/r:review:new")
        db.rollback()
    assert isinstance(source, LegacyReviewOwnerLineage)
    assert source.owners == ("o/r:review:new", "o/r:review")


def test_complete_owner_run_scan_covers_all_pages_and_implicit_legacy_owner_without_writes(tmp_path):
    control = _control(tmp_path)
    current = "o/r:review:new"
    with closing(control._connect()) as db:
        for number in range(MAX_RUN_SOURCE_PAGE + 3):
            _run(db, number, owner="o/r:review")
        _run(db, 999, owner=current)
        _run(db, 1000, owner="other/r:review")
        db.commit()
        db.execute("BEGIN")
        lineage = read_legacy_review_owner_lineage(db, "o/r", current)
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        before = db.total_changes
        result = read_complete_legacy_review_runs(db, lineage)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(result, CompleteLegacyReviewRuns)
    assert len(result.runs) == MAX_RUN_SOURCE_PAGE + 4
    assert {run.run_id for run in result.runs} == {f"run-{number}" for number in (*range(MAX_RUN_SOURCE_PAGE + 3), 999)}
    assert len(result.source_digest) == 64


@pytest.mark.parametrize("damage", ["blob_owner", "oversized", "changed_lineage", "too_many", "scan_bound"])
def test_complete_owner_run_scan_blocks_unreadable_incomplete_or_changed_sources(tmp_path, monkeypatch, damage):
    control = _control(tmp_path)
    current = "o/r:review:new"
    with closing(control._connect()) as db:
        _run(db, 1, owner="o/r:review")
        db.commit()
        db.execute("BEGIN")
        lineage = read_legacy_review_owner_lineage(db, "o/r", current)
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        db.rollback()
        if damage == "blob_owner":
            db.execute("UPDATE runs SET owner_id=CAST(owner_id AS BLOB) WHERE run_id='run-1'")
        elif damage == "oversized":
            db.execute("UPDATE runs SET outcome=? WHERE run_id='run-1'", ("x" * 65537,))
        elif damage == "changed_lineage":
            _retired_review_owner(db, "o/r:review:old", current)
        else:
            _run(db, 2, owner=current if damage == "too_many" else "other/r:review")
            if damage == "too_many":
                monkeypatch.setattr("issue_agent.adapters.review_execution_activation.MAX_LEGACY_RUN_INVENTORY_ROWS", 1)
            else:
                monkeypatch.setattr("issue_agent.adapters.review_execution_activation.MAX_LEGACY_RUN_SCAN_ROWS", 1)
        db.commit()
        db.execute("BEGIN")
        before = db.total_changes
        result = read_complete_legacy_review_runs(db, lineage)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewRuns)
    if damage == "changed_lineage":
        assert "lineage" in result.reason
    elif damage == "scan_bound":
        assert "scan bound" in result.reason
    else:
        assert result.owner_id in lineage.owners
        assert result.source_rowid is not None


def test_run_inventory_revalidation_rejects_partial_or_changed_caller_evidence(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _run(db, 1, owner=OWNER)
        db.commit()
        db.execute("BEGIN")
        lineage = read_legacy_review_owner_lineage(db, "o/r", OWNER)
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        observed = read_complete_legacy_review_runs(db, lineage)
        assert isinstance(observed, CompleteLegacyReviewRuns)
        partial = CompleteLegacyReviewRuns(lineage, ())
        assert isinstance(revalidate_complete_legacy_review_runs(db, partial), BlockedLegacyReviewRuns)
        assert revalidate_complete_legacy_review_runs(db, observed) == observed
        db.rollback()
        db.execute("UPDATE runs SET outcome='changed' WHERE run_id='run-1'")
        db.commit()
        db.execute("BEGIN")
        assert isinstance(revalidate_complete_legacy_review_runs(db, observed), BlockedLegacyReviewRuns)
        db.rollback()


@pytest.mark.parametrize("anomaly", [
    "retired_current", "blob_current_retirement", "foreign_role", "foreign_repository", "cycle",
    "oversized", "invalid_utf8", "configured_elsewhere", "blob_successor", "too_many",
])
def test_owner_lineage_blocks_contradictory_or_unreadable_sources(tmp_path, anomaly):
    control = _control(tmp_path)
    current = "o/r:review:new"
    with closing(control._connect()) as db:
        if anomaly == "retired_current":
            _retired_review_owner(db, current, "o/r:review:later")
        elif anomaly == "blob_current_retirement":
            _retired_review_owner(db, current, "o/r:review:later")
            db.execute("UPDATE retired_owners SET owner_id=CAST(owner_id AS BLOB) WHERE owner_id=?", (current,))
        elif anomaly == "foreign_role":
            _retired_review_owner(db, "o/r:fix:old", current, role="fix")
        elif anomaly == "foreign_repository":
            _retired_review_owner(db, "o/r:review:old", current, repository="other/r")
        elif anomaly == "cycle":
            _retired_review_owner(db, "o/r:review:a", "o/r:review:b")
            _retired_review_owner(db, "o/r:review:b", "o/r:review:a")
        elif anomaly == "configured_elsewhere":
            _retired_review_owner(db, "o/r:review:a", "o/r:review:foreign")
        elif anomaly == "blob_successor":
            _retired_review_owner(db, "o/r:review:old", current)
            db.execute("UPDATE retired_owners SET successor_owner_id=CAST(successor_owner_id AS BLOB) "
                       "WHERE owner_id='o/r:review:old'")
        elif anomaly == "too_many":
            for number in range(65):
                _retired_review_owner(db, f"o/r:review:old-{number}", current)
        elif anomaly == "oversized":
            _retired_review_owner(db, "o/r:review:old", current)
            db.execute("UPDATE retired_owners SET retired_at=? WHERE owner_id='o/r:review:old'",
                       ("x" * 65537,))
        else:
            _retired_review_owner(db, "o/r:review:old", current)
            db.execute("UPDATE retired_owners SET retired_at=CAST(x'ff' AS TEXT) "
                       "WHERE owner_id='o/r:review:old'")
        db.execute("BEGIN")
        source = read_legacy_review_owner_lineage(
            db, "o/r", current,
            configured_prior_owners=("o/r:review:a",) if anomaly in {"cycle", "configured_elsewhere"} else (),
        )
        db.rollback()
    assert isinstance(source, UnreadableLegacyReviewOwnerLineage)


def test_owner_lineage_requires_snapshot_and_canonical_repository(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        with pytest.raises(ValueError, match="transaction"):
            read_legacy_review_owner_lineage(db, "o/r", OWNER)
        db.execute("BEGIN")
        with pytest.raises(ValueError, match="canonical"):
            read_legacy_review_owner_lineage(db, "Other/R", OWNER)
        with pytest.raises(ValueError, match="repository and role"):
            read_legacy_review_owner_lineage(db, "o/r", "o/r:fix:owner")
        db.rollback()
