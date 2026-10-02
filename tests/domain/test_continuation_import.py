"""Stopped import refuses unknown facts while preserving direct typed debt."""

import hashlib

import pytest

from issue_agent.domain import continuation_components as cc
from issue_agent.domain.continuation_import import (
    ISSUE_FIELDS, PR_FIELDS, STEWARD_FIELDS, LegacyContinuationRecord, classify_record,
)
from issue_agent.domain.resolve_activation import LegacyAbsent, LegacyPresent, LegacyUnreadable
from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowFamily, WorkflowKey


def record(family=WorkflowFamily.ISSUE_RECOVERY, **fields):
    names = ISSUE_FIELDS if family is WorkflowFamily.ISSUE_RECOVERY else PR_FIELDS if family is WorkflowFamily.PR_REPAIR else STEWARD_FIELDS
    target = PullRequestRef(7) if family is WorkflowFamily.PR_REPAIR else IssueRef(7)
    def source(name):
        if name not in fields:
            return LegacyAbsent()
        value = fields[name]
        return LegacyPresent(hashlib.sha256(value.encode()).hexdigest(), value)
    return LegacyContinuationRecord(WorkflowKey("o/r", target), family, "fix", tuple((name, source(name)) for name in names))


def test_direct_issue_debt_and_consumed_reply_are_preserved():
    value = classify_record(record(**{"resumed-at": "2026-10-01T00:00:00Z", "resumes": "2", "ci-retries": "3", "infra-deaths": "4"}))
    assert isinstance(value, cc.IssueRecoveryState)
    assert (value.consumed_reply_at, value.resumes, value.ci_failures, value.infra_failures) == ("2026-10-01T00:00:00Z", 2, 3, 4)


def test_review_timestamp_and_count_are_imported_together():
    value = classify_record(record(**{"review-retries": "2026-10-01T00:00:00Z 3"}))
    assert isinstance(value, cc.IssueRecoveryState)
    assert value.review_failures == cc.ReviewRetryState("2026-10-01T00:00:00Z", 3)


@pytest.mark.parametrize("value", ["", "3", "garbage 3", "2026-10-01 3", "2026-10-01T00:00:00Z -1",
                                  "2026-10-01T00:00:00Z 1.5", "2026-10-01T00:00:00Z " + str(2**63)])
def test_unknown_or_malformed_review_episode_stays_source_specifically_blocked(value):
    result = classify_record(record(**{"review-retries": value}))
    assert isinstance(result, tuple) and result
    assert all("review-retries" in blocker.reason for blocker in result)




@pytest.mark.parametrize("value", ["", "-1", "1.5", "1" * 1000, str(2**63)])
def test_malformed_legacy_ci_debt_is_source_specifically_blocked(value):
    result = classify_record(record(**{"ci-retries": value}))
    assert isinstance(result, tuple) and result
    assert all(blocker.source.startswith("legacy:") and "ci-retries" in blocker.reason for blocker in result)


def test_pre_f4_issue_plan_digest_survives_pending_ci_evolution():
    from issue_agent.domain.continuation_import import ContinuationImportPlan, ContinuationImportTarget
    # Captured using accepted 0014d93's IssueRecoveryState, before pending_ci.
    target = ContinuationImportTarget(WorkflowKey("o/r", IssueRef(7)), WorkflowFamily.ISSUE_RECOVERY,
        None, "a" * 64, cc.IssueRecoveryState(ci_failures=3, resumes=2))
    plan = ContinuationImportPlan("o/r", (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR),
        1, "a" * 64, "c" * 64, "d" * 64, (target,))
    assert plan.plan_digest == "33e8ce0f8ae7de17109c46f1775f6ee76d993f063767a5c523b15915b78b1555"


@pytest.mark.parametrize("field,value", [("resumes", ""), ("resumes", "-1"), ("resumes", "1.5"), ("resumes", "1" * 1000),
                                         ("resumed-at", "yesterday"), ("resumed-at", "2026-10-01"),
                                         ("session", "codex:handle"), ("model", "codex:model"), ("machine-block", "unknown")])
def test_unknown_or_malformed_selected_fields_are_blocked(field, value):
    result = classify_record(record(**{field: value}))
    assert isinstance(result, tuple) and result
    assert all(isinstance(b, cc.ContinuationImportBlocker) and b.reason for b in result)


def test_steward_lanes_preserve_independent_baselines_and_stalls():
    value = classify_record(record(WorkflowFamily.STEWARD, **{"triage-at": "2026-10-01T00:00:00Z", "steward-stalls-triage": "2", "steward-stalls-groom": "3"}))
    assert isinstance(value, cc.StewardState)
    assert (value.triage.baseline_at, value.triage.stalls, value.groom.baseline_at, value.groom.stalls) == ("2026-10-01T00:00:00Z", 2, "", 3)


@pytest.mark.parametrize("field,value", [("triage-at", "yesterday"), ("triage-at", ""),
                                         ("steward-stalls-triage", ""), ("steward-stalls-triage", "-1"),
                                         ("steward-stalls-triage", "1.5"), ("steward-stalls-triage", "1" * 1000)])
