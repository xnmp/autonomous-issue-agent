from dataclasses import replace

import pytest

from issue_agent.app.dashweb import snapshots

from test_dashweb_ghdata import reader
from test_dashweb_snapshots import NOW, RESOLVE, row_for, ts


def ask(card):
    """What the board asks of the operator for this card, or None."""
    board = card["board"]
    return board["ask"] if board and board["status"] == "needs_you" else None


ESCALATED_ASKS = {"escalated", "rearm", "budget"}


def _pr_row():
    return row_for([
        RESOLVE,
        {"ts": ts(hours=1), "event": "agent_run", "role": "review",
         "kind": "REVIEW", "issue": 7, "number": 90, "status": "APPROVE"},
    ], 7)


def test_pr_query_collects_human_action_inputs():
    gh, calls = reader([("[]", "")])

    gh.prs("o/r")

    fields = calls[0][calls[0].index("--json") + 1]
    for field in (
        "reviewDecision",
        "statusCheckRollup",
        "autoMergeRequest",
        "latestReviews",
        "labels",
        "comments",
        "isDraft",
        "mergeable",
        "mergeStateStatus",
    ):
        assert field in fields


def test_approved_green_open_pr_without_automerge_is_merge_ready():
    card = snapshots.issue_card(
        _pr_row(),
        NOW,
        gh_prs={90: {
            "state": "OPEN",
            "reviewDecision": "APPROVED",
            "autoMergeRequest": None,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{
                "conclusion": "SUCCESS",
                "completedAt": ts(minutes=20),
            }],
            "latestReviews": [{
                "state": "APPROVED",
                "submittedAt": ts(minutes=30),
            }],
        }},
    )

    assert ask(card) == "merge"
    assert card["human_wait_s"] == 1_200
    assert card["board"]["since_ts"] == card["human_wait_since_ts"]


@pytest.mark.parametrize(
    "override",
    [
        {"statusCheckRollup": [{"conclusion": "FAILURE"}]},
        {"autoMergeRequest": {"enabledAt": "now"}},
        {"state": "MERGED", "mergedAt": ts()},
        {"isDraft": True},
        {"mergeable": "CONFLICTING"},
        {"mergeStateStatus": "DIRTY"},
        {"mergeStateStatus": "HAS_HOOKS"},
        {"mergeStateStatus": "UNSTABLE"},
    ],
)
def test_pr_that_still_has_harness_work_is_not_merge_ready(override):
    pr = {
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "autoMergeRequest": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        **override,
    }

    assert ask(snapshots.issue_card(_pr_row(), NOW, gh_prs={90: pr})) is None


def test_ai_blocked_question_is_awaiting_reply_until_a_later_comment():
    issue = {
        "labels": [{"name": "ai-blocked"}],
        "comments": [{
            "createdAt": ts(minutes=40),
            "body": "**Agent needs input:** choose A or B\n\n_Reply to unblock._",
            "author": {"login": "agent-bot"},
        }],
    }

    card = snapshots.issue_card(row_for([RESOLVE], 7), NOW, gh_issue=issue)
    assert ask(card) == "awaiting_reply"
    assert card["human_wait_s"] == 2_400

    issue["comments"].append({
        "createdAt": ts(minutes=20),
        "body": "Automated progress update.",
        "author": {"login": "other-app[bot]"},
    })
    assert ask(snapshots.issue_card(row_for([RESOLVE], 7), NOW, gh_issue=issue)) == "awaiting_reply"

    issue["comments"].append({
        "createdAt": ts(minutes=10),
        "body": "Choose A.",
        "author": {"login": "operator"},
    })
    assert ask(snapshots.issue_card(row_for([RESOLVE], 7), NOW, gh_issue=issue)) is None


def test_pr_thread_ai_blocked_question_is_awaiting_reply():
    card = snapshots.issue_card(
        _pr_row(),
        NOW,
        gh_prs={90: {
            "state": "OPEN",
            "labels": [{"name": "ai-blocked"}],
            "comments": [{
                "createdAt": ts(minutes=25),
                "body": (
                    "**Agent needs clarification on review:** choose a path\n\n"
                    "_Reply to unblock._"
                ),
                "author": {"login": "agent-bot"},
            }],
        }},
    )

    assert ask(card) == "awaiting_reply"
    assert card["human_wait_s"] == 1_500


def test_past_escalation_is_not_a_current_human_action_after_resume():
    row = row_for([
        RESOLVE,
        {"ts": ts(hours=2), "event": "escalation", "issue": 7},
        {"ts": ts(hours=1), "event": "action",
         "desc": "relabel issue #7: -needs-human-review +ai-working"},
    ], 7)

    assert ask(snapshots.issue_card(
        row,
        NOW,
        gh_issue={"labels": [{"name": "ai-working"}]},
    )) is None
    # A done issue has left the board altogether.
    assert snapshots.issue_card(
        replace(row, done=True),
        NOW,
        gh_issue={"labels": [{"name": "needs-human-review"}]},
    )["board"] is None


def test_readded_external_escalation_does_not_reuse_cleared_wait_start():
    row = row_for([
        RESOLVE,
        {"ts": ts(hours=2), "event": "escalation", "issue": 7},
        {"ts": ts(hours=1), "event": "action",
         "desc": "relabel issue #7: -needs-human-review +ai-working"},
    ], 7)

    card = snapshots.issue_card(
        row,
        NOW,
        gh_issue={
            "labels": [{"name": "needs-human-review"}],
            "updatedAt": ts(minutes=10),
        },
    )

    assert ask(card) in ESCALATED_ASKS
    assert card["human_wait_s"] == 600


def test_malformed_check_rollup_is_not_merge_ready():
    pr = {
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "autoMergeRequest": None,
        "statusCheckRollup": ["invalid"],
    }

    assert ask(snapshots.issue_card(_pr_row(), NOW, gh_prs={90: pr})) is None


def test_malformed_comment_author_does_not_break_human_action_projection():
    issue = {
        "labels": [{"name": "ai-blocked"}],
        "comments": [{
            "createdAt": ts(minutes=10),
            "body": "**Agent needs input:** choose A\n\n_Reply to unblock._",
            "author": ["invalid"],
        }],
    }

    assert ask(snapshots.issue_card(row_for([RESOLVE], 7), NOW, gh_issue=issue)) == "awaiting_reply"


@pytest.mark.parametrize("kind", ["ADDRESS_REVIEW", "REVIEW"])
def test_cleanly_blocked_leg_asks_for_a_reply_and_is_not_a_successful_step(kind):
    blocked = {
        "ts": ts(minutes=20),
        "event": "agent_run",
        "role": "fix",
        "kind": kind,
        "number": 90,
        "issue": 7,
        "status": "blocked",
        "exit_reason": "ok",
    }

    relabel = {"ts": ts(minutes=19), "event": "action", "issue": 7,
               "desc": "relabel issue #7: +ai-blocked"}

    card = snapshots.issue_card(row_for([RESOLVE, blocked, relabel], 7), NOW)

    assert ask(card) == "awaiting_reply"
    assert card["steps"][-1]["state"] == "blocked"
    assert card["rail"]["steps"] >= 2
