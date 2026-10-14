import asyncio
from contextlib import closing, contextmanager
import json
import os
import sqlite3
import stat
import sys

import pytest

from issue_agent.adapters.accounting_runtime import AccountingRuntime, _default_claim_target
from issue_agent.adapters.codex_runner import CodexRunner, spend_evidence_path, write_spend_evidence
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.contracts import Money, RunId
from issue_agent.domain.outcome import AgentResult, DAILY_HEADROOM_STATUS, ExitReason, MAX_BUDGET_STATUS
from issue_agent.domain.pricing import BudgetPolicy, ModelRates, RateTable, UnknownModelPolicy


def _policy(maximum=2_000_000):
    return BudgetPolicy({("fix", "RESOLVE"): Money("USD", maximum)})


def _rates():
    return RateTable(
        "test", "v1", "USD", {}, UnknownModelPolicy.USE_FALLBACK, ModelRates(5_000_000, 30_000_000, 500_000, 5_000_000)
    )


class Runtime:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def run(self, _task):
        self.calls += 1
        return self.result


def _lease(wrapper):
    """Take the item lease the roles hold before they run a paid leg."""
    issue, kind, number, claim_target, *_ = wrapper._context.get()
    target = claim_target if claim_target is not None else _default_claim_target(kind, number, issue)
    wrapper.store.lease(wrapper.repository, wrapper.role, target, wrapper._owner())


def _run(wrapper, plan):
    """Run one leg the way the roles do: holding its item lease."""
    _lease(wrapper)
    return asyncio.run(wrapper.run(plan))


def _work(wrapper, prompt="work", **plan):
    return _run(wrapper, RuntimePlan(prompt, max_budget_usd=2, **plan))


@contextmanager
def _db(store):
    """Raw SQL on the store file, for assertions the public API does not expose."""
    with closing(sqlite3.connect(store.path)) as db:
        db.row_factory = sqlite3.Row
        yield db
        db.commit()


def _ended(store, columns="charged_micros, basis"):
    with store._read() as db:
        return [tuple(row) for row in db.execute(f"SELECT {columns} FROM legs WHERE state='ended'")]


def _wrapper(tmp_path, runtime, cap=5_000_000, context=("fix", "RESOLVE", 1, 1), **fields):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    wrapper = AccountingRuntime(
        **{
            "inner": runtime,
            "store": store,
            "repository": "o/r",
            "role": "fix",
            "repository_cap": Money("USD", cap),
            "global_cap": None,
            "issue_cap": None,
            "config_version": "cfg",
            "runtime_name": "codex",
            "budget_policy": _policy(),
            "rate_table": _rates(),
            **fields,
        }
    )
    wrapper.set_context(*context)
    return wrapper, store


def _usage(**extra):
    return {"input_tokens": 100, "output_tokens": 10, **extra}


def test_paid_run_has_reservation_before_runtime_and_settles(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=1.25, session_id="receipt", usage=_usage()))
    wrapper, store = _wrapper(tmp_path, runtime)

    assert _work(wrapper, model="gpt-test").status == "ok"
    assert runtime.calls == 1
    blocked = _run(wrapper, RuntimePlan("more", max_budget_usd=4))
    assert blocked.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1


_ROLLOUT = {"input_tokens": 100, "output_tokens": 10, "source": "rollout"}
_STREAM = {"input_tokens": 100, "output_tokens": 10}

_ARMED, _CAPS_OFF, _UNPRICED = "armed", "caps-off", "unpriced"


