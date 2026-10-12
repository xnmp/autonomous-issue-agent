
from tests.domain.builders import observed_pr
from issue_agent.domain.checks import Check
from dataclasses import replace
from types import SimpleNamespace

import pytest

from issue_agent.adapters.control_store import (
    RetiredOwnerError,
    SQLiteControlStore,
)
from issue_agent.adapters.effects import (
    GitHubEffectExecutor,
    ImmediateGitHubEffectExecutor,
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
from issue_agent.domain.fix import FixConfig
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
        self.dependencies = set()
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


def _executor(tmp_path, *, ci=False, legacy_review=False, now=None):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=now)
    store.initialize()
    gateway = Gateway()
    if ci:
        gateway.ci_state = "failure"
    from issue_agent.adapters.store import Store
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    if not legacy_review:
        legacy = Store(str(tmp_path / "state"))
        object.__setattr__(legacy, "_workflow_db", store)
        install_pr_repair_authority(SimpleNamespace(prs={7: observed_pr(7, head_ref_oid="head-1")}), legacy)
    return (
        GitHubEffectExecutor(
            gateway,
            store,
            repository="o/r",
            identity="o/r:fix",
            fix_config=FixConfig(fix_bot="fix-bot", ci_free_rerun=False),
        ),
        gateway,
        store,
    )


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


def test_remote_effect_is_committed_before_execution_and_deduplicated(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = Relabel(Target.ISSUE, 4, Label.AI_READY, Label.AI_WORKING)

    executor.execute(action)
    executor.execute(action)

    assert gateway.calls == [
        ("relabel", "issue", 4, "ai-ready", "ai-working")
    ]
    assert store.pending_commands() == ()


def test_compound_relabel_recovers_after_comment_lands_before_reply_is_lost(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = _compound_projection(gateway)
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
        ("comment", "issue", 4, action.comment),
        ("relabel", "issue", 4, None, "recurrence"),
        ("relabel", "issue", 4, None, "maybe-resolved"),
        ("relabel", "issue", 4, "ai-working", None),
    ]
    assert store.pending_commands() == ()
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM lifecycle_commands"
        ).fetchone()[0] == 1


