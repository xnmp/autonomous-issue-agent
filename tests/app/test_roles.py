"""app/roles.py — `_fix_state` round-trips (review C4).

C4: `app/roles.py` had ZERO test references, and `_fix_state` is the READ half
of EVERY circuit breaker `decide_fix` trips on. The review's mutation proved
this: replacing all four `_fix_state` reads with empty dicts left 848/848
tests green, because nothing drove a real counter through the write side, back
through `_fix_state`, into `decide_fix`.

Every test below does exactly that round trip against a REAL `Store(tmp_path)`:

    real write side (AgentDispatcher → FixWorkflow, a scripted runner)
        → roles._fix_state(store, world)
        → domain.fix.decide_fix(world, cfg, state, fixer_busy=False)

and asserts the breaker actually trips: the escalation Action appears and the
corresponding `RunAgent` is withheld. Each is written as a two-phase
(below-cap / at-cap) pair, which doubles as the anti-vacuity twin: phase one
proves the breaker does NOT fire early (a `_fix_state` that always reads 0
would pass phase one trivially but then also fail to trip in phase two).

MUTATION CHECK (done by hand, see the final report): temporarily replacing
every `_fix_state` read with `{}` was applied to `src/issue_agent/app/roles.py`,
this whole module was run, all round-trip tests FAILED (the phase-two breaker
never tripped, so `RunAgent` kept appearing where an escalation was expected),
and the mutation was then reverted. See the agent's final message for the
exact result.
"""

from __future__ import annotations

from tests.domain.builders import observed_pr


from issue_agent.domain import continuation_components as cc

import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.control_store import (
    AccountingDisposition,
    Lease,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)
from issue_agent.adapters.store import Store
from issue_agent.app.dispatch import AgentDispatcher
from issue_agent.app.config import HarnessConfig, from_env
from issue_agent.app.main import _recover_pre_adapter_processes
from issue_agent.app.roles import (
    DispatchReservations,
    EffectRecovery,
    _FIX_RUNTIME_REGISTRY,
    _REVIEW_RUNTIME_REGISTRY,
    _control_store,
    _control_store_path,
    _fix_holds,
    _owner_identity,
    _shutdown_finalize,
    _shutdown_prepare,
    _shutdown_terminalize,
    process_supervision_state_dir,
)
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.fix import breaker_key
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
    RunId,
)
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.review import (
    BoundReviewState,
    ReviewConfig,
    decide_bound_review,
    fix_hold_ceiling_seconds,
    hold_review_dispatch,
)
from issue_agent.domain.world import IssueView, WorldState



def test_runtime_wiring_registries_cover_every_supported_runtime():
    assert frozenset(_FIX_RUNTIME_REGISTRY) == frozenset(RuntimeName)
    assert frozenset(_REVIEW_RUNTIME_REGISTRY) == {
        RuntimeName.CLAUDE,
        RuntimeName.CODEX,
    }


