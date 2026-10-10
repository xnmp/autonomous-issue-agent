"""A departed pull request whose linked issue is an inert shell is still planned and recorded."""

from contextlib import closing
import sqlite3

from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import workflow_scope
from issue_agent.domain.workflow import IssueWorkflowShell
from issue_agent.domain.workflow_identity import IssueRef, WorkflowFence, WorkflowKey, WorkflowRevision
from tests.app.test_review_operator_api import (  # noqa: F401
    bare, ceiling_held_outside_the_review_read, deployment, http,
)

REPO = "o/r"
KEY = WorkflowKey(REPO, IssueRef(1))


def store_shell(path: str, *, row_version: int = 0) -> None:
    """The linked issue as a shell, in a repository whose resolve family is active (as live deployments are)."""
    shell = IssueWorkflowShell(WorkflowRevision(WorkflowFence(KEY, 0), 0))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT OR IGNORE INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   (REPO, "resolve", 1, "acceptance", "acceptance", "2026-10-10T00:00:00+00:00"))
        db.execute("INSERT INTO workflows VALUES (?,?,?,0,?,?)", (*workflow_scope(KEY), row_version, encode_workflow(shell)))


def test_a_departed_pull_request_linking_a_shell_issue_is_re_read_and_recorded_unheld(deployment):
    daemon = ceiling_held_outside_the_review_read(deployment)
    before = http(deployment).read()["daemon"]
    store_shell(deployment.control_path)
    daemon.tick(minutes=31)
    after = http(deployment).read()["daemon"]
    assert after["read"] == "departed_read" and after["as_of"] > before["as_of"]
    assert after["pull_request"]["external_holds"] == []
    assert after["dispatch"] == before["dispatch"]


def test_an_inconsistent_linked_shell_still_leaves_the_departed_read_unrecorded(deployment):
    daemon = ceiling_held_outside_the_review_read(deployment)
    before = http(deployment).read()["daemon"]
    store_shell(deployment.control_path, row_version=3)
    daemon.tick(minutes=31)
    assert http(deployment).read()["daemon"]["as_of"] == before["as_of"]