def test_compound_relabel_recovers_only_missing_labels_after_partial_apply(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = _compound_projection(gateway)
    original = gateway.relabel

    def first_label_then_lost(kind, number, remove, add):
        original(kind, number, remove, add)
        if add == Label.RECURRENCE.value:
            gateway.issue_revision = "issue-rev-after-first-label"
            raise RuntimeError("first label reply lost")

    gateway.relabel = first_label_then_lost
    with pytest.raises(RuntimeError, match="first label reply lost"):
        executor.execute(action)
    gateway.relabel = original

    assert executor.execute(action)
    assert gateway.calls == [
        ("comment", "issue", 4, action.comment),
        ("relabel", "issue", 4, None, "recurrence"),
        ("relabel", "issue", 4, None, "maybe-resolved"),
        ("relabel", "issue", 4, "ai-working", None),
    ]
    assert store.pending_commands() == ()


@pytest.mark.parametrize("changed_fact", ("title", "body", "author", "comments", "hold"))
def test_compound_relabel_refuses_unrelated_human_change_after_partial_apply(
    tmp_path,
    changed_fact,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = _compound_projection(gateway)
    original_comment = gateway.comment

    def committed_then_lost(*args):
        original_comment(*args)
        gateway.issue_revision = "issue-rev-after-comment"
        raise RuntimeError("comment reply lost")

    gateway.comment = committed_then_lost
    with pytest.raises(RuntimeError, match="comment reply lost"):
        executor.execute(action)
    gateway.comment = original_comment

    original_get_issue = gateway.get_issue

    def changed_issue(issue):
        current = original_get_issue(issue)
        if changed_fact == "title":
            return replace(current, title="human retitled")
        if changed_fact == "body":
            return replace(current, body="human edited body")
        if changed_fact == "author":
            return replace(current, author="different-author")
        if changed_fact == "comments":
            return replace(
                current,
                comments=current.comments
                + (Comment("human", "new direction", "later"),),
            )
        return replace(
            current,
            labels=current.labels | {Label.NEEDS_HUMAN_REVIEW},
        )

    gateway.get_issue = changed_issue
    with pytest.raises(StaleExpectedRevision, match="completion facts changed"):
        executor.execute(action)

    assert gateway.calls == [("comment", "issue", 4, action.comment)]
    assert store.pending_commands()[0].state == "unknown_outcome"


def test_immediate_executor_applies_and_verifies_compound_relabel() -> None:
    gateway = Gateway()
    action = _compound_projection(gateway)

    assert ImmediateGitHubEffectExecutor(gateway).execute(action) is True
    assert gateway.calls == [
        ("comment", "issue", 4, action.comment),
        ("relabel", "issue", 4, None, "recurrence"),
        ("relabel", "issue", 4, None, "maybe-resolved"),
        ("relabel", "issue", 4, "ai-working", None),
    ]


def test_immediate_executor_rejects_stale_compound_relabel() -> None:
    gateway = Gateway()
    action = _compound_projection(gateway)
    gateway.issue_revision = "human-edit"

    with pytest.raises(StaleExpectedRevision, match="expected revision"):
        ImmediateGitHubEffectExecutor(gateway).execute(action)

    assert gateway.calls == []


def test_compound_relabel_requires_every_postcondition_before_success(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = _compound_projection(gateway)
    original = gateway.relabel

    def drop_second_add(kind, number, remove, add):
        if add == Label.MAYBE_RESOLVED.value:
            gateway.calls.append(("relabel", kind, number, remove, add))
            return
        original(kind, number, remove, add)

    gateway.relabel = drop_second_add

    assert executor.execute(action) is False
    assert store.pending_commands()[0].state == "retryable_failure"


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
    _, gateway, store = _executor(tmp_path, legacy_review=True)
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


def test_expected_revision_blocks_stale_effect(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path, ci=True)
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


@pytest.mark.parametrize("escalation", [False, True])
def test_pr_recovery_receipt_bounds_same_head_after_remote_projection_restoration(tmp_path, escalation):
    from issue_agent.domain.actions import EscalateCiPending
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending" if escalation else "clear"
    gateway.merge_state = "BEHIND"
    action = EscalateCiPending(7, "head-1", "stalled CI requires a human") if escalation else UpdateBranch(7, "head-1", "base-1")
    assert executor.execute(action)
    before = list(gateway.calls)
    assert before
    gateway.head = "head-1"
    gateway.labels.clear()
    restarted = GitHubEffectExecutor(gateway, store, repository="o/r", identity="o/r:fix", fix_config=executor.fix_config)
    assert restarted.execute(action)
    assert gateway.calls == before


@pytest.mark.parametrize("escalation", [False, True])
def test_lost_pr_recovery_success_reconciles_once_and_new_identity_remains_eligible(tmp_path, escalation):
    from issue_agent.domain.actions import EscalateCiPending
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending" if escalation else "clear"
    gateway.merge_state = "BEHIND"
    action = EscalateCiPending(7, "head-1", "stalled") if escalation else UpdateBranch(7, "head-1", "base-1")
    name = "relabel" if escalation else "update_branch"
    original = getattr(gateway, name)
    def lost(*args):
        original(*args)
        raise RuntimeError("PR recovery success reply lost")
    setattr(gateway, name, lost)
    with pytest.raises(RuntimeError, match="reply lost"):
        executor.execute(action)
    field = "pending_ci_escalation" if escalation else "branch_refresh"
    key = WorkflowKey("o/r", PullRequestRef(7))
    owed = getattr(store.read_pr_repair(key).component.state, field)
    assert isinstance(owed, cc.OwedPRRecoveryEffect)
    before = list(gateway.calls)
    setattr(gateway, name, original)
    restarted = GitHubEffectExecutor(gateway, store, repository="o/r", identity="o/r:fix", fix_config=executor.fix_config)
    assert restarted.drain_pending_commands() == 1
    completed = getattr(store.read_pr_repair(key).component.state, field)
    assert isinstance(completed, cc.SettledPRRecoveryEffect) and completed.owed == owed
    assert restarted.execute(action)
    assert gateway.calls == before
    gateway.head = "head-2" if escalation else "head-1"
    gateway.labels.clear()
    next_action = EscalateCiPending(7, "head-2", "new head stalled") if escalation else UpdateBranch(7, "head-1", "base-2")
    assert restarted.execute(next_action)
    newer = getattr(store.read_pr_repair(key).component.state, field)
    assert isinstance(newer, cc.SettledPRRecoveryEffect) and newer.owed.command_id != owed.command_id
    assert len(gateway.calls) > len(before)


@pytest.mark.parametrize("escalation", [False, True])
def test_stale_pr_recovery_receipt_cannot_complete_replacement_head_or_base(tmp_path, escalation):
    from issue_agent.adapters.durable_effects import EffectOutcome
    from issue_agent.domain.contracts import EffectStatus
    from issue_agent.domain.actions import EscalateCiPending
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending" if escalation else "clear"
    gateway.merge_state = "BEHIND"
    action = EscalateCiPending(7, "head-1", "stalled") if escalation else UpdateBranch(7, "head-1", "base-1")
    old_command = executor._command(action)
    store.enqueue_command(old_command)
    store.begin_command(old_command.command_id)
    gateway.head = "head-2" if escalation else "head-1"
    newer_action = EscalateCiPending(7, "head-2", "new stalled") if escalation else UpdateBranch(7, "head-1", "base-2")
    assert executor.execute(newer_action)
    key = WorkflowKey("o/r", PullRequestRef(7))
    before = store.read_pr_repair(key)
    assert executor.effects.resolve_unknown(old_command, EffectOutcome(True, EffectStatus.SUCCEEDED))
    assert store.read_pr_repair(key) == before


@pytest.mark.parametrize("escalation", [False, True])
def test_authorized_successor_receipts_original_pr_recovery_without_remote_repeat(tmp_path, escalation):
    from issue_agent.domain.actions import EscalateCiPending
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending" if escalation else "clear"
    gateway.merge_state = "BEHIND"
    action = EscalateCiPending(7, "head-1", "stalled") if escalation else UpdateBranch(7, "head-1", "base-1")
    name = "relabel" if escalation else "update_branch"
    original = getattr(gateway, name)
    def lost(*args):
        original(*args)
        raise RuntimeError("PR recovery success reply lost")
    setattr(gateway, name, lost)
    with pytest.raises(RuntimeError, match="reply lost"):
        executor.execute(action)
    before = list(gateway.calls)
    setattr(gateway, name, original)
    store.adopt_legacy_owner("o/r", "fix", "o/r:fix:successor", prior_owner_ids=("o/r:fix",))
    with pytest.raises(RetiredOwnerError):
        executor.execute(action)
    successor = GitHubEffectExecutor(gateway, store, repository="o/r", identity="o/r:fix:successor")
    assert successor.drain_pending_commands() == 1
    assert successor.execute(action)
    assert gateway.calls == before


@pytest.mark.parametrize("refresh", [False, True])
def test_terminal_pr_cleanup_cannot_drop_unsettled_remediation_evidence(tmp_path, refresh):
    from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef, WorkflowWriter
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "clear" if refresh else "failure"
    gateway.merge_state = "BEHIND"
    action = UpdateBranch(7, "head-1", "base-1") if refresh else RerunFailedChecks(7, "head-1", (42,))
    store.enqueue_command(executor._command(action))
    key = WorkflowKey("o/r", PullRequestRef(7))
    before = store.read_pr_repair(key)
    authority = SQLiteWorkflowControl(store, WorkflowWriter("o/r", "fix", "o/r:fix"), resolve_ceiling=3)
    from issue_agent.domain import continuation_components as cc
    source = cc.PRRepairCleanup(before.binding, before.revision, "head-1", True)
    with pytest.raises(WorkflowAuthorityError, match="unsettled"):
        authority.cleanup_pr_repair(key, True, source)
    assert store.read_pr_repair(key) == before
    assert len(store.pending_commands()) == 1


def test_ambiguous_branch_update_uses_original_revision_to_reconcile(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "clear"
    gateway.merge_state = "BEHIND"
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


@pytest.mark.parametrize("cancel", [False, True])
def test_late_ci_receipt_cannot_replace_new_head_and_changed_run_set_cannot_replay(tmp_path, cancel):
    from issue_agent.adapters.durable_effects import EffectOutcome
    from issue_agent.domain.contracts import EffectStatus
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending" if cancel else "failure"
    make = CancelWorkflowRuns if cancel else RerunFailedChecks
    name = "cancel_workflow_runs" if cancel else "rerun_failed_checks"
    field = "ci_cancel" if cancel else "ci_rerun"
    original = getattr(gateway, name)
    def lost(*args):
        accepted = original(*args)
        assert accepted is not None
        raise RuntimeError("original accepted CI reply lost")
    setattr(gateway, name, lost)
    with pytest.raises(RuntimeError, match="reply lost"):
        executor.execute(make(7, "head-1", (42,)))
    (old,) = store.pending_commands()
    setattr(gateway, name, original)
    with pytest.raises(ValueError, match="another exact run set"):
        executor.execute(make(7, "head-1", (43,)))
    gateway.head = "head-2"
    assert executor.execute(make(7, "head-2", (43,)))
    key = WorkflowKey("o/r", PullRequestRef(7))
    current = store.read_pr_repair(key).component.state
    assert isinstance(getattr(current, field), cc.SettledCIRemediation)
    assert getattr(current, field).owed.capture.head == "head-2"
    assert executor.effects.resolve_unknown(old.command, EffectOutcome(True, EffectStatus.SUCCEEDED))
    assert store.read_pr_repair(key).component.state == current
    assert gateway.calls == [("cancel" if cancel else "rerun", "head-1", (42,)),
                             ("cancel" if cancel else "rerun", "head-2", (43,))]


@pytest.mark.parametrize("cancel", [False, True])
def test_ci_lost_success_adopts_authorized_owner_and_refuses_retired_owner(tmp_path, cancel):
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending" if cancel else "failure"
    action = CancelWorkflowRuns(7, "head-1", (42,)) if cancel else RerunFailedChecks(7, "head-1", (42,))
    field = "ci_cancel" if cancel else "ci_rerun"
    name = "cancel_workflow_runs" if cancel else "rerun_failed_checks"
    original = getattr(gateway, name)
    def lost(*args):
        original(*args)
        raise RuntimeError("accepted CI effect reply lost")
    setattr(gateway, name, lost)
    with pytest.raises(RuntimeError, match="reply lost"):
        executor.execute(action)
    setattr(gateway, name, original)
    if cancel:
        gateway.run_states[42] = "completed"
    else:
        gateway.advance_run_attempts()
    key = WorkflowKey("o/r", PullRequestRef(7))
    owed = getattr(store.read_pr_repair(key).component.state, field)
    assert isinstance(owed, cc.OwedCIRemediation)
    store.adopt_legacy_owner("o/r", "fix", "o/r:fix:successor", prior_owner_ids=("o/r:fix",))
    with pytest.raises(RetiredOwnerError):
        executor.execute(action)
    assert getattr(store.read_pr_repair(key).component.state, field) == owed
    successor = GitHubEffectExecutor(gateway, store, repository="o/r", identity="o/r:fix:successor")
    assert successor.drain_pending_commands() == 1
    assert successor.execute(action)
    after = getattr(store.read_pr_repair(key).component.state, field)
    assert isinstance(after, cc.SettledCIRemediation) and after.owed == owed
    assert after.receipt.executor_identity == "o/r:fix:successor"
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("settling", [False, True])
@pytest.mark.parametrize("refresh", [False, True])
def test_caught_ci_projection_failure_rolls_back_command_or_receipt_with_state(tmp_path, monkeypatch, settling, refresh):
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    from issue_agent.domain.contracts import EffectReceipt, EffectStatus
    from datetime import datetime, timezone
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "clear" if refresh else "failure"
    gateway.merge_state = "BEHIND"
    command = executor._command(UpdateBranch(7, "head-1", "base-1") if refresh else RerunFailedChecks(7, "head-1", (42,)))
    key = WorkflowKey("o/r", PullRequestRef(7))
    if settling:
        store.enqueue_command(command)
        store.begin_command(command.command_id)
    before = store.read_pr_repair(key)
    name = ("_settle_pr_recovery" if settling else "_record_pr_recovery") if refresh else ("_settle_ci_remediation" if settling else "_record_ci_remediation")
    original = getattr(store, name)
    def fail_after_projection(*args):
        original(*args)
        raise ValueError("caught CI projection failure")
    monkeypatch.setattr(store, name, fail_after_projection)
    with store.transaction() as db:
        with pytest.raises(ValueError, match="caught CI projection"):
            if settling:
                now = datetime.now(timezone.utc)
                receipt = EffectReceipt(command.command_id, "o/r:fix", now, now, EffectStatus.SUCCEEDED)
                store._insert_effect(db, receipt, "injected-receipt")
            else:
                with store.mutation_savepoint(db):
                    store._enqueue_command(db, command)
    assert store.read_pr_repair(key) == before
    assert store.effect_receipt(command.command_id) is None
    pending = store.pending_commands()
    assert len(pending) == int(settling)
    if settling:
        assert pending[0].command.command_id == command.command_id and pending[0].state == "executing"


def test_actual_review_rearm_preserves_original_ci_obligation_and_receipts_it_once(tmp_path):
    from tests.adapters.test_review_workflow_control import Rig, PR_KEY
    from tests.fakes.accounted_workflow import install_pr_repair_authority
    from issue_agent.adapters.store import Store
    from issue_agent.domain.world import PRView
    from issue_agent.domain import continuation_components as cc
    rig = Rig(tmp_path)
    rig.gated()
    legacy = Store(str(tmp_path / "state"))
    object.__setattr__(legacy, "_workflow_db", rig.store)
    install_pr_repair_authority(SimpleNamespace(prs={9: observed_pr(9, head_ref_oid="head-1")}), legacy)
    gateway = Gateway()
    gateway.ci_state = "failure"
    executor = GitHubEffectExecutor(gateway, rig.store, repository="o/r", identity="o/r:fix")
    original = gateway.rerun_failed_checks
    def lost(*args):
        original(*args)
        raise RuntimeError("accepted rerun reply lost before review rearm")
    gateway.rerun_failed_checks = lost
    action = RerunFailedChecks(9, "head-1", (42,))
    with pytest.raises(RuntimeError, match="reply lost"):
        executor.execute(action)
    before = rig.store.read_pr_repair(PR_KEY)
    assert isinstance(before.component.state.ci_rerun, cc.OwedCIRemediation)
    _, accepted = rig.request()
    assert accepted.snapshot.revision.fence.generation == before.revision.fence.generation+1
    assert accepted.snapshot.pr_repair == before.component
    gateway.rerun_failed_checks = original
    gateway.advance_run_attempts()
    assert executor.drain_pending_commands() == 1
    settled = rig.store.read_pr_repair(PR_KEY).component.state.ci_rerun
    assert isinstance(settled, cc.SettledCIRemediation)
    assert settled.owed == before.component.state.ci_rerun
    assert settled.owed.capture.binding.fence == before.revision.fence
    assert executor.execute(action)
    assert gateway.calls == [("rerun", "head-1", (42,))]


def test_ambiguous_check_rerun_is_reconciled_by_attempt_number(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path, ci=True)
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
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.get_pr = lambda number, **kwargs: observed_pr(number, head_ref_oid="head-1", checks=(
        Check("unit", bucket="fail", run_id=42), Check("lint", bucket="fail", run_id=43)))
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
    executor, gateway, store = _executor(tmp_path, ci=True)

    assert executor.execute(RerunFailedChecks(7, "head-1", (42,))) is True

    assert gateway.calls == [("rerun", "head-1", (42,))]
    assert gateway.run_attempt == 0
    assert store.pending_commands() == ()


def test_refused_check_rerun_is_terminal_for_this_head(tmp_path) -> None:
    """The free re-run's one attempt per head is spent by the request itself,
    so a refusal must not be redriven next tick (ADR 0012 ceiling)."""
    executor, gateway, store = _executor(tmp_path, ci=True)
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
    executor, gateway, store = _executor(tmp_path, ci=True)
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
    executor, gateway, store = _executor(tmp_path, ci=True)
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
    executor, gateway, store = _executor(tmp_path, ci=True)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("GitHub unavailable")

    gateway.get_pr = unavailable

    with pytest.raises(RuntimeError, match="unavailable"):
        executor.execute(RerunFailedChecks(7, "head-1", (42,)))

    pending = store.pending_commands()
    assert pending == ()


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
    executor, gateway, store = _executor(tmp_path, legacy_review=True)
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
    executor, gateway, store = _executor(tmp_path, ci=isinstance(action, UpdateBranch))
    gateway.ci_state = "clear"
    gateway.merge_state = "BEHIND" if isinstance(action, UpdateBranch) else "CLEAN"
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
    executor, gateway, store = _executor(tmp_path, legacy_review=True)
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
    gateway.merge_state = "BEHIND" if isinstance(action, UpdateBranch) else "CLEAN"
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


# ── wedged-run cancel (keifu PR #177) ────────────────────────────────────────
def test_cancelling_wedged_runs_is_receipted_like_any_other_remote_mutation(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"

    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,))) is True
    assert gateway.calls == [("cancel", "head-1", (42,))]
    assert store.pending_commands() == ()


def test_a_lost_cancel_reply_replays_safely_and_settles(tmp_path) -> None:
    # A cancel whose reply was lost cannot be told from one never sent, so the
    # replay re-issues it. That is safe by construction: the gateway re-verifies
    # the head SHA and skips runs GitHub has already completed, so the repeat can
    # neither touch a newer push's run nor 409 on a finished one.
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"
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
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"
    gateway.run_states[42] = "completed"

    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,))) is True
    assert gateway.calls == []
    assert store.pending_commands() == ()


