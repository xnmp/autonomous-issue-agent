"""Review execution legacy file and owner-run inventories are bounded and read-only."""

import sqlite3
import json
from contextlib import closing
from dataclasses import replace

import pytest

import issue_agent.adapters.review_execution_activation as review_activation
from issue_agent.adapters.control_store import SQLiteControlStore, _encoded_payload_digest
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from issue_agent.adapters.review_execution_activation import (
    MAX_RUN_SOURCE_PAGE, MAX_SUBMISSION_SOURCE_PAGE, read_legacy_execution_roots, read_legacy_execution_state,
    read_complete_legacy_execution_root_files, revalidate_complete_legacy_execution_file_values,
    CompleteLegacyExecutionRootFiles,
    read_complete_legacy_review_runs, read_legacy_review_run_page, read_legacy_review_submission_page,
    read_complete_legacy_review_submissions, revalidate_complete_legacy_review_submissions,
    revalidate_complete_legacy_review_runs,
    read_legacy_review_submission_receipt_page, MAX_SUBMISSION_RECEIPT_SOURCE_PAGE,
    read_complete_legacy_review_submission_receipts, revalidate_complete_legacy_review_submission_receipts,
    read_legacy_review_owner_lineage,
    read_legacy_review_round_page, MAX_REVIEW_ROUND_SOURCE_PAGE,
    read_complete_legacy_review_rounds, revalidate_complete_legacy_review_rounds,
    read_legacy_review_outcome_receipt_page, MAX_OUTCOME_RECEIPT_SOURCE_PAGE,
    read_complete_legacy_review_outcome_receipts, revalidate_complete_legacy_review_outcome_receipts,
    verify_legacy_round_outcome_storage,
    verify_complete_legacy_round_outcome_storage,
    verify_complete_legacy_submission_settlement_storage,
    VerifiedLegacySubmissionRegistrationStorage, CompleteLegacySubmissionSettlementStorage,
    verify_legacy_stale_productivity_receipt_path,
    VerifiedLegacyStaleProductivityReceiptPath, UnverifiedLegacyStaleProductivityReceiptPath,
    read_complete_legacy_review_generation_receipts, revalidate_complete_legacy_review_generation_receipts,
    match_legacy_review_generation_boundary_sources,
    MatchedLegacyReviewGenerationBoundarySources, UnmatchedLegacyReviewGenerationBoundarySources,
    read_review_generation_archive_page, verify_checkpoint_generation_anchor,
    read_checkpoint_round_row_source, read_checkpoint_round_receipt_source,
    read_checkpoint_round_receipt_members,
    ObservedReviewGenerationArchivePage, BlockedReviewGenerationArchivePage,
    read_review_generation_receipt_sweep_page, read_reconciled_review_generation_receipt_sweep_page,
    ObservedReviewGenerationReceiptSweepPage, BlockedReviewGenerationReceiptSweepPage,
    match_legacy_stale_productivity_generation_sources,
    MatchedLegacyStaleProductivityGenerationSources, UnmatchedLegacyStaleProductivityGenerationSources,
    classify_complete_legacy_review_successes,
    CompleteLegacyReviewSuccessClassifications, BlockedLegacyReviewSuccessClassifications,
    ClassifiedLegacyPlainReviewSuccess, ClassifiedLegacyEscalatedReviewSuccess,
    ClassifiedLegacyStaleReviewSuccess, ClassifiedLegacyCeilingReviewSuccess,
    UnresolvedLegacyReviewSuccess,
    match_legacy_escalated_review_hold_plan,
    MatchedLegacyEscalatedReviewHoldPlan, UnmatchedLegacyEscalatedReviewHoldPlan,
    verify_complete_legacy_ordered_outcome_coverage,
    CompleteLegacyOrderedOutcomeCoverage, BlockedLegacyOrderedOutcomeCoverage,
    verify_complete_legacy_allocation_generation_sources,
    CompleteLegacyAllocationGenerationSources, BlockedLegacyAllocationGenerationSources,
    observe_legacy_ordered_checkpoint_replay,
    ObservedLegacyOrderedCheckpointReplay, BlockedLegacyOrderedCheckpointReplay,
    read_legacy_recorded_review, read_review_submission_round_link,
    verify_legacy_plain_productive_round,
)
from issue_agent.adapters.review_execution_compaction import observe_review_checkpoint_manifest
from issue_agent.adapters.review_round_receipt_sweep import (
    ObservedReviewRoundReceiptSweepPage, read_review_round_receipt_sweep_page,
)
from issue_agent.domain.resolve_activation import (
    MAX_LEGACY_FILE_BYTES, LegacyAbsent, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry,
)
from issue_agent.domain.review_execution import (
    ActivatedReviewExecution, CopiedReviewProgress, PaidReviewProgress, ReviewUnlaunchedRound, ReviewRoundOutcome,
    ReviewExecutionAcceptedTransitionSource, ReviewExecutionTransitionProofSource,
    ReviewExecutionBoundaryCursor, ReviewGenerationArchiveFingerprint,
    ReviewExecutionCompactionRoundSource, build_review_execution_checkpoint_receipt,
    plan_review_execution_compaction,
    accepted_review_transition_source,
    review_generation_chain_digest,
)
from issue_agent.domain.review_rounds import ReviewRoundFacts, ReviewRoundPlan, StaleReviewProductivitySettlement
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, encode_workflow_episodes, load_review_workflow_row,
    review_execution_checkpoint_receipt_id,
)
from issue_agent.domain.review_execution_activation import (
    LegacyExecutionCounterRecord, LegacyExecutionNumber, LegacyExecutionPRRecord,
    LegacyExecutionRunRecord, LegacyReviewRunState, LegacyReviewSessionCandidate,
    LegacyReviewSessionModeMarker,
    parse_legacy_review_counter, parse_legacy_review_round, parse_legacy_review_session,
    classify_legacy_execution_file_values, CompleteLegacyExecutionFileValues,
    BlockedLegacyExecutionFileValues,
    UnreadableLegacyExecutionRun, LegacyReviewSubmissionSource, UnreadableLegacyReviewSubmission,
    CompleteLegacyReviewSubmissions, BlockedLegacyReviewSubmissions,
    LegacyReviewSubmissionReceiptSource, UnreadableLegacyReviewSubmissionReceipt,
    CompleteLegacyReviewSubmissionReceipts, BlockedLegacyReviewSubmissionReceipts,
    LegacyReviewOwnerLineage, UnreadableLegacyReviewOwnerLineage,
    CompleteLegacyReviewRuns, BlockedLegacyReviewRuns,
    LegacyReviewRoundSource, UnreadableLegacyReviewRound, MAX_LEGACY_ROUND_JSON_BYTES,
    CompleteLegacyReviewRounds, BlockedLegacyReviewRounds,
    LEGACY_REVIEW_OUTCOME_RECEIPT_NAMESPACES, MAX_LEGACY_OUTCOME_RECEIPT_JSON_BYTES,
    LegacyReviewOutcomeReceiptPage, LegacyReviewOutcomeReceiptSource, UnreadableLegacyReviewOutcomeReceipt,
    BlockedLegacyReviewOutcomeReceiptPage,
    CompleteLegacyReviewOutcomeReceipts, BlockedLegacyReviewOutcomeReceipts,
    VerifiedLegacyRoundOutcomeStorage, UnverifiedLegacyRoundOutcomeStorage,
    CompleteLegacyRoundOutcomeStorage, BlockedLegacyRoundOutcomeStorage,
    LegacyReviewGenerationReceiptSource, CompleteLegacyReviewGenerationReceipts,
    BlockedLegacyReviewGenerationReceipts, MAX_LEGACY_GENERATION_RECEIPT_JSON_BYTES,
    BlockedLegacySubmissionSettlementStorage,
    VerifiedLegacyRecordedReview, VerifiedReviewSubmissionRoundLink,
    VerifiedLegacyPlainProductiveRound, UnverifiedLegacyPlainProductiveRound,
)
from tests.adapters.test_review_execution_control import rig, current, OWNER as CONTROL_OWNER
from tests.adapters.test_review_productive_settlement import (
    accepted_generation_with_open_submission,
    confirmed_copy, confirmed_escalation, workflow_for, _forge_generation_with_open_round,
)
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.domain.workflow import (
    RequestRearm, ContinueGrantedRearm, CancelRearm, AuthorizedOperatorCommand,
    ObservedTransition, OperatorActor, KeepGuidance, WorkflowCommitted, WorkflowRejected,
)
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.labels import Label
from tests.adapters.test_review_preparation_finalization import stopped_round
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_submission import command_for
from issue_agent.domain.review_rounds import ReviewSubmissionKind


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
    session = parse_legacy_review_session(prs[7].session)
    assert isinstance(session, LegacyReviewSessionCandidate)
    assert (session.runtime.value, session.handle, session.file_digest) == (
        "codex", "session-1", prs[7].session.digest,
    )
    assert parse_legacy_review_round(prs[7]) == LegacyExecutionNumber(4, prs[7].round.digest)
    assert parse_legacy_review_counter(counters[-7]) == LegacyExecutionNumber(3, counters[-7].infra_deaths.digest)
    complete = classify_legacy_execution_file_values(entries)
    assert isinstance(complete, CompleteLegacyExecutionFileValues)
    assert [item.source.pr for item in complete.prs] == [7, 8]
    assert [item.source.key for item in complete.counters] == [-7, 99]


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


def test_legacy_mode_marker_file_is_inventoried_as_nonreusable_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pr-7.session").write_text("codex:multi")

    complete = classify_legacy_execution_file_values(read_legacy_execution_state(str(state)))

    assert isinstance(complete, CompleteLegacyExecutionFileValues)
    assert isinstance(complete.prs[0].session, LegacyReviewSessionModeMarker)
    assert complete.prs[0].session.marker == "multi"


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


