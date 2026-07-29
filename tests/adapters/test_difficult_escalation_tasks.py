"""Adapter-visible contracts for the hard-end escalation route (#62)."""

from issue_agent.app.planners.tasks import (
    FixTaskConfig,
    build_author_task,
    build_implement_task,
    build_resolve_task,
    route_ceiling_model,
)


def test_anthropic_ceiling_routes_to_fable():
    assert route_ceiling_model("claude") == "fable"


def test_difficult_history_is_visible_in_the_fixer_system_prompt():
    task = build_resolve_task(
        62,
        "Hard regression",
        "body",
        "/tmp/worktree",
        FixTaskConfig(
            "xnmp/autonomous-issue-agent",
            model="gpt-5.6-sol",
            difficulty_notice=(
                "This is a difficult problem that has failed in the past; "
                "take extra care."
            ),
        ),
    )

    assert "difficult problem" in task.append_system_prompt
    assert "failed in the past" in task.append_system_prompt
    assert "take extra care" in task.append_system_prompt
    assert "difficult problem" not in task.prompt


def test_author_implement_split_preserves_the_difficult_history_warning():
    cfg = FixTaskConfig("xnmp/autonomous-issue-agent", agent_md="agent contract", difficulty_notice="take extra care")

    author = build_author_task(62, "Hard regression", "body", "/tmp/worktree", cfg)
    implementer = build_implement_task(62, "Hard regression", "body", "/tmp/worktree", cfg)

    assert "take extra care" in author.append_system_prompt
    assert "take extra care" in implementer.append_system_prompt
