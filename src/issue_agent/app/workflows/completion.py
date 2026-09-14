"""Fresh completion decisions across confirmed, sequential remote effects."""

from dataclasses import replace

from ...domain.actions import CloseIssue, PostComment, Relabel, Target
from ...domain.labels import ADVISORY_LABELS, Label
from ...domain.lifecycle import (
    AllowedEligibility, LifecycleOperation, completion_guard, completion_matches,
    completion_eligibility,
)


def execute_issue_completion(gateway, executor, issue, actions, operation, log) -> bool:
    """Only a confirmed predecessor permits its exact delta in the next observation."""
    baseline = issue
    previous = None
    for action in actions:
        current = gateway.get_issue(issue.number)
        removed = previous.removed_labels if isinstance(previous, Relabel) else ()
        added = ((previous.add,) if isinstance(previous, Relabel) and previous.add else ())
        comment = previous.body if isinstance(previous, PostComment) else ""
        guard = completion_guard(baseline, operation)
        if not completion_matches(guard, current, removed=removed, added=added, comment=comment):
            log(f"issue #{issue.number}: completion deferred; decision facts changed")
            return False
        # A previous lifecycle mutation may itself have installed a hold. It
        # cannot authorize a second disposition, but its explanatory comment is
        # still owed as part of the same confirmed completion.
        action_operation = (
            LifecycleOperation.GROOM
            if isinstance(action, Relabel) and action.add in ADVISORY_LABELS
            and action.add is not Label.NEEDS_BUDGET_REVIEW else operation
        )
        permission = completion_eligibility(current, action_operation, action)
        if not isinstance(permission, AllowedEligibility) and not isinstance(action, PostComment):
            log(f"issue #{issue.number}: completion deferred; {permission}")
            return False
        if isinstance(action, (Relabel, CloseIssue)) and (
            isinstance(action, CloseIssue) or action.target is Target.ISSUE
        ):
            action = replace(action, expected_revision=current.updated_at,
                             completion_guard=completion_guard(current, action_operation))
        if not executor.execute_all([action]):
            return False
        baseline = current
        previous = action
    return True
