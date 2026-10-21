"""The `act` step of a tick (ADR 0001 as amended by slice 5b): executes derived
Actions against GitHub.

There is no command queue. Every write is either naturally idempotent (a
label set, a merge bound to its head, an update-branch bound to its head, a
PR opened only when the branch has no open one) or goes through
`GitHubAct.keyed_write`: an idempotency key claimed in SQLite before the
write and settled by GitHub's answer to it (design rule 2: SQLite owns
idempotency keys; #440: the answer is the receipt). A write GitHub did not
answer raises; the daemon logs it and the next tick reads the claim back.
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
    ReportGap,
    RerunFailedJobs,
    RunAgent,
    SetIssueState,
    Target,
    UpdateBranch,
)
from ..domain.gaps import GAP_REPORTS_PER_DAY
from ..domain.labels import Label
from ..domain.policy import blocker_closed_comment, blocker_comment
from ..domain.refs import harness_branch_issue
from ..domain.review import Verdict
from ..domain.triage import link_refusal
from .github import Accepted, FixObservationIncomplete, Refused

# A stored key's text is never changed: a rename orphans the keys already
# claimed (the cutover carried them in this form), and a claimed key decides
# whether to post.
_COMMENT_NAMESPACE = "fix-comment"
_ISSUE_NAMESPACE = "fix-issue"
_REPORT_NAMESPACE = "fix-report"
# The record that makes a PR the harness's (ADR 0018), keyed by its head branch.
_PR_NAMESPACE = "fix-pr"
# Stable across retries: the comment's read-back matches its exact body.
_GITHUB_REFUSED = "GitHub would not read or write the link"


def reported_gaps(control) -> frozenset[str]:
    """The keys of the harness-gap reports already confirmed: policy asks
    for no other (`policy.gap_actions`), so a done report is never re-derived."""
    return frozenset(key.removeprefix(f"{_REPORT_NAMESPACE}:") for key in control.done_keys(f"{_REPORT_NAMESPACE}:"))


def owned_branches(control, repository: str) -> frozenset[str]:
    """The head branches the act layer recorded publishing as `repository`'s
    harness PRs: derive's only test of whether an open PR is the harness's."""
    prefix = f"{_PR_NAMESPACE}:{repository}:"
    return frozenset(key.removeprefix(prefix) for key in control.done_keys(prefix))


def submit_review(gateway, pr: int, head: str, verdict: Verdict, bot: str, since: str = "") -> bool:
    """One review on exactly `head` under `bot`, the identity the review
    gateway acts as (checked at boot). GitHub's read-back is the idempotency:
    a standing bot verdict on `head`, no older than `since` (the body edit the
    review judged, #472), means it landed (a crash after the POST, or a racing
    leg), so nothing is posted twice. GitHub's answer is the
    receipt: False is its refusal (a 4xx), which no retry changes; no answer
    raises."""
    if gateway.reviewed_on(pr, head, bot, since):
        return True
    return isinstance(gateway.submit_review(pr, verdict.event, verdict.body, head), Accepted)


def relabel(
    gateway, kind: str, number: int, *, add: Iterable[str] = (), remove: Iterable[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The one label operation for fix and operator writes. A pre-read makes
    replay idempotent, and GitHub's answer is the receipt. Adds go first, so
    a refusal strands "both labels", never an unlabelled item."""
    added: list[str] = []
    removed: list[str] = []
    for label in dict.fromkeys(add):
        if not gateway.has_label(kind, number, label):
            if isinstance(answer := gateway.relabel(kind, number, None, label), Refused):
                raise RuntimeError(f"{kind} #{number}: relabel +{label} refused: {answer.message}")
            added.append(label)
    for label in dict.fromkeys(remove):
        if gateway.has_label(kind, number, label):
            if isinstance(answer := gateway.relabel(kind, number, label, None), Refused):
                raise RuntimeError(f"{kind} #{number}: relabel -{label} refused: {answer.message}")
            removed.append(label)
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
        bot: str,
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
        self.bot = bot
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
        elif isinstance(action, ReportGap):
            self.report(action)
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
            self.set_issue_state(SetIssueState(issue, False, f"{self.marker}\nResolved by #{action.pr}."))
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
                answer = self.gateway.cancel_run(run_id)
            except Exception as exc:  # noqa: BLE001 - throttled or down: the rest would only deepen it
                failed.append(f"{run_id}: {exc}")
                break
            if isinstance(answer, Refused):  # one run's 403/409 must not block the rest
                failed.append(f"{run_id}: refused (HTTP {answer.status}) {answer.message}")
            else:
                done += 1
        if stale:
            self.log(f"{label}: cancelled {done}/{len(stale)} workflow runs left on the merged branch"
                     + (f"; first failure {failed[0]}" if failed else ""))

    def rerun(self, action: RerunFailedJobs) -> None:
        """The harness's one re-run of a head's failed jobs. A run's attempt,
        read before the POST, is the replay guard; the POST's own 2xx answer
        is the receipt (GET /runs lags the POST, #433). A refusal raises and
        repeats each poll, as update-branch's does, until a push or a human."""
        for run_id, observed in action.runs:
            if self.gateway.run_attempt(run_id) > observed:
                continue
            if isinstance(answer := self.gateway.rerun_failed_checks(run_id), Refused):
                raise RuntimeError(f"run {run_id}: re-run refused (HTTP {answer.status}) {answer.message}")
            self.log(f"PR #{action.pr}: re-ran the failed jobs of run {run_id} on {action.head[:12]} "
                     "(the harness's one re-run of this head)")

    def relabel(self, action: Relabel) -> None:
        relabel(
            self.gateway, "issue" if action.target is Target.ISSUE else "pr", action.number,
            add=(action.add.value,) if action.add is not None else (),
            remove=(action.remove.value,) if action.remove is not None else (),
        )

    def keyed_write(self, key: str, present: Callable[[], bool], write: Callable[[], Accepted | Refused],
                    what: str, state: str = "") -> Accepted | Refused | None:
        """The one keyed write (ADR 0001), None when `present` finds it. A key
        claimed (here, unless `state` is given) but never settled may have
        landed unseen, and a settled one may have been deleted (#395), so
        GitHub, read by `present`, decides; a new key writes unread. The answer
        settles the key, a refusal with one line; no answer raises, the claim
        standing for the next tick."""
        state = state or self.control.claim_key(key)
        if state != "new" and present():
            self.control.confirm_key(key)
            return None
        answer = write()
        self.control.confirm_key(key)
        if isinstance(answer, Refused):
            self.log(f"{what}: GitHub refused it (HTTP {answer.status}), settled: {answer.message}")
        elif state == "done":
            self.log(f"{what} was settled but absent (deleted, or refused before); written again")
        return answer

    def post(self, action: PostComment) -> None:
        kind = "issue" if action.target is Target.ISSUE else "pr"
        # A stop comment on an unlabelled item reads as already released, so
        # it is posted only once its `needs-human` label is verified.
        if action.key.startswith("needs-human:") and not self.gateway.has_label(kind, action.number, Label.NEEDS_HUMAN.value):
            raise RuntimeError(f"{kind} #{action.number}: stop comment withheld until needs-human is applied")
        # Present means under the current FIX_BOT: a copy under a previous
        # login reads as human (Harness.wrote), so it is posted again (#424).
        self.keyed_write(f"{_COMMENT_NAMESPACE}:{self.repository}:{action.key}",
                         lambda: self.gateway.has_comment(kind, action.number, action.body, self.bot),
                         lambda: self.gateway.comment(kind, action.number, action.body),
                         f"{kind} #{action.number}: comment {action.key}")

    def report(self, action: ReportGap) -> None:
        """A harness gap in `action.repo` under the fix role's credential (ADR
        0001), once per key, ever; past GAP_REPORTS_PER_DAY new keys a UTC day
        (every daemon on the store) it raises: not taken, so no re-tick. Filed
        when this token has no open issue of the title there, else commented
        on the oldest; a closed or retitled one is a human's answer, so a
        recurrence files afresh. A refusal settles the key with one log line;
        no answer raises for the next tick, which finds a lost filing by its
        exact body and a lost comment by its text."""
        key = f"{_REPORT_NAMESPACE}:{action.key}"
        state = self.control.claim_key_within(key, f"{_REPORT_NAMESPACE}:", GAP_REPORTS_PER_DAY)
        if state == "done":
            return
        if state == "full":
            raise RuntimeError(f"harness gap {action.title!r} not reported: {GAP_REPORTS_PER_DAY} gap reports "
                               "already today (the daily ceiling); it is retried tomorrow while it persists")
        gateway = self.gateway if action.repo == self.repository else self.gateway.for_repo(action.repo)
        found = sorted(n for n, status, title, _ in gateway.issue_index(1000, author="@me")
                       if status == "open" and title.strip() == action.title)
        what = f"harness gap {action.title!r} in {action.repo}"
        answer = self.keyed_write(
            key, lambda: bool(found) and (any(gateway.get_issue(n).body.strip() == action.body.strip() for n in found)
                                          or gateway.has_comment("issue", found[0], action.recurrence, self.bot)),
            (lambda: gateway.comment("issue", found[0], action.recurrence)) if found
            else (lambda: gateway.create_issue(action.title, action.body)), what, state)
        if isinstance(answer, Accepted):
            self.log(f"{what}: " + (f"seen again, commented on #{found[0]}" if found else f"filed as #{answer.id}"))

    def file_issue(self, action: FileIssue) -> None:
        """Keyed. As `post`: the listing of this token's own issues (complete,
        and not the search index), not the key, says whether the issue is
        there. Derive's issue search can lag a filing, and an unconfirmed
        claim may have landed; a confirmed key whose exact title the listing
        lacks was deleted or retitled, and derive asks again (#395)."""
        answer = self.keyed_write(
            f"{_ISSUE_NAMESPACE}:{self.repository}:{action.key}",
            lambda: action.title in {title for _, _, title, _ in self.gateway.issue_index(1000, author="@me")},
            lambda: self.gateway.create_issue(action.title, action.body), f"issue {action.title!r}")
        if isinstance(answer, Accepted):
            self.log(f"issue #{answer.id}: filed {action.title!r}")

    def set_issue_state(self, action: SetIssueState) -> None:
        if self.gateway.is_issue_closed(action.number) != action.open:
            return
        if action.comment:
            self.post(PostComment(Target.ISSUE, action.number, action.comment,
                                  key=f"issue-state:{action.number}:{action.open}"))
        answer = (self.gateway.reopen_issue(action.number) if action.open else
                  self.gateway.close_issue(action.number, "completed"))
        if isinstance(answer, Refused):
            raise RuntimeError(f"issue #{action.number}: state refused: {answer.message}")
        self.log(f"issue #{action.number}: {'reopened' if action.open else 'closed'}: "
                 f"{' '.join(action.comment.split())[:300]}")

    def link_blocker(self, action: LinkBlockedBy) -> None:
        """A steward's "blocked by" claim, rechecked on fresh reads
        (`triage.link_refusal`), linked, then one comment saying so or why
        not, keyed. Once done, nothing here runs again, so a link a human
        deletes stays deleted (derive also stops at the comment)."""
        key = f"{_COMMENT_NAMESPACE}:{self.repository}:{action.key}"
        if (state := self.control.claim_key(key)) == "done":
            # Derive asks again only when the harness's comment is gone (a
            # human deleted it). Say once more that the matter is closed, so
            # derive moves on; the link itself is never touched again.
            closed = blocker_closed_comment(self.marker, action.blocker)
            self.keyed_write(key, lambda: self.gateway.has_comment("issue", action.issue, closed, self.bot),
                             lambda: self.gateway.comment("issue", action.issue, closed),
                             f"issue #{action.issue}: blocked-by comment {action.key}", state)
            return
        try:
            refusal = self._link(action)
        except FixObservationIncomplete as exc:
            # GitHub answered and would not read it (a deleted or transferred
            # blocker): retrying every tick changes nothing, so it is a refusal.
            self.log(f"issue #{action.issue}: blocked-by #{action.blocker}: {str(exc)[:300]}")
            refusal = _GITHUB_REFUSED
        if refusal:
            self.log(f"issue #{action.issue}: steward's blocked-by #{action.blocker} refused: {refusal}")
        body = blocker_comment(self.marker, action.blocker, action.reason, refusal)
        self.keyed_write(key, lambda: self.gateway.has_comment("issue", action.issue, body, self.bot),
                         lambda: self.gateway.comment("issue", action.issue, body),
                         f"issue #{action.issue}: blocked-by comment", state)

    def _link(self, action: LinkBlockedBy) -> str:
        """Link it unless fresh reads or GitHub refuse; the refusal, or "" once
        linked. A claim left by no answer is read back here as `linked`."""
        facts = self.gateway.blocker_link(action.issue, action.blocker)
        if facts.linked:  # whatever changed since (a retry after a lost reply), the link is there
            return ""
        refusal = link_refusal(action.issue, action.blocker, facts.blocker_open, facts.blocker_blocked_by)
        if refusal:
            return refusal
        if isinstance(answer := self.gateway.add_blocked_by(facts.issue_id, facts.blocker_id), Refused):
            self.log(f"issue #{action.issue}: blocked-by #{action.blocker}: {answer.message}")
            return _GITHUB_REFUSED
        self.log(f"issue #{action.issue}: linked blocked by #{action.blocker}")
        return ""

    def open_pr(self, action: OpenPullRequest) -> None:
        # The record comes before the PR (ADR 0018): a crash after it leaves a
        # recorded branch with no PR, which derive owes this action again, so
        # no PR the harness opens is ever unrecorded. An open PR already there
        # is recorded only at the head derive judged a harness leg's push: on a
        # stale observation it is someone else's.
        key = f"{_PR_NAMESPACE}:{self.repository}:{action.branch}"
        if (existing := self.gateway.pull_request_for_branch(action.branch)) is not None:
            if action.branch in owned_branches(self.control, self.repository):
                return  # opened by an earlier call whose answer was lost; the observation predates it
            head = self.gateway.get_pr(existing, mergeability=False, checks=False).head_ref_oid
            if not action.head or head != action.head:
                self.log(f"issue #{action.issue}: PR #{existing} on {action.branch} is not the harness's; not recorded")
                return
            self._record(key)
            self.log(f"issue #{action.issue}: recorded PR #{existing} on {action.branch} as the harness's")
            return
        if action.head:  # the PR derive judged closed since: adoption opens nothing
            self.log(f"issue #{action.issue}: no open PR on {action.branch} to record")
            return
        self._record(key)
        issue = self.gateway.get_issue(action.issue)
        # The agent writes the whole description (AGENT.md step 9). Without
        # one the PR still names its issue; nothing depends on that line,
        # since merge closes the issue itself and the reviewer's bar is the
        # steward's criteria, not this body.
        written = self.latest_resolve_summary(action.issue)
        body = written if written.strip() else f"Closes #{action.issue}"
        answer = self.gateway.open_pull_request(action.branch, self.base_branch, issue.title or action.branch, body)
        if isinstance(answer, Refused):
            raise RuntimeError(f"issue #{action.issue}: PR for {action.branch} refused (HTTP {answer.status}) {answer.message}")
        self.log(f"issue #{action.issue}: opened PR for {action.branch}")

    def _record(self, key: str) -> None:
        if self.control.claim_key(key) != "done":
            self.control.confirm_key(key)

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


__all__ = ["GitHubAct", "relabel", "reported_gaps", "submit_review"]
