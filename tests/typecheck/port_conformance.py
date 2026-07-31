"""Compile-time assertions for authority-bearing hexagonal seams.

The repository is migrating a large legacy annotation backlog to Pyrefly's
default preset. These assignments deliberately re-enable structural checks for
the ports where a drift could skip a durable write or corrupt control state.
"""

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.store import Store
from issue_agent.adapters.worktree import Worktrees
from issue_agent.domain.ports import (
    ControlStore,
    FixWorktreePort,
    WorkflowState,
)


def workflow_state(store: Store) -> WorkflowState:
    return store


def control_store(store: SQLiteControlStore) -> ControlStore:
    return store


def fix_worktrees(worktrees: Worktrees) -> FixWorktreePort:
    return worktrees