def test_file_inventory_refuses_directory_and_retained_byte_excess(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    for number in (7, 8, 9):
        (state / f"pr-{number}.round").write_text("1")
    monkeypatch.setattr(review_activation, "MAX_LEGACY_EXECUTION_FILE_ENTRIES", 2)
    (blocked,) = read_legacy_execution_state(str(state))
    assert isinstance(blocked, UnmappedLegacyEntry) and "entry bound" in blocked.reason

    monkeypatch.setattr(review_activation, "MAX_LEGACY_EXECUTION_FILE_ENTRIES", 3)
    monkeypatch.setattr(review_activation, "MAX_LEGACY_EXECUTION_FILE_BYTES", 2)
    (blocked,) = read_legacy_execution_state(str(state))
    assert isinstance(blocked, UnmappedLegacyEntry) and "byte bound" in blocked.reason


def test_listed_file_disappearance_is_unreadable_not_absent(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pr-7.session").write_text("codex:handle")
    (state / "issue--7.infra-deaths").write_text("1")
    monkeypatch.setattr(review_activation, "_read_legacy_file", lambda _path: LegacyAbsent())

    entries = read_legacy_execution_state(str(state))

    pr = next(entry for entry in entries if isinstance(entry, LegacyExecutionPRRecord))
    counter = next(entry for entry in entries if isinstance(entry, LegacyExecutionCounterRecord))
    assert isinstance(pr.session, LegacyUnreadable) and "disappeared" in pr.session.reason
    assert isinstance(counter.infra_deaths, LegacyUnreadable) and "disappeared" in counter.infra_deaths.reason
    assert isinstance(classify_legacy_execution_file_values(entries), BlockedLegacyExecutionFileValues)


def test_complete_legacy_file_roots_revalidate_without_writes(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "pr-7.session").write_text("codex:handle")
    (second / "issue--7.infra-deaths").write_text("2")
    roots = (("first", str(first)), ("second", str(second)))
    before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in (
        first / "pr-7.session", second / "issue--7.infra-deaths",
    )}

    observed = read_complete_legacy_execution_root_files(roots)
    assert isinstance(observed, CompleteLegacyExecutionRootFiles)
    current = revalidate_complete_legacy_execution_file_values(roots, observed)

    assert current == observed
    assert current.source_digest == observed.source_digest
    assert {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in before} == before


@pytest.mark.parametrize("mutation", ("change", "add", "remove", "malformed", "duplicate"))
def test_complete_legacy_file_roots_block_changed_or_partial_sources(tmp_path, mutation):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    session = first / "pr-7.session"
    session.write_text("codex:handle")
    roots = (("first", str(first)), ("second", str(second)))
    observed = read_complete_legacy_execution_root_files(roots)
    assert isinstance(observed, CompleteLegacyExecutionRootFiles)

    if mutation == "change":
        session.write_text("codex:other")
    elif mutation == "add":
        (second / "issue--7.infra-deaths").write_text("1")
    elif mutation == "remove":
        session.unlink()
    elif mutation == "malformed":
        session.write_text("unknown:handle")
    else:
        (second / "pr-7.round").write_text("2")

    assert isinstance(revalidate_complete_legacy_execution_file_values(roots, observed),
                      BlockedLegacyExecutionFileValues)


def test_complete_legacy_file_roots_bind_empty_and_alias_configuration(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "pr-7.round").write_text("1")
    alias = tmp_path / "alias"
    alias.symlink_to(first)
    roots = (("first", str(first)), ("alias", str(alias)), ("empty", str(second)))
    observed = read_complete_legacy_execution_root_files(roots)
    assert isinstance(observed, CompleteLegacyExecutionRootFiles)
    assert len(observed.roots) == 3
    assert isinstance(revalidate_complete_legacy_execution_file_values(roots, observed),
                      CompleteLegacyExecutionRootFiles)

    alias.unlink()
    alias.symlink_to(second)
    assert isinstance(revalidate_complete_legacy_execution_file_values(roots, observed),
                      BlockedLegacyExecutionFileValues)
    alias.unlink()
    alias.symlink_to(first)
    assert isinstance(revalidate_complete_legacy_execution_file_values(roots[:-1], observed),
                      BlockedLegacyExecutionFileValues)


def test_complete_legacy_file_roots_block_invented_partial_and_invalid_configuration(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pr-7.round").write_text("1")
    roots = (("review", str(state)),)
    observed = read_complete_legacy_execution_root_files(roots)
    assert isinstance(observed, CompleteLegacyExecutionRootFiles)
    partial = CompleteLegacyExecutionRootFiles(observed.roots, CompleteLegacyExecutionFileValues((), ()))

    assert isinstance(revalidate_complete_legacy_execution_file_values(roots, partial),
                      BlockedLegacyExecutionFileValues)
    assert isinstance(revalidate_complete_legacy_execution_file_values((("review", ""),), observed),
                      BlockedLegacyExecutionFileValues)
    assert isinstance(revalidate_complete_legacy_execution_file_values(roots, observed.values),
                      BlockedLegacyExecutionFileValues)


def test_complete_legacy_file_roots_block_missing_root_replacement(tmp_path):
    missing = tmp_path / "missing"
    roots = (("review", str(missing)),)
    observed = read_complete_legacy_execution_root_files(roots)
    assert isinstance(observed, CompleteLegacyExecutionRootFiles)
    assert observed.roots[0].directory_identity is None

    missing.mkdir()
    assert isinstance(revalidate_complete_legacy_execution_file_values(roots, observed),
                      BlockedLegacyExecutionFileValues)


def test_complete_legacy_file_roots_bind_same_content_to_physical_directory(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for path in (first, second):
        (path / "pr-7.round").write_text("1")
    alias = tmp_path / "alias"
    alias.symlink_to(first)
    roots = (("review", str(alias)),)
    observed = read_complete_legacy_execution_root_files(roots)
    assert isinstance(observed, CompleteLegacyExecutionRootFiles)

    alias.unlink()
    alias.symlink_to(second)
    changed = read_complete_legacy_execution_root_files(roots)
    assert isinstance(changed, CompleteLegacyExecutionRootFiles)
    assert changed.values == observed.values
    assert isinstance(revalidate_complete_legacy_execution_file_values(roots, observed),
                      BlockedLegacyExecutionFileValues)


def test_complete_legacy_file_roots_share_directory_entry_budget(tmp_path, monkeypatch):
    first, second, third = (tmp_path / name for name in ("first", "second", "third"))
    for path in (first, second, third):
        path.mkdir()
    for name in ("unrelated-1", "unrelated-2"):
        (first / name).write_text("other")
        (second / name).write_text("other")
    (third / "pr-7.round").write_text("1")
    scanned: list[str] = []
    original = review_activation.read_legacy_execution_state

    def record_scan(path, root="review", *, _budget=None):
        scanned.append(root)
        return original(path, root, _budget=_budget)

    monkeypatch.setattr(review_activation, "read_legacy_execution_state", record_scan)
    monkeypatch.setattr(review_activation, "MAX_LEGACY_EXECUTION_FILE_ENTRIES", 3)
    result = read_complete_legacy_execution_root_files((
        ("first", str(first)), ("second", str(second)), ("third", str(third)),
    ))

    assert isinstance(result, BlockedLegacyExecutionFileValues)
    assert scanned == ["first", "second"]


def test_complete_legacy_file_roots_share_retained_byte_budget(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "pr-7.round").write_text("12")
    (second / "issue--7.infra-deaths").write_text("12")
    monkeypatch.setattr(review_activation, "MAX_LEGACY_EXECUTION_FILE_BYTES", 3)

    roots = (("first", str(first)), ("second", str(second)))
    entries = read_legacy_execution_roots(roots)
    result = read_complete_legacy_execution_root_files(roots)

    assert any(isinstance(entry, UnmappedLegacyEntry) and "byte bound" in entry.reason for entry in entries)
    assert isinstance(result, BlockedLegacyExecutionFileValues)


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


def test_sealed_round_outcome_joins_exact_durable_receipt_without_writes(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        proof = verify_legacy_round_outcome_storage(db, rounds, receipts, plan.round.round_id)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(proof, VerifiedLegacyRoundOutcomeStorage)
    assert proof.facts.plan.round == plan.round
    assert proof.outcome.round == plan.round
    assert isinstance(proof.outcome.disposition, CopiedReviewProgress)
    assert dict(proof.receipt_source.fields)["key"] == plan.round.round_id


@pytest.mark.parametrize("damage", ["receipt_payload", "round_outcome", "missing_receipt"])
def test_sealed_round_outcome_rejects_changed_or_missing_sources(rig, damage):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        db.rollback()
    assert isinstance(rounds, CompleteLegacyReviewRounds)
    assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
    with store.transaction() as db:
        if damage == "receipt_payload":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='review-round-outcome'")
        elif damage == "round_outcome":
            db.execute("UPDATE review_execution_rounds SET outcome_json='{}' WHERE round_id=?",
                       (plan.round.round_id,))
        else:
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-outcome'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = verify_legacy_round_outcome_storage(db, rounds, receipts, plan.round.round_id)
        db.rollback()

    assert isinstance(result, UnverifiedLegacyRoundOutcomeStorage)


@pytest.mark.parametrize("damage", ["receipt_payload", "round_plan", "round_closure", "expiring_receipt"])
def test_sealed_round_outcome_rejects_matching_inventories_with_contradictory_storage(rig, damage):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        if damage == "receipt_payload":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='review-round-outcome'")
        elif damage == "round_plan":
            db.execute("UPDATE review_execution_rounds SET plan_json='{}' WHERE round_id=?",
                       (plan.round.round_id,))
        elif damage == "round_closure":
            db.execute("UPDATE review_execution_rounds SET closure_json='{}' WHERE round_id=?",
                       (plan.round.round_id,))
        else:
            db.execute("UPDATE dedupe_keys SET reusable_after='2026-09-29T00:00:00Z' "
                       "WHERE namespace='review-round-outcome'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        result = verify_legacy_round_outcome_storage(db, rounds, receipts, plan.round.round_id)
        db.rollback()

    assert isinstance(result, UnverifiedLegacyRoundOutcomeStorage)


def test_open_round_cannot_claim_a_sealed_outcome(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        result = verify_legacy_round_outcome_storage(db, rounds, receipts, plan.round.round_id)
        db.rollback()

    assert isinstance(result, UnverifiedLegacyRoundOutcomeStorage)


def test_sealed_round_outcome_rejects_forged_but_matching_receipt(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        row = db.execute("SELECT closure_json,outcome_json FROM review_execution_rounds WHERE round_id=?",
                         (plan.round.round_id,)).fetchone()
        facts = decode_workflow(row["closure_json"], ReviewRoundFacts)
        outcome = decode_workflow(row["outcome_json"], ReviewRoundOutcome)
        forged = replace(outcome, disposition=ReviewUnlaunchedRound(facts.closure_receipt))
        db.execute("UPDATE review_execution_rounds SET outcome_json=? WHERE round_id=?",
                   (encode_workflow(forged), plan.round.round_id))
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-outcome'",
                   (encode_workflow((facts, forged)),))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        result = verify_legacy_round_outcome_storage(db, rounds, receipts, plan.round.round_id)
        db.rollback()

    assert isinstance(result, UnverifiedLegacyRoundOutcomeStorage)
    assert "closure facts" in result.reason


def test_sealed_round_outcome_returns_unverified_for_control_characters_in_bad_tag(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        db.execute("UPDATE review_execution_rounds SET closure_json=? WHERE round_id=?",
                   (json.dumps({"format": 1, "value": {"type": "unknown\nvariant"}}), plan.round.round_id))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        result = verify_legacy_round_outcome_storage(db, rounds, receipts, plan.round.round_id)
        db.rollback()

    assert isinstance(result, UnverifiedLegacyRoundOutcomeStorage)
    assert "unknown" in result.reason and "\n" not in result.reason


def test_complete_round_outcome_storage_accounts_for_real_sealed_round_without_writes(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        complete = verify_complete_legacy_round_outcome_storage(db, rounds, receipts)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(complete, CompleteLegacyRoundOutcomeStorage)
    assert [entry.outcome.round.round_id for entry in complete.sealed] == [plan.round.round_id]
    assert complete.repository == "o/r"
    with pytest.raises(ValueError, match="unaccounted"):
        CompleteLegacyRoundOutcomeStorage(complete.rounds, complete.receipts, ())
    outcome_receipt = complete.receipts.receipts[0]
    extras = tuple(replace(
        outcome_receipt, source_rowid=index,
        fields=tuple((name, namespace if name == "namespace" else value)
                     for name, value in outcome_receipt.fields),
    ) for index, namespace in ((-1, "review-round-stale-productivity"),
                               (-2, "review-round-superseded-productivity")))
    contradictory = CompleteLegacyReviewOutcomeReceipts("o/r", (outcome_receipt, *extras))
    with pytest.raises(ValueError, match="contradictory auxiliary"):
        CompleteLegacyRoundOutcomeStorage(complete.rounds, contradictory, complete.sealed)


def test_complete_round_outcome_storage_rejects_partial_caller_inventory(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        partial = CompleteLegacyReviewOutcomeReceipts("o/r", ())
        result = verify_complete_legacy_round_outcome_storage(db, rounds, partial)
        db.rollback()

    assert isinstance(result, BlockedLegacyRoundOutcomeStorage)
    assert "changed" in result.reason


def test_complete_round_outcome_storage_accepts_open_round_without_outcome_receipt(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        complete = verify_complete_legacy_round_outcome_storage(db, rounds, receipts)
        db.rollback()

    assert isinstance(complete, CompleteLegacyRoundOutcomeStorage)
    assert complete.sealed == ()
    assert [dict(source.fields)["round_id"] for source in complete.rounds.rounds] == [plan.round.round_id]


def test_complete_round_outcome_storage_accepts_real_deferred_preparation(stopped_round):
    store, _, control, _, child, _, _ = stopped_round
    control.finalize_completed_round(child.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        complete = verify_complete_legacy_round_outcome_storage(db, rounds, receipts)
        db.rollback()

    assert isinstance(complete, CompleteLegacyRoundOutcomeStorage)
    assert len(complete.sealed) == 1
    assert {dict(source.fields)["namespace"] for source in complete.receipts.receipts} == {
        "review-round-outcome", "review-round-preparation",
    }
    outcome_receipt = next(source for source in complete.receipts.receipts
                           if dict(source.fields)["namespace"] == "review-round-outcome")
    stale = replace(outcome_receipt, source_rowid=-1,
                    fields=tuple((name, "review-round-stale-productivity" if name == "namespace" else value)
                                 for name, value in outcome_receipt.fields))
    contradictory = CompleteLegacyReviewOutcomeReceipts("o/r", (*complete.receipts.receipts, stale))
    with pytest.raises(ValueError, match="contradictory auxiliary"):
        CompleteLegacyRoundOutcomeStorage(complete.rounds, contradictory, complete.sealed)


@pytest.mark.parametrize("damage", ["missing_outcome_receipt", "orphan_outcome_receipt", "orphan_productivity_receipt",
                                     "receipt_on_open_round", "partially_sealed_round", "expiring_productivity_receipt",
                                     "forged_preparation", "malformed_stale", "malformed_superseded",
                                     "malformed_claim_time"])
def test_complete_round_outcome_storage_blocks_unaccounted_evidence(rig, damage):
    if damage == "receipt_on_open_round":
        store, _, _, selection, control = rig
        plan = control.preflight(selection)
    else:
        store, gateway, _, control, plan, command = confirmed_copy(rig)
        workflow_for(store, gateway).record_review(command.command_id)
        control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        if damage == "missing_outcome_receipt":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-outcome'")
        elif damage in {"orphan_outcome_receipt", "orphan_productivity_receipt"}:
            namespace = ("review-round-outcome" if damage == "orphan_outcome_receipt"
                         else "review-round-stale-productivity")
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,?,?,?,?)", (namespace, "o/r", "unknown-round", "now", "{}"))
        elif damage == "receipt_on_open_round":
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES ('review-round-preparation','o/r',?,'now','{}')", (plan.round.round_id,))
        elif damage == "partially_sealed_round":
            db.execute("UPDATE review_execution_rounds SET closure_json=NULL WHERE round_id=?",
                       (plan.round.round_id,))
        elif damage == "expiring_productivity_receipt":
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,reusable_after,payload_json) "
                       "VALUES ('review-round-stale-productivity','o/r',?,'now','later','{}')",
                       (plan.round.round_id,))
        elif damage == "malformed_claim_time":
            db.execute("UPDATE dedupe_keys SET claimed_at='now' WHERE namespace='review-round-outcome'")
        else:
            namespace = {"forged_preparation": "review-round-preparation",
                         "malformed_stale": "review-round-stale-productivity",
                         "malformed_superseded": "review-round-superseded-productivity"}[damage]
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,?,?,?,?)", (namespace, "o/r", plan.round.round_id,
                                             "2026-09-28T00:00:00Z", "{}"))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        result = verify_complete_legacy_round_outcome_storage(db, rounds, receipts)
        db.rollback()

    assert isinstance(result, BlockedLegacyRoundOutcomeStorage)


def test_active_plain_recording_can_join_its_exact_productive_round(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        page = read_legacy_review_submission_page(db, "o/r")
        source = next(entry for entry in page.entries if isinstance(entry, LegacyReviewSubmissionSource)
                      and dict(entry.command_fields)["command_id"] == str(command.command_id))
        lineage = read_legacy_review_owner_lineage(db, "o/r", CONTROL_OWNER)
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        baseline = read_legacy_recorded_review(db, source, lineage)
        link = read_review_submission_round_link(db, source, lineage)
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        storage = verify_complete_legacy_round_outcome_storage(db, rounds, receipts)
        assert isinstance(storage, CompleteLegacyRoundOutcomeStorage)
        proof = verify_legacy_plain_productive_round(db, storage, source, lineage)
        db.rollback()

    assert isinstance(baseline, VerifiedLegacyRecordedReview)
    assert isinstance(link, VerifiedReviewSubmissionRoundLink)
    assert link.round == plan.round
    assert isinstance(proof, VerifiedLegacyPlainProductiveRound)
    assert proof.storage.outcome.round == plan.round
    with pytest.raises(ValueError, match="contradictory"):
        replace(proof, effect_receipt_id="effect-receipt:wrong")
    assert proof.baseline.execution_binding == plan.round.binding
    alternate = replace(proof.baseline.execution_binding, execution_episode="different-episode")
    with pytest.raises(ValueError, match="contradictory"):
        replace(proof, baseline=replace(proof.baseline, execution_binding=alternate))
    with pytest.raises(ValueError, match="contradictory"):
        replace(proof, baseline=replace(proof.baseline, execution_binding=None))


def test_paid_plain_recording_can_join_its_confirmed_round(rig):
    store, _, gateway, selection, control = rig
    plan = control.preflight(selection)
    planned = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, planned)
    proposed = request(planned.children[0])
    assert store.reserve(proposed).admitted
    register(store, proposed)
    store.settle_review_return(settlement(proposed), returned(store, proposed))
    command = command_for(current(store, selection), patch_id="0" * 64)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.PAID)
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, CONTROL_OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:paid"))
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        page = read_legacy_review_submission_page(db, "o/r")
        source = next(entry for entry in page.entries if isinstance(entry, LegacyReviewSubmissionSource)
                      and dict(entry.command_fields)["command_id"] == str(command.command_id))
        lineage = read_legacy_review_owner_lineage(db, "o/r", CONTROL_OWNER)
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        storage = verify_complete_legacy_round_outcome_storage(db, rounds, receipts)
        assert isinstance(storage, CompleteLegacyRoundOutcomeStorage)
        proof = verify_legacy_plain_productive_round(db, storage, source, lineage)
        db.rollback()

    assert isinstance(proof, VerifiedLegacyPlainProductiveRound)
    assert isinstance(proof.storage.outcome.disposition, PaidReviewProgress)
    assert proof.storage.outcome.round == plan.round


def _complete_settlement_observations(db):
    commands = read_complete_legacy_review_submissions(db, "o/r")
    receipts = read_complete_legacy_review_submission_receipts(db, "o/r")
    rounds = read_complete_legacy_review_rounds(db, "o/r")
    outcome_receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
    assert isinstance(commands, CompleteLegacyReviewSubmissions)
    assert isinstance(receipts, CompleteLegacyReviewSubmissionReceipts)
    assert isinstance(rounds, CompleteLegacyReviewRounds)
    assert isinstance(outcome_receipts, CompleteLegacyReviewOutcomeReceipts)
    storage = verify_complete_legacy_round_outcome_storage(db, rounds, outcome_receipts)
    assert isinstance(storage, CompleteLegacyRoundOutcomeStorage)
    return commands, receipts, storage


def _accepted_generation(rig):
    store, gateway, selection, _, _, command = confirmed_escalation(rig, cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", CONTROL_OWNER), gateway=gateway,
        ceiling=1, trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowCommitted)
    before = current(store, selection)
    request = RequestRearm("rearm:generation-inventory", before.revision, before.gates[0].gate_id,
                           KeepGuidance(), expected_binding=before.binding)
    assert isinstance(workflow.request_rearm(request, OperatorActor("operator", "test")), WorkflowCommitted)
    return store


def test_complete_generation_receipts_read_actual_accepted_boundary_without_writes(rig):
    store = _accepted_generation(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
        current_set = revalidate_complete_legacy_review_generation_receipts(db, observed)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(current_set, CompleteLegacyReviewGenerationReceipts)
    assert len(observed.receipts) == 1
    source = observed.receipts[0]
    assert source.source_rowid > 0
    accepted_source = decode_workflow(
        dict(source.fields)["payload_json"], ReviewExecutionTransitionProofSource,
    )
    assert accepted_source.namespace == "workflow-request"
    assert accepted_source.key == "rearm:generation-inventory"
    assert (accepted_source.boundary.from_revision.fence.generation + 1
            == accepted_source.boundary.to_revision.fence.generation)
    assert len(accepted_source.payload_sha256) == 64 and len(source.source_digest) == 64
    assert len(accepted_source.audit_sha256) == 64


def test_generation_boundary_proves_one_accepted_transition_and_causal_rows(rig):
    store = _accepted_generation(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
        receipt_id = dict(observed.receipts[0].fields)["key"]
        before = db.total_changes
        proof = match_legacy_review_generation_boundary_sources(db, observed, receipt_id)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(proof, MatchedLegacyReviewGenerationBoundarySources)
    assert proof.accepted_rowid > 0 and proof.receipt_source.source_rowid > 0
    assert proof.boundary.to_revision.fence.generation == proof.boundary.from_revision.fence.generation + 1
    assert len(proof.source_digest) == 64
    assert replace(proof, accepted_rowid=proof.receipt_source.source_rowid + 1).source_digest != proof.source_digest


@pytest.mark.parametrize("legacy_format", ["digest", "full_payload"])
def test_generation_boundary_keeps_legacy_receipts_readable(rig, legacy_format):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        row = db.execute(
            "SELECT key,payload_json FROM dedupe_keys WHERE namespace='review-execution-generation'",
        ).fetchone()
        source = decode_workflow(row["payload_json"], ReviewExecutionTransitionProofSource)
        accepted = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope='o/r' AND key=?",
            (source.namespace, source.key),
        ).fetchone()["payload_json"]
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-generation' AND key=?",
            (encode_workflow(
                accepted_review_transition_source(source.boundary, source.namespace, source.key, accepted)
                if legacy_format == "digest"
                else (source.boundary, source.namespace, source.key, accepted)
            ), row["key"]),
        )
    with store.transaction() as db:
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
        proof = match_legacy_review_generation_boundary_sources(db, observed, source.boundary.receipt_id)
        workflow = load_review_workflow_row(db, rig[3].expected.fence.key)
        archive = read_review_generation_archive_page(
            db, source.boundary.before.binding,
            source.boundary.to_revision.fence.generation,
            source.boundary.to_revision.fence.generation,
        )

    assert isinstance(proof, MatchedLegacyReviewGenerationBoundarySources)
    assert workflow is not None and workflow.execution.boundaries[-1] == source.boundary
    assert isinstance(archive, ObservedReviewGenerationArchivePage)
    assert archive.sources[0].boundary == source.boundary


def test_generation_proof_rejects_changed_cause_with_unchanged_accepted_bytes(rig):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-generation'",
        ).fetchone()
        source = decode_workflow(row["payload_json"], ReviewExecutionTransitionProofSource)
        changed = replace(source, audit_sha256="f" * 64)
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-generation'",
            (encode_workflow(changed),),
        )
    with store.transaction() as db:
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
        matched = match_legacy_review_generation_boundary_sources(db, observed, source.boundary.receipt_id)
        archive = read_review_generation_archive_page(
            db, source.boundary.before.binding,
            source.boundary.to_revision.fence.generation,
            source.boundary.to_revision.fence.generation,
        )
        with pytest.raises(WorkflowAuthorityError, match="proof disagrees"):
            load_review_workflow_row(db, rig[1].revision.fence.key)
    assert isinstance(matched, UnmatchedLegacyReviewGenerationBoundarySources)
    assert isinstance(archive, BlockedReviewGenerationArchivePage)


def test_generation_digest_rejects_a_changed_accepted_payload(rig):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-generation'",
        ).fetchone()
        source = decode_workflow(row["payload_json"], ReviewExecutionTransitionProofSource)
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? WHERE namespace=? AND scope='o/r' AND key=?",
            ("{}", source.namespace, source.key),
        )
    with store.transaction() as db:
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
        proof = match_legacy_review_generation_boundary_sources(db, observed, source.boundary.receipt_id)
        archive = read_review_generation_archive_page(
            db, source.boundary.before.binding,
            source.boundary.to_revision.fence.generation,
            source.boundary.to_revision.fence.generation,
        )
        with pytest.raises(WorkflowAuthorityError, match="lost its accepted transition"):
            load_review_workflow_row(db, rig[1].revision.fence.key)

    assert isinstance(proof, UnmatchedLegacyReviewGenerationBoundarySources)
    assert isinstance(archive, BlockedReviewGenerationArchivePage)
    assert "accepted transition" in archive.reason


