"""Legacy execution source records keep untrusted namespaces distinct."""

import pytest
from dataclasses import replace
import json

from issue_agent.domain.resolve_activation import LegacyAbsent
from issue_agent.domain.review_execution_activation import (
    AttributedLegacyReviewDeath, LegacyExecutionCounterRecord, LegacyExecutionPRRecord, LegacyExecutionRunPage,
    LegacyExecutionRunRecord, LegacyReviewRunNoDeath, LegacyReviewRunState, UnattributedLegacyReviewDeath,
    UnreadableLegacyExecutionRun, classify_legacy_review_death, LEGACY_SUBMISSION_COMMAND_COLUMNS,
    LEGACY_SUBMISSION_RECEIPT_COLUMNS, LegacyReviewSubmissionPage, LegacyReviewSubmissionSource,
    UnreadableLegacyReviewSubmission,
    LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS, LegacyReviewSubmissionReceiptPage,
    LegacyReviewSubmissionReceiptSource, UnreadableLegacyReviewSubmissionReceipt,
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


def review_dispatch(**changes):
    payload = {"repository": "o/r", "role": "review", "kind": "REVIEW", "number": 7,
               "breaker_key": -7, "issue": None, "claim_target": 99}
    payload.update(changes)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def test_review_death_attribution_retains_pr_and_both_legacy_counter_namespaces():
    negative = classify_legacy_review_death(run_source(dispatch_id=review_dispatch()))
    positive = classify_legacy_review_death(run_source(dispatch_id=review_dispatch(breaker_key=99, issue=99)))
    assert isinstance(negative, AttributedLegacyReviewDeath)
    assert (negative.repository, negative.pr, negative.counter_key) == ("o/r", 7, -7)
    assert isinstance(positive, AttributedLegacyReviewDeath)
    assert (positive.pr, positive.counter_key) == (7, 99)
    assert positive.source.source_digest != negative.source.source_digest


def test_completed_run_does_not_become_a_death_even_with_opaque_dispatch():
    result = classify_legacy_review_death(run_source(state=LegacyReviewRunState.COMPLETED,
                                                     dispatch_id="opaque historical id"))
    assert isinstance(result, LegacyReviewRunNoDeath)


@pytest.mark.parametrize("dispatch", [
    "opaque historical id", "[]", '{"breaker_key":-7,"breaker_key":99}',
    '{"repository":"o/r","role":"review","kind":"REVIEW","number":7,"breaker_key":NaN}',
    review_dispatch(role="fix"), review_dispatch(kind="FIX"), review_dispatch(kind=[]),
    review_dispatch(kind={}), review_dispatch(number=True),
    review_dispatch(number=0), review_dispatch(breaker_key=True), review_dispatch(breaker_key=0),
    review_dispatch(breaker_key=-8), review_dispatch(repository="other"),
    review_dispatch(breaker_key=99, issue=42), review_dispatch(breaker_key=99),
])
def test_ambiguous_or_foreign_review_death_is_an_explicit_refusal(dispatch):
    result = classify_legacy_review_death(run_source(dispatch_id=dispatch))
    assert isinstance(result, UnattributedLegacyReviewDeath)
    assert result.source.run_id == "run-1" and result.reason


def test_review_death_kind_must_agree_with_the_run_receipt():
    result = classify_legacy_review_death(run_source(dispatch_id=review_dispatch(kind="RE_REVIEW")))
    assert isinstance(result, UnattributedLegacyReviewDeath)


def test_attributed_variant_cannot_claim_facts_that_disagree_with_its_source():
    source = run_source(dispatch_id=review_dispatch())
    with pytest.raises(ValueError, match="source"):
        AttributedLegacyReviewDeath(source, "o/r", 8, -8)
    with pytest.raises(ValueError, match="source"):
        AttributedLegacyReviewDeath(source, "other/r", 7, -7)
    with pytest.raises(ValueError, match="source"):
        AttributedLegacyReviewDeath(run_source(dispatch_id=review_dispatch(kind=[])), "o/r", 7, -7)


def submission_source(**changes):
    command = dict.fromkeys(LEGACY_SUBMISSION_COMMAND_COLUMNS, "value")
    command.update(command_id="command-7", kind="submit_review", repository="o/r", occurrence=0,
                   attempts=1, refusals=0, expected_revision=None, workflow_binding_json=None,
                   workflow_reconciliation_json=None, attempt_owner=None, prewrite_read_json=None)
    command.update(changes)
    receipt = dict.fromkeys(LEGACY_SUBMISSION_RECEIPT_COLUMNS, "value")
    receipt.update(command_id="command-7", remote_revision=None, error=None)
    return LegacyReviewSubmissionSource(7, tuple(command.items()), tuple(receipt.items()))


def test_submission_source_digest_covers_command_and_receipt_without_sql_cursor():
    source = submission_source()
    assert replace(source, source_rowid=-1).source_digest == source.source_digest
    changed = tuple((name, "different" if name == "remote_object_id" else value)
                    for name, value in source.receipt_fields)
    assert replace(source, receipt_fields=changed).source_digest != source.source_digest
    assert LegacyReviewSubmissionPage((UnreadableLegacyReviewSubmission(-1, "bad"), source), 7, False).next_rowid == 7


@pytest.mark.parametrize("changes", [
    {"kind": "relabel"}, {"repository": "Other/R"}, {"occurrence": True},
    {"payload_json": "x" * 65537}, {"command_id": ""},
])
def test_submission_source_rejects_invalid_command_fields(changes):
    with pytest.raises(ValueError):
        submission_source(**changes)


def test_submission_source_requires_exact_fields_and_matching_receipt():
    source = submission_source()
    with pytest.raises(ValueError):
        replace(source, command_fields=source.command_fields[:-1])
    with pytest.raises(ValueError):
        replace(source, receipt_fields=(("command_id", "other"), *source.receipt_fields[1:]))


def receipt_source(**changes):
    fields = dict.fromkeys(LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS, "value")
    fields.update(namespace="review-round-submission-index", scope="o/r", key="index-key",
                  reusable_after=None, payload_json="{}")
    fields.update(changes)
    return LegacyReviewSubmissionReceiptSource(5, tuple(fields.items()))


def test_submission_receipt_source_digest_and_page_cursor():
    source = receipt_source()
    assert replace(source, source_rowid=-1).source_digest == source.source_digest
    assert replace(source, fields=tuple((name, "other" if name == "payload_json" else value)
                                        for name, value in source.fields)).source_digest != source.source_digest
    page = LegacyReviewSubmissionReceiptPage((UnreadableLegacyReviewSubmissionReceipt(-1, "bad"), source), 5, False)
    assert page.next_rowid == 5


@pytest.mark.parametrize("changes", [
    {"namespace": "other"}, {"scope": "Other/R"}, {"key": ""}, {"claimed_at": None},
    {"payload_json": "x" * 65537},
])
def test_submission_receipt_source_rejects_bad_identity_or_text(changes):
    with pytest.raises(ValueError):
        receipt_source(**changes)
