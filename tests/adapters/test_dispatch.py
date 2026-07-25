"""AgentDispatcher — end-to-end glue per AgentKind, with fakes (no SDK, no gh).

Drives each handler and asserts the resulting gateway writes + persisted state,
proving the dispatcher routes inputs→task→run→follow-up correctly. The DECISIONS
are tested in tests/domain; here we verify the wiring."""

import asyncio

import pytest

from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.store import Store
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from issue_agent.app.execute import Executor
from issue_agent.domain.actions import AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.fix import reap_actions
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
from issue_agent.domain.world import (
    Check,
    Comment,
    IssueView,
    PRView,
    Review,
    REVIEW_CHANGES_REQUESTED,
    WorldState,
)
from issue_agent.domain.labels import Label


class FakeGateway:
    def __init__(self, issues=None, prs=None, pr_for_issue=None, submit_ok=True, fail_on=None):
        self._issues = issues or {}
        self._prs = prs or {}
        self._pr_for_issue = pr_for_issue or {}
        self.calls = []
        self.submit_ok = submit_ok  # simulate a failed review POST (422/403) when False
        # T3: method names that raise GhUnavailable when called — lets a test
        # script a gh outage arriving between the paid run and the follow-up
        # (money spent, nothing/partially persisted). Default empty set keeps
        # every existing test's behavior unchanged.
        self.fail_on = fail_on or set()

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise GhUnavailable("network", f"{name} unavailable (test-injected)")

    def get_issue(self, n):
        self._maybe_fail("get_issue")
        return self._issues[n]

    def get_pr(self, n):
        self._maybe_fail("get_pr")
        return self._prs[n]

    def pr_for_issue(self, issue):
        self._maybe_fail("pr_for_issue")
        return self._pr_for_issue.get(issue)

    def issue_for_pr(self, pr):
        self._maybe_fail("issue_for_pr")
        return self._prs[pr].closing_issue

    def pr_diff(self, pr):
        self._maybe_fail("pr_diff")
        return "diff text"

    def submit_review(self, pr, event, body):
        self._maybe_fail("submit_review")
        self.calls.append(("review", pr, event, body))
        return self.submit_ok  # mirror real gateway: True iff the POST succeeded

    def relabel(self, kind, number, remove, add):
        self._maybe_fail("relabel")
        self.calls.append(("relabel", kind, number, remove, add))

    def comment(self, kind, number, body):
        self._maybe_fail("comment")
        self.calls.append(("comment", kind, number, body))

    def close_issue(self, number, reason, comment):
        self._maybe_fail("close_issue")
        self.calls.append(("close", number))

    def enable_auto_merge(self, pr, method):
        self._maybe_fail("enable_auto_merge")
        self.calls.append(("automerge", pr))

    def update_branch(self, pr):
        self._maybe_fail("update_branch")
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
        self.infra_deaths = {}

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

    def get_resolve_retries(self, i):
        return getattr(self, "resolve_retries", {}).get(i, 0)

    def set_resolve_retries(self, i, n):
        self.__dict__.setdefault("resolve_retries", {})[i] = n

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

    def get_infra_deaths(self, key):
        return self.infra_deaths.get(key, 0)

    def bump_infra_deaths(self, key):
        self.infra_deaths[key] = self.infra_deaths.get(key, 0) + 1
        return self.infra_deaths[key]


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


# ── review C3: a pre-result session death is COUNTED, and still propagates ─────
def _resolve_gateway():
    return FakeGateway(issues={5: IssueView(5, title="Null token", body="x")},
                       pr_for_issue={})


def test_pre_result_death_bumps_the_infra_counter(tmp_path):
    """review C3. The handler dies before `runner.run` returns, so every
    attempt-recording write in the workflow tail is skipped and the reaper
    requeues the issue — one full paid session every two polls, forever, with
    nothing on the ledger and no breaker moving. The infra-death counter is the
    only durable trace of that attempt, so it must be written on the raise path.
    Driven against a REAL Store: the key the dispatcher writes is the key the
    decide side later reads."""
    store = Store(state_dir=str(tmp_path))
    d = _dispatcher(_resolve_gateway(), store, RaisingRunner())

    with pytest.raises(RuntimeError, match="stream closed"):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert store.get_infra_deaths(5) == 1


def test_pre_result_death_re_raises_so_the_daemon_still_isolates_the_crash(tmp_path):
    """Bump then re-raise UNCHANGED: `Daemon.tick`'s catch (issue keeps
    ai-working, reaper requeues) is the crash-isolation contract. Swallowing
    here would silently change it."""
    store = Store(state_dir=str(tmp_path))
    boom = ValueError("transport gone")
    d = _dispatcher(_resolve_gateway(), store, RaisingRunner(boom))

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert excinfo.value is boom


