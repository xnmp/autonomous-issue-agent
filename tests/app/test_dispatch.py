"""AgentDispatcher — end-to-end glue per AgentKind, with fakes (no SDK, no gh).

Drives each handler and asserts the resulting gateway writes + persisted state,
proving the dispatcher routes inputs→task→run→follow-up correctly. The DECISIONS
are tested in tests/domain; here we verify the wiring."""

from tests.domain.builders import observed_pr
from tests.fakes.workflow_authority import retained_issue_file, seed_retained_issue_file


from tests.fakes.accounted_workflow import review_retry_pair, seed_review_retry

import asyncio
from dataclasses import replace

import pytest

from issue_agent.adapters.store import Store
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.outcome import AgentResult
from issue_agent.app.planners.tasks import (
    FixTaskConfig,
    ReviewTaskConfig,
)
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.world import (
    Check,
    IssueView,
    PRView,
    Review,
    REVIEW_CHANGES_REQUESTED,
)
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
    return with_review_authority(AgentDispatcher(cfg, gw, store, None, runner, executor))


def _dispatcher(gw, store, runner):
    cfg = DispatchConfig(
        fix=FixTaskConfig("o/r", agent_md="AM"),
        review=ReviewTaskConfig("o/r", review_prompt="round=$ROUND_NUMBER"),
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
    gw = FakeGateway(prs={100: observed_pr(100, head_ref_oid="sha1")})
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
def test_resolve_authority_hold_never_calls_provider(authority):
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
    assert not any(call[0] == "relabel" for call in gw.calls)
    assert any("held" in message for message in logged)


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
    return observed_pr(100, head_ref_name="fix/5-x", closing_issue=5,
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


# ── review C3: a pre-result session death still propagates ─────────────────
def _resolve_gateway():
    return FakeGateway(issues={5: IssueView(5, title="Null token", body="x")},
                       pr_for_issue={})


def test_pre_result_death_re_raises_so_the_daemon_still_isolates_the_crash(tmp_path):
    """Re-raise UNCHANGED: `Daemon.tick`'s catch (issue keeps ai-working,
    reaper requeues) is the crash-isolation contract. Swallowing here would
    silently change it."""
    store = Store(state_dir=str(tmp_path))
    boom = ValueError("transport gone")
    d = _dispatcher(_resolve_gateway(), store, RaisingRunner(boom))

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(d.dispatch(RunAgent(AgentKind.RESOLVE, 5)))

    assert excinfo.value is boom


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


## NOTE (B5→B4 coordination): `Executor.execute_all`'s exact failure-propagation
## policy (app/execute.py, review S7) was still being actively revised by a
## concurrent sibling agent while these three tests were written — it flipped
## between "swallow every action-level failure" and "run every action, then
## re-raise the first" more than once during this session. Both designs keep
## the invariants T3 actually cares about true (session persisted, the
## already-run counter bump survives, the failed write never landed, the
## label is recoverable via reap next tick); they differ ONLY in whether
## `dispatch()` raises and — for
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
    death. The dropped ai-blocked write is self-correcting: the next
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


def test_rebase_mid_dispatch_failure_keeps_the_retry_counted(tmp_path):
    """rebase: same ordering as fix_ci (retry bump before the follow-up
    relabel), so the same invariants hold for the CONFLICTING-branch leg."""
    store = Store(state_dir=str(tmp_path))
    pr = observed_pr(100, head_ref_name="fix/5-x", closing_issue=5, mergeable="CONFLICTING")
    gw = FakeGateway(prs={100: pr}, fail_on={"relabel"})
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="reb-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.REBASE, 100)))

    assert pr_repair_state(store).session.handle == "reb-live"
    assert issue_recovery_state(store).rebase_failures == 1
    assert not any(c[0] == "relabel" for c in gw.calls)


def test_address_review_mid_dispatch_failure_still_applies_the_rest_of_the_tail(tmp_path):
    """address_review on a BLOCKED envelope emits an ask + ai-blocked. When the
    ask fails mid-tail, `execute_all` isolates it and the ai-blocked relabel —
    the action that actually stops re-dispatch — still lands. (Blocked burns no
    retry by design: waiting on a human is not a failed attempt.)"""
    store = Store(state_dir=str(tmp_path))
    pr = observed_pr(100, head_ref_name="fix/5-x", closing_issue=5,
               reviews=(Review(state=REVIEW_CHANGES_REQUESTED, author="human1", submitted_at="2026-01-01T00:00:01Z"),))
    gw = FakeGateway(prs={100: pr}, fail_on={"comment"})
    runner = FakeRunner(AgentResult(is_error=False, status="blocked", session_id="ar-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, 100)))

    assert pr_repair_state(store).session.handle == "ar-live"
    assert ("relabel", "pr", 100, None, "ai-blocked") in gw.calls
    assert review_retry_pair(store)[1] == 0


def test_address_review_success_counts_the_retry_and_touches_no_label(tmp_path):
    """The ok path's whole tail is the counter (review M22/D9): the bump is coded
    AFTER `executor.execute_all`, and with nothing left to execute it must still
    run — the review-retry breaker is the ONLY thing bounding this leg now."""
    store = Store(state_dir=str(tmp_path))
    pr = observed_pr(100, head_ref_name="fix/5-x", closing_issue=5,
               reviews=(Review(state=REVIEW_CHANGES_REQUESTED, author="human1", submitted_at="2026-01-01T00:00:01Z"),))
    gw = FakeGateway(prs={100: pr})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="ar-live"))
    d = _ci_dispatcher(gw, store, runner)

    asyncio.run(d.dispatch(RunAgent(AgentKind.ADDRESS_REVIEW, 100)))

    assert pr_repair_state(store).session.handle == "ar-live"
    assert review_retry_pair(store) == ("2026-01-01T00:00:01Z", 1)
    assert not any(c[0] == "relabel" for c in gw.calls)  # no gh write at all, not a failed one


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
    pr = observed_pr(100, head_ref_name="fix/5-x", closing_issue=5)
    gw = FakeGateway(prs={100: pr}, fail_on={"issue_for_pr"})
    runner = FakeRunner(AgentResult(is_error=False, status="pushed", session_id="c5"))
    action = RunAgent(kind, 100)
    d = _ci_dispatcher(
        gw, store, runner,
        dispatch_claim_target=lambda candidate: -100 if candidate == action else None,
    )

    asyncio.run(d.dispatch(action))

    assert runner.tasks == []
    assert retained_issue_file(store, 5, "session") == ""
    assert gw.calls == []
