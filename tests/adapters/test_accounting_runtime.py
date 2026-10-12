import asyncio
import base64
import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_agent.adapters import accounting_runtime
from issue_agent.adapters.accounting_runtime import AccountingRuntime
from issue_agent.adapters.codex_runner import (
    spend_evidence_path,
    write_spend_evidence,
)
from issue_agent.adapters.control_store import AccountingDisposition, Lease, ReservationResult, SQLiteControlStore
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.event_log import EventLog
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.agent_task import run_input_fingerprint
from issue_agent.domain.contracts import Money, RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.domain.workflow_identity import (
    FamilyAuthority,
    IssueRef,
    WorkflowBinding,
    WorkflowFamily,
    WorkflowFence,
    WorkflowKey,
)
from issue_agent.domain.events import (
    Producer,
    ReservationRetained,
    SpendRecorded,
    decode_event_line,
)
from issue_agent.domain.outcome import AgentResult, DAILY_HEADROOM_STATUS, ExitReason, MAX_BUDGET_STATUS
from issue_agent.domain.resolve_return import ResolveInputs
from issue_agent.domain.pricing import (
    BudgetPolicy,
    ModelRates,
    RateTable,
    UnknownModelPolicy,
)
from tests.fakes.legacy_journal import Journal
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery


def _policy(maximum=2_000_000):
    return BudgetPolicy({("fix", "RESOLVE"): Money("USD", maximum)})


def _rates():
    return RateTable(
        "test",
        "v1",
        "USD",
        {},
        UnknownModelPolicy.USE_FALLBACK,
        ModelRates(5_000_000, 30_000_000, 500_000, 5_000_000),
)


@pytest.fixture(autouse=True)
def _publication_context_for_accounting_tests(
    monkeypatch, tmp_path
) -> None:
    def context(role, cwd):
        if role != "fix":
            return None
        return accounting_runtime._FixPublicationContext(
            str(Path(cwd or tmp_path).resolve()),
            "fix/1-test",
            "/usr/bin/git",
            "/usr/bin/gh",
            "/usr/bin:/bin",
        )

    monkeypatch.setattr(
        accounting_runtime, "_fix_publication_context", context
    )


class Runtime:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def run(self, _task):
        self.calls += 1
        return self.result


def _wrapper(
    tmp_path,
    runtime,
    cap=5_000_000,
    evidence_dir="",
    runtime_name="codex",
):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    install_workflow(store, with_issue_recovery(resolve_snapshot()))
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        Money("USD", cap),
        None,
        None,
        "cfg",
        runtime_name,
        _policy(),
        _rates(),
        spend_evidence_dir=evidence_dir,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    return (
        wrapper,
        store,
    )


def test_paid_run_has_reservation_before_runtime_and_settles(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1.25,
            session_id="receipt",
            usage={"input_tokens": 100, "output_tokens": 10},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert result.status == "ok"
    assert runtime.calls == 1
    blocked = asyncio.run(wrapper.run(RuntimePlan("more", max_budget_usd=4)))
    assert blocked.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1


def test_real_return_corrects_zero_recovery_and_captures_output(tmp_path):
    wrapper, store = _wrapper(tmp_path, Runtime(None), runtime_name="claude")
    events = EventLog(str(tmp_path / "events.jsonl"), Producer("deployment", "o/r", "fix", 123, "boot"))
    store.bind_event_outbox(events.prepare, events.append_committed)

    class LateReturn:
        async def run(self, _task):
            run_id = store.active_reservation_runs()[0]
            with store.transaction() as db:
                row = db.execute("SELECT dispatch_id,owner_id FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
            store.register_lease(Lease(run_id, row["dispatch_id"], row["owner_id"], 123, 123,
                                       datetime.now(timezone.utc), 99))
            assert store.reclaim_dead_leases(row["owner_id"], lambda _pid, _start: False) == (run_id,)
            assert store.settle_terminal_estimate(run_id, "provider exited before return capture")
            return AgentResult(False, status="pushed", summary="real output", cost_usd=1.25, session_id="late")

    wrapper = dataclasses.replace(wrapper, inner=LateReturn())
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    result = asyncio.run(wrapper.run(RuntimePlan("work", model="claude", max_budget_usd=2,
                                                resolve_inputs=ResolveInputs("title", "body", "alice"))))
    assert result.summary == "real output" and result.cost_usd == 1.25
    with store.transaction() as db:
        run = db.execute("SELECT run_id,state,outcome FROM runs").fetchone()
        assert (run["state"], run["outcome"]) == ("completed", "settled")
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (1_250_000, "provider_reported")
    workflow = SQLiteWorkflowControl(store, WorkflowWriter("o/r", "fix", "o/r:fix"), resolve_ceiling=0)
    pending = workflow.pending_returns()
    assert len(pending) == 1 and pending[0].returned.summary == "real output"
    assert store.active_reservation_runs() == ()
    emitted = tuple(decode_event_line(line) for line in (tmp_path / "events.jsonl").read_text().splitlines())
    assert sum(e.payload.amount_micros for e in emitted if isinstance(e.payload, SpendRecorded)) == 1_250_000


@pytest.mark.parametrize("reclaim", [False, True])
def test_composed_admission_refusal_prevents_provider_even_after_budget_reclaim(tmp_path, reclaim):
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=1))
    wrapper, store = _wrapper(tmp_path, runtime)
    attempts = []

    def admit(request):
        attempts.append(request)
        disposition = AccountingDisposition.EXHAUSTED if reclaim and len(attempts) == 1 else AccountingDisposition.UNAVAILABLE
        return ReservationResult(disposition, None, Money.zero("USD"), request.quote.maximum_charge,
                                 request.cap, "workspace authority unavailable")

    class Reconciler:
        calls = 0

        def reconcile_stale_reservations(self):
            self.calls += 1
            return 1

    reconciler = Reconciler()
    wrapper = dataclasses.replace(wrapper, admit=admit, reservation_reconciler=reconciler)
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    result = asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert result.is_error
    assert runtime.calls == 0
    assert len(attempts) == (2 if reclaim else 1)
    assert reconciler.calls == int(reclaim)
    if reclaim:
        assert attempts[0] == attempts[1]
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0