def test_a_returned_result_does_not_bump_the_infra_counter(tmp_path):
    """Anti-vacuity twin: a leg that RETURNS (even an errored one) took the
    workflow tail, so the retry breakers see it — it must not also consume the
    infra ceiling, or a normally-failing issue escalates for the wrong reason."""
    store = Store(state_dir=str(tmp_path))
    runner = FakeRunner(AgentResult(is_error=True))
    d = _dispatcher(_resolve_gateway(), store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert store.get_infra_deaths(5) == 0


def test_pre_result_death_on_a_pr_leg_is_keyed_by_the_linked_issue(tmp_path):
    """PR-keyed kinds count against the ISSUE behind the PR — the same key
    `domain.fix.breaker_key` reads for that PR's legs."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: _ci_pr()})
    d = _ci_dispatcher(gw, store, RaisingRunner())

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))

    assert store.get_infra_deaths(5) == 1       # closing_issue of PR #100
    assert store.get_infra_deaths(100) == 0     # not the PR number


def test_pre_result_death_on_an_issueless_pr_uses_the_negated_pr_key(tmp_path):
    """A PR with no resolvable issue (Jira-keyed branch, no closing-ref) still
    accumulates: `-pr` is exactly `breaker_key`'s fallback, so the decide side
    reads what this wrote. The old `or -1` sentinel collapsed every such PR onto
    one dead key and the breaker never tripped (PR #112)."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="feat/ACME-2468-x")})
    d = _dispatcher(gw, store, RaisingRunner())

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert store.get_infra_deaths(-100) == 1


def test_a_counter_write_failure_does_not_replace_the_original_death():
    """The bump is a receipt, not a gate: if the state dir is unwritable the
    ORIGINAL exception must still be what the daemon sees, or the traceback that
    explains the death is lost behind an OSError about a counter file."""
    class BrokenStore(FakeStore):
        def bump_infra_deaths(self, key):
            raise OSError("read-only state dir")

    logged: list[str] = []
    cfg = DispatchConfig(fix=FixTaskConfig("o/r", agent_md="AM"), log=logged.append)
    gw = _resolve_gateway()
    d = AgentDispatcher(cfg, gw, BrokenStore(), FakeWorktrees(),
                        RaisingRunner(), Executor(gw))

    with pytest.raises(RuntimeError, match="stream closed"):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert any("infra-death counter not persisted" in m for m in logged)


# ── T3: a GhUnavailable arriving BETWEEN the paid run and the follow-up ────────
# In every leg below the agent session ITSELF succeeds (a real, scripted
# AgentResult comes back with a session id) — the money is spent — and then a
# gh call in the follow-up path raises. The invariants under test: (1) the
# session id the runner returned is persisted (save_session already ran before
# the failing call in every leg here), (2) no counter is corrupted — either
# untouched because the follow-up decision never ran, or bumped exactly once,
# never both/double-counted, (3) nothing was half-applied: the failing gh call
# raises BEFORE its write is recorded, so no partial label state is left
# looking like a real, silent success. `AgentDispatcher.dispatch`'s own
# crash-isolation net (see its except BaseException block) still bumps
# infra-deaths for the SAME key breaker_key/the write side agree on — it can't
# distinguish "the session died" from "a follow-up read/write died", so any
# uncaught exception anywhere in the handler counts as one, which is the
# documented "bump then re-raise unchanged" contract, not a bug introduced by
# T3's fail_on injection.

def test_resolve_mid_dispatch_failure_persists_the_session_before_the_gh_read_fails(tmp_path):
    """resolve: pr_for_issue (the ground-truth check) is called exactly once,
    strictly AFTER save_session — so a GhUnavailable there can't unwind the
    session write. Nothing else in the tail (the retry bump, the relabel) ever
    runs, so resolve-retries stays untouched and no relabel is recorded — the
    issue is left exactly ai-working, which the reaper safely requeues next
    idle tick rather than a half-applied label move."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, fail_on={"pr_for_issue"})
    runner = FakeRunner(AgentResult(is_error=False, status="done", session_id="paid-session"))
    d = _dispatcher(gw, store, runner)

    with pytest.raises(GhUnavailable):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert store.get_session(5) == "paid-session"      # money spent → persisted
    assert store.get_resolve_retries(5) == 0            # follow-up never reached: no false bump
    assert not any(c[0] == "relabel" for c in gw.calls)  # nothing half-applied
    assert store.get_infra_deaths(5) == 1                # dispatcher's crash-isolation net, same key


## NOTE (B5→B4 coordination): `Executor.execute_all`'s exact failure-propagation
## policy (app/execute.py, review S7) was still being actively revised by a
## concurrent sibling agent while these three tests were written — it flipped
## between "swallow every action-level failure" and "run every action, then
## re-raise the first" more than once during this session. Both designs keep
## the invariants T3 actually cares about true (session persisted, the
## already-run counter bump survives, the failed write never landed, the
## label is recoverable via reap next tick); they differ ONLY in whether
## `dispatch()` raises and, as a consequence, whether the dispatcher's OWN
## crash-isolation net additionally bumps infra-deaths, and — for
## address_review specifically — whether the retry bump coded after
## `execute_all` is reached at all. The assertions below match the
## `execute_all` behavior on disk as of THIS commit (full swallow, no
## re-raise); if `app/execute.py` changes again, re-run this file — a
## disagreement here is a real signal, not a stale test.
def test_fix_ci_mid_dispatch_failure_keeps_the_retry_counted_and_the_label_recoverable(tmp_path):
    """fix_ci: ci-retries is bumped BEFORE the follow-up clear-ai-working
    relabel is attempted — the attempt genuinely happened, so the counter must
    survive even though the relabel then fails. `Executor.execute_all` isolates
    the failing action (logs it, does not re-raise), so `dispatch()` completes
    normally instead of the follow-up write being conflated with a pre-result
    session death (infra-deaths — the dispatcher's OTHER, separate
    crash-isolation net — stays untouched). The stuck ai-working label is
    still recoverable: decide_fix's own reap step re-issues exactly the
    relabel that failed, the next time the fixer is idle."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: _ci_pr()}, fail_on={"relabel"})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))  # completes: the bad action is isolated, not raised

    assert store.get_session(5) == "c-live"              # money spent → persisted
    assert store.get_ci_retries(5) == 1                   # the attempt DID count
    assert not any(c[0] == "relabel" for c in gw.calls)    # the clear-ai-working write never landed
    assert store.get_infra_deaths(5) == 0                  # isolated at the action level, not the session level

    # Recoverable, not silently stranded: with ai-working still on the PR, the
    # idle-tick reap step re-issues exactly the relabel that failed.
    reaped = reap_actions(WorldState(prs=(PRView(100, labels=frozenset({Label.AI_WORKING})),)))
    assert Relabel(Target.PR, 100, Label.AI_WORKING, None) in reaped


def test_rebase_mid_dispatch_failure_keeps_the_retry_counted_and_the_label_recoverable(tmp_path):
    """rebase: same ordering as fix_ci (retry bump before the follow-up
    relabel), so the same invariants hold for the CONFLICTING-branch leg."""
    store = Store(state_dir=str(tmp_path))
    pr = PRView(100, head_ref_name="fix/5-x", closing_issue=5, mergeable="CONFLICTING")
    gw = FakeGateway(prs={100: pr}, fail_on={"relabel"})
    runner = FakeRunner(AgentResult(is_error=True, session_id="reb-live"))  # a failed attempt, not an infra death
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.REBASE, 100)))

    assert store.get_session(5) == "reb-live"
    assert store.get_rebase_retries(5) == 1
    assert not any(c[0] == "relabel" for c in gw.calls)
    assert store.get_infra_deaths(5) == 0


def test_address_review_mid_dispatch_failure_still_counts_the_retry(tmp_path):
    """address_review: the review-retry bump is coded AFTER
    `executor.execute_all` (workflows/fix.py's address_review tail). Because
    `execute_all` isolates the failing relabel instead of raising, control
    still reaches the bump: the attempt is correctly counted even though its
    relabel write failed and never landed."""
    store = Store(state_dir=str(tmp_path))
    pr = PRView(100, head_ref_name="fix/5-x", closing_issue=5,
               reviews=(Review(state=REVIEW_CHANGES_REQUESTED, author="human1", submitted_at="T1"),))
    gw = FakeGateway(prs={100: pr}, fail_on={"relabel"})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="ar-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, 100)))

    assert store.get_session(5) == "ar-live"
    assert store.get_review_retry(5) == ("T1", 1)   # the bump still runs (execute_all isolates, doesn't raise)
    assert not any(c[0] == "relabel" for c in gw.calls)
    assert store.get_infra_deaths(5) == 0