@pytest.mark.parametrize(
    "result,runtime_name,pricing,charge",
    [
        # An unpinned leg with no reported model is priced at the fallback rates.
        (
            AgentResult(False, status="ok", cost_usd=1.25, session_id="r", usage=_ROLLOUT),
            "codex",
            _ARMED,
            (800, "estimate"),
        ),
        # Usage-blind, stderr-only or malformed usage is a marked zero estimate:
        # it settles (never stays reserved) without claiming a provider figure.
        (
            AgentResult(False, status="pushed", cost_usd=0, session_id="blind", usage=None),
            "codex",
            _ARMED,
            (0, "estimate"),
        ),
        (
            AgentResult(
                False,
                status="ok",
                cost_usd=1.25,
                session_id="s",
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 125_000, "source": "stderr"},
            ),
            "codex",
            _ARMED,
            (0, "estimate"),
        ),
        (
            AgentResult(False, status="blind", cost_usd=0, usage=None, stream_usage=None),
            "claude",
            _ARMED,
            (0, "estimate"),
        ),
        (AgentResult(False, status="ok", usage={"input_tokens": "malformed"}), "codex", _ARMED, (0, "estimate")),
        # Malformed, boundary and huge primary usage falls back to a valid stream estimate.
        *(
            (
                AgentResult(
                    False, status="done", usage={"input_tokens": bad, "output_tokens": 1}, stream_usage=_STREAM
                ),
                "claude",
                _ARMED,
                (800, "estimate"),
            )
            for bad in (None, True, -1, 1.5, "12", 10**100)
        ),
        # Caps off still prices observed stream usage at the configured rate card...
        (
            AgentResult(
                False,
                status="stream-only",
                cost_usd=0,
                usage=None,
                stream_usage={"input_tokens": 1_000_000, "output_tokens": 100_000},
            ),
            "claude",
            _CAPS_OFF,
            (8_000_000, "estimate"),
        ),
        # ...and an explicit unpriced opt-out (no rate card) still reserves and settles.
        (AgentResult(False, status="untracked", cost_usd=0, usage=None), "codex", _UNPRICED, (0, "estimate")),
        (AgentResult(False, status="ok", cost_usd=0, session_id="subscription"), "claude", _UNPRICED, (0, "estimate")),
        # Valid provider dollars survive malformed token telemetry; unusable ones
        # fall back to the stream estimate.
        (
            AgentResult(False, status="done", cost_usd=1.25, usage={"input_tokens": "bad"}),
            "claude",
            _ARMED,
            (1_250_000, "provider_reported"),
        ),
        *(
            (AgentResult(False, status="done", cost_usd=bad, stream_usage=_STREAM), "claude", _ARMED, (800, "estimate"))
            for bad in (float("nan"), float("inf"), -1, 10**100)
        ),
    ],
)
def test_a_completed_leg_settles_once_with_its_basis(tmp_path, result, runtime_name, pricing, charge) -> None:
    runtime = Runtime(result)
    rates = _rates() if pricing == _CAPS_OFF else None
    unarmed = {} if pricing == _ARMED else {"repository_cap": None, "rate_table": rates}
    wrapper, store = _wrapper(tmp_path, runtime, runtime_name=runtime_name, **unarmed)

    assert _work(wrapper).status == result.status

    assert runtime.calls == 1
    assert store.active_reservation_runs("o/r:fix") == ()
    assert _ended(store) == [charge]


def test_global_only_cap_keeps_workload_repository(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=1, session_id="global-receipt", usage=_usage()))
    wrapper, store = _wrapper(tmp_path, runtime, repository_cap=None, global_cap=Money("USD", 5_000_000))

    assert _work(wrapper, model="gpt-test").status == "ok"
    with store._read() as db:
        assert db.execute("SELECT repository FROM legs").fetchone()[0] == "o/r"


def test_a_leg_without_its_item_lease_is_refused_before_anything_runs(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="must-not-run"))
    wrapper, store = _wrapper(tmp_path, runtime, owner_id="o/r:fix:deployment")
    assert store.lease("o/r", "fix", 1, "o/r:fix:someone-else")

    result = asyncio.run(wrapper.run(RuntimePlan("work", model="gpt-test", max_budget_usd=2)))

    assert result.status == "dispatch_claim_unavailable"
    assert result.exit_reason is ExitReason.AGENT_ERROR
    assert runtime.calls == 0
    assert store.active_reservation_runs("o/r:fix") == ()
    # The refusal is a zero-cost machine end that spaces the retry.
    assert [run.receipt for run in store.leg_runs("o/r", (1,))] == [None]


def test_a_review_leg_leases_its_issue_and_records_its_pr(tmp_path) -> None:
    seen = []

    class Inspecting(Runtime):
        async def run(self, task):
            seen.extend((lease.role, lease.target) for lease in store.leases())
            return await super().run(task)

    runtime = Inspecting(AgentResult(False, status="reviewed", usage={"input_tokens": 1, "output_tokens": 1}))
    wrapper, store = _wrapper(
        tmp_path,
        runtime,
        context=("review", "REVIEW", 42, 9),
        role="review",
        owner_id="o/r:review:deployment",
        budget_policy=BudgetPolicy({("review", "REVIEW"): Money("USD", 2_000_000)}),
    )
    wrapper.set_context("review", "REVIEW", 42, 9, claim_target=9, head="abc")

    assert _run(wrapper, RuntimePlan("review", model="gpt-test", max_budget_usd=2)).status == "reviewed"

    assert seen == [("review", 9)]
    with store._read() as db:
        assert tuple(db.execute("SELECT role, kind, issue, number, head FROM legs").fetchone()) == (
            "review",
            "REVIEW",
            9,
            42,
            "abc",
        )


