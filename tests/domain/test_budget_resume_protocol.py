"""Budget review keeps the established blocked-issue resume contract."""

from builders import Comment, issue, world

from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import budget_actions


def test_fix_bot_reply_after_budget_prompt_dispatches_resume():
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
        FixState(sessions=frozenset({7})),
        fixer_busy=False,
    )

    assert Relabel(Target.ISSUE, 7, Label.AI_BLOCKED, Label.AI_WORKING) in actions
    assert RunAgent(AgentKind.RESUME, 7) in actions