def test_a_cancel_github_has_not_finished_yet_is_still_a_success(tmp_path) -> None:
    # The regression this exists for (adversarial review S1): GitHub takes a
    # cancelled run down asynchronously. Verifying "the run is down" right after
    # the POST failed EVERY successful cancel — re-POSTing it to the retry
    # ceiling and raising an operator alert that pointed at token scopes.
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"

    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,))) is True
    assert gateway.calls == [("cancel", "head-1", (42,))]
    assert store.pending_commands() == ()

    # …and the run coming down later does not re-issue anything.
    gateway.run_states[42] = "completed"
    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,))) is True
    assert gateway.calls == [("cancel", "head-1", (42,))]


def test_a_run_from_a_newer_head_never_reaches_a_cancel_command(tmp_path) -> None:
    executor, gateway, _store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"

    with pytest.raises((UnknownEffectOutcome, StaleExpectedRevision)):
        executor.execute(CancelWorkflowRuns(7, "stale-head", (42,)))

    assert gateway.calls == []


def test_a_cancel_without_run_identities_is_refused(tmp_path) -> None:
    executor, gateway, _store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"

    with pytest.raises((UnknownEffectOutcome, StaleExpectedRevision)):
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


@pytest.mark.parametrize("kind", ["merge", "refresh", "rerun", "cancel"])
@pytest.mark.parametrize("held_evidence", ["unavailable", "missing"])
def test_same_head_ci_policy_change_withholds_then_requalifies_existing_command(tmp_path, kind, held_evidence):
    from issue_agent.domain.checks import Observed, RequiredCheck, Unavailable
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "failure" if kind == "rerun" else "pending" if kind == "cancel" else "clear"
    gateway.merge_state = "BEHIND" if kind == "refresh" else "CLEAN"
    action = {"merge": EnableAutoMerge(7, "squash", "head-1"), "refresh": UpdateBranch(7, "head-1"),
        "rerun": RerunFailedChecks(7, "head-1", (42,)), "cancel": CancelWorkflowRuns(7, "head-1", (42,))}[kind]
    command = executor._command(action)
    executor.effects.enqueue(command)
    original = gateway.get_pr
    current = original(7)
    if held_evidence == "unavailable":
        ci = Unavailable("partial paginated report read")
    elif held_evidence == "missing":
        ci = Observed("head-1", "head-1", frozenset({RequiredCheck("never-reported")}), ())
    else:
        ci = Observed("head-1", "head-1", frozenset({RequiredCheck("unit")}),
            (Check("unit", bucket="fail" if kind in {"merge", "refresh"} else "pass", required=True, run_id=43),))
    gateway.get_pr = lambda number, **kwargs: replace(current, ci=ci)
    for _ in range(2):
        with pytest.raises(UnknownEffectOutcome):
            executor.execute(action)
        assert not gateway.calls and store.effect_receipt(command.command_id) is None
    gateway.get_pr = original
    assert executor.execute(action)
    assert executor.execute(action)
    assert len(gateway.calls) == 1
    assert store.effect_receipt(command.command_id).status.value == "succeeded"