def test_missing_issue_still_blocks_issue_scoped_fix_work(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="should-not-run"))
    wrapper, _store = _wrapper(
        tmp_path, runtime, context=("fix", "RESOLVE", 1, None), issue_cap=Money("USD", 25_000_000)
    )

    assert _work(wrapper).summary == "paid admission blocked: issue spend scope is unavailable"
    assert runtime.calls == 0


def test_resumed_session_uses_distinct_per_leg_settlement_receipts(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(False, status="ok", cost_usd=0.1, session_id="resumed-session", usage=_usage(source="rollout"))
    )
    wrapper, store = _wrapper(tmp_path, runtime)

    _work(wrapper, "first", model="gpt-test")
    wrapper.store.unlease("o/r", "fix", 1, "o/r:fix")  # the role hands its claim back after each leg
    _work(wrapper, "second", model="gpt-test")

    receipts = [receipt for (receipt,) in _ended(store, "provider_receipt_id")]
    assert len(receipts) == 2
    assert len(set(receipts)) == 2
    assert all("resumed-session" in receipt for receipt in receipts)
    assert store.active_reservation_runs("o/r:fix") == ()


def test_exhausted_cap_blocks_before_runtime(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="ok"))
    wrapper, _store = _wrapper(tmp_path, runtime, cap=1_000_000)

    result = _work(wrapper)

    assert runtime.calls == 0
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED


def test_subscription_usage_is_synthetically_priced_and_consumes_cap(tmp_path) -> None:
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
    wrapper, store = _wrapper(tmp_path, runtime, runtime_name="claude")

    first = _work(wrapper, model="gpt-test")
    second = _work(wrapper, "more", model="gpt-test")

    assert first.status == "ok"
    assert second.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1
    assert _ended(store, "charged_micros, basis, rate_card_id") == [(8_000_000, "estimate", "test")]


def test_armed_subscription_cap_prices_stream_usage_when_final_usage_is_missing(tmp_path) -> None:
    runtime = Runtime(
        AgentResult(
            False,
            status="no-final-usage",
            cost_usd=0,
            session_id="stream-observed",
            usage=None,
            stream_usage={"input_tokens": 1_100_000, "output_tokens": 0},
        )
    )
    wrapper, store = _wrapper(tmp_path, runtime, cap=2_000_000, runtime_name="claude")

    first = _work(wrapper, model="gpt-test")
    second = _work(wrapper, "more", model="gpt-test")

    assert first.status == "no-final-usage"
    assert second.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1
    [(amount, basis, usage_json)] = _ended(store, "charged_micros, basis, usage_json")
    assert (amount, basis) == (5_500_000, "estimate")
    assert json.loads(usage_json)["input_tokens"] == 1_100_000


def test_codex_runtime_exception_releases_after_completed_teardown(tmp_path) -> None:
    class Death:
        async def run(self, _task):
            raise RuntimeError("transport died")

    wrapper, store = _wrapper(tmp_path, Death())
    with pytest.raises(RuntimeError):
        _work(wrapper)
    assert store.active_reservation_runs("o/r:fix") == ()
    with store._read() as db:
        assert tuple(db.execute("SELECT state, end_reason FROM legs").fetchone()) == (
            "released",
            "runtime_exception_before_launch",
        )

    # A machine failure is no attempt: the ended run carries no leg receipt.
    assert [run.receipt for run in store.leg_runs("o/r", (1,))] == [None]


def test_forced_codex_shutdown_intent_preserves_reservation_for_recovery(tmp_path) -> None:
    class Death:
        async def run(self, _task):
            self.prepare_shutdown()
            raise RuntimeError("killed")

    runtime = Death()
    wrapper, store = _wrapper(tmp_path, runtime)
    runtime.prepare_shutdown = lambda: store.record_owned_cancellation_intents(
        "o/r:fix", "shutdown_deadline_spend_unknown"
    )

    with pytest.raises(RuntimeError, match="killed"):
        _work(wrapper)

    assert len(store.active_reservation_runs("o/r:fix")) == 1
    assert store.leases() != ()  # still held: the leg's process may outlive the kill


