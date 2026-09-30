"""Legacy execution source records keep untrusted namespaces distinct."""

import pytest
from dataclasses import replace
import hashlib
import json

from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.contracts import CommandId
from issue_agent.domain.review_execution import (
    ReviewExecutionBinding, ReviewRoundIdentity, ReviewRoundOutcome, CopiedReviewProgress, ReviewPinDeferred,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)

from issue_agent.domain.resolve_activation import LegacyAbsent, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry
from issue_agent.domain.review_execution_activation import (
    BlockedLegacyExecutionValue,
    LegacyExecutionNumber,
    LegacyReviewSessionCandidate,
    LegacyReviewSessionModeMarker,
    LegacyReviewSessionEncoding,
    parse_legacy_review_counter,
    parse_legacy_review_round,
    parse_legacy_review_session,
    classify_legacy_execution_file_values,
    CompleteLegacyExecutionFileValues,
    BlockedLegacyExecutionFileValues,
    MAX_LEGACY_EXECUTION_FILE_ENTRIES,
    ParsedLegacyExecutionPR,
    AttributedLegacyReviewDeath,
    LegacyExecutionCounterRecord,
    LegacyExecutionPRRecord,
    LegacyExecutionRunPage,
    LegacyExecutionRunRecord,
    LegacyReviewRunNoDeath,
    LegacyReviewRunState,
    UnattributedLegacyReviewDeath,
    UnreadableLegacyExecutionRun,
    classify_legacy_review_death,
    LEGACY_SUBMISSION_COMMAND_COLUMNS,
    LEGACY_SUBMISSION_RECEIPT_COLUMNS,
    LegacyReviewSubmissionPage,
    LegacyReviewSubmissionSource,
    UnreadableLegacyReviewSubmission,
    potentially_successful_review_submission,
    LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS,
    LegacyReviewSubmissionReceiptPage,
    LegacyReviewSubmissionReceiptSource,
    UnreadableLegacyReviewSubmissionReceipt,
    LegacyReviewOwnerLink,
    LegacyReviewOwnerLineage,
    CompleteLegacyReviewRuns,
    LegacyReviewOutcomeReceiptSource,
    CompleteLegacyReviewOutcomeReceipts,
)


def _legacy_text(value: str) -> LegacyPresent:
    return LegacyPresent(hashlib.sha256(value.encode()).hexdigest(), value)


@pytest.mark.parametrize("raw,runtime,handle,encoding", (
    ("bare-id", RuntimeName.CLAUDE, "bare-id", LegacyReviewSessionEncoding.BARE_CLAUDE),
    ("codex:session:part", RuntimeName.CODEX, "session:part", LegacyReviewSessionEncoding.RUNTIME_TAG),
    ("RuntimeName.GLM:handle", RuntimeName.GLM, "handle", LegacyReviewSessionEncoding.LEGACY_ENUM_TAG),
    (" claude:session \n", RuntimeName.CLAUDE, "session", LegacyReviewSessionEncoding.RUNTIME_TAG),
))
def test_legacy_session_parser_retains_runtime_handle_and_file_identity(raw, runtime, handle, encoding):
    source = _legacy_text(raw)
    parsed = parse_legacy_review_session(source)
    assert isinstance(parsed, LegacyReviewSessionCandidate)
    assert (parsed.runtime, parsed.handle, parsed.encoding, parsed.file_digest) == (
        runtime, handle, encoding, source.digest,
    )


@pytest.mark.parametrize("raw", ("", " ", "codex:", "unknown:session", "x" * 513,
                                 "claude:\ud800"))
def test_legacy_session_parser_blocks_unusable_values_without_losing_source(raw):
    source = LegacyPresent("a" * 64, raw) if "\ud800" in raw else _legacy_text(raw)
    blocked = parse_legacy_review_session(source)
    assert isinstance(blocked, BlockedLegacyExecutionValue)
    assert blocked.source == source


