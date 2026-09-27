"""Review import filesystem safety, transactional rollback and retained authority."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import sqlite3

import pytest

from issue_agent.adapters import review_activation as module
from issue_agent.adapters.control_store import CorruptControlStore, SQLiteControlStore
from issue_agent.adapters.review_activation import (
    ReviewActivationRefused, ReviewActivationSourcesChanged, SQLiteReviewActivation,
    read_activation_commit_messages, read_legacy_review_roots, read_legacy_review_state,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.resolve_activation import ActivationBlocked, UnmappedLegacyEntry
from issue_agent.domain.review_activation import (
    LegacyHoldProvenance, OpenPullRequestsListed, ReviewActivationFacts, ReviewActivationPlan, ReviewFamilyActive, plan_review_activation,
)
from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState, SubmittedReviewFact, SubmittedReviewState
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey

HEAD, PATCH = "a" * 40, "b" * 64
NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


@pytest.fixture
def control(tmp_path):
    value = SQLiteControlStore(str(tmp_path / "control" / "db.sqlite"))
    value.initialize()
    return value


@pytest.fixture
def state(tmp_path):
    value = tmp_path / "state"
    value.mkdir()
    (value / "pr-7.last-sha").write_text(HEAD)
    (value / "pr-7.patch-id").write_text(PATCH)
    return value


def evaluate(control, state):
    inventory, recorded = SQLiteReviewActivation(control).inventory("o/r")
    reviews = CompleteReviews(tuple(SubmittedReviewFact(str(n), "bot", SubmittedReviewState.CHANGES_REQUESTED,
                                                        HEAD, NOW) for n in (1, 2)))
    read = ObservedPullRequest(WorkflowKey("o/r", PullRequestRef(7)), HEAD, PullRequestState.OPEN,
                               frozenset({Label.NEEDS_HUMAN_REVIEW}), reviews, NOW)
    return plan_review_activation("o/r", read_legacy_review_state(str(state)), (read,), OpenPullRequestsListed((7,)),
                                  inventory, ceiling=3, review_bot="bot", trigger_label=Label.AI_PR_OPEN, recorded=recorded,
                                  facts=(ReviewActivationFacts(7, (), (), "f" * 64, LegacyHoldProvenance.CEILING),))


def dump(control):
    with closing(sqlite3.connect(control.path)) as db:
        return tuple(db.iterdump())


@pytest.mark.parametrize("name", ["pr-.last-sha", "pr-0.patch-id", "pr-01.last-sha", "pr-x.operator-guidance",
                                 "pr-9223372036854775808.last-sha"])
def test_malformed_legacy_number_is_reported_not_ignored(state, name):
    (state / name).write_text("value")
    assert any(isinstance(entry, UnmappedLegacyEntry) and entry.name == name
               for entry in read_legacy_review_state(str(state)))


@pytest.mark.parametrize("kind", ["symlink", "directory", "invalid_utf8", "large"])
def test_unreadable_markers_refuse_instead_of_disappearing(control, state, kind, tmp_path):
    path = state / "pr-7.last-sha"
    path.unlink()
    if kind == "symlink":
        (tmp_path / "target").write_text(HEAD)
        path.symlink_to(tmp_path / "target")
    elif kind == "directory":
        path.mkdir()
    elif kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    else:
        path.write_text("x" * 100_000)
    decision = evaluate(control, state)
    assert isinstance(decision, ActivationBlocked)
    assert "unreadable" in " ".join(decision.reasons)


def test_distinct_roots_keep_duplicate_targets_and_deduplicate_root_aliases(state, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "pr-7.operator-guidance").write_text("note")
    records = read_legacy_review_roots((("review", str(state)), ("alias", str(state)), ("old", str(other))))
    assert len(records) == 2 and {record.root for record in records} == {"review", "old"}


@pytest.mark.parametrize("failure", ["snapshot", "gate", "journal"])
def test_failure_after_epoch_insertion_rolls_back_every_activation_row(control, state, monkeypatch, failure):
    activation = SQLiteReviewActivation(control)
    plan = evaluate(control, state)
    assert isinstance(plan, ReviewActivationPlan)
    before = dump(control)

    def crash(*args, **kwargs):
        raise RuntimeError("injected crash")

    if failure == "snapshot":
        monkeypatch.setattr(module, "imported_snapshot", crash)
    elif failure == "gate":
        monkeypatch.setattr(module, "persist_workflow_snapshot", crash)
    else:
        monkeypatch.setattr(control, "_transition", crash)
    with pytest.raises(RuntimeError, match="injected"):
        activation.activate(plan, revalidate=lambda: evaluate(control, state))
    assert dump(control) == before


def test_revalidation_catches_changed_sources_and_replay_never_reimports(control, state):
    activation = SQLiteReviewActivation(control)
    plan = evaluate(control, state)
    assert isinstance(plan, ReviewActivationPlan)
    (state / "pr-7.patch-id").write_text("c" * 64)
    before = dump(control)
    with pytest.raises(ReviewActivationSourcesChanged):
        activation.activate(plan, revalidate=lambda: evaluate(control, state))
    assert dump(control) == before
    plan = evaluate(control, state)
    assert isinstance(plan, ReviewActivationPlan)
    activation.activate(plan, revalidate=lambda: evaluate(control, state))
    before = dump(control)
    (state / "pr-7.patch-id").write_text("old stale bytes")
    assert isinstance(evaluate(control, state), ReviewFamilyActive)
    assert activation.activate(plan, revalidate=lambda: evaluate(control, state)).idempotent
    assert dump(control) == before
    wrong = replace(plan, source_digest="0" * 64)
    with pytest.raises(ReviewActivationRefused):
        activation.activate(wrong, revalidate=lambda: wrong)
    assert dump(control) == before


@pytest.mark.parametrize("corrupt", ["decision", "index", "evidence", "deleted_import", "canonical_edit", "plan_inputs"])
def test_corrupt_retained_activation_refuses_as_unavailable(control, state, corrupt):
    activation = SQLiteReviewActivation(control)
    plan = evaluate(control, state)
    activation.activate(plan, revalidate=lambda: evaluate(control, state))
    with closing(sqlite3.connect(control.path)) as db:
        if corrupt == "decision":
            db.execute("UPDATE workflow_activation_imports SET decision_json='{}'")
        elif corrupt == "index":
            db.execute("UPDATE workflow_activation_imports SET target_number=8")
        elif corrupt == "evidence":
            db.execute("DELETE FROM workflow_activation_evidence")
        elif corrupt == "deleted_import":
            db.execute("DELETE FROM workflow_activation_imports")
        elif corrupt == "canonical_edit":
            from issue_agent.domain.review_activation import ImportedReviewTarget
            raw = db.execute("SELECT decision_json FROM workflow_activation_imports").fetchone()[0]
            edited = replace(ImportedReviewTarget.parse(raw), guidance="rewritten valid guidance")
            db.execute("UPDATE workflow_activation_imports SET decision_json=?", (edited.canonical(),))
        else:
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-activation-plan'")
        db.commit()
    with pytest.raises(ValueError, match="activation evidence"):
        activation.inventory("o/r")


def test_inventory_does_not_create_missing_authority(tmp_path):
    control = SQLiteControlStore(str(tmp_path / "missing" / "control.sqlite"))
    with pytest.raises(CorruptControlStore):
        SQLiteReviewActivation(control).inventory("o/r")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("raw", [None, [], [[]], [[{}]], [[{"sha": HEAD, "commit": {}}]],
    [[{"sha": "d" * 40, "commit": {"message": "moved head"}}]],
    [[{"sha": HEAD, "commit": {"message": "same"}}] * 2],
    [[{"sha": f"{number:040x}", "commit": {"message": "many"}} for number in range(250)]]])
def test_incomplete_commit_trailers_never_count_approval_as_resetting(raw):
    class Gateway:
        def _gh_json(self, args):
            return raw

    with pytest.raises(ValueError):
        read_activation_commit_messages(Gateway(), "o/r", 7, HEAD)


def test_complete_commit_pages_keep_every_message_and_pin_the_head():
    class Gateway:
        def _gh_json(self, args):
            assert args == ["api", "--paginate", "--slurp", "repos/o/r/pulls/7/commits?per_page=100"]
            return [[{"sha": "b" * 40, "commit": {"message": "old\nbody"}}],
                    [{"sha": HEAD, "commit": {"message": "head"}}]]

    assert read_activation_commit_messages(Gateway(), "o/r", 7, HEAD) == ("old\nbody", "head")


def test_review_activation_preserves_an_already_activated_resolve_family(control, state):
    from issue_agent.adapters.resolve_activation import SQLiteResolveActivation, read_legacy_resolve_state
    from issue_agent.domain.resolve_activation import plan_resolve_activation

    (state / "issue-9.resolve-retries").write_text("3")
    resolve = SQLiteResolveActivation(control)

    def resolve_plan():
        return plan_resolve_activation("o/r", read_legacy_resolve_state(str(state)), resolve.inventory("o/r"), ceiling=3)

    plan = resolve_plan()
    resolve.activate(plan, revalidate=resolve_plan)
    with closing(sqlite3.connect(control.path)) as db:
        before = db.execute("SELECT * FROM workflows WHERE target_kind='issue'").fetchall()
    review = SQLiteReviewActivation(control)
    review.activate(evaluate(control, state), revalidate=lambda: evaluate(control, state))
    with closing(sqlite3.connect(control.path)) as db:
        assert db.execute("SELECT * FROM workflows WHERE target_kind='issue'").fetchall() == before
        assert db.execute("SELECT count(*) FROM workflow_authority_epochs").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM workflows WHERE target_kind='pr'").fetchone()[0] == 1
