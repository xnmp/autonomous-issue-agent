"""ADR 0015 review-rejection fault matrix through the composed review daemon.

Every case runs ``review_acceptance_harness`` (production
``compose_review_daemon`` over real SQLite, a real Git review checkout and a
persistent GitHub double) and restarts it over the same databases. Assertions
read public outcomes: GitHub reviews/labels and harness write calls, paid
provider launches, receipts and ``load_workflow``.
"""

import json
import asyncio
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from issue_agent.adapters.store import Store
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewConfig
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review_dispatch_record import ReviewNotAdmitted
from issue_agent.domain.review_execution import PaidReviewProgress, ReviewRoundOutcome
from issue_agent.domain.review_rounds import (
    ReviewChildReturned, ReviewParentAbandoned, ReviewRoundFacts, ReviewSubmissionConfirmed,
)
from issue_agent.domain.world import IssueView
from issue_agent.domain.review_workflow import (
    CompletedReviewRearm, PendingReviewRearm, ReviewedBaseline, ReviewRejectionCeiling,
    SupersededReviewRearm,
)
from issue_agent.domain.workflow import (
    ClearGuidance, ContinueGrantedRearm, EvidenceUnavailable, OpenGate, SetGuidance, StopReason, WorkflowAbsent,
    WorkflowCommitted, WorkflowMigrationRequired,
)
from tests.app.resolve_acceptance_harness import ScriptedProvider, run_until_exit
from tests.fakes.persistent_github import PersistentGitHub
from tests.app.review_acceptance_harness import (
    ACTOR, BOT, KEY, NHR, PR, TRIGGER, child_environment, compose_review, create_review_deployment, diff_text,
    install_clock, isolate_authority, verdicts,
)

RC, OK = "REQUEST_CHANGES", "APPROVE"


@pytest.fixture
def bare(tmp_path, monkeypatch):
    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    return deployment


@pytest.fixture
def deployment(bare, monkeypatch):
    bare.activate()
    compose_review(bare).activate_execution(monkeypatch)
    return bare


@pytest.mark.parametrize("label", [Label.AI_BLOCKED, Label.AI_PARKED, Label.NEEDS_HUMAN_REVIEW,
                                    Label.NEEDS_BUDGET_REVIEW])
def test_linked_issue_labels_hold_review_without_spend_or_issue_mutation(deployment, label):
    remote = deployment.github()
    remote.relabel("issue", 1, None, label.value)
    before = remote.calls
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)))
    client.boot()
    client.tick(3)
    assert client.provider.tasks == [] and client.charges() == [] and client.submissions() == []
    assert remote.calls == before and label in remote.get_issue(1).labels
    remote.relabel("issue", 1, label.value, None)
    client.tick()
    assert len(client.provider.tasks) == 1 and client.bot_reviews()[-1].state == "APPROVED"


def test_linked_resolve_gate_holds_review_even_without_an_issue_hold_label(deployment):
    from issue_agent.adapters.control_store import SQLiteControlStore
    from issue_agent.domain.workflow import NoAuthorizedRemediation, ResolveCeiling
    from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

    store = SQLiteControlStore(deployment.control_path)
    state = resolve_snapshot(1)
    cause = ResolveCeiling(state.episode.episode_id, 3, 3)
    install_workflow(store, replace(state, gates=(OpenGate("resolve:held", 2, cause,
                                                         NoAuthorizedRemediation("operator decision")),)))
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)))
    client.boot()
    client.tick(3)
    assert client.provider.tasks == [] and client.charges() == [] and client.submissions() == []


def test_issue_hold_added_after_review_planning_is_rechecked_before_paid_work(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, OK)))
    client.boot()
    client.tick()
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    runtime = client.review.runtime
    assert runtime is not None
    plan = runtime.plan(client.remote.build_review_world(), ReviewConfig(review_bot=BOT))
    _, binding = plan.owed[PR]
    client.remote.relabel("issue", 1, None, Label.AI_PARKED.value)
    with pytest.raises(WorkflowAuthorityError, match="linked issue #1: ai-parked"):
        runtime.begin(binding, deployment.heads[1], expected_claim_target=1)
    assert len(client.provider.tasks) == 1 and len(client.charges()) == 1


def test_relink_after_the_dispatch_claim_check_defers_before_any_paid_work(deployment, monkeypatch):
    from issue_agent.app.workflows.review import ReviewWorkflow

    client = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)))
    client.remote.seed(IssueView(2, title="Another issue", state="open"))
    original = ReviewWorkflow._claim_matches

    def relink_after_claim(workflow, pr, claim_target):
        matches = original(workflow, pr, claim_target)
        assert matches
        client.remote.external_pr_change(PR, closing_issue=2, head_ref_name="fix/2-relinked")
        return matches

    client.boot()
    with monkeypatch.context() as patch:
        patch.setattr(ReviewWorkflow, "_claim_matches", relink_after_claim)
        client.tick()
    assert client.provider.tasks == [] and client.charges() == [] and client.bot_reviews() == []
    assert any("issue linkage changed during dispatch" in line for line in client.log)
    assert Store(client.cfg.review_state_dir).get_infra_deaths(-PR) == 0
    client.tick()
    assert len(client.provider.tasks) == 1 and len(client.charges()) == 1
    assert client.bot_reviews()[-1].state == "APPROVED"


def test_an_excluded_unobserved_pr_preserves_its_basis_without_creating_a_workflow(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)))
    before = client.state()
    client.remote.external_pr_change(PR, labels=[Label.AI_PARKED.value])
    client.boot()
    runtime = client.review.runtime
    assert runtime is not None
    plan = runtime.plan(client.remote.build_review_world(), ReviewConfig(review_bot=BOT))
    decision = plan.decisions[PR]
    assert isinstance(decision.decision, ReviewNotAdmitted)
    assert decision.basis is not None and decision.basis.external_holds == ("ai-parked",)
    runtime.record_plan(plan)
    client.tick()
    assert client.state() == before
    assert client.provider.tasks == [] and client.charges() == []
    assert not any("dispatch decisions not recorded" in line for line in client.log)


def test_known_link_read_failure_is_bounded_visible_and_never_becomes_issueless(deployment, monkeypatch):
    broken, issue_reads = [False], []
    original = PersistentGitHub.get_issue

    def read(self, number):
        issue_reads.append(number)
        if broken[0]:
            raise TimeoutError("linked issue unavailable")
        return original(self, number)

    monkeypatch.setattr(PersistentGitHub, "get_issue", read)
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, OK)))
    client.boot()
    client.tick()
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    broken[0] = True
    tick_reads = []
    for _ in range(120):
        before = len(issue_reads)
        client.tick()
        tick_reads.append(len(issue_reads) - before)
        if any(g.gate_id.startswith("linked-issue:") for g in client.gates()):
            break
    assert any(isinstance(g.cause, EvidenceUnavailable) and g.gate_id.startswith("linked-issue:")
               for g in client.gates())
    assert 0 in tick_reads and sum(tick_reads) <= 12 < len(tick_reads)
    assert len(client.provider.tasks) == 1 and len(client.charges()) == 1
    broken[0] = False
    client.remote.relabel("issue", 1, None, Label.AI_BLOCKED.value)
    progress_until(client, lambda c: not any(g.gate_id.startswith("linked-issue:") for g in c.gates()),
                   limit=6, minutes=31)
    assert len(client.provider.tasks) == 1
    client.remote.relabel("issue", 1, Label.AI_BLOCKED.value, None)
    client.tick()
    assert len(client.provider.tasks) == 2 and client.bot_reviews()[-1].state == "APPROVED"


def test_explicitly_issueless_pr_needs_no_issue_read(deployment, monkeypatch):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)))
    client.remote.external_pr_change(PR, closing_issue=None, head_ref_name="feature/no-issue")

    def unexpected(self, number):
        raise AssertionError("an explicitly issueless PR must not fetch an issue")

    monkeypatch.setattr(PersistentGitHub, "get_issue", unexpected)
    client.boot()
    client.tick()
    assert len(client.provider.tasks) == 1 and client.bot_reviews()[-1].state == "APPROVED"


