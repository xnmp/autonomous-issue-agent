"""Legacy execution source records keep untrusted namespaces distinct."""

import pytest
from dataclasses import replace

from issue_agent.domain.resolve_activation import LegacyAbsent
from issue_agent.domain.review_execution_activation import (
    LegacyExecutionCounterRecord, LegacyExecutionPRRecord, LegacyExecutionRunPage, LegacyExecutionRunRecord,
    LegacyReviewRunState, UnreadableLegacyExecutionRun,
)


@pytest.mark.parametrize("pr", [0, -1, 2**63, True, "7"])
def test_pr_source_requires_a_valid_pull_request_number(pr):
    with pytest.raises(ValueError):
        LegacyExecutionPRRecord(pr, LegacyAbsent(), LegacyAbsent())


@pytest.mark.parametrize("key", [0, -(2**63), 2**63, True, "-7"])
def test_counter_source_requires_a_signed_legacy_target_key(key):
    with pytest.raises(ValueError):
        LegacyExecutionCounterRecord(key, LegacyAbsent())


@pytest.mark.parametrize("record", [
    lambda: LegacyExecutionPRRecord(7, None, LegacyAbsent()),
    lambda: LegacyExecutionCounterRecord(-7, "1"),
    lambda: LegacyExecutionPRRecord(7, LegacyAbsent(), LegacyAbsent(), ""),
])
def test_source_records_reject_untyped_files_and_missing_root(record):
    with pytest.raises(ValueError):
        record()


def run_source(**changes):
    values = dict(source_rowid=1, owner_id="o/r:review:owner", run_id="run-1", dispatch_id='{"breaker_key":-7}',
                  kind="REVIEW", state=LegacyReviewRunState.ABANDONED, config_version="cfg", started_at="2026-09-28",
                  completed_at="2026-09-28", outcome="crashed", provider_receipt_id=None,
                  workflow_binding_json=None, workflow_episode_key=None, review_round_id=None,
                  review_child_id=None)
    values.update(changes)
    return LegacyExecutionRunRecord(**values)


def test_run_source_digest_covers_stored_evidence_but_not_the_page_cursor():
    source = run_source()
    assert replace(source, source_rowid=22).source_digest == source.source_digest
    assert replace(source, completed_at="later").source_digest != source.source_digest
    assert replace(source, state=LegacyReviewRunState.COMPLETED).source_digest != source.source_digest


@pytest.mark.parametrize("changes", [
    {"run_id": ""}, {"dispatch_id": None}, {"state": "abandoned"},
    {"outcome": "x" * 65537}, {"source_rowid": -(2**63) - 1},
])
def test_run_source_rejects_malformed_or_unbounded_rows(changes):
    with pytest.raises(ValueError):
        run_source(**changes)


def test_run_pages_require_monotonic_bounded_source_cursors():
    source = run_source()
    assert LegacyExecutionRunPage((source, UnreadableLegacyExecutionRun(2, "oversized")), 2, False).next_rowid == 2
    with pytest.raises(ValueError):
        LegacyExecutionRunPage((source, source), 1, False)
    with pytest.raises(ValueError):
        LegacyExecutionRunPage((source,), 2, False)