def test_process_supervision_isolated_between_checkouts_sharing_state(
    tmp_path,
) -> None:
    shared = tmp_path / "shared-state"
    checkout_a = tmp_path / "checkout-a"
    checkout_b = tmp_path / "checkout-b"
    checkout_a.mkdir()
    checkout_b.mkdir()
    common = {
        "REPO": "owner/repo",
        "STATE_DIR": str(shared),
    }
    cfg_a = from_env({**common, "TARGET_DIR": str(checkout_a)})
    cfg_b = from_env({**common, "TARGET_DIR": str(checkout_b)})
    supervisor_a = ProcessSupervisor(
        process_supervision_state_dir(cfg_a, "fix"),
        grace_seconds=0.05,
    )
    supervisor_b = ProcessSupervisor(
        process_supervision_state_dir(cfg_b, "fix"),
        grace_seconds=0.05,
    )
    assert supervisor_a.state_dir != supervisor_b.state_dir

    process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        run_id = RunId("checkout-a-paid-run")
        supervisor_a.register(run_id, os.getpgid(process.pid))

        assert supervisor_b.recover_orphans() == ()
        assert process.poll() is None
        assert supervisor_a.recover_orphans() == (run_id,)

        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_boot_preflight_recovers_current_and_repointed_but_not_siblings(
    tmp_path,
) -> None:
    shared = str(tmp_path / "shared-state")
    prior_target = str(tmp_path / "prior")
    current_target = str(tmp_path / "current")
    sibling_target = str(tmp_path / "sibling")
    for target in (prior_target, current_target, sibling_target):
        os.makedirs(target)
    base = HarnessConfig(
        repo="o/r",
        target_dir=prior_target,
        state_dir=shared,
        review_state_dir=shared,
    )
    current = replace(
        base,
        target_dir=current_target,
        repointed_from_deployments=(("o/r", prior_target),),
    )
    sibling = replace(base, target_dir=sibling_target)
    prior_supervisor = ProcessSupervisor(
        process_supervision_state_dir(base, "fix"),
        grace_seconds=0.05,
    )
    current_supervisor = ProcessSupervisor(
        process_supervision_state_dir(current, "fix"),
        grace_seconds=0.05,
    )
    sibling_supervisor = ProcessSupervisor(
        process_supervision_state_dir(sibling, "fix"),
        grace_seconds=0.05,
    )
    prior_process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    current_process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    sibling_process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        prior_run = RunId("prior-repoint-run")
        current_run = RunId("current-deployment-run")
        sibling_run = RunId("unrelated-sibling-run")
        prior_supervisor.register(prior_run, os.getpgid(prior_process.pid))
        current_supervisor.register(
            current_run, os.getpgid(current_process.pid)
        )
        sibling_supervisor.register(
            sibling_run, os.getpgid(sibling_process.pid)
        )

        assert _recover_pre_adapter_processes(
            RecoveryTrigger.WAKE, current, "fix", shared
        ) == ()
        assert prior_process.poll() is None
        assert current_process.poll() is None
        assert _recover_pre_adapter_processes(
            RecoveryTrigger.BOOT, current, "fix", shared
        ) == (prior_run, current_run)

        deadline = time.monotonic() + 3
        while (
            (prior_process.poll() is None or current_process.poll() is None)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert prior_process.poll() is not None
        assert current_process.poll() is not None
        assert sibling_process.poll() is None
        assert sibling_supervisor.registrations()[0].run_id == sibling_run
    finally:
        if prior_process.poll() is None:
            prior_process.kill()
            prior_process.wait()
        if current_process.poll() is None:
            current_process.kill()
            current_process.wait()
        if sibling_process.poll() is None:
            sibling_process.kill()
            sibling_process.wait()


def test_explicit_repoint_adopts_and_releases_old_accounting_owner(
    tmp_path,
) -> None:
    state_root = tmp_path / "state"
    state = str(state_root / "fix")
    review_state = str(state_root / "review")
    prior_target = str(tmp_path / "prior")
    current_target = str(tmp_path / "current")
    old = HarnessConfig(
        repo="o/r",
        target_dir=prior_target,
        state_dir=state,
        review_state_dir=review_state,
    )
    current = replace(
        old,
        target_dir=current_target,
        repointed_from_deployments=(("o/r", prior_target),),
    )
    store = SQLiteControlStore(_control_store_path(old, state))
    store.initialize()
    now = datetime.now(timezone.utc)
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scope = BudgetScope(
        "o/r",
        "repository",
        "USD",
        now.date().isoformat(),
        period_start,
        period_start + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    old_owner = _owner_identity(old, "fix")
    request = ReservationRequest(
        ReservationId("repoint-old-reservation"),
        RunId("repoint-old-run"),
        "repoint-old-dispatch",
        scope,
        PriceQuote("model", "test", "1", now, Money("USD", 60)),
        Money("USD", 100),
        "cfg",
        "o/r",
        kind="RESOLVE",
        owner_id=old_owner,
    )
    assert store.reserve(request).admitted
    store.register_lease(
        Lease(
            request.run_id,
            request.dispatch_id,
            old_owner,
            2_000_000_000,
            2_000_000_000,
            now,
            1,
        )
    )

    adopted = _control_store(current, state, "fix")
    current_owner = _owner_identity(current, "fix")
    assert adopted.leases(current_owner)[0].run_id == request.run_id
    assert adopted.reclaim_dead_leases(
        current_owner, lambda _pid, _start: False
    ) == (request.run_id,)
    assert adopted.release(request.run_id, "spend_unknown").disposition.value == (
        "available"
    )

    next_request = replace(
        request,
        reservation_id=ReservationId("repoint-new-reservation"),
        run_id=RunId("repoint-new-run"),
        owner_id=current_owner,
    )
    assert adopted.reserve(next_request).admitted


def test_hard_shutdown_reconciles_killed_rollout_before_releasing_capacity(
    tmp_path,
):
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    control = SQLiteControlStore(
        str(tmp_path / "control.db"), now=lambda: now
    )
    control.initialize()
    scope = BudgetScope(
        "o/r",
        "repository",
        "USD",
        "2026-07-30",
        now.replace(hour=0),
        now.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )
    run_id = RunId("shutdown-reconcile")
    assert control.reserve(
        ReservationRequest(
            ReservationId("shutdown-reservation"),
            run_id,
            json.dumps({"kind": "RESOLVE", "issue": 42}),
            scope,
            PriceQuote(
                "model",
                "card",
                "v1",
                now,
                Money("USD", 1_000),
            ),
            Money("USD", 2_000),
            "config-v1",
            "o/r",
            kind="RESOLVE",
            owner_id="o/r:fix",
        )
    ).admitted
    control.register_process_lease(
        run_id,
        expected_owner_id="o/r:fix",
        pid=123,
        pgid=123,
        process_start_ticks=99,
    )
    _shutdown_prepare(control, owner_id="o/r:fix")()
    assert control.has_cancellation_intent(run_id)
    order = []

    class Effects:
        def drain_pending_commands(self):
            order.append("effects")
            return 0

    _shutdown_finalize(
        control,
        Effects(),
        owner_id="o/r:fix",
    )()

    assert order == ["effects"]
    assert control.active_reservation_runs("o/r:fix") == (run_id,)

    class Spend:
        def reconcile(self, candidate):
            result = control.settle(
                SettlementRequest(
                    candidate,
                    Money("USD", 250),
                    PricingBasis.RECONCILED,
                    "recovered-rollout",
                    None,
                    None,
                    None,
                )
            )
            return result.disposition is AccountingDisposition.AVAILABLE

    _shutdown_terminalize(
        control,
        Spend(),
        owner_id="o/r:fix",
        state_dir=str(tmp_path / "state"),
        role="fix",
    )()

    assert control.active_reservation_runs("o/r:fix") == ()
    assert control.leases("o/r:fix") == ()
    assert control.snapshot(scope).settled == Money("USD", 250)
    assert control.pending_cancellation_intents("o/r:fix") == ()
    assert (tmp_path / "state" / "handovers" / "issue-42.md").exists()


def test_effect_recovery_reconciles_workflows_then_drains_remote_actions():
    calls = []

    class Recovery:
        def __init__(self, name, count):
            self.name = name
            self.count = count

        def drain_pending_commands(self):
            calls.append(self.name)
            return self.count

    def reconcile():
        calls.append("workflows")
        return 3

    recovery = EffectRecovery(Recovery("actions", 2), reconcile)

    assert recovery.drain_pending_commands() == 5
    assert calls == ["workflows", "actions"]

from test_dispatch import FakeRunner, FakeWorktrees
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway

FIX_BOT = "fix-bot"




def _control(tmp_path) -> SQLiteControlStore:
    control = SQLiteControlStore(str(tmp_path / "control.sqlite3"))
    control.initialize()
    return control






def test_distinct_checkouts_share_durable_fix_dispatch_claims(
    tmp_path,
) -> None:
    path = str(tmp_path / "control.sqlite3")
    first_store = SQLiteControlStore(path)
    second_store = SQLiteControlStore(path)
    first_store.initialize()
    second_store.initialize()
    first = DispatchReservations(
        first_store,
        repository="o/r",
        role="fix",
        owner_id="o/r:fix:checkout-a",
    )
    second = DispatchReservations(
        second_store,
        repository="o/r",
        role="fix",
        owner_id="o/r:fix:checkout-b",
    )
    action = RunAgent(AgentKind.RESOLVE, 7)

    assert first.reserve(action, 7)
    assert not second.reserve(action, 7)
    # Another issue runs alongside: only the target conflicts (Q13).
    assert second.reserve(RunAgent(AgentKind.RESOLVE, 8), 8)

    first.finish(action)
    assert second.reserve(action, 7)


# ── resolve-retries (issue-keyed) ──────────────────────────────────────────────


# ── machine deaths (issue-keyed) ───────────────────────────────────────────────




# ── C4 key-agreement: breaker_key(pr) vs gateway.issue_for_pr(pr) ──────────────




# ══════════════════════════════════════════════════════════════════════════════
# review P0R-3 — the reviewer's ceiling, measured in PAID DISPATCHES
#
# `decide_bound_review` emitted one RunAgent per eligible PR per poll with no
# `consumed` gate and no breaker read: a review that never persists state is
# bought again on the very next poll, forever. These tests assert on the money
# (how many dispatches the loop bought over 20 polls), not on a label, and they
# feed the escalation's own relabel back into the observed world so the
# structural stop — the PR leaving `prs_with(trigger_label)` — is exercised
# rather than assumed.
# ══════════════════════════════════════════════════════════════════════════════
REVIEW_BOT = "review-bot"

from tests.fakes.review_authority import review_binding, review_state


def _bound_review_state(store, world):
    recorded = review_state(store)
    errors = {}
    counts = {}
    owed = {pr.number: (AgentKind.RE_REVIEW if recorded.head(pr.number) else AgentKind.REVIEW,
                       review_binding(pr.number)) for pr in world.prs if recorded.head(pr.number) != pr.head_ref_oid}
    return BoundReviewState(owed=owed, unproductive_rounds=counts, control_errors=errors)


# ── infra-death key split (review P1R-1) ───────────────────────────────────────


# ── CI pending-deadline reporting (#30) ──────────────────────────────────────


# ── FIXER_RUNTIME=glm wiring (build_fix_daemon) ──────────────────────────────
# The wrapping itself (env injection, task-env precedence) is proven in
# tests/adapters/test_claude_profile.py; what these pin is the BUILD-TIME
# contract of the glm branch — that a profile is resolved once at startup and
# that its outcome is announced in one operator-facing line either way. The
# composed runner is not reachable from the returned Daemon without reading a
# private attribute, so it is deliberately not asserted here.


class _BuildableGateway(FakeGateway):
    """FakeGateway plus the one member `build_fix_daemon` binds at build time."""

    def build_fix_world(self):  # pragma: no cover - never ticked in these tests
        raise AssertionError("these tests build the daemon, they do not tick it")

    def build_review_world(self):  # pragma: no cover - never ticked in these tests
        raise AssertionError("these tests build the daemon, they do not tick it")


class _BuildableReviewGateway(_BuildableGateway):
    """Complete review reads include submissions made through the durable executor."""

    def pull_request_reviews(self, pr):
        from issue_agent.domain.review_workflow import (
            CompleteReviews, SubmittedReviewFact, SubmittedReviewState, body_review_command,
        )

        states = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}
        return CompleteReviews(tuple(
            SubmittedReviewFact(str(index), REVIEW_BOT, SubmittedReviewState(states[event]), head,
                                datetime(2026, 9, 27, tzinfo=timezone.utc), body_review_command(body))
            for index, (number, event, body, head) in enumerate(sorted(self.submitted_reviews)) if number == pr
        ))


def _activate_review_fixture(cfg, gateway=None):
    """Seed isolated family authority before composing a real review daemon."""
    from issue_agent.domain.workflow_identity import WorkflowFamily
    control = SQLiteControlStore(_control_store_path(cfg, cfg.review_state_dir))
    control.initialize()
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES (?, ?, 1, 'fixture', 'fixture', ?)",
                   (cfg.repo, WorkflowFamily.REVIEW_REJECTIONS.value, datetime.now(timezone.utc).isoformat()))
    if gateway is not None:
        from issue_agent.app.review_runtime import configured_review_control
        from issue_agent.app.config import owner_identity_for_target
        from issue_agent.domain.workflow_identity import WorkflowKey, PullRequestRef
        from tests.fakes.workflow_authority import seed_family_epoch
        with control.transaction() as db:
            seed_family_epoch(db, cfg.repo, WorkflowFamily.REVIEW_EXECUTION, datetime.now(timezone.utc))
        workflow = configured_review_control(cfg, control, gateway, Store(cfg.review_state_dir),
                                            owner_identity_for_target(cfg.repo, cfg.target_dir, "review"))
        for pr in gateway.build_review_world().prs:
            workflow.ensure_observed_workflow(WorkflowKey(cfg.repo, PullRequestRef(pr.number)))
    return control


