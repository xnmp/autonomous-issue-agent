"""Agent-result interpretation — envelope over prose, ground truth over status."""

from issue_agent.domain.outcome import (
    AgentResult,
    ExitReason,
    MAX_BUDGET_STATUS,
    Outcome,
    classify_outcome,
    deterministic_death,
    infra_death,
    offending_path,
)


def _ok(status="pr_opened", **kw):
    return AgentResult(is_error=False, status=status, **kw)


def test_error_envelope_wins():
    assert classify_outcome(AgentResult(is_error=True, status="pr_opened")) is Outcome.ERROR


def test_status_maps_to_outcome():
    assert classify_outcome(_ok("pr_opened")) is Outcome.OK
    assert classify_outcome(_ok("pushed")) is Outcome.OK
    assert classify_outcome(_ok("blocked")) is Outcome.BLOCKED


def test_unknown_status_defaults_ok():
    assert classify_outcome(_ok("")) is Outcome.OK


# ── infra death: WHY the session ended, separate from WHAT it decided ─────────
def test_infra_death_only_for_external_kill_and_transport_death():
    assert infra_death(ExitReason.KILLED_EXTERNALLY) is True
    assert infra_death(ExitReason.TRANSPORT_DEATH) is True


def test_timeouts_are_attempts_not_infra_deaths():
    # A hang is NOT free: the agent was alive and had its chance, so a reliably
    # hanging task must still march the breaker forward rather than loop at full
    # session cost forever.
    assert infra_death(ExitReason.IDLE_TIMEOUT) is False
    assert infra_death(ExitReason.WALL_TIMEOUT) is False


def test_agent_side_reasons_are_not_infra_deaths():
    assert infra_death(ExitReason.OK) is False
    assert infra_death(ExitReason.AGENT_ERROR) is False
    assert infra_death(ExitReason.BUDGET_EXHAUSTED) is False
    assert infra_death(ExitReason.OPERATOR_CANCELLED) is False
    assert infra_death(ExitReason.NO_RESULT) is False


def test_unclassified_result_is_never_an_infra_death():
    # None = a legacy/hand-built result. Every consumer must keep pre-taxonomy
    # behavior, so adopting the field is additive and can't silently change policy.
    assert infra_death(None) is False


# ── deterministic deaths: the retry cannot win, so it must not be bought (#13) ─
_BUFFER_DEATH = (
    "CLIJSONDecodeError: Fatal error in message reader: Failed to decode JSON: "
    "JSON message exceeded maximum buffer size of 1048576 bytes"
)


def test_buffer_overflow_death_is_deterministic():
    d = deterministic_death(_BUFFER_DEATH)
    assert d is not None
    assert d.signature == "exceeded maximum buffer size"


def test_deterministic_death_names_the_file_the_session_died_on():
    # The operator's first question is "which file?" — the answer is the last
    # tool call the runtime traced before the transport went down.
    d = deterministic_death(_BUFFER_DEATH, "AssistantMessage → Read(evidence/ac-3-edge-case-titles.png)")
    assert d.path == "evidence/ac-3-edge-case-titles.png"


def test_ordinary_transport_deaths_are_not_deterministic():
    # These are the stochastic shape the breakers were built for: a retry may
    # well survive, so classifying them here would escalate PRs a retry rescues.
    assert deterministic_death("RuntimeError: Stream closed before any result") is None
    assert deterministic_death("ProcessError: exit status 1") is None
    assert deterministic_death("ConnectionResetError: [Errno 104]") is None


def test_empty_and_missing_text_is_not_deterministic():
    assert deterministic_death("") is None
    assert deterministic_death(None) is None


def test_deterministic_death_without_a_trace_still_classifies():
    d = deterministic_death(_BUFFER_DEATH, "")
    assert d is not None and d.path == ""


def test_offending_path_takes_the_last_tool_call_and_never_raises():
    assert offending_path("AssistantMessage → Bash(ls), Read(a/b.png)") == "a/b.png"
    assert offending_path("AssistantMessage") == ""
    assert offending_path("") == ""
    assert offending_path(None) == ""
    assert offending_path("Read(") == ""


# ── classify_leg: what one returned fix leg counts as (slice 5b) ────────────
import pytest

from dataclasses import replace

from issue_agent.domain.outcome import PROVIDER_REFUSED_SUBTYPE, LegClass, classify_leg, resumable_session


@pytest.mark.parametrize(
    "reason",
    [
        ExitReason.TRANSPORT_DEATH,
        ExitReason.KILLED_EXTERNALLY,
        ExitReason.RUNTIME_UNAVAILABLE,
        ExitReason.OPERATOR_CANCELLED,
    ],
)
@pytest.mark.parametrize("resumed", [False, True])
def test_a_harness_or_provider_death_is_never_an_attempt(reason, resumed):
    result = AgentResult(is_error=True, status="", exit_reason=reason)
    assert classify_leg(result, resumed=resumed) is LegClass.MACHINE


_WALL = AgentResult(is_error=True, status="", exit_reason=ExitReason.WALL_TIMEOUT, session_id="s-wall")
_SPENT = AgentResult(is_error=True, status=MAX_BUDGET_STATUS, envelope_subtype=MAX_BUDGET_STATUS,
                     exit_reason=ExitReason.BUDGET_EXHAUSTED, session_id="s-spent")


@pytest.mark.parametrize("capped", [_WALL, _SPENT], ids=["wall", "budget"])
def test_a_leg_cap_is_a_machine_end_once_then_an_attempt_on_the_resume(capped):
    # Owner, 2026-10-14: a per-leg budget hit is a wall timeout, one rule.
    assert classify_leg(capped, resumed=False) is LegClass.MACHINE
    assert classify_leg(capped, resumed=True) is LegClass.AGENT


@pytest.mark.parametrize("capped", [_WALL, _SPENT], ids=["wall", "budget"])
def test_a_leg_cap_leaves_its_session_resumable(capped):
    assert resumable_session(capped) == capped.session_id
    assert resumable_session(replace(capped, session_id="")) == ""


def test_a_blocked_return_is_a_question_and_an_error_is_agent_work():
    assert classify_leg(_ok("blocked", needs="which API?"), resumed=False) is LegClass.ASKED
    assert classify_leg(AgentResult(is_error=True, status="blocked"), resumed=False) is LegClass.AGENT
    assert classify_leg(_ok("pushed"), resumed=False) is LegClass.AGENT
    assert classify_leg(_ok("failed"), resumed=True) is LegClass.AGENT


def test_unknown_or_garbage_status_counts_as_agent_work():
    for status in ("", "pr_opened", "x" * 100_000, "BLOCKED"):
        assert classify_leg(_ok(status), resumed=False) is LegClass.AGENT


@pytest.mark.parametrize("reason", [ExitReason.TRANSPORT_DEATH, ExitReason.AGENT_ERROR])
def test_a_provider_refused_end_is_machine_fresh_and_an_attempt_on_its_resume(reason):
    # Claude's api_error and codex's provider failure share this marker; a
    # permanent provider error must not loop free (#418).
    ended = AgentResult(is_error=True, exit_reason=reason, envelope_subtype=PROVIDER_REFUSED_SUBTYPE, session_id="s")
    assert classify_leg(ended, resumed=False) is LegClass.MACHINE
    assert classify_leg(ended, resumed=True) is LegClass.AGENT