def test_generation_archive_page_reads_exact_expected_source_without_writes(rig):
    store = _accepted_generation(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        workflow = load_review_workflow_row(db, rig[1].revision.fence.key)
        assert workflow is not None
        boundary = workflow.execution.boundaries[0]
        binding = boundary.before.binding
        generation = boundary.to_revision.fence.generation
        before = db.total_changes
        page = read_review_generation_archive_page(db, binding, generation, generation)
        foreign = read_review_generation_archive_page(
            db, replace(binding, execution_episode="another-episode"), generation, generation,
        )
        assert db.total_changes == before
        db.rollback()

    assert isinstance(page, ObservedReviewGenerationArchivePage)
    assert len(page.sources) == 1 and page.sources[0].generation == generation
    assert page.sources[0].boundary == workflow.execution.boundaries[0]
    assert len(page.sources[0].receipt_digest) == len(page.sources[0].accepted_digest) == 64
    assert page.next_generation == generation + 1 and not page.has_more
    assert isinstance(foreign, BlockedReviewGenerationArchivePage)
    assert foreign.generation == generation and "absent" in foreign.reason


def test_checkpoint_generation_anchor_matches_retained_pair_without_writes(rig):
    store = _accepted_generation(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        workflow = load_review_workflow_row(db, rig[1].revision.fence.key)
        assert workflow is not None
        boundary = workflow.execution.boundaries[0]
        binding = boundary.before.binding
        generation = boundary.to_revision.fence.generation
        page = read_review_generation_archive_page(db, binding, generation, generation)
        assert isinstance(page, ObservedReviewGenerationArchivePage)
        source = page.sources[0]
        fingerprint = ReviewGenerationArchiveFingerprint(
            ReviewExecutionBoundaryCursor(source.boundary.receipt_id, source.boundary.to_revision),
            source.receipt_digest, source.accepted_namespace, source.accepted_key,
            source.accepted_digest,
        )
        before = db.total_changes
        assert verify_checkpoint_generation_anchor(db, binding, fingerprint) == fingerprint
        assert db.total_changes == before
        with pytest.raises(WorkflowAuthorityError, match="checkpoint generation anchor"):
            verify_checkpoint_generation_anchor(
                db, binding, replace(fingerprint, accepted_sha256="a" * 64),
            )
        with pytest.raises(WorkflowAuthorityError, match="checkpoint generation anchor"):
            verify_checkpoint_generation_anchor(
                db, replace(binding, execution_episode="foreign-episode"), fingerprint,
            )
        db.rollback()

    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00+00:00' "
            "WHERE namespace=? AND scope='o/r' AND key=?",
            (source.accepted_namespace, source.accepted_key),
        )
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        with pytest.raises(WorkflowAuthorityError, match="checkpoint generation anchor"):
            verify_checkpoint_generation_anchor(db, binding, fingerprint)
        db.rollback()


def test_checkpoint_manifest_observes_real_round_and_generation_sources(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        before = db.total_changes
        observed = observe_review_checkpoint_manifest(db, key, 1)
        assert observed.plan.checkpoint.through_sequence == 1
        assert len(observed.plan.retired_outcomes) == 1
        assert len(observed.plan.retired_boundaries) == 1
        assert observed.receipt.last_round_source is not None
        assert observed.receipt.last_generation_source is not None
        assert observed.receipt.last_round_source.round == observed.plan.retired_outcomes[0].round
        assert observed.receipt.last_generation_source.cursor.receipt_id == (
            observed.plan.retired_boundaries[0].receipt_id
        )
        assert observed.source_bytes > 0
        assert db.total_changes == before
        assert observe_review_checkpoint_manifest(
            db, key, 1, max_source_bytes=observed.source_bytes,
        ).receipt == observed.receipt
        with pytest.raises(WorkflowAuthorityError, match="byte bound"):
            observe_review_checkpoint_manifest(
                db, key, 1, max_source_bytes=observed.source_bytes - 1,
            )


def test_checkpoint_round_receipt_members_match_the_indexed_source_sweep(rig):
    store = _accepted_generation(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        row = db.execute(
            "SELECT outcome_json FROM review_execution_rounds WHERE repository='o/r' AND sequence=1",
        ).fetchone()
        outcome = decode_workflow(row["outcome_json"], ReviewRoundOutcome)
        before = db.total_changes
        expected = read_checkpoint_round_receipt_members(db, outcome.round)
        assert expected.source == read_checkpoint_round_receipt_source(db, outcome.round)
        with pytest.raises(ValueError, match="disagree with their aggregate"):
            replace(expected, members=(
                replace(expected.members[0], source_sha256="0" * 64),
                *expected.members[1:],
            ))
        actual = {}
        cursor = None
        upper = None
        while True:
            page = read_review_round_receipt_sweep_page(db, after=cursor, upper=upper)
            assert isinstance(page, ObservedReviewRoundReceiptSweepPage)
            actual.update({
                (item.cursor.namespace, item.cursor.scope, item.cursor.key): item.source_sha256
                for item in page.sources
            })
            if not page.has_more:
                break
            cursor, upper = page.next_cursor, page.upper_cursor
        assert db.total_changes == before
        db.rollback()
    assert len(expected.members) >= 3
    assert {
        (member.namespace, "o/r", member.key): member.source_sha256
        for member in expected.members
    }.items() <= actual.items()


def test_checkpoint_round_receipt_member_digest_tracks_retained_source_change(rig):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        row = db.execute(
            "SELECT outcome_json FROM review_execution_rounds WHERE repository='o/r' AND sequence=1",
        ).fetchone()
        outcome = decode_workflow(row["outcome_json"], ReviewRoundOutcome)
        before = read_checkpoint_round_receipt_members(db, outcome.round)
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-09-30T00:00:00+00:00' "
            "WHERE namespace='review-round-outcome' AND scope='o/r' AND key=?",
            (outcome.round.round_id,),
        )
        after = read_checkpoint_round_receipt_members(db, outcome.round)
    before_by_key = {(item.namespace, item.key): item.source_sha256 for item in before.members}
    after_by_key = {(item.namespace, item.key): item.source_sha256 for item in after.members}
    assert before_by_key.keys() == after_by_key.keys()
    assert before_by_key[("review-round-outcome", outcome.round.round_id)] != (
        after_by_key[("review-round-outcome", outcome.round.round_id)]
    )
    assert before.source.receipt_set_sha256 != after.source.receipt_set_sha256


@pytest.mark.parametrize("mutation", ("missing", "aliased", "changed"))
def test_checkpoint_manifest_refuses_corrupt_retired_outcome_source(rig, mutation):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        row = db.execute(
            "SELECT scope,key,claimed_at,payload_json FROM dedupe_keys "
            "WHERE namespace='review-round-outcome' AND scope='o/r'",
        ).fetchone()
        assert row is not None
        if mutation == "missing":
            db.execute(
                "DELETE FROM dedupe_keys WHERE namespace='review-round-outcome' "
                "AND scope=? AND key=?",
                (row["scope"], row["key"]),
            )
        elif mutation == "aliased":
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (CAST(? AS BLOB),?,?,?,?)",
                ("review-round-outcome", row["scope"], row["key"],
                 row["claimed_at"], row["payload_json"]),
            )
        else:
            db.execute(
                "UPDATE dedupe_keys SET payload_json='{}' "
                "WHERE namespace='review-round-outcome' AND scope=? AND key=?",
                (row["scope"], row["key"]),
            )
        with pytest.raises(WorkflowAuthorityError):
            observe_review_checkpoint_manifest(db, key, 1)


def test_staged_checkpoint_load_checks_generation_anchor_before_global_refusal(rig):
    store = _accepted_generation(rig)
    key = rig[1].revision.fence.key
    with store.transaction() as db:
        workflow = load_review_workflow_row(db, key)
        assert workflow is not None
        execution = workflow.execution
        assert isinstance(execution, ActivatedReviewExecution)
        assert execution.last_sequence == 1 and len(execution.boundaries) == 1
        outcome_row = db.execute(
            "SELECT outcome_json FROM review_execution_rounds WHERE repository='o/r' AND sequence=1",
        ).fetchone()
        outcome = decode_workflow(outcome_row["outcome_json"], ReviewRoundOutcome)
        plan = plan_review_execution_compaction(execution, (outcome,), 1)
        boundary = execution.boundaries[0]
        page = read_review_generation_archive_page(
            db, boundary.before.binding, boundary.to_revision.fence.generation,
            boundary.to_revision.fence.generation,
        )
        assert isinstance(page, ObservedReviewGenerationArchivePage)
        source = page.sources[0]
        fingerprint = ReviewGenerationArchiveFingerprint(
            ReviewExecutionBoundaryCursor(source.boundary.receipt_id, source.boundary.to_revision),
            source.receipt_digest, source.accepted_namespace, source.accepted_key,
            source.accepted_digest,
        )
        receipt = build_review_execution_checkpoint_receipt(
            execution, plan,
            (ReviewExecutionCompactionRoundSource(
                outcome.round, read_checkpoint_round_row_source(db, outcome.round).source_digest,
                read_checkpoint_round_receipt_source(db, outcome.round).receipt_set_sha256,
            ),),
            (fingerprint,),
        )
        receipt_id = review_execution_checkpoint_receipt_id(receipt)
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('review-execution-checkpoint','o/r',?,'2026-09-29T00:00:00+00:00',?)",
            (receipt_id, encode_workflow(receipt)),
        )
        compacted = replace(workflow, execution=replace(
            execution, checkpoint=plan.checkpoint, state=plan.checkpoint.state,
            checkpoint_receipt=receipt_id, boundaries=plan.remaining_boundaries,
        ))
        db.execute(
            "UPDATE workflows SET episode_json=? WHERE repository='o/r' AND target_kind='pr'",
            (encode_workflow_episodes(compacted),),
        )
        with pytest.raises(WorkflowAuthorityError, match="source anchors"):
            load_review_workflow_row(db, key)
        db.execute(
            "UPDATE dedupe_keys SET claimed_at='2026-09-30T00:00:00+00:00' "
            "WHERE namespace=? AND scope='o/r' AND key=?",
            (fingerprint.accepted_namespace, fingerprint.accepted_key),
        )
        with pytest.raises(WorkflowAuthorityError, match="checkpoint generation anchor"):
            load_review_workflow_row(db, key)


@pytest.mark.parametrize("corruption", ["missing", "blob_alias", "naive_claim", "pair_budget"])
def test_checkpoint_generation_anchor_refuses_incomplete_or_unbounded_source_pair(rig, monkeypatch, corruption):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        workflow = load_review_workflow_row(db, rig[1].revision.fence.key)
        assert workflow is not None
        boundary = workflow.execution.boundaries[0]
        binding = boundary.before.binding
        generation = boundary.to_revision.fence.generation
        page = read_review_generation_archive_page(db, binding, generation, generation)
        assert isinstance(page, ObservedReviewGenerationArchivePage)
        source = page.sources[0]
        fingerprint = ReviewGenerationArchiveFingerprint(
            ReviewExecutionBoundaryCursor(source.boundary.receipt_id, source.boundary.to_revision),
            source.receipt_digest, source.accepted_namespace, source.accepted_key,
            source.accepted_digest,
        )
        if corruption == "missing":
            db.execute(
                "DELETE FROM dedupe_keys WHERE namespace=? AND scope='o/r' AND key=?",
                (source.accepted_namespace, source.accepted_key),
            )
        elif corruption == "blob_alias":
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES (CAST(? AS BLOB),'o/r',?,'2026-09-29T00:00:00+00:00','{}')",
                (source.accepted_namespace, source.accepted_key),
            )
        elif corruption == "naive_claim":
            db.execute(
                "UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00' "
                "WHERE namespace=? AND scope='o/r' AND key=?",
                (source.accepted_namespace, source.accepted_key),
            )
        else:
            receipt_size = review_activation._recording_source_size(
                db, "review-execution-generation", "o/r", source.boundary.receipt_id,
            )
            accepted_size = review_activation._recording_source_size(
                db, source.accepted_namespace, "o/r", source.accepted_key,
            )
            assert receipt_size is not None and accepted_size is not None
            monkeypatch.setattr(
                review_activation, "MAX_GENERATION_ARCHIVE_PAGE_BYTES",
                receipt_size + accepted_size - 1,
            )
        with pytest.raises(WorkflowAuthorityError, match="checkpoint generation anchor"):
            verify_checkpoint_generation_anchor(db, binding, fingerprint)


