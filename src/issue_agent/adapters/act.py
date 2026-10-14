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
from datetime import datetime, timezone

from ..domain.actions import (
    Action,
    FileIssue,
    LinkBlockedBy,
    MergePullRequest,
    OpenPullRequest,
    PostComment,
    Relabel,
    RerunFailedJobs,
    RunAgent,
    SetIssueState,
    Target,
    UpdateBranch,
)
from ..domain.labels import Label
from ..domain.policy import blocker_closed_comment, blocker_comment
from ..domain.refs import harness_branch_issue
from ..domain.review import Verdict
from ..domain.triage import link_refusal
from .github import FixObservationIncomplete, GhUnavailable

# A stored key's text is never changed: a rename orphans the keys already
# claimed (the cutover carried them in this form), and a claimed key decides
# whether to post.
_COMMENT_NAMESPACE = "fix-comment"
_ISSUE_NAMESPACE = "fix-issue"
# Stable across retries: the comment's read-back matches its exact body.
_GITHUB_REFUSED = "GitHub would not read or write the link"


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
        elif isinstance(action, RerunFailedJobs):
            self.rerun(action)
        elif isinstance(action, Relabel):
            self.relabel(action)
        elif isinstance(action, PostComment):
            self.post(action)
        elif isinstance(action, OpenPullRequest):
            self.open_pr(action)
        elif isinstance(action, LinkBlockedBy):
            self.link_blocker(action)
        elif isinstance(action, FileIssue):
            self.file_issue(action)
        elif isinstance(action, SetIssueState):
            self.set_issue_state(action)
        else:
            raise TypeError(f"act does not execute {action!r}")

    def merge(self, action: MergePullRequest) -> None:
        merged_after = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # --match-head-commit: a push after the decision is never merged.
        if not self.gateway.enable_auto_merge(action.pr, self.merge_method, action.head):
            raise RuntimeError(f"PR #{action.pr}: merge of {action.head[:12]} did not land")
        self.log(f"PR #{action.pr}: merged at {action.head[:12]}")
        pr = self.gateway.get_pr(action.pr, mergeability=False, checks=False)
        issue = harness_branch_issue(pr.head_ref_name)
        if issue is None:
            return
        # The merge closes the issue, so the merged PR is never observed again:
        # this is the one place its runs are known to be pointless. Runs
        # created after the merge started are post-merge workflows.
        self.cancel_runs(f"PR #{action.pr} ({pr.head_ref_name})", pr.head_ref_name, merged_after)
        if not self.gateway.is_issue_closed(issue):
            # A non-default base does not close the issue through `Fixes #N`.
            self.gateway.close_issue(issue, "completed", f"{self.marker}\nResolved by #{action.pr}.")
        try:
            self.remove_worktree(issue)
        except Exception as exc:  # noqa: BLE001 - a stale worktree is disk, not state
            self.log(f"issue #{issue}: worktree removal failed after merge: {exc}")

    def cancel_runs(self, label: str, branch: str, created_before: str) -> None:
        """Cancel the unfinished workflow runs of a merged harness branch that
        were created before `created_before` (UTC `YYYY-MM-DDTHH:MM:SSZ`); a
        later run is a post-merge workflow (a `closed` deploy or cleanup) that
        must finish. The target's own `concurrency` groups already cancel a
        superseded push; what no group covers is a run still going after the
        merge, since no further push arrives. Never raises: a missing
        `actions:write` (403), a run that just completed (409) or an unreadable
        list costs one log line, never the merge's follow-through. The run list
        is the read-back, so a replay finds nothing to do."""
        try:
            stale = tuple(i for i, status, created in self.gateway.branch_runs(branch)
                          if status != "completed" and created < created_before)
        except Exception as exc:  # noqa: BLE001 - best-effort
            self.log(f"{label}: merged-branch run cancel unavailable: {exc}")
            return
        failed: list[str] = []
        done = 0
        for run_id in stale:
            try:
                self.gateway.cancel_run(run_id)
                done += 1
            except GhUnavailable as exc:  # throttled or down: the rest would only deepen it
                failed.append(f"{run_id}: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - one run's 403/409 must not block the rest
                failed.append(f"{run_id}: {exc}")
        if stale:
            self.log(f"{label}: cancelled {done}/{len(stale)} workflow runs left on the merged branch"
                     + (f"; first failure {failed[0]}" if failed else ""))

    def rerun(self, action: RerunFailedJobs) -> None:
        """The harness's one re-run of a head's failed jobs, read back by each
        run's attempt so a replay posts nothing. A refusal raises and repeats
        each poll, as update-branch's does, until a push or a human."""
        for run_id, observed in action.runs:
            if self.gateway.run_attempt(run_id) > observed:
                continue
            self.gateway.rerun_failed_checks(run_id)
            if self.gateway.run_attempt(run_id) <= observed:
                raise RuntimeError(f"PR #{action.pr}: re-run of run {run_id} was refused or did not land")
            self.log(f"PR #{action.pr}: re-ran the failed jobs of run {run_id} on {action.head[:12]} "
                     "(the harness's one re-run of this head)")

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
        # GitHub, not the key, says whether the comment is there. A confirmed
        # key is a read-back that once saw it, so a fresh read that does not is
        # a deletion (derive asks again when its own, possibly stale,
        # observation lacks the comment, and it may lack it while it is there); a claimed key may have landed unseen.
        if state != "new":
            if self.gateway.has_comment(kind, action.number, action.body):
                self.control.confirm_key(key)
                return
            if state == "done":
                self.log(f"{kind} #{action.number}: comment {action.key} was deleted; posting it again")
        self.gateway.comment(kind, action.number, action.body)
        if action.key:
            if not self.gateway.has_comment(kind, action.number, action.body):
                raise RuntimeError(f"{kind} #{action.number}: comment {action.key} not confirmed")
            self.control.confirm_key(key)

    def file_issue(self, action: FileIssue) -> None:
        """Claimed before the write, confirmed once the issue reads back. As
        `post`: the listing of this token's own issues (complete, and not the
        search index), not the key, says whether the issue is there. Derive's
        issue search can lag a filing, and an unconfirmed claim may have
        landed; a confirmed key whose exact title the listing lacks was deleted
        or retitled, and derive asks again (#395)."""
        key = f"{_ISSUE_NAMESPACE}:{self.repository}:{action.key}"
        if (state := self.control.claim_key(key)) != "new":
            if action.title in {title for _, _, title, _ in self.gateway.issue_index(1000, author="@me")}:
                self.control.confirm_key(key)
                return
            if state == "done":
                self.log(f"issue for {action.title!r} was deleted or retitled; filing again")
        number = self.gateway.create_issue(action.title, action.body)
        if self.gateway.get_issue(number).title.strip() != action.title.strip():
            raise RuntimeError(f"issue #{number}: filed issue {action.key} not confirmed")
        self.control.confirm_key(key)
        self.log(f"issue #{number}: filed {action.title!r}")

    def set_issue_state(self, action: SetIssueState) -> None:
        if self.gateway.is_issue_closed(action.number) != action.open:
            return
        if action.open:
            self.gateway.reopen_issue(action.number, action.comment)
        else:
            self.gateway.close_issue(action.number, "completed", action.comment)
        if self.gateway.is_issue_closed(action.number) == action.open:
            raise RuntimeError(f"issue #{action.number}: {'reopen' if action.open else 'close'} not confirmed")
        self.log(f"issue #{action.number}: {'reopened' if action.open else 'closed'}: "
                 f"{' '.join(action.comment.split())[:300]}")

    def link_blocker(self, action: LinkBlockedBy) -> None:
        """A steward's "blocked by" claim, rechecked on fresh reads
        (`triage.link_refusal`), linked and read back, then one comment saying
        so or why not. Keyed: once done, nothing here runs again, so a link a
        human deletes stays deleted (derive also stops at the comment)."""
        key = f"{_COMMENT_NAMESPACE}:{self.repository}:{action.key}"
        if self.control.claim_key(key) == "done":
            # Derive asks again only when the harness's comment is gone (a
            # human deleted it). Say once more that the matter is closed, so
            # derive moves on; the link itself is never touched again.
            closed = blocker_closed_comment(self.marker, action.blocker)
            if not self.gateway.has_comment("issue", action.issue, closed):
                self.log(f"issue #{action.issue}: closing comment for {action.key} not found; posting it")
                self.gateway.comment("issue", action.issue, closed)
            return
        try:
            refusal = self._link(action)
        except FixObservationIncomplete as exc:
            # GitHub answered and would not read or write it (a deleted or
            # transferred blocker, a refused mutation): retrying every tick
            # changes nothing, so it is a refusal. GhUnavailable (no answer) raises.
            self.log(f"issue #{action.issue}: blocked-by #{action.blocker}: {str(exc)[:300]}")
            refusal = _GITHUB_REFUSED
        if refusal:
            self.log(f"issue #{action.issue}: steward's blocked-by #{action.blocker} refused: {refusal}")
        body = blocker_comment(self.marker, action.blocker, action.reason, refusal)
        if not self.gateway.has_comment("issue", action.issue, body):
            self.gateway.comment("issue", action.issue, body)
            if not self.gateway.has_comment("issue", action.issue, body):
                raise RuntimeError(f"issue #{action.issue}: blocked-by comment not confirmed")
        self.control.confirm_key(key)

    def _link(self, action: LinkBlockedBy) -> str:
        """Link it unless fresh reads refuse; the refusal, or "" once linked."""
        facts = self.gateway.blocker_link(action.issue, action.blocker)
        if facts.linked:  # whatever changed since (a retry after a lost reply), the link is there
            return ""
        refusal = link_refusal(action.issue, action.blocker, facts.blocker_open, facts.blocker_blocked_by)
        if refusal:
            return refusal
        try:
            self.gateway.add_blocked_by(facts.issue_id, facts.blocker_id)
        except FixObservationIncomplete:
            # An error reply is not proof of no write (a commit before the
            # error, an unclassified gh failure): only a read says it did not land.
            if not self.gateway.blocker_link(action.issue, action.blocker).linked:
                raise
        if not self.gateway.blocker_link(action.issue, action.blocker).linked:
            raise RuntimeError(f"issue #{action.issue}: blocked-by #{action.blocker} not confirmed")
        self.log(f"issue #{action.issue}: linked blocked by #{action.blocker}")
        return ""

    def open_pr(self, action: OpenPullRequest) -> None:
        if self.gateway.pull_request_for_branch(action.branch) is not None:
            return
        issue = self.gateway.get_issue(action.issue)
        # The agent writes the whole description (AGENT.md step 9). Without
        # one the PR still names its issue; nothing depends on that line,
        # since merge closes the issue itself and the reviewer's bar is the
        # steward's criteria, not this body.
        written = self.latest_resolve_summary(action.issue)
        body = written if written.strip() else f"Closes #{action.issue}"
        self.gateway.open_pull_request(action.branch, self.base_branch, issue.title or action.branch, body)
        if self.gateway.pull_request_for_branch(action.branch) is None:
            raise RuntimeError(f"issue #{action.issue}: PR for {action.branch} not confirmed")
        self.log(f"issue #{action.issue}: opened PR for {action.branch}")

    def latest_resolve_summary(self, issue: int) -> str:
        """The agent's summary from the issue's latest returned resolve leg:
        the PR description it wrote, an untrusted claim used only as that.
        The receipt bounds it (20,000 characters) below GitHub's body limit."""
        runs = [
            r for r in self.control.leg_runs(self.repository, (issue,))
            if r.kind == "RESOLVE" and r.receipt is not None and isinstance(r.receipt.get("summary"), str)
        ]
        latest = max(runs, key=lambda r: r.completed_at, default=None)
        return str(latest.receipt["summary"]) if latest is not None and latest.receipt is not None else ""


__all__ = ["GitHubAct", "relabel", "submit_review"]
