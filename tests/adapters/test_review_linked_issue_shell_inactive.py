"""A shell created while the resolve family is inactive holds nothing."""

from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_rows import workflow_scope
from issue_agent.domain.workflow import IssueWorkflowShell
from issue_agent.domain.workflow_identity import WorkflowFence, WorkflowRevision
from tests.adapters.test_review_linked_issue_shell import control, holds_of  # noqa: F401
from tests.fakes.review_workflow import ISSUE_KEY


def test_shell_under_an_inactive_resolve_family_contributes_no_holds(control):
    revision = WorkflowRevision(WorkflowFence(ISSUE_KEY, 0), 0)
    with control.transaction() as db:
        db.execute("INSERT INTO workflows VALUES (?,?,?,0,0,?)",
                   (*workflow_scope(ISSUE_KEY), encode_workflow(IssueWorkflowShell(revision))))
        assert db.execute("SELECT COUNT(*) FROM workflow_authority_epochs").fetchone()[0] == 0
    assert holds_of(control) == ()