def test_runtime_exception_after_child_registration_retains_reservation(tmp_path) -> None:
    class Death:
        store: SQLiteControlStore
        run_id: RunId

        async def run(self, task):
            self.run_id = RunId(task.env["ISSUE_AGENT_RUN_ID"])
            self.store.register_process_lease(
                self.run_id,
                expected_owner_id=task.env["ISSUE_AGENT_OWNER_ID"],
                pid=os.getpid(),
                pgid=os.getpgid(os.getpid()),
                process_start_ticks=1,
            )
            raise RuntimeError("transport died after provider launch")

    runtime = Death()
    wrapper, store = _wrapper(tmp_path, runtime, runtime_name="claude")
    runtime.store = store

    with pytest.raises(RuntimeError, match="after provider launch"):
        _work(wrapper)

    assert store.active_reservation_runs("o/r:fix") == (runtime.run_id,)
    with store._read() as db:
        state, ended_at, reason = db.execute("SELECT state, ended_at, end_reason FROM legs").fetchone()
    assert (state, ended_at is not None, reason) == (
        "running",
        True,
        "runtime failed after paid execution may have started",
    )
    assert store.leases() == ()


def test_successful_settlement_removes_crash_recovery_evidence(tmp_path) -> None:
    evidence_dir = str(tmp_path / "spend-evidence")
    wrapper, _store = _wrapper(
        tmp_path,
        Runtime(AgentResult(False, status="ok", cost_usd=1, session_id="receipt")),
        spend_evidence_dir=evidence_dir,
    )
    wrapper.set_run_identity("known-run", "leg")
    write_spend_evidence(evidence_dir, "known-run", {"session_id": "receipt"})

    _work(wrapper)

    assert not os.path.exists(spend_evidence_path(evidence_dir, "known-run"))


@pytest.mark.parametrize(
    "cap_kind,status",
    [("repository", DAILY_HEADROOM_STATUS), ("global", DAILY_HEADROOM_STATUS), ("issue", MAX_BUDGET_STATUS)],
)
def test_fix_admission_refusal_is_a_daily_wait_only_for_daily_caps(tmp_path, cap_kind, status):
    # Decision #7: daily/global windows refill with time, so their refusal is a
    # label-free wait; the lifetime issue cap needs a human decision. Neither
    # opens a paid run; the zero-cost refused row spaces the retry (#262).
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=0.01))
    wrapper, store = _wrapper(tmp_path, runtime, **{"repository_cap": None, f"{cap_kind}_cap": Money("USD", 0)})
    result = _work(wrapper, model="gpt-test")
    assert result.is_error and result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert (result.status, result.envelope_subtype) == (status, status)
    assert runtime.calls == 0
    with store._read() as db:
        assert [tuple(row) for row in db.execute("SELECT state, end_reason, reserved_micros FROM legs")] == [
            ("released", "admission_refused", 0)
        ]
    assert [run.receipt for run in store.leg_runs("o/r", (1,))] == [None]


def test_a_completed_leg_settles_and_the_daily_cap_gates_with_no_event_log_or_outbox(tmp_path) -> None:
    runtime = Runtime(AgentResult(False, status="ok", cost_usd=1.25, session_id="receipt"))
    wrapper, store = _wrapper(tmp_path, runtime)

    settled = _work(wrapper, model="gpt-test")
    refused = _run(wrapper, RuntimePlan("more", model="gpt-test", max_budget_usd=4))

    assert settled.status == "ok"
    assert refused.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert runtime.calls == 1
    assert _ended(store, "charged_micros") == [(1_250_000,)]
    with store._read() as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name IN ('event_outbox','world_observations')").fetchall()
            == []
        )
    assert sorted(path.name for path in tmp_path.iterdir() if path.suffix == ".jsonl") == []


