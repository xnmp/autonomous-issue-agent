"""Review execution legacy file and owner-run inventories are bounded and read-only."""

from contextlib import closing

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_activation import (
    MAX_RUN_SOURCE_PAGE, MAX_SUBMISSION_SOURCE_PAGE, read_legacy_execution_roots, read_legacy_execution_state,
    read_legacy_review_run_page, read_legacy_review_submission_page,
    read_legacy_review_submission_receipt_page, MAX_SUBMISSION_RECEIPT_SOURCE_PAGE,
)
from issue_agent.domain.resolve_activation import (
    MAX_LEGACY_FILE_BYTES, LegacyAbsent, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry,
)
from issue_agent.domain.review_execution_activation import (
    LegacyExecutionCounterRecord, LegacyExecutionPRRecord, LegacyExecutionRunRecord, LegacyReviewRunState,
    UnreadableLegacyExecutionRun, LegacyReviewSubmissionSource, UnreadableLegacyReviewSubmission,
    LegacyReviewSubmissionReceiptSource, UnreadableLegacyReviewSubmissionReceipt,
)


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
