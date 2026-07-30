from __future__ import annotations

from dataclasses import replace

from issue_agent.adapters.github import GhUnavailable
from issue_agent.domain.labels import Label
from issue_agent.domain.refs import issue_from_branch
from issue_agent.domain.world import IssueView, PRView


class InMemoryGitHub:
    """State-model fake for the small authoritative read protocol."""

    def __init__(
        self,
        issues: tuple[IssueView, ...] | dict[int, IssueView] | None = None,
        prs: dict[int, PRView] | None = None,
        pr_for_issue: dict[int, int] | None = None,
        submit_ok: bool = True,
        fail_on: set[str] | None = None,
    ):
        self.issues = (
            dict(issues)
            if isinstance(issues, dict)
            else {issue.number: issue for issue in (issues or ())}
        )
        self.prs = dict(prs or {})
        self._issues = self.issues
        self._prs = self.prs
        self.comments: dict[tuple[str, int], list[str]] = {}
        self.loose_labels: dict[tuple[str, int], set[str]] = {}
        self.pr_issues: dict[int, int] = {}
        self._pr_for_issue = dict(pr_for_issue or {})
        self.calls: list[tuple] = []
        self.submit_ok = submit_ok
        self.fail_on = set(fail_on or ())
        self.automerge_ok = True
        self.automerge_enabled: set[int] = set()
        self.rerun_run_id = 4242
        self.run_attempts: dict[str, int] = {}
        self.dependencies: set[tuple[int, int]] = set()
        self.submitted_reviews: set[tuple[int, str, str, str]] = set()

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_on:
            raise GhUnavailable("network", f"{name} unavailable (test-injected)")

    def get_issue(self, number: int) -> IssueView:
        self._maybe_fail("get_issue")
        return self.issues[number]

    def get_pr(
        self, number: int, *, mergeability: bool = True, checks: bool = True
    ) -> PRView:
        del mergeability, checks
        self._maybe_fail("get_pr")
        return self.prs[number]

    def pr_for_issue(self, issue: int) -> int | None:
        self._maybe_fail("pr_for_issue")
        return self._pr_for_issue.get(issue)

    def issue_for_pr(self, pr: int) -> int | None:
        self._maybe_fail("issue_for_pr")
        if pr in self.pr_issues:
            return self.pr_issues[pr]
        view = self.prs.get(pr)
        if view is None:
            return None
        return view.closing_issue or issue_from_branch(view.head_ref_name)

    def pr_diff(self, pr: int) -> str:
        self._maybe_fail("pr_diff")
        return "diff text"

    def has_label(self, kind: str, number: int, label: str) -> bool:
        views = self.issues if kind == "issue" else self.prs
        if number not in views:
            return label in self.loose_labels.get((kind, number), set())
        try:
            expected = Label(label)
        except ValueError:
            return False
        return expected in views[number].labels

    def has_comment(self, kind: str, number: int, body: str) -> bool:
        return body in self.comments.get((kind, number), ())

    def is_issue_closed(self, issue: int) -> bool:
        current = self.issues.get(issue)
        return current is not None and current.state.casefold() != "open"

    def has_review(self, pr: int, event: str, body: str, head: str) -> bool:
        return (pr, event, body, head) in self.submitted_reviews

    def is_auto_merge_enabled_or_merged(self, pr: int) -> bool:
        current = self.prs.get(pr)
        return pr in self.automerge_enabled or (
            current is not None and current.state.upper() == "MERGED"
        )

    def is_branch_updated(self, pr: int, prior_head: str) -> bool:
        current = self.prs.get(pr)
        return (
            current is not None
            and current.head_ref_oid != prior_head
            and not current.is_behind()
        )

    def max_run_attempt(self, head: str) -> int | None:
        return self.run_attempts.get(head, 0)

    def has_blocked_by(self, issue: int, blocker: int) -> bool:
        return (issue, blocker) in self.dependencies

    def relabel_issue(
        self, issue: int, *, remove: Label | None = None, add: Label | None = None
    ) -> None:
        current = self.issues[issue]
        labels = set(current.labels)
        if remove is not None:
            labels.discard(remove)
        if add is not None:
            labels.add(add)
        self.issues[issue] = replace(current, labels=frozenset(labels))

    def relabel(
        self,
        kind: str,
        number: int,
        remove: str | None,
        add: str | None,
    ) -> None:
        self._maybe_fail("relabel")
        self.calls.append(("relabel", kind, number, remove, add))
        views = self.issues if kind == "issue" else self.prs
        current = views.get(number)
        if current is None:
            labels = self.loose_labels.setdefault((kind, number), set())
            if remove:
                labels.discard(remove)
            if add:
                labels.add(add)
            return
        labels = set(current.labels)
        if remove:
            labels.discard(Label(remove))
        if add:
            labels.add(Label(add))
        views[number] = replace(current, labels=frozenset(labels))

    def comment(self, kind: str, number: int, body: str) -> None:
        self._maybe_fail("comment")
        self.calls.append(("comment", kind, number, body))
        self.comments.setdefault((kind, number), []).append(body)

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self._maybe_fail("close_issue")
        self.calls.append(("close", number, reason, comment))
        current = self.issues.get(number)
        if current is not None:
            self.issues[number] = replace(current, state="closed")

    def enable_auto_merge(self, pr: int, method: str) -> bool:
        self._maybe_fail("enable_auto_merge")
        self.calls.append(("automerge", pr, method))
        if self.automerge_ok:
            self.automerge_enabled.add(pr)
        return self.automerge_ok

    def update_branch(self, pr: int) -> None:
        self._maybe_fail("update_branch")
        self.calls.append(("update_branch", pr))
        current = self.prs.get(pr)
        if current is not None:
            self.prs[pr] = replace(
                current,
                head_ref_oid=f"{current.head_ref_oid}-updated",
                merge_state="CLEAN",
            )

    def rerun_failed_checks(self, pr: int) -> int:
        self._maybe_fail("rerun_failed_checks")
        self.calls.append(("rerun", pr))
        current = self.prs.get(pr)
        if current is not None:
            head = current.head_ref_oid
            self.run_attempts[head] = self.run_attempts.get(head, 0) + 1
        return self.rerun_run_id

    def submit_review(self, pr: int, event: str, body: str) -> bool:
        self._maybe_fail("submit_review")
        self.calls.append(("review", pr, event, body))
        current = self.prs.get(pr)
        if self.submit_ok and current is not None:
            self.submitted_reviews.add((pr, event, body, current.head_ref_oid))
        return self.submit_ok

    def add_blocked_by(self, issue: int, blocker: int) -> None:
        self._maybe_fail("add_blocked_by")
        self.calls.append(("dependency", issue, blocker))
        self.dependencies.add((issue, blocker))
