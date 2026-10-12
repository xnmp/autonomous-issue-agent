"""Offline review activation over real SQLite authority and read-only remote fixtures."""

from tests.domain.builders import observed_pr

from contextlib import closing
from dataclasses import replace
import gc
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.github import parse_review_pages
from issue_agent.adapters.resolve_activation import file_digests
from issue_agent.adapters.review_activation import SQLiteReviewActivation, ReviewActivationSourcesChanged
from issue_agent.adapters.workflow_rows import load_workflow_row
from issue_agent.app.review_activation import (
    ActivationNotConfirmed, ReviewActivationScope, activate, evaluate, inspect, rehearse,
)
from issue_agent.domain.contracts import RunId
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewConfig
from issue_agent.domain.review_activation import ReviewActivationPlan
from issue_agent.domain.review_workflow import FreshReviewRequired, ImportedBaseline, ReviewedBaseline
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey
from issue_agent.domain.world import IssueView, PRView

HEAD, PATCH, BOT, OWNER = "a" * 40, "b" * 64, "review-bot", "o/r:review:test"


def review_row(number=1, state="CHANGES_REQUESTED", minute=0):
    return {"id": number, "state": state, "user": {"login": BOT}, "commit_id": HEAD,
            "submitted_at": f"2026-09-27T00:{minute:02d}:00Z", "body": (
                "Through 3 rounds of requested changes without resolution. Requesting human review.\n\nDetails."
                if state == "COMMENTED" else "")}


class Remote:
    def __init__(self):
        self.views = {7: observed_pr(7, state="OPEN", head_ref_oid=HEAD, base_ref_oid="c" * 40,
                               labels=frozenset({Label.AI_PR_OPEN}), author="author", head_ref_name="issue-3")}
        self.reviews = {7: [[review_row()]]}
        self.listing = None
        self.calls = []
        self.diff_text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        self.messages = ()
        self.issues = {3: IssueView(3, state="open", updated_at="2026-09-27T00:00:00Z")}
        self.dependencies = {}

    def _gh_json(self, args):
        self.calls.append(tuple(args))
        if args[-1].endswith("/reviews?per_page=100"):
            return self.reviews[int(args[-1].split("/")[-2])]
        if args[-1].endswith("/commits?per_page=100"):
            return [[{"sha": HEAD, "commit": {"message": "\n".join(self.messages)}}]]
        if self.listing is not None:
            return self.listing
        return [[{"number": number, "state": "open"} for number, view in self.views.items() if view.state == "OPEN"]]

    def get_pr(self, number, **kwargs):
        return self.views[number]

    def get_issue(self, number):
        return self.issues[number]

    def open_blockers(self, number):
        return self.dependencies.get(number, ())

    def pull_request_reviews(self, number):
        return parse_review_pages(self.reviews[number])

    def pr_diff(self, number, base, head):
        from types import SimpleNamespace
        return SimpleNamespace(base_oid=base, head_oid=head, text=self.diff_text)

    def pr_commit_messages(self, number):
        return self.messages


