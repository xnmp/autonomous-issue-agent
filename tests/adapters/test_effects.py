from issue_agent.adapters.control_store import SQLiteControlStore
from types import SimpleNamespace

from issue_agent.adapters.effects import GitHubEffectExecutor, command_for_action
from issue_agent.domain.actions import (
    AddDependency,
    EnableAutoMerge,
    PostComment,
    Relabel,
    RerunFailedChecks,
    Target,
    UpdateBranch,
)
from issue_agent.domain.labels import Label


class Gateway:
    def __init__(self):
        self.labels = set()
        self.comments = set()
        self.calls = []
        self.head = "head-1"
        self.auto_merge = set()
        self.run_attempt = 0
        self.dependencies = set()
        self.reviews = set()

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

    def get_pr(self, pr, *, mergeability=True, checks=True):
        del mergeability, checks
        return SimpleNamespace(head_ref_oid=self.head)

    def rerun_failed_checks(self, pr):
        self.calls.append(("rerun", pr))
        self.run_attempt += 1
        return 42

    def max_run_attempt(self, head):
        del head
        return self.run_attempt

    def enable_auto_merge(self, pr, method):
        self.calls.append(("auto_merge", pr, method))
        self.auto_merge.add(pr)
        return True

    def is_auto_merge_enabled_or_merged(self, pr):
        return pr in self.auto_merge

    def update_branch(self, pr):
        self.calls.append(("update_branch", pr))
        self.head = f"{self.head}-updated"

    def is_branch_updated(self, pr, prior_head):
        del pr
        return self.head != prior_head

    def add_blocked_by(self, issue, blocker):
        self.calls.append(("dependency", issue, blocker))
        self.dependencies.add((issue, blocker))

    def has_blocked_by(self, issue, blocker):
        return (issue, blocker) in self.dependencies

    def submit_review(self, pr, event, body):
        self.calls.append(("review", pr, event, body, self.head))
        self.reviews.add((pr, event, body, self.head))
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


def test_expected_revision_blocks_stale_effect(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    gateway.head = "new-head"

    assert executor.execute(RerunFailedChecks(7, "old-head")) is False

    assert gateway.calls == []
    assert store.pending_commands() == ()


def test_ambiguous_auto_merge_is_reconciled_without_duplicate(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = EnableAutoMerge(7, "squash")
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
    assert gateway.calls == [("auto_merge", 7, "squash")]
    assert store.pending_commands() == ()


def test_ambiguous_branch_update_uses_original_revision_to_reconcile(
    tmp_path,
) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = UpdateBranch(7)
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
    assert gateway.calls == [("update_branch", 7)]
    assert store.pending_commands() == ()


def test_ambiguous_check_rerun_is_reconciled_by_attempt_number(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = RerunFailedChecks(7, "head-1")
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

    assert executor.execute(action)
    assert gateway.calls == [("rerun", 7)]
    assert store.pending_commands() == ()


def test_unconfirmed_check_rerun_stays_retryable(tmp_path) -> None:
    executor, gateway, store = _executor(tmp_path)
    action = RerunFailedChecks(7, "head-1")
    original = gateway.rerun_failed_checks
    gateway.rerun_failed_checks = lambda _pr: None

    assert executor.execute(action) is None
    assert store.pending_commands()[0].state == "retryable_failure"

    gateway.rerun_failed_checks = original
    assert executor.execute(action) == 42
    assert gateway.calls == [("rerun", 7)]
    assert store.pending_commands() == ()


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
        executor.submit_review(7, "APPROVE", "looks good")
    except RuntimeError:
        pass
    gateway.submit_review = original

    assert executor.submit_review(7, "APPROVE", "looks good")
    gateway.head = "head-2"
    assert executor.submit_review(7, "APPROVE", "looks good")

    assert gateway.calls == [
        ("review", 7, "APPROVE", "looks good", "head-1"),
        ("review", 7, "APPROVE", "looks good", "head-2"),
    ]
    assert store.pending_commands() == ()


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
