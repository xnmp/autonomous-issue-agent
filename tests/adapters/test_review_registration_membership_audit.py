"""Durable expected-command census against the independent lifecycle census."""

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.review_lifecycle_archive_audit import (
    advance_review_lifecycle_archive_audit,
)
from issue_agent.adapters.review_lifecycle_registration import (
    read_review_lifecycle_registration_link, read_review_lifecycle_registration_page,
)
from issue_agent.adapters.review_registration_membership_audit import (
    advance_review_registration_membership_audit,
    observe_matched_review_registration_membership,
    read_review_registration_membership_state,
)
from issue_agent.adapters.workflow_rows import REVIEW_SUBMISSION_NAMESPACE, WorkflowAuthorityError
from issue_agent.domain.review_lifecycle_archive_audit import CompletedReviewLifecycleArchiveAudit
from issue_agent.domain.review_registration_membership_audit import (
    BlockedReviewRegistrationMembership, CompletedReviewRegistrationMembership,
    ScanningReviewRegistrationMembership,
)
from issue_agent.domain.review_rounds import ReviewSubmissionKind
from issue_agent.domain.contracts import EffectReceipt, EffectStatus
from tests.adapters.test_review_archive_source_versions import _insert_lifecycle_command
from tests.adapters.test_review_execution_control import OWNER, current, rig
from tests.adapters.test_review_execution_submission import command_for
from tests.adapters.test_review_productive_settlement import confirmed_copy


NOW = datetime(2026, 9, 29, 0, tzinfo=timezone.utc)


def _complete_registration(store, *, start=NOW):
    for step in range(128):
        with store.transaction() as db:
            state = advance_review_registration_membership_audit(
                db, "o/r", now=start + timedelta(seconds=step),
            )
        if isinstance(state, (CompletedReviewRegistrationMembership,
                              BlockedReviewRegistrationMembership)):
            return state
    raise AssertionError("registration membership scan did not finish")


def _complete_lifecycle(store, *, start=NOW):
    for step in range(128):
        with store.transaction() as db:
            state = advance_review_lifecycle_archive_audit(
                db, "o/r", now=start + timedelta(seconds=step),
            )
        if isinstance(state, CompletedReviewLifecycleArchiveAudit):
            return state
    raise AssertionError("lifecycle membership scan did not finish")


def test_registration_membership_matches_real_recorded_submission(rig):
    store, _, _, _, _, _ = confirmed_copy(rig)
    registration = _complete_registration(store)
    lifecycle = _complete_lifecycle(store)
    assert isinstance(registration, CompletedReviewRegistrationMembership)
    assert registration.expected_commands.count == 1
    with store.transaction() as db:
        before = db.total_changes
        match = observe_matched_review_registration_membership(
            db, "o/r", now=NOW + timedelta(seconds=10),
        )
        assert db.total_changes == before
        assert read_review_registration_membership_state(db, "o/r") == registration
    assert match.registration == registration
    assert match.lifecycle == lifecycle


def test_registration_membership_exposes_unregistered_historical_command(rig):
    store, _, _, _, _, _ = confirmed_copy(rig)
    with store.transaction() as db:
        _insert_lifecycle_command(db, "fixture:unregistered", state="pending")
        db.execute(
            "UPDATE lifecycle_commands SET repository='o/r' "
            "WHERE command_id='fixture:unregistered'",
        )
    registration = _complete_registration(store)
    lifecycle = _complete_lifecycle(store)
    assert isinstance(registration, CompletedReviewRegistrationMembership)
    assert lifecycle.commands.count == registration.expected_commands.count + 1
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="disagree"):
            observe_matched_review_registration_membership(
                db, "o/r", now=NOW + timedelta(seconds=10),
            )


def test_registration_membership_blocks_changed_link_and_restarts_after_repair(rig):
    store, _, selection, _, _, command = confirmed_copy(rig)
    repository = selection.expected.fence.key.repository
    with store.transaction() as db:
        original = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_SUBMISSION_NAMESPACE, repository, command.command_id),
        ).fetchone()[0]
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{}' "
            "WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_SUBMISSION_NAMESPACE, repository, command.command_id),
        )
    blocked = _complete_registration(store)
    assert isinstance(blocked, BlockedReviewRegistrationMembership)
    with store.transaction() as db:
        assert read_review_registration_membership_state(db, repository) == blocked
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? "
            "WHERE namespace=? AND scope=? AND key=?",
            (original, REVIEW_SUBMISSION_NAMESPACE, repository, command.command_id),
        )
    recovered = _complete_registration(store, start=NOW + timedelta(seconds=1))
    assert isinstance(recovered, CompletedReviewRegistrationMembership)
    assert recovered.round_receipts_version > blocked.prior.round_receipts_version


