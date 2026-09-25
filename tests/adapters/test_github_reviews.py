"""Gateway review reads and dismissal: complete-or-partial reads, typed refusals (ADR 0015)."""

import json

import pytest

from issue_agent.adapters.github import GhUnavailable, GitHubGateway, parse_review_pages
from issue_agent.domain.review_workflow import CompleteReviews, PartialReviews, SubmittedReviewState

HEAD = "a" * 40
BOT = "review-bot[bot]"


def row(review_id=1, state="CHANGES_REQUESTED", *, login=BOT, commit=HEAD, at="2026-09-25T11:00:00Z", **extra):
    value = {"id": review_id, "state": state, "user": {"login": login}, "commit_id": commit, "submitted_at": at}
    value.update(extra)
    return value


def runner_returning(answer, calls=None):
    def run(argv, _token):
        if calls is not None:
            calls.append(argv)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return run


def test_every_page_contributes_to_one_chronological_complete_read():
    pages = [[row(1, at="2026-09-25T11:00:00Z"), row(2, "APPROVED", at="2026-09-25T11:00:02Z")],
             [row(3, "DISMISSED", at="2026-09-25T11:00:01Z")]]
    read = parse_review_pages(pages)
    assert isinstance(read, CompleteReviews)
    assert [fact.review_id for fact in read.reviews] == ["1", "3", "2"]
    assert read.reviews[1].state is SubmittedReviewState.DISMISSED


def test_pending_drafts_are_skipped_and_never_make_a_read_partial():
    read = parse_review_pages([[row(1), {"id": 2, "state": "PENDING", "user": {"login": BOT}}]])
    assert isinstance(read, CompleteReviews) and [fact.review_id for fact in read.reviews] == ["1"]


def test_an_unresolved_commit_maps_to_none_at_the_boundary():
    read = parse_review_pages([[row(1, commit=""), row(2, commit=None, at="2026-09-25T11:00:01Z")]])
    assert isinstance(read, CompleteReviews) and [fact.commit_id for fact in read.reviews] == [None, None]


def test_a_deleted_account_is_the_ghost_author():
    read = parse_review_pages([[row(1, user=None)]])
    assert isinstance(read, CompleteReviews) and read.reviews[0].author == "ghost"


def test_no_reviews_is_a_complete_empty_read():
    assert parse_review_pages([[]]) == CompleteReviews(())
    assert parse_review_pages([]) == CompleteReviews(())


@pytest.mark.parametrize("pages", [
    None, "", {"id": 1}, [row(1)], [[row(1)], {"message": "Server Error"}], [["not-a-review"]],
    [[row(1, "EXOTIC")]], [[row("1")]], [[row(0)]], [[row(True)]], [[row(1, commit="short")]],
    [[row(1, at=None)]], [[row(1, at="yesterday")]], [[row(1, at="2026-09-25T11:00:00")]],
    [[row(1)], [row(1, at="2026-09-25T11:00:01Z")]],
], ids=["absent", "text", "object", "flat", "error-page", "entry", "state", "string-id", "zero-id", "bool-id",
        "commit", "no-time", "bad-time", "naive-time", "duplicate-across-pages"])
def test_anything_short_of_every_well_formed_page_is_partial(pages):
    assert isinstance(parse_review_pages(pages), PartialReviews)


def test_more_reviews_than_one_bounded_observation_is_partial_not_truncated():
    many = [[row(index, at=f"2026-09-25T11:{index // 60:02d}:{index % 60:02d}Z") for index in range(1, 514)]]
    read = parse_review_pages(many)
    assert isinstance(read, PartialReviews) and "512" in read.reason
    assert isinstance(parse_review_pages([many[0][:512]]), CompleteReviews)


def test_the_gateway_reads_every_page_of_the_rest_reviews_list():
    calls = []
    gateway = GitHubGateway("o/r", runner=runner_returning(json.dumps([[row(7)]]), calls))
    read = gateway.pull_request_reviews(9)
    assert isinstance(read, CompleteReviews) and read.reviews[0].review_id == "7"
    assert calls == [["gh", "api", "--paginate", "--slurp", "repos/o/r/pulls/9/reviews?per_page=100"]]