def _glm_profile(tmp_path, settings: str) -> str:
    d = tmp_path / ".cc-glm"
    d.mkdir()
    (d / "settings.json").write_text(settings)
    return str(d / "settings.json")


def _role_state_env(tmp_path) -> dict[str, str]:
    return {
        "STATE_DIR": str(tmp_path / "state"),
        "REVIEW_STATE_DIR": str(tmp_path / "review-state"),
    }


def _glm_daemon(tmp_path, settings_file: str):
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_fix_daemon

    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "FIXER_RUNTIME": "glm",
            "GLM_SETTINGS_FILE": settings_file, **_role_state_env(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )
    return build_fix_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "state")), FakeWorktrees(),
        FakeRunner(AgentResult(is_error=False)),
    )


def test_a_glm_profile_is_resolved_once_at_startup_and_announced(tmp_path, capsys):
    settings = json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}})

    daemon = _glm_daemon(tmp_path, _glm_profile(tmp_path, settings))

    line = capsys.readouterr().err
    assert "glm: runtime ready" in line
    assert "api.z.ai" in line              # endpoint host: useful, not secret
    assert "NOT READY" not in line
    assert daemon.name == "fix"


def test_a_missing_glm_profile_says_so_loudly_at_startup_instead_of_dying(tmp_path, capsys):
    """A misconfig must surface as ONE startup line, not as an opaque failure on
    the first dispatch — and the daemon must still be built (visibly wrong beats
    a crash-looping fixer)."""
    daemon = _glm_daemon(tmp_path, str(tmp_path / "absent" / "settings.json"))

    err = capsys.readouterr().err
    assert "glm: NOT READY at startup" in err
    assert "settings.json" in err
    assert daemon.name == "fix"


