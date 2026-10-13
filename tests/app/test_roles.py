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
    BudgetLimit,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)
from issue_agent.adapters.store import Store
from issue_agent.app.config import HarnessConfig, from_env
from issue_agent.app.main import _recover_pre_adapter_processes
from issue_agent.app.roles import _FIX_RUNTIME_REGISTRY, _REVIEW_RUNTIME_REGISTRY, _control_spend_gate, _control_store, _headroom, _owner_identity, _shutdown_prepare, _shutdown_terminalize, process_supervision_state_dir
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.contracts import (
    BudgetScope,
    ChargeOrigin,
    Money,
    PricingBasis,
    RunId,
)
from issue_agent.domain.recovery import RecoveryTrigger


class _UnreadableLedger:
    def snapshot(self, _scope):
        raise OSError("database disk image is malformed")


def test_paid_headroom_follows_the_daily_cap_and_fails_closed_on_an_unreadable_ledger(tmp_path):
    cfg = HarnessConfig(repo="o/r", daily_spend_cap_usd=10.0)
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()

    assert _headroom(_control_spend_gate(cfg, store, 5.0))() is True
    assert _headroom(_control_spend_gate(cfg, store, 11.0))() is False
    assert _headroom(_control_spend_gate(cfg, _UnreadableLedger(), 5.0))() is False
    # No cap configured: nothing to read, nothing to wait on.
    uncapped = HarnessConfig(repo="o/r", daily_spend_cap_usd=0.0)
    assert _headroom(_control_spend_gate(uncapped, _UnreadableLedger(), 5.0))() is True


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


def _daily(now: datetime) -> BudgetScope:
    return BudgetScope(
        "o/r", "repository", "USD", now.date().isoformat(),
        now.replace(hour=0), now.replace(hour=0) + timedelta(days=1), ZoneInfo("UTC"),
    )


