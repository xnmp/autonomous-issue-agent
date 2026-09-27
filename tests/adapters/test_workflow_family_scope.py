"""Family-scoped workflow authority: a review row is never resolve authority, and vice versa."""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_store import (
    AccountingDisposition, ControlStoreError, ReservationRequest, SQLiteControlStore, workflow_episode_key,
)
from issue_agent.adapters.effects import command_for_action
from issue_agent.adapters.resolve_activation import read_inventory
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.adapters.workflow_commit import insert_pending_rearm
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, load_family_authority, load_resolve_authority, load_resolve_workflow_row,
    load_review_workflow_row, load_workflow_read, load_workflow_row, persist_workflow_snapshot,
    require_workflow_binding, workflow_scope,
)
from issue_agent.domain.actions import Relabel, Target
from issue_agent.domain.contracts import (
    BudgetScope, CommandId, CommandKind, LifecycleCommand, Money, PriceQuote, ReservationId, RunId,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.review_workflow import ReviewEpisode, ReviewWorkflowSnapshot
from issue_agent.domain.workflow import (
    AuthorizedOperatorCommand, ResolveEpisode, WorkflowAbsent, WorkflowMigrationRequired, WorkflowSnapshot,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, PullRequestRef, WorkflowFamily, WorkflowKey, WorkflowWriter,
)
from tests.fakes.review_workflow import (
    ACTOR, NOW, PR_KEY, REVIEW, held, pending, preflight, request,
)
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

FIX = "o/r:fix:test"
REVIEWER = "o/r:review:test"
RESOLVE_KEY = resolve_snapshot().revision.fence.key


@pytest.fixture
def store(tmp_path):
    control = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    control.initialize()
    return control


def read(store, reader, *args):
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        return reader(db, *args)


def install_review(store, state: ReviewWorkflowSnapshot):
    """Seed rows exactly as a committed review transition would persist them."""
    install_workflow(store, replace(state, gates=(), intent=None))
    with store.transaction() as db:
        persist_workflow_snapshot(db, state)
        if state.intent is not None:
            audit = AuthorizedOperatorCommand(ACTOR, request(held()), preflight())
            insert_pending_rearm(db, workflow_scope(PR_KEY), state.intent, audit)


def bound_run(store, binding, name, owner):
    store.start_run(RunId(name), json.dumps({"repository": "o/r"}), "RESOLVE", owner, "config", state="completed")
    with store.transaction() as db:
        db.execute("UPDATE runs SET workflow_binding_json=?, workflow_episode_key=? WHERE run_id=?",
                   (encode_workflow(binding), workflow_episode_key(binding), name))


def drop_activation(store, family):
    with store.transaction() as db:
        db.execute("PRAGMA defer_foreign_keys=ON")
        db.execute("DELETE FROM workflow_authority_epochs WHERE family=?", (family.value,))


# -- activation authority -----------------------------------------------------

def test_retained_review_rows_are_never_resolve_authority(store):
    install_review(store, pending())
    assert read(store, load_resolve_authority, "o/r") is None
    assert read(store, load_family_authority, WorkflowFamily.REVIEW_REJECTIONS, "o/r") == REVIEW


def test_retained_rows_without_activation_fail_closed_only_for_their_own_family(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    drop_activation(store, WorkflowFamily.REVIEW_REJECTIONS)
    with pytest.raises(WorkflowAuthorityError, match="review_rejections activation is missing"):
        read(store, load_family_authority, WorkflowFamily.REVIEW_REJECTIONS, "o/r")
    assert read(store, load_resolve_authority, "o/r") == resolve_snapshot().authority
    drop_activation(store, WorkflowFamily.RESOLVE)
    with pytest.raises(WorkflowAuthorityError, match="resolve activation is missing"):
        read(store, load_resolve_authority, "o/r")


@pytest.mark.parametrize("family", [WorkflowFamily.RESOLVE, WorkflowFamily.REVIEW_REJECTIONS])
def test_a_retained_run_alone_blocks_only_its_own_familys_absent_activation(store, family):
    binding = held().binding if family is WorkflowFamily.REVIEW_REJECTIONS else resolve_snapshot().binding
    owner = REVIEWER if family is WorkflowFamily.REVIEW_REJECTIONS else FIX
    bound_run(store, binding, "retained", owner)
    other = next(f for f in WorkflowFamily if f is not family)
    with pytest.raises(WorkflowAuthorityError, match="missing despite retained"):
        read(store, load_family_authority, family, "o/r")
    assert read(store, load_family_authority, other, "o/r") is None


def test_a_retained_execution_component_cannot_be_confused_with_rejection_activation(store):
    install_review(store, held())
    assert read(store, load_family_authority, WorkflowFamily.REVIEW_EXECUTION, "o/r") is None
    with store.transaction() as db:
        db.execute("INSERT INTO workflow_components VALUES ('o/r','pr',9,'review_execution',2,'execution:retained')")
    with pytest.raises(WorkflowAuthorityError, match="review_execution activation is missing"):
        read(store, load_family_authority, WorkflowFamily.REVIEW_EXECUTION, "o/r")
    assert read(store, load_family_authority, WorkflowFamily.REVIEW_REJECTIONS, "o/r") == REVIEW
    assert read(store, load_resolve_authority, "o/r") is None


def test_family_authority_rejects_untyped_families(store):
    with pytest.raises(ValueError):
        read(store, load_family_authority, "review_rejections", "o/r")


# -- row dispatch and cross-family confusion ----------------------------------

def test_each_family_decodes_only_its_own_rows(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, pending())
    resolve = read(store, load_workflow_row, RESOLVE_KEY)
    review = read(store, load_workflow_row, PR_KEY)
    assert isinstance(resolve, WorkflowSnapshot) and resolve == resolve_snapshot()
    assert isinstance(review, ReviewWorkflowSnapshot) and review == pending()
    with pytest.raises(WorkflowAuthorityError, match="does not own"):
        read(store, load_resolve_workflow_row, PR_KEY)
    with pytest.raises(WorkflowAuthorityError, match="does not own"):
        read(store, load_review_workflow_row, RESOLVE_KEY)


@pytest.mark.parametrize("target, payload", [
    (("pr", 10), lambda: (REVIEW, ResolveEpisode("forged"))),
    (("pr", 10), lambda: (FamilyAuthority(WorkflowFamily.RESOLVE, 1), ReviewEpisode("forged"))),
    (("issue", 2), lambda: (FamilyAuthority(WorkflowFamily.RESOLVE, 1), ReviewEpisode("forged"))),
    (("issue", 2), lambda: (REVIEW, ResolveEpisode("forged"))),
])
def test_forged_cross_family_episode_rows_never_load(store, target, payload):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    with store.transaction() as db:
        db.execute("INSERT INTO workflows VALUES ('o/r', ?, ?, 0, 0, ?)", (*target, encode_workflow(payload())))
    key = WorkflowKey("o/r", PullRequestRef(target[1]) if target[0] == "pr" else IssueRef(target[1]))
    with pytest.raises(WorkflowAuthorityError, match="invalid workflow authority"):
        read(store, load_workflow_row, key)


def test_review_rearm_index_is_checked_against_its_payload(store):
    install_review(store, pending())
    with store.transaction() as db:
        db.execute("UPDATE workflow_rearms SET state='completed'")
    with pytest.raises(WorkflowAuthorityError, match="rearm index disagrees"):
        read(store, load_workflow_row, PR_KEY)


def test_review_read_without_review_activation_is_migration_required(store):
    install_workflow(store, resolve_snapshot())
    migration = read(store, load_workflow_read, PR_KEY)
    assert migration == WorkflowMigrationRequired(PR_KEY, WorkflowFamily.REVIEW_REJECTIONS)
    assert migration.reason == "review_rejections_migration_required"
    assert read(store, load_workflow_read, RESOLVE_KEY) == resolve_snapshot()
    assert read(store, load_workflow_read, WorkflowKey("o/r", IssueRef(99))) == WorkflowAbsent(WorkflowKey("o/r", IssueRef(99)))
    with pytest.raises(WorkflowAuthorityError, match="review_rejections workflow migration is required"):
        read(store, load_workflow_row, PR_KEY)


def test_resolve_read_without_resolve_activation_stays_resolve_migration_required(store):
    install_review(store, held())
    migration = read(store, load_workflow_read, RESOLVE_KEY)
    assert migration == WorkflowMigrationRequired(RESOLVE_KEY)
    assert migration.reason == "resolve_migration_required"
    assert read(store, load_workflow_read, PR_KEY) == held()
    other = WorkflowKey("o/r", PullRequestRef(10))
    assert read(store, load_workflow_read, other) == WorkflowAbsent(other)


def test_captured_binding_is_required_per_family(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        assert require_workflow_binding(db, held().binding, allow_gated=True) == held()
        assert require_workflow_binding(db, resolve_snapshot().binding) == resolve_snapshot()
        with pytest.raises(WorkflowAuthorityError, match="unresolved gates"):
            require_workflow_binding(db, held().binding)
        stale = replace(held().binding, fence=replace(held().binding.fence, generation=1))
        with pytest.raises(WorkflowAuthorityError, match="stale"):
            require_workflow_binding(db, stale, allow_gated=True)


# -- resolve control is blind to review rows ----------------------------------

def test_resolve_reconciliation_never_schedules_a_pending_review_rearm(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, pending())
    control = SQLiteWorkflowControl(store, WorkflowWriter("o/r", "fix", FIX), resolve_ceiling=3)
    assert control.pending_rearms() == ()
    assert control.load_workflow(RESOLVE_KEY) == resolve_snapshot()
    with pytest.raises(WorkflowAuthorityError, match="resolve writer scope"):
        control.load_workflow(PR_KEY)
    with closing(store._connect()) as db:
        unavailable = db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='workflow-rearm-observation'").fetchone()[0]
    assert unavailable == 0


def test_resolve_activation_inventory_ignores_review_family_rows(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, pending())
    bound_run(store, held().binding, "review-run", REVIEWER)
    drop_activation(store, WorkflowFamily.RESOLVE)
    with closing(store._connect()) as db:
        retained = read_inventory(db, "o/r").retained_targets
    assert retained == ("issue:1",)


def test_legacy_unblock_stays_closed_across_the_review_family(store):
    with store.legacy_resolve_authority("o/r"):
        pass
    install_review(store, held())
    with pytest.raises(WorkflowAuthorityError, match="review rejection control"):
        with store.legacy_resolve_authority("o/r"):
            pass
    drop_activation(store, WorkflowFamily.REVIEW_REJECTIONS)
    with pytest.raises(WorkflowAuthorityError, match="missing despite retained"):
        with store.legacy_resolve_authority("o/r"):
            pass


# -- admission and command validation split by binding family ------------------

def reservation(name, binding, owner, kind="REVIEW"):
    return ReservationRequest(
        ReservationId("reservation:" + name), RunId(name),
        json.dumps({"repository": "o/r", "role": owner.split(":")[1], "kind": kind, "leg_id": "leg:" + name,
                    "runtime": "codex", "pr": 9, "number": 9}),
        BudgetScope("o/r", "daily", "USD", "2026-09-25", NOW, NOW + timedelta(days=1), ZoneInfo("UTC")),
        PriceQuote("model", "card", "1", NOW, Money("USD", 1)), Money("USD", 100), "config", "o/r",
        kind=kind, owner_id=owner, workflow=binding,
    )


def assert_refused(store, request, match):
    result = store.reserve(request)
    assert result.disposition is AccountingDisposition.UNAVAILABLE and match in result.detail
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM runs WHERE run_id=?", (str(request.run_id),)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='workflow-admission'").fetchone()[0] == 0


def test_review_admission_requires_the_review_owner_and_records_no_resolve_receipt(store):
    install_workflow(store, resolve_snapshot())
    state = ReviewWorkflowSnapshot(held().revision, REVIEW, ReviewEpisode("review-episode"))
    install_review(store, state)
    assert_refused(store, reservation("fix-owned", state.binding, FIX), "review owner")
    # Increment 6: a current, ungated review binding admits its paid review, but
    # never through the resolve admission receipt (that is resolve-only).
    admitted = store.reserve(reservation("review-owned", state.binding, REVIEWER))
    assert admitted.disposition is AccountingDisposition.AVAILABLE, admitted.detail
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='workflow-admission'").fetchone()[0] == 0
    stale = replace(state.binding, episode_id="other-episode")
    assert_refused(store, reservation("stale", stale, REVIEWER), "stale")
    assert_refused(store, reservation("resolve-by-reviewer", resolve_snapshot().binding, REVIEWER, "RESOLVE"), "fix owner")


def test_review_admission_without_review_activation_is_refused(store):
    install_workflow(store, resolve_snapshot())
    assert_refused(store, reservation("inactive", held().binding, REVIEWER), "migration is required")


@pytest.mark.parametrize("case", ["review", "absent", "mismatched"])
def test_resolve_admission_receipt_is_never_recorded_for_other_bindings(store, case):
    # Called directly: the public reserve path refuses review bindings earlier,
    # so this pins the recorder's own guard (defence in depth).
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    resolve = resolve_snapshot()
    snapshot, binding = {
        "review": (held(), held().binding),
        "absent": (resolve, None),
        "mismatched": (resolve, replace(resolve.binding, episode_id="other-episode")),
    }[case]
    request = replace(reservation("direct", binding, FIX, "RESOLVE"), workflow=binding)
    with store.transaction() as db:
        versions = db.execute("SELECT target_kind, version FROM workflows ORDER BY target_kind").fetchall()
        with pytest.raises(WorkflowAuthorityError, match="only a resolve binding"):
            SQLiteControlStore._record_workflow_admission(db, request, snapshot, "2026-09-25T12:00:00Z")  # type: ignore[arg-type]
        assert db.execute("SELECT target_kind, version FROM workflows ORDER BY target_kind").fetchall() == versions
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='workflow-admission'").fetchone()[0] == 0


def review_command(binding, owner=REVIEWER, number=9, target=Target.PR):
    return command_for_action(Relabel(target, number, Label.NEEDS_HUMAN_REVIEW, Label.AI_REVIEW), repository="o/r",
                              executor_identity=owner, workflow=binding)


def raw_command(kind, binding, name, payload=None):
    return LifecycleCommand(CommandId(name), f"idem:{name}", kind, "pr:9", None, payload or {"pr": 9},
                            repository="o/r", executor_identity=REVIEWER, workflow=binding)


HEAD = "a" * 40


def guarded_relabel(binding, name="relabel", *, owner=REVIEWER, number=9, **changes):
    """A PR projection shaped as the review policy mints it, guard included."""
    payload = {
        "target": Target.PR.value, "number": number, "remove": Label.NEEDS_HUMAN_REVIEW.value,
        "add": Label.AI_REVIEW.value, "expected_revision": HEAD, "harness_signal": False, "removes": [],
        "completion_guard": None, "pull_request_hold_guard": {"head_sha": HEAD, "labels": ["needs-human-review"]},
    }
    payload.update(changes)
    return LifecycleCommand(CommandId(name), f"idem:{name}", CommandKind.RELABEL, f"pr:{number}", HEAD, payload,
                            repository="o/r", executor_identity=owner, workflow=binding)


def bound_submission(binding, name="submission", **changes):
    """A bound SUBMIT_REVIEW verdict shaped as `submit_bound_review` enqueues it."""
    payload = {"pr": 9, "event": "REQUEST_CHANGES", "body": "fix", "head": HEAD, "author": "review-bot[bot]",
               "patch_id": "f" * 64, "escalate_cause": None, "non_resetting_commits": [], **changes}
    return LifecycleCommand(CommandId(name), f"idem:{name}", CommandKind.SUBMIT_REVIEW, "pr:9", HEAD, payload,
                            repository="o/r", executor_identity=REVIEWER, workflow=binding)


def test_a_bound_submission_without_its_verdict_or_escalation_shape_is_unrepresentable(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    for index, changes in enumerate((
        {"escalate_cause": "ceiling"}, {"event": "COMMENT"}, {"patch_id": None}, {"author": ""}, {"head": "short"},
        {"pr": 10}, {"extra": True}, {"non_resetting_commits": ["a" * 40]},
        {"event": "COMMENT", "escalate_cause": "ceiling", "non_resetting_commits": ["b" * 40, "a" * 40]},
        {"event": "COMMENT", "escalate_cause": "ceiling", "non_resetting_commits": "a" * 40},
    )):
        with pytest.raises(ControlStoreError):
            store.enqueue_command(bound_submission(held().binding, f"malformed-{index}", **changes))
    assert store.pending_commands() == ()


def guarded_dismissal(binding, name="dismissal", **changes):
    payload = {"pr": 9, "review_id": "r0", "message": "re-armed",
               "guard": {"review_id": "r0", "author": "review-bot[bot]", "head_sha": HEAD, "state": "CHANGES_REQUESTED"}}
    payload.update(changes)
    return LifecycleCommand(CommandId(name), f"idem:{name}", CommandKind.DISMISS_REVIEW, "pr:9", HEAD, payload,
                            repository="o/r", executor_identity=REVIEWER, workflow=binding)


_REVIEW_KINDS = (CommandKind.DISMISS_REVIEW, CommandKind.RELABEL, CommandKind.SUBMIT_REVIEW)


def test_review_family_enqueues_only_dismissal_relabel_and_review_submission(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    store.enqueue_command(guarded_dismissal(held().binding, "allowed-dismiss_review"))
    store.enqueue_command(guarded_relabel(held().binding, "allowed-relabel"))
    store.enqueue_command(bound_submission(held().binding, "allowed-submit_review"))
    for kind in (k for k in CommandKind if k not in _REVIEW_KINDS):
        with pytest.raises(ControlStoreError, match=f"cannot enqueue {kind.value} commands"):
            store.enqueue_command(raw_command(kind, held().binding, f"refused-{kind.value}"))
    assert {p.command.kind for p in store.pending_commands()} == set(_REVIEW_KINDS)


def test_review_family_cannot_publish_branches_even_without_a_captured_run(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    for kind in (CommandKind.PUBLISH_BRANCH, CommandKind.OPEN_PULL_REQUEST, CommandKind.UPDATE_PULL_REQUEST_BODY):
        command = raw_command(kind, held().binding, f"publish-{kind.value}", {"branch": "agent/issue-7", "cwd": "/w"})
        with pytest.raises(ControlStoreError, match="review rejection workflow cannot enqueue"):
            store.enqueue_command(command)
    assert store.pending_commands() == ()


def test_review_commands_require_the_review_owner_and_this_pull_request(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    accepted = guarded_relabel(held().binding)
    store.enqueue_command(accepted)
    assert [p.command.workflow for p in store.pending_commands()] == [held().binding]
    refused = [
        guarded_relabel(held().binding, "fix-owned", owner=FIX),
        guarded_relabel(held().binding, "other-pr", number=10),
        review_command(held().binding, number=7, target=Target.ISSUE),
        guarded_relabel(replace(held().binding, episode_id="other"), "other-episode"),
        review_command(resolve_snapshot().binding, number=1, target=Target.ISSUE),
    ]
    for command in refused:
        with pytest.raises(ControlStoreError):
            store.enqueue_command(command)
    assert len(store.pending_commands()) == 1


def test_review_commands_without_review_activation_are_refused(store):
    install_workflow(store, resolve_snapshot())
    with pytest.raises(ControlStoreError, match="migration is required"):
        store.enqueue_command(guarded_relabel(held().binding))


@pytest.mark.parametrize("command", [
    lambda b: review_command(b),  # an Action-shaped relabel carries no hold guard
    lambda b: raw_command(CommandKind.RELABEL, b, "bare-relabel"),
    lambda b: raw_command(CommandKind.DISMISS_REVIEW, b, "bare-dismissal"),
    lambda b: guarded_relabel(b, removes=["ai-blocked"]),
    lambda b: guarded_relabel(b, harness_signal=True),
    lambda b: guarded_relabel(b, expected_revision="b" * 40),
    lambda b: guarded_relabel(b, pull_request_hold_guard={"head_sha": HEAD}),
    lambda b: guarded_relabel(b, pull_request_hold_guard={"head_sha": HEAD, "labels": ["p1"]}),
    lambda b: guarded_dismissal(b, review_id="r1"),
    lambda b: guarded_dismissal(b, guard={"review_id": "r0", "author": "review-bot[bot]", "head_sha": HEAD,
                                          "state": "APPROVED"}),
    lambda b: guarded_dismissal(b, message=""),
    lambda b: guarded_dismissal(b, pr=10),
    lambda b: replace(guarded_dismissal(b), expected_revision="b" * 40),
], ids=["action-relabel", "bare-relabel", "bare-dismissal", "extra-removals", "harness-signal", "revision",
        "guard-without-labels", "ordering-label-guard", "guard-for-another-review", "guard-state", "empty-message",
        "other-pr", "dismissal-revision"])
def test_review_commands_without_their_exact_guard_are_unrepresentable(store, command):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    with pytest.raises(ControlStoreError):
        store.enqueue_command(command(held().binding))
    assert store.pending_commands() == ()


def test_an_accepted_review_relabel_is_selectable_only_by_the_review_executor(store):
    # The increment 1-2 probe: review obligations were never runnable because
    # the drain read their workflow as resolve authority.
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    store.enqueue_command(guarded_relabel(held().binding))
    diagnostics = []
    selected = store.runnable_commands(repository="o/r", executor_identity=REVIEWER, diagnose=diagnostics.append)
    assert [p.command.command_id for p in selected] == [CommandId("relabel")] and diagnostics == []
    assert store.runnable_commands(repository="o/r", executor_identity=FIX, diagnose=diagnostics.append) == ()


def test_resolve_commands_keep_the_fix_owner_rule(store):
    install_workflow(store, resolve_snapshot())
    install_review(store, held())
    command = review_command(resolve_snapshot().binding, owner=FIX, number=1, target=Target.ISSUE)
    store.enqueue_command(command)
    with pytest.raises(ControlStoreError, match="fix owner"):
        store.enqueue_command(review_command(resolve_snapshot().binding, owner=REVIEWER, number=1, target=Target.ISSUE))


# -- increment 6: one admission path and read-only guidance once review is active ------------------

def test_an_unbound_paid_review_is_admitted_only_while_the_review_family_is_inactive(store):
    inactive = store.reserve(reservation("legacy", None, REVIEWER))
    assert inactive.disposition is AccountingDisposition.AVAILABLE, inactive.detail
    install_review(store, ReviewWorkflowSnapshot(held().revision, REVIEW, ReviewEpisode("review-episode")))
    assert_refused(store, reservation("unbound", None, REVIEWER), "captured workflow binding")
    assert_refused(store, reservation("unbound-re", None, REVIEWER, "RE_REVIEW"), "captured workflow binding")


def test_a_bound_paid_review_must_name_its_own_pull_request(store):
    state = ReviewWorkflowSnapshot(held().revision, REVIEW, ReviewEpisode("review-episode"))
    install_review(store, state)
    request = reservation("elsewhere", state.binding, REVIEWER)
    request = replace(request, dispatch_id=request.dispatch_id.replace('"number": 9', '"number": 10'))
    assert_refused(store, request, "disagrees with its workflow")


def test_a_gated_review_workflow_admits_no_paid_review(store):
    install_review(store, held())
    result = store.reserve(reservation("gated", held().binding, REVIEWER))
    assert result.disposition is AccountingDisposition.UNAVAILABLE


def test_review_guidance_and_activation_reads_distinguish_inactive_absent_and_committed(store):
    from issue_agent.adapters.review_workflow_control import read_review_guidance, review_family_activated

    assert review_family_activated(store, "o/r") is False
    assert read_review_guidance(store, "o/r", 9) is None  # the legacy record still owns it
    install_review(store, ReviewWorkflowSnapshot(held().revision, REVIEW, ReviewEpisode("review-episode", "Scope: parser")))
    assert review_family_activated(store, "o/r") is True
    assert read_review_guidance(store, "o/r", 9) == "Scope: parser"
    assert read_review_guidance(store, "o/r", 10) == ""  # activated but never observed


def test_an_unreadable_authority_raises_rather_than_reading_as_inactive(store):
    from issue_agent.adapters.review_workflow_control import read_review_guidance, review_family_activated

    install_review(store, ReviewWorkflowSnapshot(held().revision, REVIEW, ReviewEpisode("review-episode", "x")))
    with store.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='{' WHERE target_kind='pr'")
    with pytest.raises(WorkflowAuthorityError):
        read_review_guidance(store, "o/r", 9)
    with pytest.raises(ValueError):
        review_family_activated(object(), "o/r")  # type: ignore[arg-type]
