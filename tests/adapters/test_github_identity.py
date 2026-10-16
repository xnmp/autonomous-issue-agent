"""Account provenance across REST, GraphQL, pickup and privileged effects (#481)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from issue_agent.adapters.github import GitHubGateway, parse_fix_issue, parse_fix_pr, parse_issue, parse_pr
from issue_agent.adapters.github_identity import parse_account
from issue_agent.domain.actions import AgentKind, Relabel
from issue_agent.domain.backoff import LegEnd
from issue_agent.domain.identity import account_eq
from issue_agent.domain.policy import architecture_actions, base_ci_actions
from issue_agent.domain.state import Dispatch, Harness, Local, bot_verdict_on_head, derive, harness_gap, human_comments, issue_key
from issue_agent.domain.world import Pickup
from tests.adapters.test_architecture_observe import Server
from tests.adapters.test_github_base_ci import job, run_row, tip
from tests.adapters.test_github_fix_observe import issue_node, labels, pr_node

H = Harness("steward[bot]", "reviewer[bot]")
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
BOTS = [{"__typename": "Bot", "login": "steward"},
        {"__typename": "Bot", "login": "steward[bot]"},
        {"type": "Bot", "login": "steward[bot]"}]
BAD = [None, {}, "steward", {"login": "steward"}, {"__typename": "User"},
       {"__typename": "Bot", "login": None}, {"__typename": "Organization", "login": "steward"},
       {"__typename": "User", "login": "steward[bot]"}, {"__typename": "Bot", "login": "x" * 100_000}]


@pytest.mark.parametrize("raw", BOTS + [{"is_bot": True, "login": "steward"}])
def test_api_representations_of_the_bot_match_bot_configuration_only(raw):
    account = parse_account(raw)
    assert account_eq(account, H.fix_bot)
    assert not account_eq(account, "steward")


@pytest.mark.parametrize("raw", BAD)
def test_absent_malformed_and_huge_authors_cannot_claim_an_account(raw):
    assert parse_account(raw) is None
    assert not account_eq(parse_issue({"number": 5, "author": raw}).author, H.fix_bot)


class IdentityServer(Server):
    def __call__(self, argv, token):
        if any("baseTip:ref" in a for a in argv):
            return json.dumps({"data": {"repository": {"baseTip": tip(job("smoke", "Smoke"))}}})
        if any("/actions/runs?" in a for a in argv):
            return json.dumps({"workflow_runs": [run_row("Smoke")]})
        if any("/branches/" in a and "/rules/" not in a for a in argv):
            return json.dumps({"protected": True, "protection": {"required_status_checks": {"contexts": ["smoke"]}}})
        if any('in:title "failing on dev"' in a for a in argv):
            return json.dumps({"data": {"search": {"issueCount": len(self.passes), "nodes": self.passes}}})
        return super().__call__(argv, token)


def observed(title, author):
    node = issue_node(5, title=title, author=author, labels=labels())
    server = IdentityServer(issues=(node,), passes=(node,))
    return GitHubGateway("o/r", runner=server).build_world("dev", filed_by=H.fix_bot, pickup=Pickup.OPEN)


@pytest.mark.parametrize("title", ["CI: Smoke failing on dev", "Architecture pass (first)", "Harness gap: defect"])
@pytest.mark.parametrize("author", [{"__typename": "User", "login": "steward"}, *BAD])
def test_open_pickup_never_gives_same_name_users_or_unknown_authors_issue_privileges(title, author):
    world = observed(title, author)
    assert derive(world, Local(now=NOW), H) == {issue_key(5): Dispatch(AgentKind.STEWARD)}
    assert not harness_gap(world.issues[0], H)
    actions = architecture_actions(world, H) + base_ci_actions(world.base, H.marker)
    assert not any(isinstance(a, Relabel) and a.number == 5 for a in actions)


@pytest.mark.parametrize("author", BOTS)
@pytest.mark.parametrize("title", ["CI: Smoke failing on dev", "Architecture pass (first)", "Harness gap: defect"])
def test_the_actual_bot_keeps_privileged_issue_behavior(title, author):
    world = observed(title, author)
    states = derive(world, Local(now=NOW), H)
    if title.startswith("Harness gap:"):
        assert states == {} and harness_gap(world.issues[0], H)
    else:
        assert states[issue_key(5)].act is AgentKind.RESOLVE
        actions = architecture_actions(world, H) + base_ci_actions(world.base, H.marker)
        assert any(isinstance(a, Relabel) and a.number == 5 for a in actions)


@pytest.mark.parametrize("kind,login,accepted", [("User", "reviewer", False), ("Bot", "reviewer", True),
                                                 ("Bot", "reviewer[bot]", True)])
def test_a_same_name_user_cannot_supply_the_review_bots_merge_verdict(kind, login, accepted):
    pr = parse_pr({"number": 7, "headRefOid": "head", "reviews": [
        {"author": {"__typename": kind, "login": login}, "state": "APPROVED", "commit": {"oid": "head"}}]})
    assert (bot_verdict_on_head(pr, H) == "APPROVED") is accepted


def test_a_user_pat_filer_is_still_recognized_as_a_user_account():
    world = observed("Architecture pass (first)", {"__typename": "User", "login": "xnmp-fixer"})
    harness = replace(H, fix_bot="xnmp-fixer")
    assert derive(world, Local(now=NOW), harness)[issue_key(5)].act is AgentKind.RESOLVE


@pytest.mark.parametrize("fix_login", ["xnmp-fixer", "xnmp"])
@pytest.mark.parametrize("target", ["issue", "pr"])
def test_342_a_user_pat_agents_mid_leg_comment_is_not_a_human_reply(target, fix_login):
    # A PAT belongs to a User, so typed attribution alone cannot disown the
    # agent's unmarked progress. Its parsed author must match the window's actor.
    harness = Harness(fix_login, "reviewer[bot]")
    capped = LegEnd(AgentKind.RESOLVE if target == "issue" else AgentKind.FIX_CI,
                    NOW + timedelta(minutes=10), True, started_at=NOW + timedelta(minutes=1))
    comments = [
        {"author": {"__typename": "User", "login": fix_login}, "body": harness.marker + " question",
         "createdAt": NOW.isoformat()},
        {"author": {"__typename": "User", "login": fix_login}, "body": "Progress report",
         "createdAt": (NOW + timedelta(minutes=5)).isoformat()},
    ]

    def observed_comments():
        view = parse_fix_issue(issue_node(), comments) if target == "issue" else parse_fix_pr(pr_node(), comments, [], [])
        return view.comments

    assert human_comments(observed_comments(), harness, (capped,)) == ()
    comments.append({"author": {"__typename": "User", "login": "xnmp"}, "body": "Please continue",
                     "createdAt": (NOW + timedelta(minutes=11)).isoformat()})
    assert [c.body for _, c in human_comments(observed_comments(), harness, (capped,))] == ["Please continue"]


@pytest.mark.parametrize("kind,login,accepted", [("User", "steward", False), ("Bot", "steward", True),
                                                 ("Bot", "steward[bot]", True)])
def test_marker_comments_keep_account_provenance_through_single_item_reads(kind, login, accepted):
    body = H.marker + " posted"
    node = issue_node(author={"__typename": "User", "login": "alice"})
    node["comments"] = {"totalCount": 1, "pageInfo": {"hasPreviousPage": False}, "nodes": [
        {"author": {"__typename": kind, "login": login}, "body": body, "createdAt": NOW.isoformat()}]}
    calls = []

    def run(argv, _token):
        calls.append(argv)
        return json.dumps({"data": {"repository": {"issue": node}}})

    gateway = GitHubGateway("o/r", runner=run)
    assert gateway.has_comment("issue", 5, body, H.fix_bot) is accepted
    issue = gateway.get_issue(5)
    assert H.wrote(issue.comments[0]) is accepted
    assert all("author{__typename login}" in a for call in calls for a in call if a.startswith("query="))


@pytest.mark.parametrize("author,accepted", [({"type": "User", "login": "reviewer"}, False),
                                            ({"type": "Bot", "login": "reviewer[bot]"}, True),
                                            ({"type": "Bot", "login": "reviewer"}, True),
                                            ({"login": "reviewer[bot]"}, False), (None, False)])
def test_rest_review_readback_cannot_accept_a_same_name_user_or_untyped_author(author, accepted):
    review = {"user": author, "commit_id": "head", "state": "APPROVED"}
    gateway = GitHubGateway("o/r", runner=lambda *_: json.dumps([[review]]))
    assert gateway.reviewed_on(7, "head", H.review_bot) is accepted


@pytest.mark.parametrize("kind,accepted", [("User", False), ("Bot", True)])
def test_base_ci_close_notes_require_the_filers_account_type(kind, accepted):
    from issue_agent.adapters.github_ci import base_issues

    node = {"number": 5, "title": "CI: Smoke failing on dev", "state": "CLOSED", "closedAt": NOW.isoformat(),
            "author": BOTS[0], "timelineItems": {"nodes": [
                {"__typename": "IssueComment", "author": {"__typename": kind, "login": "steward"}, "body": "note"},
                {"__typename": "ClosedEvent", "closer": None}]}}
    (issue,) = base_issues({"issueCount": 1, "nodes": [node]}, H.fix_bot)
    assert (issue.note == "note") is accepted


@pytest.mark.parametrize("login,expected", [("steward[bot]", "steward[bot]"), ("steward", "steward"),
                                           (None, ""), ("x" * 100_000, "")])
def test_concrete_commit_user_preserves_bot_type_from_its_full_login(login, expected):
    from issue_agent.adapters.github import parse_fix_pr
    from tests.adapters.test_github_fix_observe import pr_node

    node = pr_node()
    node["commits"]["nodes"][0]["commit"]["author"]["user"] = {"__typename": "User", "login": login}
    pr = parse_fix_pr(node, [], [], [])
    assert pr.pushes[0].actor == expected


@pytest.mark.parametrize("size,accepted", [(39, True), (40, False)])
@pytest.mark.parametrize("kind,suffix", [("User", ""), ("Bot", "[bot]")])
def test_account_login_length_boundaries(size, accepted, kind, suffix):
    raw = {"type": kind, "login": "a" * size + suffix}
    assert account_eq(parse_account(raw), "a" * size + suffix) is accepted
