
from tests.domain.builders import observed_pr
from issue_agent.domain.checks import Check
from dataclasses import replace
from types import SimpleNamespace

import pytest

from issue_agent.adapters.control_store import (
    RetiredOwnerError,
    SQLiteControlStore,
)
from issue_agent.adapters.effects import action_from_command, command_for_action
from issue_agent.domain.actions import (
    CancelWorkflowRuns,
    CloseIssue,
    EnableAutoMerge,
    PostComment,
    Relabel,
    RerunFailedChecks,
    UpdateBranch,
    Target,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.eligibility import LifecycleOperation
from issue_agent.domain.lifecycle import completion_guard
from issue_agent.domain.world import IssueView, Comment


class Gateway:
    def __init__(self):
        self.labels = set()
        self.comments = set()
        self.calls = []
        self.head = "head-1"
        self.ci_state = "clear"
        self.merge_state = "CLEAN"
        self.issue_revision = "issue-rev-1"
        self.auto_merge = set()
        self.run_attempt = 0
        self.run_states = {}
        self.reviews = set()
        self.advance_before_auto_merge = ""
        self.advance_before_update = ""
        self.advance_before_review = ""
        self.issue_closures = {}
        self.linked_prs = {}

    def pr_for_issue(self, number):
        return self.linked_prs.get(number)

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))
        if remove:
            self.labels.discard((kind, number, remove))
        if add:
            self.labels.add((kind, number, add))

    def has_label(self, kind, number, label):
        return (kind, number, label) in self.labels

    def comment(self, kind, number, body):
        self.calls.append(("comment", kind, number, body))
        self.comments.add((kind, number, body))

    def has_comment(self, kind, number, body):
        return (kind, number, body) in self.comments

    def close_issue(self, number, reason, comment):
        self.calls.append(("close", number, reason, comment))
        self.issue_closures[number] = (True, reason)
        if comment:
            self.comments.add(("issue", number, comment))

    def issue_closure(self, number):
        return self.issue_closures.get(number, (False, ""))

    def get_pr(self, pr, *, mergeability=True, checks=True):
        del mergeability, checks
        reports = () if self.ci_state == "clear" else (Check("unit",
            "IN_PROGRESS" if self.ci_state == "pending" else "FAILURE",
            "pending" if self.ci_state == "pending" else "fail", "2000-01-01T00:00:00Z",
            run_id=43 if self.head == "head-2" else 42),)
        return observed_pr(pr, head_ref_oid=self.head, checks=reports, merge_state=self.merge_state,
            author="fix-bot", labels=frozenset({Label.AI_PR_OPEN}))

    def get_issue(self, issue):
        closed, reason = self.issue_closure(issue)
        return IssueView(
            issue, updated_at=self.issue_revision, state="closed" if closed else "open",
            state_reason=reason,
            labels=frozenset(Label(label) for kind, number, label in self.labels if kind == "issue" and number == issue),
            comments=tuple(Comment("bot", body) for kind, number, body in sorted(self.comments) if kind == "issue" and number == issue),
        )

    def rerun_failed_checks(self, head, run_ids, expected_attempts=None):
        assert expected_attempts in (None, {run_id: self.run_attempt for run_id in run_ids})
        self.calls.append(("rerun", head, run_ids))
        # Asynchronous, like the real endpoint: `run_attempt` is NOT yet
        # incremented when the POST returns (issue #177). Tests call
        # `advance_run_attempts` to model GitHub catching up.
        return run_ids

    def advance_run_attempts(self):
        self.run_attempt += 1

    def workflow_run_states(self, head, run_ids):
        if head != self.head:
            return None
        return {
            run_id: self.run_states.get(run_id, "in_progress")
            for run_id in run_ids
        }

    def cancel_workflow_runs(self, head, run_ids):
        states = self.workflow_run_states(head, run_ids)
        if states is None:
            return None
        self.calls.append(("cancel", head, run_ids))
        # Asynchronous, like the real endpoint: the run is NOT down when the
        # POST returns. Tests flip run_states themselves to model GitHub
        # catching up.
        return tuple(
            run_id for run_id, status in states.items() if status != "completed"
        )

    def workflow_run_attempts(self, head, run_ids):
        del head
        return {run_id: self.run_attempt for run_id in run_ids}

    def enable_auto_merge(self, pr, method, expected_head):
        if self.advance_before_auto_merge:
            self.head = self.advance_before_auto_merge
        if self.head != expected_head:
            return False
        self.calls.append(("auto_merge", pr, method, expected_head))
        self.auto_merge.add(pr)
        return True

    def is_expected_head_merged(self, pr, expected_head):
        return pr in self.auto_merge and self.head == expected_head

    def update_branch(self, pr, expected_head):
        if self.advance_before_update:
            self.head = self.advance_before_update
        if self.head != expected_head:
            return False
        self.calls.append(("update_branch", pr, expected_head))
        self.head = f"{self.head}-updated"
        return True

    def is_branch_updated(self, pr, prior_head):
        del pr
        return self.head != prior_head

    def submit_review(self, pr, event, body, expected_head):
        if self.advance_before_review:
            self.head = self.advance_before_review
        if self.head != expected_head:
            return False
        self.calls.append(("review", pr, event, body, expected_head))
        self.reviews.add((pr, event, body, expected_head))
        return True

    def has_review(self, pr, event, body, head):
        return (pr, event, body, head) in self.reviews


