"""Complete open-PR linkage is reread before offline counter attribution."""

from dataclasses import replace
import json

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.github import GitHubGateway
from issue_agent.adapters.review_execution_counter_links import (
    read_complete_legacy_open_counter_links, revalidate_complete_legacy_open_counter_links,
)
from issue_agent.domain.review_execution_counter_mapping import (
    BlockedLegacyOpenCounterLinks, CompleteLegacyOpenCounterLinks,
)
from issue_agent.domain.world import IssueView, PRView

HEAD = "a" * 40


class LinkGateway:
    def __init__(self, views, *, issues=None, listings=None):
        self.repo = "o/r"
        self.views = views
        self.issues = issues or {}
        self.listings = listings
        self.list_calls = 0
        self.reads = []

    def _gh_json(self, args):
        assert args[:3] == ["api", "--paginate", "--slurp"]
        numbers = self.listings[min(self.list_calls, len(self.listings) - 1)] if self.listings else tuple(self.views)
        self.list_calls += 1
        return [[{"number": number, "state": "open"} for number in numbers]]

    def get_review_pull_request(self, number):
        self.reads.append(("pr", number))
        value = self.views[number]
        if isinstance(value, Exception):
            raise value
        return value

    def get_review_linked_issue(self, number):
        self.reads.append(("issue", number))
        value = self.issues[number]
        if isinstance(value, Exception):
            raise value
        return value

    def review_issue_blockers(self, number):
        self.reads.append(("blockers", number))
        return ()


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    return store


def _gateway():
    return LinkGateway({
        9: PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD),
        10: PRView(10, head_ref_name="work/topic", head_ref_oid=HEAD),
    }, issues={7: IssueView(7)})


def test_complete_counter_links_read_each_open_pr_and_revalidate_without_writes(control):
    gateway = _gateway()
    with control._connect() as db:
        before = tuple(db.iterdump())

    observed = read_complete_legacy_open_counter_links(control, gateway, "o/r")
    assert isinstance(observed, CompleteLegacyOpenCounterLinks)
    current = revalidate_complete_legacy_open_counter_links(control, gateway, "o/r", observed)

    assert current == observed
    assert [(link.pr, link.linked_issue) for link in observed.links] == [(9, 7), (10, None)]
    assert gateway.reads == [
        ("pr", 9), ("issue", 7), ("blockers", 7), ("pr", 10),
        ("pr", 9), ("issue", 7), ("blockers", 7), ("pr", 10),
    ]
    with control._connect() as db:
        assert tuple(db.iterdump()) == before


def test_counter_link_reader_blocks_changed_listing_during_read(control):
    gateway = _gateway()
    gateway.listings = [(9,), (9, 10)]

    result = read_complete_legacy_open_counter_links(control, gateway, "o/r")

    assert isinstance(result, BlockedLegacyOpenCounterLinks)
    assert "changed" in result.reason


@pytest.mark.parametrize("change", ("new_pr", "head", "issue", "closed"))
def test_counter_link_revalidation_blocks_changed_or_unavailable_sources(control, change):
    gateway = _gateway()
    observed = read_complete_legacy_open_counter_links(control, gateway, "o/r")
    assert isinstance(observed, CompleteLegacyOpenCounterLinks)

    if change == "new_pr":
        gateway.views[11] = PRView(11, head_ref_name="work/another", head_ref_oid=HEAD)
    elif change == "head":
        gateway.views[9] = replace(gateway.views[9], head_ref_oid="b" * 40)
    elif change == "issue":
        gateway.issues[7] = IssueView(8)
    else:
        gateway.views[9] = replace(gateway.views[9], state="CLOSED")

    assert isinstance(revalidate_complete_legacy_open_counter_links(control, gateway, "o/r", observed),
                      BlockedLegacyOpenCounterLinks)


@pytest.mark.parametrize("failure", (ConnectionError("offline"), None, IssueView(8)))
def test_counter_link_reader_blocks_missing_or_mismatched_linked_issue(control, failure):
    gateway = _gateway()
    gateway.issues[7] = failure
    assert isinstance(read_complete_legacy_open_counter_links(control, gateway, "o/r"),
                      BlockedLegacyOpenCounterLinks)


def test_counter_link_reader_refuses_untyped_observation_and_missing_complete_reader(control):
    gateway = _gateway()
    observed = read_complete_legacy_open_counter_links(control, gateway, "o/r")
    assert isinstance(observed, CompleteLegacyOpenCounterLinks)
    assert isinstance(revalidate_complete_legacy_open_counter_links(control, gateway, "o/r", None),
                      BlockedLegacyOpenCounterLinks)
    gateway.get_review_pull_request = None
    assert isinstance(read_complete_legacy_open_counter_links(control, gateway, "o/r"),
                      BlockedLegacyOpenCounterLinks)


def test_counter_link_reader_refuses_gateway_for_another_repository(control):
    gateway = _gateway()
    gateway.repo = "another/repo"

    result = read_complete_legacy_open_counter_links(control, gateway, "o/r")

    assert isinstance(result, BlockedLegacyOpenCounterLinks)
    assert "another repository" in result.reason
    assert gateway.list_calls == 0


def test_counter_link_reader_uses_real_gateway_complete_pr_and_issue_paths(control):
    calls = []

    def runner(argv, *_):
        calls.append(argv)
        if argv[1:3] == ["pr", "view"]:
            return json.dumps({"number": 9, "headRefName": "fix/7-work", "headRefOid": HEAD,
                               "state": "OPEN", "labels": [], "closingIssuesReferences": []})
        if argv[1:3] == ["issue", "view"]:
            return json.dumps({"number": 7, "state": "OPEN", "labels": []})
        if "pulls?state=open" in argv[-1]:
            return '[[{"number":9,"state":"open"}]]'
        return "[[]]"

    result = read_complete_legacy_open_counter_links(control, GitHubGateway("o/r", runner=runner), "o/r")

    assert isinstance(result, CompleteLegacyOpenCounterLinks)
    assert [(link.pr, link.linked_issue) for link in result.links] == [(9, 7)]
    assert sum("pulls?state=open" in call[-1] for call in calls) == 2
    assert any(call[1:3] == ["issue", "view"] for call in calls)