def test_local_linked_authority_failure_never_advances_the_remote_read_backoff(deployment, monkeypatch):
    from issue_agent.adapters import review_linkage
    from issue_agent.domain.review_evidence import ReviewEvidenceKind

    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, OK)))
    client.boot()
    client.tick()
    head = deployment.heads[1]
    client.remote.external_pr_change(PR, head_ref_oid=head)

    def unavailable(*args):
        raise sqlite3.OperationalError("linked workflow storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(review_linkage, "linked_issue_workflow_holds", unavailable)
        client.tick(3)
    assert len(client.provider.tasks) == 1 and len(client.charges()) == 1
    assert client.workflow.evidence_due(KEY, head, ReviewEvidenceKind.LINKED_ISSUE)
    assert not any(g.gate_id.startswith("linked-issue:") for g in client.gates())
    client.tick()
    assert len(client.provider.tasks) == 2 and client.bot_reviews()[-1].state == "APPROVED"


def unbound_submissions(client) -> int:
    with sqlite3.connect(client.review.control.path) as db:
        return db.execute("SELECT COUNT(*) FROM lifecycle_commands WHERE kind='submit_review' "
                          "AND workflow_binding_json IS NULL").fetchone()[0]


def escalate(client) -> None:
    """Initial rejection at head 0, a push, then the ceiling-th rejection escalates (ceiling 2).

    The gate commits when the escalation's receipt settles; its hold is a
    durable projection the next drain applies.
    """
    client.boot()
    client.tick()
    client.remote.external_pr_change(PR, head_ref_oid=client.deployment.heads[1])
    client.tick()
    assert client.gate_causes() == ["ReviewRejectionCeiling"]
    client.tick()
    assert NHR in client.labels() and TRIGGER not in client.labels()


def rearmed(client) -> bool:
    return isinstance(client.state().intent, CompletedReviewRearm)


def progress_until(client, done, *, limit: int = 12, minutes: float = 1) -> None:
    for _ in range(limit):
        if done(client):
            return
        client.tick(minutes=minutes)
    assert done(client), f"condition not reached within {limit} ticks: {client.log[-8:]}"


# -- inactive families require migration ---------------------------------------------------------

def test_an_inactive_repository_requires_migration_and_never_buys_a_review(bare):
    client = compose_review(bare, provider=ScriptedProvider(verdicts(RC)))
    client.boot()
    client.tick(3)
    assert client.provider.tasks == [] and client.submissions() == []
    assert isinstance(client.workflow.load_workflow(KEY), WorkflowMigrationRequired)
    assert unbound_submissions(client) == 0
    assert sum("review_migration_required" in line for line in client.log) == 1


# -- the active path: baselines, patch reuse, fresh reviews -------------------------------------

def test_an_active_review_is_bound_records_its_baseline_and_is_bought_once(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC)))
    client.boot()
    assert client.state().execution.last_sequence == 0
    client.tick(3)
    assert len(client.provider.tasks) == 1 and len(client.submissions()) == 1
    baseline = client.state().episode.continuation
    assert isinstance(baseline, ReviewedBaseline) and baseline.head_sha == deployment.heads[0]
    assert unbound_submissions(client) == 0
    # The legacy head cache is not a second authority once the family is active.
    assert not (Path(client.cfg.review_state_dir) / f"pr-{PR}.last-sha").exists()


def test_an_unchanged_patch_reuses_the_same_generation_baseline_without_a_paid_leg(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC)))
    client.boot()
    client.tick()
    rebased = deployment.heads[2]
    client.remote.set_diff(rebased, diff_text("change0", offset=9))  # same patch, moved hunks
    client.remote.external_pr_change(PR, head_ref_oid=rebased)
    client.tick(2)
    assert len(client.provider.tasks) == 1
    assert [call[3] for call in client.submissions()] == [deployment.heads[0], rebased]
    baseline = client.state().episode.continuation
    assert isinstance(baseline, ReviewedBaseline) and baseline.head_sha == rebased
    assert baseline.reviewed_generation == 0 and unbound_submissions(client) == 0


def test_a_completed_rearm_buys_exactly_one_fresh_review_even_for_an_unchanged_patch(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    assert isinstance(client.request(), WorkflowCommitted)
    progress_until(client, lambda c: len(c.provider.tasks) == 3)
    client.tick(3)
    assert rearmed(client) and len(client.provider.tasks) == 3
    reviews = client.bot_reviews()
    assert [r.state for r in reviews] == ["DISMISSED", "COMMENTED", "APPROVED"]
    baseline = client.state().episode.continuation
    assert isinstance(baseline, ReviewedBaseline) and baseline.reviewed_generation == 1
    assert baseline.head_sha == deployment.heads[1] and TRIGGER in client.labels()
    assert unbound_submissions(client) == 0


def test_reescalation_after_a_completed_rearm_opens_a_new_generation_gate(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, RC, RC)))
    escalate(client)
    first_gate = client.gates()[0].gate_id
    client.request()
    progress_until(client, lambda c: len(c.provider.tasks) == 3)
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[2])
    progress_until(client, lambda c: bool(c.gate_causes()) and NHR in c.labels())
    (gate,) = client.gates()
    assert isinstance(gate.cause, ReviewRejectionCeiling) and gate.gate_id != first_gate
    assert gate.opened_generation == 1 and NHR in client.labels()
    client.tick(3)
    assert len(client.provider.tasks) == 4


def test_a_human_dismissing_the_counted_reviews_releases_the_ceiling_and_buys_one_review(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    generation = client.state().revision.fence.generation
    (counted,) = client.gates()[0].cause.counted
    client.remote.external_review_change(counted.review_id, state="DISMISSED")
    progress_until(client, lambda c: len(c.provider.tasks) == 3, minutes=1)
    client.tick(3)
    state = client.state()
    assert state.gates == () and state.revision.fence.generation == generation + 1
    assert TRIGGER in client.labels() and NHR not in client.labels()
    assert len(client.provider.tasks) == 3 and client.bot_reviews()[-1].state == "APPROVED"


# -- operator guidance ----------------------------------------------------------------------------

def test_guidance_set_keep_and_clear_reach_exactly_the_next_paid_review(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, RC, RC, RC, RC, OK)))
    escalate(client)
    client.request("request:set", guidance=SetGuidance("Only judge the parser change."))
    progress_until(client, lambda c: len(c.provider.tasks) == 3)
    assert "Only judge the parser change." in client.provider.tasks[2].intent.prompt
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[2])
    progress_until(client, lambda c: bool(c.gate_causes()))
    assert "Only judge the parser change." in client.provider.tasks[3].intent.prompt
    client.request("request:keep")
    progress_until(client, lambda c: len(c.provider.tasks) == 5)
    assert "Only judge the parser change." in client.provider.tasks[4].intent.prompt
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[3])
    progress_until(client, lambda c: bool(c.gate_causes()))
    client.request("request:clear", guidance=ClearGuidance())
    progress_until(client, lambda c: len(c.provider.tasks) == 7)
    assert "Only judge the parser change." not in client.provider.tasks[6].intent.prompt
    assert client.state().episode.guidance == ""


def test_a_failed_guidance_commit_renews_nothing_and_the_next_review_reads_the_committed_guidance(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    before = client.state()
    with sqlite3.connect(client.review.control.path) as db:
        db.execute("CREATE TRIGGER injected BEFORE INSERT ON workflow_rearms "
                   "BEGIN SELECT RAISE(ABORT, 'injected re-arm commit failure'); END")
    with pytest.raises(Exception, match="injected re-arm commit failure"):
        client.request("request:lost", guidance=SetGuidance("Never committed."))
    client.tick(2)
    assert client.state() == before and len(client.provider.tasks) == 2
    with sqlite3.connect(client.review.control.path) as db:
        db.execute("DROP TRIGGER injected")
    client.request("request:ok", guidance=SetGuidance("Committed scope."))
    progress_until(client, lambda c: len(c.provider.tasks) == 3)
    prompt = client.provider.tasks[2].intent.prompt
    assert "Committed scope." in prompt and "Never committed." not in prompt


# -- dismissal outcomes and interventions during a pending re-arm ----------------------------------

@pytest.mark.parametrize("outcome", ["all", "some", "none", "unknown"])
def test_dismissal_outcomes_complete_or_stop_visibly(tmp_path, monkeypatch, outcome):
    deployment = create_review_deployment(tmp_path, ceiling=3)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    compose_review(deployment).activate_execution(monkeypatch)
    dismissals = [0]

    def fault(point):
        if point == "dismiss_requested":
            dismissals[0] += 1
            if outcome == "some" and dismissals[0] == 2:
                client.remote.soft_failures.add("dismiss_review")
        if point == "dismiss_applied" and outcome == "unknown" and dismissals[0] == 1:
            raise TimeoutError("reply lost after GitHub applied the dismissal")

    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC, RC, OK)))
    client.boot()
    for head in deployment.heads[:3]:
        client.remote.external_pr_change(PR, head_ref_oid=head)
        client.tick()
    client.tick()
    assert client.gate_causes() == ["ReviewRejectionCeiling"] and len(client.gates()[0].cause.counted) == 2
    assert NHR in client.labels()
    if outcome == "none":
        client.remote.deny_dismissals()
    client.request()
    client.tick(4)
    intent = client.state().intent
    if outcome in ("all", "unknown"):
        progress_until(client, lambda c: len(c.provider.tasks) == 4)
        assert rearmed(client) and TRIGGER in client.labels()
        assert [r.state for r in client.bot_reviews()][:2] == ["DISMISSED", "DISMISSED"]
        assert len([c for c in client.remote.calls if c[0] == "dismiss_review"]) == 2
    else:
        assert isinstance(intent, SupersededReviewRearm) and intent.reason is StopReason.REQUIRED_EFFECT_REJECTED
        # The trigger projection waited on the dismissals, so needs-human-review
        # is still the visible hold and the fixer never sees an open review.
        assert "RearmNeedsDecision" in client.gate_causes()
        assert NHR in client.labels() and TRIGGER not in client.labels()
        assert not [c for c in client.remote.calls if c[0] == "relabel_pr" and c[3] == TRIGGER]
        assert len(client.provider.tasks) == 3