@pytest.fixture
def deployment(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    control = SQLiteControlStore(str(tmp_path / "authority" / "control.sqlite"))
    control.initialize()
    remote = Remote()
    scope = ReviewActivationScope("test", "o/r", control.path, (("review", str(state)),), str(state),
                                  3, BOT, Label.AI_PR_OPEN, remote, review_owner=OWNER)
    return state, control, remote, scope


def seed(state, number=7):
    (state / f"pr-{number}.last-sha").write_text(HEAD)
    (state / f"pr-{number}.patch-id").write_text(PATCH)
    (state / f"pr-{number}.operator-guidance").write_text("Keep the API stable")


def dump(control):
    with closing(sqlite3.connect(control.path)) as db:
        return tuple(db.iterdump())


def settle(control):
    """Keep fixture WAL checkpointing outside the rehearsal's source digests."""
    gc.collect()
    with closing(sqlite3.connect(control.path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_inventory_shadow_and_activation_import_exact_continuation(deployment):
    state, control, remote, scope = deployment
    seed(state)
    before = dump(control)
    inventory = inspect(scope, shadow=False)
    shadow = inspect(scope, shadow=True)
    assert inventory["status"] == shadow["status"] == "ready"
    assert dump(control) == before
    assert shadow["paid_reviews_on_activation"] == []
    assert shadow["shadow"][0]["new_dispatch"] == "reviewed"
    digest = shadow["plan"]["plan_digest"]
    assert activate(scope, confirm_plan_digest=digest)["status"] == "activated"
    with control.transaction() as db:
        snapshot = load_workflow_row(db, WorkflowKey("o/r", PullRequestRef(7)))
        assert snapshot.episode.guidance == "Keep the API stable"
        baseline = snapshot.episode.continuation
        assert isinstance(baseline, ReviewedBaseline) and isinstance(baseline.source, ImportedBaseline)
        assert baseline.head_sha == HEAD and baseline.patch_id == PATCH
        assert not db.execute("SELECT 1 FROM dedupe_keys WHERE namespace='review-dispatch-decision'").fetchall()
    assert all(call[0] == "api" for call in remote.calls)


def test_replay_uses_retained_evidence_despite_changed_files_remote_and_active_work(deployment):
    state, control, remote, scope = deployment
    seed(state)
    digest = inspect(scope, shadow=True)["plan"]["plan_digest"]
    activate(scope, confirm_plan_digest=digest)
    (state / "pr-7.last-sha").write_text("broken")
    remote.views.clear()
    remote.calls.clear()
    control.start_run(RunId("live"), "dispatch", "REVIEW", OWNER, "cfg", state="running")
    before = dump(control)
    assert activate(scope, confirm_plan_digest=digest)["status"] == "already_active_with_this_plan"
    assert not remote.calls and dump(control) == before
    with pytest.raises(ActivationNotConfirmed):
        activate(scope, confirm_plan_digest="0" * 64)


@pytest.mark.parametrize("corrupt", ["delete", "canonical_edit"])
def test_inspection_and_same_digest_replay_refuse_corrupted_import_evidence(deployment, corrupt):
    from issue_agent.domain.review_activation import ImportedReviewTarget

    state, control, _, scope = deployment
    seed(state)
    digest = inspect(scope, shadow=True)["plan"]["plan_digest"]
    activate(scope, confirm_plan_digest=digest)
    with control.transaction() as db:
        if corrupt == "delete":
            db.execute("DELETE FROM workflow_activation_imports")
        else:
            raw = db.execute("SELECT decision_json FROM workflow_activation_imports").fetchone()[0]
            edited = replace(ImportedReviewTarget.parse(raw), guidance="changed guidance")
            db.execute("UPDATE workflow_activation_imports SET decision_json=?", (edited.canonical(),))
    before = dump(control)
    with pytest.raises(ValueError, match="confirmed plan digest"):
        inspect(scope, shadow=False)
    with pytest.raises(ValueError, match="confirmed plan digest"):
        activate(scope, confirm_plan_digest=digest)
    assert dump(control) == before


@pytest.mark.parametrize("change", ["file", "reviews", "head", "listing", "ceiling"])
def test_changed_plan_requires_a_fresh_confirmation(deployment, change):
    state, control, remote, scope = deployment
    seed(state)
    digest = inspect(scope, shadow=True)["plan"]["plan_digest"]
    if change == "file":
        (state / "pr-7.operator-guidance").write_text("new instruction")
    elif change == "reviews":
        remote.reviews[7][0].append(review_row(2, minute=2))
    elif change == "head":
        remote.views[7] = replace(remote.views[7], head_ref_oid="d" * 40)
    elif change == "listing":
        remote.views[8] = replace(remote.views[7], number=8)
        remote.reviews[8] = [[]]
    else:
        scope = replace(scope, ceiling=4)
    before = dump(control)
    with pytest.raises(ActivationNotConfirmed):
        activate(scope, confirm_plan_digest=digest)
    assert dump(control) == before


@pytest.mark.parametrize("bad_listing", [None, {}, [], [[None]], [[{"number": True, "state": "open"}]],
    [[{"number": 7, "state": "open"}, {"number": 7, "state": "open"}]]])
def test_incomplete_or_malformed_listing_never_activates(deployment, bad_listing, monkeypatch):
    _, control, remote, scope = deployment
    monkeypatch.setattr(remote, "_gh_json", lambda args: bad_listing)
    before = dump(control)
    result = activate(scope, confirm_plan_digest="0" * 64)
    assert result["status"] == "blocked"
    assert "listing" in " ".join(result["blockers"])
    assert dump(control) == before


def test_ceiling_import_and_missing_baseline_fresh_obligation(deployment):
    state, control, remote, scope = deployment
    (state / "pr-7.operator-guidance").write_text("scope")
    remote.views[7] = replace(remote.views[7], labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    remote.reviews[7] = [[review_row(1), review_row(2, minute=1), review_row(3, "COMMENTED", 2)]]
    shadow = inspect(scope, shadow=True)
    assert shadow["shadow"][0]["ceiling_gate"]
    activate(scope, confirm_plan_digest=shadow["plan"]["plan_digest"])
    with control.transaction() as db:
        snapshot = load_workflow_row(db, WorkflowKey("o/r", PullRequestRef(7)))
        assert snapshot.episode.continuation == FreshReviewRequired(0)
        assert [r.review_id for r in snapshot.gates[0].cause.counted] == ["1", "2"]


def test_unlabelled_ceiling_cannot_disappear_when_history_becomes_partial(deployment):
    _, control, remote, scope = deployment
    remote.views[7] = replace(remote.views[7], labels=frozenset())
    remote.reviews[7] = [[review_row(1), review_row(2, minute=1), review_row(3, "COMMENTED", 2)]]
    complete = inspect(scope, shadow=True)
    assert complete["status"] == "ready" and complete["shadow"][0]["ceiling_gate"]
    remote.reviews[7] = [[review_row(1)], None]
    before = dump(control)
    partial = activate(scope, confirm_plan_digest=complete["plan"]["plan_digest"])
    assert partial["status"] == "blocked"
    assert "whether a rejection ceiling holds it is unknown" in " ".join(partial["blockers"])
    assert dump(control) == before


@pytest.mark.parametrize("body,labels,expected", [
    ("The reviewer returned ESCALATE on rejection 3 of 3. Requesting human review.\n\nNeeds design input.",
     frozenset({Label.NEEDS_HUMAN_REVIEW}), "verdict"),
    ("The reviewer returned ESCALATE on rejection 3 of 3. Requesting human review.\n\nNeeds design input.",
     frozenset(), "blocked"),
    ("Unclassified human attention reason", frozenset({Label.NEEDS_HUMAN_REVIEW}), "blocked"),
    ("Quoted text:\n\nThrough 3 rounds of requested changes without resolution. Requesting human review.",
     frozenset({Label.NEEDS_HUMAN_REVIEW}), "blocked"),
])
def test_import_never_turns_an_independent_or_unknown_hold_into_a_rearmable_ceiling(deployment, body, labels, expected):
    state, control, remote, scope = deployment
    seed(state)
    remote.views[7] = replace(remote.views[7], labels=labels)
    comment = {**review_row(3, "COMMENTED", 2), "body": body}
    remote.reviews[7] = [[review_row(1), review_row(2, minute=1), comment]]
    shadow = inspect(scope, shadow=True)
    if expected == "blocked":
        assert shadow["status"] == "blocked"
        assert "hold" in " ".join(shadow["blockers"])
        return
    assert shadow["status"] == "ready"
    assert shadow["plan"]["imports"][0]["ceiling"] is None
    assert shadow["paid_reviews_on_activation"] == []
    activate(scope, confirm_plan_digest=shadow["plan"]["plan_digest"])
    with control.transaction() as db:
        snapshot = load_workflow_row(db, WorkflowKey("o/r", PullRequestRef(7)))
        assert snapshot.gates == ()
        assert isinstance(snapshot.episode.continuation, ReviewedBaseline)
        assert not db.execute("SELECT 1 FROM lifecycle_commands").fetchall()
    assert remote.views[7].labels == frozenset({Label.NEEDS_HUMAN_REVIEW})


def test_guidance_is_imported_verbatim_into_the_persisted_episode(deployment):
    state, control, _, scope = deployment
    seed(state)
    note = "\tKeep café and 𝄞 intact.\n  "
    (state / "pr-7.operator-guidance").write_text(note)
    digest = inspect(scope, shadow=True)["plan"]["plan_digest"]
    activate(scope, confirm_plan_digest=digest)
    with control.transaction() as db:
        snapshot = load_workflow_row(db, WorkflowKey("o/r", PullRequestRef(7)))
        assert snapshot.episode.guidance == note


def test_newline_filename_is_preserved_by_rehearsal_and_refuses_import(deployment, tmp_path):
    state, control, _, scope = deployment
    (state / "pr-\n7.last-sha").write_text(HEAD)
    settle(control)
    result = rehearse(scope, copy_to=str(tmp_path / "copy"))
    assert result["status"] == "blocked" and result["checks"]["copied_sources_match"]
    assert result["checks"]["originals_unchanged"]
    assert any("unmapped legacy key" in reason for reason in result["blockers"])


def test_shadow_respects_unlabelled_adoption_and_unproductive_counter(deployment):
    state, _, remote, scope = deployment
    remote.views[7] = replace(remote.views[7], labels=frozenset())
    scope = replace(scope, config=ReviewConfig(BOT, adopt_unlabeled=True, max_unproductive_rounds=2))
    shadow = inspect(scope, shadow=True)
    assert shadow["paid_reviews_on_activation"] == [7]
    digest = shadow["plan"]["plan_digest"]
    (state / "issue--7.infra-deaths").write_text("2")
    held = inspect(scope, shadow=True)
    assert held["paid_reviews_on_activation"] == []
    assert "unproductive" in held["shadow"][0]["new_dispatch"]
    assert held["plan"]["plan_digest"] != digest


def test_rehearsal_only_changes_isolated_copy_and_verifies_imports(deployment, tmp_path):
    state, control, _, scope = deployment
    seed(state)
    (state / "issue--7.infra-deaths").write_text("2")
    original_plan = inspect(scope, shadow=True)["plan"]["plan_digest"]
    settle(control)
    originals = (control.path, control.authority_witness_path, *(str(p) for p in state.iterdir()))
    before = file_digests(originals)
    result = rehearse(scope, copy_to=str(tmp_path / "copy"))
    assert result["status"] == "rehearsed", result
    assert result["plan"]["plan_digest"] == original_plan
    assert all(result["checks"].values()) and not result["verification_failures"]
    assert before == file_digests(originals)
    assert inspect(scope, shadow=False)["status"] == "ready"


def test_approval_resets_rejections_during_import_despite_legacy_trailers(deployment):
    state, control, remote, scope = deployment
    seed(state)
    remote.views[7] = replace(remote.views[7], labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    remote.reviews[7] = [[review_row(1), review_row(2, minute=1), review_row(3, "APPROVED", 2),
                          review_row(4, "COMMENTED", 3)]]
    remote.messages = ("Gate-Exemption: required-test missing_test",)
    result = inspect(scope, shadow=True)
    assert result["status"] == "ready", result
    assert not result["shadow"][0]["ceiling_gate"]
    activate(scope, confirm_plan_digest=result["plan"]["plan_digest"])
    with control.transaction() as db:
        snapshot = load_workflow_row(db, WorkflowKey("o/r", PullRequestRef(7)))
        assert snapshot.gates == ()


def test_activation_does_not_read_retired_grant_evidence(deployment, monkeypatch):
    state, _, remote, scope = deployment
    seed(state)
    remote.reviews[7] = [[review_row(1, "APPROVED")]]

    original = remote._gh_json

    def unavailable(args):
        if args[-1].endswith("/commits?per_page=100"):
            raise AssertionError("activation must not read exemption trailers")
        return original(args)

    monkeypatch.setattr(remote, "_gh_json", unavailable)
    result = inspect(scope, shadow=True)
    assert result["status"] == "ready", result
    assert not result["shadow"][0]["ceiling_gate"]


def test_malformed_counter_defers_instead_of_inventing_paid_dispatch(deployment):
    state, _, _, scope = deployment
    (state / "issue--7.infra-deaths").write_text("not a counter")
    result = inspect(scope, shadow=True)
    assert result["status"] == "blocked" and "counter" in " ".join(result["blockers"])


def test_work_started_after_revalidation_refuses_in_atomic_commit(deployment):
    state, control, _, scope = deployment
    seed(state)
    adapter = SQLiteReviewActivation(control)
    plan = evaluate(scope, adapter).decision
    assert isinstance(plan, ReviewActivationPlan)

    def race():
        fresh = evaluate(scope, adapter).decision
        control.start_run(RunId("race"), "dispatch", "REVIEW", OWNER, "cfg", state="running")
        return fresh

    with pytest.raises(ReviewActivationSourcesChanged):
        adapter.activate(plan, revalidate=race)
    assert adapter.inventory("o/r")[0].activation is None


def test_shadow_preserves_legacy_dispatch_and_shows_new_linked_issue_hold(deployment):
    _, _, remote, scope = deployment
    remote.views[7] = replace(remote.views[7], closing_issue=3)
    remote.reviews[7] = [[]]
    remote.issues[3] = replace(remote.issues[3], labels=frozenset({Label.AI_BLOCKED}))
    result = inspect(scope, shadow=True)
    assert result["status"] == "ready", result
    row, = result["shadow"]
    assert row["legacy_dispatch"] == "review"
    assert row["new_dispatch"] == "held: linked issue #3: ai-blocked"
    assert result["paid_reviews_on_activation"] == []


def test_known_linked_issue_read_failure_blocks_activation(deployment):
    _, control, remote, scope = deployment
    remote.views[7] = replace(remote.views[7], closing_issue=3)
    remote.issues.clear()
    before = dump(control)
    result = activate(scope, confirm_plan_digest="0" * 64)
    assert result["status"] == "blocked"
    assert "linked issue unavailable" in " ".join(result["blockers"])
    assert dump(control) == before


@pytest.mark.parametrize("change", ["labels", "state", "updated_at", "dependency", "identity"])
def test_linked_issue_evidence_changes_invalidate_confirmed_activation_plan(deployment, change):
    _, control, remote, scope = deployment
    remote.views[7] = replace(remote.views[7], closing_issue=3)
    before = inspect(scope, shadow=True)
    assert before["status"] == "ready", before
    if change == "dependency":
        remote.dependencies[3] = (9,)
    elif change == "identity":
        remote.views[7] = replace(remote.views[7], head_ref_name="feature", closing_issue=4)
        remote.issues[4] = replace(remote.issues[3], number=4)
    else:
        value = {"labels": frozenset({Label.AI_PARKED}), "state": "closed",
                 "updated_at": "2026-09-27T00:01:00Z"}[change]
        remote.issues[3] = replace(remote.issues[3], **{change: value})
    original_db = dump(control)
    with pytest.raises(ActivationNotConfirmed):
        activate(scope, confirm_plan_digest=before["plan"]["plan_digest"])
    assert dump(control) == original_db


def test_shadow_reads_current_sql_resolve_hold_on_linked_issue(deployment):
    from issue_agent.domain.workflow import NoAuthorizedRemediation, OpenGate, ResolveCeiling
    from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

    _, control, remote, scope = deployment
    remote.views[7] = replace(remote.views[7], closing_issue=3)
    remote.reviews[7] = [[]]
    original = inspect(scope, shadow=True)
    assert original["shadow"][0]["new_dispatch"] == "review"
    snapshot = resolve_snapshot(3)
    gate = OpenGate("resolve-hold", snapshot.revision.fence.generation,
                    ResolveCeiling(snapshot.episode.episode_id, 3, 3), NoAuthorizedRemediation("operator decision"))
    install_workflow(control, replace(snapshot, gates=(gate,)))
    result = inspect(scope, shadow=True)
    assert result["status"] == "ready", result
    row, = result["shadow"]
    assert row["legacy_dispatch"] == "review"
    assert "linked issue #3: unresolved workflow gates" in row["new_dispatch"]
    assert result["paid_reviews_on_activation"] == []
    with pytest.raises(ActivationNotConfirmed):
        activate(scope, confirm_plan_digest=original["plan"]["plan_digest"])
