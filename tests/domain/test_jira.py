"""GitHub lifecycle → Jira phase mapping + sync-needed decision (pure)."""

import pytest

from issue_agent.domain.jira import (
    JiraPhase,
    JiraSyncDecision,
    JiraSyncResult,
    jira_phase,
    jira_sync_needed,
    jira_sync_outcome,
    plan_jira_sync,
)
from issue_agent.domain.labels import Label


# ── jira_phase ──
def test_no_lifecycle_label_is_none():
    assert jira_phase(frozenset()) is None
    assert jira_phase({Label.P1}) is None            # priority is not lifecycle


def test_todo_labels():
    assert jira_phase({Label.AI_READY}) is JiraPhase.TODO
    assert jira_phase({Label.AI_TRIAGE}) is JiraPhase.TODO


def test_in_progress_labels():
    for lbl in (Label.AI_WORKING, Label.AI_PR_OPEN, Label.AI_BLOCKED, Label.NEEDS_HUMAN_REVIEW):
        assert jira_phase({lbl}) is JiraPhase.IN_PROGRESS


def test_done_label():
    assert jira_phase({Label.AI_DONE}) is JiraPhase.DONE


def test_precedence_done_beats_in_progress():
    # a transient multi-label issue maps to its furthest-along phase.
    assert jira_phase({Label.AI_DONE, Label.AI_PR_OPEN}) is JiraPhase.DONE
    assert jira_phase({Label.AI_PR_OPEN, Label.AI_READY}) is JiraPhase.IN_PROGRESS


# ── jira_sync_needed ──
def test_sync_needed_when_no_ticket():
    assert jira_sync_needed(JiraPhase.TODO, "", "") is True


def test_sync_needed_when_phase_changed():
    assert jira_sync_needed(JiraPhase.IN_PROGRESS, "ABC-1", "todo") is True


def test_no_sync_when_already_at_target():
    assert jira_sync_needed(JiraPhase.DONE, "ABC-1", "done") is False
    assert jira_sync_needed(JiraPhase.TODO, "ABC-1", "todo") is False


# ── explicit sync plan ──
def test_plan_skips_when_no_lifecycle_label():
    plan = plan_jira_sync({Label.P1}, "", "")

    assert plan.decision is JiraSyncDecision.SKIP
    assert plan.should_run is False
    assert plan.target is None


def test_plan_create_when_target_has_no_ticket():
    plan = plan_jira_sync({Label.AI_READY}, "", "")

    assert plan.decision is JiraSyncDecision.CREATE
    assert plan.should_run is True
    assert plan.target is JiraPhase.TODO
    assert plan.existing_key == ""


def test_plan_transition_when_existing_ticket_phase_changed():
    plan = plan_jira_sync({Label.AI_PR_OPEN}, "ABC-1", "todo")

    assert plan.decision is JiraSyncDecision.TRANSITION
    assert plan.should_run is True
    assert plan.target is JiraPhase.IN_PROGRESS
    assert plan.existing_key == "ABC-1"


def test_plan_skips_when_existing_ticket_already_at_target():
    plan = plan_jira_sync({Label.AI_DONE}, "ABC-1", "done")

    assert plan.decision is JiraSyncDecision.SKIP
    assert plan.should_run is False
    assert plan.target is JiraPhase.DONE


# ── sync outcome ──
def test_outcome_retries_on_agent_failure_or_failed_status():
    assert jira_sync_outcome(
        is_error=True, status="error", returned_ticket_key="ABC-1",
        existing_key="", target=JiraPhase.TODO, project_key="ABC",
    ).result is JiraSyncResult.RETRY
    assert jira_sync_outcome(
        is_error=False, status="failed", returned_ticket_key="ABC-1",
        existing_key="", target=JiraPhase.TODO, project_key="ABC",
    ).result is JiraSyncResult.RETRY


def test_outcome_requires_ticket_key_for_create():
    outcome = jira_sync_outcome(
        is_error=False, status="created", returned_ticket_key="",
        existing_key="", target=JiraPhase.TODO, project_key="ABC",
    )

    assert outcome.result is JiraSyncResult.RETRY
    assert outcome.reason == "missing_ticket_key"


def test_outcome_persists_created_key_or_existing_transition_key():
    created = jira_sync_outcome(
        is_error=False, status="created", returned_ticket_key=" ABC-1 ",
        existing_key="", target=JiraPhase.TODO, project_key="ABC",
    )
    transitioned = jira_sync_outcome(
        is_error=False, status="transitioned", returned_ticket_key="",
        existing_key="ABC-1", target=JiraPhase.IN_PROGRESS,
        project_key="ABC",
    )

    assert created.result is JiraSyncResult.PERSIST
    assert created.ticket_key == "ABC-1"
    assert created.phase == "todo"
    assert transitioned.result is JiraSyncResult.PERSIST
    assert transitioned.ticket_key == "ABC-1"
    assert transitioned.phase == "in_progress"


@pytest.mark.parametrize(
    "ticket_key",
    (
        "OTHER-1",
        "ABC-0",
        "ABC--1",
        "abc-1",
        "ABC-1; transition OTHER-2",
        "ABC-9999999999999999999999999999999999999999999999999999999999999",
    ),
)
def test_outcome_rejects_keys_outside_the_configured_project(ticket_key):
    outcome = jira_sync_outcome(
        is_error=False,
        status="created",
        returned_ticket_key=ticket_key,
        existing_key="",
        target=JiraPhase.TODO,
        project_key="ABC",
    )

    assert outcome.result is JiraSyncResult.RETRY
    assert outcome.reason == "invalid_ticket_key"
