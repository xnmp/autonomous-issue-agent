"""Real fix-role wiring and SQLite, with offline GitHub and agent effects."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.domain.architecture import NoPass, PassEnded, PassInFlight, architecture_pass
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.labels import Label
from issue_agent.domain.routing import Tier
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.state import Dispatch, Harness, Local, pr_key
from tests.fakes.recorded import derive
from issue_agent.domain.refs import harness_branch_issue
from issue_agent.domain.world import Review
from tests.app.test_fix_role import Agent, FakeGitHub, did_nothing, dispatched, role, tick
from tests.app.test_fix_role import _ci


class ArchitectureGitHub(FakeGitHub):
    observed_at = ""
    merged_prs = 0

    def build_world(self, base, *, filed_by="", pickup=None):
        kwargs = {"filed_by": filed_by} | ({"pickup": pickup} if pickup is not None else {})
        observed = super().build_world(base, **kwargs)
        latest = max((i for i in self.issues.values() if i.number not in self.unindexed and architecture_pass(i, filed_by)),
                     key=lambda i: i.number, default=None)
        ended = latest and (latest.state == "closed" or any(
            p.state == "MERGED" and harness_branch_issue(p.head_ref_name) == latest.number for p in observed.prs))
        architecture = (NoPass() if latest is None else PassEnded(latest.number, self.merged_prs)
                        if ended else PassInFlight(latest))
        return replace(observed, now=self.observed_at or observed.now, architecture=architecture)


@pytest.mark.parametrize("lost_reply", [False, True])
def test_pass_is_filed_once_readied_and_resolved_at_the_fixed_high_route(role, lost_reply):
    gh = ArchitectureGitHub()
    if lost_reply:
        gh.lose_reply.add("create_issue")
    composed, _ = role(gh, Agent(did_nothing()))
    gh.observed_at = "2026-10-15T23:59:59Z"
    tick(composed)
    (issue,) = gh.issues.values()
    assert issue.author == "fix-bot" and issue.title == "Architecture pass (first)"
    assert "<!-- issue-agent -->" in issue.body
    gh.unindexed = {issue.number}
    gh.observed_at = "2026-10-16T00:00:01Z"
    tick(composed)  # Search lag and a lost filing reply still use act's ordinary read-back.
    assert len(gh.issues) == 1
    gh.unindexed.clear()
    gh.observed_at = ""
    tick(composed)
    assert Label.AI_READY in gh.issues[issue.number].labels
    actions = tick(composed)
    (resolve,) = dispatched(actions, AgentKind.RESOLVE)
    assert resolve.number == issue.number and resolve.tier is Tier.HIGH
    assert not dispatched(actions, AgentKind.STEWARD) and not dispatched(actions, AgentKind.TRIAGE)
    assert len(gh.issues) == 1


def test_pass_uses_normal_branch_publication_review_and_merge(role):
    gh = ArchitectureGitHub()

    def push(task):
        # The normal planner names the prepared branch in its push instruction.
        branch = task.prompt.split("git push origin ", 1)[1].split()[0]
        gh.push(branch, "h1")
        return AgentResult(False, status="pushed", summary="Updated architectural pressure.", cost_usd=0.1)

    composed, _ = role(gh, Agent(push))
    tick(composed)  # File.
    tick(composed)  # Ready.
    tick(composed)  # Resolve and push.
    tick(composed)  # Publish.
    (issue,) = gh.issues.values()
    (pr,) = gh.prs.values()
    assert pr.head_ref_name.startswith(f"fix/{issue.number}-")
    assert pr.body == "Updated architectural pressure."
    gh.prs[pr.number] = replace(pr, ci=_ci("h1", "pass"))
    states = derive(gh.build_world("dev", filed_by="fix-bot"), Local(now=datetime.now(timezone.utc)),
                    Harness("fix-bot", "review-bot"))
    assert states[pr_key(pr.number)] == Dispatch(AgentKind.REVIEW, "h1", Tier.HIGH)
    gh.prs[pr.number] = replace(gh.prs[pr.number], reviews=(
        Review("APPROVED", "review-bot", submitted_at="2026-10-16T00:00:00Z", commit_id="h1"),))
    tick(composed)
    assert gh.merged == [(pr.number, "h1")]