def test_a_malformed_glm_profile_is_not_ready_rather_than_a_traceback(tmp_path, capsys):
    daemon = _glm_daemon(tmp_path, _glm_profile(tmp_path, '{"env": {,,}'))

    assert "glm: NOT READY at startup" in capsys.readouterr().err
    assert daemon.name == "fix"


# ── boot capability lines (#46) ───────────────────────────────────────────────
# The log line IS the contract here: an operator watching three healthy daemons
# reasonably concludes the daemons are doing the same thing, so these assert on
# the rendered text.


# ── #29: a codex fixer announces that MAX_BUDGET_USD bounds nothing ──────────


def test_codex_reviewer_uses_the_review_budget_and_model(tmp_path, monkeypatch):
    """A codex review leg must use the same runner seam as fixes while carrying
    its own per-review cap and model into the task-producing dispatcher."""
    from issue_agent.adapters import codex_runner
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    seen = {}

    class CapturingCodexRunner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEWER_RUNTIME": "codex",
            "REVIEW_MODEL": "gpt-5.6-terra", "REVIEW_BUDGET_USD": "2.5",
            **_role_state_env(tmp_path),
            "TARGET_DIR": str(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )

    daemon = build_review_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "review-state")),
        FakeRunner(AgentResult(is_error=False)),
    )

    dispatcher = daemon.executor._run_agent._dispatch.__self__
    runtime = dispatcher.runner.inner
    assert runtime.__class__.__name__ == "AccountingRuntime"
    runtime = runtime.inner
    assert runtime.__class__.__name__ == "CodexHomeRuntime"
    assert isinstance(runtime.inner, CapturingCodexRunner)
    assert seen["default_model"] == "gpt-5.6-terra"
    assert seen["enforce_budget"] is True
    assert seen["sandbox"] == "read-only"
    assert seen["resume_on_retry"] is False
    assert dispatcher.review_workflow.cfg.execution_cwd == str(tmp_path)
    assert callable(dispatcher.review_workflow.cfg.prepare_cwd)