def _codex_rollout(tmp_path, session: str, info: str, tail: str) -> CodexRunner:
    """A codex binary that writes one rollout session (``info`` is the
    token_count payload, as Python source) and then runs ``tail``."""
    rollout = (
        "import json,os,datetime;d=os.path.join(os.environ['CODEX_HOME'],'sessions','2026','10','13');"
        "os.makedirs(d,exist_ok=True);now=datetime.datetime.now(datetime.timezone.utc).isoformat();"
        f"open(os.path.join(d,'rollout-now-{session}.jsonl'),'w').write("
        f"json.dumps({{'timestamp':now,'type':'session_meta','payload':{{'id':'{session}','originator':'codex_exec',"
        "'source':'exec','cwd':os.getcwd()}})+'\\n'+json.dumps({'timestamp':now,'type':'event_msg','payload':"
        f"{{'type':'token_count','info':{info}}}}})+'\\n')"
    )
    codex = tmp_path / "codex"
    codex.write_text(f'#!/usr/bin/env bash\n{sys.executable} -c "{rollout}"\n{tail}')
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    return CodexRunner(
        codex_bin=str(codex),
        env={"OPENAI_API_KEY": "k", "CODEX_HOME": str(tmp_path / "codex-home")},
        rate_table=_rates(),
    )


def test_a_launch_that_drops_mid_run_is_settled_at_its_real_cost_and_the_caps_see_it(tmp_path) -> None:
    """A leg is one launch (slice 9 review P0): a codex run that spends and
    then loses its stream settles what its rollout measured, so the daily
    cap refuses the next leg instead of seeing only a cheap last attempt."""
    usage = "{'input_tokens':300000,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':10000}"
    runner = _codex_rollout(
        tmp_path,
        "sess-dear",
        f"{{'total_token_usage':{usage},'last_token_usage':{usage}}}",
        'echo "stream disconnected before completion" >&2\nexit 1\n',
    )
    wrapper, store = _wrapper(tmp_path, runner, cap=3_000_000)

    dropped = _work(wrapper, model="gpt-test")
    refused = _work(wrapper, "again", model="gpt-test")

    assert dropped.exit_reason is ExitReason.TRANSPORT_DEATH
    # 300k input at $5/M + 10k output at $30/M.
    assert _ended(store, "charged_micros") == [(1_800_000,)]
    assert refused.exit_reason is ExitReason.BUDGET_EXHAUSTED


def test_a_leg_whose_counters_are_malformed_settles_its_reserved_maximum_and_the_caps_see_it(tmp_path) -> None:
    """Unknown spend is charged at the reservation's bound, never at zero:
    the daily cap then refuses the next leg."""
    runner = _codex_rollout(tmp_path, "sess-bad", "{'total_token_usage':{'input_tokens':'x'}}", "exit 0\n")
    wrapper, store = _wrapper(tmp_path, runner, cap=3_000_000)

    malformed = _work(wrapper, model="gpt-test")
    refused = _work(wrapper, "again", model="gpt-test")

    assert malformed.exit_reason is ExitReason.AGENT_ERROR
    assert _ended(store) == [(2_000_000, "estimate")]
    assert refused.exit_reason is ExitReason.BUDGET_EXHAUSTED


# ── TRIAGE results and routed tiers (owner, 2026-10-13) ─────────────────────


def _light_wrapper(tmp_path, kind, result, **fields):
    from issue_agent.domain.contracts import Money as M

    return _wrapper(tmp_path, Runtime(result), context=("fix", kind, 3, 3),
                    budget_policy=BudgetPolicy({("fix", kind): M("USD", 2_000_000)}), **fields)


def _receipt(store):
    (run,) = store.leg_runs("o/r", (3,))
    return run.receipt


def test_a_triage_result_records_the_tier_and_nothing_the_steward_owns(tmp_path) -> None:
    claim = {"tier": "low", "rationale": "small", "acceptance_criteria": ["smuggled"], "duplicate_of": 2}
    wrapper, store = _light_wrapper(tmp_path, "TRIAGE", AgentResult(False, structured=claim, session_id="s"))
    _work(wrapper)
    receipt = _receipt(store)
    assert receipt["leg"] == "agent"
    assert receipt["triage"] == {"tier": "low", "rationale": "small"} and "steward" not in receipt


def test_a_steward_result_is_recorded_laundered_against_the_issues_the_harness_offered(tmp_path) -> None:
    from issue_agent.domain.triage import steward_schema

    claim = {"acceptance_criteria": ["Esc closes it"], "duplicate_of": 9, "duplicate_reason": "same",
             "blocked_by": 2, "blocked_by_reason": "needs it", "touches": ["./src/ui/", 7]}
    wrapper, store = _light_wrapper(tmp_path, "STEWARD", AgentResult(False, structured=claim, session_id="s"))
    _work(wrapper, schema=steward_schema([2, 4], blockable=[4]))  # 9 never offered; 2 offered but not open; 7 not a path
    receipt = _receipt(store)
    assert receipt["leg"] == "agent"
    assert receipt["steward"] == {"acceptance_criteria": ["Esc closes it"], "duplicate_of": None,
                                  "duplicate_reason": "", "blocked_by": None, "blocked_by_reason": "",
                                  "touches": ["src/ui"]} and "triage" not in receipt


