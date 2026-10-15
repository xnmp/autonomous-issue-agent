"""The shared role composition (`tick.compose_role`): headroom, process
isolation, recovery scope, shutdown hooks, and runtime binding."""

from __future__ import annotations


import os
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.control_store import (
    AccountingDisposition,
    BudgetLimit,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)
from issue_agent.app.config import HarnessConfig, from_env
from issue_agent.app.config import supervision_dir
from issue_agent.app.tick import control_store, headroom, owner_identity, shutdown_hooks
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.contracts import BudgetScope, ChargeOrigin, Money, PricingBasis, RunId


class _UnreadableLedger:
    def snapshot(self, _scope):
        raise OSError("database disk image is malformed")


def test_paid_headroom_follows_the_daily_cap_and_fails_closed_on_an_unreadable_ledger(tmp_path):
    cfg = HarnessConfig(repo="o/r", daily_spend_cap_usd=10.0)
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()

    assert headroom(cfg, store, 5.0)() is True
    assert headroom(cfg, store, 11.0)() is False
    assert headroom(cfg, _UnreadableLedger(), 5.0)() is False
    # No cap configured: nothing to read, nothing to wait on.
    uncapped = HarnessConfig(repo="o/r", daily_spend_cap_usd=0.0)
    assert headroom(uncapped, _UnreadableLedger(), 5.0)() is True


def test_process_supervision_isolated_between_checkouts_sharing_state(tmp_path) -> None:
    shared = tmp_path / "shared-state"
    checkout_a = tmp_path / "checkout-a"
    checkout_b = tmp_path / "checkout-b"
    checkout_a.mkdir()
    checkout_b.mkdir()
    common = {"REPO": "owner/repo", "STATE_DIR": str(shared)}
    cfg_a = from_env({**common, "TARGET_DIR": str(checkout_a)})
    cfg_b = from_env({**common, "TARGET_DIR": str(checkout_b)})
    supervisor_a = ProcessSupervisor(supervision_dir(cfg_a, "fix"), grace_seconds=0.05)
    supervisor_b = ProcessSupervisor(supervision_dir(cfg_b, "fix"), grace_seconds=0.05)
    assert supervisor_a.state_dir != supervisor_b.state_dir

    process = subprocess.Popen(["sleep", "30"], start_new_session=True, stdout=subprocess.DEVNULL)
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


def _daily(now: datetime) -> BudgetScope:
    return BudgetScope(
        "o/r",
        "repository",
        "USD",
        now.date().isoformat(),
        now.replace(hour=0),
        now.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )


def test_a_repointed_deployment_recovers_its_predecessors_dead_leg(tmp_path) -> None:
    # Recovery scopes by repository and role, not deployment: the leg a
    # repointed checkout's predecessor left behind is reclaimed and settled
    # by the new deployment, and the item is free for it again.
    state = str(tmp_path / "state")
    old = HarnessConfig(repo="o/r", target_dir=str(tmp_path / "old"), state_dir=state, review_state_dir=state)
    current = replace(old, target_dir=str(tmp_path / "current"))
    old_owner = owner_identity(old, "fix")
    current_owner = owner_identity(current, "fix")
    assert old_owner != current_owner
    store = control_store(old, "fix")
    run_id = RunId("repoint-old-run")
    assert store.lease("o/r", "fix", 7, old_owner)
    assert store.reserve(
        ReservationRequest(
            run_id,
            "o/r",
            "fix",
            "RESOLVE",
            old_owner,
            Money("USD", 1_000),
            (BudgetLimit(_daily(datetime.now(timezone.utc)), Money("USD", 10_000)),),
            7,
            issue=7,
        )
    ).admitted
    store.register_process_lease(run_id, expected_owner_id=old_owner, pid=999_999, pgid=999_999, process_start_ticks=1)

    adopted = control_store(current, "fix")
    assert adopted.path == store.path
    assert adopted.reclaim_dead_leases(current_owner, lambda _pid, _ticks: False) == (run_id,)
    assert adopted.active_reservation_runs(current_owner) == (run_id,)
    assert adopted.settle_terminal_estimate(run_id, "spend_unknown")
    assert adopted.lease("o/r", "fix", 7, current_owner)


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
    assert control.reserve(
        ReservationRequest(
            run_id,
            "o/r",
            "fix",
            "RESOLVE",
            "o/r:fix",
            Money("USD", 1_000),
            (BudgetLimit(_daily(now), Money("USD", 2_000)),),
            42,
            issue=42,
            runtime="codex",
        )
    ).admitted
    control.register_process_lease(
        run_id, expected_owner_id="o/r:fix", pid=999_999, pgid=999_999, process_start_ticks=1
    )
    prepare, _ = shutdown_hooks(control, None, owner_id="o/r:fix")  # type: ignore[arg-type]
    prepare()
    # The forced-shutdown intent is durable before teardown starts.
    assert control.has_cancellation_intent(run_id)
    control.unlease("o/r", "fix", 42, "o/r:fix")  # the role's `finally` after the cancelled leg

    shutdown_hooks(
        control,
        CodexSpendReconciler(control, (str(tmp_path / "unreadable-yet"),)),
        owner_id="o/r:fix",
    )[1]()

    assert control.active_reservation_runs("o/r:fix") == (run_id,)
    assert [lease.run_id for lease in control.leases()] == [run_id]
    # Boot: the group is reaped, its lease reclaimed, and the measured
    # rollout settles the leg.
    assert control.reclaim_dead_leases("o/r:fix", lambda _pid, _ticks: False) == (run_id,)
    measured = control.settle(
        SettlementRequest(
            run_id,
            Money("USD", 250),
            PricingBasis.RECONCILED,
            "codex-recovered:s:killed",
            None,
            None,
            None,
            origin=ChargeOrigin.RECOVERY_ESTIMATE,
        )
    )
    assert measured.disposition is AccountingDisposition.AVAILABLE
    assert control.snapshot(_daily(now)).settled == Money("USD", 250)