def test_generation_archive_page_is_bounded_and_requires_every_expected_receipt(rig, monkeypatch):
    store, _, selection, _, _, _, workflow = accepted_generation_with_open_submission(rig)
    first = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:archive-page", first.revision, "rearm:with-older-submission", "stop",
                    expected_binding=first.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:archive-page", stopped.revision,
                             "rearm:with-older-submission", expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)
    execution = current(store, selection).execution
    binding = execution.boundaries[0].before.binding
    last = execution.binding.rejection.fence.generation
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        first_page = read_review_generation_archive_page(db, binding, last - 1, last, limit=1)
        assert isinstance(first_page, ObservedReviewGenerationArchivePage)
        second_page = read_review_generation_archive_page(
            db, binding, last, last, preceding=first_page.sources[0].boundary, limit=1,
        )
        small = ReviewExecutionBoundaryCursor(
            first_page.sources[0].boundary.receipt_id,
            first_page.sources[0].boundary.to_revision,
        )
        resumed = read_review_generation_archive_page(
            db, binding, last, last, preceding=small, limit=1,
        )
        with pytest.raises(ValueError, match="same-episode predecessor"):
            read_review_generation_archive_page(
                db, binding, last, last, preceding=replace(small, receipt_id="forged"), limit=1,
            )
        with pytest.raises(ValueError, match="contiguous generation"):
            read_review_generation_archive_page(db, binding, last, last)
        db.rollback()
    assert isinstance(first_page, ObservedReviewGenerationArchivePage)
    assert isinstance(second_page, ObservedReviewGenerationArchivePage)
    assert resumed == second_page
    assert first_page.next_generation == last and first_page.has_more
    assert second_page.next_generation == last + 1 and not second_page.has_more
    assert [first_page.sources[0].generation, second_page.sources[0].generation] == [last - 1, last]

    second = second_page.sources[0]
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET payload_json=CAST(x'ff' AS BLOB) "
            "WHERE namespace=? AND scope='o/r' AND key=?",
            (second.accepted_namespace, second.accepted_key),
        )
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        first_source = first_page.sources[0]
        first_bytes = (
            review_activation._recording_source_size(
                db, "review-execution-generation", "o/r", first_source.boundary.receipt_id,
            ) + review_activation._recording_source_size(
                db, first_source.accepted_namespace, "o/r", first_source.accepted_key,
            )
        )
        with monkeypatch.context() as patcher:
            patcher.setattr(review_activation, "MAX_GENERATION_ARCHIVE_PAGE_BYTES", first_bytes)
            bounded = read_review_generation_archive_page(db, binding, last - 1, last)
        malformed = read_review_generation_archive_page(
            db, binding, last, last, preceding=first_page.sources[0].boundary,
        )
        db.rollback()
    assert isinstance(bounded, ObservedReviewGenerationArchivePage)
    assert len(bounded.sources) == 1 and bounded.next_generation == last and bounded.has_more
    assert isinstance(malformed, BlockedReviewGenerationArchivePage)
    assert malformed.generation == last

    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-generation' AND key=?",
                   (second_page.sources[0].boundary.receipt_id,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        missing = read_review_generation_archive_page(db, binding, last - 1, last)
        db.rollback()
    assert isinstance(missing, BlockedReviewGenerationArchivePage)
    assert missing.generation == last and "absent" in missing.reason


def test_generation_receipt_sweep_observes_source_without_writes(rig):
    store = _accepted_generation(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        workflow = load_review_workflow_row(db, rig[1].revision.fence.key)
        assert workflow is not None
        boundary = workflow.execution.boundaries[0]
        before = db.total_changes
        sweep = read_review_generation_receipt_sweep_page(db, boundary.before.binding)
        archive = read_review_generation_archive_page(
            db, boundary.before.binding,
            boundary.to_revision.fence.generation,
            boundary.to_revision.fence.generation,
        )
        assert db.total_changes == before
        db.rollback()
    assert isinstance(sweep, ObservedReviewGenerationReceiptSweepPage)
    assert isinstance(archive, ObservedReviewGenerationArchivePage)
    assert len(sweep.sources) == 1 and not sweep.has_more
    assert sweep.next_cursor == sweep.upper_cursor
    assert sweep.sources[0].boundary == boundary
    assert sweep.sources[0].receipt_digest == archive.sources[0].receipt_digest


def test_generation_receipt_reconciliation_blocks_an_extra_valid_advance(rig, monkeypatch):
    store, _, selection, _, _, _, workflow = accepted_generation_with_open_submission(rig)
    first = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:reconcile", first.revision, "rearm:with-older-submission", "stop",
                    expected_binding=first.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:reconcile", stopped.revision,
                             "rearm:with-older-submission", expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)
    execution = current(store, selection).execution
    origin = execution.boundaries[0].before.binding
    first_generation = execution.boundaries[0].to_revision.fence.generation
    last_generation = execution.binding.rejection.fence.generation
    monkeypatch.setattr(review_activation, "MAX_GENERATION_RECEIPT_SWEEP_PAGE", 1)

    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        cursor = upper = None
        observed = []
        while True:
            page = read_reconciled_review_generation_receipt_sweep_page(
                db, origin, last_generation, after=cursor, upper=upper,
            )
            assert isinstance(page, ObservedReviewGenerationReceiptSweepPage)
            observed.extend(page.sources)
            if not page.has_more:
                break
            cursor, upper = page.next_cursor, page.upper_cursor
        assert len(observed) == 2

        cursor = upper = None
        while True:
            limited = read_reconciled_review_generation_receipt_sweep_page(
                db, origin, first_generation, after=cursor, upper=upper,
            )
            if isinstance(limited, BlockedReviewGenerationReceiptSweepPage):
                break
            assert limited.has_more
            cursor, upper = limited.next_cursor, limited.upper_cursor
        assert db.total_changes == before
        db.rollback()
    assert "outside the expected episode frontier" in limited.reason
    assert limited.source_rowid is not None


@pytest.mark.parametrize("damage", ["blob_namespace", "blob_scope", "blob_key", "scope_alias",
                                    "wrong_key", "foreign_scope", "malformed_payload", "oversized_payload"])
def test_generation_receipt_sweep_blocks_unclaimed_or_malformed_source(rig, damage):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        row = db.execute(
            "SELECT namespace,scope,key,claimed_at,reusable_after,payload_json "
            "FROM dedupe_keys WHERE namespace='review-execution-generation'",
        ).fetchone()
        if damage in {"malformed_payload", "oversized_payload"}:
            db.execute(
                "UPDATE dedupe_keys SET payload_json=? "
                "WHERE namespace='review-execution-generation'",
                ("{}" if damage == "malformed_payload" else "x" * (2**21 + 1),),
            )
        else:
            values = tuple(row)
            if damage == "blob_namespace":
                values = (b"review-execution-generation", *values[1:])
            elif damage == "blob_scope":
                values = (values[0], b"o/r", *values[2:])
            elif damage == "blob_key":
                values = (*values[:2], values[2].encode(), *values[3:])
            elif damage == "scope_alias":
                values = (values[0], "O/R", *values[2:])
            elif damage == "wrong_key":
                values = (*values[:2], "unclaimed:generation", *values[3:])
            else:
                values = (values[0], "x/y", *values[2:])
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,reusable_after,payload_json) "
                "VALUES (?,?,?,?,?,?)", values,
            )
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        binding = decode_workflow(row["payload_json"], ReviewExecutionTransitionProofSource).boundary.before.binding
        sweep = read_review_generation_receipt_sweep_page(db, binding)
        db.rollback()
    assert isinstance(sweep, BlockedReviewGenerationReceiptSweepPage)


def test_generation_receipt_sweep_pages_to_fixed_upper_and_catches_later_insert(rig, monkeypatch):
    store, _, selection, _, _, _, workflow = accepted_generation_with_open_submission(rig)
    first = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:sweep-page", first.revision, "rearm:with-older-submission", "stop",
                    expected_binding=first.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:sweep-page", stopped.revision,
                             "rearm:with-older-submission", expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)
    binding = current(store, selection).execution.boundaries[0].before.binding
    monkeypatch.setattr(review_activation, "MAX_GENERATION_RECEIPT_SWEEP_PAGE", 1)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        first_page = read_review_generation_receipt_sweep_page(db, binding)
        assert isinstance(first_page, ObservedReviewGenerationReceiptSweepPage)
        db.rollback()
    assert first_page.has_more and first_page.next_cursor != first_page.upper_cursor
    with store.transaction() as db:
        source = db.execute(
            "SELECT namespace,scope,claimed_at,reusable_after,payload_json FROM dedupe_keys "
            "WHERE namespace='review-execution-generation' LIMIT 1",
        ).fetchone()
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,reusable_after,payload_json) "
            "VALUES (?,?,?,?,?,?)", (source["namespace"], source["scope"], "zzzz:unclaimed",
                                  source["claimed_at"], source["reusable_after"], source["payload_json"]),
        )
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        second_page = read_review_generation_receipt_sweep_page(
            db, binding, after=first_page.next_cursor, upper=first_page.upper_cursor,
        )
        db.rollback()
    assert isinstance(second_page, ObservedReviewGenerationReceiptSweepPage)
    assert len(second_page.sources) == 1 and not second_page.has_more
    assert second_page.next_cursor == first_page.upper_cursor

    with closing(store._connect()) as db:
        db.execute("BEGIN")
        cursor = None
        upper = None
        while True:
            page = read_review_generation_receipt_sweep_page(db, binding, after=cursor, upper=upper)
            if isinstance(page, BlockedReviewGenerationReceiptSweepPage):
                break
            assert page.has_more
            cursor, upper = page.next_cursor, page.upper_cursor
        db.rollback()
    assert "identity" in page.reason


def test_generation_source_match_does_not_infer_time_from_sql_rowids(rig):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        row = db.execute("SELECT namespace,scope,key,claimed_at,reusable_after,payload_json "
                         "FROM dedupe_keys WHERE namespace='review-execution-generation'").fetchone()
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-generation'")
        db.execute("INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,reusable_after,payload_json) "
                   "VALUES (?,?,?,?,?,?,?)", (-1, *tuple(row)))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
        result = match_legacy_review_generation_boundary_sources(db, observed, row["key"])
        db.rollback()
    assert isinstance(result, MatchedLegacyReviewGenerationBoundarySources)
    assert result.receipt_source.source_rowid < result.accepted_rowid


@pytest.mark.parametrize("accepted_rowid", [-1, 0])
def test_generation_source_match_accepts_nonpositive_accepted_sql_rowid(rig, accepted_rowid):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        row = db.execute("SELECT namespace,scope,key,claimed_at,reusable_after,payload_json "
                         "FROM dedupe_keys WHERE namespace='workflow-request' "
                         "AND key='rearm:generation-inventory'").fetchone()
        db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-request' "
                   "AND key='rearm:generation-inventory'")
        db.execute("INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,reusable_after,payload_json) "
                   "VALUES (?,?,?,?,?,?,?)", (accepted_rowid, *tuple(row)))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
        receipt_id = dict(observed.receipts[0].fields)["key"]
        result = match_legacy_review_generation_boundary_sources(db, observed, receipt_id)
        db.rollback()
    assert isinstance(result, MatchedLegacyReviewGenerationBoundarySources)
    assert result.accepted_rowid == accepted_rowid


@pytest.mark.parametrize("damage", ["boundary_payload", "transition_payload", "boundary_reusable",
                                    "transition_reusable", "transition_removed", "transition_alias",
                                    "transition_scope_case_alias", "transition_scope_space_alias"])
def test_generation_boundary_rejects_changed_or_contradictory_sources(rig, damage):
    store = _accepted_generation(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        db.rollback()
    assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
    receipt_id = dict(observed.receipts[0].fields)["key"]
    with store.transaction() as db:
        if damage == "boundary_payload":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' "
                       "WHERE namespace='review-execution-generation'")
        elif damage == "transition_payload":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-request' "
                       "AND key='rearm:generation-inventory'")
        elif damage == "boundary_reusable":
            db.execute("UPDATE dedupe_keys SET reusable_after='later' "
                       "WHERE namespace='review-execution-generation'")
        elif damage == "transition_reusable":
            db.execute("UPDATE dedupe_keys SET reusable_after='later' WHERE namespace='workflow-request' "
                       "AND key='rearm:generation-inventory'")
        elif damage == "transition_removed":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-request' "
                       "AND key='rearm:generation-inventory'")
        elif damage in {"transition_scope_case_alias", "transition_scope_space_alias"}:
            scope = "O/R" if damage == "transition_scope_case_alias" else " o/r "
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,?,?,?,?)",
                       ("workflow-request", scope, "rearm:generation-inventory", "now", "{}"))
        else:
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,?,?,?,?)",
                       (b"workflow-request", "o/r", "rearm:generation-inventory", "now", "{}"))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = match_legacy_review_generation_boundary_sources(db, observed, receipt_id)
        db.rollback()
    assert isinstance(result, UnmatchedLegacyReviewGenerationBoundarySources)


@pytest.mark.parametrize("damage", ["wrong_audit_variant", "release_audit", "malformed_commands"])
def test_generation_boundary_rejects_mutated_accepted_transition_contents(rig, damage):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-generation'").fetchone()
        source = decode_workflow(row["payload_json"], ReviewExecutionTransitionProofSource)
        boundary, namespace, key = source.boundary, source.namespace, source.key
        accepted = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND key=?",
                              (namespace, key)).fetchone()["payload_json"]
        payload = json.loads(accepted)
        if damage == "wrong_audit_variant":
            audit = decode_workflow(payload["audit"], AuthorizedOperatorCommand)
            alternate = ContinueGrantedRearm(
                audit.command.request_id, audit.command.expected, "stopped:other",
                expected_binding=audit.command.expected_binding,
            )
            payload["audit"] = encode_workflow(replace(audit, command=alternate))
        elif damage == "release_audit":
            payload["audit"] = encode_workflow(ObservedTransition(("unrelated",)))
        else:
            payload["commands"] = [{"id": "truncated"}]
        changed = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='workflow-request' AND key=?",
                   (changed, key))
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-generation'",
                   (encode_workflow(accepted_review_transition_source(
                       boundary, namespace, key, changed,
                   )),))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
        result = match_legacy_review_generation_boundary_sources(db, observed, boundary.receipt_id)
        archive = read_review_generation_archive_page(
            db, boundary.before.binding,
            boundary.to_revision.fence.generation,
            boundary.to_revision.fence.generation,
        )
        db.rollback()
    assert isinstance(result, UnmatchedLegacyReviewGenerationBoundarySources)
    assert isinstance(archive, BlockedReviewGenerationArchivePage)


@pytest.mark.parametrize("damage", ["blob_alias", "scope_case_alias", "scope_space_alias",
                                    "invalid_utf8", "oversized", "reclaimable"])
