"""The review read-back: only a complete read of every page can answer "not reviewed"."""

import json

import pytest

from issue_agent.adapters.github import GhUnavailable, GitHubGateway

HEAD = "a" * 40
BOT = "review-bot[bot]"


def row(review_id=1, state="CHANGES_REQUESTED", *, login=BOT, commit=HEAD):
    return {"id": review_id, "state": state, "user": {"login": login}, "commit_id": commit}


def gateway(answer, calls=None):
    def run(argv, _token):
        if calls is not None:
            calls.append(argv)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return GitHubGateway("o/r", runner=run)


@pytest.mark.parametrize("state", ["APPROVED", "CHANGES_REQUESTED", "approved"])
def test_a_standing_verdict_by_the_login_on_the_head_is_reviewed(state):
    assert gateway(json.dumps([[row(state=state)]])).reviewed_on(7, HEAD, BOT) is True


def test_every_page_is_read_and_the_login_compares_normalized():
    calls = []
    pages = [[row(1, login="someone")], [row(2, state="APPROVED", login="Review-Bot")]]

    assert gateway(json.dumps(pages), calls).reviewed_on(7, HEAD, BOT) is True
    assert "--paginate" in calls[0] and "--slurp" in calls[0]


@pytest.mark.parametrize(
    "review",
    [
        row(state="DISMISSED"),
        row(state="COMMENTED"),
        row(state="PENDING"),
        row(commit="b" * 40),
        row(login="someone-else"),
        {"id": 3, "state": "APPROVED", "user": None, "commit_id": HEAD},
        "not a review",
    ],
)
def test_anything_but_the_bots_standing_verdict_on_this_head_is_not_reviewed(review):
    assert gateway(json.dumps([[review]])).reviewed_on(7, HEAD, BOT) is False


def test_no_reviews_is_a_complete_answer_of_not_reviewed():
    assert gateway(json.dumps([[]])).reviewed_on(7, HEAD, BOT) is False


@pytest.mark.parametrize("answer", ["", json.dumps({"message": "Not Found"}), json.dumps([[row()], {"oops": 1}])])
def test_an_incomplete_or_refused_read_raises_instead_of_answering(answer):
    with pytest.raises(RuntimeError):
        gateway(answer).reviewed_on(7, HEAD, BOT)


def test_an_outage_propagates():
    with pytest.raises(GhUnavailable):
        gateway(GhUnavailable("server", "HTTP 503")).reviewed_on(7, HEAD, BOT)
