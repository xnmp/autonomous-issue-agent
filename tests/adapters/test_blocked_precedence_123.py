from issue_agent.domain import dashboard as dashboard_domain

from test_dashboard import action, at, start


def test_blocked_and_escalated_attention_outrank_running_legs():
    running = (dashboard_domain._running_leg(start(kind="RESUME")),)

    assert dashboard_domain.stage_for(
        [action("relabel issue #7: -ai-working +ai-blocked")], running
    ) == "blocked"
    assert dashboard_domain.stage_for(
        [action("relabel issue #7: -ai-working +needs-human-review")], running
    ) == "escalated"


def test_reply_transition_clears_attention_and_restores_running_stage():
    events = [
        action("relabel issue #7: -ai-working +ai-blocked", ts_min=2),
        action("relabel issue #7: -ai-blocked +ai-working", ts_min=1),
    ]
    running = (dashboard_domain._running_leg(start(kind="RESUME")),)

    assert dashboard_domain.stage_for(events, running) == "resolving"


def test_terminal_clear_ends_historical_human_attention():
    events = [
        {"ts": at(30), "event": "escalation", "target": "pr", "number": 90},
        action("relabel pr #90: +needs-human-review", ts_min=20),
        action("clear state for pr #90, issue #7 (closed-unmerged)", ts_min=10),
    ]

    assert dashboard_domain.stage_for(events) == "closed unmerged"
