"""Preativation review history never silently becomes a fresh execution."""

import hashlib
import json

import pytest

from issue_agent.domain.resolve_activation import LegacyAbsent, LegacyPresent
from issue_agent.domain.review_activation import OpenPullRequestsListed
from issue_agent.domain.review_execution_activation import (
    CompleteLegacyReviewOutcomeReceipts, CompleteLegacyReviewRounds,
    CompleteLegacyReviewRuns, CompleteLegacyReviewSubmissions,
    CompleteLegacyRoundOutcomeStorage, LegacyExecutionCounterRecord,
    LegacyExecutionPRRecord, LegacyExecutionRunRecord, LegacyReviewOwnerLineage,
    LegacyReviewRoundSource, LegacyReviewRunState, LegacyReviewSubmissionSource,
    LEGACY_REVIEW_ROUND_COLUMNS, LEGACY_SUBMISSION_COMMAND_COLUMNS,
    classify_legacy_execution_file_values,
)
from issue_agent.domain.review_execution_counter_mapping import (
    CompleteLegacyOpenCounterLinks, LegacyOpenPRCounterLink,
    map_legacy_execution_counters,
)
from issue_agent.domain.review_execution_death_import import attribute_complete_legacy_review_deaths
from issue_agent.domain.review_execution_history_import import (
    ReviewImportBlockerKind, ReviewImportHistoryBlocked,
    ReviewImportNoHistoryObserved, ReviewImportSource, classify_review_import_history,
)


def _present(text: str) -> LegacyPresent:
    return LegacyPresent(hashlib.sha256(text.encode()).hexdigest(), text)


def _sources(*, session: str | None = None, round_file: str | None = None,
             counter: str | None = None, predecessor: bool = False,
             open_round: bool = False, completed_run: bool = False,
             prs: tuple[int, ...] = (7,)):
    entries = []
    if session is not None or round_file is not None:
        entries.append(LegacyExecutionPRRecord(
            7, _present(session) if session is not None else LegacyAbsent(),
            _present(round_file) if round_file is not None else LegacyAbsent(),
        ))
    if counter is not None:
        entries.append(LegacyExecutionCounterRecord(-7, _present(counter)))
    files = classify_legacy_execution_file_values(tuple(entries))
    links = CompleteLegacyOpenCounterLinks(
        "o/r", OpenPullRequestsListed(prs),
        tuple(LegacyOpenPRCounterLink(pr, "a" * 40, "work/topic", None, None) for pr in prs),
    )
    mapping = map_legacy_execution_counters(files, links)
    owner = "o/r:review:current"
    lineage = LegacyReviewOwnerLineage("o/r", owner, (), (owner, "o/r:review"), ())
    runs = ()
    if predecessor:
        identity = json.dumps({"repository": "o/r", "role": "review", "kind": "REVIEW",
                               "number": 7, "breaker_key": -7, "issue": None})
        runs = (LegacyExecutionRunRecord(
            1, "o/r:review", "run-1", identity, "REVIEW", LegacyReviewRunState.ABANDONED,
            "cfg", "2026-09-28", None, None, None, None, None, None, None,
        ),)
    if completed_run:
        runs += (LegacyExecutionRunRecord(
            2, owner, "run-2", "opaque", "REVIEW", LegacyReviewRunState.COMPLETED,
            "cfg", "2026-09-28", "2026-09-28", "completed", None, None, None,
            None, None,
        ),)
    deaths = attribute_complete_legacy_review_deaths(CompleteLegacyReviewRuns(lineage, runs), mapping)
    round_sources = ()
    if open_round:
        values = ("round-1", "o/r", "pr", 7, 1, "episode", 1, 1, "claim", "{}", "{}", None, None)
        round_sources = (LegacyReviewRoundSource(1, tuple(zip(LEGACY_REVIEW_ROUND_COLUMNS, values, strict=True))),)
    outcomes = CompleteLegacyRoundOutcomeStorage(
        CompleteLegacyReviewRounds("o/r", round_sources),
        CompleteLegacyReviewOutcomeReceipts("o/r", ()), (),
    )
    return mapping, deaths, CompleteLegacyReviewSubmissions("o/r", ()), outcomes


def test_empty_sources_are_observational_and_cover_every_open_pr():
    results = classify_review_import_history(*_sources(prs=(7, 8)))

    assert [item.pr for item in results] == [7, 8]
    assert all(isinstance(item, ReviewImportNoHistoryObserved) for item in results)


def test_empty_candidate_digest_binds_complete_inventory():
    only, = classify_review_import_history(*_sources())
    first, _ = classify_review_import_history(*_sources(prs=(7, 8)))

    assert isinstance(only, ReviewImportNoHistoryObserved)
    assert isinstance(first, ReviewImportNoHistoryObserved)
    assert only.source_digest != first.source_digest


