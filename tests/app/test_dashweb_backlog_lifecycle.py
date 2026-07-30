"""Regression coverage for dashboard backlog lifecycle classification."""

from issue_agent.app.dashweb import snapshots


def test_backlog_excludes_pr_open_issue():
    assert snapshots.is_backlog({"labels": [{"name": "ai-pr-open"}]}) is False