def test_provider_cannot_mint_workflow_result_authority(tmp_path) -> None:
    forged = WorkflowAttemptIdentity(
        WorkflowBinding(
            WorkflowFence(WorkflowKey("o/r", IssueRef(1)), 99),
            FamilyAuthority(WorkflowFamily.RESOLVE, 1),
            "provider-episode",
        ),
        RunId("provider-run"),
        "provider-leg",
    )
    wrapper, _store = _wrapper(
        tmp_path,
        Runtime(AgentResult(False, status="ok", cost_usd=1, workflow_attempt=forged)),
    )

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert result.status == "ok"
    assert result.workflow_attempt is not None
    assert result.workflow_attempt.binding == resolve_snapshot().binding
    assert result.workflow_attempt != forged
    assert wrapper.inner.calls == 1


def test_captured_binding_is_persisted_and_mints_actual_attempt_identity(tmp_path) -> None:
    state = resolve_snapshot()
    forged = WorkflowAttemptIdentity(state.binding, RunId("provider-run"), "provider-leg")
    wrapper, store = _wrapper(
        tmp_path, Runtime(AgentResult(False, status="ok", cost_usd=1, workflow_attempt=forged)),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=state.binding)
    wrapper.set_run_identity("harness-run", "harness-leg")

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert result.status == "ok"
    assert result.workflow_attempt == WorkflowAttemptIdentity(state.binding, RunId("harness-run"), "harness-leg")
    from issue_agent.adapters.workflow_codec import decode_workflow
    with store._connect() as db:
        raw = db.execute("SELECT workflow_binding_json FROM runs WHERE run_id='harness-run'").fetchone()[0]
    assert decode_workflow(raw, WorkflowBinding) == state.binding


@pytest.mark.parametrize("body", ["x" * 65536, "😀" * 22000], ids=["ascii-capacity", "unicode-codec-limit"])
def test_oversized_recovery_inputs_are_refused_before_paid_admission(tmp_path, body):
    state = resolve_snapshot()
    provider = Runtime(AgentResult(False, status="done", cost_usd=1))
    wrapper, store = _wrapper(tmp_path, provider)
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=state.binding)
    # Include enough ASCII metadata to exceed the selected input envelope too.
    inputs = ResolveInputs("t" * 40000, body, "author")
    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2, resolve_inputs=inputs)))
    assert provider.calls == 0
    assert result.is_error
    assert "receipt capacity" in result.summary
    assert result.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert result.workflow_attempt is None
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0


def test_author_runtime_persists_fingerprint_of_supplied_inputs_before_provider_runs(tmp_path) -> None:
    from issue_agent.adapters.workflow_codec import decode_workflow
    from issue_agent.domain.workflow import ResolveAdmission
    from issue_agent.domain.resolve_split import SplitContext, SplitSelection

    state = resolve_snapshot()
    task = RuntimePlan("Author the issue contract", schema={"type": "object"},
                       append_system_prompt="Keep public behavior stable", max_budget_usd=2,
                       resolve_inputs=ResolveInputs("title", "body", "alice"),
                       split_input=SplitContext("fix/1-test", SplitSelection("codex", "<runtime-default>")))
    seen = []

    class InspectingRuntime:
        async def run(self, supplied):
            with store._connect() as db:
                row = db.execute(
                    "SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission' AND scope='o/r' AND key='author-run'",
                ).fetchone()
            receipt = decode_workflow(row[0], ResolveAdmission)
            seen.append(receipt)
            assert receipt.input_fingerprint == run_input_fingerprint(supplied.intent)
            return AgentResult(False, status="ok", cost_usd=1)

    wrapper, store = _wrapper(tmp_path, InspectingRuntime())
    wrapper = dataclasses.replace(wrapper, budget_policy=BudgetPolicy({("fix", "AUTHOR"): Money("USD", 2_000_000)}))
    wrapper.set_context("fix", "AUTHOR", 1, 1, workflow=state.binding)
    wrapper.set_run_identity("author-run", "author-leg")
    result = asyncio.run(wrapper.run(task))

    assert result.status == "ok"
    assert len(seen) == 1
    assert seen[0].input_fingerprint == run_input_fingerprint(task.intent)
    assert seen[0].attempt == result.workflow_attempt


