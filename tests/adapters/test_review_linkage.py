
from tests.domain.builders import observed_pr
from dataclasses import replace
import json
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.github import GitHubGateway
from issue_agent.adapters.review_effects import read_pull_request
from issue_agent.adapters.review_linkage import read_review_linked_issue, review_linked_issue_holds
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, persist_workflow_snapshot, workflow_scope
from issue_agent.domain.labels import Label
from issue_agent.domain.review_linkage import LinkedIssueUnavailable, NoLinkedIssue, ObservedLinkedIssue
from issue_agent.domain.review_workflow import ObservedPullRequest, PullRequestUnavailable
from issue_agent.domain.workflow import NoAuthorizedRemediation, OpenGate, ResolveCeiling
from issue_agent.domain.world import IssueView, PRView
from tests.adapters.test_workflow_admission import request
from tests.domain.test_review_workflow_policy import fresh
from tests.fakes.review_workflow import HEAD, ISSUE_KEY, NOW, PR_KEY
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


class IssueGateway:
    def __init__(self, issue=IssueView(7), *, dependencies=()):
        self.issue = issue
        self.dependencies = dependencies
        self.reads = []

    def get_issue(self, number):
        self.reads.append(number)
        if isinstance(self.issue, Exception):
            raise self.issue
        return self.issue

    def open_blockers(self, number):
        if isinstance(self.dependencies, Exception):
            raise self.dependencies
        return self.dependencies


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    return store


def held_issue():
    state = resolve_snapshot(7)
    return replace(state, episode=replace(state.episode, failures=3), gates=(OpenGate(
        "resolve:7:ceiling", state.revision.fence.generation,
        ResolveCeiling(state.episode.episode_id, 3, 3), NoAuthorizedRemediation("operator decision required"),
    ),))


def test_explicit_no_link_does_not_read_an_issue_or_initialize_authority(control):
    gateway = IssueGateway(ConnectionError("must not read"))
    with control._connect() as db:
        before = tuple(db.iterdump())
    assert isinstance(read_review_linked_issue(control, gateway, PR_KEY, observed_pr(9)), NoLinkedIssue)
    assert gateway.reads == []
    with control._connect() as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("view", [observed_pr(9, closing_issue=7), observed_pr(9, head_ref_name="fix/7-linked")])
def test_closing_reference_and_branch_fallback_observe_known_issue(control, view):
    gateway = IssueGateway()
    assert isinstance(read_review_linked_issue(control, gateway, PR_KEY, view), ObservedLinkedIssue)
    assert gateway.reads == [7]
    assert not review_linked_issue_holds(control, gateway, PR_KEY, view)


def test_closing_reference_takes_precedence_over_branch(control):
    gateway = IssueGateway(IssueView(8))
    assert not review_linked_issue_holds(control, gateway, PR_KEY, observed_pr(9, closing_issue=8, head_ref_name="fix/7-linked"))
    assert gateway.reads == [8]


@pytest.mark.parametrize("issue", [ConnectionError("offline"), None, IssueView(8), IssueView(7, state="unknown")])
def test_failed_or_mismatched_known_issue_is_unavailable(control, issue):
    result = read_review_linked_issue(control, IssueGateway(issue), PR_KEY, observed_pr(9, closing_issue=7))
    assert isinstance(result, LinkedIssueUnavailable)


@pytest.mark.parametrize("view,key", [(None, PR_KEY), (observed_pr(10), PR_KEY), (observed_pr(9), ISSUE_KEY),
                                     (observed_pr(9, closing_issue=True), PR_KEY), (observed_pr(9, closing_issue=0), PR_KEY)])
def test_malformed_or_mismatched_pr_linkage_is_unavailable(control, view, key):
    assert isinstance(read_review_linked_issue(control, IssueGateway(), key, view), LinkedIssueUnavailable)


@pytest.mark.parametrize("dependencies", [ConnectionError("dependency read failed"), None, [True], [0], ["8"]])
def test_unreadable_dependencies_never_clear_a_known_issue(control, dependencies):
    result = read_review_linked_issue(control, IssueGateway(dependencies=dependencies), PR_KEY, observed_pr(9, closing_issue=7))
    assert isinstance(result, LinkedIssueUnavailable)