@pytest.mark.parametrize("cancel", [False, True])
def test_policy_narrowed_predeploy_ci_debt_settles_and_releases_repair(tmp_path, cancel):
    from issue_agent.domain.contracts import EffectStatus
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.checks import Observed, RequiredCheck
    from issue_agent.domain.fix import FixConfig, FixState, decide_fix
    from issue_agent.domain.actions import RunAgent
    from issue_agent.domain.world import Review, WorldState
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef

    executor, gateway, store = _executor(tmp_path, ci=True)
    current = gateway.get_pr(7)
    state, bucket = ("IN_PROGRESS", "pending") if cancel else ("FAILURE", "fail")
    reports = (Check("unit", state, bucket, run_id=42), Check("advisory", state, bucket, run_id=43))
    old = replace(current, ci=Observed("head-1", "head-1", frozenset(), reports))
    gateway.get_pr = lambda *args, **kwargs: old
    action = CancelWorkflowRuns(7, "head-1", (42, 43)) if cancel else RerunFailedChecks(7, "head-1", (42, 43))
    command = executor._command(action)
    executor.effects.enqueue(command)
    narrowed = replace(old, author="fix-bot", head_ref_name="fix/7-example", closing_issue=7,
        labels=frozenset({Label.AI_PR_OPEN}), mergeable="MERGEABLE",
        reviews=(Review("APPROVED", "review-bot", commit_id="head-1"),),
        ci=Observed("head-1", "head-1", frozenset({RequiredCheck("unit")}),
            (replace(reports[0], required=True), reports[1])))
    gateway.get_pr = lambda *args, **kwargs: narrowed
    for _ in range(3):
        assert executor.drain_pending_commands() == 0
        assert not gateway.calls
        assert store.effect_receipt(command.command_id).status is EffectStatus.PERMANENT_FAILURE
        assert not store.pending_commands()
    field = "ci_cancel" if cancel else "ci_rerun"
    settled = getattr(store.read_pr_repair(WorkflowKey("o/r", PullRequestRef(7))).component.state, field)
    assert isinstance(settled, cc.SettledCIRemediation)
    if not cancel:
        actions = decide_fix(WorldState(prs=(narrowed,)), FixConfig(fix_bot="fix-bot"),
            FixState(ci_remediations={7: (settled, None)}, ci_rerun_head={7: "head-1"}), fixer_busy=False)
        assert any(isinstance(item, RunAgent) and item.kind.name == "FIX_CI" for item in actions)


