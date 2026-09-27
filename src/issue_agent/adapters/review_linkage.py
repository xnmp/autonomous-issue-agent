"""Read-only issue/SQLite join shared by review admission and effects."""

from contextlib import closing
from dataclasses import replace
import sqlite3

from ..domain.review_linkage import (
    LinkedIssueRead, LinkedIssueUnavailable, NoLinkedIssue, ObservedLinkedIssue, linked_issue_holds,
)
from ..domain.workflow_identity import IssueRef, PullRequestRef, WorkflowKey
from ..domain.world import IssueView, PRView, issue_for_pr
from .control_store import SQLiteControlStore
from .workflow_rows import linked_issue_workflow_holds


class ReviewLinkedIssueUnavailable(ValueError):
    """Remote linkage evidence is unavailable, so applicable work must defer."""


def read_review_linked_issue(
    control: SQLiteControlStore, gateway: object, key: WorkflowKey, view: object,
) -> LinkedIssueRead:
    if (not isinstance(key.target, PullRequestRef) or not isinstance(view, PRView)
            or type(view.number) is not int or view.number != key.target.number):
        return LinkedIssueUnavailable("linked issue read requires this pull request's complete view")
    try:
        number = issue_for_pr(view)
        if number is None:
            return NoLinkedIssue()
        issue_key = WorkflowKey(key.repository, IssueRef(number))
        reader = getattr(gateway, "get_review_linked_issue", None)
        if not callable(reader):
            reader = getattr(gateway, "get_issue")
        issue = reader(number)
        if not isinstance(issue, IssueView) or issue.number != number:
            raise ValueError("linked issue read returned another issue or no issue")
        blockers = getattr(gateway, "review_issue_blockers", None)
        if not callable(blockers):
            blockers = getattr(gateway, "open_blockers", None)
        if callable(blockers) and issue.state.lower() == "open":
            dependencies = blockers(number)
            if not isinstance(dependencies, (tuple, list)) or any(
                type(item) is not int or item <= 0 for item in dependencies
            ):
                raise ValueError("linked issue dependencies are unavailable")
            issue = replace(issue, blocked_by_open=bool(dependencies), open_blocker_numbers=tuple(dependencies))
        observed = ObservedLinkedIssue(issue)
    except sqlite3.Error:
        raise
    except Exception as exc:  # noqa: BLE001 - remote failure never proves absent linkage
        return LinkedIssueUnavailable(f"linked issue unavailable: {type(exc).__name__}: {exc}"[:512])
    # Local authority failures remain local failures, never remote read backoff.
    control.require_initialized()
    with closing(control._connect()) as db:
        db.execute("BEGIN")
        holds = linked_issue_workflow_holds(db, issue_key)
    return replace(observed, workflow_holds=holds)


def review_linked_issue_holds(
    control: SQLiteControlStore, gateway: object, key: WorkflowKey, view: object,
) -> tuple[str, ...]:
    read = read_review_linked_issue(control, gateway, key, view)
    if isinstance(read, LinkedIssueUnavailable):
        raise ReviewLinkedIssueUnavailable(read.reason)
    return linked_issue_holds(read)
