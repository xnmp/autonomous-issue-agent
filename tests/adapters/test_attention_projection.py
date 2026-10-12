"""Current producer-owned attention plans and their durable delivery contracts."""

from dataclasses import replace

import pytest

from issue_agent.adapters.effects import StaleExpectedRevision, command_for_action
from issue_agent.domain.actions import PostComment, Relabel, Target
from issue_agent.domain.contracts import EffectStatus
from issue_agent.domain.labels import Label
from issue_agent.domain.lifecycle import (
    completion_guard,
    pr_escalation,
)
from issue_agent.domain.eligibility import LifecycleOperation
from issue_agent.domain.outcome import (
    AgentResult, address_review_actions, budget_actions,
    fix_ci_actions, pr_budget_actions,
    resolve_actions, resume_actions,
)
from tests.adapters.test_effects import _executor

BLOCKED = AgentResult(is_error=False, status="blocked", needs="which API?", summary="clarify API")