@pytest.mark.parametrize("state", ["failed", "executing", "queued", "missing"])
@pytest.mark.parametrize("delivery", ["direct", "persisted", "legacy"])
def test_ci_escalation_is_obsolete_without_a_current_domain_escalation(tmp_path, state, delivery):
    from issue_agent.domain.actions import EscalateCiPending
    from issue_agent.domain.checks import Observed, RequiredCheck
    from issue_agent.domain.contracts import EffectStatus
    executor, gateway, store = _executor(tmp_path, ci=True)
    action = EscalateCiPending(7, "head-1", "stalled CI requires a human")
    command = None
    if delivery != "direct":
        gateway.ci_state = "pending"
        command = (executor._command(action) if delivery == "persisted" else
            command_for_action(action, repository="o/r", executor_identity="o/r:fix"))
        executor.effects.enqueue(command)
    current = gateway.get_pr(7)
    reports = () if state == "missing" else (Check("unit",
        state={"failed": "FAILURE", "executing": "IN_PROGRESS", "queued": "QUEUED"}[state],
        bucket="fail" if state == "failed" else "pending", required=True),)
    ci = Observed("head-1", "head-1", frozenset({RequiredCheck("unit")}), reports)
    gateway.get_pr = lambda *args, **kwargs: replace(current, ci=ci)
    if delivery == "direct":
        with pytest.raises(StaleExpectedRevision):
            executor.execute(action)
    else:
        assert command is not None
        assert executor.drain_pending_commands() == 0
        assert store.effect_receipt(command.command_id).status is EffectStatus.PERMANENT_FAILURE
    assert not gateway.calls
    assert not store.pending_commands()


