"""The composed review role (slice 5c) beside the fix role: production wiring,
one real SQLite authority, one fake GitHub.

Each test ticks `compose_review_daemon` (and `compose_fix_daemon` where the
split of work matters) and asserts on what GitHub ends up holding (reviews on
which commit, labels, comments, merges) and on how many paid legs ran.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from issue_agent.adapters.github import GhUnavailable
from issue_agent.app.config import from_env
from issue_agent.app.fix import compose_fix_daemon
from issue_agent.app.review import compose_review_daemon
from issue_agent.domain.actions import AgentKind, MergePullRequest, RunAgent
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.effort import Effort
from issue_agent.domain.routing import Tier
from issue_agent.domain.state import StopReason, stop_tag
from issue_agent.domain.world import Review
from tests.app.test_fix_role import (
    FIX_BOT,
    REVIEW_BOT,
    Agent,
    FakeGitHub,
    FakeWorktrees,
    _cap_hit,
    _now,
    died,
    dispatched,
    open_pr,
    pushed,
    ready,
    tick,
)
from tests.app.test_fix_role import later  # noqa: F401 — the shared backoff-clock fixture


@dataclass
class ReviewGitHub(FakeGitHub):
    """FakeGitHub plus the review bot's reads and writes. A submitted review
    lands on the commit it names, the way GitHub records `commit_id`."""

    submitted: list[tuple[int, str, str, str]] = field(default_factory=list)  # (pr, event, body, head)
    fail_submit: int = 0  # submissions GitHub fails to answer (a 502)
    refuse_submit: int = 0  # submissions GitHub refuses with a 4xx
    refused: int = 0
    login: str = REVIEW_BOT  # who the review credential acts as

    def viewer_login(self) -> str:
        return self.login

    def reviewed_on(self, pr: int, head: str, login: str) -> bool:
        return any(
            r.author == login and r.commit_id == head and r.state in ("APPROVED", "CHANGES_REQUESTED")
            for r in self.prs[pr].reviews
        )

    def submit_review(self, pr: int, event: str, body: str, head: str) -> bool:
        if self.fail_submit:
            self.fail_submit -= 1
            raise GhUnavailable("server", "HTTP 502 from the reviews endpoint")
        if self.refuse_submit:
            self.refuse_submit -= 1
            self.refused += 1
            return False
        with self.lock:
            self.submitted.append((pr, event, body, head))
            state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[event]
            p = self.prs[pr]
            self.prs[pr] = replace(
                p, reviews=(*p.reviews, Review(state, REVIEW_BOT, submitted_at=_now(), body=body, commit_id=head))
            )
        self._lossy("submit_review")
        return True


class FakeCheckouts:
    """Pinned checkouts whose base...head diff is `changed` (False: empty)."""

    def __init__(self, root: Path, changed: bool = True):
        self.root = root
        self.changed = changed
        self.pinned: list[tuple[int, str, str, str]] = []

    def ensure(self, pr: int, head: str, base_ref: str, base: str) -> str:
        self.pinned.append((pr, head, base_ref, base))
        path = self.root / f"review-pr-{pr}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def has_changes(self, base: str, head: str) -> bool:
        return self.changed

    def remove(self, pr: int) -> None:
        pass


def verdict(event: str, body: str = "Looks right."):
    return lambda _task: AgentResult(False, structured={"decision": event, "body": body}, cost_usd=0.2, session_id="r1")


def garbled(_task):
    return AgentResult(False, structured={"decision": "LGTM!!", "body": 7}, cost_usd=0.2, session_id="r1")


@pytest.fixture
def roles(tmp_path):
    def cfg(**env):
        return replace(
            from_env(
                {
                    "REPO": "o/r",
                    "FIX_BOT": FIX_BOT,
                    "REVIEW_BOT": REVIEW_BOT,
                    "BASE_BRANCH": "dev",
                    "STATE_DIR": str(tmp_path / "state"),
                    "REVIEW_STATE_DIR": str(tmp_path / "review-state"),
                    "REVIEWER_RUNTIME": "claude",
                    **env,
                }
            ),
            control_authority_root=str(tmp_path / "machine-state"),
        )

    class Roles:
        checkouts = FakeCheckouts(tmp_path / "checkouts")

        def review(self, gh, agent, checkouts=..., **env):
            return compose_review_daemon(
                cfg(**env),
                gh,
                agent,
                log=lambda _line: None,
                checkouts=self.checkouts if checkouts is ... else checkouts,
            )

        def fix(self, gh, agent, **env):
            return compose_fix_daemon(
                cfg(**env), gh, FakeWorktrees(tmp_path / "worktrees"), agent, log=lambda _line: None
            )

    return Roles()


def green(**fields) -> ReviewGitHub:
    """Issue 5 with its PR #100 open, CI green on h1."""
    gh = ReviewGitHub(issues={5: ready()}, **fields)
    open_pr(gh, head="h1", ci="pass", base_ref_name="dev", base_ref_oid="b1")
    return gh


