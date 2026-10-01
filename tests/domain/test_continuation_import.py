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