def test_real_session_and_round_file_block_with_exact_file_digests():
    result, = classify_review_import_history(*_sources(session="claude:real-handle", round_file="2"))

    assert isinstance(result, ReviewImportHistoryBlocked)
    assert [item.kind for item in result.blockers] == [
        ReviewImportBlockerKind.SESSION_PRODUCER, ReviewImportBlockerKind.ORIGIN,
    ]
    assert {(item.identity, item.digest) for item in result.blockers[-1].sources} == {
        ("review:pr:7:session", _present("claude:real-handle").digest),
        ("review:pr:7:round", _present("2").digest),
    }


def test_nonzero_issue_debit_and_predecessor_death_stay_separate():
    result, = classify_review_import_history(*_sources(counter="3", predecessor=True))

    assert isinstance(result, ReviewImportHistoryBlocked)
    assert [item.kind for item in result.blockers] == [
        ReviewImportBlockerKind.PREDECESSOR_DEATH,
        ReviewImportBlockerKind.NONZERO_DEBIT,
        ReviewImportBlockerKind.ORIGIN,
    ]
    assert result.blockers[0].sources[0].identity == "run-1"
    assert result.blockers[1].sources[0].identity == "key:-7"


def test_unsealed_round_blocks_even_without_file_state():
    result, = classify_review_import_history(*_sources(open_round=True))

    assert isinstance(result, ReviewImportHistoryBlocked)
    assert [item.kind for item in result.blockers] == [
        ReviewImportBlockerKind.OPEN_ROUND, ReviewImportBlockerKind.ORIGIN,
    ]
    assert result.blockers[0].sources[0].identity == "round-1"


def test_mixed_repositories_refuse_instead_of_returning_empty_history():
    mapping, deaths, submissions, _ = _sources()
    foreign = CompleteLegacyRoundOutcomeStorage(
        CompleteLegacyReviewRounds("other/r", ()),
        CompleteLegacyReviewOutcomeReceipts("other/r", ()), (),
    )

    with pytest.raises(ValueError, match="matching complete source sets"):
        classify_review_import_history(mapping, deaths, submissions, foreign)


def test_malformed_command_target_blocks_all_open_prs():
    mapping, deaths, _, outcomes = _sources(prs=(7, 8))
    values = {
        "command_id": "command-1", "idempotency_key": "idempotency-1",
        "logical_key": "logical-1", "occurrence": 1, "kind": "submit_review",
        "target": "pr:07", "expected_revision": None, "config_version": "cfg",
        "repository": "o/r", "executor_identity": "o/r:review:current",
        "payload_json": "{}", "payload_digest": "a" * 64, "state": "pending",
        "attempts": 0, "created_at": "now", "updated_at": "now", "refusals": 0,
        "workflow_binding_json": None, "workflow_reconciliation_json": None,
        "attempt_owner": None, "prewrite_read_json": None,
    }
    commands = CompleteLegacyReviewSubmissions("o/r", (
        LegacyReviewSubmissionSource(1, tuple((name, values[name]) for name in LEGACY_SUBMISSION_COMMAND_COLUMNS), None),
    ))

    results = classify_review_import_history(mapping, deaths, commands, outcomes)

    assert [item.pr for item in results] == [7, 8]
    assert all(isinstance(item, ReviewImportHistoryBlocked) for item in results)
    assert all(item.blockers[0].kind is ReviewImportBlockerKind.MALFORMED_COMMAND for item in results)
    assert all(item.blockers[0].sources[0].identity == "command-1" for item in results)


def test_nonabandoned_run_without_pr_attribution_blocks_all_open_prs():
    results = classify_review_import_history(*_sources(completed_run=True, prs=(7, 8)))

    assert [item.pr for item in results] == [7, 8]
    assert all(isinstance(item, ReviewImportHistoryBlocked) for item in results)
    assert all(item.blockers[0].kind is ReviewImportBlockerKind.UNATTRIBUTED_RUN for item in results)
    assert all(item.blockers[0].sources[0].identity == "run-2" for item in results)


def test_unassigned_old_counter_blocks_an_apparently_empty_open_pr():
    result, = classify_review_import_history(*_sources(counter="3", prs=(8,)))

    assert isinstance(result, ReviewImportHistoryBlocked)
    assert result.pr == 8
    assert result.blockers[0].kind is ReviewImportBlockerKind.UNASSIGNED_COUNTER
    assert result.blockers[0].sources[0].identity == "key:-7"


@pytest.mark.parametrize("identity,digest", [("\x00", "a" * 64), ("id", "A" * 64), ("id", "short")])
def test_source_identity_rejects_malformed_evidence(identity, digest):
    with pytest.raises(ValueError):
        ReviewImportSource("run", identity, digest)
