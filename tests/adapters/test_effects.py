from dataclasses import replace
from types import SimpleNamespace

import pytest

from issue_agent.adapters.control_store import (
    RetiredOwnerError,
    SQLiteControlStore,
)
from issue_agent.adapters.effects import (
    GitHubEffectExecutor,
    action_from_command,
    StaleExpectedRevision,
    command_for_action,
)
from issue_agent.adapters.effects import UnknownEffectOutcome
from issue_agent.domain.actions import (
    AddDependency,
    CancelWorkflowRuns,
    CloseIssue,
    EnableAutoMerge,
    PostComment,
    Relabel,
    RerunFailedChecks,
    Target,
    UpdateBranch,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.world import IssueView, Comment
from issue_agent.domain.triage import TriageConfig, TriageVerdict, close_actions


class Gateway:
    def __init__(self):
        self.labels = set()
        self.comments = set()
        self.calls = []
        self.head = "head-1"
        self.issue_revision = "issue-rev-1"
        self.auto_merge = set()
        self.run_attempt = 0
        self.run_states = {}
        self.dependencies = set()
        self.reviews = set()
        self.advance_before_auto_merge = ""
        self.advance_before_update = ""
        self.advance_before_review = ""
        self.issue_closures = {}

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
        return SimpleNamespace(head_ref_oid=self.head)

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

    def add_blocked_by(self, issue, blocker):
        self.calls.append(("dependency", issue, blocker))
        self.dependencies.add((issue, blocker))

    def has_blocked_by(self, issue, blocker):
        return (issue, blocker) in self.dependencies

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


def _executor(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    gateway = Gateway()
    return (
        GitHubEffectExecutor(
            gateway,
            store,
            repository="o/r",
            identity="o/r:fix",
        ),
        gateway,
        store,
    )


def test_remote_effect_is_committed_before_execution_and_deduplicated(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = Relabel(Target.ISSUE, 4, Label.AI_READY, Label.AI_WORKING)

    executor.execute(action)
    executor.execute(action)

    assert gateway.calls == [
        ("relabel", "issue", 4, "ai-ready", "ai-working")
    ]
    assert store.pending_commands() == ()


def test_owner_retirement_before_apply_fences_github_mutation(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    successor = SQLiteControlStore(store.path)
    successor.initialize()
    old_owner = "o/r:fix"
    new_owner = "o/r:fix:deployment-2"
    original = gateway.has_label
    retired = False

    def retire_before_apply(kind, number, label):
        nonlocal retired
        if not retired:
            successor.adopt_legacy_owner(
                "o/r",
                "fix",
                new_owner,
                prior_owner_ids=(old_owner,),
            )
            retired = True
        return original(kind, number, label)

    gateway.has_label = retire_before_apply

    with pytest.raises(RetiredOwnerError, match="deployment-2"):
        executor.execute(
            Relabel(Target.ISSUE, 4, Label.AI_READY, Label.AI_WORKING)
        )

    assert gateway.calls == []
    assert successor.pending_commands(
        repository="o/r",
        executor_identity=new_owner,
    )


def test_reversed_effect_gets_a_new_durable_occurrence(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = Relabel(Target.ISSUE, 4, Label.AI_READY, Label.AI_WORKING)

    assert executor.execute(action)
    gateway.labels.discard(("issue", 4, "ai-working"))
    gateway.labels.add(("issue", 4, "ai-ready"))
    assert executor.execute(action)

    assert gateway.calls == [
        ("relabel", "issue", 4, "ai-ready", "ai-working"),
        ("relabel", "issue", 4, "ai-ready", "ai-working"),
    ]
    assert store.pending_commands() == ()


def test_ambiguous_comment_is_reconciled_before_any_retry(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = PostComment(Target.ISSUE, 4, "done")
    original = gateway.comment

    def committed_then_lost(*args):
        original(*args)
        raise RuntimeError("reply lost")

    gateway.comment = committed_then_lost
    try:
        executor.execute(action)
    except RuntimeError:
        pass
    gateway.comment = original

    executor.execute(action)

    assert gateway.calls == [("comment", "issue", 4, "done")]
    assert store.pending_commands() == ()


def test_close_issue_applies_comment_then_exact_close_state(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)

    assert executor.execute(CloseIssue(4, "completed", "done"))

    assert gateway.calls == [
        ("comment", "issue", 4, "done"),
        ("close", 4, "completed", ""),
    ]
    assert gateway.issue_closure(4) == (True, "completed")
    assert gateway.has_comment("issue", 4, "done")
    assert store.pending_commands() == ()


def test_legacy_closed_target_with_missing_explanation_requires_reconciliation(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = CloseIssue(4, "completed", "done")
    command = executor._command(action)
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    gateway.issue_revision = "issue-rev-after-close"
    gateway.issue_closures[4] = (True, "completed")

    with pytest.raises(UnknownEffectOutcome, match="incomplete disposition"):
        executor.execute(action)
    assert gateway.calls == []
    assert not gateway.has_comment("issue", 4, "done")
    assert store.pending_commands()


def test_legacy_partial_close_never_binds_a_humans_newer_hold(tmp_path):
    executor, gateway, store = _executor(tmp_path)
    action = CloseIssue(4, "completed", "done")
    command = executor._command(action)
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    gateway.comments.add(("issue", 4, "done"))
    gateway.labels.add(("issue", 4, Label.NEEDS_HUMAN_REVIEW.value))
    gateway.issue_revision = "new-human-hold"

    with pytest.raises(UnknownEffectOutcome, match="legacy close"):
        executor.execute(action)

    assert gateway.calls == []
    assert gateway.issue_closure(4) == (False, "")
    assert store.pending_commands()


def test_partial_close_recovery_finishes_close_after_comment_ack_is_lost(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = CloseIssue(4, "completed", "done")
    original = gateway.comment

    def committed_then_lost(*args):
        original(*args)
        gateway.issue_revision = "issue-rev-after-comment"
        raise RuntimeError("comment reply lost")

    gateway.comment = committed_then_lost
    with pytest.raises(RuntimeError, match="comment reply lost"):
        executor.execute(action)
    gateway.comment = original

    assert executor.execute(action)

    assert gateway.calls == [
        ("comment", "issue", 4, "done"),
        ("close", 4, "completed", ""),
    ]
    assert gateway.issue_closure(4) == (True, "completed")
    assert gateway.has_comment("issue", 4, "done")
    assert store.pending_commands() == ()


def test_close_issue_reconciliation_requires_reason_and_comment(
    tmp_path,
) -> None:
    executor, gateway, _ = _executor(tmp_path)
    action = CloseIssue(4, "completed", "done")
    gateway.issue_closures[4] = (True, "not_planned")
    gateway.comments.add(("issue", 4, "done"))
    assert executor._reconcile(action) is False

    gateway.issue_closures[4] = (True, "completed")
    gateway.comments.clear()
    assert executor._reconcile(action) is False

    gateway.comments.add(("issue", 4, "done"))
    assert executor._reconcile(action) is True


def test_legacy_unknown_comment_is_adopted_and_reconciled_without_duplicate(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = PostComment(Target.ISSUE, 4, "done")
    original = gateway.comment

    def committed_then_lost(*args):
        original(*args)
        raise RuntimeError("reply lost")

    gateway.comment = committed_then_lost
    with pytest.raises(RuntimeError, match="reply lost"):
        executor.execute(action)
    store.adopt_legacy_owner("o/r", "fix", "o/r:fix:deployment-2")
    gateway.comment = original
    adopted = GitHubEffectExecutor(
        gateway,
        store,
        repository="o/r",
        identity="o/r:fix:deployment-2",
    )

    assert adopted.drain_pending_commands() == 1
    assert gateway.calls == [("comment", "issue", 4, "done")]
    assert store.pending_commands() == ()


def test_new_review_identity_reconciles_matching_legacy_review_before_submit(
    tmp_path,
) -> None:
    _, gateway, store = _executor(tmp_path)
    gateway.reviews.add((7, "APPROVE", "looks good", "head-1"))
    adopted = GitHubEffectExecutor(
        gateway,
        store,
        repository="o/r",
        identity="o/r:review:deployment-2",
    )

    assert adopted.submit_review(7, "APPROVE", "looks good", "head-1")
    assert gateway.calls == []
    assert store.pending_commands() == ()


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


def test_expected_revision_blocks_stale_effect(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    gateway.head = "new-head"

    with pytest.raises(StaleExpectedRevision):
        executor.execute(RerunFailedChecks(7, "old-head", (42,)))

    assert gateway.calls == []
    assert store.pending_commands() == ()


@pytest.mark.parametrize(
    "action",
    (
        Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_WORKING),
        PostComment(Target.ISSUE, 7, "work complete"),
        AddDependency(7, 8),
    ),
)
def test_issue_lifecycle_mutations_capture_expected_revision(
    tmp_path,
    action,
) -> None:
    executor, _, store = _executor(tmp_path)
    command = executor._command(action)

    assert command.target == "issue:7"
    assert command.expected_revision == "issue-rev-1"
    assert store.pending_commands() == ()


def test_issue_revision_change_blocks_stale_comment(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = PostComment(Target.ISSUE, 7, "stale")
    store.enqueue_command(
        command_for_action(
            action,
            expected_revision="issue-rev-1",
            repository="o/r",
            executor_identity="o/r:fix",
        )
    )
    gateway.issue_revision = "issue-rev-2"

    with pytest.raises(StaleExpectedRevision):
        executor.execute(action)
    assert gateway.calls == []
    assert store.pending_commands() == ()


def test_stale_claim_cannot_reconcile_from_newer_matching_labels(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    gateway.labels.add(("issue", 7, "ai-working"))
    gateway.issue_revision = "issue-rev-2"
    action = Relabel(
        Target.ISSUE,
        7,
        Label.AI_READY,
        Label.AI_WORKING,
        "issue-rev-1",
    )

    with pytest.raises(StaleExpectedRevision):
        executor.execute(action)
    assert gateway.calls == []
    assert store.pending_commands() == ()


def test_pr_relabel_captures_and_enforces_head_revision(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = Relabel(
        Target.PR,
        7,
        Label.AI_PR_OPEN,
        Label.NEEDS_HUMAN_REVIEW,
    )
    store.enqueue_command(
        command_for_action(
            action,
            expected_revision="head-1",
            repository="o/r",
            executor_identity="o/r:fix",
        )
    )
    gateway.head = "head-2"

    with pytest.raises(StaleExpectedRevision):
        executor.execute(action)
    assert gateway.calls == []
    assert store.pending_commands() == ()


def test_ambiguous_auto_merge_is_reconciled_without_duplicate(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = EnableAutoMerge(7, "squash", "head-1")
    original = gateway.enable_auto_merge

    def committed_then_lost(*args):
        original(*args)
        raise RuntimeError("reply lost")

    gateway.enable_auto_merge = committed_then_lost
    try:
        executor.execute(action)
    except RuntimeError:
        pass
    gateway.enable_auto_merge = original

    assert executor.execute(action)
    assert gateway.calls == [("auto_merge", 7, "squash", "head-1")]
    assert store.pending_commands() == ()


def test_ambiguous_branch_update_uses_original_revision_to_reconcile(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = UpdateBranch(7, "head-1")
    original = gateway.update_branch

    def committed_then_lost(*args):
        original(*args)
        raise RuntimeError("reply lost")

    gateway.update_branch = committed_then_lost
    try:
        executor.execute(action)
    except RuntimeError:
        pass
    gateway.update_branch = original

    assert executor.execute(action)
    assert gateway.calls == [("update_branch", 7, "head-1")]
    assert store.pending_commands() == ()


def test_ambiguous_check_rerun_is_reconciled_by_attempt_number(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = RerunFailedChecks(7, "head-1", (42,))
    original = gateway.rerun_failed_checks

    def committed_then_lost(*args):
        original(*args)
        raise RuntimeError("reply lost")

    gateway.rerun_failed_checks = committed_then_lost
    try:
        executor.execute(action)
    except RuntimeError:
        pass
    gateway.rerun_failed_checks = original
    gateway.advance_run_attempts()  # GitHub started the queued re-run

    assert executor.execute(action)
    assert gateway.calls == [("rerun", "head-1", (42,))]
    assert store.pending_commands() == ()


def test_partially_applied_multi_run_rerun_is_never_blindly_retried(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = RerunFailedChecks(7, "head-1", (42, 43))
    advanced = False

    def attempts(_head, run_ids):
        return {
            run_id: (1 if advanced and run_id == 42 else 0)
            for run_id in run_ids
        }

    def partial(_head, _run_ids, _expected_attempts):
        nonlocal advanced
        advanced = True
        gateway.calls.append(("rerun", "head-1", (42,)))
        raise RuntimeError("reply lost after first run")

    gateway.workflow_run_attempts = attempts
    gateway.rerun_failed_checks = partial

    with pytest.raises(RuntimeError, match="reply lost"):
        executor.execute(action)
    with pytest.raises(RuntimeError, match="outcome remains unknown"):
        executor.execute(action)

    assert gateway.calls == [("rerun", "head-1", (42,))]
    assert store.pending_commands()[0].state == "unknown_outcome"


def test_accepted_check_rerun_succeeds_before_the_attempt_counter_moves(
    tmp_path,
) -> None:
    """#177: GitHub queues the re-run and bumps `run_attempt` seconds later.
    Acceptance is the postcondition; convergence is observed from the world."""
    executor, gateway, store = _executor(tmp_path)

    assert executor.execute(RerunFailedChecks(7, "head-1", (42,))) is True

    assert gateway.calls == [("rerun", "head-1", (42,))]
    assert gateway.run_attempt == 0
    assert store.pending_commands() == ()


def test_refused_check_rerun_is_terminal_for_this_head(tmp_path) -> None:
    """The free re-run's one attempt per head is spent by the request itself,
    so a refusal must not be redriven next tick (ADR 0012 ceiling)."""
    executor, gateway, store = _executor(tmp_path)
    action = RerunFailedChecks(7, "head-1", (42,))
    original = gateway.rerun_failed_checks
    gateway.rerun_failed_checks = (
        lambda _head, _run_ids, _expected_attempts: None
    )

    assert executor.execute(action) is False
    assert store.pending_commands() == ()

    gateway.rerun_failed_checks = original
    assert executor.execute(action) is False
    assert gateway.calls == []


def test_an_unreadable_run_observation_mints_no_rerun_command(tmp_path) -> None:
    """Fail closed: with no observation at all there is nothing to verify
    against, so no command exists for a later tick to redrive."""
    executor, gateway, store = _executor(tmp_path)
    gateway.workflow_run_attempts = lambda _head, _run_ids: None

    with pytest.raises(UnknownEffectOutcome):
        executor.execute(RerunFailedChecks(7, "head-1", (42,)))

    assert gateway.calls == []
    assert store.pending_commands() == ()


@pytest.mark.parametrize(
    "attempts",
    (
        lambda _head, run_ids: {r: "3" for r in run_ids},   # malformed payload
        lambda _head, _run_ids: {},                         # nothing at head
    ),
)
def test_a_malformed_run_observation_refuses_the_rerun_without_a_loop(
    tmp_path, attempts
) -> None:
    """#177: a baseline that cannot be parsed cannot become parseable, so the
    re-run is refused terminally instead of parking in `unknown_outcome` and
    re-raising on every drain."""
    executor, gateway, store = _executor(tmp_path)
    gateway.workflow_run_attempts = attempts

    assert executor.execute(RerunFailedChecks(7, "head-1", (42,))) is False

    assert gateway.calls == []
    assert store.pending_commands() == ()


def test_rejected_mutation_does_not_receive_a_success_receipt(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = Relabel(Target.ISSUE, 4, Label.AI_READY, Label.AI_WORKING)

    def rejected(kind, number, remove, add):
        gateway.calls.append(("relabel", kind, number, remove, add))

    gateway.relabel = rejected

    assert executor.execute(action) is False
    assert store.pending_commands()[0].state == "retryable_failure"
    with store._connect() as db:
        status = db.execute(
            "SELECT status FROM effect_receipts"
        ).fetchone()[0]
    assert status == "retryable_failure"


def test_revision_outage_remains_pending_instead_of_permanently_failing(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("GitHub unavailable")

    gateway.get_pr = unavailable

    with pytest.raises(RuntimeError, match="unavailable"):
        executor.execute(RerunFailedChecks(7, "head-1", (42,)))

    pending = store.pending_commands()
    assert len(pending) == 1
    assert pending[0].state == "pending"


def test_reconcile_outage_blocks_write_and_leaves_command_pending(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = Relabel(Target.ISSUE, 4, Label.AI_READY, Label.AI_WORKING)

    def unavailable(*_args):
        raise RuntimeError("readback unavailable")

    gateway.has_label = unavailable

    with pytest.raises(RuntimeError, match="readback unavailable"):
        executor.execute(action)

    assert store.pending_commands()[0].state == "pending"
    assert gateway.calls == []


def test_ambiguous_dependency_is_reconciled_without_duplicate(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = AddDependency(7, 8)
    original = gateway.add_blocked_by

    def committed_then_lost(*args):
        original(*args)
        raise RuntimeError("reply lost")

    gateway.add_blocked_by = committed_then_lost
    try:
        executor.execute(action)
    except RuntimeError:
        pass
    gateway.add_blocked_by = original

    assert executor.execute(action)
    assert gateway.calls == [("dependency", 7, 8)]
    assert store.pending_commands() == ()


def test_ambiguous_review_and_same_body_on_new_head_are_distinct(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    original = gateway.submit_review

    def committed_then_lost(*args):
        original(*args)
        raise RuntimeError("reply lost")

    gateway.submit_review = committed_then_lost
    try:
        executor.submit_review(7, "APPROVE", "looks good", "head-1")
    except RuntimeError:
        pass
    gateway.submit_review = original

    assert executor.submit_review(7, "APPROVE", "looks good", "head-1")
    gateway.head = "head-2"
    assert executor.submit_review(7, "APPROVE", "looks good", "head-2")

    assert gateway.calls == [
        ("review", 7, "APPROVE", "looks good", "head-1"),
        ("review", 7, "APPROVE", "looks good", "head-2"),
    ]
    assert store.pending_commands() == ()


@pytest.mark.parametrize(
    ("action", "race_attribute"),
    (
        (
            EnableAutoMerge(7, "squash", "head-1"),
            "advance_before_auto_merge",
        ),
        (UpdateBranch(7, "head-1"), "advance_before_update"),
    ),
)
def test_pr_mutation_server_guard_rejects_check_to_write_head_race(
    tmp_path,
    action,
    race_attribute,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    setattr(gateway, race_attribute, "head-2")

    assert executor.execute(action) is False

    assert gateway.calls == []
    with store._connect() as db:
        statuses = {
            row[0]
            for row in db.execute("SELECT status FROM effect_receipts")
        }
    assert "succeeded" not in statuses


def test_review_server_guard_rejects_check_to_write_head_race(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    gateway.advance_before_review = "head-2"

    assert executor.submit_review(
        7, "APPROVE", "looks good", "head-1"
    ) is False

    assert gateway.calls == []
    with store._connect() as db:
        assert db.execute(
            "SELECT status FROM effect_receipts"
        ).fetchone()[0] == "permanent_failure"


def test_boot_drain_executes_committed_pending_command(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = PostComment(Target.ISSUE, 4, "recover me")
    store.enqueue_command(
        command_for_action(
            action,
            repository="o/r",
            executor_identity="o/r:fix",
        )
    )

    assert executor.drain_pending_commands() == 1

    assert gateway.calls == [("comment", "issue", 4, "recover me")]
    assert store.pending_commands() == ()


@pytest.mark.parametrize(
    "action",
    (
        EnableAutoMerge(7, "squash", "head-1"),
        UpdateBranch(7, "head-1"),
    ),
)
def test_boot_drain_rehydrates_legacy_pr_command_head(
    tmp_path, action
) -> None:
    executor, gateway, store = _executor(tmp_path)
    command = command_for_action(
        action,
        repository="o/r",
        executor_identity="o/r:fix",
    )
    legacy_payload = {
        key: value
        for key, value in command.payload.items()
        if key != "head"
    }
    store.enqueue_command(replace(command, payload=legacy_payload))

    assert executor.drain_pending_commands() == 1
    assert gateway.calls
    assert gateway.calls[0][-1] == "head-1"
    assert store.pending_commands() == ()


def test_pending_command_records_the_config_snapshot(tmp_path) -> None:
    _, _, store = _executor(tmp_path)
    command = command_for_action(
        PostComment(Target.ISSUE, 7, "hello"),
        config_version="cfg-123",
        repository="o/r",
        executor_identity="o/r:fix",
    )

    store.enqueue_command(command)

    assert store.pending_commands()[0].command.config_version == "cfg-123"


def test_boot_drain_cannot_claim_another_repository_or_role_command(
    tmp_path,
) -> None:
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    action = PostComment(Target.ISSUE, 4, "owned")
    store.enqueue_command(
        command_for_action(
            action,
            repository="a/repo",
            executor_identity="a/repo:review",
        )
    )
    wrong_repo_gateway = Gateway()
    wrong_role_gateway = Gateway()
    owner_gateway = Gateway()
    wrong_repo = GitHubEffectExecutor(
        wrong_repo_gateway,
        store,
        repository="b/repo",
        identity="b/repo:review",
    )
    wrong_role = GitHubEffectExecutor(
        wrong_role_gateway,
        store,
        repository="a/repo",
        identity="a/repo:fix",
    )
    owner = GitHubEffectExecutor(
        owner_gateway,
        store,
        repository="a/repo",
        identity="a/repo:review",
    )

    assert wrong_repo.drain_pending_commands() == 0
    assert wrong_role.drain_pending_commands() == 0
    assert wrong_repo_gateway.calls == []
    assert wrong_role_gateway.calls == []
    assert owner.drain_pending_commands() == 1
    assert owner_gateway.calls == [("comment", "issue", 4, "owned")]


# ── steward triage `close` disposition (keifu #143) ───────────────────────────
def test_steward_close_actions_are_durable_commands_with_receipts(tmp_path) -> None:
    """The close is a remote lifecycle mutation, so it rides the same durable
    command + receipt path as every other relabel/comment — not a direct gh call."""
    executor, gateway, store = _executor(tmp_path)
    verdict = TriageVerdict(
        verdict="close",
        close_reason="PR #170 already ships this.",
        reopen_if="the export still fails after PR #170",
        close_state_reason="completed",
    )

    actions = close_actions(143, "alice", verdict, TriageConfig(steward_bot="sb"))
    for action in actions:
        assert executor.execute(action)

    assert gateway.calls == [
        ("comment", "issue", 143, actions[0].comment),
        ("relabel", "issue", 143, None, "ai-closed"),
        ("relabel", "issue", 143, "ai-triage", None),
        ("close", 143, "completed", ""),
    ]
    assert gateway.issue_closure(143) == (True, "completed")
    assert store.pending_commands() == ()
    with store._connect() as db:
        kinds = tuple(
            row[0]
            for row in db.execute(
                "SELECT kind FROM lifecycle_commands ORDER BY occurrence"
            )
        )
        statuses = tuple(
            row[0] for row in db.execute("SELECT status FROM effect_receipts")
        )
    assert kinds == ("close_issue",)
    assert statuses == ("succeeded",)


def test_a_replayed_steward_close_neither_re_comments_nor_re_closes(tmp_path) -> None:
    """A daemon restart mid-close must converge, not post a second obituary."""
    executor, gateway, store = _executor(tmp_path)
    verdict = TriageVerdict(
        verdict="close",
        close_reason="Duplicate of #12.",
        reopen_if="#12 is closed without covering this",
    )
    actions = close_actions(143, "alice", verdict, TriageConfig(steward_bot="sb"))

    for _ in range(2):
        for action in actions:
            executor.execute(action)

    assert gateway.calls == [
        ("comment", "issue", 143, actions[0].comment),
        ("relabel", "issue", 143, None, "ai-closed"),
        ("relabel", "issue", 143, "ai-triage", None),
        ("close", 143, "not_planned", ""),
    ]

# ── wedged-run cancel (keifu PR #177) ────────────────────────────────────────
def test_cancelling_wedged_runs_is_receipted_like_any_other_remote_mutation(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)

    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,))) is True
    assert gateway.calls == [("cancel", "head-1", (42,))]
    assert store.pending_commands() == ()


def test_a_lost_cancel_reply_replays_safely_and_settles(tmp_path) -> None:
    # A cancel whose reply was lost cannot be told from one never sent, so the
    # replay re-issues it. That is safe by construction: the gateway re-verifies
    # the head SHA and skips runs GitHub has already completed, so the repeat can
    # neither touch a newer push's run nor 409 on a finished one.
    executor, gateway, store = _executor(tmp_path)
    action = CancelWorkflowRuns(7, "head-1", (42,))
    original = gateway.cancel_workflow_runs

    def committed_then_lost(*args):
        original(*args)
        raise RuntimeError("reply lost")

    gateway.cancel_workflow_runs = committed_then_lost
    with pytest.raises(RuntimeError):
        executor.execute(action)
    gateway.cancel_workflow_runs = original

    assert executor.execute(action)
    assert gateway.calls == [
        ("cancel", "head-1", (42,)), ("cancel", "head-1", (42,))
    ]
    assert store.pending_commands() == ()

    # Once GitHub has taken it down, nothing further is issued.
    gateway.run_states[42] = "completed"
    assert executor.execute(action)
    assert len(gateway.calls) == 2


def test_a_run_github_already_completed_needs_no_cancel_at_all(tmp_path) -> None:
    # Reconcile-first: the run is already in the state the command wants, so it
    # is receipted without a POST.
    executor, gateway, store = _executor(tmp_path)
    gateway.run_states[42] = "completed"

    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,))) is True
    assert gateway.calls == []
    assert store.pending_commands() == ()


def test_a_cancel_github_has_not_finished_yet_is_still_a_success(tmp_path) -> None:
    # The regression this exists for (adversarial review S1): GitHub takes a
    # cancelled run down asynchronously. Verifying "the run is down" right after
    # the POST failed EVERY successful cancel — re-POSTing it to the retry
    # ceiling and raising an operator alert that pointed at token scopes.
    executor, gateway, store = _executor(tmp_path)

    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,))) is True
    assert gateway.calls == [("cancel", "head-1", (42,))]
    assert store.pending_commands() == ()

    # …and the run coming down later does not re-issue anything.
    gateway.run_states[42] = "completed"
    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,))) is True
    assert gateway.calls == [("cancel", "head-1", (42,))]


def test_a_run_from_a_newer_head_never_reaches_a_cancel_command(tmp_path) -> None:
    executor, gateway, _store = _executor(tmp_path)

    with pytest.raises(UnknownEffectOutcome):
        executor.execute(CancelWorkflowRuns(7, "stale-head", (42,)))

    assert gateway.calls == []


def test_a_cancel_without_run_identities_is_refused(tmp_path) -> None:
    executor, gateway, _store = _executor(tmp_path)

    with pytest.raises(UnknownEffectOutcome):
        executor.execute(CancelWorkflowRuns(7, "head-1", ()))

    assert gateway.calls == []


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