@pytest.mark.parametrize("author, stops", [(BOT, True), ("octocat", False)])
def test_a_new_review_during_a_pending_rearm_stops_only_when_it_reaches_the_ceiling_again(deployment, author, stops):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    client.request()
    deployment.advance(1)
    client.remote.seed_review(PR, "mid-rearm", author=author, commit=deployment.heads[1],
                              submitted_at=deployment.clock.now().isoformat())
    client.tick(4)
    intent = client.state().intent
    if stops:
        assert isinstance(intent, SupersededReviewRearm) and intent.reason is StopReason.CEILING_REACHED_AGAIN
        assert "ReviewRejectionCeiling" in client.gate_causes() and NHR in client.labels()
        assert len(client.provider.tasks) == 2
    else:
        assert rearmed(client) and len(client.provider.tasks) == 3


@pytest.mark.parametrize("change", ["push", "close"])
def test_a_head_push_or_close_during_a_pending_rearm_supersedes_and_a_push_can_continue(deployment, change):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    accepted = client.request()
    assert isinstance(accepted.snapshot.intent, PendingReviewRearm)
    if change == "push":
        client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[2])
    else:
        client.remote.external_pr_change(PR, state="CLOSED")
    client.tick(3)
    intent = client.state().intent
    assert isinstance(intent, SupersededReviewRearm)
    assert intent.reason is (StopReason.DECISION_CHANGED if change == "push" else StopReason.TARGET_TERMINAL)
    assert len(client.provider.tasks) == 2
    if change == "close":
        return
    state = client.state()
    continued = client.workflow.continue_rearm(
        ContinueGrantedRearm("request:continue", state.revision, "request:1", expected_binding=state.binding), ACTOR)
    assert isinstance(continued, WorkflowCommitted), continued
    progress_until(client, lambda c: len(c.provider.tasks) == 3)
    assert rearmed(client) and client.bot_reviews()[-1].commit_id == deployment.heads[2]


# -- restarts around commit and remote boundaries ----------------------------------------------------

def _accept_then_exit(deployment) -> None:
    child_environment(deployment)
    client = compose_review(deployment)
    assert isinstance(client.request(), WorkflowCommitted)
    os._exit(84)


def _drain_until_death(deployment, point: str) -> None:
    child_environment(deployment)

    def fault(observed):
        if observed == point:
            os._exit(85)
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(OK)))
    for _ in range(6):
        client.tick()
    os._exit(86)


@pytest.mark.parametrize("point", [
    None, "dismiss_requested", "dismiss_applied", "relabel_pr_requested", "relabel_pr_applied",
    "review_requested", "review_submitted",
])
def test_a_restart_at_any_rearm_boundary_completes_with_one_write_each_and_one_fresh_review(deployment, point):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC)))
    escalate(client)
    run_until_exit(_accept_then_exit, deployment, code=84)
    if point is not None:
        run_until_exit(_drain_until_death, deployment, point, code=85)
    restarted = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)))
    restarted.boot()
    progress_until(restarted, lambda c: rearmed(c) and isinstance(
        c.state().episode.continuation, ReviewedBaseline))
    restarted.tick(3)
    calls = restarted.remote.calls
    dismissals = [c for c in calls if c[0] == "dismiss_review"]
    projections = [c for c in calls if c[0] == "relabel_pr" and c[3] == TRIGGER]
    fresh = [c for c in calls if c[0] == "submit_review"][2:]
    # A death before a request reaches GitHub leaves no remote write; the
    # restart proves absence and writes once. A death after it proves presence.
    assert len(dismissals) == 1 and len(projections) == 1 and len(fresh) == 1
    assert [r.state for r in restarted.bot_reviews()] == ["DISMISSED", "COMMENTED", "APPROVED"]
    assert TRIGGER in restarted.labels() and NHR not in restarted.labels()
    # A review bought before the death is never bought again after it.
    assert len(restarted.provider.tasks) == (0 if point in ("review_requested", "review_submitted") else 1)



def _settle_then_die(deployment) -> None:
    child_environment(deployment)
    from issue_agent.adapters import review_workflow_control

    review_workflow_control.SQLiteReviewWorkflowControl.settle_submission = lambda *_args, **_kwargs: os._exit(87)
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC)))
    client.boot()
    client.tick()
    os._exit(88)


def test_a_death_between_the_submission_receipt_and_its_baseline_settles_without_a_second_review(deployment):
    run_until_exit(_settle_then_die, deployment, code=87)
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC)))
    # Held, never re-bought, until reconciliation applies the landed verdict.
    assert client.remote.calls and len(client.submissions()) == 1
    client.boot()
    client.tick(3)
    assert client.provider.tasks == [] and len(client.submissions()) == 1
    baseline = client.state().episode.continuation
    assert isinstance(baseline, ReviewedBaseline) and baseline.head_sha == deployment.heads[0]


# -- unavailable evidence is visible, never an empty answer ---------------------------------------

def test_unavailable_review_pagination_holds_before_any_paid_leg_and_never_invents_a_cause(deployment):
    pages = {"reviews_page:1"}

    def fault(point):
        if point in pages:
            raise TimeoutError(point)
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC)))
    client.boot()
    client.tick()  # one review exists, so a second page is never needed yet
    assert len(client.provider.tasks) == 1
    for index in range(2):
        client.remote.seed_review(PR, f"human-{index}", author="octocat", state="COMMENTED", commit=deployment.heads[0],
                                  submitted_at=deployment.clock.now().isoformat())
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    client.tick(3)
    # The complete read needs page 1 now: no paid leg, no verdict, no gate.
    assert len(client.provider.tasks) == 1 and len(client.submissions()) == 1
    assert client.gate_causes() == [] and NHR not in client.labels()
    assert any("reviews unavailable" in line for line in client.log)
    pages.clear()
    # The next read waits for its backed-off schedule, then the review runs once.
    progress_until(client, lambda c: len(c.provider.tasks) == 2, limit=8)
    client.tick()
    assert len(client.provider.tasks) == 2 and client.gate_causes() == ["ReviewRejectionCeiling"]


def test_an_unavailable_read_during_a_pending_rearm_is_visible_and_never_completes_it(deployment):
    pages = set()

    def fault(point):
        if point in pages:
            raise TimeoutError(point)
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    client.request()
    pages.add("reviews_page:0")
    progress_until(client, lambda c: "EvidenceUnavailable" in c.gate_causes(), limit=20, minutes=30)
    # Bounded and visible: the dismissal's pre-write read backs off to a gate;
    # nothing is dismissed or relabelled blind, and no review is bought.
    assert isinstance(client.state().intent, PendingReviewRearm)
    assert not [c for c in client.remote.calls if c[0] in ("dismiss_review",)]
    assert NHR in client.labels() and len(client.provider.tasks) == 2
    pages.clear()
    progress_until(client, lambda c: len(c.provider.tasks) == 3, minutes=30)
    assert rearmed(client) and TRIGGER in client.labels()




# -- held uncertainty and the operator's retry grant ------------------------------------------------

def test_a_lost_submission_is_held_visibly_and_one_operator_grant_buys_exactly_one_write(deployment):
    lost = [True]

    def fault(point):
        if point == "review_requested" and lost[0]:
            raise TimeoutError("request lost")
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC)))
    client.boot()
    client.tick()
    progress_until(client, lambda c: "RetryAuthorizationRequired" in c.gate_causes(), limit=8, minutes=30)
    assert len(client.provider.tasks) == 1 and client.submissions() == []
    assert Store(client.cfg.review_state_dir).get_infra_deaths(-PR) == 0  # a hold, not a death
    (gate,) = client.gates()
    lost[0] = False
    state = client.state()
    from issue_agent.domain.workflow import AuthorizeAbsentRetry

    granted = client.workflow.authorize_retry(AuthorizeAbsentRetry(
        "grant:1", state.revision, gate.cause.command_id, gate.gate_id, gate.cause.absence_evidence_id,
        expected_binding=state.binding), ACTOR)
    assert isinstance(granted, WorkflowCommitted), granted
    client.tick(3)
    assert len(client.submissions()) == 1 and len(client.provider.tasks) == 1
    assert client.gate_causes() == []
    assert isinstance(client.state().episode.continuation, ReviewedBaseline)