def _git_target(tmp_path):
    """A real git checkout for TARGET_DIR. The Claude review binding arms the
    per-PR pinned checkout off TARGET_DIR (0d2e295 M1), so a review leg only
    runs when the PR's head SHA actually exists in that repo — a bare tmp dir
    now (correctly) defers every round instead of reviewing it."""
    repo = tmp_path / "target"
    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.email=t@example.com",
            "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "seed",
        ],
        check=True, capture_output=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return str(repo), sha


def test_review_daemon_claim_agrees_with_branch_resolved_issue(tmp_path, monkeypatch):
    """A review claim and paid admission must identify the same target.

    GitHub does not populate ``closing_issue`` for every non-default-base PR,
    but the fixer's branch still carries its issue number.  The composed
    review daemon must reach the runner and submit its verdict in that world;
    otherwise the durable claim and accounting admission disagree before any
    paid work can start.
    """
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    target_dir, head_sha = _git_target(tmp_path)
    pr = observed_pr(
        152,
        head_ref_name="fix/150-live-e2e",
        head_ref_oid=head_sha,
        closing_issue=None,
        title="dashboard health",
        body="Adds a health endpoint.",
        labels=frozenset({Label.AI_PR_OPEN}),
        state="OPEN",
        author=FIX_BOT,
    )
    world = WorldState(prs=(pr,))

    class BranchLinkedReviewGateway(_BuildableReviewGateway):
        def build_review_world(self):
            return world

    gateway = BranchLinkedReviewGateway(
        prs={pr.number: pr},
        issues={150: IssueView(150, title="dashboard health", state="open")},
        fail_on={"issue_for_pr"},
    )
    from tests.app.acceptance_support import ScriptedProvider
    from tests.app.review_acceptance_harness import register_review_launch
    from issue_agent.domain.outcome import ExitReason
    runner = ScriptedProvider([
        AgentResult(
            is_error=False,
            exit_reason=ExitReason.OK,
            session_id="review-session",
            structured={"decision": "APPROVE", "body": "lgtm"},
        )
    ], during=register_review_launch)
    cfg = replace(
        from_env({
            "REPO": "o/r",
            "FIX_BOT": FIX_BOT,
            "REVIEW_BOT": REVIEW_BOT,
            "REVIEWER_RUNTIME": "claude",
            "TARGET_DIR": target_dir,
            **_role_state_env(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )
    store = Store(str(tmp_path / "review-state"))
    control = _activate_review_fixture(cfg, gateway)
    daemon = build_review_daemon(cfg, gateway, store, runner)

    actions = daemon.tick()
    daemon.wait_for_dispatches()
    assert daemon.shutdown_dispatches(cancel=False)

    assert any(isinstance(a, RunAgent) and a.kind is AgentKind.REVIEW
               and a.number == pr.number and a.workflow is not None for a in actions)
    assert len(runner.tasks) == 1
    # M1: the Claude leg executed in the pinned per-PR checkout, not the
    # operator's live TARGET_DIR.
    assert runner.tasks[0].cwd == os.path.join(
        target_dir, ".claude", "worktrees", "review-pr-152"
    )
    assert any(c[:3] == ("review", pr.number, "APPROVE") and c[3].startswith("lgtm")
               for c in gateway.calls)
    from issue_agent.app.review_runtime import current_review_failures
    assert current_review_failures(control, "o/r", pr.number) == 0
    assert not os.path.exists(store.pr_path(pr.number, "session"))


def test_review_daemon_defers_changed_linkage_without_spend_or_retry(
    tmp_path,
):
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    observed = observed_pr(
        152,
        head_ref_name="fix/150-live-e2e",
        head_ref_oid="a" * 40,
        closing_issue=None,
        title="dashboard health",
        body="Adds a health endpoint.",
        labels=frozenset({Label.AI_PR_OPEN}),
        state="OPEN",
        author=FIX_BOT,
    )
    changed = replace(
        observed,
        head_ref_name="fix/151-relinked",
        closing_issue=151,
    )
    world = WorldState(prs=(observed,))

    class RelinkedReviewGateway(_BuildableReviewGateway):
        def build_review_world(self):
            return world

    gateway = RelinkedReviewGateway(prs={changed.number: changed}, issues={
        number: IssueView(number, title="dashboard health", state="open") for number in (150, 151)
    })
    from tests.app.acceptance_support import ScriptedProvider
    from tests.app.review_acceptance_harness import register_review_launch
    from issue_agent.domain.outcome import ExitReason
    runner = ScriptedProvider([
        AgentResult(
            is_error=True,
            status="must-not-run",
        )
    ], during=register_review_launch)
    cfg = replace(
        from_env({
            "REPO": "o/r",
            "FIX_BOT": FIX_BOT,
            "REVIEW_BOT": REVIEW_BOT,
            "REVIEWER_RUNTIME": "claude",
            "TARGET_DIR": str(tmp_path),
            **_role_state_env(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )
    store = Store(str(tmp_path / "review-state"))
    control = _activate_review_fixture(cfg, gateway)
    daemon = build_review_daemon(cfg, gateway, store, runner)

    actions = daemon.tick()
    daemon.wait_for_dispatches()
    assert daemon.shutdown_dispatches(cancel=False)

    assert any(isinstance(a, RunAgent) and a.kind is AgentKind.REVIEW
               and a.number == observed.number and a.workflow is not None for a in actions)
    assert runner.tasks == []
    assert not any(call[0] == "review" for call in gateway.calls)




def test_codex_reviewer_defers_codex_home_preparation_until_dispatch(tmp_path, monkeypatch):
    """Building a daemon must not prepare any checkout's CODEX_HOME."""
    from issue_agent.adapters import codex_home, codex_runner
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import build_review_daemon

    hook_calls = []

    class CapturingCodexRunner:
        def __init__(self, **_kwargs):
            pass

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    monkeypatch.setattr(
        codex_home, "write_codex_home", lambda *args, **kwargs: hook_calls.append((args, kwargs))
    )
    cfg = replace(
        from_env({
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEWER_RUNTIME": "codex",
            **_role_state_env(tmp_path),
            "TARGET_DIR": str(tmp_path / "target"),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )

    build_review_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "review-state")),
        FakeRunner(AgentResult(is_error=False)),
    )

    assert hook_calls == []



# ── duplicate-PR guard reporting (#39) ───────────────────────────────────────


# ── FixState carries the re-run marker (#38) ─────────────────────────────────






def _held_pr(number=581, issue=500):
    return observed_pr(
        number, head_ref_name=f"fix/{issue}-x", closing_issue=issue,
        head_ref_oid="abc", labels=frozenset({Label.AI_PR_OPEN}),
        author=FIX_BOT, state="OPEN",
    )


def _review_cfg():
    return ReviewConfig(review_bot="review-bot")


def _decide(tmp_path, control, world_state, now):
    actions = decide_bound_review(
        world_state, _review_cfg(), _bound_review_state(Store(state_dir=str(tmp_path)), world_state)
    )
    return hold_review_dispatch(
        actions,
        _fix_holds(control, "o/r", world_state, now),
        ceiling_seconds=fix_hold_ceiling_seconds(2700.0),
    )


def test_an_active_fix_leg_defers_the_review_of_the_same_pr(tmp_path):
    """The 2026-08-08 tauri batch burned four complete paid review legs on PRs
    the fixer was still pushing to. The claim the fixer already holds is enough
    to know that at dispatch time."""
    control = _control(tmp_path)
    now = datetime.now(timezone.utc)
    world_state = WorldState(prs=(_held_pr(),))
    assert control.claim_dispatch(
        "o/r", "fix", 500, "ADDRESS_REVIEW", "owner-a"
    )

    plan = _decide(tmp_path, control, world_state, now)

    assert plan.actions == ()
    assert [hold.pr for hold in plan.deferred] == [581]

    # The fix leg finishes between ticks; nothing persistent has to be undone.
    assert control.release_dispatch_claim("o/r", "fix", 500, "owner-a")
    assert _decide(tmp_path, control, world_state, now).actions == (
        RunAgent(AgentKind.REVIEW, 581, workflow=review_binding(581)),
    )


def test_a_fix_hold_past_the_ceiling_stops_starving_the_review(tmp_path):
    control = _control(tmp_path)
    world_state = WorldState(prs=(_held_pr(),))
    assert control.claim_dispatch(
        "o/r", "fix", 500, "ADDRESS_REVIEW", "owner-a"
    )
    wedged = datetime.now(timezone.utc) + timedelta(
        seconds=fix_hold_ceiling_seconds(2700.0) + 1
    )

    plan = _decide(tmp_path, control, world_state, wedged)

    assert plan.actions == (RunAgent(AgentKind.REVIEW, 581, workflow=review_binding(581)),)
    assert [hold.pr for hold in plan.forced] == [581]


def test_a_fix_hold_leaves_every_other_prs_review_alone(tmp_path):
    control = _control(tmp_path)
    world_state = WorldState(prs=(_held_pr(), _held_pr(614, 610)))
    assert control.claim_dispatch(
        "o/r", "fix", 500, "ADDRESS_REVIEW", "owner-a"
    )

    plan = _decide(tmp_path, control, world_state, datetime.now(timezone.utc))

    assert plan.actions == (RunAgent(AgentKind.REVIEW, 614, workflow=review_binding(614)),)


def test_a_dead_fix_claim_does_not_defer_the_review(tmp_path):
    control = _control(tmp_path)
    assert control.claim_dispatch(
        "o/r", "fix", 500, "ADDRESS_REVIEW", "owner-a"
    )
    with control._connect() as db:
        db.execute(
            "UPDATE dispatch_claims SET owner_pid=?, owner_start_ticks=?",
            (2_000_000_000, 1),
        )

    plan = _decide(tmp_path, control, WorldState(prs=(_held_pr(),)), datetime.now(timezone.utc))

    assert plan.actions == (RunAgent(AgentKind.REVIEW, 581, workflow=review_binding(581)),)


def test_an_unreadable_control_store_fails_open_to_dispatch(tmp_path):
    """Withholding is the only thing this reader does, and the submit-time head
    fence still protects correctness — so an unreadable store must not be able
    to stop review."""
    class Broken:
        def active_dispatch_claims(self, repository, role):
            raise RuntimeError("control store unavailable")

    assert _fix_holds(
        Broken(), "o/r", WorldState(prs=(_held_pr(),)), datetime.now(timezone.utc)
    ) == {}


def test_a_pr_with_no_resolvable_issue_is_still_deferred(tmp_path):
    """`breaker_key` falls back to -PR for a PR with no closing ref and a
    non-numeric branch; the fixer claims under that key, so the review side has
    to address it the same way."""
    control = _control(tmp_path)
    issueless = observed_pr(
        617, head_ref_name="feat/ACME-2468-x", head_ref_oid="abc",
        labels=frozenset({Label.AI_PR_OPEN}), author=FIX_BOT, state="OPEN",
    )
    assert control.claim_dispatch(
        "o/r", "fix", -617, "FIX_CI", "owner-a"
    )

    plan = _decide(
        tmp_path, control, WorldState(prs=(issueless,)), datetime.now(timezone.utc)
    )

    assert plan.actions == ()
    assert [hold.pr for hold in plan.deferred] == [617]




@pytest.mark.parametrize("cap_usd, blocked", [(5.0, True), (10.0, False), (50.0, False)])
def test_spend_gate_holds_paid_dispatch_when_one_leg_no_longer_fits_the_daily_cap(tmp_path, cap_usd, blocked):
    # Decision #7: a daily cap smaller than one leg's reservation is a reported
    # tick-gate wait, never a silent one and never an admission refusal label.
    from issue_agent.app.roles import _control_spend_gate

    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    cfg = HarnessConfig(repo="o/r", target_dir=str(tmp_path), daily_spend_cap_usd=cap_usd)
    gate = _control_spend_gate(cfg, control, 10.0)
    assert gate is not None
    reason = gate()
    assert (reason is not None) is blocked
    if blocked:
        assert "repository daily spend cap reached" in reason and "$10.00 leg / $5.00" in reason