def test_complete_generation_receipts_block_unreadable_or_contradictory_rows(rig, damage):
    store = _accepted_generation(rig)
    with store.transaction() as db:
        if damage == "blob_alias":
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                (b"review-execution-generation", "o/r", "alias", "now", "{}"),
            )
        elif damage in {"scope_case_alias", "scope_space_alias"}:
            scope = "O/R" if damage == "scope_case_alias" else " o/r "
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                ("review-execution-generation", scope, "alias", "now", "{}"),
            )
        elif damage == "invalid_utf8":
            db.execute("UPDATE dedupe_keys SET payload_json=CAST(x'FF' AS TEXT) "
                       "WHERE namespace='review-execution-generation'")
        elif damage == "oversized":
            db.execute("UPDATE dedupe_keys SET payload_json=? "
                       "WHERE namespace='review-execution-generation'",
                       ("x" * (MAX_LEGACY_GENERATION_RECEIPT_JSON_BYTES + 1),))
        else:
            db.execute("UPDATE dedupe_keys SET reusable_after='later' "
                       "WHERE namespace='review-execution-generation'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = read_complete_legacy_review_generation_receipts(db, "o/r")
        db.rollback()

    if damage == "reclaimable":
        assert isinstance(result, CompleteLegacyReviewGenerationReceipts)
        assert dict(result.receipts[0].fields)["reusable_after"] == "later"
    else:
        assert isinstance(result, BlockedLegacyReviewGenerationReceipts)


def test_generation_receipts_revalidation_rejects_partial_changed_or_reordered_sources(rig):
    store = _accepted_generation(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_generation_receipts(db, "o/r")
        db.rollback()
    assert isinstance(observed, CompleteLegacyReviewGenerationReceipts)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        assert isinstance(revalidate_complete_legacy_review_generation_receipts(
            db, CompleteLegacyReviewGenerationReceipts("o/r", ()),
        ), BlockedLegacyReviewGenerationReceipts)
        db.rollback()
    with store.transaction() as db:
        fields = db.execute("SELECT namespace,scope,key,claimed_at,reusable_after,payload_json "
                            "FROM dedupe_keys WHERE namespace='review-execution-generation'").fetchone()
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-generation'")
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,reusable_after,payload_json) "
                   "VALUES (?,?,?,?,?,?)", tuple(fields))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        changed = revalidate_complete_legacy_review_generation_receipts(db, observed)
        db.rollback()
    assert isinstance(changed, BlockedLegacyReviewGenerationReceipts)


def test_generation_receipt_scan_bounds_unrelated_shared_rows(tmp_path, monkeypatch):
    store = _control(tmp_path)
    monkeypatch.setattr(review_activation, "MAX_LEGACY_GENERATION_RECEIPT_SCAN_ROWS", 3)
    with store.transaction() as db:
        for number in range(4):
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES ('other','o/r',?,'now','{}')", (str(number),))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = read_complete_legacy_review_generation_receipts(db, "o/r")
        db.rollback()
    assert isinstance(result, BlockedLegacyReviewGenerationReceipts)


def test_generation_receipt_scan_preserves_negative_and_zero_rowids(tmp_path):
    store = _control(tmp_path)
    with store.transaction() as db:
        for rowid in (-1, 0):
            db.execute("INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,'review-execution-generation','o/r',?,'now','{}')",
                       (rowid, f"generation:{rowid}"))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = read_complete_legacy_review_generation_receipts(db, "o/r")
        db.rollback()
    assert isinstance(result, CompleteLegacyReviewGenerationReceipts)
    assert [source.source_rowid for source in result.receipts] == [-1, 0]
    assert all(isinstance(source, LegacyReviewGenerationReceiptSource) for source in result.receipts)


def _stale_settlement(rig):
    store, _, _, _, plan, command, workflow = accepted_generation_with_open_submission(rig)
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        complete = verify_complete_legacy_submission_settlement_storage(
            db, *_complete_settlement_observations(db),
        )
        db.rollback()
    assert isinstance(complete, CompleteLegacySubmissionSettlementStorage)
    return store, plan, command, complete


def _stale_generation_sources(db):
    storage = verify_complete_legacy_submission_settlement_storage(
        db, *_complete_settlement_observations(db),
    )
    generation = read_complete_legacy_review_generation_receipts(db, "o/r")
    assert isinstance(storage, CompleteLegacySubmissionSettlementStorage)
    assert isinstance(generation, CompleteLegacyReviewGenerationReceipts)
    return storage, generation


def _classification_observations(db):
    storage, generation = _stale_generation_sources(db)
    lineage = read_legacy_review_owner_lineage(db, "o/r", CONTROL_OWNER)
    assert isinstance(lineage, LegacyReviewOwnerLineage)
    return storage, generation, lineage


def _ordered_coverage(store):
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
        before = db.total_changes
        ordered = verify_complete_legacy_ordered_outcome_coverage(db, classified)
        assert db.total_changes == before
        db.rollback()
    return ordered


def _allocation_generation_coverage(store):
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        _, _, storage = _complete_settlement_observations(db)
        generation = read_complete_legacy_review_generation_receipts(db, "o/r")
        assert isinstance(generation, CompleteLegacyReviewGenerationReceipts)
        before = db.total_changes
        matched = verify_complete_legacy_allocation_generation_sources(db, storage, generation)
        assert db.total_changes == before
        db.rollback()
    return matched


def _ordered_replay(store):
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
        allocations = verify_complete_legacy_allocation_generation_sources(
            db, classified.storage.round_storage, classified.generation,
        )
        assert isinstance(allocations, CompleteLegacyAllocationGenerationSources)
        before = db.total_changes
        replayed = observe_legacy_ordered_checkpoint_replay(db, classified, allocations)
        assert db.total_changes == before
        db.rollback()
    return replayed


@pytest.mark.parametrize("escalated", [False, True])
def test_ordered_outcome_replay_matches_current_productive_execution(rig, escalated):
    store, gateway, _, control, plan, command = (
        confirmed_escalation(rig) if escalated else confirmed_copy(rig)
    )
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    control.finalize_completed_round(plan.round.round_id)

    replayed = _ordered_replay(store)

    assert isinstance(replayed, ObservedLegacyOrderedCheckpointReplay)
    assert replayed.coverage.episodes[0].outcomes[0].round == plan.round
    assert len(replayed.source_digest) == 64


def test_ordered_outcome_replay_keeps_unproductive_finalization(stopped_round):
    store, _, control, _, child, _, _ = stopped_round
    finalized = control.finalize_completed_round(child.round.round_id)

    replayed = _ordered_replay(store)

    assert isinstance(replayed, ObservedLegacyOrderedCheckpointReplay)
    assert replayed.coverage.episodes[0].outcomes == (finalized.outcome,)


def test_ordered_outcome_replay_keeps_earlier_open_gap_before_productive_round(rig):
    store, _, gateway, selection, control = rig
    first = control.preflight(selection)
    later = control.preflight(replace(
        selection, request_id="preflight:replay-later", expected=current(store, selection).revision,
    ))
    command = command_for(current(store, selection))
    control.register_submission(later.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    control.seal_completed_planning(later.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, CONTROL_OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:replay-later"))
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    finalized = control.finalize_completed_round(later.round.round_id)

    replayed = _ordered_replay(store)

    assert isinstance(replayed, ObservedLegacyOrderedCheckpointReplay)
    assert [entry.round for entry in replayed.coverage.episodes[0].rounds] == [first.round, later.round]
    assert replayed.coverage.episodes[0].outcomes == (finalized.outcome,)


def test_ordered_outcome_replay_blocks_unresolved_historical_success(rig):
    store, _, _, _, _, _, _ = accepted_generation_with_open_submission(rig)

    replayed = _ordered_replay(store)

    assert isinstance(replayed, BlockedLegacyOrderedCheckpointReplay)
    assert "unresolved" in replayed.reason


def test_allocation_generation_sources_assign_current_open_round(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)

    matched = _allocation_generation_coverage(store)

    assert isinstance(matched, CompleteLegacyAllocationGenerationSources)
    assert matched.episodes[0].rounds == (plan.round,)
    assert matched.episodes[0].boundaries == ()
    assert len(matched.source_digest) == 64


def test_allocation_generation_sources_assign_old_open_round_across_accepted_boundary(rig):
    store, _, _, _, old, _, _ = accepted_generation_with_open_submission(rig)

    matched = _allocation_generation_coverage(store)

    assert isinstance(matched, CompleteLegacyAllocationGenerationSources)
    assert [round_.sequence for round_ in matched.episodes[0].rounds] == [1, 2]
    assert matched.episodes[0].rounds[0] == old.round
    assert len(matched.episodes[0].boundaries) == 1
    assert matched.episodes[0].boundaries[0].boundary.through_sequence == 2


def test_allocation_generation_sources_keep_two_boundary_advances_at_one_sequence(rig):
    store, _, selection, _, old, _, workflow = accepted_generation_with_open_submission(rig)
    first = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:allocation-generation", first.revision, "rearm:with-older-submission", "stop",
                    expected_binding=first.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:allocation-generation", stopped.revision,
                             "rearm:with-older-submission", expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)

    matched = _allocation_generation_coverage(store)

    assert isinstance(matched, CompleteLegacyAllocationGenerationSources)
    assert matched.episodes[0].rounds[0] == old.round
    assert [item.boundary.through_sequence for item in matched.episodes[0].boundaries] == [2, 2]


def test_allocation_generation_sources_block_missing_accepted_boundary(rig):
    store, _, _, _, _, _, _ = accepted_generation_with_open_submission(rig)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-generation'")

    matched = _allocation_generation_coverage(store)

    assert isinstance(matched, BlockedLegacyAllocationGenerationSources)
    assert "generation boundary lacks its accepted receipt" in matched.reason


def test_allocation_generation_sources_block_accepted_transition_alias(rig):
    store, _, _, _, _, _, _ = accepted_generation_with_open_submission(rig)
    with store.transaction() as db:
        original = db.execute("SELECT key,claimed_at,payload_json FROM dedupe_keys "
                              "WHERE namespace='workflow-request' AND key='rearm:with-older-submission'").fetchone()
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                   "VALUES (?,?,?,?,?)", ("workflow-request", "O/R", *original))

    matched = _allocation_generation_coverage(store)

    assert isinstance(matched, BlockedLegacyAllocationGenerationSources)
    assert "repository alias" in matched.reason


def test_allocation_generation_sources_block_forged_checkpoint_before_classification(rig):
    store, _, _, selection, control = rig
    control.preflight(selection)
    _forge_generation_with_open_round(store, selection)

    matched = _allocation_generation_coverage(store)

    assert isinstance(matched, BlockedLegacyAllocationGenerationSources)
    assert "origin" in matched.reason


@pytest.mark.parametrize("escalated", [False, True])
def test_ordered_outcome_coverage_pairs_each_recorded_productive_round(rig, escalated):
    store, gateway, selection, control, plan, command = (
        confirmed_escalation(rig) if escalated else confirmed_copy(rig)
    )
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    control.finalize_completed_round(plan.round.round_id)

    ordered = _ordered_coverage(store)

    assert isinstance(ordered, CompleteLegacyOrderedOutcomeCoverage)
    assert len(ordered.episodes) == 1
    assert ordered.episodes[0].latest_productive_outcome_sequence == plan.round.sequence
    assert ordered.episodes[0].outcomes[0].round == plan.round
    assert len(ordered.source_digest) == 64