def capped_for_a_human(gh, roles) -> bool:
    """The fix role's verdict on the PR after the review role stopped owing."""
    fix = roles.fix(gh, Agent())
    tick(fix)
    tick(fix)
    stops = gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))
    return Label.NEEDS_HUMAN in gh.prs[100].labels and len(stops) == 1 and "REVIEW" in stops[0].body


# ── the design table's REVIEW row ────────────────────────────────────────────


def test_the_review_claim_is_handed_back_only_after_the_leg_is_cleaned_up(roles, tmp_path):
    # The claim's release is what wakes both roles (StoreWake): it must come
    # after the verdict landed and the checkout is gone, so the woken tick
    # sees the verdict and a free slot.
    gh = green()
    seen_at_remove = []

    class Recording(FakeCheckouts):
        def remove(self, pr: int) -> None:
            seen_at_remove.append((list(gh.submitted), [lease.target for lease in review.control.leases()]))

    review = roles.review(gh, Agent(verdict("APPROVE", "Correct.")), checkouts=Recording(tmp_path / "rec"))
    tick(review)

    assert seen_at_remove == [([(100, "APPROVE", "Correct.", "h1")], [5])]
    assert review.control.leases() == ()

def test_a_green_head_without_a_bot_review_gets_one_review_leg_and_one_review_on_that_commit(roles):
    gh = green()
    agent = Agent(verdict("APPROVE", "Correct and tested."))
    review = roles.review(gh, agent)

    assert dispatched(tick(review), AgentKind.REVIEW) == [RunAgent(AgentKind.REVIEW, 100, head="h1")]
    assert tick(review) == []  # reviewed: nothing more is owed on h1

    assert gh.submitted == [(100, "APPROVE", "Correct and tested.", "h1")]
    assert len(agent.tasks) == 1 and roles.checkouts.pinned == [(100, "h1", "dev", "b1")]
    task = agent.tasks[0]
    # The reviewer diffs its pinned checkout itself: pointers, no pasted diff.
    assert task.cwd.endswith("review-pr-100") and "git diff b1...h1" in task.prompt
    assert "<untrusted_diff_json>" not in task.prompt


def test_a_review_on_a_moved_head_is_skipped_without_a_paid_leg(roles):
    gh = green()
    agent = Agent()
    review = roles.review(gh, agent)
    original = gh.get_pr

    def moved(number, **kw):
        gh.push("fix/5-null-token-crashes", "h2", ci="pass")
        return original(number, **kw)

    gh.get_pr = moved  # the author pushes between the decision and the leg
    tick(review)
    assert agent.tasks == [] and gh.submitted == []


# ── idempotent submission ────────────────────────────────────────────────────