# -- concurrency: exclusive reconciliation waits for an in-flight shared effect --------------------

def test_ceiling_reconciliation_waits_for_an_in_flight_submission_without_deadlock(deployment):
    """Composed: the operator-side reconcile (exclusive barriers) runs outside any shared effect."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    entered, release = threading.Event(), threading.Event()

    def hold(point):
        if point == "review_submitted" and not release.is_set():
            entered.set()
            assert release.wait(timeout=30), "effect was never released"

    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC)))
    escalate(client)
    (counted,) = client.gates()[0].cause.counted
    client.remote.external_review_change(counted.review_id, state="DISMISSED")
    daemon = compose_review(deployment, fault=hold, provider=ScriptedProvider(verdicts(OK)))
    operator = compose_review(deployment)
    with ThreadPoolExecutor(max_workers=2) as pool:
        effect = pool.submit(daemon.tick, 1, minutes=5)  # releases the ceiling, then submits the fresh review
        assert entered.wait(timeout=60)
        reconciled = pool.submit(operator.workflow.reconcile_ceiling, KEY)
        with pytest.raises(FutureTimeout):
            reconciled.result(timeout=2)  # the exclusive barriers wait for the shared effect
        release.set()
        effect.result(timeout=60)
        reconciled.result(timeout=60)
    assert len([c for c in daemon.remote.calls if c[0] == "submit_review"]) == 3
    daemon.tick()
    assert isinstance(daemon.state().episode.continuation, ReviewedBaseline)
    assert len(daemon.provider.tasks) == 1


# -- trusted external holds -------------------------------------------------------------------------

def test_a_trusted_hold_label_keeps_needs_human_review_through_a_rearm_and_buys_nothing(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    client.remote.external_pr_change(PR, labels=sorted({NHR, "ai-parked"}))
    assert isinstance(client.request(), WorkflowCommitted)
    client.tick(4)
    assert client.bot_reviews()[0].state == "DISMISSED"
    assert NHR in client.labels() and TRIGGER not in client.labels()
    assert len(client.provider.tasks) == 2


def test_a_held_ceiling_is_reconciled_on_a_backed_off_schedule_not_every_tick(deployment):
    reads: list[int] = []
    client = compose_review(deployment, fault=lambda point: reads.append(1) if point == "reviews_page:0" else None,
                            provider=ScriptedProvider(verdicts(RC, RC)))
    escalate(client)
    reads.clear()
    client.tick(120)  # two hours of one-minute ticks with nothing changing
    assert 3 <= len(reads) <= 12
    assert client.gate_causes() == ["ReviewRejectionCeiling"] and len(client.provider.tasks) == 2


# -- escalations: the durable consequence is decided when the receipt settles ----------------------

ESCALATING = {"verdict": "ESCALATE", "ceiling": RC}
PAID_ROUNDS = {"verdict": 1, "ceiling": 2}


def nhr_projections(client) -> list:
    return [c for c in client.remote.calls if c[0] == "relabel_pr" and c[3] == NHR]


def _before_escalating_round(deployment, cause: str) -> None:
    """The ceiling escalates on the second rejection; a verdict escalation is the first round."""
    if cause == "ceiling":
        client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC)))
        client.boot()
        client.tick()
        client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])


def _escalate_until_death(deployment, cause: str, point: str) -> None:
    child_environment(deployment)
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
    from issue_agent.domain.workflow import WorkflowCommitted as Committed

    settle = SQLiteReviewWorkflowControl.settle_submission
    if point == "receipt-before-settle":
        SQLiteReviewWorkflowControl.settle_submission = lambda *_args, **_kwargs: os._exit(89)
    elif point == "settle-before-projection":
        def settled_then_die(self, submission):
            result = settle(self, submission)
            if isinstance(result, Committed) and not result.idempotent:
                os._exit(89)
            return result
        SQLiteReviewWorkflowControl.settle_submission = settled_then_die

    def fault(observed):
        if observed == point:
            os._exit(89)
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(ESCALATING[cause])))
    client.boot()
    client.tick(4)
    os._exit(90)


@pytest.mark.parametrize("point", [
    "review_submitted", "receipt-before-settle", "settle-before-projection", "relabel_pr_applied",
])
@pytest.mark.parametrize("cause", ["verdict", "ceiling"])
def test_a_restart_around_an_escalation_buys_once_posts_once_and_holds_once(deployment, cause, point):
    """success-before-receipt, receipt-before-settle, settle-before-projection, projection-before-reply."""
    _before_escalating_round(deployment, cause)
    run_until_exit(_escalate_until_death, deployment, cause, point, code=89)
    restarted = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)))
    # The death came after the escalation reached GitHub.
    assert len(restarted.submissions()) == PAID_ROUNDS[cause]
    restarted.boot()
    progress_until(restarted, lambda c: NHR in c.labels(), minutes=5)
    restarted.tick(4, minutes=5)
    assert restarted.provider.tasks == [] and len(restarted.charges()) == PAID_ROUNDS[cause]
    assert len(restarted.submissions()) == PAID_ROUNDS[cause]
    assert NHR in restarted.labels() and TRIGGER not in restarted.labels()
    # One hold, projected by the settlement itself rather than repaired later.
    assert len(nhr_projections(restarted)) == 1
    if cause == "ceiling":
        assert restarted.gate_causes() == ["ReviewRejectionCeiling"]
    else:
        baseline = restarted.state().episode.continuation
        assert restarted.gate_causes() == [] and isinstance(baseline, ReviewedBaseline)
        assert baseline.head_sha == deployment.heads[0]


@pytest.mark.parametrize("cause", ["verdict", "ceiling"])
def test_a_lost_reply_to_an_escalation_settles_from_the_drain_without_a_second_review(deployment, cause):
    _before_escalating_round(deployment, cause)
    lost = [True]

    def fault(point):
        if point == "review_submitted" and lost[0]:
            lost[0] = False
            raise TimeoutError("reply lost after GitHub applied the review")
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(ESCALATING[cause], OK)))
    client.boot()
    client.tick(10, minutes=5)
    assert len(client.provider.tasks) == 1 and len(client.submissions()) == PAID_ROUNDS[cause]
    assert NHR in client.labels() and TRIGGER not in client.labels() and len(nhr_projections(client)) == 1


def test_an_escalation_whose_outcome_read_is_unavailable_holds_once_the_read_heals(deployment):
    state = {"submitted": 0, "broken": False, "reads": 0}

    def fault(point):
        if point == "review_submitted":
            state["submitted"] += 1
            state["broken"] = state["broken"] or state["submitted"] == 2
        if point == "reviews_page:0" and state["broken"]:
            state["reads"] += 1
            raise TimeoutError("reviews unavailable")
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    client.boot()
    client.tick()
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    client.tick()
    client.tick(60)
    # Verifying the escalation's outcome backs off (an hour of ticks, a
    # handful of reads); nothing is held, bought or relabelled on a guess.
    assert 3 <= state["reads"] <= 12 and client.gate_causes() in ([], ["EvidenceUnavailable"])
    assert len(client.provider.tasks) == 2 and TRIGGER in client.labels() and NHR not in client.labels()
    state["broken"] = False
    progress_until(client, lambda c: NHR in c.labels(), limit=20, minutes=10)
    assert client.gate_causes() == ["ReviewRejectionCeiling"] and TRIGGER not in client.labels()
    assert len(client.provider.tasks) == 2 and len(nhr_projections(client)) == 1


def test_an_escalation_whose_settlement_read_is_unavailable_settles_on_a_bounded_schedule(deployment, monkeypatch):
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl

    state = {"broken": False, "healed": False, "reads": 0}
    settle = SQLiteReviewWorkflowControl.settle_submission

    def receipt_durable_then_reads_fail(self, submission):
        # Break reads only once the escalation's receipt is durable: this is the settlement's own read.
        if not state["healed"] and len([c for c in deployment.github().calls if c[0] == "submit_review"]) == 2:
            state["broken"] = True
        return settle(self, submission)
    monkeypatch.setattr(SQLiteReviewWorkflowControl, "settle_submission", receipt_durable_then_reads_fail)

    def fault(point):
        if point == "reviews_page:0" and state["broken"]:
            state["reads"] += 1
            raise TimeoutError("reviews unavailable")
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    client.boot()
    client.tick()
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    client.tick()
    assert state["broken"] and len(client.submissions()) == 2
    client.tick(60)
    # An hour of ticks costs a handful of settlement reads, then a visible
    # review-history gate; nothing is held on a guess and nothing is re-bought.
    assert 3 <= state["reads"] <= 12 and client.gate_causes() in ([], ["EvidenceUnavailable"])
    assert len(client.provider.tasks) == 2 and NHR not in client.labels()
    state.update(broken=False, healed=True)
    progress_until(client, lambda c: NHR in c.labels(), limit=20, minutes=10)
    assert client.gate_causes() == ["ReviewRejectionCeiling"] and TRIGGER not in client.labels()
    assert len(client.provider.tasks) == 2 and len(nhr_projections(client)) == 1


def test_a_human_dismissal_racing_the_escalation_refuses_the_ceiling_and_buys_one_fresh_review(deployment):
    submitted = [0]

    def fault(point):
        if point == "review_submitted":
            submitted[0] += 1
            if submitted[0] == 2:  # the ceiling escalation just landed; a human dismisses the counted rejection
                with sqlite3.connect(deployment.github_path) as db:
                    db.execute("UPDATE reviews SET state='DISMISSED' WHERE state='CHANGES_REQUESTED'")
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    client.boot()
    client.tick()
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    progress_until(client, lambda c: len(c.provider.tasks) == 3, minutes=5)
    client.tick(10, minutes=10)
    # The label follows the settled outcome: no gate means no hold, and the
    # disproven escalation owes exactly one fresh review, not a re-buy loop.
    assert client.gate_causes() == [] and NHR not in client.labels() and TRIGGER in client.labels()
    assert nhr_projections(client) == [] and len(client.provider.tasks) == 3
    assert client.bot_reviews()[-1].state == "APPROVED"


@pytest.mark.parametrize("edited", [[], [TRIGGER]], ids=["hold-removed", "hold-swapped-for-trigger"])
def test_an_open_ceiling_restores_a_hold_a_human_removed_on_its_reconciliation_schedule(deployment, edited):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    client.remote.external_pr_change(PR, labels=edited)
    progress_until(client, lambda c: NHR in c.labels(), limit=20, minutes=10)
    client.tick(6, minutes=10)
    assert NHR in client.labels() and TRIGGER not in client.labels()
    assert client.gate_causes() == ["ReviewRejectionCeiling"] and len(client.provider.tasks) == 2
    assert len(nhr_projections(client)) == 2


# -- evidence reads a dispatch depends on are bounded before any read ------------------------------

def test_a_persistently_partial_review_read_backs_off_to_a_visible_gate_and_recovers(deployment):
    broken = [False]
    reads = {"read": 0, "reviews_page:0": 0}

    def fault(point):
        if point in reads:
            reads[point] += 1
        if point == "reviews_page:0" and broken[0]:
            raise TimeoutError("review page unavailable")
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, OK)))
    client.boot()
    client.tick()
    broken[0] = True
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    ticks, idle = [], []
    while "EvidenceUnavailable" not in client.gate_causes() and len(ticks) < 120:
        before = dict(reads)
        client.tick()
        pages, prs = reads["reviews_page:0"] - before["reviews_page:0"], reads["read"] - before["read"]
        ticks.append(pages)
        if pages == 0:
            idle.append(prs)
    (gate,) = client.gates()
    assert isinstance(gate.cause, EvidenceUnavailable) and gate.gate_id.startswith("review-history:")
    # Backed off from the first failure: far fewer reads than ticks, and none paid.
    assert sum(ticks) <= 12 < len(ticks) and len(client.provider.tasks) == 1
    # While a read is not due the plan holds the PR before dispatch: the only
    # pull request read of such a tick is the world observation itself.
    assert idle and set(idle) == {1}
    broken[0] = False
    progress_until(client, lambda c: len(c.provider.tasks) == 2, limit=6, minutes=31)
    assert client.gate_causes() == [] and client.bot_reviews()[-1].state == "APPROVED"


# -- bound rounds count on their own terms --------------------------------------------------------

def test_a_bound_re_review_diffs_from_its_baseline_never_the_legacy_head_cache(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, OK)))
    client.boot()
    client.tick()
    (Path(client.cfg.review_state_dir) / f"pr-{PR}.last-sha").write_text(deployment.heads[3])
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    progress_until(client, lambda c: len(c.provider.tasks) == 2)
    prompt = client.provider.tasks[1].intent.prompt
    assert f"{deployment.heads[0]} → {deployment.heads[1]}" in prompt and deployment.heads[3] not in prompt


DECLARATION = "chore: config only\n\nGate-Exemption: required-test *"


@pytest.mark.parametrize("declared", [False, True], ids=["no-trailer", "legacy-trailer"])
def test_a_current_head_approval_resets_the_bound_count_without_grants(
        bare, monkeypatch, declared):
    bare.activate()
    compose_review(bare).activate_execution(monkeypatch)
    heads = bare.heads
    remote = bare.github()
    remote.external_pr_change(PR, head_ref_oid=heads[1])
    remote.seed_review(PR, "legacy-rejection", author=BOT, state="CHANGES_REQUESTED", commit=heads[0],
                       submitted_at="2026-09-25T10:00:00+00:00")
    remote.seed_review(PR, "legacy-approval", author=BOT, state="APPROVED", commit=heads[1],
                       submitted_at="2026-09-25T10:30:00+00:00", body="ok")
    if declared:
        remote.set_commit_messages(PR, DECLARATION)
    client = compose_review(bare, provider=ScriptedProvider(verdicts(RC)))
    client.boot()
    client.tick(3)
    assert len(client.provider.tasks) == 1
    assert client.gate_causes() == [] and TRIGGER in client.labels()
    assert client.bot_reviews()[-1].state == "CHANGES_REQUESTED"


# -- operator request replay, human reviews and release ordering ----------------------------------

def test_a_request_replayed_after_a_lost_reply_is_one_intent_with_no_duplicate_commands(deployment):
    from issue_agent.domain.workflow import KeepGuidance, RequestRearm

    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    state = client.state()
    gate_id = client.gates()[0].gate_id
    command = RequestRearm("request:replayed", state.revision, gate_id, KeepGuidance(), expected_binding=state.binding)

    def rows() -> tuple:
        with sqlite3.connect(client.review.control.path) as db:
            return (db.execute("SELECT COUNT(*) FROM workflow_rearms").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM lifecycle_commands").fetchone()[0])
    first = client.workflow.request_rearm(command, ACTOR)
    assert isinstance(first, WorkflowCommitted) and not first.idempotent
    committed = rows()
    # The reply was lost after the commit: the identical request arrives again, in a fresh process.
    again = compose_review(deployment).workflow.request_rearm(command, ACTOR)
    assert isinstance(again, WorkflowCommitted) and again.idempotent and again.snapshot == first.snapshot
    assert rows() == committed
    progress_until(client, lambda c: len(c.provider.tasks) == 3)
    client.tick(3)
    late = compose_review(deployment).workflow.request_rearm(command, ACTOR)
    assert isinstance(late, WorkflowCommitted) and late.idempotent and rearmed(client)
    client.tick(3)
    assert len([c for c in client.remote.calls if c[0] == "dismiss_review"]) == 1
    assert len(client.provider.tasks) == 3 and rearmed(client)


def test_a_human_review_during_a_pending_rearm_is_never_dismissed(deployment):
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    client.request()
    deployment.advance(1)
    client.remote.seed_review(PR, "human-mid-rearm", author="octocat", commit=deployment.heads[1],
                              submitted_at=deployment.clock.now().isoformat())
    progress_until(client, rearmed)
    assert client.remote.review_state("human-mid-rearm") == "CHANGES_REQUESTED"
    dismissed = [c[2] for c in client.remote.calls if c[0] == "dismiss_review"]
    assert "human-mid-rearm" not in dismissed and len(dismissed) == 1


def test_a_human_dismissal_release_buys_only_after_its_projection_is_verified(deployment):
    blocked = [True]

    def fault(point):
        if point == "relabel_pr_requested" and blocked[0]:
            raise TimeoutError("relabel request lost")
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    (counted,) = client.gates()[0].cause.counted
    client.remote.external_review_change(counted.review_id, state="DISMISSED")
    delayed = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(OK)))
    delayed.tick(3, minutes=5)  # below the effect retry ceiling, which would gate it for an operator
    # The release projection has not landed: the fresh review waits for it.
    assert delayed.provider.tasks == [] and TRIGGER not in delayed.labels()
    blocked[0] = False
    progress_until(delayed, lambda c: len(c.provider.tasks) == 1, limit=12, minutes=5)
    calls = delayed.remote.calls
    release = max(i for i, c in enumerate(calls) if c[0] == "relabel_pr" and c[3] == TRIGGER)
    fresh = [i for i, c in enumerate(calls) if c[0] == "submit_review"][2]
    assert release < fresh and delayed.bot_reviews()[-1].state == "APPROVED"


# -- the fix role reads PR guidance from the review workflow, or defers ---------------------------

def test_the_composed_fix_role_defers_a_repair_on_unreadable_review_guidance_then_uses_it(deployment):
    from issue_agent.adapters.worktree import Worktrees
    from issue_agent.app import roles
    from issue_agent.domain.outcome import AgentResult

    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC, RC)))
    escalate(client)
    client.request("request:scope", guidance=SetGuidance("Only judge the parser change."))
    progress_until(client, lambda c: len(c.provider.tasks) == 3)
    assert client.bot_reviews()[-1].state == "CHANGES_REQUESTED"  # the fixer now owes an address-review
    from issue_agent.adapters.control_store import SQLiteControlStore
    from issue_agent.adapters.workflow_rows import load_review_workflow_row
    from issue_agent.domain.workflow_identity import WorkflowFamily
    from tests.adapters.test_continuation_admission import active, install_secondary
    from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery
    control = SQLiteControlStore(client.review.control.path)
    control.initialize()
    install_workflow(control, with_issue_recovery(resolve_snapshot(1)))
    with control.transaction() as db:
        primary = load_review_workflow_row(db, KEY)
    assert primary is not None
    component = active(KEY, WorkflowFamily.PR_REPAIR)
    install_secondary(control, replace(primary, pr_repair=component), component)
    with sqlite3.connect(client.review.control.path) as db:
        committed = db.execute("SELECT episode_json FROM workflows WHERE target_number=?", (PR,)).fetchone()[0]
        before_money = db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0]
        before_issue = db.execute("SELECT * FROM workflows WHERE target_kind='issue' AND target_number=1").fetchone()
        db.execute("UPDATE workflows SET episode_json='corrupt' WHERE target_number=?", (PR,))
    cfg = deployment.config()
    client.remote.link_pr(1, PR)
    provider = ScriptedProvider([AgentResult(is_error=True, cost_usd=0.1, session_id="fix-1")])
    log: list[str] = []
    fix = roles.compose_fix_daemon(cfg, client.remote, Store(cfg.state_dir), Worktrees(str(deployment.target)),
                                   provider, log=log.append)

    def fix_tick() -> None:
        deployment.advance(1)
        fix.daemon.tick()
        fix.daemon.wait_for_dispatches()
    fix_tick()
    # Deferred before any side effect: no paid leg, no worktree, no infra death.
    assert provider.tasks == [] and any("ADDRESS_REVIEW #7 held: review guidance is unavailable" in line
                                        for line in log)
    assert not (deployment.target / ".claude" / "worktrees" / "issue-1").exists()
    assert Store(cfg.state_dir).get_infra_deaths(1) == 0
    with sqlite3.connect(client.review.control.path) as db:
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == before_money
        assert db.execute("SELECT * FROM workflows WHERE target_kind='issue' AND target_number=1").fetchone() == before_issue
        db.execute("UPDATE workflows SET episode_json=? WHERE target_number=?", (committed, PR))
    fix_tick()
    assert len(provider.tasks) == 1 and "Only judge the parser change." in provider.tasks[0].intent.prompt, log


# -- increment 6 re-review: escalation holds are fenced by hold facts, re-planned, signalled once ----

def escalation_events(client) -> int:
    journal = Path(client.cfg.review_state_dir) / "journal.jsonl"
    if not journal.exists():
        return 0
    return sum(json.loads(line).get("event") == "escalation" for line in journal.read_text().splitlines())


def alert_lines(path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("label, replanned", [
    ("recurrence", False), ("maybe-resolved", False), ("no-ai-review", True), ("ai-parked", True),
])
def test_an_escalation_hold_is_refused_only_by_a_hold_fact_and_then_re_planned(deployment, tmp_path, label, replanned):
    """probe test_probe_escalation_guard: a label edit between settlement and drain."""
    alerts = tmp_path / "alerts.jsonl"
    client = compose_review(deployment, provider=ScriptedProvider(verdicts("ESCALATE", OK)), alerts=alerts)
    client.boot()
    client.tick()  # the ESCALATE verdict lands and settles; its projection waits for the next drain
    client.remote.external_pr_change(PR, labels=sorted({TRIGGER, label}))
    progress_until(client, lambda c: NHR in c.labels(), minutes=5)
    client.tick(6, minutes=10)
    # The hold is visible, the human's label is kept, and the covered head is never re-bought.
    assert NHR in client.labels() and label in client.labels() and TRIGGER not in client.labels()
    assert len(client.provider.tasks) == 1 and client.gate_causes() == []
    assert len(nhr_projections(client)) == 1
    assert escalation_events(client) == 1 and len(alert_lines(alerts)) == 1


@pytest.mark.parametrize("change", ["close", "push"])
def test_a_refused_verdict_escalation_is_moot_after_a_close_or_a_push(deployment, tmp_path, change):
    alerts = tmp_path / "alerts.jsonl"
    client = compose_review(deployment, provider=ScriptedProvider(verdicts("ESCALATE", OK)), alerts=alerts)
    client.boot()
    client.tick()
    if change == "close":
        client.remote.external_pr_change(PR, state="CLOSED")
    else:
        client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    client.tick(8, minutes=10)
    # No hold is written to a closed PR or over a new head; a new head is owed exactly one review.
    assert nhr_projections(client) == [] and NHR not in client.labels()
    assert escalation_events(client) == 0
    assert len(client.provider.tasks) == (2 if change == "push" else 1) and client.gate_causes() == []


@pytest.mark.parametrize("remedy", ["labels", "push"])
def test_a_verdict_escalation_whose_hold_is_contested_becomes_a_visible_gate_and_one_signal(
    deployment, tmp_path, remedy,
):
    """An automation flaps a hold label on every read: each re-plan is refused, then the bound surfaces it."""
    alerts = tmp_path / "alerts.jsonl"
    war = [True]

    def fault(point):
        if point == "reviews_page:0" and war[0] and client.submissions():
            labels = {label.value for label in client.remote.get_pr(PR).labels}
            client.remote.external_pr_change(PR, labels=sorted(labels ^ {"no-ai-review"}))
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts("ESCALATE", OK)),
                            alerts=alerts)
    client.boot()
    client.tick()
    progress_until(client, lambda c: "ReviewProjectionRefused" in c.gate_causes(), limit=40, minutes=10)
    refused = [c for c in client.remote.calls if c[0] == "relabel_pr"]
    assert refused == []
    assert escalation_events(client) == 1 and len(alert_lines(alerts)) == 1
    assert len(client.provider.tasks) == 1
    war[0] = False
    if remedy == "labels":
        # The planned labels by hand; the next harness read verifies them.
        client.remote.external_pr_change(PR, labels=[NHR])
    else:
        # A new head retires the escalation: the refused hold is moot and the head is owed one review.
        client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1],
                                         labels=sorted(client.labels() - {"no-ai-review"}))
    progress_until(client, lambda c: c.gate_causes() == [], limit=12, minutes=10)
    client.tick(3, minutes=10)
    assert escalation_events(client) == 1 and len(alert_lines(alerts)) == 1
    assert len(client.provider.tasks) == (1 if remedy == "labels" else 2) and client.gate_causes() == []


def test_a_refused_ceiling_projection_is_restored_and_signalled_once(deployment, tmp_path):
    alerts = tmp_path / "alerts.jsonl"
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(RC, RC)), alerts=alerts)
    client.boot()
    client.tick()
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    client.tick()  # the ceiling escalation settles; its projection waits for the next drain
    client.remote.external_pr_change(PR, labels=sorted({TRIGGER, "no-ai-review"}))
    progress_until(client, lambda c: NHR in c.labels(), limit=12, minutes=10)
    client.tick(4, minutes=10)
    assert client.gate_causes() == ["ReviewRejectionCeiling"] and "no-ai-review" in client.labels()
    assert escalation_events(client) == 1 and len(alert_lines(alerts)) == 1 and len(client.provider.tasks) == 2


def refused_escalations(client) -> int:
    with sqlite3.connect(client.review.control.path) as db:
        return db.execute("SELECT COUNT(*) FROM lifecycle_commands WHERE state='permanent_failure' "
                          "AND idempotency_key LIKE 'workflow:escalation:%'").fetchone()[0]


@pytest.mark.parametrize("cause", ["verdict", "ceiling"])
def test_an_escalation_whose_hold_a_human_applied_after_its_refusal_is_signalled_once(deployment, tmp_path, cause):
    """probe test_probe_pass3_verified: the hold is verified on GitHub, but no receipt of ours applied it."""
    alerts = tmp_path / "alerts.jsonl"
    human = [True]

    def fault(point):
        # A person applies the hold after the projection is refused, before the harness reads again.
        if point == "observation_read" and human[0] and refused_escalations(client):
            human[0] = False
            client.remote.external_pr_change(PR, labels=sorted({NHR, "no-ai-review"}))
    decisions = ("ESCALATE", OK) if cause == "verdict" else (RC, RC)
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(*decisions)), alerts=alerts)
    client.boot()
    client.tick()
    if cause == "ceiling":
        client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
        client.tick()
    client.remote.external_pr_change(PR, labels=sorted({TRIGGER, "no-ai-review"}))  # refuses the projection
    progress_until(client, lambda c: not human[0], limit=12, minutes=5)
    client.tick(8, minutes=10)
    assert client.labels() == {NHR, "no-ai-review"} and nhr_projections(client) == []
    assert escalation_events(client) == 1
    assert [json.loads(line) for line in alert_lines(alerts)] == [{"event": "escalation", "number": PR,
                                                                   "target": "pr"}]
    assert client.gate_causes() == ([] if cause == "verdict" else ["ReviewRejectionCeiling"])
    assert len(client.provider.tasks) == (1 if cause == "verdict" else 2)


def test_a_ceiling_hold_whose_every_restoration_is_refused_is_contested_and_signalled_once(deployment, tmp_path):
    """An automation flaps a hold label on every read, so no restoration ever lands: the contest still pages."""
    alerts = tmp_path / "alerts.jsonl"
    war = [False]

    def fault(point):
        if point == "reviews_page:0" and war[0]:
            labels = {label.value for label in client.remote.get_pr(PR).labels}
            client.remote.external_pr_change(PR, labels=sorted(labels ^ {"no-ai-review"}))
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC)), alerts=alerts)
    client.boot()
    client.tick()
    client.remote.external_pr_change(PR, head_ref_oid=deployment.heads[1])
    client.tick()  # the ceiling escalation settles; its projection waits for the next drain
    client.remote.external_pr_change(PR, labels=sorted({TRIGGER, "no-ai-review"}))
    progress_until(client, lambda c: refused_escalations(c) == 1, limit=4, minutes=1)
    war[0] = True
    from issue_agent.domain.review_workflow_policy import MAX_HOLD_RESTORES

    progress_until(client, lambda c: escalation_events(c) == 1, limit=40, minutes=10)
    client.tick(4, minutes=10)
    assert NHR not in client.labels() and nhr_projections(client) == []
    assert escalation_events(client) == 1 and len(alert_lines(alerts)) == 1
    assert client.gate_causes() == ["ReviewRejectionCeiling"] and len(client.provider.tasks) == 2


def _drain_until_projection_applied(deployment, alerts) -> None:
    child_environment(deployment)

    def fault(point):
        if point == "relabel_pr_applied":
            os._exit(93)
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts("ESCALATE")), alerts=alerts)
    client.boot()
    client.tick(3)
    os._exit(94)


@pytest.mark.parametrize("path", ["direct", "lost-reply", "restart"])
def test_an_escalation_signals_once_on_whichever_path_settles_its_hold(deployment, tmp_path, path):
    """probe test_probe_escalation_signal: the signal follows the durable receipt, not one code path."""
    alerts = tmp_path / "alerts.jsonl"
    if path == "restart":
        run_until_exit(_drain_until_projection_applied, deployment, alerts, code=93)
        client = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)), alerts=alerts)
        client.boot()
    else:
        lost = [path == "lost-reply"]

        def fault(point):
            if point == "relabel_pr_applied" and lost[0]:
                lost[0] = False
                raise TimeoutError("reply lost after GitHub applied the relabel")
        client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts("ESCALATE", OK)),
                                alerts=alerts)
        client.boot()
    progress_until(client, lambda c: escalation_events(c) == 1, limit=12, minutes=5)
    client.tick(6, minutes=10)
    assert NHR in client.labels() and len(nhr_projections(client)) == 1
    assert escalation_events(client) == 1
    assert [json.loads(line) for line in alert_lines(alerts)] == [{"event": "escalation", "number": PR,
                                                                   "target": "pr"}]


def test_a_contested_hold_is_restored_a_bounded_number_of_times_then_surfaced_once(deployment):
    """probe test_probe_restore_war: an automation swaps the hold back for the trigger every minute."""
    from issue_agent.domain.review_workflow_policy import MAX_HOLD_RESTORES

    minute: list[int | None] = [None]
    read_at: list[int] = []

    def fault(point):
        if point == "reviews_page:0" and minute[0] is not None:
            read_at.append(minute[0])
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC, RC, OK)))
    escalate(client)
    before, signals = len(nhr_projections(client)), escalation_events(client)
    for minute[0] in range(120):
        client.remote.external_pr_change(PR, labels=[TRIGGER])
        client.tick(minutes=1)
    minute[0] = None
    # Bounded: the restorations stop at the bound; the harness's own restore
    # never resets the read backoff, so every read waits longer than the last
    # until the ceiling cadence; no restore re-pages; the contest is journalled
    # once; the gate still holds every review.
    gaps = [b - a for a, b in zip(read_at, read_at[1:])]
    assert len(nhr_projections(client)) - before == MAX_HOLD_RESTORES and len(read_at) <= 12
    assert all(later > earlier or later == max(gaps) for earlier, later in zip(gaps, gaps[1:]))
    assert signals == escalation_events(client) == 1
    assert client.gate_causes() == ["ReviewRejectionCeiling"] and len(client.provider.tasks) == 2
    # The contested state belongs to that gate: a human dismissal releases it and clears the contest.
    (counted,) = client.gates()[0].cause.counted
    client.remote.external_review_change(counted.review_id, state="DISMISSED")
    progress_until(client, lambda c: len(c.provider.tasks) == 3, limit=20, minutes=10)
    assert client.gate_causes() == []


def test_a_never_observed_pull_request_whose_first_read_fails_backs_off(bare, monkeypatch):
    bare.activate()
    deployment = bare
    broken, reads = [True], [0]

    def fault(point):
        if point == "observation_read" and broken[0]:
            reads[0] += 1
            raise TimeoutError("pull request read unavailable")
    client = compose_review(deployment, fault=fault, provider=ScriptedProvider(verdicts(RC)))
    client.boot()
    client.tick(60)
    # An hour of ticks costs a handful of first reads, not sixty; nothing is bought.
    assert 3 <= reads[0] <= 12 and client.provider.tasks == []
    assert any("first observation backing off" in line for line in client.log)
    broken[0] = False
    client.activate_execution(monkeypatch)
    progress_until(client, lambda c: len(c.provider.tasks) == 1, limit=6, minutes=31)
    assert isinstance(client.state().episode.continuation, ReviewedBaseline)


def _die_at_review_execution_boundary(deployment, boundary):
    from issue_agent.adapters.control_store import SQLiteControlStore
    from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl

    child_environment(deployment)
    if boundary == "planning":
        original_preflight = SQLiteReviewExecutionControl.preflight

        def stop_after_plan(self, selection):
            original_preflight(self, selection)
            os._exit(96)

        SQLiteReviewExecutionControl.preflight = stop_after_plan
    elif boundary == "reservation":
        original_reserve = SQLiteControlStore.reserve

        def stop_after_reservation(self, request):
            result = original_reserve(self, request)
            if request.review_child is not None and result.admitted:
                os._exit(96)
            return result

        SQLiteControlStore.reserve = stop_after_reservation
    elif boundary == "return":
        original_return = SQLiteControlStore.settle_review_return

        def stop_after_return(self, request, evidence):
            original_return(self, request, evidence)
            os._exit(96)

        SQLiteControlStore.settle_review_return = stop_after_return
    provider = ScriptedProvider(verdicts(OK), during=lambda _: os._exit(96) if boundary == "launch" else None)
    client = compose_review(deployment, provider=provider)
    client.tick()
    os._exit(97)


@pytest.mark.parametrize("boundary", ["planning", "reservation", "launch", "return"])
def test_process_loss_before_submission_recovers_without_duplicate_money_or_debit(deployment, boundary):
    run_until_exit(_die_at_review_execution_boundary, deployment, boundary, code=96)
    client = compose_review(deployment, provider=ScriptedProvider(verdicts(OK)))
    before = client.state().execution
    with client.review.control.transaction() as db:
        interrupted_runs = tuple(row["run_id"] for row in db.execute("SELECT run_id FROM runs"))
    if boundary != "launch":
        client.remote.external_pr_change(PR, labels=[Label.AI_PARKED.value])
    client.boot()
    client.tick(10)
    if boundary == "launch":
        assert len(client.provider.tasks) == len(client.submissions()) == 1
        assert len(client.bot_reviews()) == 1 and client.bot_reviews()[0].state == "APPROVED"
    else:
        assert client.provider.tasks == [] and client.submissions() == []
    tasks, submissions = tuple(client.provider.tasks), client.submissions()
    money = client.charges()
    state = client.state()
    with client.review.control.transaction() as db:
        rounds = tuple(tuple(row) for row in db.execute(
            "SELECT round_id,planning_json,closure_json,outcome_json FROM review_execution_rounds ORDER BY sequence"))
        reservations = tuple(tuple(row) for row in db.execute("SELECT run_id,state FROM budget_reservations"))
        commands = tuple(tuple(row) for row in db.execute("SELECT * FROM lifecycle_commands"))
        charges = tuple(tuple(row) for row in db.execute("SELECT run_id,amount_micros,basis FROM charges ORDER BY run_id"))
        runs = tuple(tuple(row) for row in db.execute("SELECT run_id,state,outcome FROM runs ORDER BY run_id"))
    client.boot()
    client.tick(3)
    assert client.charges() == money and client.state() == state
    assert tuple(client.provider.tasks) == tasks and client.submissions() == submissions
    with client.review.control.transaction() as db:
        assert tuple(tuple(row) for row in db.execute(
            "SELECT round_id,planning_json,closure_json,outcome_json FROM review_execution_rounds ORDER BY sequence")) == rounds
        assert tuple(tuple(row) for row in db.execute("SELECT run_id,state FROM budget_reservations")) == reservations
        assert tuple(tuple(row) for row in db.execute("SELECT * FROM lifecycle_commands")) == commands
        assert tuple(tuple(row) for row in db.execute("SELECT run_id,amount_micros,basis FROM charges ORDER BY run_id")) == charges
        assert tuple(tuple(row) for row in db.execute("SELECT run_id,state,outcome FROM runs ORDER BY run_id")) == runs
        assert db.execute("SELECT count(*) FROM dispatch_claims").fetchone()[0] == 0
    if boundary == "return":
        assert len(money) == 1 and money[0][1] == 100_000
        assert state.execution.state.failures == 1
        assert rounds[0][3] is not None
    elif boundary == "launch":
        (interrupted,) = interrupted_runs
        completed = tasks[0].env["ISSUE_AGENT_RUN_ID"]
        assert set(charges) == {(interrupted, 0, "estimate"), (completed, 100_000, "provider_reported")}
        assert set(reservations) == {(interrupted, "settled"), (completed, "settled")}
        assert set(runs) == {(interrupted, "abandoned", "dead_lease"), (completed, "completed", "settled")}
        assert state.execution.binding == before.binding
        assert state.execution.state.failures == state.execution.state.pin_deferrals == 0
        assert state.execution.state.displayed_round == 1
        assert isinstance(state.episode.continuation, ReviewedBaseline)
        assert state.episode.continuation.head_sha == deployment.heads[0]
        assert client.gates() == [] and NHR not in client.labels()
        assert len(rounds) == 2
        assert isinstance(decode_workflow(rounds[0][1], ReviewParentAbandoned), ReviewParentAbandoned)
        assert rounds[0][2:] == (None, None)
        facts = decode_workflow(rounds[1][2], ReviewRoundFacts)
        outcome = decode_workflow(rounds[1][3], ReviewRoundOutcome)
        assert isinstance(outcome.disposition, PaidReviewProgress)
        assert tuple(child.run_id for child in outcome.disposition.launched) == (completed,)
        assert len(facts.children) == 1 and isinstance(facts.children[0], ReviewChildReturned)
        assert facts.children[0].launched.run_id == completed
        assert len(facts.submissions) == 1 and isinstance(facts.submissions[0].result, ReviewSubmissionConfirmed)
        assert facts.submissions[0].result.receipt_id == outcome.disposition.submission_receipt
    else:
        assert money == [] and state.execution.state.failures == 0
        assert rounds[0][3] is not None


def test_terminal_restart_and_same_head_reopen_preserve_execution_allowance(deployment, monkeypatch):
    from issue_agent.adapters import control_store
    from issue_agent.adapters.control_store import IncompatibleSchema, SQLiteControlStore

    failure = AgentResult(is_error=True, exit_reason=ExitReason.AGENT_ERROR, cost_usd=0.1)
    client = compose_review(deployment, provider=ScriptedProvider([failure]))
    client.boot()
    client.tick()
    before = client.state().execution
    assert before.state.failures == 1
    client.remote.external_pr_change(PR, state="CLOSED")
    restarted = compose_review(deployment, provider=ScriptedProvider([failure]))
    restarted.boot()
    restarted.tick(3)
    assert restarted.provider.tasks == [] and len(restarted.charges()) == 1
    assert restarted.state().execution == before
    legacy = Store(restarted.cfg.review_state_dir)
    paths = {Path(legacy.pr_path(PR, "round")): b"0",
             Path(legacy.pr_path(PR, "session")): b"claude:obsolete-reopened-session"}
    for path, value in paths.items():
        path.write_bytes(value)
    with restarted.review.control.transaction() as db:
        sql_before = tuple(db.iterdump())
    with monkeypatch.context() as old_binary:
        old_binary.setattr(control_store, "SCHEMA_VERSION", 33)
        with pytest.raises(IncompatibleSchema, match="newer than 33"):
            SQLiteControlStore(deployment.control_path).initialize()
    with restarted.review.control.transaction() as db:
        assert tuple(db.iterdump()) == sql_before
    assert {path: path.read_bytes() for path in paths} == paths
    restarted.remote.seed(IssueView(2, title="Relinked", state="open"))
    restarted.remote.external_pr_change(PR, state="OPEN", closing_issue=2)
    restarted.tick()
    after = restarted.state().execution
    assert len(restarted.provider.tasks) == 1 and len(restarted.charges()) == 2, restarted.log
    assert after.state.binding.execution_episode == before.state.binding.execution_episode
    assert after.last_sequence == before.last_sequence + 1
    assert after.state.failures == 2 and restarted.submissions() == []
    assert {path: path.read_bytes() for path in paths} == paths


def test_multi_review_out_of_order_child_returns_settle_one_round_and_one_charge_each(deployment):
    from issue_agent.app.planners.tasks import REVIEW_DIMENSIONS

    class ReversedReturns(ScriptedProvider):
        def __init__(self):
            super().__init__()
            self.arrived = []
            self.completed = []

        async def run(self, task):
            self.tasks.append(task)
            assert self.launch is not None
            self.launch(task)
            self.during(task)
            if len(self.tasks) == 1:
                return AgentResult(False, cost_usd=0.1, exit_reason=ExitReason.OK,
                                   structured={"reviewable": True})
            index = len(self.arrived)
            ready = asyncio.Event()
            self.arrived.append(ready)
            if len(self.arrived) == len(REVIEW_DIMENSIONS):
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=10)
            self.completed.append(index)
            if index:
                self.arrived[index - 1].set()
            return AgentResult(False, cost_usd=0.1, exit_reason=ExitReason.OK,
                               structured={"findings": []})

    provider = ReversedReturns()
    client = compose_review(deployment, provider=provider, mode=ReviewMode.MULTI)
    client.boot()
    client.tick()
    count = len(REVIEW_DIMENSIONS)
    assert provider.completed == list(reversed(range(count))), client.log
    assert len(provider.tasks) == len(client.charges()) == count + 1
    assert sum(amount for _, amount in client.charges()) == (count + 1) * 100_000
    assert len(client.submissions()) == 1 and client.bot_reviews()[0].state == "APPROVED"
    state = client.state()
    assert state.execution.state.failures == 0 and state.execution.state.displayed_round == 1
    with client.review.control.transaction() as db:
        assert db.execute("SELECT count(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 1
    restarted = compose_review(deployment)
    restarted.boot()
    restarted.tick(3)
    assert restarted.provider.tasks == [] and restarted.charges() == client.charges()
    assert len(restarted.submissions()) == 1 and restarted.state().execution == state.execution






















def test_persisted_grant_downgrade_is_not_republished_after_restart(tmp_path, monkeypatch):
    deployment = create_review_deployment(tmp_path, ceiling=10)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    compose_review(deployment).activate_execution(monkeypatch)
    body = ('The reviewer returned APPROVE but did not grant every gate exemption required by the current patch. '
            'This is therefore REQUEST_CHANGES, not a mergeable approval. Either revise the patch so these '
            'exemptions are no longer required, or explicitly grant each pair on the next review.\n\n'
            'Ungranted requests:\n- `tests-immutable` for `tests/test_old.py`\n\nReviewer rationale:\nlgtm')
    prior = replace(verdicts('REQUEST_CHANGES')[0], structured={'decision': 'REQUEST_CHANGES', 'body': body})
    first = compose_review(deployment, provider=ScriptedProvider([prior]))
    first.boot()
    first.tick()
    assert first.bot_reviews()[-1].state == 'CHANGES_REQUESTED'
    assert body in first.bot_reviews()[-1].body
    assert len(first.provider.tasks) == 1
    remote = deployment.github()
    rebased = deployment.heads[2]
    remote.set_diff(rebased, diff_text('change0', offset=9))
    remote.external_pr_change(PR, head_ref_oid=rebased)
    restarted = compose_review(deployment, provider=ScriptedProvider(verdicts('APPROVE')))
    restarted.boot()
    restarted.tick(2)
    copied = [r for r in restarted.bot_reviews() if r.commit_id == rebased]
    assert copied and copied[-1].state == "APPROVED"
    assert body not in copied[-1].body
    assert len(restarted.provider.tasks) == 1