def test_registration_membership_restarts_after_lifecycle_change(rig):
    store, _, _, _, _, command = confirmed_copy(rig)
    completed = _complete_registration(store)
    assert isinstance(completed, CompletedReviewRegistrationMembership)
    with store.transaction() as db:
        db.execute(
            "UPDATE lifecycle_commands SET updated_at=updated_at WHERE command_id=?",
            (command.command_id,),
        )
        restarted = advance_review_registration_membership_audit(
            db, "o/r", now=NOW + timedelta(seconds=1),
        )
    assert isinstance(restarted, CompletedReviewRegistrationMembership)
    assert restarted.lifecycle_source_version > completed.lifecycle_source_version


def test_registration_membership_advances_bounded_unrelated_global_pages(rig):
    store = rig[0]
    with store.transaction() as db:
        for index in range(65):
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('unrelated','o/r',?,?, '{}')",
                (f"unrelated:{index}", NOW.isoformat()),
            )
    _, _, _, _, _, _ = confirmed_copy(rig)
    with store.transaction() as db:
        first = advance_review_registration_membership_audit(db, "o/r", now=NOW)
    assert isinstance(first, ScanningReviewRegistrationMembership)
    assert first.expected_commands.count == 0
    completed = _complete_registration(store, start=NOW + timedelta(seconds=1))
    assert isinstance(completed, CompletedReviewRegistrationMembership)
    assert completed.expected_commands.count == 1


def test_registration_membership_rejects_round_change_after_both_censuses(rig):
    store, _, _, _, plan, _ = confirmed_copy(rig)
    assert isinstance(_complete_registration(store), CompletedReviewRegistrationMembership)
    assert isinstance(_complete_lifecycle(store), CompletedReviewLifecycleArchiveAudit)
    with store.transaction() as db:
        db.execute(
            "UPDATE review_execution_rounds SET plan_json='{}' WHERE round_id=?",
            (plan.round.round_id,),
        )
        with pytest.raises(WorkflowAuthorityError, match="current completed censuses"):
            observe_matched_review_registration_membership(
                db, "o/r", now=NOW + timedelta(seconds=10),
            )
        rescanned = advance_review_registration_membership_audit(
            db, "o/r", now=NOW + timedelta(seconds=10),
        )
    assert isinstance(rescanned, BlockedReviewRegistrationMembership)


def test_small_caller_budget_does_not_persist_a_source_blocker(rig):
    store, _, _, _, _, _ = confirmed_copy(rig)
    with store.transaction() as db:
        with pytest.raises(ValueError, match="caller.*byte budget"):
            advance_review_registration_membership_audit(
                db, "o/r", now=NOW, max_link_source_bytes=1,
            )
        assert read_review_registration_membership_state(db, "o/r") is None
    assert isinstance(_complete_registration(store), CompletedReviewRegistrationMembership)


def test_registration_membership_shares_link_budget_across_one_page(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    for body in ("first", "second"):
        command = command_for(current(store, selection), body=body)
        control.register_submission(plan.round.round_id, current(store, selection).revision,
                                    command, ReviewSubmissionKind.COPIED)
        if body == "first":
            store.begin_command(command.command_id)
            store.record_effect(EffectReceipt(
                command.command_id, OWNER, store.now(), store.now(),
                EffectStatus.PERMANENT_FAILURE, error="head changed",
            ))
    with store.transaction() as db:
        page = read_review_lifecycle_registration_page(db, "o/r")
        assert len(page.sources) == 2
        first_bytes = read_review_lifecycle_registration_link(
            db, page.sources[0],
        ).source_bytes
        first = advance_review_registration_membership_audit(
            db, "o/r", now=NOW, max_link_source_bytes=first_bytes,
        )
    assert isinstance(first, ScanningReviewRegistrationMembership)
    assert first.expected_commands.count == 1
    completed = _complete_registration(store, start=NOW + timedelta(seconds=1))
    assert isinstance(completed, CompletedReviewRegistrationMembership)
    assert completed.expected_commands.count == 2


def test_registration_membership_rejects_corrupt_persisted_cursor(rig):
    store, _, _, _, _, _ = confirmed_copy(rig)
    assert isinstance(_complete_registration(store), CompletedReviewRegistrationMembership)
    with store.transaction() as db:
        db.execute(
            "UPDATE level_state SET payload_json='{}' "
            "WHERE namespace='review-registration-membership-scan-v1' AND scope='o/r'",
        )
        with pytest.raises(WorkflowAuthorityError, match="cursor"):
            read_review_registration_membership_state(db, "o/r")


def test_registration_membership_rejects_overdue_observations(rig):
    store, _, _, _, _, _ = confirmed_copy(rig)
    registration = _complete_registration(store)
    assert isinstance(registration, CompletedReviewRegistrationMembership)
    assert isinstance(_complete_lifecycle(store), CompletedReviewLifecycleArchiveAudit)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="overdue"):
            observe_matched_review_registration_membership(
                db, "o/r", now=registration.next_due_at,
            )
