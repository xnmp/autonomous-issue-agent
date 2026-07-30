from dataclasses import replace

import pytest

from issue_agent.app.dashweb import snapshots

from test_dashweb_ghdata import reader
from test_dashweb_snapshots import NOW, RESOLVE, row_for, ts


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

    assert card["human_action"] == "merge-ready"
    assert card["human_wait_s"] == 1_200
    assert "merge-ready" in card["flags"]


@pytest.mark.parametrize(
    "override",
    [
        {"statusCheckRollup": [{"conclusion": "FAILURE"}]},
        {"autoMergeRequest": {"enabledAt": "now"}},
        {"state": "MERGED", "mergedAt": ts()},
        {"isDraft": True},
        {"mergeable": "CONFLICTING"},
        {"mergeStateStatus": "DIRTY"},
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

    assert snapshots.issue_card(
        _pr_row(), NOW, gh_prs={90: pr}
    )["human_action"] is None


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
    assert card["human_action"] == "awaiting-reply"
    assert card["human_wait_s"] == 2_400

    issue["comments"].append({
        "createdAt": ts(minutes=20),
        "body": "Automated progress update.",
        "author": {"login": "other-app[bot]"},
    })
    assert snapshots.issue_card(
        row_for([RESOLVE], 7), NOW, gh_issue=issue
    )["human_action"] == "awaiting-reply"

    issue["comments"].append({
        "createdAt": ts(minutes=10),
        "body": "Choose A.",
        "author": {"login": "operator"},
    })
    assert snapshots.issue_card(
        row_for([RESOLVE], 7), NOW, gh_issue=issue
    )["human_action"] is None


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

    assert card["human_action"] == "awaiting-reply"
    assert card["human_wait_s"] == 1_500


def test_past_escalation_is_not_a_current_human_action_after_resume():
    row = row_for([
        RESOLVE,
        {"ts": ts(hours=2), "event": "escalation", "issue": 7},
        {"ts": ts(hours=1), "event": "action",
         "desc": "relabel issue #7: -needs-human-review +ai-working"},
    ], 7)

    assert snapshots.issue_card(
        row,
        NOW,
        gh_issue={"labels": [{"name": "ai-working"}]},
    )["human_action"] is None
    assert snapshots.issue_card(
        replace(row, done=True),
        NOW,
        gh_issue={"labels": [{"name": "needs-human-review"}]},
    )["human_action"] is None


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

    assert card["human_action"] == "escalated"
    assert card["human_wait_s"] == 600


def test_malformed_check_rollup_is_not_merge_ready():
    pr = {
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "autoMergeRequest": None,
        "statusCheckRollup": ["invalid"],
    }

    assert snapshots.issue_card(
        _pr_row(), NOW, gh_prs={90: pr}
    )["human_action"] is None


def test_malformed_comment_author_does_not_break_human_action_projection():
    issue = {
        "labels": [{"name": "ai-blocked"}],
        "comments": [{
            "createdAt": ts(minutes=10),
            "body": "**Agent needs input:** choose A\n\n_Reply to unblock._",
            "author": ["invalid"],
        }],
    }

    assert snapshots.issue_card(
        row_for([RESOLVE], 7), NOW, gh_issue=issue
    )["human_action"] == "awaiting-reply"


def test_cleanly_blocked_leg_flags_the_row_and_is_not_a_successful_step():
    blocked = {
        "ts": ts(minutes=20),
        "event": "agent_run",
        "role": "fix",
        "kind": "ADDRESS_REVIEW",
        "number": 90,
        "issue": 7,
        "status": "blocked",
        "exit_reason": "ok",
    }

    card = snapshots.issue_card(row_for([RESOLVE, blocked], 7), NOW)

    assert "blocked" in card["flags"]
    assert card["steps"][-1]["state"] == "blocked"