def test_malformed_selected_triage_history_stays_blocked(field, value):
    result = classify_record(record(WorkflowFamily.STEWARD, **{field:value}))
    assert isinstance(result, tuple) and result


def test_unreadable_present_source_never_becomes_zero():
    original = record()
    fields = tuple((name, LegacyUnreadable("oversized") if name == "resumes" else source) for name, source in original.fields)
    value = classify_record(LegacyContinuationRecord(original.key, original.family, original.root, fields))
    assert isinstance(value, tuple) and "oversized" in value[0].reason


def test_fixed_complete_field_set_cannot_omit_unknown_evidence():
    original = record()
    with pytest.raises(ValueError):
        LegacyContinuationRecord(original.key, original.family, original.root, original.fields[:-1])


@pytest.mark.parametrize("value", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 4096],
                         ids=["empty", "short", "long", "uppercase", "nonhex", "huge"])
def test_malformed_or_huge_groom_content_is_source_specifically_blocked(value):
    result = classify_record(record(WorkflowFamily.STEWARD, **{"groom-at":"2026-10-01T00:00:00Z", "groom-content":value}))
    assert isinstance(result, tuple) and result
    assert any(blocker.reason.startswith("groom-content:") for blocker in result)


def test_groom_content_without_observed_baseline_stays_blocked():
    result = classify_record(record(WorkflowFamily.STEWARD, **{"groom-content":"a" * 64}))
    assert isinstance(result, tuple) and "matching observed groom-at" in result[0].reason


def test_imported_groom_signature_and_stalls_preserve_independent_triage():
    state = classify_record(record(WorkflowFamily.STEWARD, **{
        "triage-at":"2026-09-30T00:00:00Z", "steward-stalls-triage":"1",
        "groom-at":"2026-10-01T00:00:00Z", "groom-content":"a" * 64, "steward-stalls-groom":"3"}))
    assert isinstance(state, cc.StewardState)
    assert state.groom.baseline_at == "2026-10-01T00:00:00Z" and state.groom.stalls == 3
    assert state.groom_content == cc.GroomContent("a" * 64, "a" * 64)
    assert state.triage.baseline_at == "2026-09-30T00:00:00Z" and state.triage.stalls == 1


def test_baseline_only_groom_import_keeps_content_explicitly_unknown():
    state = classify_record(record(WorkflowFamily.STEWARD, **{"groom-at":"2026-10-01T00:00:00Z"}))
    assert isinstance(state, cc.StewardState)
    assert state.groom.baseline_at == "2026-10-01T00:00:00Z"
    assert state.groom_content == cc.GroomContent()


def test_nonempty_groom_content_remains_part_of_import_plan_identity():
    from dataclasses import replace
    from issue_agent.domain.continuation_import import ContinuationImportPlan, ContinuationImportTarget
    selected = record(WorkflowFamily.STEWARD)
    state = cc.StewardState(groom_content=cc.GroomContent("b" * 64, "b" * 64))
    target = ContinuationImportTarget(selected.key, selected.family, None, "a" * 64,
                                      state)
    plan = ContinuationImportPlan("o/r", (WorkflowFamily.STEWARD,), 1, "a" * 64, "c" * 64, "d" * 64, (target,))
    other = replace(target, state=replace(state, groom_content=cc.GroomContent("c" * 64, "c" * 64)))
    assert plan.plan_digest != replace(plan, targets=(other,)).plan_digest


@pytest.mark.parametrize("value", ["claude:handle", "codex:handle", "handle", "unknown:handle", "x" * 65536],
                         ids=["claude", "codex", "untagged", "unknown-runtime", "bounded-large"])
def test_pr_repair_ambiguous_legacy_session_is_blocked_without_new_continuity(value):
    result = classify_record(record(WorkflowFamily.PR_REPAIR, session=value))
    assert isinstance(result, tuple) and result
    assert all(isinstance(item, cc.ContinuationImportBlocker) and item.source and item.reason for item in result)


@pytest.mark.parametrize("value", ["", "-1", "1.5", "9" * 5000, str(2**63), "\ufffd"])
def test_malformed_legacy_rebase_debt_stays_source_specifically_blocked(value):
    result = classify_record(record(**{"rebase-retries": value}))
    assert isinstance(result, tuple) and result
    assert all("rebase-retries" in blocker.reason for blocker in result)


def test_rebase_shadow_rejects_ambiguous_selected_source():
    from issue_agent.domain.continuation_import import ContinuationImportPlan, ContinuationImportTarget, rebase_shadow
    original = record(**{"rebase-retries": "2"})
    target = ContinuationImportTarget(original.key, original.family, None, "a" * 64, cc.IssueRecoveryState(rebase_failures=2))
    plan = ContinuationImportPlan("o/r", (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR), 1, "a" * 64, "c" * 64, "d" * 64, (target,))
    assert rebase_shadow(plan, (original,)) == ((original.key, 2, 2),)
    with pytest.raises(ValueError, match="ambiguous source ownership"):
        rebase_shadow(plan, (original, original))