def test_ordered_outcome_coverage_keeps_open_later_round_in_sequence(rig):
    store, _, gateway, selection, control = rig
    first = control.preflight(selection)
    later = control.preflight(replace(
        selection, request_id="preflight:ordered-later-open", expected=current(store, selection).revision,
    ))
    command = command_for(current(store, selection))
    control.register_submission(first.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    control.seal_completed_planning(first.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, CONTROL_OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:ordered-first"))
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    control.finalize_completed_round(first.round.round_id)

    ordered = _ordered_coverage(store)

    assert isinstance(ordered, CompleteLegacyOrderedOutcomeCoverage)
    assert [entry.round for entry in ordered.episodes[0].rounds] == [first.round, later.round]
    assert ordered.episodes[0].latest_productive_outcome_sequence == first.round.sequence
    assert len(ordered.episodes[0].outcomes) == 1


def test_ordered_outcome_coverage_retains_unproductive_finalization(stopped_round):
    store, _, control, _, child, _, _ = stopped_round
    finalized = control.finalize_completed_round(child.round.round_id)
    assert isinstance(finalized.outcome.disposition, ReviewUnlaunchedRound)

    ordered = _ordered_coverage(store)

    assert isinstance(ordered, CompleteLegacyOrderedOutcomeCoverage)
    assert ordered.episodes[0].outcomes == (finalized.outcome,)
    assert ordered.episodes[0].latest_productive_outcome_sequence == 0


def test_ordered_outcome_coverage_blocks_unresolved_productive_round(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-recorded-submission' AND scope='o/r' "
                   "AND key=?", (str(command.command_id),))

    ordered = _ordered_coverage(store)

    assert isinstance(ordered, BlockedLegacyOrderedOutcomeCoverage)
    assert "unresolved" in ordered.reason


def test_ordered_outcome_coverage_blocks_forged_checkpoint_before_classification(rig):
    store, _, _, selection, control = rig
    control.preflight(selection)
    _forge_generation_with_open_round(store, selection)

    ordered = _ordered_coverage(store)

    assert isinstance(ordered, BlockedLegacyOrderedOutcomeCoverage)
    assert "origin" in ordered.reason


def test_ordered_outcome_coverage_blocks_split_execution_identity(rig):
    store, _, _, selection, control = rig
    first = control.preflight(selection)
    second = control.preflight(replace(
        selection, request_id="preflight:conflicting-episode", expected=current(store, selection).revision,
    ))
    changed = replace(second, round=replace(second.round, binding=replace(
        second.round.binding, rejection=replace(
            second.round.binding.rejection, episode_id="rejections:conflicting",
        ),
    )))
    with store.transaction() as db:
        db.execute("UPDATE review_execution_rounds SET plan_json=? WHERE round_id=?",
                   (encode_workflow(changed), second.round.round_id))

    ordered = _ordered_coverage(store)

    assert isinstance(ordered, BlockedLegacyOrderedOutcomeCoverage)
    assert "crossed episode" in ordered.reason
    assert first.round.sequence == 1


def test_complete_success_classification_proves_plain_round_and_keeps_historical_unknown(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        _submission(db, 42)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _classification_observations(db)
        before = db.total_changes
        classified = classify_complete_legacy_review_successes(db, *observations)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    assert len(classified.results) == 2
    plain = next(result for result in classified.results if isinstance(result, ClassifiedLegacyPlainReviewSuccess))
    unknown = classified.unresolved[0]
    assert plain.proof.storage.outcome.round == plan.round
    assert plain.source == plain.proof.link.source
    assert isinstance(unknown, UnresolvedLegacyReviewSuccess)
    assert dict(unknown.source.command_fields)["command_id"] == "cmd-42"
    assert "lacks a round registration" in unknown.reason
    with pytest.raises(ValueError, match="unaccounted"):
        replace(classified, results=(plain,))


def test_complete_success_classification_proves_stale_and_ceiling_paths(rig):
    store, plan, command, _ = _stale_settlement(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _classification_observations(db)
        classified = classify_complete_legacy_review_successes(db, *observations)
        db.rollback()

    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    stale = next(result for result in classified.results if isinstance(result, ClassifiedLegacyStaleReviewSuccess))
    assert stale.proof.settlement.outcome.round == plan.round
    assert stale.source == stale.proof.stale_path.registered.command_source
    ceiling = next(result for result in classified.results if isinstance(result, ClassifiedLegacyCeilingReviewSuccess))
    assert ceiling.proof.decision.refusal is None
    assert ceiling.proof.storage.outcome.round == ceiling.proof.link.round
    assert classified.unresolved == ()
    assert len(classified.results) == 2


def test_refused_ceiling_without_durable_prior_state_remains_unresolved(rig):
    store, gateway, selection, control, plan, command = confirmed_escalation(rig, cause="ceiling")
    refused = workflow_for(store, gateway).settle_submission(command.command_id)
    assert isinstance(refused, WorkflowRejected)
    assert current(store, selection).gates == ()

    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
        assert len(classified.unresolved) == 1
        assert dict(classified.unresolved[0].source.command_fields)["command_id"] == str(command.command_id)
        assert "accepted gate transition" in classified.unresolved[0].reason
        ordered = verify_complete_legacy_ordered_outcome_coverage(db, classified)
        db.rollback()

    assert isinstance(ordered, BlockedLegacyOrderedOutcomeCoverage)
    assert "unresolved" in ordered.reason
    assert control.finalize_completed_round(plan.round.round_id).outcome.round == plan.round


def test_ordered_outcome_replay_includes_accepted_ceiling_and_stale_round(rig):
    store, plan, _, _ = _stale_settlement(rig)

    replayed = _ordered_replay(store)

    assert isinstance(replayed, ObservedLegacyOrderedCheckpointReplay)
    productive = [entry for entry in replayed.coverage.episodes[0].rounds
                  if isinstance(entry, review_activation.FinalizedLegacyProductiveRound)]
    assert len(productive) == 2
    assert productive[0].round == plan.round


@pytest.mark.parametrize("damage", (
    "transition", "transition_alias_namespace", "transition_alias_scope",
    "transition_alias_key", "transition_claim", "observation", "projection", "outbox_identity",
))
def test_ceiling_success_requires_independent_gate_sources(rig, damage):
    store, _, _, _, _, _, _ = accepted_generation_with_open_submission(rig)
    with store.transaction() as db:
        if damage == "transition":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-transition' "
                       "AND payload_json LIKE '%open_gate%'")
        elif damage.startswith("transition_alias_"):
            column = damage.removeprefix("transition_alias_")
            assert column in {"namespace", "scope", "key"}
            selected = ",".join(
                f"CAST({name} AS BLOB)" if name == column else name
                for name in ("namespace", "scope", "key")
            )
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       f"SELECT {selected},claimed_at,payload_json FROM dedupe_keys "
                       "WHERE namespace='workflow-transition' AND payload_json LIKE '%open_gate%'")
        elif damage == "transition_claim":
            db.execute("UPDATE dedupe_keys SET claimed_at='invalid' "
                       "WHERE namespace='workflow-transition' AND payload_json LIKE '%open_gate%'")
        elif damage == "observation":
            db.execute("UPDATE workflow_evidence SET payload_json='{}' WHERE kind='observation'")
        elif damage == "projection":
            db.execute("UPDATE lifecycle_commands SET payload_digest='changed' WHERE kind='relabel'")
        else:
            db.execute("UPDATE lifecycle_commands SET logical_key='changed' WHERE kind='relabel'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _classification_observations(db)
        classified = classify_complete_legacy_review_successes(db, *observations)
        db.rollback()

    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    assert any(isinstance(result, UnresolvedLegacyReviewSuccess) for result in classified.results)


def test_complete_success_classification_accounts_many_stale_review_commands(rig):
    store, snapshot, gateway, selection, control = rig
    old_plans = []
    for index in range(12):
        planned_selection = replace(
            selection, request_id=f"preflight:many-stale:{index}", expected=current(store, selection).revision,
        )
        old_plans.append(control.preflight(planned_selection))
    later_selection = replace(
        selection, request_id="preflight:many-stale:ceiling", expected=current(store, selection).revision,
    )
    _, _, _, _, _, escalation = confirmed_escalation(
        (store, snapshot, gateway, later_selection, control), cause="ceiling",
    )
    old_commands = []
    for index, plan in enumerate(old_plans):
        command = command_for(current(store, selection), body=f"approved historical {index}")
        control.register_submission(plan.round.round_id, current(store, selection).revision,
                                    command, ReviewSubmissionKind.COPIED)
        control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
        store.begin_command(command.command_id)
        store.record_effect(EffectReceipt(
            command.command_id, CONTROL_OWNER, store.now(), store.now(),
            EffectStatus.SUCCEEDED, remote_object_id=f"review:historical:{index}",
        ))
        old_commands.append(command)
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", CONTROL_OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(escalation.command_id), WorkflowCommitted)
    before = current(store, selection)
    assert isinstance(workflow.request_rearm(
        RequestRearm("rearm:many-stale", before.revision, before.gates[0].gate_id,
                     KeepGuidance(), expected_binding=before.binding),
        OperatorActor("operator", "test"),
    ), WorkflowCommitted)
    for command in old_commands:
        assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        db.rollback()
    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    stale = [result for result in classified.results if isinstance(result, ClassifiedLegacyStaleReviewSuccess)]
    assert {dict(result.source.command_fields)["command_id"] for result in stale} == {
        str(command.command_id) for command in old_commands
    }
    assert len([result for result in classified.results
                if isinstance(result, ClassifiedLegacyCeilingReviewSuccess)]) == 1
    assert classified.unresolved == ()


def test_complete_success_classification_keeps_legacy_stale_receipt_unknown(rig):
    store, plan, command, _ = _stale_settlement(rig)
    with store.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys "
                         "WHERE namespace='review-round-stale-productivity' AND key=?",
                         (plan.round.round_id,)).fetchone()
        recorded = decode_workflow(row["payload_json"], StaleReviewProductivitySettlement)
        db.execute("UPDATE dedupe_keys SET payload_json=? "
                   "WHERE namespace='review-round-stale-productivity' AND key=?",
                   (encode_workflow((recorded.facts, recorded.outcome)), plan.round.round_id))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        db.rollback()
    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    older = next(result for result in classified.unresolved
                 if dict(result.source.command_fields)["command_id"] == str(command.command_id))
    assert "legacy stale receipt" in older.reason


@pytest.mark.parametrize("damage", ["missing_effect", "effect_disagrees", "command_disagrees"])
def test_complete_success_classification_keeps_mismatched_success_claim_unresolved(rig, damage):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        if damage == "missing_effect":
            db.execute("DELETE FROM effect_receipts WHERE command_id=?", (str(command.command_id),))
        elif damage == "effect_disagrees":
            db.execute("UPDATE effect_receipts SET status='retryable_failure' WHERE command_id=?",
                       (str(command.command_id),))
        else:
            db.execute("UPDATE lifecycle_commands SET state='pending' WHERE command_id=?",
                       (str(command.command_id),))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        db.rollback()
    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    assert len(classified.unresolved) == 1
    assert "do not both confirm success" in classified.unresolved[0].reason


def test_complete_success_classification_blocks_changed_complete_source(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _classification_observations(db)
        db.rollback()
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00+00:00' "
                   "WHERE namespace='review-recorded-submission' AND key=?",
                   (str(command.command_id),))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *observations)
        db.rollback()
    assert isinstance(classified, BlockedLegacyReviewSuccessClassifications)


def test_complete_success_classification_rechecks_owner_lineage(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _classification_observations(db)
        db.rollback()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO retired_owners(owner_id,successor_owner_id,repository,role,retired_at) "
            "VALUES (?,?,?,?,?)",
            ("o/r:review:older", CONTROL_OWNER, "o/r", "review", "2026-09-28T00:00:00+00:00"),
        )
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *observations)
        db.rollback()
    assert isinstance(classified, BlockedLegacyReviewSuccessClassifications)
    assert "lineage changed" in classified.reason


def test_complete_success_classification_rejects_crossed_command_and_effect_owners(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    retired_owner = "o/r:review:retired"
    with store.transaction() as db:
        db.execute(
            "INSERT INTO retired_owners(owner_id,successor_owner_id,repository,role,retired_at) "
            "VALUES (?,?,?,?,?)",
            (retired_owner, CONTROL_OWNER, "o/r", "review", "2026-09-28T00:00:00+00:00"),
        )
        original = db.execute("SELECT * FROM effect_receipts WHERE command_id=?",
                              (str(command.command_id),)).fetchone()
        changed_receipt = EffectReceipt(
            command.command_id, retired_owner,
            review_activation._parse_stamp(original["started_at"]),
            review_activation._parse_stamp(original["completed_at"]),
            EffectStatus.SUCCEEDED, original["remote_object_id"],
            original["remote_revision"], original["error"],
        )
        changed_digest = review_activation._receipt_digest(changed_receipt)
        changed_id = review_activation._effect_receipt_id(str(command.command_id), changed_digest)
        db.execute("UPDATE effect_receipts SET executor_identity=?,receipt_digest=? WHERE command_id=?",
                   (retired_owner, changed_digest, str(command.command_id)))
        db.execute("UPDATE lifecycle_commands SET attempt_owner=? WHERE command_id=?",
                   (retired_owner, str(command.command_id)))
        row = db.execute("SELECT closure_json FROM review_execution_rounds WHERE round_id=?",
                         (plan.round.round_id,)).fetchone()
        facts = decode_workflow(row["closure_json"], ReviewRoundFacts)
        changed_facts = replace(facts, submissions=tuple(
            replace(item, result=review_activation.ReviewSubmissionConfirmed(changed_id))
            if item.plan.command_id == command.command_id else item
            for item in facts.submissions
        ))
        outcome = review_activation.finalize_review_round(changed_facts).outcome
        db.execute("UPDATE review_execution_rounds SET closure_json=?,outcome_json=? WHERE round_id=?",
                   (encode_workflow(changed_facts), encode_workflow(outcome), plan.round.round_id))
        db.execute("UPDATE dedupe_keys SET payload_json=? "
                   "WHERE namespace='review-round-outcome' AND key=?",
                   (encode_workflow((changed_facts, outcome)), plan.round.round_id))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        db.rollback()
    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    assert len(classified.unresolved) == 1
    assert "plain recorded baseline" in classified.unresolved[0].reason


def test_complete_success_classification_rejects_shared_remote_review_identity(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        remote_id = db.execute("SELECT remote_object_id FROM effect_receipts WHERE command_id=?",
                               (str(command.command_id),)).fetchone()["remote_object_id"]
        _submission(db, 42)
        db.execute("UPDATE effect_receipts SET remote_object_id=? WHERE command_id='cmd-42'", (remote_id,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        db.rollback()
    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    assert not any(isinstance(result, ClassifiedLegacyPlainReviewSuccess) for result in classified.results)
    assert any("shared by another command" in result.reason for result in classified.unresolved)


def _match_recorded_escalation(store, command_id):
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observed = read_complete_legacy_review_submissions(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewSubmissions)
        source = next(item for item in observed.submissions
                      if dict(item.command_fields)["command_id"] == str(command_id))
        lineage = read_legacy_review_owner_lineage(db, "o/r", CONTROL_OWNER)
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        before = db.total_changes
        result = match_legacy_escalated_review_hold_plan(db, source, lineage)
        assert db.total_changes == before
        db.rollback()
    return result


def test_recorded_escalation_matches_accepted_hold_plan_without_claiming_delivery(rig):
    store, gateway, _, _, _, command = confirmed_escalation(rig)
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)

    result = _match_recorded_escalation(store, command.command_id)

    assert isinstance(result, MatchedLegacyEscalatedReviewHoldPlan)
    assert result.snapshot.episode.continuation.source.command_id == command.command_id
    assert result.projection.payload["add"] == Label.NEEDS_HUMAN_REVIEW.value
    assert len(result.source_digest) == 64
    with store.transaction() as db:
        effect = db.execute("SELECT 1 FROM effect_receipts WHERE command_id=?",
                            (str(result.projection.command_id),)).fetchone()
    assert effect is None


@pytest.mark.parametrize("damage", [
    "observation", "transition", "projection", "projection_digest", "projection_identity",
])
def test_recorded_escalation_rejects_changed_hold_sources(rig, damage):
    store, gateway, _, _, _, command = confirmed_escalation(rig)
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    with store.transaction() as db:
        if damage == "observation":
            row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-transition' "
                             "AND scope='o/r' ORDER BY rowid DESC LIMIT 1").fetchone()
            accepted = json.loads(row["payload_json"])
            evidence_id = decode_workflow(accepted["audit"], ObservedTransition).evidence_ids[2]
            db.execute("UPDATE workflow_evidence SET payload_json='{}' WHERE evidence_id=?", (evidence_id,))
        elif damage == "transition":
            row = db.execute("SELECT key,payload_json FROM dedupe_keys WHERE namespace='workflow-transition' "
                             "AND scope='o/r' ORDER BY rowid DESC LIMIT 1").fetchone()
            accepted = json.loads(row["payload_json"])
            accepted["commands"][0]["payload"]["add"] = Label.AI_PR_OPEN.value
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='workflow-transition' "
                       "AND scope='o/r' AND key=?", (json.dumps(accepted), row["key"]))
        elif damage == "projection":
            db.execute("UPDATE lifecycle_commands SET payload_json='{}' WHERE kind='relabel' "
                       "AND repository='o/r'")
        elif damage == "projection_digest":
            db.execute("UPDATE lifecycle_commands SET payload_digest=? WHERE kind='relabel' "
                       "AND repository='o/r'", ("0" * 64,))
        else:
            db.execute("UPDATE lifecycle_commands SET logical_key='wrong',occurrence=7 "
                       "WHERE kind='relabel' AND repository='o/r'")

    result = _match_recorded_escalation(store, command.command_id)

    assert isinstance(result, UnmatchedLegacyEscalatedReviewHoldPlan)


def test_recorded_escalation_rechecks_owner_lineage_before_matching(rig):
    store, gateway, _, _, _, command = confirmed_escalation(rig)
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    with store.transaction() as db:
        observed = read_complete_legacy_review_submissions(db, "o/r")
        assert isinstance(observed, CompleteLegacyReviewSubmissions)
        source = next(item for item in observed.submissions
                      if dict(item.command_fields)["command_id"] == str(command.command_id))
        lineage = read_legacy_review_owner_lineage(db, "o/r", CONTROL_OWNER)
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        _retired_review_owner(db, "o/r:review:older", CONTROL_OWNER)
        result = match_legacy_escalated_review_hold_plan(db, source, lineage)

    assert isinstance(result, UnmatchedLegacyEscalatedReviewHoldPlan)
    assert "lineage changed" in result.reason


def test_complete_success_classification_proves_escalated_verdict_and_hold_plan(rig):
    store, gateway, _, control, plan, command = confirmed_escalation(rig)
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        db.rollback()

    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    assert len(classified.results) == 1
    result = classified.results[0]
    assert isinstance(result, ClassifiedLegacyEscalatedReviewSuccess)
    assert result.proof.storage.outcome.round == plan.round
    assert result.proof.hold_plan.projection.payload["add"] == Label.NEEDS_HUMAN_REVIEW.value
    assert not classified.unresolved


def test_complete_success_classification_keeps_corrupt_escalation_hold_unresolved(rig):
    store, gateway, _, control, plan, command = confirmed_escalation(rig)
    assert isinstance(workflow_for(store, gateway).record_review(command.command_id), WorkflowCommitted)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        db.execute("UPDATE lifecycle_commands SET payload_digest=? WHERE kind='relabel' "
                   "AND repository='o/r'", ("0" * 64,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        classified = classify_complete_legacy_review_successes(db, *_classification_observations(db))
        db.rollback()

    assert isinstance(classified, CompleteLegacyReviewSuccessClassifications)
    assert len(classified.unresolved) == 1
    assert "corrupt payload digest" in classified.unresolved[0].reason


def test_stale_generation_join_proves_real_accepted_crossing_without_writes(rig):
    store, plan, command, _ = _stale_settlement(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        storage, generation = _stale_generation_sources(db)
        before = db.total_changes
        matched = match_legacy_stale_productivity_generation_sources(
            db, storage, generation, str(command.command_id),
        )
        assert db.total_changes == before
        db.rollback()

    assert isinstance(matched, MatchedLegacyStaleProductivityGenerationSources)
    assert matched.settlement.outcome.round == plan.round
    assert len(matched.boundaries) == 1
    assert matched.boundaries[0].boundary.to_revision == matched.settlement.settled_revision
    assert len(matched.source_digest) == 64


def test_stale_generation_join_uses_revisions_not_sql_rowid_order(rig):
    store, _, command, _ = _stale_settlement(rig)
    with store.transaction() as db:
        for namespace, new_rowid in (("review-execution-generation", -1), ("workflow-request", 0)):
            row = db.execute(
                "SELECT namespace,scope,key,claimed_at,reusable_after,payload_json "
                "FROM dedupe_keys WHERE namespace=? AND scope='o/r' "
                "AND (namespace!='workflow-request' OR key='rearm:with-older-submission')",
                (namespace,),
            ).fetchone()
            assert row is not None
            db.execute("DELETE FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                       (row["namespace"], row["scope"], row["key"]))
            db.execute("INSERT INTO dedupe_keys(rowid,namespace,scope,key,claimed_at,reusable_after,payload_json) "
                       "VALUES (?,?,?,?,?,?,?)", (new_rowid, *tuple(row)))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        storage, generation = _stale_generation_sources(db)
        result = match_legacy_stale_productivity_generation_sources(
            db, storage, generation, str(command.command_id),
        )
        db.rollback()
    assert isinstance(result, MatchedLegacyStaleProductivityGenerationSources)
    assert result.boundaries[0].receipt_source.source_rowid == -1
    assert result.boundaries[0].accepted_rowid == 0


def test_stale_generation_join_ignores_large_unrelated_dedupe_history(rig):
    store, _, command, _ = _stale_settlement(rig)
    with store.transaction() as db:
        db.executemany(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
            (("workflow-request" if index % 2 else "unrelated-history",
              "other/repo", f"event:{index}", "now", "{}")
             for index in range(1500)),
        )
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        storage, generation = _stale_generation_sources(db)
        result = match_legacy_stale_productivity_generation_sources(
            db, storage, generation, str(command.command_id),
        )
        db.rollback()
    assert isinstance(result, MatchedLegacyStaleProductivityGenerationSources)


def test_stale_generation_join_requires_each_boundary_in_two_advance_crossing(rig):
    store, _, selection, _, plan, command, workflow = accepted_generation_with_open_submission(rig)
    first = current(store, selection)
    actor = OperatorActor("operator", "test")
    assert isinstance(workflow.cancel_rearm(
        CancelRearm("cancel:stale-crossing", first.revision, "rearm:with-older-submission", "stop",
                    expected_binding=first.binding), actor,
    ), WorkflowCommitted)
    stopped = current(store, selection)
    assert isinstance(workflow.continue_rearm(
        ContinueGrantedRearm("rearm:stale-crossing", stopped.revision, "rearm:with-older-submission",
                             expected_binding=stopped.binding), actor,
    ), WorkflowCommitted)
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        storage, generation = _stale_generation_sources(db)
        matched = match_legacy_stale_productivity_generation_sources(
            db, storage, generation, str(command.command_id),
        )
        db.rollback()
    assert isinstance(matched, MatchedLegacyStaleProductivityGenerationSources)
    original_generation = plan.round.binding.rejection.fence.generation
    assert [item.boundary.from_revision.fence.generation for item in matched.boundaries] == [
        original_generation, original_generation + 1,
    ]
    assert matched.settlement.outcome.round == plan.round

    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-generation' AND key=?",
                   (matched.boundaries[0].boundary.receipt_id,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        storage, generation = _stale_generation_sources(db)
        missing = match_legacy_stale_productivity_generation_sources(
            db, storage, generation, str(command.command_id),
        )
        db.rollback()
    assert isinstance(missing, UnmatchedLegacyStaleProductivityGenerationSources)
    assert "generation boundary" in missing.reason


@pytest.mark.parametrize("damage", [
    "legacy_receipt", "wrong_digest", "future_revision", "accepted_removed",
    "accepted_payload", "accepted_alias", "accepted_blob_alias", "generation_removed",
    "generation_payload",
])
def test_stale_generation_join_blocks_missing_changed_and_legacy_sources(rig, damage):
    store, plan, command, _ = _stale_settlement(rig)
    with store.transaction() as db:
        stale = db.execute("SELECT payload_json FROM dedupe_keys "
                           "WHERE namespace='review-round-stale-productivity' AND key=?",
                           (plan.round.round_id,)).fetchone()
        captured = decode_workflow(stale["payload_json"], StaleReviewProductivitySettlement)
        if damage in {"legacy_receipt", "wrong_digest", "future_revision"}:
            if damage == "legacy_receipt":
                changed = (captured.facts, captured.outcome)
            elif damage == "wrong_digest":
                changed = replace(captured, crossing_digest="0" * 64)
            else:
                changed = replace(captured, settled_revision=replace(
                    captured.settled_revision, version=captured.settled_revision.version + 1,
                ))
            db.execute("UPDATE dedupe_keys SET payload_json=? "
                       "WHERE namespace='review-round-stale-productivity' AND key=?",
                       (encode_workflow(changed), plan.round.round_id))
        elif damage == "accepted_removed":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-request' "
                       "AND key='rearm:with-older-submission'")
        elif damage == "accepted_payload":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-request' "
                       "AND key='rearm:with-older-submission'")
        elif damage == "accepted_alias":
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,?,?,?,?)",
                       ("workflow-request", "O/R", "rearm:with-older-submission", "now", "{}"))
        elif damage == "accepted_blob_alias":
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "VALUES (?,?,?,?,?)",
                       (b"workflow-request", "o/r", b"rearm:with-older-submission", "now", "{}"))
        elif damage == "generation_removed":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-generation'")
        else:
            db.execute("UPDATE dedupe_keys SET payload_json='{}' "
                       "WHERE namespace='review-execution-generation'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        storage, generation = _stale_generation_sources(db)
        before = db.total_changes
        result = match_legacy_stale_productivity_generation_sources(
            db, storage, generation, str(command.command_id),
        )
        assert db.total_changes == before
        db.rollback()
    assert isinstance(result, UnmatchedLegacyStaleProductivityGenerationSources)


def test_stale_generation_join_rechecks_both_complete_sets(rig):
    store, plan, command, _ = _stale_settlement(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        storage, generation = _stale_generation_sources(db)
        db.rollback()
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00+00:00' "
                   "WHERE namespace='review-round-stale-productivity' AND key=?",
                   (plan.round.round_id,))
        db.execute("UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00+00:00' "
                   "WHERE namespace='review-execution-generation'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = match_legacy_stale_productivity_generation_sources(
            db, storage, generation, str(command.command_id),
        )
        db.rollback()
    assert isinstance(result, UnmatchedLegacyStaleProductivityGenerationSources)


def test_older_successful_submission_can_settle_after_accepted_generation_advance(rig):
    store, _, selection, _, old, command, workflow = accepted_generation_with_open_submission(rig)
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        generation = read_complete_legacy_review_generation_receipts(db, "o/r")
        stale = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-stale-productivity' "
                           "AND key=?", (old.round.round_id,)).fetchone()
        db.rollback()
    assert isinstance(generation, CompleteLegacyReviewGenerationReceipts)
    assert len(generation.receipts) == 1 and stale is not None
    captured = decode_workflow(stale["payload_json"], StaleReviewProductivitySettlement)
    boundary = decode_workflow(
        dict(generation.receipts[0].fields)["payload_json"],
        ReviewExecutionTransitionProofSource,
    ).boundary
    assert captured.settled_revision == current(store, selection).revision
    assert captured.facts.plan.round == old.round
    assert captured.crossing_digest == review_generation_chain_digest((boundary.receipt_id,))


@pytest.mark.parametrize("damage", ["crossing_digest", "future_revision",
                                    "preboundary_revision", "legacy_receipt"])
def test_stale_settlement_hot_read_checks_new_witness_and_retains_legacy_reads(rig, damage):
    store, _, selection, _, old, command, workflow = accepted_generation_with_open_submission(rig)
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)
    boundary_version = current(store, selection).execution.boundaries[-1].to_revision.version
    with store.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-stale-productivity' "
                         "AND key=?", (old.round.round_id,)).fetchone()
        captured = decode_workflow(row["payload_json"], StaleReviewProductivitySettlement)
        if damage == "crossing_digest":
            changed = replace(captured, crossing_digest="0" * 64)
        elif damage == "future_revision":
            changed = replace(captured, settled_revision=replace(
                captured.settled_revision, version=captured.settled_revision.version + 1,
            ))
        elif damage == "preboundary_revision":
            changed = replace(captured, settled_revision=replace(
                captured.settled_revision, version=boundary_version - 1,
            ))
        else:
            changed = (captured.facts, captured.outcome)
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-stale-productivity' "
                   "AND key=?", (encode_workflow(changed), old.round.round_id))
    if damage == "legacy_receipt":
        assert current(store, selection).execution.state.productive_sequence > 0
    else:
        with pytest.raises(WorkflowAuthorityError, match="stale productive review round"):
            current(store, selection)


