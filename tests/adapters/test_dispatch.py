"""AgentDispatcher — end-to-end glue per AgentKind, with fakes (no SDK, no gh).

Drives each handler and asserts the resulting gateway writes + persisted state,
proving the dispatcher routes inputs→task→run→follow-up correctly. The DECISIONS
are tested in tests/domain; here we verify the wiring."""

import asyncio

from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.outcome import AgentResult
from issue_agent.app.planners.tasks import (
    FixTaskConfig,
    JiraTaskConfig,
    ReviewTaskConfig,
    TriageTaskConfig,
)
from issue_agent.domain.agent_task import McpServer
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.triage import TriageConfig
from issue_agent.domain.world import Check, Comment, IssueView, PRView
from issue_agent.domain.labels import Label


class FakeGateway:
    def __init__(self, issues=None, prs=None, pr_for_issue=None, submit_ok=True):
        self._issues = issues or {}
        self._prs = prs or {}
        self._pr_for_issue = pr_for_issue or {}
        self.calls = []
        self.submit_ok = submit_ok  # simulate a failed review POST (422/403) when False

    def get_issue(self, n):
        return self._issues[n]

    def get_pr(self, n):
        return self._prs[n]

    def pr_for_issue(self, issue):
        return self._pr_for_issue.get(issue)

    def issue_for_pr(self, pr):
        return self._prs[pr].closing_issue

    def pr_diff(self, pr):
        return "diff text"

    def submit_review(self, pr, event, body):
        self.calls.append(("review", pr, event, body))
        return self.submit_ok  # mirror real gateway: True iff the POST succeeded

    def relabel(self, kind, number, remove, add):
        self.calls.append(("relabel", kind, number, remove, add))

    def comment(self, kind, number, body):
        self.calls.append(("comment", kind, number, body))

    def close_issue(self, number, reason, comment):
        self.calls.append(("close", number))

    def enable_auto_merge(self, pr, method):
        self.calls.append(("automerge", pr))

    def update_branch(self, pr):
        self.calls.append(("update_branch", pr))


class FakeStore:
    def __init__(self):
        self.sessions = {}
        self.pr_sessions = {}
        self.triage_at = {}
        self.last_sha = {}
        self.rounds = {}
        self.resumes = {}
        self.phases = {}

    def save_session(self, i, s):
        self.sessions[i] = s

    def get_session(self, i):
        return self.sessions.get(i, "")

    def save_pr_session(self, p, s):
        self.pr_sessions[p] = s

    def get_pr_session(self, p):
        return self.pr_sessions.get(p, "")

    def set_last_sha(self, p, s):
        self.last_sha[p] = s

    def get_last_sha(self, p):
        return self.last_sha.get(p, "")

    def set_round(self, p, n):
        self.rounds[p] = n

    def get_round(self, p):
        return self.rounds.get(p, 1)

    def bump_resumes(self, i):
        self.resumes[i] = self.resumes.get(i, 0) + 1
        return self.resumes[i]

    def set_resumed_at(self, i, at):
        self.__dict__.setdefault("resumed_at", {})[i] = at

    def get_resumed_at(self, i):
        return getattr(self, "resumed_at", {}).get(i, "")

    def get_review_retry(self, i):
        return getattr(self, "review_retries", {}).get(i, ("", 0))

    def set_review_retry(self, i, at, n):
        self.__dict__.setdefault("review_retries", {})[i] = (at, n)

    def set_triage_at(self, i, at):
        self.triage_at[i] = at

    def set_ci_retries(self, i, n):
        pass

    def get_ci_retries(self, i):
        return 0

    def get_phase(self, i):
        return self.phases.get(i, "")

    def set_phase(self, i, p):
        self.phases[i] = p

    def get_jira_key(self, i):
        return getattr(self, "_jira_keys", {}).get(i, "")

    def set_jira_key(self, i, k):
        self.__dict__.setdefault("_jira_keys", {})[i] = k

    def get_jira_phase(self, i):
        return getattr(self, "_jira_phases", {}).get(i, "")

    def set_jira_phase(self, i, p):
        self.__dict__.setdefault("_jira_phases", {})[i] = p


class FakeWorktrees:
    def __init__(self):
        self.resets = []

    def ensure(self, issue, branch):
        return f"/wt/issue-{issue}"

    def remove(self, issue):
        pass

    def reset(self, issue):
        self.resets.append(issue)


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        return self.result


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
    return AgentDispatcher(cfg, gw, store, None, runner, executor)


def _dispatcher(gw, store, runner):
    cfg = DispatchConfig(
        fix=FixTaskConfig("o/r", agent_md="AM"),
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER"),
        triage=TriageTaskConfig("steward-bot", triage_prompt="T"),
        triage_cfg=TriageConfig("steward-bot", enabled=True),
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return AgentDispatcher(cfg, gw, store, FakeWorktrees(), runner, executor)


def _delegated_dispatcher(gw, store, runner):
    cfg = DispatchConfig(
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER", scout_model="sonnet"),
        review_mode=ReviewMode.DELEGATED,
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return AgentDispatcher(cfg, gw, store, None, runner, executor)


def test_delegated_review_routes_to_workflow():
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="sha1")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, structured={
        "decision": "APPROVE", "body": "lgtm"}, session_id="rs"))
    d = _delegated_dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))
    assert any(c[0] == "review" for c in gw.calls)
    assert store.pr_sessions.get(100) == "rs"


def test_resolve_routes_to_fix_workflow():
    gw = FakeGateway(issues={5: IssueView(5, title="Null token", body="x")},
                     pr_for_issue={5: 100})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="pr_opened", session_id="s5"))
    d = _dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    assert store.sessions[5] == "s5"
    assert ("relabel", "issue", 5, "ai-working", "ai-pr-open") in gw.calls
    assert ("relabel", "pr", 100, None, "ai-pr-open") in gw.calls