@pytest.mark.parametrize("legacy", [False, True])
def test_persisted_pending_escalation_is_obsolete_with_clear_ci(tmp_path, legacy):
    from issue_agent.domain.contracts import EffectStatus
    from issue_agent.domain.actions import EscalateCiPending
    from issue_agent.domain.checks import Observed, RequiredCheck, Unavailable
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"
    action = EscalateCiPending(7, "head-1", "old advisory run exceeded deadline")
    command = (command_for_action(action, repository="o/r", executor_identity="o/r:fix")
        if legacy else executor._command(action))
    executor.effects.enqueue(command)
    current = gateway.get_pr(7)
    ci = Observed("head-1", "head-1",
        frozenset({RequiredCheck("unit")}), (Check("unit", "SUCCESS", "pass", required=True),))
    gateway.get_pr = lambda *args, **kwargs: replace(current, ci=ci)
    for _ in range(3):
        assert executor.drain_pending_commands() == 0
        assert not gateway.calls
        assert store.effect_receipt(command.command_id).status is EffectStatus.PERMANENT_FAILURE
        assert not store.pending_commands()


@pytest.mark.parametrize("legacy", [False, True])
def test_unavailable_ci_keeps_escalation_owed_until_observation_recovers(tmp_path, legacy):
    from issue_agent.domain.actions import EscalateCiPending
    from issue_agent.domain.checks import Unavailable
    from issue_agent.domain.contracts import EffectStatus
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"
    stalled = gateway.get_pr(7)
    action = EscalateCiPending(7, "head-1", "external CI stalled")
    command = (command_for_action(action, repository="o/r", executor_identity="o/r:fix")
        if legacy else executor._command(action))
    executor.effects.enqueue(command)
    gateway.get_pr = lambda *args, **kwargs: replace(stalled, ci=Unavailable("rate limited"))
    assert executor.drain_pending_commands() == 0
    assert not gateway.calls
    assert store.effect_receipt(command.command_id) is None
    assert any(item.command.command_id == command.command_id for item in store.pending_commands())
    if not legacy:
        selected = store.read_pr_repair(WorkflowKey("o/r", PullRequestRef(7)))
        assert isinstance(selected.component.state.pending_ci_escalation, cc.OwedPRRecoveryEffect)
    gateway.get_pr = lambda *args, **kwargs: stalled
    assert executor.drain_pending_commands() == 1
    assert store.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
    assert gateway.calls == [("comment", "pr", 7, action.body),
        ("relabel", "pr", 7, "ai-pr-open", "needs-human-review")]
    assert not store.pending_commands()


