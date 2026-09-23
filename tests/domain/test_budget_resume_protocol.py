"""Budget review keeps the established blocked-issue resume contract."""

from dataclasses import replace

from tests.domain.builders import Comment, issue, world
from tests.fakes.workflow_authority import resolve_snapshot

from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import budget_actions
from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow import FreshResolve, ResolveSession


def test_fix_bot_reply_after_budget_prompt_dispatches_resume():
    snapshot = resolve_snapshot(7)
    source = WorkflowAttemptIdentity(snapshot.binding, RunId("prior-run"), "prior-leg")
    snapshot = replace(snapshot, revision=replace(snapshot.revision, version=1),
                       episode=replace(snapshot.episode, continuation=FreshResolve(
                           ResolveSession(source, "saved-session", "claude", "model"), 1,
                       )))
    prompt = budget_actions(7)[0].body
    blocked = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", prompt, "t1"),
            Comment("fix-bot", "Raised the cap; resume from the handover.", "t2"),
        ),
    )
    actions = decide_fix(
        world(issues=[blocked]),
        FixConfig(fix_bot="fix-bot"),
        FixState(resolve_workflows={7: snapshot}),
        fixer_busy=False,
    )

    assert Relabel(
        Target.ISSUE, 7, Label.AI_BLOCKED, Label.AI_WORKING, "", workflow=snapshot.binding
    ) in actions
    dispatch = next(action for action in actions if isinstance(action, RunAgent))
    assert dispatch == RunAgent(AgentKind.RESUME, 7, workflow=snapshot.binding)
    assert dispatch.requires in actions
    assert dispatch.requires.workflow == snapshot.binding
