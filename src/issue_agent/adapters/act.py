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

from collections.abc import Callable, Iterable

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
from ..domain.review import Verdict

# A stored key's text is never changed: a rename orphans the keys already
# claimed (the cutover carried them in this form), and a claimed key decides
# whether to post.
_COMMENT_NAMESPACE = "fix-comment"
_PR_BODY_LIMIT = 60000


def submit_review(gateway, pr: int, head: str, verdict: Verdict, bot: str) -> bool:
    """One review on exactly `head` under `bot`, the identity the review
    gateway acts as (checked at boot). GitHub's read-back is the idempotency:
    a standing bot verdict on `head` means it landed (a crash after the POST,
    or a racing leg), so nothing is posted twice. False is GitHub's refusal (a
    4xx), which no retry changes; no answer or no confirmation raises."""
    if gateway.reviewed_on(pr, head, bot):
        return True
    if not gateway.submit_review(pr, verdict.event, verdict.body, head):
        return False
    if not gateway.reviewed_on(pr, head, bot):
        raise RuntimeError(f"PR #{pr}: review of {head[:12]} not confirmed")
    return True


def relabel(
    gateway, kind: str, number: int, *, add: Iterable[str] = (), remove: Iterable[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Set labels on one issue or PR, each read back: the one label operation
    both the fix role and operator writes use. Idempotent by read: a label
    already present is not re-added, an absent one not removed. Adds go first,
    so a failure strands "both labels", never an unlabelled item. Returns the
    labels it actually added and removed; a write that does not read back
    raises."""
    added: list[str] = []
    removed: list[str] = []
    for label in dict.fromkeys(add):
        if not gateway.has_label(kind, number, label):
            gateway.relabel(kind, number, None, label)
            added.append(label)
        if not gateway.has_label(kind, number, label):
            raise RuntimeError(f"{kind} #{number}: relabel +{label} did not land")
    for label in dict.fromkeys(remove):
        if gateway.has_label(kind, number, label):
            gateway.relabel(kind, number, label, None)
            removed.append(label)
        if gateway.has_label(kind, number, label):
            raise RuntimeError(f"{kind} #{number}: relabel -{label} did not land")
    return tuple(added), tuple(removed)


class GitHubAct:
    """Executes the fix role's Actions; `RunAgent` goes to `dispatch`."""

    def __init__(
        self,
        gateway,
        control,
        *,
        repository: str,
        base_branch: str,
        merge_method: str,
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
        self.marker = marker
        self.dispatch = dispatch
        self.remove_worktree = remove_worktree
        self.log = log

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
        if not self.gateway.is_issue_closed(issue):
            # A non-default base does not close the issue through `Fixes #N`.
            self.gateway.close_issue(issue, "completed", f"{self.marker}\nResolved by #{action.pr}.")
        try:
            self.remove_worktree(issue)
        except Exception as exc:  # noqa: BLE001 - a stale worktree is disk, not state
            self.log(f"issue #{issue}: worktree removal failed after merge: {exc}")

    def relabel(self, action: Relabel) -> None:
        relabel(
            self.gateway, "issue" if action.target is Target.ISSUE else "pr", action.number,
            add=(action.add.value,) if action.add is not None else (),
            remove=(action.remove.value,) if action.remove is not None else (),
        )

    def post(self, action: PostComment) -> None:
        kind = "issue" if action.target is Target.ISSUE else "pr"
        # A stop comment on an unlabelled item reads as already released, so
        # it is posted only once its `needs-human` label is verified.
        if action.key.startswith("needs-human:") and not self.gateway.has_label(kind, action.number, Label.NEEDS_HUMAN.value):
            raise RuntimeError(f"{kind} #{action.number}: stop comment withheld until needs-human is applied")
        key = f"{_COMMENT_NAMESPACE}:{self.repository}:{action.key}"
        state = self.control.claim_key(key) if action.key else "new"
        if state == "done":
            return
        if state == "claimed" and self.gateway.has_comment(kind, action.number, action.body):
            self.control.confirm_key(key)
            return
        self.gateway.comment(kind, action.number, action.body)
        if action.key:
            if not self.gateway.has_comment(kind, action.number, action.body):
                raise RuntimeError(f"{kind} #{action.number}: comment {action.key} not confirmed")
            self.control.confirm_key(key)

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
            r for r in self.control.leg_runs(self.repository, (issue,))
            if r.kind == "RESOLVE" and r.receipt is not None and isinstance(r.receipt.get("summary"), str)
        ]
        latest = max(runs, key=lambda r: r.completed_at, default=None)
        return str(latest.receipt["summary"]) if latest is not None and latest.receipt is not None else ""


__all__ = ["GitHubAct", "relabel", "submit_review"]