@pytest.mark.parametrize("dependencies", ["", "null", "[]", '["malformed"]', '[[{"number":8}]]'])
def test_real_gateway_soft_dependency_failures_never_prove_clear_linkage(control, dependencies):
    def runner(argv, *_):
        if argv[1:3] == ["issue", "view"]:
            return json.dumps({"number": 7, "state": "OPEN", "labels": []})
        return dependencies
    gateway = GitHubGateway("o/r", runner=runner)
    assert isinstance(read_review_linked_issue(control, gateway, PR_KEY, observed_pr(9, closing_issue=7)),
                      LinkedIssueUnavailable)


@pytest.mark.parametrize("payload", [{"number": 7}, {"number": 7, "state": None, "labels": []},
                                    {"number": 7, "state": "OPEN"},
                                    {"number": 7, "state": "OPEN", "labels": "bad"}])
def test_real_gateway_incomplete_issue_response_never_proves_clear_linkage(control, payload):
    def runner(argv, *_):
        return json.dumps(payload) if argv[1:3] == ["issue", "view"] else "[[]]"
    gateway = GitHubGateway("o/r", runner=runner)
    assert isinstance(read_review_linked_issue(control, gateway, PR_KEY, observed_pr(9, closing_issue=7)),
                      LinkedIssueUnavailable)


@pytest.mark.parametrize("dependencies,blocked", [("[[]]", False),
    ('[[{"number":8,"state":"closed"}]]', False),
    ('[[{"number":8,"state":"closed"}],[{"number":10,"state":"open"}]]', True),
])
def test_real_gateway_complete_dependency_pages_decide_review_hold(control, dependencies, blocked):
    def runner(argv, *_):
        if argv[1:3] == ["issue", "view"]:
            return json.dumps({"number": 7, "state": "OPEN", "labels": []})
        return dependencies
    holds = review_linked_issue_holds(control, GitHubGateway("o/r", runner=runner), PR_KEY, observed_pr(9, closing_issue=7))
    assert bool(holds) is blocked
    if blocked:
        assert any("open dependency" in hold for hold in holds)


@pytest.mark.parametrize("field,value", [
    ("closingIssuesReferences", None), ("closingIssuesReferences", {}),
    ("closingIssuesReferences", [{}]), ("closingIssuesReferences", [{"number": True}]),
    ("closingIssuesReferences", [{"number": 0}]), ("closingIssuesReferences", [{"number": "7"}]),
    ("headRefName", None), ("headRefName", 7),
])
def test_malformed_raw_pr_linkage_is_unavailable(field, value):
    payload = {"number": 9, "headRefOid": HEAD, "state": "OPEN", "labels": [],
               "headRefName": "topic", "closingIssuesReferences": []}
    payload[field] = value
    gateway = GitHubGateway("o/r", runner=lambda *_: json.dumps(payload))
    observed, _ = read_pull_request(gateway, PR_KEY, NOW, reviews=False)
    assert isinstance(observed, PullRequestUnavailable)


@pytest.mark.parametrize("field", ["closingIssuesReferences", "headRefName"])
def test_missing_raw_pr_linkage_field_does_not_prove_absence(field):
    payload = {"number": 9, "headRefOid": HEAD, "state": "OPEN", "labels": [],
               "headRefName": "topic", "closingIssuesReferences": []}
    del payload[field]
    gateway = GitHubGateway("o/r", runner=lambda *_: json.dumps(payload))
    observed, _ = read_pull_request(gateway, PR_KEY, NOW, reviews=False)
    assert isinstance(observed, PullRequestUnavailable)


@pytest.mark.parametrize("branch,linked", [("topic", False), ("fix/7-linked", True)])
def test_explicit_empty_raw_refs_use_complete_branch_evidence(control, branch, linked):
    reads = []

    def runner(argv, *_):
        reads.append(argv)
        if argv[1:3] == ["pr", "view"]:
            return json.dumps({"number": 9, "headRefOid": HEAD, "state": "OPEN", "labels": [],
                               "headRefName": branch, "closingIssuesReferences": []})
        if argv[1:3] == ["issue", "view"]:
            return json.dumps({"number": 7, "state": "OPEN", "labels": []})
        return "[[]]"

    gateway = GitHubGateway("o/r", runner=runner)
    observed, view = read_pull_request(gateway, PR_KEY, NOW, reviews=False)
    assert isinstance(observed, ObservedPullRequest)
    read = read_review_linked_issue(control, gateway, PR_KEY, view)
    assert isinstance(read, ObservedLinkedIssue if linked else NoLinkedIssue)
    assert bool([call for call in reads if call[1:3] == ["issue", "view"]]) is linked