@pytest.mark.parametrize("invalid", ["missing", "generation", "episode", "epoch", "target"])
def test_activated_resolve_rejects_missing_or_stale_authority_before_provider(tmp_path, invalid):
    state = resolve_snapshot()
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=1))
    wrapper, store = _wrapper(tmp_path, runtime)
    binding = state.binding
    if invalid == "missing":
        binding = None
    elif invalid == "generation":
        binding = dataclasses.replace(binding, fence=dataclasses.replace(binding.fence, generation=1))
    elif invalid == "episode":
        binding = dataclasses.replace(binding, episode_id="obsolete")
    elif invalid == "epoch":
        binding = dataclasses.replace(binding, authority=dataclasses.replace(binding.authority, epoch=2))
    else:
        binding = resolve_snapshot(999).binding
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=binding)

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert result.is_error
    assert runtime.calls == 0
    assert store.active_reservation_runs() == ()


@pytest.mark.parametrize("kind", ["ROUTE", "AUTHOR", "IMPLEMENT", "ADJUDICATE"])
def test_workflow_binding_survives_nested_leg_context_and_exception(tmp_path, kind) -> None:
    wrapper, _store = _wrapper(tmp_path, Runtime(AgentResult(False)))
    journaled = JournalingRuntime(
        wrapper, Journal(str(tmp_path / "events.jsonl")), repo="o/r", role="fix"
    )
    binding = WorkflowBinding(
        WorkflowFence(WorkflowKey("o/r", IssueRef(1)), 2),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1),
        "captured-episode",
    )
    journaled.set_context("fix", "RESOLVE", 1, 1, workflow=binding)

    with pytest.raises(RuntimeError, match="leg failed"):
        with journaled.leg_kind(kind):
            assert wrapper.workflow_binding == binding
            assert journaled.workflow_binding == binding
            raise RuntimeError("leg failed")

    assert journaled.workflow_binding == wrapper.workflow_binding == binding
    journaled.set_context("fix", "RESOLVE", 2, 2)
    assert journaled.workflow_binding is None
    assert wrapper.workflow_binding is None