@pytest.mark.parametrize("raw,runtime,marker,encoding", (
    ("multi", RuntimeName.CLAUDE, "multi", LegacyReviewSessionEncoding.BARE_CLAUDE),
    ("github-review", RuntimeName.CLAUDE, "github-review", LegacyReviewSessionEncoding.BARE_CLAUDE),
    ("codex-review", RuntimeName.CLAUDE, "codex-review", LegacyReviewSessionEncoding.BARE_CLAUDE),
    ("codex:codex-review", RuntimeName.CODEX, "codex-review", LegacyReviewSessionEncoding.RUNTIME_TAG),
    ("codex:multi", RuntimeName.CODEX, "multi", LegacyReviewSessionEncoding.RUNTIME_TAG),
    ("RuntimeName.CLAUDE:github-review", RuntimeName.CLAUDE, "github-review",
     LegacyReviewSessionEncoding.LEGACY_ENUM_TAG),
))
def test_legacy_mode_marker_is_preserved_without_becoming_a_reusable_handle(raw, runtime, marker, encoding):
    source = _legacy_text(raw)
    parsed = parse_legacy_review_session(source)
    assert isinstance(parsed, LegacyReviewSessionModeMarker)
    assert (parsed.runtime, parsed.marker, parsed.encoding, parsed.file_digest) == (
        runtime, marker, encoding, source.digest,
    )
    record = LegacyExecutionPRRecord(7, source, LegacyAbsent())
    complete = classify_legacy_execution_file_values((record,))
    assert isinstance(complete, CompleteLegacyExecutionFileValues)
    assert complete.prs[0].session == parsed


def test_legacy_session_parser_keeps_absence_and_unreadability_distinct():
    absent = LegacyAbsent()
    assert parse_legacy_review_session(absent) == absent
    unreadable = LegacyUnreadable("bad UTF-8")
    blocked = parse_legacy_review_session(unreadable)
    assert isinstance(blocked, BlockedLegacyExecutionValue) and blocked.source == unreadable
    with pytest.raises(ValueError, match="bare legacy session"):
        LegacyReviewSessionCandidate(RuntimeName.CODEX, "handle", LegacyReviewSessionEncoding.BARE_CLAUDE,
                                     "a" * 64)


def test_legacy_round_and_signed_counter_parsers_preserve_default_and_file_values():
    absent_round = parse_legacy_review_round(LegacyExecutionPRRecord(7, LegacyAbsent(), LegacyAbsent()))
    assert absent_round == LegacyAbsent()
    round_file = _legacy_text(" 0002\n")
    assert parse_legacy_review_round(LegacyExecutionPRRecord(7, LegacyAbsent(), round_file)) == (
        LegacyExecutionNumber(2, round_file.digest)
    )
    counter_file = _legacy_text("0\n")
    for key in (7, -7):
        assert parse_legacy_review_counter(LegacyExecutionCounterRecord(key, counter_file)) == (
            LegacyExecutionNumber(0, counter_file.digest)
        )
    missing = parse_legacy_review_counter(LegacyExecutionCounterRecord(7, LegacyAbsent()))
    assert isinstance(missing, BlockedLegacyExecutionValue)
    with pytest.raises(ValueError, match="source digest"):
        LegacyExecutionNumber(0, None)


@pytest.mark.parametrize("raw", ("", "-1", "+1", "1.5", "٣", "1\n2", "9" * 20, str(2**63)))
def test_legacy_number_parsers_block_malformed_or_unbounded_values(raw):
    source = _legacy_text(raw)
    for parsed in (
        parse_legacy_review_round(LegacyExecutionPRRecord(7, LegacyAbsent(), source)),
        parse_legacy_review_counter(LegacyExecutionCounterRecord(-7, source)),
    ):
        assert isinstance(parsed, BlockedLegacyExecutionValue)
        assert parsed.source == source


