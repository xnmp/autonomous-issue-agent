"""A minimal `loop.ActionExecutor` for daemon-loop tests.

The loop only needs `execute` and `replay_pending`. This fake applies the few
lifecycle actions loop tests observe straight onto a gateway fake, hands
`RunAgent` to `run_agent`, and records everything else. It deliberately has
none of the production act's idempotency: loop tests assert scheduling, not
GitHub write semantics (those are `adapters/act.py`'s tests).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from issue_agent.domain.actions import Action, EnableAutoMerge, PostComment, Relabel, RerunFailedChecks, RunAgent, Target


def _kind(target: Target) -> str:
    return "issue" if target is Target.ISSUE else "pr"


class ImmediateExecutor:
    def __init__(self, gateway: Any, *, run_agent: Callable[[RunAgent], None] | None = None, **_ignored: Any) -> None:
        self.gateway = gateway
        self._run_agent = run_agent
        self.applied: list[Action] = []

    def replay_pending(self) -> bool:
        return True

    def execute(self, action: Action) -> None:
        if isinstance(action, RunAgent):
            if self._run_agent is None:
                raise RuntimeError("no run_agent wired for this test")
            self._run_agent(action)
        elif isinstance(action, Relabel):
            # Read back like act does: an unconfirmed relabel raises, which is
            # how the loop learns a claim did not complete.
            kind = _kind(action.target)
            self.gateway.relabel(kind, action.number, action.remove, action.add)
            if action.add is not None and not self.gateway.has_label(kind, action.number, action.add):
                raise RuntimeError(f"relabel {kind} #{action.number} not confirmed")
        elif isinstance(action, EnableAutoMerge):
            if not self.gateway.enable_auto_merge(action.pr, action.method, action.head):
                raise RuntimeError(f"auto-merge on PR #{action.pr} not armed")
        elif isinstance(action, PostComment):
            self.gateway.comment(_kind(action.target), action.number, action.body)
        elif isinstance(action, RerunFailedChecks):
            self.gateway.rerun_failed_checks(action.head, action.run_ids)
        self.applied.append(action)