def test_unpinned_codex_run_without_reported_model_uses_reserved_fallback(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1.25,
            session_id="resume-receipt",
            usage={
                "input_tokens": 100,
                "output_tokens": 10,
                "source": "rollout",
            },
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    result = asyncio.run(
        wrapper.run(RuntimePlan("resume", max_budget_usd=2))
    )

    assert result.status == "ok"
    assert store.active_reservation_runs() == ()
    with store._connect() as db:
        reservation_model = db.execute(
            "SELECT model FROM budget_reservations"
        ).fetchone()[0]
        amount, basis = db.execute(
            "SELECT amount_micros, basis FROM charges"
        ).fetchone()
    assert reservation_model == "<runtime-default>"
    assert (amount, basis) == (800, "estimate")


def test_global_only_cap_keeps_workload_repository(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1,
            session_id="global-receipt",
            usage={"input_tokens": 100, "output_tokens": 10},
        )
    )
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    install_workflow(store, with_issue_recovery(resolve_snapshot()))
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        None,
        Money("USD", 5_000_000),
        None,
        "cfg",
        "codex",
        _policy(),
        _rates(),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("work", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "ok"
    with store._connect() as db:
        attribution = db.execute(
            "SELECT repository, workload_repository "
            "FROM budget_reservations"
        ).fetchone()
    assert tuple(attribution) == ("*", "o/r")


def test_production_runtime_binds_paid_run_to_dispatch_claim(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1,
            session_id="receipt",
            usage={"input_tokens": 10, "output_tokens": 5},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(
        wrapper,
        owner_id="o/r:fix:deployment",
        require_dispatch_claim=True,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    assert store.claim_dispatch(
        "o/r",
        "fix",
        1,
        "RESOLVE",
        "o/r:fix:deployment",
        frozenset({"src/a.py"}),
    )

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert result.status == "ok"
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM dispatch_claim_runs "
            "WHERE repository='o/r' AND role='fix' AND target='issue:1'"
        ).fetchone()[0] == 1


def test_production_runtime_releases_admission_when_claim_is_missing(
    tmp_path,
) -> None:
    runtime = Runtime(AgentResult(False, status="must-not-run"))
    wrapper, store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(
        wrapper,
        owner_id="o/r:fix:deployment",
        require_dispatch_claim=True,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert result.status == "dispatch_claim_unavailable"
    assert runtime.calls == 0
    assert store.active_reservation_runs() == ()


def test_paid_run_carries_portable_ancestor_authority_bundle(
    tmp_path, monkeypatch
) -> None:
    class CapturingRuntime(Runtime):
        async def run(self, _task):
            self.task = _task
            return await super().run(_task)

    runtime = CapturingRuntime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1,
            session_id="receipt",
            usage={"input_tokens": 100, "output_tokens": 10},
        )
    )
    wrapper, _ = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(wrapper, github_host="github.example")
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    monkeypatch.setattr(
        accounting_runtime,
        "_fix_publication_context",
        lambda _role, _cwd: accounting_runtime._FixPublicationContext(
            str(tmp_path),
            "fix/1-test",
            "/usr/bin/git",
            "/usr/bin/gh",
            "/usr/bin:/bin",
        ),
    )

    asyncio.run(
        wrapper.run(
            RuntimePlan(
                "work",
                model="gpt-test",
                max_budget_usd=2,
                cwd=str(tmp_path),
            )
        )
    )

    encoded = runtime.task.env["ISSUE_AGENT_AUTHORITY_BUNDLE"]
    authority = json.loads(base64.urlsafe_b64decode(encoded).decode())
    assert authority["ISSUE_AGENT_GITHUB_HOST"] == "github.example"
    assert authority["ISSUE_AGENT_BASE_BRANCH"] == "dev"
    assert authority["ISSUE_AGENT_REPOSITORY"] == "o/r"
    assert authority["ISSUE_AGENT_CONTROL_DB"].endswith("control.db")
    assert authority["ISSUE_AGENT_RUN_ID"]
    assert authority["ISSUE_AGENT_OWNER_ID"]
    assert authority["ISSUE_AGENT_PUBLICATION_CWD"] == str(tmp_path)
    assert authority["ISSUE_AGENT_PUBLICATION_BRANCH"] == "fix/1-test"
    assert authority["ISSUE_AGENT_PUBLICATION_ISSUE"] == "1"
    assert Path(authority["ISSUE_AGENT_GIT_BIN"]).is_absolute()
    assert Path(authority["ISSUE_AGENT_GH_BIN"]).is_absolute()


def test_fix_publication_kind_blocks_before_paid_run_without_authority(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime(AgentResult(False, status="should-not-run"))
    wrapper, store = _wrapper(tmp_path, runtime)
    monkeypatch.setattr(
        accounting_runtime,
        "_fix_publication_context",
        lambda _role, _cwd: None,
    )

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", max_budget_usd=2))
    )

    assert result.status == "publication_authority_unavailable"
    assert result.exit_reason is ExitReason.AGENT_ERROR
    assert "publication authority" in result.summary
    assert runtime.calls == 0
    assert store.active_reservation_runs() == ()


def test_issue_less_pr_repair_keeps_branch_bound_publication_authority(
    tmp_path,
) -> None:
    class CapturingRuntime(Runtime):
        async def run(self, _task):
            self.task = _task
            return await super().run(_task)

    runtime = CapturingRuntime(
        AgentResult(
            False,
            status="ok",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    )
    wrapper, _store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(
        wrapper,
        issue_cap=None,
        budget_policy=BudgetPolicy(
            {("fix", "FIX_CI"): Money("USD", 2_000_000)}
        ),
    )
    wrapper.set_context("fix", "FIX_CI", 100, None)

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("repair", max_budget_usd=2, cwd=str(tmp_path))
        )
    )

    assert result.status == "ok"
    authority = json.loads(
        base64.urlsafe_b64decode(
            runtime.task.env["ISSUE_AGENT_AUTHORITY_BUNDLE"]
        ).decode()
    )
    assert authority["ISSUE_AGENT_PUBLICATION_ISSUE"] == "0"
    assert authority["ISSUE_AGENT_PUBLICATION_BRANCH"] == "fix/1-test"


def test_issue_less_review_keeps_repository_and_global_caps(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="reviewed",
            cost_usd=1,
            session_id="review-receipt",
            usage={"input_tokens": 100, "output_tokens": 10},
        )
    )
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "review",
        Money("USD", 5_000_000),
        Money("USD", 10_000_000),
        Money("USD", 25_000_000),
        "cfg",
        "claude",
        BudgetPolicy({("review", "REVIEW"): Money("USD", 2_000_000)}),
        _rates(),
    )
    wrapper.set_context("review", "REVIEW", 42, None)

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("review", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "reviewed"
    assert runtime.calls == 1
    with store._connect() as db:
        scopes = {
            tuple(row)
            for row in db.execute(
                "SELECT repository, dimension FROM budget_reservations "
                "UNION ALL "
                "SELECT repository, dimension FROM reservation_limits"
            )
        }
    assert scopes == {("o/r", "repository"), ("*", "global")}


def test_issue_less_review_binds_pr_claim_without_issue_budget_scope(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="reviewed",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)
    owner = "o/r:review:deployment"
    wrapper = dataclasses.replace(
        wrapper,
        role="review",
        owner_id=owner,
        require_dispatch_claim=True,
        budget_policy=BudgetPolicy(
            {("review", "REVIEW"): Money("USD", 2_000_000)}
        ),
    )
    assert store.claim_dispatch(
        "o/r",
        "review",
        -42,
        "REVIEW",
        owner,
        frozenset({"pr:42"}),
    )
    wrapper.set_context(
        "review",
        "REVIEW",
        42,
        None,
        claim_target=-42,
    )

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("review", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "reviewed"
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM dispatch_claim_runs "
            "WHERE repository='o/r' AND role='review' AND target='pr:42'"
        ).fetchone()[0] == 1
        dimensions = {
            row[0]
            for row in db.execute(
                "SELECT dimension FROM reservation_limits"
            )
        }
    assert dimensions == {"repository"}


def test_issue_less_multi_review_preserves_pr_claim_across_leg_context(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="reviewed",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    )
    accounting, store = _wrapper(tmp_path, runtime)
    owner = "o/r:review:deployment"
    accounting = dataclasses.replace(
        accounting,
        role="review",
        owner_id=owner,
        require_dispatch_claim=True,
        budget_policy=BudgetPolicy(
            {("review", "REVIEW_GATE"): Money("USD", 2_000_000)}
        ),
    )
    assert store.claim_dispatch(
        "o/r",
        "review",
        -42,
        "REVIEW",
        owner,
        frozenset({"pr:42"}),
    )
    journaled = JournalingRuntime(
        accounting,
        Journal(str(tmp_path / "events.jsonl")),
        repo="o/r",
        role="review",
    )
    journaled.set_context(
        "review",
        "REVIEW",
        42,
        None,
        claim_target=-42,
    )

    with journaled.leg_kind("REVIEW_GATE"):
        result = asyncio.run(
            journaled.run(
                RuntimePlan(
                    "review gate",
                    model="gpt-test",
                    max_budget_usd=2,
                )
            )
        )

    assert result.status == "reviewed"
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM dispatch_claim_runs "
            "WHERE repository='o/r' AND role='review' AND target='pr:42'"
        ).fetchone()[0] == 1


