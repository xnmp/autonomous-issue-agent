"""The review read-back: only a complete read of every page can answer "not reviewed"."""

import json

import pytest

from issue_agent.adapters.github import GhUnavailable, GitHubGateway

HEAD = "a" * 40
BOT = "review-bot[bot]"


def row(review_id=1, state="CHANGES_REQUESTED", *, login=BOT, commit=HEAD, submitted="2026-10-15T15:32:20Z"):
    return {"id": review_id, "state": state, "user": {"type": "Bot" if login.lower().endswith("[bot]") else "User", "login": login}, "commit_id": commit,
            "submitted_at": submitted}


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
    pages = [[row(1, login="someone")], [row(2, state="APPROVED", login="Review-Bot[bot]")]]

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


EDIT = "2026-10-15T16:04:27Z"  # tauri #1182: the body edit after the 15:32:20Z review


def test_472_a_verdict_older_than_the_body_edit_the_review_judged_has_not_landed():
    assert gateway(json.dumps([[row()]])).reviewed_on(7, HEAD, BOT, since=EDIT) is False


@pytest.mark.parametrize("submitted", [EDIT, "2026-10-15T16:20:00Z", "", None, "garbage"])
def test_472_a_verdict_no_older_than_the_edit_or_untimed_has_landed(submitted):
    assert gateway(json.dumps([[row(submitted=submitted)]])).reviewed_on(7, HEAD, BOT, since=EDIT) is True


@pytest.mark.parametrize("since", ["", "garbage", "9" * 100_000])
def test_472_an_absent_or_malformed_edit_time_is_the_head_only_read_back(since):
    assert gateway(json.dumps([[row()]])).reviewed_on(7, HEAD, BOT, since=since) is True