class FakeWorktrees:
    def ensure(self, issue, branch):
        return f"/wt/issue-{issue}"

    def remove(self, issue):
        pass

    def branch_for_issue(self, issue):
        del issue
        return ""

    def remote_head(self, branch):
        return ""


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        return self.result


from tests.fakes.in_memory_github import InMemoryGitHub as FakeGateway

FIX_BOT = "fix-bot"


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


# ── CI pending-deadline reporting (#30) ──────────────────────────────────────


class _BuildableGateway(FakeGateway):
    """FakeGateway plus the one member `build_fix_daemon` binds at build time."""

    def build_world(self):  # pragma: no cover - never ticked in these tests
        raise AssertionError("these tests build the daemon, they do not tick it")


def _role_state_env(tmp_path) -> dict[str, str]:
    return {"STATE_DIR": str(tmp_path / "state"), "REVIEW_STATE_DIR": str(tmp_path / "review-state")}


# ── #29: a codex fixer announces that MAX_BUDGET_USD bounds nothing ──────────


def test_codex_reviewer_uses_the_review_budget_and_model(tmp_path, monkeypatch):
    """A codex review leg uses the fix role's runner seam, read-only, and
    carries its own per-review cap and model into the review task."""
    from issue_agent.adapters import codex_runner
    from issue_agent.app.config import from_env
    from issue_agent.app.review import compose_review_daemon

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
        from_env(
            {
                "REPO": "o/r",
                "FIX_BOT": FIX_BOT,
                "REVIEW_BOT": REVIEW_BOT,
                "REVIEWER_RUNTIME": "codex",
                "REVIEW_MODEL": "gpt-5.6-terra",
                "REVIEW_BUDGET_USD": "2.5",
                **_role_state_env(tmp_path),
                "TARGET_DIR": str(tmp_path),
            }
        ),
        control_authority_root=str(tmp_path / "machine-state"),
    )

    composed = compose_review_daemon(cfg, _BuildableGateway(), FakeRunner(AgentResult(is_error=False)))

    assert seen["default_model"] == "gpt-5.6-terra"
    assert seen["rate_table"] is not None
    assert seen["sandbox"] == "read-only"
    # #413: the review leg runs in the leg sandbox, read-only, with no GitHub credential.
    assert not seen["leg_sandbox"].writable and not seen["leg_sandbox"].has_credential
    assert composed.legs.task_config.model == "gpt-5.6-terra"
    assert composed.legs.task_config.max_budget_usd == 2.5


def test_codex_reviewer_defers_codex_home_preparation_until_dispatch(tmp_path, monkeypatch):
    """Building a daemon must not prepare any checkout's CODEX_HOME."""
    from issue_agent.adapters import codex_home, codex_runner
    from issue_agent.app.config import from_env
    from issue_agent.app.review import compose_review_daemon

    hook_calls = []

    class CapturingCodexRunner:
        def __init__(self, **_kwargs):
            pass

        def preflight(self):
            return None

        def oauth_auth_file(self):
            return ""

    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    monkeypatch.setattr(codex_home, "write_codex_home", lambda *args, **kwargs: hook_calls.append((args, kwargs)))
    cfg = replace(
        from_env(
            {
                "REPO": "o/r",
                "FIX_BOT": FIX_BOT,
                "REVIEW_BOT": REVIEW_BOT,
                "REVIEWER_RUNTIME": "codex",
                **_role_state_env(tmp_path),
                "TARGET_DIR": str(tmp_path / "target"),
            }
        ),
        control_authority_root=str(tmp_path / "machine-state"),
    )

    compose_review_daemon(cfg, _BuildableGateway(), FakeRunner(AgentResult(is_error=False)))

    assert hook_calls == []


def test_a_fix_role_leg_sandbox_carries_fix_bot_token_and_hides_the_harness(tmp_path):
    """#413: a fix leg writes and holds FIX_BOT_TOKEN, whichever account owns
    it; a review leg holds none; neither can mount the harness, state or
    control directories."""
    from issue_agent.app.config import from_env
    from issue_agent.app.tick import leg_sandbox

    cfg = replace(from_env({"REPO": "o/r", **_role_state_env(tmp_path), "TARGET_DIR": str(tmp_path / "t")}),
                  control_authority_root=str(tmp_path / "machine-state"))
    env = {"HOME": str(tmp_path / "home"), "FIX_BOT_TOKEN": "fix-token", "REVIEW_BOT_TOKEN": "review-token"}

    fix, review = leg_sandbox(cfg, "fix", env), leg_sandbox(cfg, "review", env)

    assert fix.writable and fix.has_credential
    assert not review.writable and not review.has_credential
    assert "fix-token" not in repr(fix) and "review-token" not in repr(review)
    for path in (cfg.state_dir, cfg.review_state_dir, str(tmp_path / "machine-state")):
        assert os.path.realpath(path) in fix.protected
    assert not leg_sandbox(cfg, "fix", {"HOME": str(tmp_path), "FIX_BOT_TOKEN": "  "}).has_credential
