"""AgentDispatcher — end-to-end glue per AgentKind, with fakes (no SDK, no gh).

Drives each handler and asserts the resulting gateway writes + persisted state,
proving the dispatcher routes inputs→task→run→follow-up correctly. The DECISIONS
are tested in tests/domain; here we verify the wiring."""

from tests.fakes.accounted_workflow import review_retry_pair, seed_review_retry

import asyncio
import hashlib
from dataclasses import replace

import pytest

from issue_agent.adapters.github import GhUnavailable
from issue_agent.adapters.store import Store
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.resolve_split import ContractResetResult
from issue_agent.app.planners.tasks import (
    FixTaskConfig,
    JiraTaskConfig,
    ReviewTaskConfig,
    TriageTaskConfig,
)
from issue_agent.adapters.runtime_plan import McpServer
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.triage import TriageConfig
from issue_agent.domain.world import (
    Check,
    Comment,
    IssueView,
    PRView,
    Review,
    REVIEW_CHANGES_REQUESTED,
)
from issue_agent.domain.labels import Label
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway
from tests.fakes.review_authority import with_review_authority, review_state
from tests.fakes.accounted_dispatch import with_resolve_authority
from tests.fakes.accounted_workflow import issue_recovery_state
from tests.fakes.accounted_workflow import resolve_state, pr_repair_state
from tests.fakes.accounted_workflow import attach_workflow_authority


class FakeStore:
    def __init__(self):
        self.sessions = {}
        self.pr_sessions = {}
        self.triage_at = {}
        self.groom_at = {}
        self.groom_content = {}
        self.last_sha = {}
        self.last_patch_id = {}
        self.rounds = {}
        self.resumes = {}
        self.pr_resumed_at = {}
        self.phases = {}
        self.infra_deaths = {}
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

    def set_pr_resumed_at(self, pr, at):
        self.pr_resumed_at[pr] = at

    def get_pr_resumed_at(self, pr):
        return self.pr_resumed_at.get(pr, "")



    def set_triage_at(self, i, at):
        self.triage_at[i] = at

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

    def clear_infra_deaths(self, key):
        self.infra_deaths.pop(key, None)


class FakeWorktrees:
    def __init__(self):
        self.resets = []

    def ensure(self, issue, branch):
        return f"/wt/issue-{issue}"

    def remove(self, issue):
        pass

    def reset_contract(self, issue, revision, intent_id):
        self.resets.append(issue)
        digest = hashlib.sha256(intent_id.encode()).hexdigest()
        return ContractResetResult(
            revision, f"refs/issue-agent/contract-reset/{digest}"
        )

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
    return with_review_authority(AgentDispatcher(cfg, gw, store, None, runner, executor))


def _dispatcher(gw, store, runner):
    cfg = DispatchConfig(
        fix=FixTaskConfig("o/r", agent_md="AM"),
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER"),
        triage=TriageTaskConfig("steward-bot", triage_prompt="T"),
        triage_cfg=TriageConfig("steward-bot", enabled=True),
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return with_review_authority(with_resolve_authority(AgentDispatcher(cfg, gw, store, FakeWorktrees(), runner, executor)))


def _delegated_dispatcher(gw, store, runner):
    cfg = DispatchConfig(
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER", scout_model="sonnet"),
        review_mode=ReviewMode.DELEGATED,
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return with_review_authority(AgentDispatcher(cfg, gw, store, None, runner, executor))


def test_delegated_review_routes_to_workflow():
    gw = FakeGateway(prs={100: PRView(100, head_ref_oid="sha1")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, structured={
        "decision": "APPROVE", "body": "lgtm"}, session_id="rs"))
    d = _delegated_dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))
    assert any(c[0] == "review" for c in gw.calls)
    assert store.pr_sessions == {}


def test_resolve_routes_to_fix_workflow():
    gw = FakeGateway(issues={5: IssueView(5, title="Null token", body="x")},
                     pr_for_issue={5: 100})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False, status="pr_opened", session_id="s5"))
    d = _dispatcher(gw, store, runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))
    assert resolve_state(store).episode.continuation.session.session_id == "s5"
    assert ("relabel", "issue", 5, "ai-working", "ai-pr-open") in gw.calls
    assert ("relabel", "pr", 100, None, "ai-pr-open") in gw.calls