def test_a_repointed_deployment_recovers_its_predecessors_dead_leg(tmp_path) -> None:
    # Recovery scopes by repository and role, not deployment: the leg a
    # repointed checkout's predecessor left behind is reclaimed and settled
    # by the new deployment, and the item is free for it again.
    state = str(tmp_path / "state")
    old = HarnessConfig(repo="o/r", target_dir=str(tmp_path / "old"), state_dir=state, review_state_dir=state)
    current = replace(old, target_dir=str(tmp_path / "current"))
    old_owner = _owner_identity(old, "fix")
    current_owner = _owner_identity(current, "fix")
    assert old_owner != current_owner
    store = _control_store(old, state)
    run_id = RunId("repoint-old-run")
    assert store.lease("o/r", "fix", 7, old_owner)
    assert store.reserve(ReservationRequest(
        run_id, "o/r", "fix", "RESOLVE", old_owner, Money("USD", 1_000),
        (BudgetLimit(_daily(datetime.now(timezone.utc)), Money("USD", 10_000)),), 7, issue=7,
    )).admitted
    store.register_process_lease(
        run_id, expected_owner_id=old_owner, pid=999_999, pgid=999_999, process_start_ticks=1,
    )

    adopted = _control_store(current, state)
    assert adopted.path == store.path
    assert adopted.reclaim_dead_leases(current_owner, lambda _pid, _ticks: False) == (run_id,)
    assert adopted.active_reservation_runs(current_owner) == (run_id,)
    assert adopted.settle_terminal_estimate(run_id, "spend_unknown")
    assert adopted.lease("o/r", "fix", 7, current_owner)


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
    assert control.lease("o/r", "fix", 42, "o/r:fix")
    assert control.reserve(
        ReservationRequest(
            run_id, "o/r", "fix", "RESOLVE", "o/r:fix", Money("USD", 1_000),
            (BudgetLimit(scope, Money("USD", 2_000)),), 42, issue=42, number=42,
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
    # Recording the intent releases nothing: capacity stays reserved until
    # the killed run's spend is reconciled.
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
    assert control.leases() == ()
    assert control.snapshot(scope).settled == Money("USD", 250)
    assert not control.has_cancellation_intent(run_id)
    assert (tmp_path / "state" / "handovers" / "issue-42.md").exists()


def test_a_codex_leg_killed_at_shutdown_keeps_its_reservation_for_boot_recovery(tmp_path):
    # Its rollout may become readable only after the process group is
    # reaped: settling it at a zero estimate during shutdown could never be
    # corrected by the measured recovery that follows.
    from issue_agent.adapters.spend_recovery import CodexSpendReconciler

    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    control = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: now)
    control.initialize()
    run_id = RunId("killed-at-shutdown")
    assert control.lease("o/r", "fix", 42, "o/r:fix")
    assert control.reserve(ReservationRequest(
        run_id, "o/r", "fix", "RESOLVE", "o/r:fix", Money("USD", 1_000),
        (BudgetLimit(_daily(now), Money("USD", 2_000)),), 42, issue=42, runtime="codex",
    )).admitted
    control.register_process_lease(
        run_id, expected_owner_id="o/r:fix", pid=999_999, pgid=999_999, process_start_ticks=1,
    )
    _shutdown_prepare(control, owner_id="o/r:fix")()
    control.unlease("o/r", "fix", 42, "o/r:fix")  # the role's `finally` after the cancelled leg

    _shutdown_terminalize(
        control, CodexSpendReconciler(control, (str(tmp_path / "unreadable-yet"),)),
        owner_id="o/r:fix", state_dir=str(tmp_path / "state"), role="fix",
    )()

    assert control.active_reservation_runs("o/r:fix") == (run_id,)
    assert [lease.run_id for lease in control.leases()] == [run_id]
    assert (tmp_path / "state" / "handovers" / "issue-42.md").exists()
    # Boot: the group is reaped, its lease reclaimed, and the measured
    # rollout settles the leg.
    assert control.reclaim_dead_leases("o/r:fix", lambda _pid, _ticks: False) == (run_id,)
    measured = control.settle(SettlementRequest(
        run_id, Money("USD", 250), PricingBasis.RECONCILED, "codex-recovered:s:killed", None, None, None,
        origin=ChargeOrigin.RECOVERY_ESTIMATE,
    ))
    assert measured.disposition is AccountingDisposition.AVAILABLE
    assert control.snapshot(_daily(now)).settled == Money("USD", 250)


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
from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway

FIX_BOT = "fix-bot"


def _control(tmp_path) -> SQLiteControlStore:
    control = SQLiteControlStore(str(tmp_path / "control.sqlite3"))
    control.initialize()
    return control


# ── resolve-retries (issue-keyed) ──────────────────────────────────────────────


# ── machine deaths (issue-keyed) ───────────────────────────────────────────────


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

    def build_world(self):  # pragma: no cover - never ticked in these tests
        raise AssertionError("these tests build the daemon, they do not tick it")

    def viewer_login(self) -> str:
        return REVIEW_BOT  # the review role's boot check: its credential is REVIEW_BOT

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


def test_codex_reviewer_uses_the_review_budget_and_model(tmp_path, monkeypatch):
    """A codex review leg uses the fix role's runner seam, read-only, and
    carries its own per-review cap and model into the review task."""
    from issue_agent.adapters import codex_runner
    from issue_agent.app.config import from_env
    from issue_agent.app.roles import compose_review_daemon

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
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEW_BOT": REVIEW_BOT, "REVIEWER_RUNTIME": "codex",
            "REVIEW_MODEL": "gpt-5.6-terra", "REVIEW_BUDGET_USD": "2.5",
            **_role_state_env(tmp_path),
            "TARGET_DIR": str(tmp_path),
        }),
        control_authority_root=str(tmp_path / "machine-state"),
    )

    composed = compose_review_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "review-state")),
        FakeRunner(AgentResult(is_error=False)),
    )

    assert seen["default_model"] == "gpt-5.6-terra"
    assert seen["enforce_budget"] is True
    assert seen["sandbox"] == "read-only"
    assert seen["resume_on_retry"] is False
    assert composed.legs.task_config.model == "gpt-5.6-terra"
    assert composed.legs.task_config.max_budget_usd == 2.5


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
            "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEW_BOT": REVIEW_BOT, "REVIEWER_RUNTIME": "codex",
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
