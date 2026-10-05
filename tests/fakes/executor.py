"""Explicit non-durable executor composition for isolated application tests."""

from __future__ import annotations

from typing import Any

from issue_agent.adapters.effects import ImmediateGitHubEffectExecutor
from issue_agent.app.execute import Executor


class ImmediateExecutor(Executor):
    """Exercise action behavior without provisioning a SQLite command store."""

    def __init__(self, gateway: Any, *args: Any, **kwargs: Any) -> None:
        self.requested_retry_delays: list[float] = []
        # Isolated action tests retain retry policy without waiting for a network.
        kwargs.setdefault("sleep", self.requested_retry_delays.append)
        kwargs.setdefault(
            "effect_executor", ImmediateGitHubEffectExecutor(gateway)
        )
        super().__init__(gateway, *args, **kwargs)
