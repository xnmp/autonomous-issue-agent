"""Action executor — interpret a domain Action as adapter side-effects.

The decision functions return `[Action]`; this turns each into a gateway /
worktree / agent call. It is deliberately a flat dispatch with NO decision logic
of its own (that all lives in `domain`), so it's verified by asserting which
adapter method each Action triggers, using a fake gateway.

Leaf side-effects (Relabel/PostComment/CloseIssue/EnableAutoMerge/RemoveWorktree)
are handled fully here. `RunAgent` is delegated to an injected `run_agent`
callable — building the per-kind AgentTask (resolve/resume/fix-ci/review/…) is
Phase 4 work; the executor just routes to it.
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain.actions import (
    Action,
    AddDependency,
    CloseIssue,
    EnableAutoMerge,
    UpdateBranch,
    NoOp,
    PostComment,
    Relabel,
    RemoveWorktree,
    RunAgent,
    Target,
)

# gh subcommand kind per target.
_KIND = {Target.ISSUE: "issue", Target.PR: "pr"}


class Executor:
    """Applies Actions. `gateway` is a GitHubGateway (or fake). `remove_worktree`
    and `run_agent` are injected so the executor stays free of git/SDK imports
    and is unit-testable in isolation."""

    def __init__(
        self,
        gateway,
        remove_worktree: Callable[[int], None] | None = None,
        run_agent: Callable[[RunAgent], None] | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.gateway = gateway
        self._remove_worktree = remove_worktree or (lambda issue: None)
        self._run_agent = run_agent or (lambda action: None)
        # Log EVERY applied action so state changes (label moves, comments, close,
        # auto-merge, worktree removal, agent dispatch) are visible instead of
        # inferred. One funnel = one audit trail. Default no-op keeps tests quiet.
        self._log = log or (lambda _m: None)

    def _describe(self, action: Action) -> str:
        """Human-readable one-liner for the action being applied."""
        if isinstance(action, Relabel):
            k = _KIND[action.target]
            rm = action.remove.value if action.remove else None
            add = action.add.value if action.add else None
            move = " ".join(p for p in (f"-{rm}" if rm else "", f"+{add}" if add else "") if p)
            return f"relabel {k} #{action.number}: {move or '(no-op)'}"
        if isinstance(action, PostComment):
            return f"comment {_KIND[action.target]} #{action.number} ({len(action.body)} chars)"
        if isinstance(action, CloseIssue):
            return f"close issue #{action.number} ({action.reason})"
        if isinstance(action, EnableAutoMerge):
            return f"enable auto-merge PR #{action.pr} ({action.method})"
        if isinstance(action, UpdateBranch):
            return f"update branch PR #{action.pr} (BEHIND → up-to-date)"
        if isinstance(action, AddDependency):
            return f"link #{action.issue} blocked-by #{action.blocker}"
        if isinstance(action, RemoveWorktree):
            return f"remove worktree for #{action.issue}"
        if isinstance(action, RunAgent):
            return f"dispatch {action.kind.name} #{action.number}"
        return repr(action)

    def execute(self, action: Action) -> None:
        if not isinstance(action, NoOp):
            self._log(self._describe(action))
        if isinstance(action, Relabel):
            self.gateway.relabel(
                _KIND[action.target],
                action.number,
                action.remove.value if action.remove else None,
                action.add.value if action.add else None,
            )
        elif isinstance(action, PostComment):
            self.gateway.comment(_KIND[action.target], action.number, action.body)
        elif isinstance(action, CloseIssue):
            self.gateway.close_issue(action.number, action.reason, action.comment)
        elif isinstance(action, EnableAutoMerge):
            self.gateway.enable_auto_merge(action.pr, action.method)
        elif isinstance(action, UpdateBranch):
            self.gateway.update_branch(action.pr)
        elif isinstance(action, AddDependency):
            self.gateway.add_blocked_by(action.issue, action.blocker)
        elif isinstance(action, RemoveWorktree):
            self._remove_worktree(action.issue)
        elif isinstance(action, RunAgent):
            self._run_agent(action)
        elif isinstance(action, NoOp):
            return
        else:  # pragma: no cover - exhaustive ADT guard
            raise TypeError(f"unhandled action: {action!r}")

    def execute_all(self, actions) -> None:
        for action in actions:
            self.execute(action)