@pytest.mark.parametrize("answer", [
    GhUnavailable("server", "HTTP 502 on page 2"), GhUnavailable("network", "connection reset"),
    "", "[[{\"id\": 1", "not json",
], ids=["5xx", "transport", "refused", "truncated", "garbage"])
def test_a_failed_or_truncated_review_read_is_partial(answer):
    read = GitHubGateway("o/r", runner=runner_returning(answer)).pull_request_reviews(9)
    assert isinstance(read, PartialReviews)


def test_dismissal_is_one_put_as_the_gateways_own_identity():
    calls = []
    gateway = GitHubGateway("o/r", runner=runner_returning(json.dumps(row(7, "DISMISSED")), calls))
    assert gateway.dismiss_review(9, "7", "re-armed") is True
    assert calls == [["gh", "api", "--method", "PUT", "repos/o/r/pulls/9/reviews/7/dismissals",
                      "-f", "message=re-armed", "-f", "event=DISMISS"]]


def test_a_permission_or_protection_refusal_is_a_false_answer_not_a_retry():
    # The runner degrades a non-infra 403/422 to "", as it does for every
    # ordinary API answer; the gateway reports it as GitHub's refusal.
    assert GitHubGateway("o/r", runner=runner_returning("")).dismiss_review(9, "7", "m") is False


@pytest.mark.parametrize("failure", [GhUnavailable("server", "HTTP 503"), GhUnavailable("network", "timed out")])
def test_infra_failures_during_dismissal_are_unknown_outcomes(failure):
    with pytest.raises(GhUnavailable):
        GitHubGateway("o/r", runner=runner_returning(failure)).dismiss_review(9, "7", "m")


@pytest.mark.parametrize("pr, review_id", [(0, "7"), (-1, "7"), (9, ""), (9, "../7"), (9, "7/dismissals")])
def test_malformed_dismissal_targets_are_rejected_before_any_call(pr, review_id):
    calls = []
    with pytest.raises(ValueError):
        GitHubGateway("o/r", runner=runner_returning("{}", calls)).dismiss_review(pr, review_id, "m")
    assert calls == []


# -- has_review: only a complete answer can say "absent" ---------------------------------------

@pytest.mark.parametrize("answer", ["", "{}", json.dumps({"message": "Not Found"}), json.dumps([[], {"message": "x"}])])
def test_an_unanswered_review_read_raises_instead_of_answering_absent(answer):
    """Regression: a refused (403/404 → "") or malformed read answered False and reconciliation re-submitted."""
    from issue_agent.adapters.github import ReviewReadUnavailable
    gateway = GitHubGateway("o/r", runner=runner_returning(answer))
    with pytest.raises(ReviewReadUnavailable):
        gateway.has_review(9, "APPROVE", "lgtm", HEAD)


def test_a_complete_review_read_answers_presence_and_absence():
    pages = json.dumps([[{"state": "APPROVED", "body": "lgtm", "commit_id": HEAD}], []])
    gateway = GitHubGateway("o/r", runner=runner_returning(pages))
    assert gateway.has_review(9, "APPROVE", "lgtm", HEAD)
    assert not gateway.has_review(9, "REQUEST_CHANGES", "lgtm", HEAD)
    assert not GitHubGateway("o/r", runner=runner_returning("[[]]")).has_review(9, "APPROVE", "lgtm", HEAD)


def test_refused_review_writes_and_reads_have_their_soft_failure_shape():
    """The shapes the persistent fake's `soft_failures` mirror."""
    gateway = GitHubGateway("o/r", runner=runner_returning(""))
    assert isinstance(gateway.pull_request_reviews(9), PartialReviews)
    assert gateway.submit_review(9, "APPROVE", "lgtm", HEAD) is False
    assert gateway.dismiss_review(9, "1", "stale") is False
    with pytest.raises(AttributeError):
        gateway.get_pr(9, mergeability=False, checks=False)


def test_the_review_body_yields_its_command_marker_and_nothing_else():
    from issue_agent.domain.review_workflow import review_command_marker
    command = "c" * 64
    read = parse_review_pages([[
        row(1, body=f"verdict\n\n{review_command_marker(command)}"),
        row(2, body=None, at="2026-09-25T11:00:01Z"),
        row(3, body=17, at="2026-09-25T11:00:02Z"),
        row(4, body=f"{review_command_marker(command)} and more", at="2026-09-25T11:00:03Z"),
        row(5, at="2026-09-25T11:00:04Z"),
    ]])
    assert isinstance(read, CompleteReviews)
    assert [fact.review_command for fact in read.reviews] == [command, None, None, None, None]
