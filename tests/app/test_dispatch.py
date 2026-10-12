"""AgentDispatcher — end-to-end glue per AgentKind, with fakes (no SDK, no gh).

Drives each handler and asserts the resulting gateway writes + persisted state,
proving the dispatcher routes inputs→task→run→follow-up correctly. The DECISIONS
are tested in tests/domain; here we verify the wiring."""

from tests.domain.builders import observed_pr



import asyncio


from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.outcome import AgentResult
from issue_agent.app.planners.tasks import ReviewTaskConfig
from issue_agent.domain.review import ReviewMode
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway
from tests.fakes.review_authority import with_review_authority, review_state


class FakeStore:
    def __init__(self):
        self.sessions = {}
        self.pr_sessions = {}
        self.last_sha = {}
        self.last_patch_id = {}
        self.rounds = {}
        self.resumes = {}
        self.pr_resumed_at = {}
        self.phases = {}
        self.models = {}
        self.branches = {}
        self.predicted_paths = {}
        self.operator_guidance = {}

    def save_branch(self, issue, branch):
        self.branches[issue] = branch

    def get_branch(self, issue):
        return self.branches.get(issue, "")

    def save_predicted_paths(self, issue, paths):
        self.predicted_paths[issue] = frozenset(paths)

    def save_session(self, i, s):
        self.sessions[i] = s

    def save_model(self, i, m):
        if m:
            self.models[i] = m

    def get_model(self, i):
        return self.models.get(i, "")

    def get_session(self, i):
        return self.sessions.get(i, "")

    def set_last_sha(self, p, s):
        self.last_sha[p] = s

    def get_last_sha(self, p):
        return self.last_sha.get(p, "")

    def set_last_patch_id(self, p, s):
        self.last_patch_id[p] = s

    def get_last_patch_id(self, p):
        return self.last_patch_id.get(p, "")

    def set_operator_guidance(self, p, guidance):
        self.operator_guidance[p] = guidance

    def get_operator_guidance(self, p):
        return self.operator_guidance.get(p, "")

    def get_ci_rerun_head(self, p):
        return ""

    def bump_resumes(self, i):
        self.resumes[i] = self.resumes.get(i, 0) + 1
        return self.resumes[i]

    def set_resumed_at(self, i, at):
        self.__dict__.setdefault("resumed_at", {})[i] = at

    def get_resumed_at(self, i):
        return getattr(self, "resumed_at", {}).get(i, "")



    def get_phase(self, i):
        return self.phases.get(i, "")

    def set_phase(self, i, p):
        self.phases[i] = p



class FakeWorktrees:
    def ensure(self, issue, branch):
        return f"/wt/issue-{issue}"

    def remove(self, issue):
        pass

    def branch_for_issue(self, issue, fallback, *, preferred=""):
        del issue
        return preferred or fallback

    def prune_validate(self):
        return 0


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        return self.result


class RaisingRunner:
    """The C3 interleaving. `AgentRunner` deliberately RAISES on a pre-result
    transport death (it does not return is_error=True), so nothing in the
    workflow tail runs."""

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("stream closed before any result")
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        raise self.exc


class SeqRunner:
    """Returns a scripted sequence of results, one per run() call (last repeats)."""

    def __init__(self, results):
        self.results = list(results)
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        return self.results[min(len(self.tasks) - 1, len(self.results) - 1)]


class SchemaRunner:
    """Routes each run() to a result by the task's schema identity — lets the
    multi-agent review test script the gate / dimension / validation legs
    independently regardless of concurrency/order. `dim_by_dimension` /
    `val_by_confirmed` are callables receiving the task so a test can vary the
    result per finding."""

    def __init__(self, gate=None, dimension=None, validation=None):
        self._gate = gate
        self._dimension = dimension        # callable(task) -> AgentResult
        self._validation = validation      # callable(task) -> AgentResult
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        from issue_agent.app.planners.schemas import (
            REVIEW_FINDINGS_SCHEMA,
            REVIEW_GATE_SCHEMA,
            REVIEW_VALIDATION_SCHEMA,
        )
        if task.schema is REVIEW_GATE_SCHEMA:
            return self._gate
        if task.schema is REVIEW_FINDINGS_SCHEMA:
            return self._dimension(task) if callable(self._dimension) else self._dimension
        if task.schema is REVIEW_VALIDATION_SCHEMA:
            return self._validation(task) if callable(self._validation) else self._validation
        raise AssertionError(f"unexpected schema {task.schema}")


def _multi_dispatcher(gw, store, runner):
    cfg = DispatchConfig(
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER"),
        review_mode=ReviewMode.MULTI,
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return with_review_authority(AgentDispatcher(cfg, gw, store, runner, executor))


def _dispatcher(gw, store, runner):
    cfg = DispatchConfig(
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER"),
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return with_review_authority(AgentDispatcher(cfg, gw, store, runner, executor))


def _delegated_dispatcher(gw, store, runner):
    cfg = DispatchConfig(
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER", scout_model="sonnet"),
        review_mode=ReviewMode.DELEGATED,
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return with_review_authority(AgentDispatcher(cfg, gw, store, runner, executor))


def test_delegated_review_routes_to_workflow():
    gw = FakeGateway(prs={100: observed_pr(100, head_ref_oid="sha1")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, structured={
        "decision": "APPROVE", "body": "lgtm"}, session_id="rs"))
    d = _delegated_dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))
    assert any(c[0] == "review" for c in gw.calls)
    assert store.pr_sessions == {}


def test_review_submits_decision_from_structured():
    pr = observed_pr(100, head_ref_name="fix/5-x", head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, session_id="rs",
                                    structured={"decision": "APPROVE", "body": "lgtm"}))
    d = _dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))
    assert ("review", 100, "APPROVE", "lgtm") in gw.calls
    assert store.pr_sessions == {}
    assert review_state(store).head(100) == "abc"


# ── multi-agent review (REVIEW_MULTI_AGENT) ───────────────────────────────────
def _gate(reviewable=True, tier="standard", reason=""):
    return AgentResult(is_error=False,
                       structured={"reviewable": reviewable, "tier": tier, "reason": reason})


def _dim(findings):
    return AgentResult(is_error=False, structured={"findings": findings})


def _val(confirmed, verdict=""):
    return AgentResult(is_error=False,
                       structured={"confirmed": confirmed, "verdict": verdict})


def test_multi_review_routes_to_workflow():
    pr = observed_pr(100, head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = SchemaRunner(
        gate=_gate(),
        dimension=lambda t: _dim([{"location": "a.py:1", "claim": "bug", "severity": "blocking"}]),
        validation=lambda t: _val(True, "reproduced"),
    )
    d = _multi_dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))
    assert any(c[0] == "review" and c[2] == "REQUEST_CHANGES" for c in gw.calls)
    assert store.pr_sessions == {}


# ── T3: a GhUnavailable arriving BETWEEN the paid run and the follow-up ────────
# In every leg below the agent session ITSELF succeeds (a real, scripted
# AgentResult comes back with a session id) — the money is spent — and then a
# gh call in the follow-up path raises. The invariants under test: (1) the
# session id the runner returned is persisted (save_session already ran before
# the failing call in every leg here), (2) no counter is corrupted — either
# untouched because the follow-up decision never ran, or bumped exactly once,
# never both/double-counted, (3) nothing was half-applied: the failing gh call
# raises BEFORE its write is recorded, so no partial label state is left
# looking like a real, silent success.