def test_complete_legacy_file_values_bind_parsed_values_to_roots_and_keys():
    first = LegacyExecutionPRRecord(7, _legacy_text("codex:handle"), _legacy_text("3"), "review")
    second = LegacyExecutionPRRecord(8, LegacyAbsent(), _legacy_text("2"), "steward")
    counter = LegacyExecutionCounterRecord(-7, _legacy_text("4"), "review")

    complete = classify_legacy_execution_file_values((counter, second, first))

    assert isinstance(complete, CompleteLegacyExecutionFileValues)
    assert [item.source.pr for item in complete.prs] == [7, 8]
    assert isinstance(complete.prs[0].session, LegacyReviewSessionCandidate)
    assert complete.prs[0].session.handle == "handle"
    assert complete.counters[0].source.key == -7
    changed_root = classify_legacy_execution_file_values((replace(first, root="fix"), second, counter))
    changed_value = classify_legacy_execution_file_values((replace(first, round=_legacy_text("4")), second, counter))
    assert isinstance(changed_root, CompleteLegacyExecutionFileValues)
    assert isinstance(changed_value, CompleteLegacyExecutionFileValues)
    assert len({complete.source_digest, changed_root.source_digest, changed_value.source_digest}) == 3
    with pytest.raises(ValueError, match="disagree"):
        replace(complete.prs[0], source=replace(first, session=_legacy_text("codex:other")))


@pytest.mark.parametrize("entries,reason", (
    ((LegacyExecutionPRRecord(7, LegacyAbsent(), LegacyAbsent()),), "lacks its file source"),
    ((LegacyExecutionCounterRecord(7, LegacyAbsent()),), "counter has an unreadable"),
    ((LegacyExecutionPRRecord(7, _legacy_text("codex:handle"), LegacyAbsent(), "first"),
      LegacyExecutionPRRecord(7, _legacy_text("codex:handle"), LegacyAbsent(), "first")), "duplicated"),
    ((LegacyExecutionCounterRecord(7, _legacy_text("1"), "first"),
      LegacyExecutionCounterRecord(7, _legacy_text("1"), "first")), "duplicated"),
    ((UnmappedLegacyEntry("issue-0.infra-deaths", "malformed", "review"),), "unmapped"),
    ((LegacyExecutionCounterRecord(-7, LegacyPresent("a" * 64, "1")),), "digest"),
))
def test_complete_legacy_file_values_block_ambiguous_or_unverified_sources(entries, reason):
    blocked = classify_legacy_execution_file_values(entries)
    assert isinstance(blocked, BlockedLegacyExecutionFileValues)
    assert reason in blocked.reason


def test_complete_legacy_file_values_retain_same_numeric_keys_from_separate_roots():
    sources = (
        LegacyExecutionPRRecord(7, _legacy_text("claude:fix-handle"), LegacyAbsent(), "fix"),
        LegacyExecutionPRRecord(7, _legacy_text("claude:review-handle"), LegacyAbsent(), "review"),
        LegacyExecutionCounterRecord(7, _legacy_text("3"), "fix"),
        LegacyExecutionCounterRecord(7, _legacy_text("2"), "review"),
    )

    complete = classify_legacy_execution_file_values(sources)

    assert isinstance(complete, CompleteLegacyExecutionFileValues)
    assert [(item.source.root, item.source.pr) for item in complete.prs] == [("fix", 7), ("review", 7)]
    assert [(item.source.root, item.source.key) for item in complete.counters] == [("fix", 7), ("review", 7)]