def test_linked_review_separates_issue_claim_from_pr_retry_key(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="reviewed",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)
    owner = "o/r:review:deployment"
    wrapper = dataclasses.replace(
        wrapper,
        role="review",
        owner_id=owner,
        require_dispatch_claim=True,
        budget_policy=BudgetPolicy(
            {("review", "REVIEW"): Money("USD", 2_000_000)}
        ),
    )
    assert store.claim_dispatch(
        "o/r",
        "review",
        9,
        "REVIEW",
        owner,
        frozenset({"pr:42"}),
    )
    wrapper.set_context(
        "review",
        "REVIEW",
        42,
        9,
        claim_target=9,
    )

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("review", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "reviewed"
    with store._connect() as db:
        payload = json.loads(
            db.execute("SELECT dispatch_id FROM runs").fetchone()[0]
        )
    assert payload["claim_target"] == 9
    assert payload["breaker_key"] == -42


def test_missing_issue_still_blocks_issue_scoped_fix_work(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="should-not-run"))
    wrapper, _store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(
        wrapper,
        issue_cap=Money("USD", 25_000_000),
    )
    wrapper.set_context("fix", "RESOLVE", 1, None)

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", max_budget_usd=2))
    )

    assert result.summary == (
        "paid admission blocked: issue spend scope is unavailable"
    )
    assert runtime.calls == 0


def test_resumed_session_uses_distinct_per_leg_settlement_receipts(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=0.1,
            session_id="resumed-session",
            usage={
                "input_tokens": 100,
                "output_tokens": 10,
                "source": "rollout",
            },
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    asyncio.run(
        wrapper.run(RuntimePlan("first", model="gpt-test", max_budget_usd=2))
    )
    asyncio.run(
        wrapper.run(RuntimePlan("second", model="gpt-test", max_budget_usd=2))
    )

    with store._connect() as db:
        receipts = tuple(
            row[0]
            for row in db.execute(
                "SELECT provider_receipt_id FROM charges ORDER BY charge_id"
            )
        )
    assert len(receipts) == 2
    assert len(set(receipts)) == 2
    assert all("resumed-session" in receipt for receipt in receipts)
    assert store.active_reservation_runs() == ()


def test_codex_missing_usage_never_settles_productive_run_as_free(
    tmp_path,
) -> None:
    wrapper, store = _wrapper(
        tmp_path,
        Runtime(
            AgentResult(
                False,
                status="pushed",
                cost_usd=0,
                session_id="usage-blind",
                usage=None,
            )
        ),
    )

    event_log = EventLog(
        str(tmp_path / "events-v2.jsonl"),
        Producer("deployment", "o/r", "fix", 100, "boot"),
    )
    store.bind_event_outbox(
        event_log.prepare,
        event_log.append_committed,
    )

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert result.status == "pushed"
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")
    events = tuple(decode_event_line(line) for line in (tmp_path / "events-v2.jsonl").read_text().splitlines())
    assert not any(isinstance(event.payload, ReservationRetained) for event in events)
    assert "interrupted requests charged zero" in (tmp_path / "events-v2.jsonl").read_text()


def test_stderr_codex_usage_cannot_release_a_paid_reservation(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="ok",
            cost_usd=1.25,
            session_id="total-only-receipt",
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_tokens": 125_000,
                "source": "stderr",
            },
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )

    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


def test_exhausted_cap_blocks_before_runtime(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="ok"))
    wrapper, _store = _wrapper(tmp_path, runtime, cap=1_000_000)

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert runtime.calls == 0
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED


def test_disabled_caps_still_reserve_and_settle_subscription_run(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(False, status="ok", cost_usd=0, session_id="subscription")
    )
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    install_workflow(store, with_issue_recovery(resolve_snapshot()))
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        None,
        None,
        None,
        "cfg",
        "claude",
        _policy(),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert result.status == "ok"
    assert runtime.calls == 1
    assert store.active_reservation_runs() == ()