def test_a_crash_after_the_review_lands_never_posts_a_second_review(roles):
    gh = green()
    gh.lose_reply.add("submit_review")  # GitHub records the review; the reply is lost
    agent = Agent(verdict("REQUEST_CHANGES", "Handle None."))
    review = roles.review(gh, agent)
    tick(review)

    restarted = roles.review(gh, Agent())
    for _ in range(3):
        tick(restarted)
    composed = roles.review(gh, Agent())
    asyncio.run(composed.legs.run(RunAgent(AgentKind.REVIEW, 100, head="h1")))  # a replayed leg

    assert gh.submitted == [(100, "REQUEST_CHANGES", "Handle None.", "h1")]
    assert len(agent.tasks) == 1


def test_a_submission_github_fails_to_answer_is_resubmitted_after_backoff_without_buying_the_review_again(roles, later):
    gh = green(fail_submit=1)
    agent = Agent(verdict("APPROVE"))
    review = roles.review(gh, agent)
    tick(review)
    assert gh.submitted == []
    assert tick(review) == []  # an outage is a machine end: backoff, no stop

    later()
    assert dispatched(tick(review), AgentKind.REVIEW)
    assert gh.submitted == [(100, "APPROVE", "Looks right.", "h1")]
    assert len(agent.tasks) == 1


def test_a_submission_github_refuses_stops_for_a_human_at_the_attempt_cap_after_one_paid_review(roles):
    # A 403 (token without pull-request write) or 422 never changes on retry;
    # as a machine end it retried free forever with no `needs-human`.
    gh = green(refuse_submit=99)
    agent = Agent(verdict("APPROVE"))
    review = roles.review(gh, agent)
    for _ in range(4):
        tick(review)

    assert tick(review) == []  # capped: nothing more is owed by the review role
    assert gh.submitted == [] and gh.refused == 2
    assert len(agent.tasks) == 1
    assert capped_for_a_human(gh, roles)


def test_a_dismissed_bot_verdict_buys_a_fresh_review_not_a_replay(roles):
    gh = green()
    agent = Agent(verdict("APPROVE", "First look."), verdict("REQUEST_CHANGES", "Second look: handle None."))
    review = roles.review(gh, agent)
    tick(review)
    p = gh.prs[100]
    gh.prs[100] = replace(p, reviews=tuple(replace(r, state="DISMISSED") for r in p.reviews))  # a human dismisses

    assert dispatched(tick(review), AgentKind.REVIEW) == [RunAgent(AgentKind.REVIEW, 100, head="h1")]
    assert len(agent.tasks) == 2
    assert [(s[1], s[3]) for s in gh.submitted] == [("APPROVE", "h1"), ("REQUEST_CHANGES", "h1")]


def test_the_bots_previous_review_reaches_the_next_rounds_reviewer(roles):
    gh = green()
    agent = Agent(verdict("REQUEST_CHANGES", "Handle a None token in parse()."), verdict("APPROVE"))
    review = roles.review(gh, agent)
    tick(review)
    earlier = gh.prs[100].reviews
    gh.push("fix/5-null-token-crashes", "h2", ci="pass")
    gh.prs[100] = replace(gh.prs[100], reviews=earlier)  # GitHub keeps them across a push; the fake drops them
    tick(review)

    assert "Handle a None token" not in agent.tasks[0].prompt
    second = agent.tasks[1].prompt
    assert "<untrusted_prior_review_json>" in second and "Handle a None token in parse()." in second


def test_a_head_with_nothing_to_review_stops_for_a_human_without_a_paid_leg(roles, tmp_path):
    gh = green()
    agent = Agent()
    review = roles.review(gh, agent, checkouts=FakeCheckouts(tmp_path / "empty", changed=False))
    for _ in range(3):
        tick(review)

    assert tick(review) == []
    assert agent.tasks == [] and gh.submitted == []
    assert capped_for_a_human(gh, roles)


