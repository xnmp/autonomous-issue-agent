"""Run the multi-agent reviewer against a REAL PR diff, print the verdict.

Drives the real AgentDispatcher + AgentRunner (live SDK, real LLM legs) with a
fake gateway that serves the actual `gh pr diff <N>`. Does NOT post (the harness
author would self-review-422, and we don't want to pollute the PR) — captures and
prints the submitted event + body instead.

Usage:  uv run --with claude-agent-sdk python scripts/live_review_pr.py <owner/repo> <pr-number>
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

sys.path.insert(0, "src")

from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.adapters.agent_runner import AgentRunner
from issue_agent.app.planners.tasks import ReviewTaskConfig
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.world import PRView


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


class FakeGW:
    def __init__(self, repo: str, number: int):
        self.repo, self.number = repo, number
        self._diff = gh("pr", "diff", str(number), "-R", repo)
        meta = gh("pr", "view", str(number), "-R", repo,
                  "--json", "title,body,headRefOid").strip()
        import json
        self._m = json.loads(meta)
        self.submitted = None

    def get_pr(self, n):
        return PRView(n, head_ref_oid=self._m["headRefOid"],
                      title=self._m["title"], body=self._m.get("body") or "")

    def pr_diff(self, n):
        return self._diff

    def submit_review(self, pr, event, body):
        self.submitted = (event, body)
        print(f"\n{'='*72}\nWOULD SUBMIT  event={event}\n{'='*72}\n{body}\n{'='*72}")
        return True

    def relabel(self, *a):
        print(f"relabel: {a}")


class MemStore:
    def __init__(self):
        self.d = {}

    def get_pr_session(self, p): return self.d.get(("s", p), "")
    def save_pr_session(self, p, s): self.d[("s", p)] = s
    def get_last_sha(self, p): return self.d.get(("sha", p), "")
    def set_last_sha(self, p, v): self.d[("sha", p)] = v
    def get_round(self, p): return self.d.get(("r", p), 1)
    def set_round(self, p, n): self.d[("r", p)] = n


async def main() -> int:
    repo, number = sys.argv[1], int(sys.argv[2])
    log = lambda m: print(f"[log] {m}", flush=True)
    runner = AgentRunner(log=log)
    cfg = DispatchConfig(
        # Larger per-dimension budget: a big diff (this PR is ~1k lines) blows the
        # 0.75 default and errors the specialist. Real deployments set
        # REVIEW_DIMENSION_BUDGET_USD per repo size.
        review=ReviewTaskConfig(repo, review_prompt="", dimension_budget_usd=2.0,
                                validation_budget_usd=0.75),
        review_mode=ReviewMode.MULTI,
        log=log,
    )
    gw = FakeGW(repo, number)
    d = AgentDispatcher(cfg, gw, MemStore(), None, runner, Executor(gw, log=log))
    await d.dispatch(RunAgent(AgentKind.REVIEW, number))
    if gw.submitted is None:
        print("\nRESULT: no review produced")
        return 1
    print(f"\nRESULT: event={gw.submitted[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