def test_disabled_caps_still_price_observed_subscription_stream_usage(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="stream-only",
            cost_usd=0,
            usage=None,
            stream_usage={
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
            },
        )
    )
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    install_workflow(store, with_issue_recovery(resolve_snapshot()))
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        None,
        None,
        None,
        "cfg",
        "claude",
        _policy(),
        _rates(),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("work", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "stream-only"
    with store._connect() as db:
        amount, basis, usage_json, rate_card = db.execute(
            "SELECT amount_micros, basis, usage_json, rate_card_id "
            "FROM charges"
        ).fetchone()
    assert amount == 8_000_000
    assert basis == "estimate"
    assert json.loads(usage_json)["input_tokens"] == 1_000_000
    assert rate_card == "test"


def test_disabled_caps_honor_explicit_unpriced_codex_opt_out(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(False, status="untracked", cost_usd=0, usage=None)
    )
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    install_workflow(store, with_issue_recovery(resolve_snapshot()))
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        None,
        None,
        None,
        "cfg",
        "codex",
        _policy(),
        None,
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)

    result = asyncio.run(
        wrapper.run(RuntimePlan("work", max_budget_usd=2))
    )

    assert result.status == "untracked"
    assert runtime.calls == 1
    assert store.active_reservation_runs() == ()
    with store._connect() as db:
        amount, basis = db.execute(
            "SELECT amount_micros, basis FROM charges"
        ).fetchone()
    assert (amount, basis) == (0, "estimate")


def test_subscription_usage_is_synthetically_priced_and_consumes_cap(
    tmp_path,
) -> None:
    class SubscriptionRuntime:
        def __init__(self):
            self.calls = 0

        async def run(self, _task):
            self.calls += 1
            return AgentResult(
                False,
                status="ok",
                cost_usd=0,
                session_id=f"subscription-{self.calls}",
                usage={"input_tokens": 1_000_000, "output_tokens": 100_000},
            )

    runtime = SubscriptionRuntime()
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    install_workflow(store, with_issue_recovery(resolve_snapshot()))
    wrapper = AccountingRuntime(
        runtime,
        store,
        "o/r",
        "fix",
        Money("USD", 5_000_000),
        None,
        None,
        "cfg",
        "claude",
        _policy(),
        _rates(),
    )
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)

    first = asyncio.run(
        wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2))
    )
    second = asyncio.run(
        wrapper.run(RuntimePlan("more", model="gpt-test", max_budget_usd=2))
    )

    assert first.status == "ok"
    assert second.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1
    with store._connect() as db:
        charge = db.execute(
            "SELECT amount_micros, basis, rate_card_id FROM charges"
        ).fetchone()
    assert tuple(charge) == (8_000_000, "estimate", "test")


def test_armed_subscription_cap_prices_stream_usage_when_final_usage_is_missing(
    tmp_path,
) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="no-final-usage",
            cost_usd=0,
            session_id="stream-observed",
            usage=None,
            stream_usage={
                "input_tokens": 1_100_000,
                "output_tokens": 0,
            },
        )
    )
    wrapper, store = _wrapper(
        tmp_path,
        runtime,
        cap=2_000_000,
        runtime_name="claude",
    )

    first = asyncio.run(
        wrapper.run(
            RuntimePlan("work", model="gpt-test", max_budget_usd=2)
        )
    )
    second = asyncio.run(
        wrapper.run(
            RuntimePlan("more", model="gpt-test", max_budget_usd=2)
        )
    )

    assert first.status == "no-final-usage"
    assert second.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1
    with store._connect() as db:
        amount, basis, usage_json = db.execute(
            "SELECT amount_micros, basis, usage_json FROM charges"
        ).fetchone()
    assert amount == 5_500_000
    assert basis == "estimate"
    assert json.loads(usage_json)["input_tokens"] == 1_100_000


def test_armed_non_codex_run_without_any_usage_settles_zero_estimate(
    tmp_path,
) -> None:
    wrapper, store = _wrapper(
        tmp_path,
        Runtime(
            AgentResult(
                False,
                status="usage-blind",
                cost_usd=0,
                usage=None,
                stream_usage=None,
            )
        ),
        runtime_name="claude",
    )

    result = asyncio.run(
        wrapper.run(
            RuntimePlan("work", model="gpt-test", max_budget_usd=2)
        )
    )

    assert result.status == "usage-blind"
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


def test_codex_runtime_exception_releases_after_completed_teardown(
    tmp_path,
) -> None:
    class Death:
        async def run(self, _task):
            raise RuntimeError("transport died")

    wrapper, store = _wrapper(tmp_path, Death())
    try:
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))
    except RuntimeError:
        pass
    assert store.active_reservation_runs() == ()
    with store._connect() as db:
        state, reason = db.execute(
            "SELECT state, release_reason FROM budget_reservations"
        ).fetchone()
    assert (state, reason) == (
        "released",
        "runtime_exception_before_launch",
    )

    selected = store.read_issue_recovery(WorkflowKey("o/r", IssueRef(1)))
    assert isinstance(selected, cc.SelectedIssueRecovery)
    assert selected.component.state.infra_failures == 0
    assert selected.component.state.infra_episode is None


