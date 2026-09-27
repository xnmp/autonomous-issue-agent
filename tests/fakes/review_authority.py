"""Explicit review authority for isolated prompt, dispatch and verdict tests.

SQLite admission, generations, receipts and reconciliation are exercised by
the composed review acceptance suite. This double owns only the workflow
boundary used by these tests; legacy Store files never authorize a round.
"""

from dataclasses import dataclass, field, fields

from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.app.workflows.review import BoundReviewRound, ReviewWorkflow
from issue_agent.domain.review import count_prior_rejections
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)


@dataclass
class ReviewFixtureState:
    heads: dict = field(default_factory=dict)
    patches: dict = field(default_factory=dict)
    guidance: dict = field(default_factory=dict)
    submissions: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    evidence_ready: bool = True
    unavailable: bool = False

    def set_head(self, pr, head):
        self.heads[pr] = head

    def head(self, pr):
        return self.heads.get(pr, "")

    def set_patch(self, pr, patch):
        self.patches[pr] = patch

    def patch(self, pr):
        return self.patches.get(pr, "")

    def set_guidance(self, pr, guidance):
        self.guidance[pr] = guidance


def review_state(store):
    if not hasattr(store, "_review_fixture_state"):
        object.__setattr__(store, "_review_fixture_state", ReviewFixtureState())
    return store._review_fixture_state


def review_binding(pr):
    return WorkflowBinding(WorkflowFence(WorkflowKey("o/r", PullRequestRef(pr)), 0),
                           FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1), f"review:{pr}")


class FixtureReviewAuthority:
    def __init__(self, gateway, store, executor, bot):
        self.gateway, self.state, self.executor, self.bot = gateway, review_state(store), executor, bot

    def evidence_due(self, binding, head_sha):
        return self.state.evidence_ready

    def begin(self, binding, head_sha, *, expected_claim_target):
        if self.state.unavailable:
            raise WorkflowAuthorityError("review authority unavailable")
        pr = binding.fence.key.target.number
        if binding != review_binding(pr):
            raise WorkflowAuthorityError("stale review authority")
        if self.state.head(pr) == head_sha:
            return None
        return BoundReviewRound(binding, head_sha, self.state.head(pr) or None, self.state.guidance.get(pr, ""))

    def patch_covered(self, round, patch_id):
        return self.state.patch(round.binding.fence.key.target.number) == patch_id

    def prior_rejections(self, round, non_resetting):
        pr = self.gateway.get_pr(round.binding.fence.key.target.number)
        return count_prior_rejections(pr.reviews, self.bot, non_resetting_approval_commits=non_resetting)

    def record_evidence(self, round, error):
        self.state.evidence.append((round.head_sha, error))

    def submit(self, round, event, body, *, patch_id, escalate_cause, counted_past=frozenset()):
        pr = round.binding.fence.key.target.number
        self.state.submissions.append((pr, event, escalate_cause, counted_past))
        if not self.executor.submit_review(pr, event, body, round.head_sha):
            return False
        self.state.set_head(pr, round.head_sha)
        self.state.set_patch(pr, patch_id)
        return True


class BoundFixtureReviewWorkflow(ReviewWorkflow):
    async def review(self, pr, *, claim_target=None, binding=None):
        return await super().review(pr, claim_target=claim_target, binding=binding or review_binding(pr))

    async def re_review(self, pr, *, claim_target=None, binding=None):
        return await super().re_review(pr, claim_target=claim_target, binding=binding or review_binding(pr))


def with_review_authority(dispatcher):
    workflow = dispatcher.review_workflow
    values = {f.name: getattr(workflow, f.name) for f in fields(workflow)}
    values["authority"] = FixtureReviewAuthority(workflow.gateway, workflow.store, dispatcher.executor,
                                                 workflow.cfg.review_bot)
    dispatcher.review_workflow = BoundFixtureReviewWorkflow(**values)
    return dispatcher