def test_stale_successful_submission_has_sealed_round_without_recorded_baseline(rig):
    store, plan, command, complete = _stale_settlement(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        before = db.total_changes
        proof = verify_legacy_stale_productivity_receipt_path(db, complete, str(command.command_id))
        assert db.total_changes == before
        db.rollback()

    assert isinstance(proof, VerifiedLegacyStaleProductivityReceiptPath)
    assert proof.sealed.outcome.round == plan.round
    assert proof.registered.registration.submission.command_id == command.command_id
    assert complete.recorded_markers == ()
    assert len(proof.source_digest) == 64
    with pytest.raises(ValueError, match="lacks a confirmed effect"):
        replace(proof, registered=replace(
            proof.registered, command_source=replace(
                proof.registered.command_source, receipt_fields=None,
            ),
        ))


@pytest.mark.parametrize("damage", [
    "stale_removed", "stale_payload", "recorded_marker", "effect_changed",
    "effect_missing", "round_outcome", "superseded_receipt", "ceiling_receipt",
])
def test_stale_successful_submission_rejects_changed_or_conflicting_sources(rig, damage):
    store, plan, command, complete = _stale_settlement(rig)
    with store.transaction() as db:
        if damage == "stale_removed":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-stale-productivity' AND key=?",
                       (plan.round.round_id,))
        elif damage == "stale_payload":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' "
                       "WHERE namespace='review-round-stale-productivity' AND key=?",
                       (plan.round.round_id,))
        elif damage == "recorded_marker":
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       ("review-recorded-submission", "o/r", str(command.command_id),
                        "2026-09-28T00:00:00+00:00", "{}"))
        elif damage == "effect_changed":
            db.execute("UPDATE effect_receipts SET remote_object_id='changed' WHERE command_id=?",
                       (str(command.command_id),))
        elif damage == "effect_missing":
            db.execute("DELETE FROM effect_receipts WHERE command_id=?", (str(command.command_id),))
        elif damage == "round_outcome":
            db.execute("UPDATE review_execution_rounds SET outcome_json='{}' WHERE round_id=?",
                       (plan.round.round_id,))
        else:
            namespace = ("review-round-superseded-productivity" if damage == "superseded_receipt"
                         else "review-round-ceiling-productivity")
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       (namespace, "o/r", plan.round.round_id,
                        "2026-09-28T00:00:00+00:00", "{}"))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = verify_legacy_stale_productivity_receipt_path(db, complete, str(command.command_id))
        db.rollback()

    assert isinstance(result, UnverifiedLegacyStaleProductivityReceiptPath)


def test_stale_receipt_path_rejects_freshly_observed_claim_time_mismatch(rig):
    store, plan, command, _ = _stale_settlement(rig)
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET claimed_at='2026-09-29T00:00:00+00:00' "
                   "WHERE namespace='review-round-stale-productivity' AND key=?",
                   (plan.round.round_id,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        complete = verify_complete_legacy_submission_settlement_storage(
            db, *_complete_settlement_observations(db),
        )
        assert isinstance(complete, CompleteLegacySubmissionSettlementStorage)
        result = verify_legacy_stale_productivity_receipt_path(db, complete, str(command.command_id))
        db.rollback()

    assert isinstance(result, UnverifiedLegacyStaleProductivityReceiptPath)


@pytest.mark.parametrize("damage", ["wrong_pr", "deep_json"])
def test_complete_settlement_storage_rejects_self_consistent_invalid_command_payload(rig, damage):
    store, _, _, _, plan, command = confirmed_copy(rig)
    with store.transaction() as db:
        row = db.execute("SELECT payload_json,workflow_binding_json FROM lifecycle_commands WHERE command_id=?",
                         (str(command.command_id),)).fetchone()
        if damage == "wrong_pr":
            payload = json.loads(row["payload_json"])
            payload["pr"] = plan.round.binding.rejection.fence.key.target.number + 1
            changed_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        else:
            changed_payload = row["payload_json"][:-1] + ',"extra":' + '[' * 10_000 + '0' + ']' * 10_000 + '}'
        changed_digest = _encoded_payload_digest(changed_payload, row["workflow_binding_json"])
        db.execute("UPDATE lifecycle_commands SET payload_json=?,payload_digest=? WHERE command_id=?",
                   (changed_payload, changed_digest, str(command.command_id)))
        source = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-submission' "
                            "AND key=?", (str(command.command_id),)).fetchone()
        registration = decode_workflow(source["payload_json"], review_activation.ReviewSubmissionRegistration)
        changed_registration = encode_workflow(replace(registration, command_digest=changed_digest))
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace IN "
                   "('review-round-submission','review-round-submission-index') AND scope='o/r'",
                   (changed_registration,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = verify_complete_legacy_submission_settlement_storage(
            db, *_complete_settlement_observations(db),
        )
        db.rollback()

    assert isinstance(result, BlockedLegacySubmissionSettlementStorage)


def test_stale_receipt_path_requires_named_remote_review_even_with_consistent_effect(rig):
    store, plan, command, _ = _stale_settlement(rig)
    with store.transaction() as db:
        effect = db.execute("SELECT * FROM effect_receipts WHERE command_id=?",
                            (str(command.command_id),)).fetchone()
        missing_remote = EffectReceipt(
            command.command_id, effect["executor_identity"],
            review_activation._parse_stamp(effect["started_at"]),
            review_activation._parse_stamp(effect["completed_at"]),
            EffectStatus.SUCCEEDED,
        )
        digest = review_activation._receipt_digest(missing_remote)
        receipt_id = review_activation._effect_receipt_id(str(command.command_id), digest)
        db.execute("UPDATE effect_receipts SET remote_object_id=NULL,receipt_digest=? WHERE command_id=?",
                   (digest, str(command.command_id)))
        row = db.execute("SELECT closure_json FROM review_execution_rounds WHERE round_id=?",
                         (plan.round.round_id,)).fetchone()
        facts = decode_workflow(row["closure_json"], ReviewRoundFacts)
        changed_facts = replace(facts, submissions=tuple(
            replace(item, result=review_activation.ReviewSubmissionConfirmed(receipt_id))
            if item.plan.command_id == command.command_id else item
            for item in facts.submissions
        ))
        final = review_activation.finalize_review_round(changed_facts)
        db.execute("UPDATE review_execution_rounds SET closure_json=?,outcome_json=? WHERE round_id=?",
                   (encode_workflow(changed_facts), encode_workflow(final.outcome), plan.round.round_id))
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace IN "
                   "('review-round-outcome','review-round-stale-productivity') AND scope='o/r' AND key=?",
                   (encode_workflow((changed_facts, final.outcome)), plan.round.round_id))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        complete = verify_complete_legacy_submission_settlement_storage(
            db, *_complete_settlement_observations(db),
        )
        assert isinstance(complete, CompleteLegacySubmissionSettlementStorage)
        result = verify_legacy_stale_productivity_receipt_path(db, complete, str(command.command_id))
        db.rollback()

    assert isinstance(result, UnverifiedLegacyStaleProductivityReceiptPath)