@pytest.mark.parametrize("authority", ["missing", "uncaptured", "stale"])
def test_resolve_authority_hold_never_calls_provider_or_charges_infra(authority):
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")})
    store = FakeStore()
    runner = FakeRunner(AgentResult(is_error=False))
    logged = []
    control = None if authority == "missing" else attach_workflow_authority(gw, store)
    action = RunAgent(AgentKind.RESOLVE, 5)
    if authority == "stale":
        binding = resolve_state(store).binding
        action = replace(action, workflow=replace(binding, fence=replace(binding.fence, generation=0)))
    dispatcher = AgentDispatcher(
        DispatchConfig(fix=FixTaskConfig("o/r", agent_md="AM", model="m"), log=logged.append),
        gw, store, FakeWorktrees(), runner, Executor(gw), workflow_control=control,
    )
    asyncio.run(dispatcher.dispatch(action))
    assert not runner.tasks
    assert store.get_infra_deaths(5) == 0
    assert not any(call[0] == "relabel" for call in gw.calls)
    assert any("held" in message for message in logged)


def _split_dispatcher(gw, store, runner, wt=None, max_contract_rounds=2):
    cfg = DispatchConfig(
        # Pinned model: split tests script exact run sequences; an unpinned
        # config would prepend the difficulty-router leg (see test_fix_workflow).
        fix=FixTaskConfig("o/r", agent_md="AM", model="m"),
        author_tests_split=True, max_contract_rounds=max_contract_rounds,
    )
    wt = wt or FakeWorktrees()
    executor = Executor(gw, remove_worktree=wt.remove)
    return with_resolve_authority(AgentDispatcher(cfg, gw, store, wt, runner, executor))


def test_split_resolve_routes_to_fix_workflow():
    gw = FakeGateway(
        issues={5: IssueView(5, title="Null token", body="x")},
        prs={100: PRView(100)},
        pr_for_issue={},
    )
    store, wt = FakeStore(), FakeWorktrees()

    class PrOpeningRunner(SeqRunner):
        async def run(self, task):
            result = await super().run(task)
            if result.status == "pr_opened":
                gw._pr_for_issue[5] = 100
            return result

    runner = PrOpeningRunner([
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
    assert store.pr_sessions == {}


def test_triage_routes_to_steward_workflow():
    iss = IssueView(7, title="Add export", body="want it", author="alice",
                    comments=(Comment("alice", "please add export"),), updated_at="2026-09-14T10:00:00+00:00")
    gw = FakeGateway(issues={7: iss})
    store = FakeStore()
    runner = FakeRunner(AgentResult(
        is_error=False,
        structured={"verdict": "blocked", "plain_summary": "Need detail",
                    "creator_questions": ["Which format?"], "owner_questions": [],
                    "acceptance_criteria": []},
    ))
    d = _dispatcher(gw, store, runner)
    from dataclasses import fields
    from tests.fakes.accounted_steward import AccountedStewardWorkflow, triage_lane
    original = d.steward_workflow
    d.steward_workflow = AccountedStewardWorkflow(**{field.name: getattr(original, field.name) for field in fields(original)})
    asyncio.run(d.dispatch(RunAgent(AgentKind.TRIAGE, 7)))
    assert ("relabel", "issue", 7, None, "ai-triage") in gw.calls
    assert triage_lane(store, 7).baseline_at == iss.updated_at


def _ci_dispatcher(
    gw, store, runner, fix_mcp_config="", dispatch_claim_target=None,
    operator_guidance=None,
):
    cfg = DispatchConfig(
        fix=FixTaskConfig("o/r", agent_md="AM"),
        fix_mcp_config=fix_mcp_config, harness_root="/tmp/none",
        operator_guidance=operator_guidance or (lambda pr: review_state(store).guidance.get(pr, "")),
    )
    executor = Executor(gw, remove_worktree=FakeWorktrees().remove)
    return with_resolve_authority(AgentDispatcher(
        cfg,
        gw,
        store,
        FakeWorktrees(),
        runner,
        executor,
        dispatch_claim_target=dispatch_claim_target,
    ))


def _ci_pr():
    return PRView(100, head_ref_name="fix/5-x", closing_issue=5,
                  checks=(Check("SonarQube Code Analysis", bucket="fail"),))


def test_fix_ci_routes_to_fix_workflow():
    gw = FakeGateway(prs={100: _ci_pr()})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))
    d = _ci_dispatcher(gw, FakeStore(), runner)
    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))
    assert "SonarQube Code Analysis" in runner.tasks[0].prompt