def test_a_steward_dependency_on_an_offered_open_issue_is_recorded_with_its_path_guesses(tmp_path) -> None:
    from issue_agent.domain.triage import steward_schema

    claim = {"acceptance_criteria": [], "duplicate_of": None, "duplicate_reason": "",
             "blocked_by": 4, "blocked_by_reason": "needs the API #4 adds", "touches": ["./src/ui/", "docs"]}
    wrapper, store = _light_wrapper(tmp_path, "STEWARD", AgentResult(False, structured=claim, session_id="s"))
    _work(wrapper, schema=steward_schema([2, 4], blockable=[4]))
    steward = _receipt(store)["steward"]
    assert (steward["blocked_by"], steward["blocked_by_reason"], steward["touches"]) == (
        4, "needs the API #4 adds", ["src/ui", "docs"])


def test_steward_criteria_are_dropped_when_the_issue_states_its_own(tmp_path) -> None:
    from issue_agent.domain.triage import steward_schema

    claim = {"duplicate_of": None, "duplicate_reason": "", "acceptance_criteria": ["invented"]}
    wrapper, store = _light_wrapper(tmp_path, "STEWARD", AgentResult(False, structured=claim, session_id="s"))
    _work(wrapper, schema=steward_schema([], criteria_wanted=False))
    assert _receipt(store)["steward"]["acceptance_criteria"] == []


@pytest.mark.parametrize("kind", ["TRIAGE", "STEWARD"])
@pytest.mark.parametrize("structured", [
    None, {"tier": "enormous"}, {"tier": "low", "rationale": "x" * 100_000}, {"acceptance_criteria": "x"},
    {"acceptance_criteria": ["x" * 100_000]},
])
def test_a_malformed_or_huge_light_result_records_an_agent_leg_with_no_result(tmp_path, kind, structured) -> None:
    wrapper, store = _light_wrapper(tmp_path, kind, AgentResult(False, structured=structured, session_id="s"))
    _work(wrapper)
    receipt = _receipt(store)
    assert receipt["leg"] == "agent" and "triage" not in receipt and "steward" not in receipt


@pytest.mark.parametrize("kind", ["TRIAGE", "STEWARD"])
def test_a_light_act_that_claims_blocked_is_not_a_question(tmp_path, kind) -> None:
    wrapper, store = _light_wrapper(tmp_path, kind, AgentResult(False, status="blocked", needs="?", session_id="s"))
    _work(wrapper)
    assert _receipt(store)["leg"] == "agent" and "question" not in _receipt(store)


def test_the_routed_tier_a_leg_ran_at_is_recorded_with_it(tmp_path) -> None:
    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="pushed", session_id="s")),
                              context=("fix", "RESOLVE", 3, 3))
    wrapper.set_context("fix", "RESOLVE", 3, 3, tier="high")
    _work(wrapper)
    assert _receipt(store)["tier"] == "high"


def test_a_leg_planned_for_another_runtime_is_refused_by_this_runtimes_accounting(tmp_path) -> None:
    from issue_agent.domain.agent_task import RuntimeName

    runtime = Runtime(AgentResult(False, status="pushed", session_id="s"))
    wrapper, store = _wrapper(tmp_path, runtime, context=("fix", "RESOLVE", 3, 3))  # a codex chain
    result = _work(wrapper, runtime=RuntimeName.CLAUDE)
    assert result.is_error and runtime.calls == 0


def test_the_effort_a_plan_carried_is_recorded_and_absent_when_none_was_set(tmp_path) -> None:
    from issue_agent.domain.effort import Effort

    wrapper, store = _wrapper(tmp_path, Runtime(AgentResult(False, status="pushed", session_id="s")),
                              context=("fix", "RESOLVE", 3, 3))
    _work(wrapper, effort=Effort.HIGH)
    assert _receipt(store)["effort"] == "high"
    wrapper2, store2 = _wrapper(tmp_path / "b", Runtime(AgentResult(False, status="pushed", session_id="s")),
                                context=("fix", "RESOLVE", 3, 3))
    _work(wrapper2)
    assert "effort" not in _receipt(store2)