@pytest.mark.parametrize("verdict", ["SUCCESS", "SKIPPED", "empty"])
def test_empty_policy_post_cancel_escalation_is_delivered_and_prevents_merge(tmp_path, verdict):
    from issue_agent.domain.actions import EscalateCiPending
    from issue_agent.domain.checks import Observed
    from issue_agent.domain.fix import FixConfig, FixState, decide_fix
    from issue_agent.domain.world import Review, WorldState
    from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "pending"
    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,), "2026-10-12T01:00:00Z"))
    pr = gateway.get_pr(7)
    reports = () if verdict == "empty" else (Check("unit", verdict, "pass", run_id=42),)
    pr = replace(pr, author="fix-bot", head_ref_name="fix/7-example", closing_issue=7,
        labels=frozenset({Label.AI_PR_OPEN}), mergeable="MERGEABLE", merge_state="CLEAN",
        reviews=(Review("APPROVED", "review-bot", commit_id="head-1"),),
        ci=Observed("head-1", "head-1", frozenset(), reports))
    gateway.get_pr = lambda *args, **kwargs: pr
    world = WorldState(prs=(pr,), now="2026-10-12T02:00:00Z")
    cancel = store.read_pr_repair(WorkflowKey("o/r", PullRequestRef(7))).component.state.ci_cancel.owed.capture
    state = FixState(ci_cancel_head={7: f"{cancel.head}@{cancel.decided_at}"})
    actions = decide_fix(world, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    assert len(actions) == 1 and isinstance(actions[0], EscalateCiPending)
    assert executor.execute(actions[0])
    assert gateway.calls[-2:] == [("comment", "pr", 7, actions[0].body),
        ("relabel", "pr", 7, "ai-pr-open", "needs-human-review")]
    assert not store.pending_commands()


def test_pending_auto_merge_is_withheld_by_same_head_cancel_aftermath(tmp_path):
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.ci_state = "clear"
    action = EnableAutoMerge(7, "squash", "head-1")
    command = executor._command(action)
    executor.effects.enqueue(command)
    gateway.ci_state = "pending"
    assert executor.execute(CancelWorkflowRuns(7, "head-1", (42,)))
    gateway.ci_state = "clear"
    assert executor.drain_pending_commands() == 0
    assert gateway.calls == [("cancel", "head-1", (42,))]
    assert store.effect_receipt(command.command_id) is None
    assert store.pending_commands()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("run_head", ["head-1", "merge-sha", "unrelated"])
def test_declared_test_merge_runs_can_be_remediated_with_pr_head_fence(tmp_path, cancel, run_head):
    from issue_agent.domain.checks import Observed, RequiredCheck
    from issue_agent.adapters.github import GitHubGateway
    import json
    executor, gateway, store = _executor(tmp_path, ci=True)
    pr = gateway.get_pr(7)
    state, bucket = ("IN_PROGRESS", "pending") if cancel else ("FAILURE", "fail")
    pr = replace(pr, ci=Observed("head-1", "merge-sha", frozenset({RequiredCheck("unit")}),
        (Check("unit", state, bucket, run_id=42, required=True),)))
    gateway.get_pr = lambda *args, **kwargs: pr
    calls = []
    def run(argv, token):
        calls.append(argv)
        if "POST" in argv:
            return ""
        return json.dumps({"id": 42, "head_sha": run_head, "run_attempt": 1,
            "status": "in_progress" if cancel else "completed"})
    api = GitHubGateway("o/r", runner=run)
    gateway.workflow_run_attempts = api.workflow_run_attempts
    gateway.workflow_run_states = api.workflow_run_states
    gateway.rerun_failed_checks = api.rerun_failed_checks
    gateway.cancel_workflow_runs = api.cancel_workflow_runs
    action = CancelWorkflowRuns(7, "head-1", (42,)) if cancel else RerunFailedChecks(7, "head-1", (42,))
    if run_head == "unrelated":
        with pytest.raises(UnknownEffectOutcome):
            executor.execute(action)
        assert not any("POST" in call for call in calls)
        assert not store.pending_commands()
        return
    assert executor.execute(action)
    assert len([call for call in calls if "POST" in call]) == 1
    assert not store.pending_commands()


@pytest.mark.parametrize("cancel", [False, True])
def test_changed_test_merge_obsoletes_unstarted_ci_remediation(tmp_path, cancel):
    from issue_agent.domain.checks import Observed, RequiredCheck
    from issue_agent.domain.contracts import EffectStatus
    executor, gateway, store = _executor(tmp_path, ci=True)
    gateway.workflow_run_states = lambda *args: {42: "in_progress"}
    gateway.ci_state = "pending" if cancel else "failure"
    pr = gateway.get_pr(7)
    ci = Observed("head-1", "merge-old", frozenset({RequiredCheck("unit")}),
        (replace(pr.checks[0], required=True),))
    gateway.get_pr = lambda *args, **kwargs: replace(pr, ci=ci)
    action = CancelWorkflowRuns(7, "head-1", (42,)) if cancel else RerunFailedChecks(7, "head-1", (42,))
    command = executor._command(action)
    executor.effects.enqueue(command)
    ci = replace(ci, tested_revision="merge-new")
    assert executor.drain_pending_commands() == 0
    assert not gateway.calls and not store.pending_commands()
    assert store.effect_receipt(command.command_id).status is EffectStatus.PERMANENT_FAILURE


def test_lost_test_merge_rerun_receipt_recovers_against_captured_revision(tmp_path):
    from issue_agent.domain.checks import Observed, RequiredCheck
    from issue_agent.adapters.github import GitHubGateway
    import json
    executor, gateway, store = _executor(tmp_path, ci=True)
    pr = gateway.get_pr(7)
    ci = Observed("head-1", "merge-old", frozenset({RequiredCheck("unit")}),
        (replace(pr.checks[0], required=True),))
    gateway.get_pr = lambda *args, **kwargs: replace(pr, ci=ci)
    posts = []
    def run(argv, token):
        if "POST" in argv:
            posts.append(argv)
            raise RuntimeError("accepted rerun reply lost")
        return json.dumps({"id": 42, "head_sha": "merge-old", "run_attempt": 2 if posts else 1})
    api = GitHubGateway("o/r", runner=run)
    gateway.workflow_run_attempts = api.workflow_run_attempts
    gateway.rerun_failed_checks = api.rerun_failed_checks
    with pytest.raises(RuntimeError, match="reply lost"):
        executor.execute(RerunFailedChecks(7, "head-1", (42,)))
    ci = replace(ci, tested_revision="merge-new")
    assert executor.drain_pending_commands() == 1
    assert len(posts) == 1 and not store.pending_commands()


@pytest.mark.parametrize("merge_state", ["HAS_HOOKS", "UNSTABLE"])
def test_empty_policy_cannot_capture_new_merge_eligibility(tmp_path, merge_state):
    executor, gateway, store = _executor(tmp_path)
    gateway.merge_state = merge_state
    with pytest.raises(StaleExpectedRevision):
        executor.execute(EnableAutoMerge(7, "squash", "head-1"))
    assert not gateway.calls and not store.pending_commands()


@pytest.mark.parametrize("refresh", [False, True])
def test_same_head_transient_red_ci_does_not_permanently_disable_merge_or_refresh(tmp_path, refresh):
    from issue_agent.domain.contracts import EffectStatus
    executor, gateway, store = _executor(tmp_path, ci=refresh)
    gateway.ci_state = "clear"
    gateway.merge_state = "BEHIND" if refresh else "CLEAN"
    action = UpdateBranch(7, "head-1") if refresh else EnableAutoMerge(7, "squash", "head-1")
    command = executor._command(action)
    executor.effects.enqueue(command)
    gateway.ci_state = "failure"
    assert executor.drain_pending_commands() == 0
    assert not gateway.calls
    gateway.ci_state = "clear"
    assert executor.execute(action)
    assert executor.execute(action)
    assert len(gateway.calls) == 1
    assert store.effect_receipt(command.command_id).status is EffectStatus.SUCCEEDED
