"""Deterministic full-stack server for Playwright."""

from pathlib import Path

import uvicorn

from issue_agent.app.dashweb.server import create_app


def role(name: str) -> dict:
    return {
        "role": name,
        "alive": True,
        "stale": False,
        "last_event_ts": "2026-07-29T08:00:00Z",
        "last_event": "tick",
        "events": 4,
        "log_path": f"/logs/{name}.log",
    }


ISSUE_CARD = {
    "issue": 50,
    "title": "Explain completed work",
    "url": "https://github.com/openai/autonomous-issue-agent/issues/50",
    "pr": 75,
    "pr_url": "https://github.com/openai/autonomous-issue-agent/pull/75",
    "pr_state": "merged",
    "stage": "done",
    "done": True,
    "cost_usd": 2,
    "estimated": False,
    "elapsed_s": 60,
    "started_ts": "2026-07-29T07:00:00Z",
    "last_ts": "2026-07-29T08:00:00Z",
    "merged_ts": "2026-07-29T08:00:00Z",
    "projected_cost_usd": 2,
    "projected_eta_s": 60,
    "rounds": 1,
    "models": ["claude-sonnet-5"],
    "flags": [],
    "steps": [{"kind": "RESOLVE", "state": "ok", "status": "done", "cost_usd": 2}],
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
                "roles": [role("fix"), role("review"), role("steward")],
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
            "cost_mix": None,
            "caps": {"issue_cap_usd": 5, "daily_cap_usd": 10, "today_usd": 2},
            "health": [],
            "unattributed": [],
            "health_trend": [{
                "day": "2026-07-29",
                "opened": 3,
                "closed": 1,
                "net_open": 2,
                "recurrences": 0,
                "first_observation": True,
            }],
            "warnings": [],
        }

    def issues(self, _name: str) -> dict:
        return {
            "in_flight": [],
            "pending": [],
            "done": [ISSUE_CARD],
            "backlog": [],
            "gh_ok": True,
            "warnings": [],
        }

    def issue(self, _name: str, _number: int) -> dict:
        return {
            "issue": 50,
            "title": "Explain completed work",
            "url": "https://github.com/openai/autonomous-issue-agent/issues/50",
            "stage": "done",
            "done": True,
            "prs": [75],
            "pull_requests": [{
                "number": 75,
                "url": "https://github.com/openai/autonomous-issue-agent/pull/75",
                "state": "merged",
            }],
            "cost_usd": 2,
            "elapsed_s": 60,
            "steps": [{
                "idx": 0,
                "kind": "RESOLVE",
                "role": "fix",
                "state": "ok",
                "model": "claude-sonnet-5",
                "status": "done",
                "exit_reason": "ok",
                "duration_s": 60,
                "cost_usd": 2,
                "estimated": False,
                "session_id": "session-50",
                "has_transcript": False,
                "turns": 3,
                "usage": None,
                "ts": "2026-07-29T08:00:00Z",
            }],
            "events": [{
                "ts": "2026-07-29T07:59:00Z",
                "event": "action",
                "role": "steward",
                "desc": "comment issue #50: verification recorded",
            }],
            "warnings": [],
        }

    def step_events(self, _name: str, _number: int, _idx: int) -> dict:
        return {"events": [], "warnings": []}

    def cost_chart(self, _session_id: str) -> tuple[str, str]:
        return "<html></html>", ""

    def start(self, _name: str) -> tuple[bool, str]:
        return True, "started"

    def promote(self, _name: str, _number: int) -> tuple[bool, str]:
        return True, "promoted"

    def priority(
        self, _name: str, _number: int, _priority: str | None
    ) -> tuple[bool, str]:
        return True, "updated"


if __name__ == "__main__":
    harness_root = Path(__file__).resolve().parents[2]
    uvicorn.run(
        create_app(FixtureBackend(), harness_root=str(harness_root)),
        host="127.0.0.1",
        port=4174,
        log_level="warning",
    )