def test_complete_legacy_file_values_enforce_inventory_bound_and_typed_membership():
    source = LegacyExecutionCounterRecord(-7, _legacy_text("1"))
    blocked = classify_legacy_execution_file_values((source,) * (MAX_LEGACY_EXECUTION_FILE_ENTRIES + 1))
    assert isinstance(blocked, BlockedLegacyExecutionFileValues)
    with pytest.raises(ValueError, match="typed inventory"):
        classify_legacy_execution_file_values((object(),))
    with pytest.raises(ValueError, match="disagree"):
        ParsedLegacyExecutionPR(
            LegacyExecutionPRRecord(7, _legacy_text("claude:real"), LegacyAbsent()),
            LegacyAbsent(), LegacyAbsent(),
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


def test_complete_run_inventory_requires_unique_authorized_sources():
    lineage = LegacyReviewOwnerLineage("o/r", "o/r:review:owner", (),
                                       ("o/r:review:owner", "o/r:review"), ())
    source = run_source()
    complete = CompleteLegacyReviewRuns(lineage, (source,))

    assert len(complete.source_digest) == 64
    with pytest.raises(ValueError, match="unique owner"):
        CompleteLegacyReviewRuns(lineage, (source, replace(source, source_rowid=2)))
    with pytest.raises(ValueError, match="unique owner"):
        CompleteLegacyReviewRuns(lineage, (replace(source, owner_id="other/r:review"),))


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


def test_success_claim_candidate_includes_either_durable_side():
    source = submission_source(state="succeeded")
    failed_effect = replace(source, receipt_fields=tuple(
        (name, "retryable_failure" if name == "status" else value)
        for name, value in source.receipt_fields
    ))
    assert potentially_successful_review_submission(failed_effect)
    effect_only = replace(source, command_fields=tuple(
        (name, "pending" if name == "state" else value)
        for name, value in source.command_fields
    ), receipt_fields=tuple(
        (name, "succeeded" if name == "status" else value)
        for name, value in source.receipt_fields
    ))
    assert potentially_successful_review_submission(effect_only)
    neither = replace(effect_only, receipt_fields=tuple(
        (name, "retryable_failure" if name == "status" else value)
        for name, value in effect_only.receipt_fields
    ))
    assert not potentially_successful_review_submission(neither)
    assert potentially_successful_review_submission(replace(source, receipt_fields=None))
    with pytest.raises(ValueError, match="typed"):
        potentially_successful_review_submission(None)


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


def outcome_receipt_source(**changes):
    fields = dict.fromkeys(LEGACY_REVIEW_SUBMISSION_RECEIPT_COLUMNS, "value")
    fields.update(namespace="review-round-outcome", scope="o/r", key="round-1",
                  reusable_after=None, payload_json="{}")
    fields.update(changes)
    return LegacyReviewOutcomeReceiptSource(5, tuple(fields.items()))


def test_complete_outcome_receipt_sources_require_unique_repository_identities():
    source = outcome_receipt_source()
    complete = CompleteLegacyReviewOutcomeReceipts("o/r", (source,))
    assert complete.source_digest == CompleteLegacyReviewOutcomeReceipts(
        "o/r", (replace(source, source_rowid=-5),),
    ).source_digest
    with pytest.raises(ValueError, match="unique"):
        CompleteLegacyReviewOutcomeReceipts("o/r", (source, replace(source, source_rowid=6)))
    with pytest.raises(ValueError, match="repository"):
        CompleteLegacyReviewOutcomeReceipts("o/r", (outcome_receipt_source(scope="other/repo"),))


@pytest.mark.parametrize("changes", [
    {"namespace": "other"}, {"scope": "Other/R"}, {"key": ""},
    {"payload_json": None}, {"payload_json": "x" * (2**21 + 1)},
])
def test_outcome_receipt_source_rejects_untrusted_identity_and_payload(changes):
    with pytest.raises(ValueError):
        outcome_receipt_source(**changes)


def test_review_owner_lineage_binds_retirement_sources_without_rowid():
    first = LegacyReviewOwnerLink(1, "o/r:review:old", "o/r:review:new", "o/r", "review", "retired")
    second = LegacyReviewOwnerLink(2, "o/r:review:oldest", "o/r:review:old", "o/r", "review", "earlier")
    lineage = LegacyReviewOwnerLineage("o/r", "o/r:review:new", (),
                                       ("o/r:review:new", "o/r:review", "o/r:review:old", "o/r:review:oldest"),
                                       (first, second))
    assert replace(first, source_rowid=9).source_digest == first.source_digest
    assert replace(lineage, links=(replace(first, source_rowid=9), second)).source_digest == lineage.source_digest
    assert replace(lineage, links=(second, first)).source_digest == lineage.source_digest
    with pytest.raises(ValueError, match="incomplete"):
        replace(lineage, owners=lineage.owners[:-1])


def test_review_owner_lineage_rejects_reachable_cycle():
    a = LegacyReviewOwnerLink(1, "old-a", "old-b", "o/r", "review", "first")
    b = LegacyReviewOwnerLink(2, "old-b", "old-a", "o/r", "review", "second")
    with pytest.raises(ValueError, match="cycle"):
        LegacyReviewOwnerLineage("o/r", "o/r:review:current", ("old-a",),
                                 ("o/r:review:current", "o/r:review", "old-a", "old-b"), (a, b))
