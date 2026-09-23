"""Characterization tests for durable fix-workflow behavior.

These sit above the existing detailed regression tests. They intentionally check
workflow outcomes without pinning the entire ordered action list, so the fix
workflow can be extracted/refactored while preserving behavior.
"""

from dataclasses import replace

from tests.domain.builders import Check, Comment, Review, issue, pr, world
from tests.fakes.workflow_authority import resolve_snapshot

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
from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow import FreshResolve, ResolveSession


CFG = FixConfig(fix_bot="fix-bot", arm_on_open=True)


def _runs(actions, kind: AgentKind, number: int) -> bool:
    binding = resolve_snapshot(number).binding if kind in (AgentKind.RESOLVE, AgentKind.RESUME) else None
    return RunAgent(kind, number, workflow=binding) in actions


def _state(*numbers, session=False, **kwargs):
    snapshots = {number: resolve_snapshot(number) for number in numbers}
    if session:
        for number, snapshot in snapshots.items():
            source = WorkflowAttemptIdentity(snapshot.binding, RunId(f"prior-{number}"), f"leg-{number}")
            snapshots[number] = replace(snapshot, revision=replace(snapshot.revision, version=1),
                                        episode=replace(snapshot.episode, continuation=FreshResolve(
                                            ResolveSession(source, f"session-{number}", "claude", "model"), 1,
                                        )))
    return FixState(resolve_workflows=snapshots, **kwargs)


def _relabels(actions, target: Target, number: int, remove, add) -> bool:
    return any(
        isinstance(action, Relabel)
        and action.target is target
        and action.number == number
        and action.remove is remove
        and action.add is add
        and action.workflow == (resolve_snapshot(number).binding if target is Target.ISSUE else None)
        for action in actions
    )


def _closes_issue(actions, number: int) -> bool:
    return any(isinstance(action, CloseIssue) and action.number == number
               and action.workflow == resolve_snapshot(number).binding for action in actions)


def test_ready_issue_dispatch_is_single_fixer_and_marks_working():
    actions = decide_fix(
        world(issues=[issue(2, "ai-ready", "P2"), issue(1, "ai-ready", "P1")]),
        CFG,
        _state(1, 2),
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
        _state(3),
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
        _state(7, session=True),
        fixer_busy=False,
    )
    second = decide_fix(
        world(issues=[blocked]),
        CFG,
        _state(
            7, session=True,
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
                    head_ref_oid="head-101",
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
                    head_ref_oid="head-101",
                    reviews=(
                        Review(
                            "APPROVED",
                            "review-bot",
                            "t1",
                            commit_id="head-101",
                        ),
                    ),
                    checks=(Check("unit", bucket="ok"),),
                )
            ]
        ),
        CFG,
        FixState(),
        fixer_busy=False,
    )

    assert EnableAutoMerge(101, "squash", "head-101") in actions
    assert not any(isinstance(action, RunAgent) for action in actions)


def test_merged_pr_cleanup_updates_issue_pr_and_worktree():
    actions = decide_fix(
        world(
            issues=[issue(9, "ai-pr-open")],
            prs=[pr(101, "ai-pr-open", state="MERGED", closing_issue=9)],
        ),
        CFG,
        _state(9),
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
                    head_ref_oid="head-101",
                    reviews=(
                        Review(
                            "APPROVED",
                            "review-bot",
                            "t1",
                            commit_id="head-101",
                        ),
                    ),
                    checks=(Check("unit", bucket="ok"),),
                ),
                pr(102, "ai-pr-open", state="MERGED", closing_issue=9),
            ],
        ),
        CFG,
        _state(5, 9),
        fixer_busy=True,
    )

    assert not any(isinstance(action, RunAgent) for action in actions)
    assert EnableAutoMerge(101, "squash", "head-101") in actions
    assert RemoveWorktree(9) in actions