def _compound_projection(gateway: Gateway) -> Relabel:
    gateway.labels.add(("issue", 4, Label.AI_WORKING.value))
    observed = gateway.get_issue(4)
    return Relabel(
        Target.ISSUE,
        4,
        Label.AI_WORKING,
        Label.RECURRENCE,
        expected_revision=observed.updated_at,
        completion_guard=completion_guard(
            observed, LifecycleOperation.REPAIR
        ),
        adds=(Label.MAYBE_RESOLVED,),
        comment="The bounded workflow ceiling was reached.",
    )


def test_command_identity_includes_expected_revision() -> None:
    first = command_for_action(
        Relabel(Target.PR, 2, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW),
        repository="o/r",
        executor_identity="o/r:fix",
    )
    second = command_for_action(
        Relabel(Target.PR, 3, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW),
        repository="o/r",
        executor_identity="o/r:fix",
    )
    assert first.command_id != second.command_id


def test_legacy_relabel_identity_and_decoder_ignore_empty_projection_fields() -> None:
    legacy = Relabel(
        Target.ISSUE,
        7,
        Label.AI_READY,
        Label.AI_WORKING,
        "issue-rev-1",
    )
    command = command_for_action(
        legacy,
        repository="o/r",
        executor_identity="o/r:fix",
    )

    assert "adds" not in command.payload
    assert "comment" not in command.payload
    decoded = action_from_command(command)
    assert decoded == legacy
    assert command_for_action(
        decoded,
        repository="o/r",
        executor_identity="o/r:fix",
    ).command_id == command.command_id


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("adds", "recurrence", "projection labels"),
        ("adds", ["not-a-real-label"], "not-a-real-label"),
        ("comment", 3, "projection comment"),
        ("comment", "contains\x00nul", "projection comment"),
    ),
)
def test_compound_relabel_decoder_rejects_malformed_projection(
    field,
    value,
    message,
) -> None:
    command = command_for_action(
        Relabel(
            Target.ISSUE,
            7,
            Label.AI_READY,
            Label.AI_WORKING,
            "issue-rev-1",
        ),
        repository="o/r",
        executor_identity="o/r:fix",
    )

    with pytest.raises(ValueError, match=message):
        action_from_command(
            replace(command, payload={**command.payload, field: value})
        )


def test_command_identity_canonicalizes_repository_case_aliases() -> None:
    action = Relabel(
        Target.PR,
        2,
        Label.AI_PR_OPEN,
        Label.NEEDS_HUMAN_REVIEW,
    )

    first = command_for_action(
        action,
        repository="Acme/App",
        executor_identity="broker",
    )
    second = command_for_action(
        action,
        repository="acme/app",
        executor_identity="broker",
    )

    assert first.repository == second.repository == "acme/app"
    assert first.command_id == second.command_id


def test_a_queued_cancel_is_not_re_minted_every_tick(tmp_path) -> None:
    # Adversarial review N4: `_pending_for_action` finds a queued command by
    # reconstructing its action and comparing for equality, so a field dropped
    # in `action_from_command` makes the rebuilt action differ from the one
    # being executed — and every tick enqueues ANOTHER command for the cancel
    # already queued.
    action = CancelWorkflowRuns(7, "head-1", (42,), "2026-08-02T12:00:00+00:00")
    command = command_for_action(
        action, repository="o/r", executor_identity="fix"
    )

    assert action_from_command(command) == action


def test_every_cancel_field_survives_the_command_round_trip(tmp_path) -> None:
    # Anti-vacuity for the test above: it must fail if a NEW field is added to
    # the action and forgotten in the adapter, not merely for today's fields.
    from dataclasses import fields as dataclass_fields

    action = CancelWorkflowRuns(7, "head-1", (42, 43), "2026-08-02T12:00:00Z")
    command = command_for_action(
        action, repository="o/r", executor_identity="fix"
    )
    rebuilt = action_from_command(command)

    for field in dataclass_fields(CancelWorkflowRuns):
        assert getattr(rebuilt, field.name) == getattr(action, field.name), (
            f"{field.name} was dropped in the command round trip"
        )