def test_malformed_usage_settles_zero_estimate_without_changing_work_result(
    tmp_path,
) -> None:
    wrapper, store = _wrapper(
        tmp_path,
        Runtime(
            AgentResult(
                False,
                status="ok",
                usage={"input_tokens": "malformed"},
            )
        ),
    )

    result = asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))
    assert result.status == "ok"
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (0, "estimate")


def test_forced_codex_shutdown_intent_preserves_reservation_for_recovery(
    tmp_path,
) -> None:
    class Death:
        def __init__(self):
            self.prepare_shutdown = lambda: None

        async def run(self, _task):
            self.prepare_shutdown()
            raise RuntimeError("killed")

    runtime = Death()
    wrapper, store = _wrapper(tmp_path, runtime)
    runtime.prepare_shutdown = lambda: store.record_owned_cancellation_intents(
        "o/r:fix", "shutdown_deadline_spend_unknown"
    )

    with pytest.raises(RuntimeError, match="killed"):
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert len(store.active_reservation_runs()) == 1


def test_non_codex_runtime_exception_releases_after_completed_teardown(
    tmp_path,
) -> None:
    class Death:
        async def run(self, _task):
            raise RuntimeError("transport died")

    wrapper, store = _wrapper(tmp_path, Death(), runtime_name="claude")

    with pytest.raises(RuntimeError, match="transport died"):
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert store.active_reservation_runs() == ()
    with store._connect() as db:
        state, reason = db.execute(
            "SELECT state, release_reason FROM budget_reservations"
        ).fetchone()
    assert (state, reason) == (
        "released",
        "runtime_exception_before_launch",
    )

    selected = store.read_issue_recovery(WorkflowKey("o/r", IssueRef(1)))
    assert isinstance(selected, cc.SelectedIssueRecovery)
    assert selected.component.state.infra_failures == 0
    assert selected.component.state.infra_episode is None


def test_runtime_exception_after_child_registration_retains_reservation(
    tmp_path,
) -> None:
    class Death:
        store: SQLiteControlStore
        run_id: RunId

        async def run(self, task):
            run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
            self.run_id = run_id
            with self.store._connect() as db:
                dispatch_id, owner_id = db.execute(
                    "SELECT dispatch_id, owner_id FROM runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            self.store.register_lease(
                Lease(
                    run_id,
                    dispatch_id,
                    owner_id,
                    os.getpid(),
                    os.getpgid(os.getpid()),
                    self.store._now(),
                    1,
                )
            )
            raise RuntimeError("transport died after provider launch")

    runtime = Death()
    wrapper, store = _wrapper(tmp_path, runtime, runtime_name="claude")
    runtime.store = store

    with pytest.raises(RuntimeError, match="after provider launch"):
        asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert store.active_reservation_runs() == (runtime.run_id,)
    with store._connect() as db:
        policy, run_state, outcome, reservation_state = db.execute(
            "SELECT rr.policy, r.state, r.outcome, b.state "
            "FROM reservation_recovery rr JOIN runs r ON r.run_id=rr.run_id "
            "JOIN budget_reservations b ON b.run_id=rr.run_id"
        ).fetchone()
    assert (policy, run_state, outcome, reservation_state) == (
        "reconcile_only",
        "completed",
        "spend_unreconciled",
        "active",
    )


def test_journal_supplied_run_identity_is_the_reservation_identity(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(False, status="ok", cost_usd=1, session_id="receipt")
    )
    wrapper, store = _wrapper(tmp_path, runtime)
    event_path = tmp_path / "events.jsonl"
    events = EventLog(
        str(event_path),
        Producer("deployment", "o/r", "fix", 123, "boot"),
    )
    store.bind_event_outbox(events.prepare, events.append_committed)
    wrapper.set_run_identity("journal-run", "journal-leg")

    asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    emitted = tuple(
        decode_event_line(line) for line in event_path.read_text().splitlines()
    )
    spend = next(
        event for event in emitted if isinstance(event.payload, SpendRecorded)
    )
    assert spend.correlation.run_id == "journal-run"


def test_successful_settlement_removes_crash_recovery_evidence(tmp_path) -> None:
    evidence_dir = str(tmp_path / "spend-evidence")
    wrapper, _store = _wrapper(
        tmp_path,
        Runtime(
            AgentResult(
                False,
                status="ok",
                cost_usd=1,
                session_id="receipt",
            )
        ),
        evidence_dir=evidence_dir,
    )
    wrapper.set_run_identity("known-run", "leg")
    write_spend_evidence(evidence_dir, "known-run", {"session_id": "receipt"})

    asyncio.run(wrapper.run(RuntimePlan("work", max_budget_usd=2)))

    assert not os.path.exists(spend_evidence_path(evidence_dir, "known-run"))


