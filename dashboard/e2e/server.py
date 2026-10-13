"""Deterministic full-stack server for Playwright."""

from pathlib import Path

import uvicorn


from issue_agent.app.dashweb.server import create_app


def role(name: str) -> dict:
    return {"role": name, "alive": True, "stale": False, "heartbeat_ts": "2026-07-29T08:00:00Z"}


DONE_CARD = {
    "issue": 50,
    "title": "Explain completed work",
    "url": "https://github.com/openai/autonomous-issue-agent/issues/50",
    "pr": 75,
    "pr_url": "https://github.com/openai/autonomous-issue-agent/pull/75",
    "merged_ts": "2026-07-29T08:00:00Z",
    "cost_usd": 2,
    "legs": 1,
    "review_rounds": 0,
    "labels": [],
}


class FixtureBackend:
    def known(self, name: str) -> bool:
        return name == "self"

    def repos(self) -> dict:
        return {
            "repos": [{
                "name": "self",
                "repo": "openai/autonomous-issue-agent",
                "target_dir": "/tmp/self",
                "is_self": True,
                "active": True,
                "roles": [role("fix"), role("review")],
                "frecency": 1,
                "last_activity_ts": "2026-07-29T08:00:00Z",
                "in_flight": 0,
                "done": 1,
            }],
            "warnings": [],
        }

    def usage(self) -> dict:
        return {
            "claude": None,
            "codex": None,
            "harness": {
                "global_daily_cap_usd": 50,
                "today_all_repos_usd": 2,
                "per_repo": [{
                    "name": "self",
                    "repo": "openai/autonomous-issue-agent",
                    "daily_cap_usd": 10,
                    "today_usd": 2,
                }],
            },
            "warnings": [],
        }

    def verification(self) -> dict:
        return {
            "rows": [{
                "id": "V-1",
                "change": "Dashboard",
                "status": "verified",
                "evidence": "E2E",
                "shipped": "yes",
                "raw": "",
            }],
            "warnings": [],
        }

    def overview(self, _name: str, _timezone_name: str | None = None) -> dict:
        return {
            "metrics": None,
            "caps": {"issue_cap_usd": 5, "daily_cap_usd": 10, "global_daily_cap_usd": 50, "today_usd": 2},
            "health": [],
            "needs_attention": [],
            "health_trend": [{"day": "2026-07-29", "opened": 3, "closed": 1, "net_open": 2}],
            "spend_per_hour": [],
            "warnings": [],
        }

    def issues(self, _name: str) -> dict:
        return {
            "in_flight": [],
            "done": [DONE_CARD],
            "backlog": [],
            "gh_ok": True,
            "observed_at": "2026-07-29T08:00:00Z",
            "notifications": [],
            "warnings": [],
        }

    def issue(self, _name: str, _number: int) -> dict | None:
        if _number != 50:
            return None
        return {
            "issue": 50,
            "title": "Explain completed work",
            "url": "https://github.com/openai/autonomous-issue-agent/issues/50",
            "labels": [],
            "board": None,
            "action": None,
            "done": True,
            "held_for_human": False,
            "pull_requests": [{
                "number": 75,
                "url": "https://github.com/openai/autonomous-issue-agent/pull/75",
                "state": "merged",
            }],
            "cost_usd": 2,
            "estimated": False,
            "rail": {
                "current": "merge", "furthest": "merge", "resolve_attempts": 1, "review_rounds": 0,
                "ci_runs": 0, "steps": 1, "live": False,
            },
            "history": [{"stage": "resolve", "steps": [{"kind": "RESOLVE", "outcome": "done", "count": 1}]}],
            "steps": [{
                "idx": 0,
                "leg_id": "leg-50-0",
                "kind": "RESOLVE",
                "role": "fix",
                "runtime": "codex",
                "state": "ended",
                "outcome": "done",
                "started_ts": "2026-07-29T07:59:00Z",
                "ended_ts": "2026-07-29T08:00:00Z",
                "duration_s": 60,
                "cost_usd": 2,
                "estimated": False,
                "pricing_basis": "provider_reported",
                "end_reason": "normal_return",
                "session_id": "session-50",
                "usage": None,
                "head": None,
                "pr": None,
                "summary": "Explained the completed work.",
            }],
            "warnings": [],
        }

    def promote(self, _name: str, _number: int) -> tuple[bool, str]:
        return True, "promoted"

    def unblock(self, _name: str, _number: int) -> tuple[bool, str]:
        return True, "unblocked"

def fixture_app():
    """The real dashboard app over the fixture backend."""
    harness_root = Path(__file__).resolve().parents[2]
    backend = FixtureBackend()
    app = create_app(backend, harness_root=str(harness_root))

    return app


if __name__ == "__main__":
    uvicorn.run(
        fixture_app(),
        host="127.0.0.1",
        port=4174,
        log_level="warning",
    )