def test_fix_ci_uses_observed_claim_issue_when_live_lookup_fails():
    gw = FakeGateway(
        prs={100: _ci_pr()},
        fail_on={"issue_for_pr"},
    )
    runner = FakeRunner(
        AgentResult(is_error=False, status="pushed", session_id="c5")
    )
    store = FakeStore()
    action = RunAgent(AgentKind.FIX_CI, 100)
    d = _ci_dispatcher(
        gw,
        store,
        runner,
        dispatch_claim_target=lambda candidate: (
            5 if candidate == action else None
        ),
    )

    asyncio.run(d.dispatch(action))

    assert "SonarQube Code Analysis" in runner.tasks[0].prompt
    assert pr_repair_state(store).session.handle == "c5"


def test_fix_ci_defers_when_fresh_pr_linkage_disagrees_with_claim():
    changed = replace(
        _ci_pr(),
        head_ref_name="fix/6-moved",
        closing_issue=6,
    )
    gw = FakeGateway(prs={100: changed})
    runner = FakeRunner(
        AgentResult(is_error=False, status="pushed", session_id="wrong")
    )
    store = FakeStore()
    action = RunAgent(AgentKind.FIX_CI, 100)
    d = _ci_dispatcher(
        gw,
        store,
        runner,
        dispatch_claim_target=lambda candidate: (
            5 if candidate == action else None
        ),
    )

    asyncio.run(d.dispatch(action))

    assert runner.tasks == []
    assert store.sessions == {}


# ── Jira lifecycle sync ─────────────────────────────────────────────────────────
def _jira_dispatcher(gw, store, runner):
    jira = JiraTaskConfig(
        project_key="ABC", repo="o/r",
        mcp_servers=(McpServer(name="atlassian", config={"type": "http"}, tools="mcp__atlassian__*"),),
    )
    cfg = DispatchConfig(jira=jira)
    executor = Executor(gw)
    return with_review_authority(AgentDispatcher(cfg, gw, store, None, runner, executor))


def test_jira_dispatch_keeps_command_attribution_separate_from_authority():
    owner = "o/r:steward:deployment-1"
    cfg = DispatchConfig(
        repo="o/r",
        command_identity=f"{owner}:jira",
        deployment_owner_id=owner,
    ).jira_workflow_config()

    assert cfg.command_identity == f"{owner}:jira"
    assert cfg.deployment_owner_id == owner


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
    assert "Reconcile or create the Jira ticket" in runner.tasks[0].prompt
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
    d = with_resolve_authority(AgentDispatcher(cfg, gw, FakeStore(), FakeWorktrees(), runner, executor))
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


# ── deterministic deaths must not buy the same unwinnable dispatch again (#13) ─
_BUFFER_DEATH = RuntimeError(
    "Fatal error in message reader: Failed to decode JSON: JSON message "
    "exceeded maximum buffer size of 1048576 bytes"
)


def _capped_dispatcher(gw, store, runner, cap):
    cfg = DispatchConfig(
        review=ReviewTaskConfig("o/r", review_prompt="r"),
        max_infra_deaths=cap,
    )
    return with_review_authority(AgentDispatcher(
        cfg, gw, store, None, runner, Executor(gw, remove_worktree=FakeWorktrees().remove)
    ))


def test_a_deterministic_death_spends_the_whole_retry_budget_at_once(tmp_path):
    """The input, not the infrastructure, killed this session — the next
    dispatch reads the same file and dies identically. Charging it 1 buys
    cap-1 more unwinnable sessions at full cost, which is what happened on
    xnmp/tauri-explorer PR #552."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="feat/x")})
    d = _capped_dispatcher(gw, store, RaisingRunner(_BUFFER_DEATH), cap=5)

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert store.get_infra_deaths(-100) == 5, "one death, the whole budget"


def test_a_deterministic_death_reaches_the_reviewers_existing_ceiling(tmp_path):
    """The admission policy holds the PR after a deterministic paid failure."""
    from issue_agent.domain.review import ReviewConfig, ReviewExcluded, review_eligibility

    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="feat/x")})
    d = _capped_dispatcher(gw, store, RaisingRunner(_BUFFER_DEATH), cap=5)

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    cfg = ReviewConfig(review_bot="rb", max_unproductive_rounds=5)
    decision = review_eligibility(gw.get_pr(100), cfg, store.get_infra_deaths(-100))
    assert isinstance(decision, ReviewExcluded) and decision.breaker


def test_an_ordinary_transport_death_still_costs_exactly_one_life(tmp_path):
    """Anti-vacuity. A stochastic death is what the breakers were built for; if
    the classifier widened to cover it, every flaky network blip would escalate
    a PR a retry would have rescued."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="feat/x")})
    d = _capped_dispatcher(gw, store, RaisingRunner(RuntimeError("Stream closed")), cap=5)

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert store.get_infra_deaths(-100) == 1