def test_stale_receipt_path_rejects_a_shared_remote_review_id(rig):
    store, _, command, _ = _stale_settlement(rig)
    with store.transaction() as db:
        remote = db.execute("SELECT remote_object_id FROM effect_receipts WHERE command_id=?",
                            (str(command.command_id),)).fetchone()["remote_object_id"]
        _submission(db, 42)
        db.execute("UPDATE effect_receipts SET remote_object_id=? WHERE command_id='cmd-42'", (remote,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        complete = verify_complete_legacy_submission_settlement_storage(
            db, *_complete_settlement_observations(db),
        )
        assert isinstance(complete, CompleteLegacySubmissionSettlementStorage)
        result = verify_legacy_stale_productivity_receipt_path(db, complete, str(command.command_id))
        db.rollback()

    assert isinstance(result, UnverifiedLegacyStaleProductivityReceiptPath)


def test_complete_settlement_storage_accounts_registered_and_historical_commands(rig):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        _submission(db, 42, receipt=False)
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   ("review-recorded-submission", "o/r", "cmd-42", "2026-09-28T00:00:00+00:00", "{}"))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _complete_settlement_observations(db)
        before = db.total_changes
        result = verify_complete_legacy_submission_settlement_storage(db, *observations)
        assert db.total_changes == before
        db.rollback()

    assert isinstance(result, CompleteLegacySubmissionSettlementStorage)
    assert [dict(proof.command_source.command_fields)["command_id"] for proof in result.registered] == [
        str(command.command_id),
    ]
    assert [dict(source.command_fields)["command_id"] for source in result.unregistered] == ["cmd-42"]
    assert {dict(source.fields)["key"] for source in result.recorded_markers} == {
        str(command.command_id), "cmd-42",
    }
    assert result.registered[0].plan.round == plan.round
    pair = result.registered[0]
    forged_index = replace(pair.index_source, fields=tuple(
        (name, "orphan-index" if name == "key" else value) for name, value in pair.index_source.fields
    ))
    with pytest.raises(ValueError, match="contradictory"):
        replace(pair, index_source=forged_index)
    forged_registration = replace(pair.registration_source, fields=tuple(
        (name, "{}" if name == "payload_json" else value) for name, value in pair.registration_source.fields
    ))
    forged_index = replace(pair.index_source, fields=tuple(
        (name, "{}" if name == "payload_json" else value) for name, value in pair.index_source.fields
    ))
    with pytest.raises(ValueError, match="contradictory"):
        replace(pair, registration_source=forged_registration, index_source=forged_index)
    changed_plan = replace(pair.plan, displayed_round=pair.plan.displayed_round + 1)
    changed_round = replace(pair.round_source, fields=tuple(
        (name, encode_workflow(changed_plan) if name == "plan_json" else value)
        for name, value in pair.round_source.fields
    ))
    changed_rounds = replace(result.round_storage.rounds, rounds=(changed_round,))
    changed_seal = replace(result.round_storage.sealed[0], round_source=changed_round)
    changed_storage = replace(result.round_storage, rounds=changed_rounds, sealed=(changed_seal,))
    changed_pair = replace(pair, round_source=changed_round, plan=changed_plan)
    with pytest.raises(ValueError, match="round plan"):
        replace(result, round_storage=changed_storage, round_plans=((changed_round, changed_plan),),
                registered=(changed_pair,))


@pytest.mark.parametrize("damage", [
    "orphan_registration", "orphan_index", "orphan_marker", "missing_registration",
    "index_payload", "index_claim", "index_reusable", "registration_claim",
])
def test_complete_settlement_storage_blocks_orphaned_or_contradictory_receipts(rig, damage):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        if damage == "orphan_registration":
            row = db.execute("SELECT payload_json,claimed_at FROM dedupe_keys "
                             "WHERE namespace='review-round-submission'").fetchone()
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       ("review-round-submission", "o/r", "missing-command", row["claimed_at"], row["payload_json"]))
        elif damage == "orphan_index":
            row = db.execute("SELECT payload_json,claimed_at FROM dedupe_keys "
                             "WHERE namespace='review-round-submission-index'").fetchone()
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       ("review-round-submission-index", "o/r", "orphan-index", row["claimed_at"], row["payload_json"]))
        elif damage == "orphan_marker":
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                       ("review-recorded-submission", "o/r", "missing-command",
                        "2026-09-28T00:00:00+00:00", "{}"))
        elif damage == "missing_registration":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-submission'")
        elif damage == "index_payload":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' "
                       "WHERE namespace='review-round-submission-index'")
        elif damage == "index_claim":
            db.execute("UPDATE dedupe_keys SET claimed_at='2026-09-28T00:00:00+00:00' "
                       "WHERE namespace='review-round-submission-index'")
        elif damage == "index_reusable":
            db.execute("UPDATE dedupe_keys SET reusable_after='2026-09-29T00:00:00+00:00' "
                       "WHERE namespace='review-round-submission-index'")
        else:
            db.execute("UPDATE dedupe_keys SET claimed_at='invalid' "
                       "WHERE namespace='review-round-submission'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _complete_settlement_observations(db)
        result = verify_complete_legacy_submission_settlement_storage(db, *observations)
        db.rollback()

    assert isinstance(result, BlockedLegacySubmissionSettlementStorage)


def test_complete_settlement_storage_blocks_round_plan_without_its_registration(rig):
    store, _, _, control, plan, command = confirmed_copy(rig)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-submission'")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _complete_settlement_observations(db)
        result = verify_complete_legacy_submission_settlement_storage(db, *observations)
        db.rollback()

    assert isinstance(result, BlockedLegacySubmissionSettlementStorage)
    assert "receipt" in result.reason or "planned" in result.reason


def test_complete_settlement_storage_blocks_registration_missing_from_open_plan(rig):
    store, _, _, _, plan, _ = confirmed_copy(rig)
    with store.transaction() as db:
        row = db.execute("SELECT plan_json FROM review_execution_rounds WHERE round_id=?",
                         (plan.round.round_id,)).fetchone()
        stored = decode_workflow(row["plan_json"], ReviewRoundPlan)
        db.execute("UPDATE review_execution_rounds SET plan_json=? WHERE round_id=?",
                   (encode_workflow(replace(stored, submissions=())), plan.round.round_id))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _complete_settlement_observations(db)
        result = verify_complete_legacy_submission_settlement_storage(db, *observations)
        db.rollback()

    assert isinstance(result, BlockedLegacySubmissionSettlementStorage)


@pytest.mark.parametrize("changed", ["command", "receipt", "round"])
def test_complete_settlement_storage_rechecks_every_observed_sql_set(rig, changed):
    store, _, _, _, plan, command = confirmed_copy(rig)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        observations = _complete_settlement_observations(db)
        db.rollback()
    with store.transaction() as db:
        if changed == "command":
            db.execute("UPDATE lifecycle_commands SET payload_json='{}' WHERE command_id=?",
                       (str(command.command_id),))
        elif changed == "receipt":
            db.execute("UPDATE dedupe_keys SET claimed_at='changed' "
                       "WHERE namespace='review-round-submission'")
        else:
            db.execute("UPDATE review_execution_rounds SET plan_json='{}' WHERE round_id=?",
                       (plan.round.round_id,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = verify_complete_legacy_submission_settlement_storage(db, *observations)
        db.rollback()

    assert isinstance(result, BlockedLegacySubmissionSettlementStorage)


@pytest.mark.parametrize("damage", [
    "recording_marker", "recording_reusable", "recording_claimed", "transition_reusable",
    "transition_claimed", "effect_receipt", "round_outcome", "stale_productivity",
])
def test_plain_productive_round_rejects_changed_bound_sources(rig, damage):
    store, gateway, _, control, plan, command = confirmed_copy(rig)
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(plan.round.round_id)
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        page = read_legacy_review_submission_page(db, "o/r")
        source = next(entry for entry in page.entries if isinstance(entry, LegacyReviewSubmissionSource)
                      and dict(entry.command_fields)["command_id"] == str(command.command_id))
        lineage = read_legacy_review_owner_lineage(db, "o/r", CONTROL_OWNER)
        rounds = read_complete_legacy_review_rounds(db, "o/r")
        receipts = read_complete_legacy_review_outcome_receipts(db, "o/r")
        assert isinstance(lineage, LegacyReviewOwnerLineage)
        assert isinstance(rounds, CompleteLegacyReviewRounds)
        assert isinstance(receipts, CompleteLegacyReviewOutcomeReceipts)
        storage = verify_complete_legacy_round_outcome_storage(db, rounds, receipts)
        assert isinstance(storage, CompleteLegacyRoundOutcomeStorage)
        db.rollback()
    with store.transaction() as db:
        if damage == "recording_marker":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-recorded-submission'")
        elif damage in {"recording_reusable", "transition_reusable"}:
            namespace = "review-recorded-submission" if damage == "recording_reusable" else "workflow-transition"
            db.execute("UPDATE dedupe_keys SET reusable_after=? WHERE namespace=? AND scope=?",
                       ("2026-09-29T00:00:00+00:00", namespace, "o/r"))
        elif damage in {"recording_claimed", "transition_claimed"}:
            namespace = "review-recorded-submission" if damage == "recording_claimed" else "workflow-transition"
            db.execute("UPDATE dedupe_keys SET claimed_at=? WHERE namespace=? AND scope=?",
                       ("not-a-timestamp", namespace, "o/r"))
        elif damage == "effect_receipt":
            db.execute("UPDATE effect_receipts SET remote_object_id='changed' WHERE command_id=?",
                       (str(command.command_id),))
        elif damage == "stale_productivity":
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace='review-round-outcome' AND key=?",
                (plan.round.round_id,),
            ).fetchone()
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                ("review-round-stale-productivity", "o/r", plan.round.round_id,
                 "2026-09-28T00:00:00+00:00", row["payload_json"]),
            )
        else:
            db.execute("UPDATE review_execution_rounds SET outcome_json='{}' WHERE round_id=?",
                       (plan.round.round_id,))
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        result = verify_legacy_plain_productive_round(db, storage, source, lineage)
        db.rollback()

    assert isinstance(result, UnverifiedLegacyPlainProductiveRound)


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


def test_complete_submission_inventory_covers_exact_commands_and_rechecks_sql(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission(db, 1, rowid=-3)
        _submission(db, 2, receipt=False, rowid=0)
        _submission(db, 3, repository="other/r")
        _submission(db, 4, kind="relabel")
        db.execute("BEGIN")
        before = db.total_changes
        observed = read_complete_legacy_review_submissions(db, "o/r")
        assert db.total_changes == before
        assert isinstance(observed, CompleteLegacyReviewSubmissions)
        same = revalidate_complete_legacy_review_submissions(db, observed)
        assert same == observed
        partial = CompleteLegacyReviewSubmissions("o/r", observed.submissions[:1])
        assert isinstance(revalidate_complete_legacy_review_submissions(db, partial), BlockedLegacyReviewSubmissions)
        with pytest.raises(ValueError, match="unique command"):
            CompleteLegacyReviewSubmissions("o/r", (observed.submissions[0], observed.submissions[0]))
        db.rollback()
        db.execute("UPDATE lifecycle_commands SET payload_json='changed' WHERE command_id='cmd-1'")
        db.execute("BEGIN")
        changed = revalidate_complete_legacy_review_submissions(db, observed)
        db.rollback()

    assert [dict(source.command_fields)["command_id"] for source in observed.submissions] == ["cmd-1", "cmd-2"]
    assert isinstance(changed, BlockedLegacyReviewSubmissions)


@pytest.mark.parametrize("alias", ["repository", "kind"])
def test_complete_submission_inventory_blocks_matching_blob_alias(tmp_path, alias):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission(db, 1, repository=b"o/r" if alias == "repository" else "o/r",
                    kind=b"submit_review" if alias == "kind" else "submit_review", receipt=False)
        db.execute("BEGIN")
        page = read_legacy_review_submission_page(db, "o/r")
        result = read_complete_legacy_review_submissions(db, "o/r")
        db.rollback()

    assert isinstance(page.entries[0], UnreadableLegacyReviewSubmission)
    assert isinstance(result, BlockedLegacyReviewSubmissions)


def test_complete_submission_inventory_blocks_foreign_row_scan_excess(tmp_path, monkeypatch):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        for number in range(3):
            _submission(db, number, repository="other/r", receipt=False)
        db.execute("BEGIN")
        monkeypatch.setattr(review_activation, "MAX_LEGACY_SUBMISSION_SCAN_ROWS", 2)
        result = read_complete_legacy_review_submissions(db, "o/r")
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewSubmissions)
    assert "total scan bound" in result.reason


def test_complete_submission_inventory_blocks_unreadable_source(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission(db, 1)
        db.execute("UPDATE lifecycle_commands SET payload_json=? WHERE command_id='cmd-1'", ("x" * 65537,))
        db.execute("BEGIN")
        result = read_complete_legacy_review_submissions(db, "o/r")
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewSubmissions)


@pytest.mark.parametrize("bound", ["rows", "bytes"])
def test_complete_submission_inventory_blocks_total_source_excess(tmp_path, monkeypatch, bound):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission(db, 1)
        _submission(db, 2)
        db.execute("BEGIN")
        if bound == "rows":
            monkeypatch.setattr(review_activation, "MAX_LEGACY_SUBMISSION_INVENTORY_ROWS", 1)
        else:
            page = read_legacy_review_submission_page(db, "o/r")
            assert isinstance(page.entries[0], LegacyReviewSubmissionSource)
            monkeypatch.setattr(review_activation, "MAX_LEGACY_SUBMISSION_INVENTORY_BYTES",
                                page.entries[0].source_bytes - 1)
        result = read_complete_legacy_review_submissions(db, "o/r")
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewSubmissions)
    assert "total bound" in result.reason


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


def test_complete_submission_receipt_inventory_rechecks_exact_sql_set(tmp_path):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission_receipt(db, 1, namespace="review-round-submission", rowid=-3)
        _submission_receipt(db, 2, namespace="review-round-submission-index", rowid=0)
        _submission_receipt(db, 3, namespace="review-recorded-submission")
        _submission_receipt(db, 4, namespace="other")
        _submission_receipt(db, 5, scope="other/r")
        db.execute("BEGIN")
        before = db.total_changes
        observed = read_complete_legacy_review_submission_receipts(db, "o/r")
        assert db.total_changes == before
        assert isinstance(observed, CompleteLegacyReviewSubmissionReceipts)
        assert revalidate_complete_legacy_review_submission_receipts(db, observed) == observed
        partial = CompleteLegacyReviewSubmissionReceipts("o/r", observed.receipts[:1])
        assert isinstance(revalidate_complete_legacy_review_submission_receipts(db, partial),
                          BlockedLegacyReviewSubmissionReceipts)
        with pytest.raises(ValueError, match="unique namespace"):
            CompleteLegacyReviewSubmissionReceipts("o/r", (observed.receipts[0], observed.receipts[0]))
        db.rollback()
        db.execute("UPDATE dedupe_keys SET payload_json='changed' WHERE key='key-1'")
        db.execute("BEGIN")
        changed = revalidate_complete_legacy_review_submission_receipts(db, observed)
        db.rollback()

    assert len(observed.receipts) == 3
    assert isinstance(changed, BlockedLegacyReviewSubmissionReceipts)


@pytest.mark.parametrize("alias", ["scope", "namespace"])
def test_complete_submission_receipt_inventory_blocks_matching_blob_alias(tmp_path, alias):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission_receipt(db, 1, scope=b"o/r" if alias == "scope" else "o/r",
                            namespace=b"review-round-submission" if alias == "namespace"
                            else "review-round-submission")
        db.execute("BEGIN")
        page = read_legacy_review_submission_receipt_page(db, "o/r")
        result = read_complete_legacy_review_submission_receipts(db, "o/r")
        db.rollback()

    assert isinstance(page.entries[0], UnreadableLegacyReviewSubmissionReceipt)
    assert isinstance(result, BlockedLegacyReviewSubmissionReceipts)


def test_complete_submission_receipt_inventory_blocks_foreign_row_scan_excess(tmp_path, monkeypatch):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        for number in range(3):
            _submission_receipt(db, number, scope="other/r")
        db.execute("BEGIN")
        monkeypatch.setattr(review_activation, "MAX_LEGACY_SUBMISSION_RECEIPT_SCAN_ROWS", 2)
        result = read_complete_legacy_review_submission_receipts(db, "o/r")
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewSubmissionReceipts)
    assert "total scan bound" in result.reason


@pytest.mark.parametrize("damage", ["unreadable", "rows", "bytes"])
def test_complete_submission_receipt_inventory_blocks_source_excess(tmp_path, monkeypatch, damage):
    control = _control(tmp_path)
    with closing(control._connect()) as db:
        _submission_receipt(db, 1)
        _submission_receipt(db, 2)
        if damage == "unreadable":
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE key='key-1'", ("x" * 65537,))
        db.execute("BEGIN")
        if damage == "rows":
            monkeypatch.setattr(review_activation, "MAX_LEGACY_SUBMISSION_RECEIPT_INVENTORY_ROWS", 1)
        elif damage == "bytes":
            page = read_legacy_review_submission_receipt_page(db, "o/r")
            assert isinstance(page.entries[0], LegacyReviewSubmissionReceiptSource)
            monkeypatch.setattr(review_activation, "MAX_LEGACY_SUBMISSION_RECEIPT_INVENTORY_BYTES",
                                page.entries[0].source_bytes - 1)
        result = read_complete_legacy_review_submission_receipts(db, "o/r")
        db.rollback()

    assert isinstance(result, BlockedLegacyReviewSubmissionReceipts)


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
