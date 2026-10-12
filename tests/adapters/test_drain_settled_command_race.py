"""A drain that loses the reconcile race to the executing process (keifu #177).

2026-08-02 04:10:52Z, review daemon, keifu PR #177:

    lifecycle command d18b8bfc… (submit_review on pr:177) could not be
    drained: ControlStoreError: command is not awaiting reconciliation

The review had in fact landed — the verdict was visible and the relabel
followed. `pending_commands` returns a SNAPSHOT, and everything between it and
the reconcile is a remote read, so a second drain (hot reload makes two drains
overlap routinely) can enumerate a command as `executing`, verify on GitHub the
very effect the executing process is committing, and reach
`resolve_unknown_effect` after that process settled it `succeeded`.

Pinned here: two actors proving the SAME success converge — the drain reports
the command as drained, logs nothing, re-submits nothing, and the first
actor's receipt survives untouched. States that cannot hold a verified success
still raise, because there the receipt and the command genuinely disagree.

Convergence is a claim about the OUTCOME, not just the status: a reconciling
read need not be read-only, so two overlapping actors can create PROJ-100 and
PROJ-101 under one command, and a silent False would let the caller persist the
key the authoritative receipt does not name. Differing non-null
`remote_object_id`s raise; a null on either side carries no evidence of
disagreement and stays benign.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from issue_agent.adapters.control_store import ControlStoreError, SQLiteControlStore
from issue_agent.adapters.effects import GitHubEffectExecutor
from issue_agent.domain.contracts import (
    CommandId,
    CommandKind,
    EffectReceipt,
    EffectStatus,
    LifecycleCommand,
)

REPOSITORY = "o/r"
IDENTITY = "o/r:review"
PR = 177
HEAD = "head-sha-1"
EVENT = "APPROVE"
BODY = "verdict: approve"


class ReviewGateway:
    """Reports the review as present, and runs `on_verify` INSIDE the readback.

    That hook is the interleaving: the executing process commits its succeeded
    receipt while this drain is still on the wire asking GitHub.
    """

    def __init__(self, *, present: bool = True, on_verify=None):
        self.present = present
        self.on_verify = on_verify
        self.submissions: list[tuple] = []
        self.verifications = 0

    def has_review(self, pr, event, body, head):
        self.verifications += 1
        if self.on_verify is not None:
            hook, self.on_verify = self.on_verify, None
            hook()
        return self.present

    def submit_review(self, pr, event, body, head):
        self.submissions.append((pr, event, body, head))
        return True

    def get_pr(self, number, mergeability=True, checks=True):
        return _PullRequest(HEAD)


class _PullRequest:
    def __init__(self, head_ref_oid: str):
        self.head_ref_oid = head_ref_oid


def _store(tmp_path) -> SQLiteControlStore:
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    return store


def _executor(store, gateway, log) -> GitHubEffectExecutor:
    return GitHubEffectExecutor(
        gateway,
        store,
        repository=REPOSITORY,
        identity=IDENTITY,
        log=log,
    )


def _review_command() -> LifecycleCommand:
    return LifecycleCommand(
        CommandId("d18b8bfc"),
        "github:d18b8bfc",
        CommandKind.SUBMIT_REVIEW,
        f"pr:{PR}",
        HEAD,
        {"pr": PR, "event": EVENT, "body": BODY, "head": HEAD},
        "",
        REPOSITORY,
        IDENTITY,
    )


def _receipt(
    status: EffectStatus,
    *,
    error: str | None = None,
    remote_object_id: str | None = "review-9001",
) -> EffectReceipt:
    started = datetime(2026, 8, 2, 4, 10, 30, tzinfo=timezone.utc)
    return EffectReceipt(
        command_id=CommandId("d18b8bfc"),
        executor_identity=IDENTITY,
        started_at=started,
        completed_at=datetime(2026, 8, 2, 4, 10, 48, tzinfo=timezone.utc),
        status=status,
        remote_object_id=remote_object_id,
        remote_revision=HEAD,
        error=error,
    )


def test_command_settled_between_enumeration_and_reconcile_drains_cleanly(tmp_path):
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)
    store.begin_command(command.command_id)  # the executing process, mid-submit

    settled = _receipt(EffectStatus.SUCCEEDED)
    gateway = ReviewGateway(
        present=True,
        # Committed while this drain is inside `has_review`.
        on_verify=lambda: store.record_effect(settled),
    )
    lines: list[str] = []

    assert _executor(store, gateway, lines.append).drain_pending_commands() == 1

    # No scary line for an outcome both actors agree on.
    assert lines == []
    # No double-apply: the drain verified, it did not re-submit.
    assert gateway.submissions == []
    # The queue is empty and the first actor's receipt is the surviving one,
    # with its real execution window rather than the drain's reconcile clock.
    assert (
        store.pending_commands(repository=REPOSITORY, executor_identity=IDENTITY) == ()
    )
    assert store.effect_receipt(command.command_id) == settled


def test_drained_review_is_not_resubmitted_on_later_ticks(tmp_path):
    """The convergence is terminal, not a loop: the command leaves the pending
    set, so no ceiling is owed."""
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    gateway = ReviewGateway(
        present=True,
        on_verify=lambda: store.record_effect(_receipt(EffectStatus.SUCCEEDED)),
    )
    lines: list[str] = []
    executor = _executor(store, gateway, lines.append)

    drained = [executor.drain_pending_commands() for _ in range(5)]

    assert drained == [1, 0, 0, 0, 0]
    assert gateway.submissions == []
    assert lines == []


def test_resolve_of_a_command_that_never_executed_still_raises(tmp_path):
    """A receipt for a `pending` command is corruption, not convergence."""
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)

    with pytest.raises(ControlStoreError, match="not awaiting reconciliation"):
        store.resolve_unknown_effect(_receipt(EffectStatus.SUCCEEDED))


def test_resolve_of_an_unknown_command_still_raises(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(ControlStoreError, match="not awaiting reconciliation"):
        store.resolve_unknown_effect(_receipt(EffectStatus.SUCCEEDED))


def test_resolve_contradicting_a_recorded_failure_still_raises(tmp_path):
    """A verified success against a settled FAILURE is a real disagreement."""
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    store.record_effect(
        _receipt(EffectStatus.RETRYABLE_FAILURE, error="GitHub refused the review")
    )

    with pytest.raises(ControlStoreError, match="not awaiting reconciliation"):
        store.resolve_unknown_effect(_receipt(EffectStatus.SUCCEEDED))


def test_resolve_contradicting_a_retired_command_still_raises(tmp_path):
    """A verified success against a RETIRED command is a real disagreement:
    the ceiling escalation already told an operator it never landed."""
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    store.record_effect(
        _receipt(EffectStatus.PERMANENT_FAILURE, error="review head changed")
    )

    with pytest.raises(ControlStoreError, match="not awaiting reconciliation"):
        store.resolve_unknown_effect(_receipt(EffectStatus.SUCCEEDED))


def test_succeeded_command_holding_an_unknown_receipt_still_raises(tmp_path):
    """`succeeded` with an AMBIGUOUS receipt is the same corruption as
    `succeeded` with none: the settled state without settling evidence."""
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    store.record_effect(
        _receipt(EffectStatus.UNKNOWN_OUTCOME, error="GhUnavailable: 502")
    )
    with store.transaction() as db:
        db.execute(
            "UPDATE lifecycle_commands SET state='succeeded' WHERE command_id=?",
            (str(command.command_id),),
        )

    with pytest.raises(ControlStoreError, match="without a succeeded receipt"):
        store.resolve_unknown_effect(_receipt(EffectStatus.SUCCEEDED))


def test_success_against_a_different_remote_object_raises(tmp_path):
    """Two overlapping actors that created PROJ-100 and PROJ-101 have NOT
    converged, and answering False would let the caller persist the key the
    receipt does not name."""
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    landed = _receipt(EffectStatus.SUCCEEDED, remote_object_id="PROJ-100")
    store.record_effect(landed)

    with pytest.raises(ControlStoreError, match="different remote object"):
        store.resolve_unknown_effect(
            _receipt(EffectStatus.SUCCEEDED, remote_object_id="PROJ-101")
        )

    # The authoritative receipt is untouched by the refused resolve.
    assert store.effect_receipt(command.command_id) == landed


def test_success_without_a_remote_object_stays_benign(tmp_path):
    """Divergence needs evidence on BOTH sides. Most effects name no remote
    object at all, so a null on either side must not become an error."""
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    landed = _receipt(EffectStatus.SUCCEEDED, remote_object_id="PROJ-100")
    store.record_effect(landed)

    assert (
        store.resolve_unknown_effect(
            _receipt(EffectStatus.SUCCEEDED, remote_object_id=None)
        )
        is False
    )
    assert store.effect_receipt(command.command_id) == landed


def test_succeeded_command_without_a_succeeded_receipt_still_raises(tmp_path):
    """The settled state without the evidence that produces it is corruption:
    treating it as already-drained would launder a lost receipt into success."""
    store = _store(tmp_path)
    command = _review_command()
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    store.record_effect(_receipt(EffectStatus.SUCCEEDED))
    with store.transaction() as db:
        db.execute(
            "DELETE FROM effect_receipts WHERE command_id=?",
            (str(command.command_id),),
        )

    with pytest.raises(ControlStoreError, match="without a succeeded receipt"):
        store.resolve_unknown_effect(_receipt(EffectStatus.SUCCEEDED))