def test_a_deterministic_death_never_lowers_an_existing_tally(tmp_path):
    store = Store(state_dir=str(tmp_path))
    for _ in range(7):
        store.bump_infra_deaths(-100)
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="feat/x")})
    d = _capped_dispatcher(gw, store, RaisingRunner(_BUFFER_DEATH), cap=5)

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert store.get_infra_deaths(-100) == 7


def test_without_a_configured_ceiling_a_deterministic_death_bumps_as_before(tmp_path):
    """cap 0 means the role's breaker is off; saturating to 0 would ERASE the
    tally, so the ordinary bump must stand."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="feat/x")})
    d = _capped_dispatcher(gw, store, RaisingRunner(_BUFFER_DEATH), cap=0)

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert store.get_infra_deaths(-100) == 1


def test_a_deterministic_death_still_re_raises_unchanged(tmp_path):
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="feat/x")})
    d = _capped_dispatcher(gw, store, RaisingRunner(_BUFFER_DEATH), cap=5)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert excinfo.value is _BUFFER_DEATH


# ── review P1R-1: a flaky issue lookup must not relocate the counter ──────────
# The trap is the COUPLING: the flakiness that kills sessions is the flakiness
# that fails `gateway.issue_for_pr`, so the old catch-everything→None→`-pr`
# fallback moved the infra-death counter into a key space the fix role's decide
# side never reads — the ceiling failed exactly when it was needed.
class FlakyIssueLookup(FakeGateway):
    """`issue_for_pr` raises on the listed call numbers (1-based); `always`
    raises on every call. Counts its calls so a test can pin the retry."""

    def __init__(self, prs, issue, fail_calls=(), always=False):
        super().__init__(prs=prs)
        self._issue = issue
        self._fail_calls = set(fail_calls)
        self._always = always
        self.lookups = 0

    def issue_for_pr(self, pr):
        self.lookups += 1
        if self._always or self.lookups in self._fail_calls:
            raise GhUnavailable("network", "issue_for_pr timed out (test-injected)")
        return self._issue


def _review_pr():
    return PRView(100, head_ref_name="fix/5-x", closing_issue=5)


def test_review_death_stays_pr_keyed_when_issue_lookup_fails(tmp_path):
    """Review convergence belongs to the PR, independently of linkage reads."""
    store = Store(state_dir=str(tmp_path))
    gw = FlakyIssueLookup(prs={100: _review_pr()}, issue=5, fail_calls={1})
    d = _dispatcher(gw, store, RaisingRunner())

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert gw.lookups == 1
    assert store.get_infra_deaths(5) == 0
    assert store.get_infra_deaths(-100) == 1


def test_repeated_review_deaths_share_one_pr_key_across_lookup_outcomes(tmp_path):
    store = Store(state_dir=str(tmp_path))
    gw = FlakyIssueLookup(prs={100: _review_pr()}, issue=5, fail_calls=set(range(2, 99)))
    d = _dispatcher(gw, store, RaisingRunner())

    for _ in range(6):
        with pytest.raises(RuntimeError):
            asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert store.get_infra_deaths(5) == 0
    assert store.get_infra_deaths(-100) == 6, "one stable key, six deaths"


def test_review_death_needs_no_retry_lookup_to_choose_its_pr_key(tmp_path):
    store = Store(state_dir=str(tmp_path))
    logged: list[str] = []
    gw = FlakyIssueLookup(prs={100: _review_pr()}, issue=5, always=True)
    cfg = DispatchConfig(review=ReviewTaskConfig("o/r"), log=logged.append)
    d = with_review_authority(AgentDispatcher(cfg, gw, store, None, RaisingRunner(), Executor(gw)))

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert store.get_infra_deaths(-100) == 1
    assert any("could not resolve issue for PR #100" in m for m in logged)


def test_a_clean_none_is_still_a_genuinely_issueless_pr(tmp_path):
    """Anti-vacuity twin: a lookup that ANSWERS None is a fact, not an outage.
    `-pr` is exactly `breaker_key`'s convention for it, and treating that as
    unknown would strand every Jira-keyed PR's counter."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: PRView(100, head_ref_name="feat/ACME-2468-x")})
    d = _dispatcher(gw, store, RaisingRunner())

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.REVIEW, 100)))

    assert store.get_infra_deaths(-100) == 1


