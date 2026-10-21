"""STEWARD, TRIAGE and routing (owner, 2026-10-13/14): a steward leg (criteria,
duplicate check), then a cheap triage leg, per admitted issue before RESOLVE;
the triage tier routes every fix act, one rung stronger per
earlier attempt; a laundered duplicate claim stops for a human; the reviewer
runs at the tier of the fix leg that pushed its head."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.actions import AgentKind, PostComment, Relabel, RunAgent, Target
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.backoff import LegEnd
from issue_agent.domain.checks import Check, Observed, RequiredCheck
from issue_agent.domain.labels import Label
from issue_agent.domain.policy import actions
from issue_agent.domain.effort import Effort
from issue_agent.domain.routing import DEFAULT_TIER, ModelRoute, Tier, escalate, parse_model_route, parse_tier
from issue_agent.domain.state import (
    TRIAGE_TAG, Backoff, Dispatch, DuplicateStop, Harness, Local, PlainStop, Release, StopReason,
    TriageNote, Wait, WaitReason, issue_key, pr_key, stop_tag,
)
from tests.fakes.recorded import derive
from issue_agent.domain.triage import (
    states_criteria,
    MAX_CRITERIA, MAX_CRITERION, MAX_RATIONALE, Steward, Triage, criteria_wanted, link_refusal,
    offered_blockers, offered_duplicates, parse_steward, parse_triage,
    steward_from_record, steward_schema, triage_schema,
)
from issue_agent.domain.world import Comment, IssueView, PRView, Push, WorldState

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=1)
H = Harness("fix-bot", "review-bot")
HEAD = "h1"


def at(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)


def iso(minute: float) -> str:
    return at(minute).isoformat()


def ready(number: int = 3, *labels: Label, **kw) -> IssueView:
    return IssueView(number=number, labels=frozenset((Label.AI_READY, *labels)), **kw)


def green_pr(number: int = 7, issue: int = 3, **kw) -> PRView:
    check = Check("test", state="COMPLETED", bucket=kw.pop("bucket", "pass"), required=True)
    return PRView(number=number, head_ref_name=f"fix/{issue}-slug", head_ref_oid=HEAD,
                  ci=Observed(HEAD, HEAD, frozenset({RequiredCheck("test")}), (check,)),
                  mergeable="MERGEABLE", merge_state="CLEAN", **kw)


def triage_leg(minute: float = 0, tier: Tier | None = Tier.MED, *, machine: bool = False, **fields) -> LegEnd:
    result = Triage(tier, **fields) if tier is not None and not machine else None
    return LegEnd(AgentKind.TRIAGE, at(minute), machine, triage=result)


def steward_leg(minute: float = -1, *, machine: bool = False, usable: bool = True, **fields) -> LegEnd:
    result = Steward(**fields) if usable and not machine else None
    return LegEnd(AgentKind.STEWARD, at(minute), machine, steward=result)


def done(tier: Tier | None = Tier.MED, minute: float = 0, **steward) -> tuple[LegEnd, LegEnd]:
    """Both light legs ended: the steward just before the triage."""
    return steward_leg(minute - 0.5, **steward), triage_leg(minute, tier)


def fix_leg(kind: AgentKind, minute: float, tier: Tier | None = None, *, machine: bool = False) -> LegEnd:
    return LegEnd(kind, at(minute), machine, head=HEAD if kind is not AgentKind.RESOLVE else "", tier=tier)


def noted(minute: float = 1) -> Comment:
    return Comment("fix-bot", f"{H.marker}\n{TRIAGE_TAG}\n**Triage**", iso(minute))


def state_of(world: WorldState, legs: dict | None = None, **kw):
    states = derive(world, Local(now=kw.pop("now", NOW), legs=legs or {}, **kw), H)
    assert len(states) == 1, states
    return next(iter(states.values()))


def stop_comment(minute: float = 1) -> Comment:
    return Comment("fix-bot", f"{H.marker}\n{stop_tag(StopReason.DUPLICATE)}\nduplicate", iso(minute))


# ── derive: STEWARD, then TRIAGE, then the comment, then RESOLVE ────────────


def test_an_unstewarded_ready_issue_dispatches_steward_first():
    world = WorldState(issues=(ready(),))
    assert state_of(world) == Dispatch(AgentKind.STEWARD)
    assert actions(derive(world, Local(now=NOW), H), H) == (RunAgent(AgentKind.STEWARD, 3),)


def test_a_stewarded_issue_dispatches_triage_then_comment_then_resolve_in_order():
    world = WorldState(issues=(ready(),))
    steward = steward_leg(-1, acceptance_criteria=("Esc closes it",))
    assert state_of(world, {issue_key(3): (steward,)}) == Dispatch(AgentKind.TRIAGE)

    both = {issue_key(3): (steward, triage_leg(0, Tier.HIGH))}
    assert isinstance(state_of(world, both), TriageNote)

    posted = WorldState(issues=(ready(comments=(noted(),)),))
    assert state_of(posted, both) == Dispatch(AgentKind.RESOLVE, tier=Tier.HIGH)


def test_a_duplicate_stops_the_issue_before_triage_is_bought():
    legs = {issue_key(3): (steward_leg(-1, duplicate_of=2, duplicate_reason="Same Esc bug"),)}
    world = WorldState(issues=(ready(),))
    assert state_of(world, legs) == DuplicateStop(2, "Same Esc bug", iso(-1))
    assert not any(isinstance(a, RunAgent) for a in actions(derive(world, Local(now=NOW, legs=legs), H), H))


def test_a_released_duplicate_stop_proceeds_to_triage():
    legs = {issue_key(3): (steward_leg(-1, duplicate_of=2, duplicate_reason="Same"),)}
    released = ready(3, comments=(stop_comment(1), Comment("alice", "not a dup", iso(2))))
    assert state_of(WorldState(issues=(released,)), legs) == Dispatch(AgentKind.TRIAGE)


def test_a_triaged_issue_is_never_stewarded_or_triaged_again_and_resolves_at_its_tier():
    world = WorldState(issues=(ready(comments=(noted(),)),))
    assert state_of(world, {issue_key(3): done(Tier.HIGH)}) == Dispatch(AgentKind.RESOLVE, tier=Tier.HIGH)


@pytest.mark.parametrize("kind", [AgentKind.STEWARD, AgentKind.TRIAGE])
def test_a_machine_ended_light_leg_retries_under_the_machine_backoff(kind):
    world = WorldState(issues=(ready(),))
    ended = steward_leg(0, machine=True) if kind is AgentKind.STEWARD else triage_leg(0, machine=True)
    prior = () if kind is AgentKind.STEWARD else (steward_leg(-1),)
    legs = {issue_key(3): (*prior, ended)}
    assert state_of(world, legs, now=at(0.5)) == Backoff(kind, at(1))
    assert state_of(world, legs, now=at(2)) == Dispatch(kind)


def test_unusable_light_results_resolve_at_the_default_tier_with_no_comment_and_no_criteria():
    world = WorldState(issues=(ready(),))
    legs = {issue_key(3): (steward_leg(-1, usable=False), triage_leg(0, None))}
    assert state_of(world, legs) == Dispatch(AgentKind.RESOLVE, tier=DEFAULT_TIER)
    # An unusable steward alone still ran: triage is next, not another steward.
    assert state_of(world, {issue_key(3): (steward_leg(-1, usable=False),)}) == Dispatch(AgentKind.TRIAGE)


def test_a_failed_grade_still_posts_the_stewards_criteria_at_the_default_tier():
    legs = {issue_key(3): (steward_leg(-1, acceptance_criteria=("Esc closes it",)), triage_leg(0, None))}
    (comment,) = actions(derive(WorldState(issues=(ready(),)), Local(now=NOW, legs=legs), H), H)
    assert "- [ ] Esc closes it" in comment.body and f"`{DEFAULT_TIER.value}`" in comment.body


def test_light_acts_wait_under_the_same_gates_as_any_paid_act():
    world = WorldState(issues=(ready(),))
    assert state_of(world, headroom=False) == Wait(WaitReason.HEADROOM)
    assert state_of(world, lifetime_capped=frozenset({3})) == PlainStop(StopReason.LIFETIME_CAP, "-")
    assert state_of(world, {issue_key(3): (steward_leg(-1),)}, headroom=False) == Wait(WaitReason.HEADROOM)


def test_neither_light_act_holds_a_heavy_dispatch_slot():
    light = (AgentKind.STEWARD, AgentKind.TRIAGE)
    assert not any(kind.holds_dispatch_slot for kind in light)
    assert all(kind.holds_dispatch_slot for kind in AgentKind if kind not in light)


# ── derive: combined-result legs recorded before the split ──────────────────


def test_a_triage_leg_with_no_usable_result_is_stewarded():
    # Indistinguishable from a post-split triage run without a steward leg:
    # one extra cheap steward leg is the cost of failing safe.
    legs = {issue_key(3): (LegEnd(AgentKind.TRIAGE, at(0), False),)}
    assert state_of(WorldState(issues=(ready(),)), legs) == Dispatch(AgentKind.STEWARD)


def test_a_post_split_triage_alone_does_not_satisfy_the_steward():
    legs = {issue_key(3): (triage_leg(0, Tier.LOW),)}
    assert state_of(WorldState(issues=(ready(),)), legs) == Dispatch(AgentKind.STEWARD)


# ── derive: the triage comment ──────────────────────────────────────────────


def test_one_comment_carries_the_tier_rationale_and_the_stewards_criteria():
    steward = steward_leg(-1, acceptance_criteria=("Pressing Esc closes the dialog",))
    triage = Triage(Tier.LOW, "One handler.")
    world = WorldState(issues=(ready(),))
    legs = {issue_key(3): (steward, LegEnd(AgentKind.TRIAGE, at(0), False, triage=triage))}
    assert state_of(world, legs) == TriageNote(triage, iso(0), steward.steward)

    (comment,) = actions(derive(world, Local(now=NOW, legs=legs), H), H)
    assert isinstance(comment, PostComment) and comment.target is Target.ISSUE and comment.number == 3
    assert comment.key == f"triage:issue:3:{iso(0)}"
    assert TRIAGE_TAG in comment.body and H.marker in comment.body
    assert "`low`" in comment.body and "One handler." in comment.body
    assert "- [ ] Pressing Esc closes the dialog" in comment.body

    posted = WorldState(issues=(ready(comments=(Comment("fix-bot", comment.body, iso(1)),)),))
    assert state_of(posted, legs) == Dispatch(AgentKind.RESOLVE, tier=Tier.LOW)


def test_a_steward_with_no_criteria_posts_the_tier_without_an_acceptance_heading():
    legs = {issue_key(3): done(Tier.MED)}
    (comment,) = actions(derive(WorldState(issues=(ready(),)), Local(now=NOW, legs=legs), H), H)
    assert "`med`" in comment.body and "Acceptance criteria" not in comment.body


def test_agent_text_in_the_triage_comment_cannot_mention_or_embed_an_image():
    steward = steward_leg(-1, acceptance_criteria=("Ping @acme/security ![x](https://evil.example/p.png)",))
    triage = LegEnd(AgentKind.TRIAGE, at(0), False,
                    triage=Triage(Tier.LOW, 'cc @owner <img src="https://evil.example/t.png">'))
    legs = {issue_key(3): (steward, triage)}
    (comment,) = actions(derive(WorldState(issues=(ready(),)), Local(now=NOW, legs=legs), H), H)
    assert "@acme" not in comment.body and "@owner" not in comment.body and "![" not in comment.body
    assert "<img" not in comment.body


def test_a_forged_triage_tag_from_a_human_does_not_count_as_the_comment():
    forged = Comment("mallory", f"{H.marker}\n{TRIAGE_TAG}", iso(1))
    legs = {issue_key(3): done()}
    assert isinstance(state_of(WorldState(issues=(ready(comments=(forged,)),)), legs), TriageNote)


# ── derive: the duplicate stop comment and its release ─────────────────────


def test_a_suspected_duplicate_stops_for_a_human_with_a_comment_linking_the_original():
    legs = {issue_key(3): (steward_leg(0, duplicate_of=2, duplicate_reason="Same Esc bug"),)}
    world = WorldState(issues=(ready(),))
    label, comment = actions(derive(world, Local(now=NOW, legs=legs), H), H)
    assert label == Relabel(Target.ISSUE, 3, remove=None, add=Label.NEEDS_HUMAN)
    assert "#2" in comment.body and "`Same Esc bug`" in comment.body
    assert stop_tag(StopReason.DUPLICATE) in comment.body


def test_a_reply_releases_the_duplicate_stop_and_the_issue_then_resolves():
    legs = {issue_key(3): done(Tier.MED, 0, duplicate_of=2, duplicate_reason="Same")}
    held = ready(3, Label.NEEDS_HUMAN, comments=(stop_comment(1),))
    assert state_of(WorldState(issues=(held,)), legs) == Wait(WaitReason.NEEDS_HUMAN)
    replied = ready(3, Label.NEEDS_HUMAN, comments=(stop_comment(1), Comment("alice", "not a dup", iso(2))))
    assert state_of(WorldState(issues=(replied,)), legs) == Release()

    released = ready(3, comments=(stop_comment(1), Comment("alice", "not a dup", iso(2)), noted(3)))
    assert state_of(WorldState(issues=(released,)), legs) == Dispatch(AgentKind.RESOLVE, tier=Tier.MED)


def test_removing_the_label_also_releases_the_duplicate_stop():
    legs = {issue_key(3): done(Tier.MED, 0, duplicate_of=2, duplicate_reason="Same")}
    assert isinstance(state_of(WorldState(issues=(ready(comments=(stop_comment(1),)),)), legs), TriageNote)


# ── derive: routing and escalation ──────────────────────────────────────────


@pytest.mark.parametrize(("prior", "tier"), [(0, Tier.LOW), (1, Tier.MED), (2, Tier.HIGH)])
def test_each_earlier_attempt_escalates_the_next_one_rung(prior, tier):
    legs = {issue_key(3): (*done(Tier.LOW), *(fix_leg(AgentKind.RESOLVE, 1 + i) for i in range(prior)))}
    world = WorldState(issues=(ready(comments=(noted(0.5),)),))
    assert state_of(world, legs) == Dispatch(AgentKind.RESOLVE, tier=tier)


def routed(state) -> tuple:
    """A dispatch's act, head and tier (its evidence is test_state_policy's)."""
    assert isinstance(state, Dispatch), state
    return (state.act, state.head, state.tier)


def test_escalation_is_capped_at_xhigh():
    legs = {issue_key(3): (*done(Tier.XHIGH),), pr_key(7): (fix_leg(AgentKind.FIX_CI, 1),
                                                                      fix_leg(AgentKind.FIX_CI, 2))}
    assert routed(state_of(WorldState(prs=(green_pr(bucket="fail"),)), legs)) == (AgentKind.FIX_CI, HEAD, Tier.XHIGH)


@pytest.mark.parametrize("kind_pr", [
    (AgentKind.FIX_CI, {"bucket": "fail"}),
    (AgentKind.REBASE, {"mergeable": "CONFLICTING"}),
])
def test_pr_stage_acts_route_by_the_issues_triage_tier(kind_pr):
    kind, shape = kind_pr
    pr = green_pr(**shape) if "bucket" in shape else green_pr()
    if "mergeable" in shape:
        pr = PRView(**{**pr.__dict__, "mergeable": "CONFLICTING"})
    legs = {issue_key(3): (*done(Tier.HIGH),)}
    assert routed(state_of(WorldState(prs=(pr,)), legs)) == (kind, HEAD, Tier.HIGH)


def test_a_pre_routing_pr_without_triage_runs_at_the_default_tier():
    assert routed(state_of(WorldState(prs=(green_pr(bucket="fail"),)))) == (AgentKind.FIX_CI, HEAD, DEFAULT_TIER)


def test_a_human_reset_drops_escalation_back_to_the_triaged_tier():
    legs = {issue_key(3): (*done(Tier.LOW),),
            pr_key(7): (fix_leg(AgentKind.FIX_CI, 1), fix_leg(AgentKind.FIX_CI, 2))}
    pr = green_pr(bucket="fail", comments=(Comment("alice", "try again", iso(3)),))
    assert routed(state_of(WorldState(prs=(pr,)), legs)) == (AgentKind.FIX_CI, HEAD, Tier.LOW)


# ── derive: the reviewer's tier ─────────────────────────────────────────────


def test_review_runs_at_the_tier_of_the_fix_leg_that_pushed_the_head():
    legs = {issue_key(3): (*done(Tier.LOW), fix_leg(AgentKind.RESOLVE, 1, Tier.LOW)),
            pr_key(7): (fix_leg(AgentKind.FIX_CI, 2, Tier.MED),)}
    assert state_of(WorldState(prs=(green_pr(),)), legs) == Dispatch(AgentKind.REVIEW, HEAD, Tier.MED)


def test_review_of_a_head_a_human_pushed_falls_back_to_the_triaged_tier():
    legs = {issue_key(3): (*done(Tier.HIGH), fix_leg(AgentKind.RESOLVE, 1, Tier.LOW))}
    pr = green_pr(pushes=(Push("alice", iso(5)),))
    assert state_of(WorldState(prs=(pr,)), legs) == Dispatch(AgentKind.REVIEW, HEAD, Tier.HIGH)


def test_review_with_no_routing_data_carries_no_tier_so_review_model_applies():
    legs = {issue_key(3): (fix_leg(AgentKind.RESOLVE, 1),)}  # a pre-routing leg: no tier recorded
    state = state_of(WorldState(prs=(green_pr(),)), legs)
    assert state == Dispatch(AgentKind.REVIEW, HEAD) and state.tier is None
    assert actions({pr_key(7): state}, H) == (RunAgent(AgentKind.REVIEW, 7, head=HEAD),)


def test_a_machine_ended_fix_leg_is_not_the_head_producer():
    legs = {issue_key(3): (fix_leg(AgentKind.RESOLVE, 1, Tier.LOW),),
            pr_key(7): (fix_leg(AgentKind.FIX_CI, 2, Tier.XHIGH, machine=True),)}
    assert state_of(WorldState(prs=(green_pr(),)), legs).tier is Tier.LOW


# ── tiers on actions ────────────────────────────────────────────────────────


def test_tiers_are_carried_exactly_where_routing_applies():
    assert RunAgent(AgentKind.RESOLVE, 3).tier is DEFAULT_TIER
    assert RunAgent(AgentKind.REVIEW, 7, head=HEAD).tier is None
    with pytest.raises(ValueError):
        RunAgent(AgentKind.TRIAGE, 3, tier=Tier.LOW)
    with pytest.raises(ValueError):
        RunAgent(AgentKind.STEWARD, 3, tier=Tier.LOW)
    with pytest.raises(ValueError):
        Dispatch(AgentKind.STEWARD, head="h")
    with pytest.raises(ValueError):
        Dispatch(AgentKind.TRIAGE, tier=Tier.LOW)
    with pytest.raises(ValueError):
        Dispatch(AgentKind.TRIAGE, head="h")
    with pytest.raises(ValueError):
        DuplicateStop(0, "", "-")  # no target


def test_escalate_clamps_absurd_attempt_counts():
    assert escalate(Tier.MED, -5) is Tier.MED
    assert escalate(Tier.LOW, 10**9) is Tier.XHIGH
    assert escalate(Tier.LOW, True) is Tier.LOW  # type: ignore[arg-type]
    assert parse_tier("HIGH") is None and parse_tier(None) is None and parse_tier("high") is Tier.HIGH


# ── the model route spec ────────────────────────────────────────────────────


@pytest.mark.parametrize(("spec", "route"), [
    ("codex:gpt-6-luna", ModelRoute(RuntimeName.CODEX, "gpt-6-luna")),
    ("claude:claude-haiku-5-5", ModelRoute(RuntimeName.CLAUDE, "claude-haiku-5-5")),
    ("claude:claude-fable-5-1", ModelRoute(RuntimeName.CLAUDE, "claude-fable-5-1")),
    (" claude:opus ", ModelRoute(RuntimeName.CLAUDE, "opus")),
    ("claude:us.anthropic.claude-opus-5-5-v1:0", ModelRoute(RuntimeName.CLAUDE, "us.anthropic.claude-opus-5-5-v1:0")),
])
def test_runtime_model_specs_parse(spec, route):
    assert parse_model_route(spec) == route


@pytest.mark.parametrize(("spec", "route"), [
    ("claude:claude-sonnet-5-5:high", ModelRoute(RuntimeName.CLAUDE, "claude-sonnet-5-5", Effort.HIGH)),
    ("codex:gpt-6-luna:low", ModelRoute(RuntimeName.CODEX, "gpt-6-luna", Effort.LOW)),
    ("  claude:opus:HIGH  ", ModelRoute(RuntimeName.CLAUDE, "opus", Effort.HIGH)),
    ("claude:opus:xhigh", ModelRoute(RuntimeName.CLAUDE, "opus", Effort.XHIGH)),
    ("claude:opus:max", ModelRoute(RuntimeName.CLAUDE, "opus", Effort.MAX)),
    ("codex:gpt-6-luna:xhigh", ModelRoute(RuntimeName.CODEX, "gpt-6-luna", Effort.XHIGH)),
    ("claude:us.anthropic.claude-opus-5-5-v1:0:medium",
     ModelRoute(RuntimeName.CLAUDE, "us.anthropic.claude-opus-5-5-v1:0", Effort.MEDIUM)),
])
def test_an_optional_effort_segment_parses_and_round_trips(spec, route):
    assert parse_model_route(spec) == route
    assert parse_model_route(route.spec) == route


@pytest.mark.parametrize("spec", [
    "codex:gpt-6-luna:max",        # codex has no max
    "claude:opus:turbo",           # unknown word
    "claude:opus:",                # empty segment
    "claude:opus:high:extra",      # extra segment: "extra" is not an effort
    "claude:opus:high:",
    "claude:opus: high",           # whitespace inside the spec
    "claude::high",                # no model
])
def test_an_invalid_effort_is_refused_with_the_reason(spec):
    with pytest.raises(ValueError):
        parse_model_route(spec)


def test_the_invalid_effort_message_names_the_runtimes_choices():
    with pytest.raises(ValueError, match="xhigh"):
        parse_model_route("codex:gpt-6-luna:max")


@pytest.mark.parametrize("spec", [
    "", "gpt-6-luna", "glm:glm-5", "codex:", "codex:claude-haiku-5-5", "claude:gpt-6-luna", "claude:has space",
    "codex:" + "x" * 300, None, 5,
])
def test_malformed_or_cross_runtime_specs_are_refused(spec):
    with pytest.raises(ValueError):
        parse_model_route(spec)


# ── the light results: parse and launder ────────────────────────────────────

TRIAGE_OK = {"tier": "high", "rationale": "Touches two subsystems."}
OK = {"acceptance_criteria": ["A works", "B works"], "duplicate_of": None, "duplicate_reason": ""}


def test_a_well_formed_triage_parses_and_ignores_legacy_steward_keys():
    assert parse_triage(TRIAGE_OK) == Triage(Tier.HIGH, "Touches two subsystems.")
    assert parse_triage({**TRIAGE_OK, **OK}) == Triage(Tier.HIGH, "Touches two subsystems.")


@pytest.mark.parametrize("raw", [
    None, [], "high", {}, {**TRIAGE_OK, "tier": "trivial"}, {**TRIAGE_OK, "tier": None}, {**TRIAGE_OK, "rationale": 7},
    {**TRIAGE_OK, "rationale": "x" * (MAX_RATIONALE + 1)}, {"rationale": "no tier"},
])
def test_malformed_or_oversized_triage_is_no_triage(raw):
    assert parse_triage(raw) is None


def test_a_triage_record_round_trips():
    triage = parse_triage(TRIAGE_OK)
    assert parse_triage(triage.record()) == triage
    assert parse_triage("{not json") is None


def test_a_well_formed_steward_parses():
    assert parse_steward(OK, (), 3) == Steward(("A works", "B works"))


@pytest.mark.parametrize("raw", [
    None, [], "A", {**OK, "acceptance_criteria": "A"}, {**OK, "acceptance_criteria": ["A", 5]},
    {**OK, "duplicate_reason": 3},
    {**OK, "acceptance_criteria": ["x"] * (MAX_CRITERIA + 1)},
    {**OK, "acceptance_criteria": ["x" * (MAX_CRITERION + 1)]},
    {**OK, "blocked_by_reason": ["no"]},
])
def test_malformed_or_oversized_steward_output_is_no_steward(raw):
    assert parse_steward(raw, (2,), 3) is None


@pytest.mark.parametrize("raw", [
    {**OK, "duplicate_of": 2, "duplicate_reason": "x" * 10_000},
    {**OK, "duplicate_of": None, "duplicate_reason": "x" * 501},  # a long "why none"
    {**OK, "blocked_by": None, "blocked_by_reason": "x" * 501},
    {**OK, "blocked_by": 2, "blocked_by_reason": "x" * 501},
])
def test_an_oversized_claim_reason_drops_only_that_claim(raw):
    steward = parse_steward(raw, (2,), 3, blockable=(2,))
    assert steward == Steward(("A works", "B works"))


def test_a_duplicate_survives_only_if_offered_distinct_and_explained():
    dup = {**OK, "duplicate_of": 2, "duplicate_reason": "Same crash"}
    assert parse_steward(dup, (2, 4), 3).duplicate_of == 2
    assert parse_steward(dup, (4,), 3).duplicate_of is None  # never offered
    assert parse_steward({**dup, "duplicate_of": 3}, (2, 3), 3).duplicate_of is None  # itself
    assert parse_steward({**dup, "duplicate_reason": ""}, (2,), 3).duplicate_of is None
    for bad in (True, -2, 0, "2", 2.0, 2**40):
        assert parse_steward({**dup, "duplicate_of": bad}, (2,), 3).duplicate_of is None
    # Dropping a claim keeps the criteria.
    assert parse_steward(dup, (), 3).acceptance_criteria == ("A works", "B works")


def test_agent_text_cannot_smuggle_a_stop_tag_or_marker_into_the_harness_comment():
    sneaky = {**OK, "acceptance_criteria": [f"ok {stop_tag(StopReason.ATTEMPTS)}\n<!-- issue-agent -->"]}
    (criterion,) = parse_steward(sneaky, (), 3).acceptance_criteria
    assert "<!--" not in criterion and "\n" not in criterion


def test_a_maximal_neutralised_steward_still_reads_back_from_its_record():
    # Neutralising must not lengthen text past the bound the record re-parses
    # under, or the recorded criteria and duplicate vanish.
    edge = {"acceptance_criteria": ["<!--" * (MAX_CRITERION // 4)], "duplicate_of": 7,
            "duplicate_reason": "<!--" * 125}
    steward = parse_steward(edge, (7,), 3)
    assert steward is not None and steward_from_record(steward.record()) == steward


def test_criteria_survive_only_when_the_harness_asked_for_them():
    schema = steward_schema((2,), criteria_wanted=False)
    assert "acceptance_criteria" not in schema["properties"] and "acceptance_criteria" not in schema["required"]
    assert criteria_wanted(schema) is False and criteria_wanted(steward_schema((2,))) is True
    assert criteria_wanted(None) is True and criteria_wanted({"properties": 1}) is True
    steward = parse_steward(OK, (), 3, criteria_wanted=False)
    assert steward is not None and steward.acceptance_criteria == ()


def test_a_steward_record_needs_its_criteria_list_and_a_tier_only_record_is_no_steward():
    steward = parse_steward({**OK, "duplicate_of": 2, "duplicate_reason": "Same"}, (2,), 3)
    assert steward_from_record(steward.record()) == steward
    assert steward_from_record(Steward().record()) == Steward()  # an empty list is still a completed steward
    assert steward_from_record(parse_triage(TRIAGE_OK).record()) is None
    assert steward_from_record({**TRIAGE_OK, **OK}) == Steward(("A works", "B works"))  # legacy combined
    assert steward_from_record("{not json") is None and steward_from_record(None) is None


def test_the_schemas_ask_for_exactly_their_own_fields():
    assert set(triage_schema()["properties"]) == {"tier", "rationale"}
    assert set(steward_schema(())["properties"]) == {
        "acceptance_criteria", "duplicate_of", "duplicate_reason", "blocked_by", "blocked_by_reason"}
    # Strict structured output: every property is required (a nullable or empty answer is still an answer).
    schema = steward_schema(())
    assert set(schema["required"]) == set(schema["properties"])


def test_the_schema_offers_exactly_the_listed_issues_and_reads_them_back():
    schema = steward_schema([5, 2, 2, -1, "x", 0])  # type: ignore[list-item]
    assert schema["properties"]["duplicate_of"]["enum"] == [2, 5, None]
    assert offered_duplicates(schema) == frozenset({2, 5})
    assert steward_schema([])["properties"]["duplicate_of"] == {"type": "null"}
    assert offered_duplicates(None) == offered_duplicates({"properties": 1}) == frozenset()
    assert len(offered_duplicates(steward_schema(range(1, 100_000)))) == 300


# ── STEWARD's dependency claim and path guesses ──────────────────────────────


def test_a_blocked_by_claim_survives_only_for_an_offered_open_issue_with_a_reason():
    claim = {**OK, "blocked_by": 4, "blocked_by_reason": "Needs the export API #4 adds"}
    steward = parse_steward(claim, (2, 4), 3, blockable=(4,))
    assert (steward.blocked_by, steward.blocked_by_reason) == (4, "Needs the export API #4 adds")
    assert parse_steward(claim, (2, 4), 3, blockable=()).blocked_by is None  # offered, but not open
    assert parse_steward({**claim, "blocked_by": 3}, (3,), 3, blockable=(3,)).blocked_by is None  # itself
    assert parse_steward({**claim, "blocked_by_reason": ""}, (4,), 3, blockable=(4,)).blocked_by is None
    for bad in (True, "4", 4.0, 0, -4, None, 2**40):
        dropped = parse_steward({**claim, "blocked_by": bad}, (4,), 3, blockable=(4,))
        assert dropped is not None and (dropped.blocked_by, dropped.blocked_by_reason) == (None, "")
    assert parse_steward({**claim, "blocked_by_reason": "x" * 501}, (4,), 3, blockable=(4,)).blocked_by is None


def test_the_claims_read_back_from_the_record_and_legacy_records_have_none():
    claim = {**OK, "blocked_by": 4, "blocked_by_reason": "Needs #4"}
    steward = parse_steward(claim, (), 3, blockable=(4,))
    assert steward is not None and steward.blocked_by == 4
    assert steward_from_record(steward.record()) == steward
    legacy = steward_from_record({"acceptance_criteria": ["A"], "duplicate_of": None, "duplicate_reason": ""})
    assert legacy == Steward(("A",))


def test_the_schema_offers_open_issues_as_blockers_and_reads_them_back():
    schema = steward_schema((2, 4, 5), blockable=(5, 4))
    assert schema["properties"]["blocked_by"]["enum"] == [4, 5, None]
    assert offered_blockers(schema) == frozenset({4, 5}) and offered_duplicates(schema) == frozenset({2, 4, 5})
    assert steward_schema((2,))["properties"]["blocked_by"] == {"type": "null"}
    assert offered_blockers(None) == frozenset()


def test_a_recorded_claim_naming_the_issue_itself_is_dropped():
    claim = {**OK, "blocked_by": 3, "blocked_by_reason": "Needs itself", "duplicate_of": 3, "duplicate_reason": "Same"}
    steward = steward_from_record(claim, 3)
    assert steward is not None and steward.blocked_by is None and steward.duplicate_of is None
    assert steward.acceptance_criteria == ("A works", "B works")
    assert steward_from_record(claim, 4) == Steward(
        ("A works", "B works"), duplicate_of=3, duplicate_reason="Same", blocked_by=3, blocked_by_reason="Needs itself")


@pytest.mark.parametrize(
    ("issue", "blocker", "open_", "its_blockers", "refused"),
    [
        (3, 4, True, (), ""),
        (3, 4, True, (9, 10), ""),
        (3, 3, True, (), "an issue cannot block itself"),
        (3, 4, False, (), "#4 is closed"),
        (3, 4, True, (9, 3), "#4 is itself blocked by this issue, so linking would make a cycle"),
    ],
)
def test_link_refusal_rechecks_the_claim_on_fresh_reads(issue, blocker, open_, its_blockers, refused):
    assert link_refusal(issue, blocker, open_, its_blockers) == refused


def test_a_blocker_and_its_reason_come_together():
    assert Steward(blocked_by=4, blocked_by_reason="why").blocked_by == 4
    for blocked_by, reason in ((4, ""), (None, "why"), (0, "why"), (True, "why")):
        with pytest.raises(ValueError):
            Steward(blocked_by=blocked_by, blocked_by_reason=reason)


# ── an issue that states its own criteria is not asked for any ───────────────


@pytest.mark.parametrize(
    "body,states",
    [
        ("Crash.\n\n## Acceptance criteria\n- [ ] 401", True),
        ("   ### ACCEPTANCE CRITERIA (draft)\n- a", True),
        ("We need acceptance criteria for this.", False),  # prose, not a heading
        ("    ## Acceptance criteria", False),  # four spaces: indented code
        ("#Acceptance criteria", False),  # not an ATX heading
        ("<!-- template:\n## Acceptance criteria\n-->\nCrash.", False),  # a commented-out template heading
        ("<!-- unclosed\n## Acceptance criteria", False),
        ("", False),
        ("x" * 1_000_000 + "\n## Acceptance criteria", True),
    ],
    ids=["heading", "upper-indented", "prose", "code-indent", "no-space", "html-comment", "unclosed-comment", "empty", "huge"],
)
def test_states_criteria_is_an_acceptance_criteria_heading(body, states):
    assert states_criteria(body) is states