@pytest.mark.parametrize("issue,dependencies,reason", [
    (IssueView(7, labels=frozenset({Label.AI_PARKED})), (), "ai-parked"),
    (IssueView(7, state="closed"), (), "closed"),
    (IssueView(7), [8], "open dependency"),
])
def test_current_remote_holds_block_review(control, issue, dependencies, reason):
    holds = review_linked_issue_holds(control, IssueGateway(issue, dependencies=dependencies), PR_KEY, observed_pr(9, closing_issue=7))
    assert any(reason in hold and "#7" in hold for hold in holds)


def test_current_sql_gate_holds_review_even_after_remote_label_is_cleared(control):
    install_workflow(control, held_issue())
    with control._connect() as db:
        before = tuple(db.iterdump())
    holds = review_linked_issue_holds(control, IssueGateway(), PR_KEY, observed_pr(9, closing_issue=7))
    assert any("#7" in hold and "workflow gates" in hold for hold in holds)
    with control._connect() as db:
        assert tuple(db.iterdump()) == before


def test_pending_issue_rearm_is_preserved_as_a_review_hold_with_clear_remote_labels(control):
    from issue_agent.adapters.workflow_commit import insert_pending_rearm
    from tests.adapters.test_workflow_control import request as rearm_request

    state = held_issue()
    install_workflow(control, state)
    change = rearm_request(state)
    with control.transaction() as db:
        persist_workflow_snapshot(db, change.snapshot)
        insert_pending_rearm(db, workflow_scope(change.snapshot.revision.fence.key), change.snapshot.intent, change.audit)
    holds = review_linked_issue_holds(control, IssueGateway(), PR_KEY, observed_pr(9, closing_issue=7))
    assert any("pending workflow re-arm" in hold and "#7" in hold for hold in holds)


def test_retained_issue_authority_without_activation_is_local_corruption(control):
    install_workflow(control, held_issue())
    with control.transaction() as db:
        db.execute("DELETE FROM workflow_authority_epochs")
    with pytest.raises(WorkflowAuthorityError, match="retained"):
        read_review_linked_issue(control, IssueGateway(), PR_KEY, observed_pr(9, closing_issue=7))


def test_local_sql_error_propagates_without_becoming_remote_evidence(control):
    with control.transaction() as db:
        db.execute("DROP TABLE workflow_authority_epochs")
    with pytest.raises(sqlite3.Error):
        read_review_linked_issue(control, IssueGateway(), PR_KEY, observed_pr(9, closing_issue=7))


def review_reservation(issue=7):
    base = request("review-linked", binding=fresh().binding, stage="REVIEW")
    metadata = json.loads(base.dispatch_id)
    metadata.update(role="review", number=9, issue=issue)
    return replace(base, dispatch_id=json.dumps(metadata), owner_id="o/r:review:test")


def test_issue_gate_arriving_after_review_plan_refuses_paid_reservation(control):
    install_workflow(control, fresh())
    install_workflow(control, resolve_snapshot(7))
    planned = review_reservation()
    assert not review_linked_issue_holds(control, IssueGateway(), PR_KEY, observed_pr(9, closing_issue=7))
    with control.transaction() as db:
        persist_workflow_snapshot(db, held_issue())
    assert not control.reserve(planned).admitted
    assert control.active_reservation_runs() == ()


def test_clear_linked_issue_allows_paid_review_reservation(control):
    install_workflow(control, fresh())
    assert control.reserve(review_reservation()).admitted


@pytest.mark.parametrize("issue", [True, 0, -1, "7", 2**63])
def test_malformed_linked_issue_cannot_buy_paid_review(control, issue):
    install_workflow(control, fresh())
    assert not control.reserve(review_reservation(issue)).admitted
    assert control.active_reservation_runs() == ()