# ── review P1R-3: an operator stopping the daemon is not an infra death ───────
class InterruptingRunner:
    def __init__(self, exc):
        self.exc = exc
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        raise self.exc


@pytest.mark.parametrize(
    "exc", [KeyboardInterrupt(), SystemExit(0), asyncio.CancelledError()],
    ids=["ctrl-c", "sys.exit", "cancelled"],
)
def test_stopping_the_daemon_burns_no_infra_death_life(tmp_path, exc):
    """review P1R-3. `issue-agent start` runs in the FOREGROUND and Ctrl-C is
    the documented way to stop it, so counting these meant five ordinary stops
    escalated a healthy issue to needs-human-review — while a SIGTERM (systemd's
    stop signal, which raises nothing) counted zero. CancelledError is in the
    list because `asyncio.run` turns a Ctrl-C at an await point into exactly
    that before it re-raises KeyboardInterrupt to the caller."""
    store = Store(state_dir=str(tmp_path))
    d = _dispatcher(_resolve_gateway(), store, InterruptingRunner(exc))

    with pytest.raises(type(exc)):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert store.get_infra_deaths(5) == 0


def test_five_operator_stops_do_not_escalate_a_healthy_issue(tmp_path):
    """The repro, at the ceiling: five Ctrl-Cs used to be five 'deaths'."""
    store = Store(state_dir=str(tmp_path))
    d = _dispatcher(_resolve_gateway(), store, InterruptingRunner(KeyboardInterrupt()))

    for _ in range(5):
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert store.get_infra_deaths(5) == 0


def test_an_ordinary_exception_is_still_counted(tmp_path):
    """Anti-vacuity twin: exempting the interrupt family must not exempt the
    transport death the counter exists for."""
    store = Store(state_dir=str(tmp_path))
    d = _dispatcher(_resolve_gateway(), store, InterruptingRunner(RuntimeError("stream gone")))

    with pytest.raises(RuntimeError):
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert store.get_infra_deaths(5) == 1


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
    d = with_resolve_authority(AgentDispatcher(cfg, gw, BrokenStore(), FakeWorktrees(),
                        RaisingRunner(), Executor(gw)))

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
    """The paid handle survives uncertainty without granting another dispatch.

    Raw evidence is durable before observation; continuation is deliberately
    unchanged until classification, so blocked RESUME can still clear its handle.
    """
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, fail_on={"pr_for_issue"})
    runner = FakeRunner(AgentResult(is_error=False, status="done", session_id="paid-session"))
    d = _dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    with patch.object(store._workflow_db, "_now", return_value=datetime.now(timezone.utc) + timedelta(hours=1)):
        pending = store._workflow_authority.pending_returns()
    assert len(pending) == 1
    assert pending[0].returned.session.session_id == "paid-session"
    assert resolve_state(store).gates
    assert resolve_state(store).episode.continuation.session is None
    assert resolve_state(store).episode.failures == 0  # unknown PR outcome is not absence
    assert not any(c[0] == "relabel" for c in gw.calls)  # nothing half-applied
    assert store.get_infra_deaths(5) == 0  # observation failed, not the paid provider


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
## The three legs below run on a BLOCKED envelope because that is now the only
## PR-leg outcome that writes a label at all: review M22/D9 deleted the
## clear-ai-working relabel every branch used to open with (nothing in the SDK
## port ever ADDED ai-working to a PR, so it removed nothing). A leg with no
## follow-up write can't exercise a follow-up-write failure.
def test_fix_ci_mid_dispatch_failure_keeps_the_retry_counted(tmp_path):
    """fix_ci: ci-retries is bumped BEFORE the follow-up label write is
    attempted — the attempt genuinely happened, so the counter must survive even
    though the write then fails. `Executor.execute_all` isolates the failing
    action (logs it, does not re-raise), so `dispatch()` completes normally
    instead of the follow-up write being conflated with a pre-result session
    death (infra-deaths — the dispatcher's OTHER, separate crash-isolation net —
    stays untouched). The dropped ai-blocked write is self-correcting: the next
    poll sees the same red CI and re-dispatches, bounded by the very counter
    this test proves advanced."""
    store = Store(state_dir=str(tmp_path))
    gw = FakeGateway(prs={100: _ci_pr()}, fail_on={"relabel"})
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="c-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.FIX_CI, 100)))  # completes: the bad action is isolated, not raised

    assert pr_repair_state(store).session.handle == "c-live"
    assert issue_recovery_state(store).ci_failures == 1                   # the attempt DID count
    assert not any(c[0] == "relabel" for c in gw.calls)    # the ai-blocked write never landed
    assert store.get_infra_deaths(5) == 0                  # isolated at the action level, not the session level