def test_a_push_during_the_review_leaves_the_verdict_on_the_old_head_and_never_merges_on_it(roles):
    gh = green()

    def author_pushes_meanwhile(_task):
        gh.push("fix/5-null-token-crashes", "h2", ci="pass")
        return AgentResult(False, structured={"decision": "APPROVE", "body": "h1 is right."}, cost_usd=0.2)

    review = roles.review(gh, Agent(author_pushes_meanwhile, verdict("APPROVE", "h2 is right.")))
    fix = roles.fix(gh, Agent())
    tick(review)
    assert gh.submitted == [(100, "APPROVE", "h1 is right.", "h1")]
    assert not any(isinstance(a, MergePullRequest) for a in tick(fix)) and gh.merged == []

    assert dispatched(tick(review), AgentKind.REVIEW) == [RunAgent(AgentKind.REVIEW, 100, head="h2")]
    tick(fix)
    assert gh.merged == [(100, "h2")]


# ── boot checks: misconfigurations no retry can fix ──────────────────────────


def test_a_review_credential_that_is_not_review_bot_refuses_to_start(roles):
    # It posted under the real login while the read-back looked for REVIEW_BOT:
    # one duplicate review per retry, forever.
    for login in ("someone-else", ""):
        with pytest.raises(ValueError, match="REVIEW_BOT"):
            roles.review(ReviewGitHub(issues={5: ready()}, login=login), Agent())


def test_review_bot_matches_its_credential_up_to_case_and_the_bot_suffix(roles):
    roles.review(ReviewGitHub(issues={5: ready()}, login=REVIEW_BOT.upper().removesuffix("[BOT]")), Agent())


def test_the_review_role_refuses_to_start_without_a_checkout_to_pin_reviews_to(roles):
    with pytest.raises(ValueError, match="TARGET_DIR"):
        roles.review(ReviewGitHub(issues={5: ready()}), Agent(), checkouts=None)


# ── the split of work between the two roles ──────────────────────────────────