def test_concurrent_review_validators_share_context_without_false_dedup(
    tmp_path,
) -> None:
    class ConcurrentRuntime:
        def __init__(self):
            self.calls = 0
            self.ready = asyncio.Event()

        async def run(self, _task):
            self.calls += 1
            call = self.calls
            if call == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=1)
            return AgentResult(
                False,
                status="ok",
                cost_usd=1,
                session_id=f"validator-{call}",
            )

    runtime = ConcurrentRuntime()
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()

    def validator(run_id: str, leg_id: str) -> AccountingRuntime:
        wrapper = AccountingRuntime(
            runtime,
            store,
            "o/r",
            "review",
            Money("USD", 5_000_000),
            None,
            None,
            "cfg",
            "claude",
            BudgetPolicy(
                {
                    ("review", "VALIDATE_SECURITY"): Money(
                        "USD", 2_000_000
                    )
                }
            ),
        )
        wrapper.set_context("review", "VALIDATE_SECURITY", 7, 1)
        wrapper.set_run_identity(run_id, leg_id)
        return wrapper

    async def run_both():
        return await asyncio.gather(
            validator("run-a", "finding-a").run(
                RuntimePlan("a", max_budget_usd=2)
            ),
            validator("run-b", "finding-b").run(
                RuntimePlan("b", max_budget_usd=2)
            ),
        )

    results = asyncio.run(run_both())

    assert runtime.calls == 2
    assert [result.status for result in results] == ["ok", "ok"]
    assert store.active_reservation_runs() == ()


def test_reconciled_provisional_fix_refusal_does_not_persist_budget_hold(tmp_path):
    from issue_agent.domain.contracts import ReservationId, SettlementRequest, PricingBasis
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=.01))
    wrapper, store = _wrapper(tmp_path, runtime, cap=5_000_000)
    attempts = []
    def admit(request):
        if not attempts:
            overhang = dataclasses.replace(request, run_id=RunId("overhang"), reservation_id=ReservationId("overhang"),
                kind="overhang", owner_id="o/r:fix:overhang", dispatch_id="overhang", workflow=None, continuations=(),
                quote=dataclasses.replace(request.quote, maximum_charge=Money("USD", 4_000_000)))
            assert store.reserve(overhang).admitted
        attempts.append(request)
        return store.reserve(request)
    class Reconciler:
        def reconcile_stale_reservations(self):
            store.settle(SettlementRequest(RunId("overhang"), Money("USD", 0), PricingBasis.PROVIDER_REPORTED,
                                          "overhang-zero", None, None, None))
            return 1
    wrapper = dataclasses.replace(wrapper, admit=admit, reservation_reconciler=Reconciler())
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    result = asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert not result.is_error and runtime.calls == 1 and len(attempts) == 2, (result.summary, len(attempts))
    with store.transaction() as db:
        assert db.execute("SELECT sum(amount_micros) FROM charges").fetchone()[0] == 10000


@pytest.mark.parametrize("cap_kind,status", [
    ("repository", DAILY_HEADROOM_STATUS), ("global", DAILY_HEADROOM_STATUS), ("issue", MAX_BUDGET_STATUS),
])
def test_fix_admission_refusal_is_a_daily_wait_only_for_daily_caps(tmp_path, cap_kind, status):
    # Decision #7: daily/global windows refill with time, so their refusal is a
    # label-free wait; the lifetime issue cap needs a human decision. Neither
    # persists workflow state or opens a paid run.
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=.01))
    wrapper, store = _wrapper(tmp_path, runtime)
    wrapper = dataclasses.replace(wrapper, repository_cap=Money("USD", 0) if cap_kind == "repository" else None,
        global_cap=Money("USD", 0) if cap_kind == "global" else None,
        issue_cap=Money("USD", 0) if cap_kind == "issue" else None)
    wrapper.set_context("fix", "RESOLVE", 1, 1, workflow=resolve_snapshot().binding)
    key = WorkflowKey("o/r", IssueRef(1))
    before = store.read_issue_recovery(key)
    result = asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert result.is_error and result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert (result.status, result.envelope_subtype) == (status, status)
    assert runtime.calls == 0
    assert store.read_issue_recovery(key) == before
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0


@pytest.mark.parametrize("invalid", [None, True, -1, 1.5, "12", 10**100])
def test_malformed_primary_usage_preserves_valid_stream_estimate(tmp_path, invalid):
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="done",
        usage={"input_tokens": invalid, "output_tokens": 1},
        stream_usage={"input_tokens": 100, "output_tokens": 10})), runtime_name="claude")
    result = asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert result.status == "done" and store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (800, "estimate")


def test_valid_provider_dollars_survive_malformed_token_telemetry(tmp_path):
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="done", cost_usd=1.25,
        usage={"input_tokens": "bad"})), runtime_name="claude")
    asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (1_250_000, "provider_reported")


@pytest.mark.parametrize("invalid_cost", [float("nan"), float("inf"), -1, 10**100])
def test_unusable_provider_dollars_preserve_valid_stream_estimate(tmp_path, invalid_cost):
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="done", cost_usd=invalid_cost,
        stream_usage={"input_tokens": 100, "output_tokens": 10})), runtime_name="claude")
    asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))
    assert store.active_reservation_runs() == ()
    with store.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,basis FROM charges").fetchone()) == (800, "estimate")