def test_rebase_mid_dispatch_failure_keeps_the_retry_counted(tmp_path):
    """rebase: same ordering as fix_ci (retry bump before the follow-up
    relabel), so the same invariants hold for the CONFLICTING-branch leg."""
    store = Store(state_dir=str(tmp_path))
    pr = PRView(100, head_ref_name="fix/5-x", closing_issue=5, mergeable="CONFLICTING")
    gw = FakeGateway(prs={100: pr}, fail_on={"relabel"})
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="reb-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.REBASE, 100)))

    assert store.get_session(5) == "reb-live"
    assert store.get_rebase_retries(5) == 1
    assert not any(c[0] == "relabel" for c in gw.calls)
    assert store.get_infra_deaths(5) == 0


def test_address_review_mid_dispatch_failure_still_applies_the_rest_of_the_tail(tmp_path):
    """address_review on a BLOCKED envelope emits an ask + ai-blocked. When the
    ask fails mid-tail, `execute_all` isolates it and the ai-blocked relabel —
    the action that actually stops re-dispatch — still lands. (Blocked burns no
    retry by design: waiting on a human is not a failed attempt.)"""
    store = Store(state_dir=str(tmp_path))
    pr = PRView(100, head_ref_name="fix/5-x", closing_issue=5,
               reviews=(Review(state=REVIEW_CHANGES_REQUESTED, author="human1", submitted_at="2026-01-01T00:00:01Z"),))
    gw = FakeGateway(prs={100: pr}, fail_on={"comment"})
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="ar-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, 100)))

    assert pr_repair_state(store).session.handle == "ar-live"
    assert ("relabel", "pr", 100, None, "ai-blocked") in gw.calls
    assert review_retry_pair(store)[1] == 0
    assert store.get_infra_deaths(5) == 0


def test_address_review_success_counts_the_retry_and_touches_no_label(tmp_path):
    """The ok path's whole tail is the counter (review M22/D9): the bump is coded
    AFTER `executor.execute_all`, and with nothing left to execute it must still
    run — the review-retry breaker is the ONLY thing bounding this leg now."""
    store = Store(state_dir=str(tmp_path))
    pr = PRView(100, head_ref_name="fix/5-x", closing_issue=5,
               reviews=(Review(state=REVIEW_CHANGES_REQUESTED, author="human1", submitted_at="2026-01-01T00:00:01Z"),))
    gw = FakeGateway(prs={100: pr})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="ar-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, 100)))

    assert pr_repair_state(store).session.handle == "ar-live"
    assert review_retry_pair(store) == ("2026-01-01T00:00:01Z", 1)
    assert not any(c[0] == "relabel" for c in gw.calls)  # no gh write at all, not a failed one
    assert store.get_infra_deaths(5) == 0


@pytest.mark.parametrize(
    "kind",
    [AgentKind.PR_RESUME, AgentKind.ADDRESS_REVIEW, AgentKind.FIX_CI, AgentKind.REBASE],
)
def test_fix_pr_kind_defers_without_dispatch_when_claim_target_carries_no_issue(kind, tmp_path):
    """ADR 0008: a reserved claim that resolves to a negative (issueless) target
    must fence the dispatch entirely — no handler call, no live gh lookup to
    re-derive an identity that was never claimed. `fail_on={"issue_for_pr"}`
    proves the live lookup path is never taken (it would raise if it were)."""
    store = Store(state_dir=str(tmp_path))
    pr = PRView(100, head_ref_name="fix/5-x", closing_issue=5)
    gw = FakeGateway(prs={100: pr}, fail_on={"issue_for_pr"})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))
    action = RunAgent(kind, 100)
    d = _ci_dispatcher(
        gw, store, runner,
        dispatch_claim_target=lambda candidate: -100 if candidate == action else None,
    )

    asyncio.run(d.dispatch(action))

    assert runner.tasks == []
    assert store.get_session(5) == ""
    assert store.get_infra_deaths(5) == 0
    assert gw.calls == []