def test_a_bot_change_request_is_addressed_by_the_fix_role_not_reviewed_again(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(
        gh,
        head="h1",
        ci="pass",
        reviews=(Review("CHANGES_REQUESTED", REVIEW_BOT, body="Handle None.", submitted_at=_now(), commit_id="h1"),),
    )
    review = roles.review(gh, Agent())
    fix = roles.fix(gh, Agent(lambda _t: AgentResult(False, status="pushed", cost_usd=0.1, session_id="s1")))

    assert tick(review) == []
    assert dispatched(tick(fix), AgentKind.ADDRESS_REVIEW) == [RunAgent(AgentKind.ADDRESS_REVIEW, 100, head="h1")]


def test_three_unparseable_verdicts_on_one_head_stop_for_a_human_written_by_the_fix_role(roles):
    gh = green()
    agent = Agent(garbled, garbled, garbled)
    review = roles.review(gh, agent)
    fix = roles.fix(gh, Agent())
    for _ in range(3):
        assert dispatched(tick(review), AgentKind.REVIEW)
    assert tick(review) == []  # capped: the review role writes nothing
    assert Label.NEEDS_HUMAN not in gh.prs[100].labels

    tick(fix)
    tick(fix)
    assert Label.NEEDS_HUMAN in gh.prs[100].labels
    stops = gh.comments_with("pr", 100, stop_tag(StopReason.ATTEMPTS))
    assert len(stops) == 1 and "REVIEW" in stops[0].body and stops[0].author == FIX_BOT
    assert gh.submitted == [] and len(agent.tasks) == 3


@pytest.mark.parametrize("cap", ["wall", "budget"])
def test_a_capped_review_is_an_attempt_redispatched_without_backoff_until_the_cap(roles, cap):
    # A REVIEW never resumes, so either per-leg cap is one attempt (#51: a
    # budget-capped review must not re-dispatch forever as a free machine end).
    gh = green()
    agent = Agent(*(_cap_hit(cap, "r1") for _ in range(3)))
    review = roles.review(gh, agent)
    for _ in range(3):
        assert dispatched(tick(review), AgentKind.REVIEW)
    assert tick(review) == []  # capped: the review role writes nothing

    assert capped_for_a_human(gh, roles)
    assert gh.submitted == [] and len(agent.tasks) == 3
    assert all(task.resume is None for task in agent.tasks)


def test_a_machine_death_counts_no_attempt_and_backs_off(roles, later):
    gh = green()
    review = roles.review(gh, Agent(died, died, died, died))
    assert dispatched(tick(review), AgentKind.REVIEW)
    assert tick(review) == []  # backing off: no redispatch, no stop

    later()
    for _ in range(3):  # past the attempt cap of 3: deaths are not attempts
        assert dispatched(tick(review), AgentKind.REVIEW)
    assert not capped_for_a_human(gh, roles)
    assert Label.NEEDS_HUMAN not in gh.prs[100].labels


# ── the reviewer's tier (owner, 2026-10-13) ──────────────────────────────────

TIERS = {"FIX_MODEL_MED": "claude:claude-sonnet-5-5", "FIX_MODEL_HIGH": "claude:claude-opus-5-5",
         "REVIEW_MODEL": "claude-fable-5-1"}


def test_the_reviewer_runs_on_the_model_of_the_tier_that_pushed_the_head(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="fail")
    fixer = Agent(pushed(gh, "fix/5-null-token-crashes", "h2", ci="pass"))
    tick(roles.fix(gh, fixer, **TIERS))  # FIX_CI at MED (no triage data) pushes h2
    reviewer = Agent(verdict("APPROVE"))
    taken = tick(roles.review(gh, reviewer, **TIERS))
    assert dispatched(taken, AgentKind.REVIEW) == [RunAgent(AgentKind.REVIEW, 100, head="h2", tier=Tier.MED)]
    assert fixer.tasks[0].model == reviewer.tasks[0].model == "claude-sonnet-5-5"


def test_with_no_routed_fix_leg_the_reviewer_keeps_review_model(roles):
    gh = green()
    reviewer = Agent(verdict("APPROVE"))
    tick(roles.review(gh, reviewer, **TIERS))
    assert reviewer.tasks[0].model == "claude-fable-5-1"


def test_the_reviewer_runs_at_the_effort_of_the_fixers_tier_route(roles):
    gh = ReviewGitHub(issues={5: ready()})
    open_pr(gh, head="h1", ci="fail")
    tiers = {**TIERS, "FIX_MODEL_MED": "claude:claude-sonnet-5-5:high"}
    tick(roles.fix(gh, Agent(pushed(gh, "fix/5-null-token-crashes", "h2", ci="pass")), **tiers))
    reviewer = Agent(verdict("APPROVE"))
    tick(roles.review(gh, reviewer, **tiers))
    assert reviewer.tasks[0].effort is Effort.HIGH


# ── the bar the reviewer judges against (agent-autonomy audit, item 3) ───────


def test_the_reviewer_judges_against_the_stewards_criteria_not_the_pr_description(roles):
    from issue_agent.domain.triage import Steward
    from tests.app.test_fix_role import seed_light_legs

    gh = green()
    gh.prs[100] = replace(gh.prs[100], body="## Acceptance Criteria\n- whatever the author says")
    agent = Agent(verdict("APPROVE"))
    review = roles.review(gh, agent)
    seed_light_legs(review, 5, STEWARD={"steward": Steward(acceptance_criteria=("A null token returns 401",)).record()})
    tick(review)

    prompt = agent.tasks[0].prompt
    bar = prompt.split("<untrusted_steward_acceptance_criteria_json>")[1].split("</untrusted_steward")[0]
    assert "A null token returns 401" in bar and "whatever the author says" not in bar
    assert "<untrusted_issue_body_json>" not in prompt


def test_without_steward_criteria_the_reviewer_is_briefed_the_issue(roles):
    gh = green()
    gh.issues[5] = replace(gh.issues[5], body="## Acceptance criteria\n- [ ] The API answers 401")
    agent = Agent(verdict("APPROVE"))
    tick(roles.review(gh, agent))

    prompt = agent.tasks[0].prompt
    assert "The API answers 401" in prompt.split("<untrusted_issue_body_json>")[1]
    assert gh.submitted == [(100, "APPROVE", "Looks right.", "h1")]
