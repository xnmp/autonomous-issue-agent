"""Loop-convergence property for fix orchestration (T3), driven end-to-end
through the REAL application and adapter layers (review D13).

`decide_fix` is edge-triggered: it dispatches a PAID agent session when the world
looks a certain way, and the ONLY thing that stops it re-dispatching on the next
poll is state the daemon itself writes back — a label move, a resumed-at mark, a
retry counter. Every historical money-burn in FAILURE-MODES (#13, #14, #39, #64,
V1, V2) has the same shape: one of those write-backs was missing, mis-keyed, or
compared against a different fact than the read side, so an UNCHANGED world
re-fired a full session every tick, forever.

This file tests that shape rather than one instance of it. Each scenario runs the
REAL `app.loop.Daemon` tick loop — `decide_fix` → `Executor.execute` →
`AgentDispatcher.dispatch` → the REAL `app/workflows/fix.py` → real SQLite
workflow/admission authority and the remaining `Store(tmp_path)` state — against
a FROZEN GitHub world (a `_SimGateway` double whose
only behaviour beyond plain data is applying the daemon's OWN `relabel`/
`close_issue` writes, exactly as GitHub itself would). The agent's own run is the
pathological case where it accomplishes nothing (no push, no PR, no comment);
the read side (`app.roles._fix_state`) is imported from production, not
reimplemented, so both halves of the decide/write pair are the actual code.

Asserted property: the number of PAID ``RunAgent`` dispatches over an unbounded
poll loop is capped by the relevant circuit breaker, and `decide_fix` then
settles on a STABLE, RunAgent-free fixed point. Repeated $0 actions (an
idempotent auto-merge arm) are acceptable; repeated sessions are not.

review D13 / T2 (docs/REVIEW-2026-07-25-adversarial.md §2, §5): the previous
version of this file hand-mirrored `app/workflows/fix.py`'s decide/write pairing
in a private `_after_run` — a description of the workflow, not the workflow. The
review mutation-proved the gap: deleting the workflow's resolve-breaker bump left
this suite 10/10 green (only `test_fix_workflow.py` caught it), and `_after_run`'s
own `:189` comment described an unconditional CI bump the workflow had already
stopped performing. Driving the real `FixWorkflow`/`Store`/`AgentDispatcher`
means that class of drift now fails THIS suite too — see the module's final
report for the mutation re-run that confirms it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest
from tests.domain.builders import Check, Comment, Review, issue, pr, world

from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.effects import (
    GitHubEffectExecutor,
    ImmediateGitHubEffectExecutor,
)
from issue_agent.adapters.store import Store
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.app import roles
from issue_agent.app.dispatch import AgentDispatcher, DispatchConfig
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.loop import Daemon
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.review_runtime import review_guidance_reader
from issue_agent.domain import outcome
from issue_agent.domain.actions import Action, AgentKind, Relabel, RunAgent, Target
from issue_agent.domain.contracts import Money, RunId, WorkflowAttemptIdentity
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.pricing import BudgetPolicy
from issue_agent.domain.refs import issue_from_branch
from issue_agent.domain.world import IssueView, PRView, WorldState
from issue_agent.domain.workflow import FreshResolve, ResolveSession
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey, WorkflowWriter
from tests.fakes.review_workflow import held as held_review
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery

CFG = FixConfig(fix_bot="fix-bot")

# Well past every cap in CFG (3) — a bounded leg must be quiet long before this.
TICKS = 12
# How many trailing ticks must be identical to call the loop settled.
STABLE_TAIL = 3

# The pathological agent run: it errors, and — critically — it changed NOTHING on
# GitHub (no push, no PR, no comment). is_error=True → Outcome.ERROR; session_id
# is empty, matching a run that died before the SDK returned one.
ERRORED_RUN = outcome.AgentResult(is_error=True)


@pytest.fixture(autouse=True)
def controlled_publication_context(monkeypatch):
    monkeypatch.setattr(
        accounting_runtime, "_fix_publication_context",
        lambda _role, cwd: accounting_runtime._FixPublicationContext(
            cwd, f"fix/{cwd.rsplit('-', 1)[-1]}-test", "/usr/bin/git", "/usr/bin/gh", "/usr/bin:/bin",
        ),
    )


# ── adapter doubles ──────────────────────────────────────────────────────────
# Everything past this point that used to be a hand-written mirror of decide/
# write logic is now the REAL `app.workflows.fix.FixWorkflow` /
# `app.dispatch.AgentDispatcher` / `adapters.store.Store` / `app.roles._fix_state`.
# The only things this file still fakes are: the GitHub transport (a PR/issue
# store that must persist a `relabel`/`close_issue` write so the NEXT poll
# observes it — GitHub does exactly that) and the worktree/agent-runner I/O.
class _FakeWorktrees:
    def ensure(self, issue: int, branch: str) -> str:
        return f"/wt/issue-{issue}"

    def remove(self, issue: int) -> None:
        pass

    def reset(self, issue: int) -> None:
        pass

    def branch_for_issue(self, issue: int, fallback: str, *, preferred: str = "") -> str:
        del issue
        return preferred or fallback


class _FakeRunner:
    """Every dispatch in this file scripts the SAME pathological result — the
    agent errors and changes nothing on GitHub — unless overridden."""

    def __init__(self, result: outcome.AgentResult = ERRORED_RUN):
        self.result = result
        self.tasks: list = []

    async def run(self, task):
        self.tasks.append(task)
        return self.result


class _RaisingRunner:
    """The C3 shape (review): the session dies BEFORE `runner.run` returns, so
    the run RAISES rather than returning a result — the whole workflow tail
    (save_session, the retry bump, the follow-up actions) never executes."""

    def __init__(self):
        self.tasks: list = []

    async def run(self, task):
        self.tasks.append(task)
        raise RuntimeError("stream closed before any result")


def _find(views, number: int):
    for view in views:
        if view.number == number:
            return view
    raise AssertionError(f"scenario world has no #{number}")


def _issue_for(p: PRView) -> int | None:
    """The SAME fallback the real gateway and `domain.fix.breaker_key` use:
    prefer the closing-reference, else the branch-encoded issue number
    (adapters/github.py:823-829). Re-implemented here (rather than imported)
    so this double stays a plain data object — the exact convention
    `tests/adapters/test_roles.py`'s own `FakeGateway` already uses for the
    same reason, including for the C4 key-agreement property this suite also
    depends on."""
    if p.closing_issue is not None:
        return p.closing_issue
    return issue_from_branch(p.head_ref_name)


def _moved(labels: frozenset[Label], remove: Label | None, add: Label | None) -> frozenset[Label]:
    out = set(labels)
    if remove is not None:
        out.discard(remove)
    if add is not None:
        out.add(add)
    return frozenset(out)


class _SimGateway:
    """A GitHubGateway double whose `relabel`/`close_issue` MUTATE the observed
    world. That mutation is not a decision-logic mirror (the thing D13 removes)
    — it is what GitHub itself does when the real gateway is asked to write,
    and it has to persist here for the identical reason it must in production:
    so the NEXT poll's `build_world()` observes it. Every actual DECISION
    (which counter moves, which follow-up action fires, whether a breaker
    trips) is made by the real `FixWorkflow`/`decide_fix`/`Store` from here
    down."""

    def __init__(self, world_state: WorldState):
        self._revision = 0
        self.world = replace(
            world_state,
            issues=tuple(
                issue
                if issue.updated_at
                else replace(
                    issue,
                    updated_at=self._next_issue_revision(issue.number),
                )
                for issue in world_state.issues
            ),
        )
        self.calls: list = []

    def _next_issue_revision(self, number: int) -> str:
        self._revision += 1
        return f"fixture-revision-{number}-{self._revision}"

    def build_world(self) -> WorldState:
        return self.world

    def get_issue(self, n: int) -> IssueView:
        return _find(self.world.issues, n)

    def get_pr(
        self, n: int, *, mergeability: bool = True, checks: bool = True
    ) -> PRView:
        del mergeability, checks
        return _find(self.world.prs, n)

    def has_label(self, kind: str, number: int, label: str) -> bool:
        view = self.get_issue(number) if kind == "issue" else self.get_pr(number)
        return Label(label) in view.labels

    def pr_for_issue(self, issue_number: int) -> int | None:
        for p in self.world.prs:
            if p.state == "OPEN" and _issue_for(p) == issue_number:
                return p.number
        return None

    def issue_for_pr(self, pr_number: int) -> int | None:
        return _issue_for(self.get_pr(pr_number))

    def relabel(self, kind: str, number: int, remove, add) -> None:
        self.calls.append(("relabel", kind, number, remove, add))
        rm = Label(remove) if remove else None
        ad = Label(add) if add else None
        if kind == "issue":
            self.world = replace(self.world, issues=tuple(
                replace(
                    i,
                    labels=_moved(i.labels, rm, ad),
                    updated_at=self._next_issue_revision(number),
                )
                if i.number == number else i
                for i in self.world.issues
            ))
        else:
            self.world = replace(self.world, prs=tuple(
                replace(p, labels=_moved(p.labels, rm, ad)) if p.number == number else p
                for p in self.world.prs
            ))

    def comment(self, kind: str, number: int, body: str) -> None:
        self.calls.append(("comment", kind, number, body))
        if kind == "issue":
            self.world = replace(self.world, issues=tuple(
                replace(
                    i,
                    comments=i.comments + (
                        Comment(
                            "fix-bot",
                            body,
                            self._next_issue_revision(number),
                        ),
                    ),
                    updated_at=self._next_issue_revision(number),
                )
                if i.number == number else i
                for i in self.world.issues
            ))
        else:
            self.world = replace(self.world, prs=tuple(
                replace(
                    p,
                    comments=p.comments + (Comment("fix-bot", body, "fixture-comment"),),
                )
                if p.number == number else p
                for p in self.world.prs
            ))

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self.calls.append(("close", number))
        self.world = replace(self.world, issues=tuple(
            replace(
                i,
                state="closed",
                updated_at=self._next_issue_revision(number),
            )
            if i.number == number else i
            for i in self.world.issues
        ))

    def enable_auto_merge(self, pr_number: int, method: str) -> None:
        self.calls.append(("automerge", pr_number))

    def update_branch(self, pr_number: int) -> None:
        self.calls.append(("update_branch", pr_number))


class _SimEffects:
    """Keep ordinary fixture writes immediate while draining atomic outbox work."""

    def __init__(self, gateway: _SimGateway, control: SQLiteControlStore):
        self.immediate = ImmediateGitHubEffectExecutor(gateway)
        self.durable = GitHubEffectExecutor(
            gateway,
            control,
            repository="o/r",
            identity="o/r:fix",
        )

    def handles(self, action: Action) -> bool:
        return self.immediate.handles(action)

    def execute(self, action: Action):
        return self.immediate.execute(action)

    def reconcile(self, action: Action) -> bool | None:
        return self.immediate.reconcile(action)

    def drain_pending_commands(self) -> int:
        return self.durable.drain_pending_commands()


@dataclass(frozen=True)
class Tick:
    decided: tuple[Action, ...]        # what decide_fix emitted this poll
    dispatches: tuple[RunAgent, ...]   # the PAID subset


@dataclass(frozen=True)
class Scenario:
    """One frozen world + the convergence budget it must respect."""

    name: str
    world: WorldState
    cfg: FixConfig = CFG
    expect_dispatches: int = 0            # exact PAID sessions over the whole run
    expect_kinds: frozenset = frozenset()  # which legs fired
    cap: int = 0                          # the retry cap that must bound them
    # The agent dies pre-result (raises) instead of returning an errored result
    # — the C3 shape, driven by `_RaisingRunner` rather than `_FakeRunner`.
    runner_raises: bool = False
    runner_result: outcome.AgentResult = ERRORED_RUN
    # PR-family sessions remain in Store; selected resolve sessions belong to
    # the explicitly imported workflow episode instead.
    seed: Callable[[Store], None] | None = None
    resolve_session: str = ""


def simulate(scenario: Scenario, tmp_path, ticks: int = TICKS) -> list[Tick]:
    """Run the daemon's REAL poll loop (`app.loop.Daemon`) against a frozen
    GitHub world — the world only ever changes through the daemon's own writes,
    and the agent is inert. Wires the SAME pieces `app.roles.build_fix_daemon`
    does:

        decide_fix(world, cfg, roles._fix_state(store, world, workflow_control=...), fixer_busy=False)
            → Daemon.tick() → Executor.execute → AgentDispatcher.dispatch
            → FixWorkflow.<leg>() → real workflow authority and accounting

    so a decide/write drift — the review's D13 finding — fails THIS suite
    instead of only `tests/adapters/test_fix_workflow.py`.
    """
    store = Store(state_dir=str(tmp_path))
    if scenario.seed is not None:
        scenario.seed(store)
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    for item in scenario.world.issues:
        snapshot = with_issue_recovery(resolve_snapshot(item.number))
        if scenario.resolve_session:
            source = WorkflowAttemptIdentity(snapshot.binding, RunId(f"prior:{item.number}"), "prior-leg")
            snapshot = replace(
                snapshot, revision=replace(snapshot.revision, version=1),
                episode=replace(snapshot.episode, continuation=FreshResolve(
                    ResolveSession(source, scenario.resolve_session, "claude", "<runtime-default>"), 1,
                )),
            )
        install_workflow(control, snapshot)
    for item in scenario.world.prs:
        install_workflow(control, replace(held_review(key=WorkflowKey("o/r", PullRequestRef(item.number))), gates=()))
    workflow = SQLiteWorkflowControl(
        control, WorkflowWriter("o/r", "fix", "o/r:fix"),
        resolve_ceiling=scenario.cfg.max_resolve_retries,
    )
    gw = _SimGateway(scenario.world)
    worktrees = _FakeWorktrees()
    provider = (
        _RaisingRunner()
        if scenario.runner_raises
        else _FakeRunner(scenario.runner_result)
    )
    runner = AccountingRuntime(
        provider, control, "o/r", "fix", None, None, None, "test", "claude",
        BudgetPolicy({("fix", kind.name): Money("USD", 3_000_000) for kind in AgentKind}),
    )
    log: Callable[[str], None] = lambda _m: None  # noqa: E731
    executor = Executor(
        gw, remove_worktree=worktrees.remove, log=log,
        mark_automerge=store.mark_automerge, clear_issue_state=store.cleanup_pr,
        effect_executor=_SimEffects(gw, control),
    )
    dcfg = DispatchConfig(
        fix=FixTaskConfig("o/r", agent_md="AM"), fix_bot=scenario.cfg.fix_bot, log=log,
        operator_guidance=review_guidance_reader(control, "o/r"),
    )
    dispatcher = AgentDispatcher(dcfg, gw, store, worktrees, runner, executor, workflow_control=workflow)
    executor.bind_workflow_reconciliation(
        dispatcher.fix_workflow.reconcile_returns
    )
    executor._run_agent = lambda a: asyncio.run(dispatcher.dispatch(a))  # noqa: SLF001

    def decide(w: WorldState) -> list[Action]:
        return decide_fix(w, scenario.cfg, roles._fix_state(store, w, workflow_control=workflow), fixer_busy=False)

    daemon = Daemon("fix", gw.build_world, decide, executor, poll_seconds=1, log=log)

    trace: list[Tick] = []
    for _ in range(ticks):
        decided = daemon.tick()
        dispatches = tuple(a for a in decided if isinstance(a, RunAgent))
        trace.append(Tick(tuple(decided), dispatches))
    assert sum(len(tick.dispatches) for tick in trace) == len(provider.tasks), (
        "scheduled work must actually reach the provider; an authority refusal is not convergence"
    )
    return trace


# ── scenarios ────────────────────────────────────────────────────────────────
def _resume_dedup() -> Scenario:
    """V1/#13: one unanswered human reply must buy exactly ONE resume session,
    even though the resume leg keeps failing and the issue keeps returning to
    ai-blocked with that same reply still last in the thread."""
    return Scenario(
        name="resume-dedup",
        world=world(issues=[
            issue(7, "ai-blocked", comments=(
                Comment("alice", "here is the missing context", "2026-01-01T00:00:00Z"),
            )),
        ]),
        resolve_session="prior-session",
        expect_dispatches=1,
        expect_kinds=frozenset({AgentKind.RESUME}),
        cap=0,  # not a retry loop: the dedup mark alone must hold it to one
    )


def _pr_resume_dedup() -> Scenario:
    """#50: one PR-thread reply must buy exactly ONE resume session, even when
    that session asks another question and leaves the PR blocked."""
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    return Scenario(
        name="pr-resume-dedup",
        world=world(
            issues=[issue(7, "ai-pr-open")],
            prs=[
                pr(
                    100,
                    "ai-pr-open",
                    "ai-blocked",
                    closing_issue=7,
                    author="fix-bot",
                    comments=(
                        Comment("fix-bot", prompt, "2026-01-01T00:00:00Z"),
                        Comment("alice", "Use A.", "2026-01-01T00:01:00Z"),
                    ),
                ),
            ],
        ),
        seed=lambda store: store.save_session(7, "prior-session"),
        runner_result=outcome.AgentResult(
            is_error=False,
            status="blocked",
            needs="Which A variant?",
            session_id="resumed-session",
        ),
        expect_dispatches=1,
        expect_kinds=frozenset({AgentKind.PR_RESUME}),
        cap=0,
    )


def _address_review_breaker() -> Scenario:
    """A CHANGES_REQUESTED review on the CURRENT head with a fixer that never
    lands a fix: the attempt counter (keyed to the review) must trip the breaker
    and escalate instead of paying forever."""
    p = pr(
        100, "ai-pr-open",
        head_ref_oid="sha-head",
        reviews=(Review("CHANGES_REQUESTED", "review-bot", "2026-01-02T00:00:00Z",
                        "please fix", "sha-head"),),
    )
    return Scenario(
        name="address-review-breaker",
        world=world(prs=[p]),
        expect_dispatches=CFG.max_review_retries,
        expect_kinds=frozenset({AgentKind.ADDRESS_REVIEW}),
        cap=CFG.max_review_retries,
    )


def _address_review_superseded() -> Scenario:
    """V2/#64: the review judged an OLDER commit than the current head, so the
    ball is in the REVIEWER's court. Zero sessions, ever — not "a few then a
    breaker". A frozen world here means the review daemon is down, which is
    exactly when the old code burned a session per poll."""
    p = pr(
        101, "ai-pr-open",
        head_ref_oid="sha-new",
        reviews=(Review("CHANGES_REQUESTED", "review-bot", "2026-01-02T00:00:00Z",
                        "please fix", "sha-old"),),
    )
    return Scenario(
        name="address-review-superseded",
        world=world(prs=[p]),
        expect_dispatches=0,
        expect_kinds=frozenset(),
        cap=0,
    )


def _fix_ci_breaker() -> Scenario:
    p = pr(102, "ai-pr-open", checks=(Check("unit", "COMPLETED", "fail"),))
    return Scenario(
        name="fix-ci-breaker",
        world=world(prs=[p]),
        expect_dispatches=CFG.max_ci_retries,
        expect_kinds=frozenset({AgentKind.FIX_CI}),
        cap=CFG.max_ci_retries,
    )


def _rebase_breaker() -> Scenario:
    p = pr(103, "ai-pr-open", mergeable="CONFLICTING")
    return Scenario(
        name="rebase-breaker",
        world=world(prs=[p]),
        expect_dispatches=CFG.max_rebase_retries,
        expect_kinds=frozenset({AgentKind.REBASE}),
        cap=CFG.max_rebase_retries,
    )


def _zero_spend_steady_state() -> Scenario:
    """The happy end of the pipeline: a merged PR to clean up and an approved,
    green PR to arm for auto-merge. Neither is a session, and cleanup must stop
    once its labels are applied rather than re-emitting every poll."""
    merged = pr(104, "ai-pr-open", state="MERGED", closing_issue=9)
    approved = pr(
        105, "ai-pr-open",
        reviews=(Review("APPROVED", "review-bot", "2026-01-03T00:00:00Z"),),
    )
    return Scenario(
        name="zero-spend-steady-state",
        world=world(issues=[issue(9, "ai-pr-open")], prs=[merged, approved]),
        expect_dispatches=0,
        expect_kinds=frozenset(),
        cap=0,
    )


def _resolve_retry_loop() -> Scenario:
    """The T3 headline finding, now FIXED: an ai-ready issue whose resolve run
    keeps erroring is requeued to ai-ready by `outcome.resolve_actions`
    (ResolveFollowup.RETRY), and the workflow counts each requeue
    (the durable episode result). At `max_resolve_retries` the decide side escalates
    the issue to needs-human-review instead of paying for another full session
    — the same breaker shape as review/CI/rebase, on the most expensive leg."""
    return Scenario(
        name="resolve-retry-loop",
        world=world(issues=[issue(5, "ai-ready")]),
        expect_dispatches=CFG.max_resolve_retries,
        expect_kinds=frozenset({AgentKind.RESOLVE}),
        cap=CFG.max_resolve_retries,
    )


def _pre_result_death_loop() -> Scenario:
    """Review C3, as a convergence row. The session dies before returning a
    result, so `AgentRunner` raises: the workflow tail never runs, the issue is
    left ai-working, the reaper requeues it to ai-ready, and the poll after that
    pays for another full session — the observed "one paid session every two
    polls, forever", with $0.00 on the ledger and every retry breaker reading 0.

    The infra-death counter (bumped by `AgentDispatcher.dispatch` on the raise
    path) is the ONLY thing that can bound this, because no attempt is ever
    recorded. Cap 2 here rather than the production default 5 purely to fit the
    tick budget; the shape is what matters.
    """
    return Scenario(
        name="pre-result-death-loop",
        world=world(issues=[issue(5, "ai-ready")]),
        cfg=replace(CFG, max_infra_deaths=2),
        expect_dispatches=2,
        expect_kinds=frozenset({AgentKind.RESOLVE}),
        cap=2,
        runner_raises=True,
    )


SCENARIOS = [
    pytest.param(_resume_dedup(), id="resume-dedup"),
    pytest.param(_pr_resume_dedup(), id="pr-resume-dedup"),
    pytest.param(_address_review_breaker(), id="address-review-breaker"),
    pytest.param(_address_review_superseded(), id="address-review-superseded"),
    pytest.param(_fix_ci_breaker(), id="fix-ci-breaker"),
    pytest.param(_rebase_breaker(), id="rebase-breaker"),
    pytest.param(_zero_spend_steady_state(), id="zero-spend-steady-state"),
    pytest.param(_resolve_retry_loop(), id="resolve-retry-loop"),
    pytest.param(_pre_result_death_loop(), id="pre-result-death-loop"),
]


# ── the property ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fix_loop_converges_on_a_frozen_world(scenario: Scenario, tmp_path):
    trace = simulate(scenario, tmp_path)
    dispatches = [run for tick in trace for run in tick.dispatches]

    # 1) PAID work is bounded by the circuit breaker, not by the poll count.
    assert len(dispatches) <= scenario.cap + 1, (
        f"{scenario.name}: {len(dispatches)} agent dispatches over {TICKS} ticks on an "
        f"UNCHANGED world (cap {scenario.cap}) — this is a money-burning loop"
    )
    assert len(dispatches) == scenario.expect_dispatches
    # 2) …and the right leg fired (a leg that goes quiet for the wrong reason —
    #    e.g. the PR silently dropping out of scope — is not convergence).
    assert {run.kind for run in dispatches} == scenario.expect_kinds

    tail = trace[-STABLE_TAIL:]
    # 3) The fixed point is RunAgent-free: no session is ever paid for again.
    assert all(not tick.dispatches for tick in tail), (
        f"{scenario.name}: still dispatching at tick {TICKS}"
    )
    # 4) …and stable: the same ($0) actions repeat forever rather than the
    #    decision oscillating between states.
    assert all(tick.decided == tail[0].decided for tick in tail), (
        f"{scenario.name}: decide_fix does not settle — tail={[t.decided for t in tail]}"
    )


def test_escalation_is_the_only_thing_that_stops_a_stuck_pr(tmp_path):
    """The breaker must escalate to a human exactly ONCE and then go silent —
    an escalation that re-fires every poll would spam the PR (and would mean the
    label write-back that removes it from scope never happened)."""
    trace = simulate(_address_review_breaker(), tmp_path)
    escalated = [
        i for i, tick in enumerate(trace)
        if Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in tick.decided
    ]
    assert escalated == [CFG.max_review_retries]  # right after the last attempt
    assert all(tick.decided == () for tick in trace[CFG.max_review_retries + 1:])


def test_cleanup_stops_once_its_labels_are_applied(tmp_path):
    """Merged-PR cleanup is $0 but not free of consequences (it closes the issue
    and drops worktrees) — it must run once, not every poll."""
    trace = simulate(_zero_spend_steady_state(), tmp_path)
    cleanup_ticks = [
        i for i, tick in enumerate(trace)
        if Relabel(Target.PR, 104, Label.AI_PR_OPEN, None) in tick.decided
    ]
    assert cleanup_ticks == [0]


def test_pre_result_death_is_unbounded_without_the_infra_ceiling(tmp_path):
    """Anti-vacuity twin for the pre-result-death row: with the ceiling disabled
    (MAX_INFRA_DEATHS=0) the SAME frozen world pays for a full session every
    other tick, forever — dispatch, die, reaper requeues, dispatch. That is the
    C3 signature, and no other breaker in the file can see it, because a raised
    run records no attempt anywhere."""
    scenario = replace(_pre_result_death_loop(), cfg=replace(CFG, max_infra_deaths=0))
    trace = simulate(scenario, tmp_path)
    assert sum(len(tick.dispatches) for tick in trace) == TICKS // 2


def test_simulator_can_detect_an_unbounded_loop():
    """Guard against a vacuously-passing property (not the harness — that part
    is real `Daemon`/`Store`/`FixWorkflow` code now, see the module docstring):
    `decide_fix` given the SAME, never-updated `FixState` re-fires the resume
    leg every tick, because the resumed-at dedup mark it depends on is never
    written back. The property above is only meaningful because a `decide_fix`
    that ignored its own state entirely would fail it."""
    scenario = _resume_dedup()
    frozen_state = FixState(resolve_workflows={7: with_issue_recovery(resolve_snapshot(7))})
    dispatches = 0
    for _ in range(TICKS):
        decided = decide_fix(scenario.world, scenario.cfg, frozen_state, fixer_busy=False)
        dispatches += sum(1 for a in decided if isinstance(a, RunAgent))
        # deliberately never updates frozen_state / scenario.world
    assert dispatches == TICKS


def test_resolve_loop_is_unbounded_without_the_durable_episode_ceiling(tmp_path):
    scenario = replace(_resolve_retry_loop(), cfg=replace(CFG, max_resolve_retries=0))

    trace = simulate(scenario, tmp_path)

    assert sum(len(tick.dispatches) for tick in trace) == TICKS
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    workflow = SQLiteWorkflowControl(control, WorkflowWriter("o/r", "fix", "o/r:fix"), resolve_ceiling=0)
    assert workflow.load_workflow(resolve_snapshot(5).revision.fence.key).episode.failures == TICKS
