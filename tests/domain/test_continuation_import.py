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
