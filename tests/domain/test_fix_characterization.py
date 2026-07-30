"""Characterization tests for durable fix-workflow behavior.

These sit above the existing detailed regression tests. They intentionally check
workflow outcomes without pinning the entire ordered action list, so the fix
workflow can be extracted/refactored while preserving behavior.
"""

from tests.domain.builders import Check, Comment, Review, issue, pr, world

from issue_agent.domain.actions import (
    AgentKind,
    CloseIssue,
    EnableAutoMerge,
    Relabel,
    RemoveWorktree,
    RunAgent,
    Target,
)
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label


CFG = FixConfig(fix_bot="fix-bot")


def _runs(actions, kind: AgentKind, number: int) -> bool:
    return RunAgent(kind, number) in actions


def _relabels(actions, target: Target, number: int, remove, add) -> bool:
    return Relabel(target, number, remove, add) in actions


def _closes_issue(actions, number: int) -> bool:
    return any(isinstance(action, CloseIssue) and action.number == number for action in actions)


def test_ready_issue_dispatch_is_single_fixer_and_marks_working():
    actions = decide_fix(
        world(issues=[issue(2, "ai-ready", "P2"), issue(1, "ai-ready", "P1")]),
        CFG,
        FixState(),
        fixer_busy=False,
    )

    assert _relabels(actions, Target.ISSUE, 1, Label.AI_READY, Label.AI_WORKING)
    assert _runs(actions, AgentKind.RESOLVE, 1)
    assert not _runs(actions, AgentKind.RESOLVE, 2)


def test_ready_issue_is_held_when_known_paths_overlap_open_pr():
    actions = decide_fix(
        world(
            issues=[
                issue(3, "ai-ready", declared_paths=frozenset({"src/payment.py"})),
            ],
            prs=[
                pr(20, "ai-pr-open", changed_files=frozenset({"src/payment.py"})),
            ],
        ),
        CFG,
        FixState(),
        fixer_busy=False,
    )

    assert not _runs(actions, AgentKind.RESOLVE, 3)
    assert not _relabels(actions, Target.ISSUE, 3, Label.AI_READY, Label.AI_WORKING)


def test_blocked_issue_with_new_human_reply_resumes_once():
    blocked = issue(
        7,
        "ai-blocked",
        comments=(Comment("alice", "answered", "2026-01-01T00:00:00Z"),),
    )

    first = decide_fix(
        world(issues=[blocked]),
        CFG,
        FixState(sessions=frozenset({7})),
        fixer_busy=False,
    )
    second = decide_fix(
        world(issues=[blocked]),
        CFG,
        FixState(
            sessions=frozenset({7}),
            resumed_at={7: "2026-01-01T00:00:00Z"},
        ),
        fixer_busy=False,
    )

    assert _relabels(first, Target.ISSUE, 7, Label.AI_BLOCKED, Label.AI_WORKING)
    assert _runs(first, AgentKind.RESUME, 7)
    assert not _runs(second, AgentKind.RESUME, 7)


def test_open_pr_prioritizes_review_changes_before_ci_fix():
    actions = decide_fix(
        world(
            prs=[
                pr(
                    101,
                    "ai-pr-open",
                    closing_issue=9,
                    reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),),
                    checks=(Check("unit", bucket="fail"),),
                )
            ]
        ),
        CFG,
        FixState(),
        fixer_busy=False,
    )

    assert _runs(actions, AgentKind.ADDRESS_REVIEW, 101)
    assert not _runs(actions, AgentKind.FIX_CI, 101)


def test_green_approved_pr_enables_automerge_without_consuming_fixer():
    actions = decide_fix(
        world(
            prs=[
                pr(
                    101,
                    "ai-pr-open",
                    reviews=(Review("APPROVED", "review-bot", "t1"),),
                    checks=(Check("unit", bucket="ok"),),
                )
            ]
        ),
        CFG,
        FixState(),
        fixer_busy=False,
    )

    assert EnableAutoMerge(101, "squash") in actions
    assert not any(isinstance(action, RunAgent) for action in actions)


def test_merged_pr_cleanup_updates_issue_pr_and_worktree():
    actions = decide_fix(
        world(
            issues=[issue(9, "ai-pr-open")],
            prs=[pr(101, "ai-pr-open", state="MERGED", closing_issue=9)],
        ),
        CFG,
        FixState(),
        fixer_busy=False,
    )

    assert RemoveWorktree(9) in actions
    assert _relabels(actions, Target.ISSUE, 9, Label.AI_PR_OPEN, Label.AI_DONE)
    assert _relabels(actions, Target.PR, 101, Label.AI_PR_OPEN, None)
    assert _closes_issue(actions, 9)


def test_fixer_busy_still_allows_lock_free_cleanup_and_automerge():
    actions = decide_fix(
        world(
            issues=[issue(5, "ai-ready"), issue(9, "ai-pr-open")],
            prs=[
                pr(
                    101,
                    "ai-pr-open",
                    reviews=(Review("APPROVED", "review-bot", "t1"),),
                    checks=(Check("unit", bucket="ok"),),
                ),
                pr(102, "ai-pr-open", state="MERGED", closing_issue=9),
            ],
        ),
        CFG,
        FixState(),
        fixer_busy=True,
    )

    assert not any(isinstance(action, RunAgent) for action in actions)
    assert EnableAutoMerge(101, "squash") in actions
    assert RemoveWorktree(9) in actions