def _split_dispatcher(gw, store, runner, wt=None, max_contract_rounds=2):
    cfg = DispatchConfig(
        fix=FixTaskConfig("o/r", agent_md="AM"),
        author_tests_split=True, max_contract_rounds=max_contract_rounds,
    )
    wt = wt or FakeWorktrees()
    executor = Executor(gw, remove_worktree=wt.remove)
    return AgentDispatcher(cfg, gw, store, wt, runner, executor)


def test_split_resolve_routes_to_fix_workflow():
    gw = FakeGateway(issues={5: IssueView(5, title="Null token", body="x")},
                     pr_for_issue={5: 100})
    store, wt = FakeStore(), FakeWorktrees()
    runner = SeqRunner([
        AgentResult(is_error=False, status="tests_authored", session_id="a5"),
        AgentResult(is_error=False, status="pr_opened", session_id="i5"),
    ])
    d = _split_dispatcher(gw, store, runner, wt=wt)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    assert len(runner.tasks) == 2
    assert ("relabel", "issue", 5, "ai-working", "ai-pr-open") in gw.calls


def test_review_submits_decision_from_structured():
    pr = PRView(100, head_ref_name="fix/5-x", head_ref_oid="abc", title="t", body="b")
    gw = FakeGateway(prs={100: pr})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, session_id="rs",
                                    structured={"decision": "APPROVE", "body": "lgtm"}))
    d = _dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))
    assert ("review", 100, "APPROVE", "lgtm") in gw.calls
    assert store.pr_sessions[100] == "rs"
    assert store.last_sha[100] == "abc"


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
    pr = PRView(100, head_ref_oid="abc", title="t", body="b")
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
    assert store.pr_sessions[100] == "multi"


def test_triage_routes_to_steward_workflow():
    iss = IssueView(7, title="Add export", body="want it", author="alice",
                    comments=(Comment("alice", "please add export"),), updated_at="t_post")
    gw = FakeGateway(issues={7: iss})
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={"verdict": "blocked", "plain_summary": "Need detail",
                    "creator_questions": ["Which format?"], "owner_questions": [],
                    "acceptance_criteria": []},
    ))
    d = _dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.TRIAGE, 7)))
    assert ("relabel", "issue", 7, None, "ai-triage") in gw.calls
    assert store.triage_at[7] == "t_post"


def _ci_dispatcher(gw, store, runner, fix_mcp_config=""):
    cfg = DispatchConfig(
        fix=FixTaskConfig("o/r", agent_md="AM"),
        fix_mcp_config=fix_mcp_config, harness_root="/tmp/none",
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return AgentDispatcher(cfg, gw, store, FakeWorktrees(), runner, executor)


def _ci_pr():
    return PRView(100, head_ref_name="fix/5-x", closing_issue=5,
                  checks=(Check("SonarQube Code Analysis", bucket="fail"),))


def test_fix_ci_routes_to_fix_workflow():
    gw = FakeGateway(prs={100: _ci_pr()})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))
    d = _ci_dispatcher(gw, FakeStore(), runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))
    assert "SonarQube Code Analysis" in runner.tasks[0].prompt


# ── Jira lifecycle sync ─────────────────────────────────────────────────────────
def _jira_dispatcher(gw, store, runner):
    jira = JiraTaskConfig(
        project_key="ABC", repo="o/r",
        mcp_servers=(McpServer(name="atlassian", config={"type": "http"}, tools="mcp__atlassian__*"),),
    )
    cfg = DispatchConfig(jira=jira)
    executor = Executor(gw)
    return AgentDispatcher(cfg, gw, store, None, runner, executor)


def _issue(n, *labels):
    return IssueView(n, title="t", body="b", labels=frozenset(labels))


def test_jira_create_persists_key_and_phase():
    """Dispatcher-level smoke: JIRA_SYNC routes to the Jira workflow.

    Detailed create/transition/skip/retry behavior is covered directly in
    test_steward_workflows.py and domain/test_jira.py.
    """
    gw = FakeGateway(issues={5: _issue(5, Label.AI_READY)})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="created",
                                    structured={"status": "created", "ticket_key": "ABC-9"}))
    d = _jira_dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.JIRA_SYNC, 5)))
    assert "Create a Jira ticket" in runner.tasks[0].prompt   # no key → create
    assert store.get_jira_key(5) == "ABC-9"
    assert store.get_jira_phase(5) == "todo"


# ── logging breadcrumbs: no more silent gap between dispatch and PR ────────────
def test_resolve_logs_worktree_and_agent_lifecycle():
    """A resolve emits: worktree ready → agent running → agent done. This is the
    in-process half of the fix log (the per-commit red:/green: half is the
    governor hook's, tested in test_governor_adapter)."""
    gw = FakeGateway(issues={5: IssueView(5, title="Null token", body="x")},
                     pr_for_issue={5: 100})
    runner = FakeRunner(AgentResult(is_error=False, status="pr_opened",
                                    session_id="s5", cost_usd=1.23))
    logged: list[str] = []
    cfg = DispatchConfig(fix=FixTaskConfig("o/r", agent_md="AM"), log=logged.append)
    executor = Executor(gw, remove_worktree=lambda i: None)
    d = AgentDispatcher(cfg, gw, FakeStore(), FakeWorktrees(), runner, executor)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    joined = "\n".join(logged)
    assert "worktree #5: ready on 'fix/5-" in joined
    assert "agent #5: resolve — running" in joined
    assert "agent #5: resolve — done (status=pr_opened, $1.23)" in joined
