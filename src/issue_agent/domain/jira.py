"""GitHub issue lifecycle → Jira ticket sync (pure decision logic).

The steward mirrors each GitHub issue into a Jira ticket and keeps the ticket's
status in step with the issue's GitHub lifecycle label. This module is the PURE
part: it maps a label set to a coarse, board-agnostic `JiraPhase` and decides
whether a sync is needed given what we've already pushed to Jira.

The actual Jira calls happen in a steward agent run with the Atlassian MCP
attached (the harness has no standalone MCP client, and per-board transition IDs
are best discovered by the agent at run time) — see tasks.build_jira_sync_task.
Stdlib only; no adapters/SDK imports (enforced by import-linter).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from .labels import Label


class JiraPhase(Enum):
    """Coarse target status, mapped onto the board's real columns by the agent.
    `value` doubles as the persisted phase marker (see Store.get/set_jira_phase)."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class JiraSyncDecision(Enum):
    SKIP = "skip"
    CREATE = "create"
    TRANSITION = "transition"


class JiraSyncResult(Enum):
    RETRY = "retry"
    PERSIST = "persist"


@dataclass(frozen=True)
class JiraSyncPlan:
    decision: JiraSyncDecision
    target: JiraPhase | None = None
    existing_key: str = ""

    @property
    def should_run(self) -> bool:
        return self.decision in (JiraSyncDecision.CREATE, JiraSyncDecision.TRANSITION)


@dataclass(frozen=True)
class JiraSyncOutcome:
    result: JiraSyncResult
    ticket_key: str = ""
    phase: str = ""
    reason: str = ""

    @property
    def should_persist(self) -> bool:
        return self.result is JiraSyncResult.PERSIST


_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


def valid_jira_project_key(project_key: str) -> bool:
    return bool(_PROJECT_KEY.fullmatch(project_key))


def valid_jira_ticket_key(ticket_key: str, project_key: str) -> bool:
    if not valid_jira_project_key(project_key):
        return False
    return bool(
        re.fullmatch(
            rf"{re.escape(project_key)}-[1-9][0-9]*",
            ticket_key,
        )
    ) and len(ticket_key) <= 64


def jira_correlation_id(
    repository: str,
    project_key: str,
    issue: int,
) -> str:
    if not repository or not valid_jira_project_key(project_key) or issue <= 0:
        raise ValueError("complete Jira correlation scope is required")
    digest = hashlib.sha256(
        f"{repository}\0{project_key}\0{issue}".encode()
    ).hexdigest()
    return f"issue-agent:{digest[:32]}"


# Lifecycle label → phase, in DESCENDING precedence (first match wins). An issue
# can briefly carry several lifecycle labels (e.g. ai-done before ai-pr-open is
# cleared); the furthest-along phase is the truthful one.
_PHASE_BY_LABEL: tuple[tuple[Label, JiraPhase], ...] = (
    (Label.AI_DONE, JiraPhase.DONE),
    (Label.AI_PR_OPEN, JiraPhase.IN_PROGRESS),
    (Label.AI_WORKING, JiraPhase.IN_PROGRESS),
    (Label.AI_BLOCKED, JiraPhase.IN_PROGRESS),
    (Label.NEEDS_HUMAN_REVIEW, JiraPhase.IN_PROGRESS),
    (Label.AI_READY, JiraPhase.TODO),
    (Label.AI_TRIAGE, JiraPhase.TODO),
)


def jira_phase(labels: Iterable[Label]) -> JiraPhase | None:
    """The Jira phase an issue's labels imply, or None when no lifecycle label is
    present (an untriaged issue → no ticket is created). Precedence: DONE beats
    IN_PROGRESS beats TODO, so a transient multi-label issue maps to its
    furthest-along state."""
    have = set(labels)
    for label, phase in _PHASE_BY_LABEL:
        if label in have:
            return phase
    return None


def jira_sync_needed(target: JiraPhase, existing_key: str, last_phase: str) -> bool:
    """True when the steward must act: no ticket yet (must create), or the target
    phase differs from what we last pushed (must transition). When the ticket
    exists and is already at `target`, no work — avoids re-driving the agent (and
    its token cost) every poll."""
    if not existing_key:
        return True
    return last_phase != target.value


def plan_jira_sync(labels: Iterable[Label], existing_key: str, last_phase: str) -> JiraSyncPlan:
    target = jira_phase(labels)
    if target is None:
        return JiraSyncPlan(JiraSyncDecision.SKIP)
    if not jira_sync_needed(target, existing_key, last_phase):
        return JiraSyncPlan(JiraSyncDecision.SKIP, target=target, existing_key=existing_key)
    return JiraSyncPlan(
        JiraSyncDecision.TRANSITION if existing_key else JiraSyncDecision.CREATE,
        target=target,
        existing_key=existing_key,
    )


def jira_sync_outcome(
    *,
    is_error: bool,
    status: str,
    returned_ticket_key: str,
    existing_key: str,
    target: JiraPhase,
    project_key: str,
) -> JiraSyncOutcome:
    if is_error or status == "failed":
        return JiraSyncOutcome(JiraSyncResult.RETRY, reason="failed")
    ticket_key = existing_key or returned_ticket_key.strip()
    if not ticket_key:
        return JiraSyncOutcome(JiraSyncResult.RETRY, reason="missing_ticket_key")
    if not valid_jira_ticket_key(ticket_key, project_key):
        return JiraSyncOutcome(JiraSyncResult.RETRY, reason="invalid_ticket_key")
    return JiraSyncOutcome(JiraSyncResult.PERSIST, ticket_key=ticket_key, phase=target.value)
