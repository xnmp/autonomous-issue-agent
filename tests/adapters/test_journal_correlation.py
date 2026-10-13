"""Which lifecycle numbers a dispatch's legs are correlated with.

2026-08-03 (keifu): a REVIEW dispatch knows both its PR and the issue that PR
closes, but the correlation only claimed a `pr` when the issue was unknown, so
the review of PR #186 for issue #185 journalled pr=None and the dashboard
relabelled the run "PR #185".
"""

from __future__ import annotations

import asyncio

import pytest

from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.outcome import AgentResult


class _CapturingSink:
    """Stands in for the typed journal: keeps what would have been written."""

    canonical = True
    path = ""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, fields) -> None:
        self.events.append((event, dict(fields)))


class _Inner:
    def __init__(self, status: str) -> None:
        self._status = status

    async def run(self, _task):
        return AgentResult(is_error=False, status=self._status)


def _run_correlations(role: str, kind: str, number: int, issue: int | None):
    sink = _CapturingSink()
    runtime = JournalingRuntime(_Inner("APPROVE"), sink, repo="o/r", role=role)
    runtime.set_context(role, kind, number, issue)

    asyncio.run(runtime.run(RuntimePlan(prompt="p")))

    return [
        (fields.get("issue"), fields.get("pr"))
        for event, fields in sink.events
        if event in ("agent_run_start", "agent_run")
    ]


def test_a_review_leg_correlates_with_both_its_pr_and_its_issue():
    correlations = _run_correlations("review", "REVIEW", 186, 185)

    assert correlations
    assert set(correlations) == {(185, 186)}


@pytest.mark.parametrize("kind", ["RE_REVIEW", "FIX_CI", "ADDRESS_REVIEW", "REBASE"])
def test_every_pr_numbered_kind_claims_its_pr(kind: str):
    assert set(_run_correlations("fix", kind, 186, 185)) == {
        (185, 186)
    }


@pytest.mark.parametrize(
    ("role", "kind", "number", "issue"),
    [
        ("fix", "RESOLVE", 142, 142),
        ("fix", "RESOLVE", 0, 142),
        ("review", "REVIEW", -1, 185),
        ("fix", "", 142, 142),
    ],
)
def test_issue_numbered_and_numberless_contexts_never_claim_a_pr(
    role: str, kind: str, number: int, issue: int | None
):
    correlations = _run_correlations(role, kind, number, issue)

    assert correlations
    assert set(correlations) == {(issue, None)}
