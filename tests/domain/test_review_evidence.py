"""Bounded review-history reads and historical hold recognition (ADR 0012/0015)."""

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.review_evidence import (
    ReviewEvidenceKind, evidence_due, evidence_gate_id,
    is_review_evidence_gate, review_evidence_gate_kind, unavailable_evidence,
)
from issue_agent.domain.review_workflow import ends_with_advance
from issue_agent.domain.workflow import EvidenceUnavailable, OpenGate, ReconciliationPolicy
from tests.fakes.review_workflow import HEAD, OTHER_HEAD, ceiling_gate, held

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
POLICY = ReconciliationPolicy()
BINDING = held().binding


def unavailable(prior, *, at=NOW, head=HEAD, error="review history unavailable", policy=POLICY):
    return unavailable_evidence(ReviewEvidenceKind.REVIEW_HISTORY, BINDING, head, prior, now=at, error=error, policy=policy)


def test_unavailable_reads_back_off_exponentially_then_gate_visibly_at_the_ceiling():
    progress, at, delays = None, NOW, []
    for _ in range(POLICY.maximum_reads + 5):
        progress = unavailable(progress.schedule if progress else None, at=at)
        if progress.gate is not None:
            break
        delay = (progress.schedule.history.next_read_at - at).total_seconds()
        delays.append(delay)
        at = progress.schedule.history.next_read_at
    assert delays[:3] == [POLICY.initial_delay_seconds * 1, POLICY.initial_delay_seconds * 2,
                          POLICY.initial_delay_seconds * 4]
    assert all(delay <= POLICY.maximum_delay_seconds for delay in delays)
    gate = progress.gate
    assert isinstance(gate, OpenGate) and isinstance(gate.cause, EvidenceUnavailable)
    assert gate.gate_id == evidence_gate_id(ReviewEvidenceKind.REVIEW_HISTORY, 9, HEAD)
    assert is_review_evidence_gate(gate)
    assert gate.cause.last_error == "review history unavailable"
    assert "never counts" in gate.remediation.reason


def test_a_new_head_restarts_the_history_instead_of_inheriting_its_gate():
    first = unavailable(None)
    for _ in range(40):
        first = unavailable(first.schedule, at=first.schedule.history.next_read_at)
        if first.gate is not None:
            break
    assert first.gate is not None
    moved = unavailable(first.schedule, at=NOW + timedelta(days=1), head=OTHER_HEAD)
    assert moved.gate is None and moved.schedule.history.failed_reads == 1


def test_backward_time_cannot_age_an_unresolved_read_into_a_gate():
    first = unavailable(None, at=NOW)
    earlier = unavailable(first.schedule, at=NOW - timedelta(days=30))
    assert earlier.gate is None
    assert earlier.schedule.history.unresolved_since == NOW - timedelta(days=30)


def test_a_huge_error_is_bounded_and_reads_saturate_without_overflow():
    progress = unavailable(None, error="x" * 1_000_000)
    assert len(progress.schedule.last_error) < 20_000


def test_due_is_per_head_and_waits_for_the_schedule():
    progress = unavailable(None)
    assert not evidence_due(progress.schedule, HEAD, now=NOW)
    assert evidence_due(progress.schedule, HEAD, now=progress.schedule.history.next_read_at)
    assert evidence_due(progress.schedule, OTHER_HEAD, now=NOW)
    assert evidence_due(None, HEAD, now=NOW)


@pytest.mark.parametrize("kwargs", [
    dict(head="not-a-sha"), dict(error=""), dict(at=datetime(2026, 9, 25)), dict(prior="schedule"),
])
def test_malformed_inputs_are_refused(kwargs):
    at = kwargs.pop("at", NOW)
    prior = kwargs.pop("prior", None)
    with pytest.raises(ValueError):
        unavailable(prior, at=at, **kwargs)


def test_an_issue_binding_or_bad_pr_number_is_refused():
    from tests.fakes.workflow_authority import resolve_snapshot

    with pytest.raises(ValueError):
        unavailable_evidence(ReviewEvidenceKind.REVIEW_HISTORY, resolve_snapshot().binding, HEAD, None, now=NOW, error="x")
    with pytest.raises(ValueError):
        evidence_gate_id(ReviewEvidenceKind.REVIEW_HISTORY, 0, HEAD)


def test_a_generation_advance_supersedes_an_evidence_hold_but_never_a_ceiling():
    gated = unavailable(None, policy=ReconciliationPolicy(maximum_reads=1)).gate
    assert gated is not None and ends_with_advance(gated)
    assert not is_review_evidence_gate(ceiling_gate())


def test_each_evidence_kind_gates_on_its_own_subject_and_never_as_another_kind():
    policy = ReconciliationPolicy(maximum_reads=1)
    history = unavailable_evidence(ReviewEvidenceKind.REVIEW_HISTORY, BINDING, HEAD, None, now=NOW,
                                   error="review page 0 failed", policy=policy).gate
    trailers = unavailable_evidence(ReviewEvidenceKind.EXEMPTION_TRAILERS, BINDING, HEAD, None, now=NOW,
                                    error="legacy trailers unavailable", policy=policy).gate
    assert history is not None and trailers is not None and history.gate_id != trailers.gate_id
    assert history.gate_id == evidence_gate_id(ReviewEvidenceKind.REVIEW_HISTORY, 9, HEAD)
    assert review_evidence_gate_kind(history) is ReviewEvidenceKind.REVIEW_HISTORY
    assert review_evidence_gate_kind(trailers) is ReviewEvidenceKind.EXEMPTION_TRAILERS
    assert is_review_evidence_gate(history)
    assert "complete review history" in history.cause.required_fact and "never counts" in history.remediation.reason
    assert ends_with_advance(history)
    assert review_evidence_gate_kind(ceiling_gate()) is None


@pytest.mark.parametrize("kind", [None, "review-history", 3])
def test_an_untyped_evidence_kind_is_refused(kind):
    with pytest.raises(ValueError):
        unavailable_evidence(kind, BINDING, HEAD, None, now=NOW, error="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        evidence_gate_id(kind, 9, HEAD)  # type: ignore[arg-type]


def test_an_unobserved_pull_request_backs_off_and_surfaces_its_ceiling_exactly_once():
    from issue_agent.domain.review_evidence import unavailable_first_observation

    progress, at, crossings, delays, surfaced = None, NOW, 0, [], False
    for _ in range(POLICY.maximum_reads + 5):
        progress = unavailable_first_observation(progress.history if progress else None, surfaced=surfaced, now=at,
                                                 policy=POLICY)
        crossings += progress.surface
        surfaced = surfaced or progress.surface
        delays.append((progress.history.next_read_at - at).total_seconds())
        at = progress.history.next_read_at
    assert progress.at_ceiling
    assert delays[:3] == [POLICY.initial_delay_seconds, POLICY.initial_delay_seconds * 2,
                          POLICY.initial_delay_seconds * 4]
    assert max(delays) == POLICY.maximum_delay_seconds and crossings == 1
    backwards = unavailable_first_observation(progress.history, surfaced=True, now=NOW - timedelta(days=1),
                                              policy=POLICY)
    assert backwards.history.unresolved_since <= NOW - timedelta(days=1)
    with pytest.raises(ValueError):
        unavailable_first_observation(None, surfaced=False, now=NOW.replace(tzinfo=None), policy=POLICY)
    with pytest.raises(ValueError):
        unavailable_first_observation("history", surfaced=False, now=NOW, policy=POLICY)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        unavailable_first_observation(None, surfaced="no", now=NOW, policy=POLICY)  # type: ignore[arg-type]
