"""Adapter-visible contracts for the hard-end escalation route (#62)."""

from issue_agent.app.planners.tasks import FixTaskConfig, build_resolve_task, route_model


def test_anthropic_ceiling_routes_to_fable():
    assert route_model("complex", "claude") == "fable"


def test_difficult_history_is_visible_in_the_fixer_prompt():
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

    assert "difficult problem" in task.prompt
    assert "failed in the past" in task.prompt
    assert "take extra care" in task.prompt
