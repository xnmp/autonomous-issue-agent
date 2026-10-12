"""The `act` step of a tick (ADR 0001 as amended by slice 5b): executes derived
Actions against GitHub.

There is no command queue. Every write is either naturally idempotent (a
label set, a merge bound to its head, an update-branch bound to its head, a
PR opened only when the branch has no open one) or carries an idempotency key
claimed in SQLite before the write and confirmed after a read-back (design
rule 2: SQLite owns idempotency keys). A write whose read-back fails raises;
the daemon logs it and the next tick re-derives from GitHub.
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain.actions import (
    Action,
    MergePullRequest,
    OpenPullRequest,
    PostComment,
    Relabel,
    RunAgent,
    Target,
    UpdateBranch,
)
from ..domain.labels import Label
from ..domain.pr_body import complete_pr_body, issue_acceptance_criteria
from ..domain.refs import harness_branch_issue

_COMMENT_NAMESPACE = "fix-comment"
_PR_BODY_LIMIT = 60000


class GitHubAct:
    """Executes the fix role's Actions; `RunAgent` goes to `dispatch`.
    `replay_pending` is trivially clean: nothing is queued."""

    def __init__(
        self,
        gateway,
        control,
        *,
        repository: str,
        base_branch: str,
        merge_method: str,
        close_issue_on_merge: bool,
        marker: str,
        dispatch: Callable[[RunAgent], None],
        remove_worktree: Callable[[int], None],
        log: Callable[[str], None],
    ) -> None:
        self.gateway = gateway
        self.control = control
        self.repository = repository
        self.base_branch = base_branch
        self.merge_method = merge_method
        self.close_issue_on_merge = close_issue_on_merge
        self.marker = marker
        self.dispatch = dispatch
        self.remove_worktree = remove_worktree
        self.log = log

    def replay_pending(self) -> bool:
        return True

    def execute(self, action: Action) -> None:
        if isinstance(action, RunAgent):
            self.dispatch(action)
        elif isinstance(action, MergePullRequest):
            self.merge(action)
        elif isinstance(action, UpdateBranch):
            if not self.gateway.update_branch(action.pr, action.head):
                raise RuntimeError(f"PR #{action.pr}: update-branch did not land on {action.head[:12]}")
        elif isinstance(action, Relabel):
            self.relabel(action)
        elif isinstance(action, PostComment):
            self.post(action)
        elif isinstance(action, OpenPullRequest):
            self.open_pr(action)
        else:
            raise TypeError(f"act does not execute {action!r}")

    def merge(self, action: MergePullRequest) -> None:
        # --match-head-commit: a push after the decision is never merged.
        if not self.gateway.enable_auto_merge(action.pr, self.merge_method, action.head):
            raise RuntimeError(f"PR #{action.pr}: merge of {action.head[:12]} did not land")
        self.log(f"PR #{action.pr}: merged at {action.head[:12]}")
        pr = self.gateway.get_pr(action.pr, mergeability=False, checks=False)
        issue = harness_branch_issue(pr.head_ref_name)
        if issue is None:
            return
        if self.close_issue_on_merge and not self.gateway.is_issue_closed(issue):
            # A non-default base does not close the issue through `Fixes #N`.
            self.gateway.close_issue(issue, "completed", f"{self.marker}\nResolved by #{action.pr}.")
        try:
            self.remove_worktree(issue)
        except Exception as exc:  # noqa: BLE001 - a stale worktree is disk, not state
            self.log(f"issue #{issue}: worktree removal failed after merge: {exc}")

    def relabel(self, action: Relabel) -> None:
        kind = "issue" if action.target is Target.ISSUE else "pr"
        self.gateway.relabel(
            kind, action.number,
            action.remove.value if action.remove is not None else None,
            action.add.value if action.add is not None else None,
        )
        for label, want in ((action.add, True), (action.remove, False)):
            if label is not None and self.gateway.has_label(kind, action.number, label.value) is not want:
                raise RuntimeError(f"{kind} #{action.number}: relabel {label.value} did not land")

    def post(self, action: PostComment) -> None:
        kind = "issue" if action.target is Target.ISSUE else "pr"
        # A stop comment on an unlabelled item reads as already released, so
        # it is posted only once its `needs-human` label is verified.
        if action.key.startswith("needs-human:") and not self.gateway.has_label(kind, action.number, Label.NEEDS_HUMAN.value):
            raise RuntimeError(f"{kind} #{action.number}: stop comment withheld until needs-human is applied")
        state = self.control.claim_key(_COMMENT_NAMESPACE, self.repository, action.key) if action.key else "new"
        if state == "done":
            return
        if state == "claimed" and self.gateway.has_comment(kind, action.number, action.body):
            self.control.confirm_key(_COMMENT_NAMESPACE, self.repository, action.key)
            return
        self.gateway.comment(kind, action.number, action.body)
        if action.key:
            if not self.gateway.has_comment(kind, action.number, action.body):
                raise RuntimeError(f"{kind} #{action.number}: comment {action.key} not confirmed")
            self.control.confirm_key(_COMMENT_NAMESPACE, self.repository, action.key)

    def open_pr(self, action: OpenPullRequest) -> None:
        if self.gateway.pull_request_for_branch(action.branch) is not None:
            return
        issue = self.gateway.get_issue(action.issue)
        criteria = issue_acceptance_criteria(issue.body or "", [c.body for c in issue.comments])
        body = complete_pr_body(self.latest_resolve_summary(action.issue), action.issue, self.repository, criteria)
        if len(body) > _PR_BODY_LIMIT:
            body = complete_pr_body("", action.issue, self.repository, criteria)
        self.gateway.open_pull_request(action.branch, self.base_branch, issue.title or action.branch, body)
        if self.gateway.pull_request_for_branch(action.branch) is None:
            raise RuntimeError(f"issue #{action.issue}: PR for {action.branch} not confirmed")
        self.log(f"issue #{action.issue}: opened PR for {action.branch}")

    def latest_resolve_summary(self, issue: int) -> str:
        """The agent's summary from the issue's latest returned resolve leg:
        an untrusted claim, used only as the PR description."""
        runs = [
            r for r in self.control.fix_runs(self.repository, (issue,))
            if r.kind == "RESOLVE" and r.receipt is not None and isinstance(r.receipt.get("summary"), str)
        ]
        latest = max(runs, key=lambda r: r.completed_at, default=None)
        return str(latest.receipt["summary"]) if latest is not None and latest.receipt is not None else ""


__all__ = ["GitHubAct"]
