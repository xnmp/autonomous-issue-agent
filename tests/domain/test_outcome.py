"""Agent-result interpretation — envelope over prose, ground truth over status."""

from issue_agent.domain.outcome import (
    AgentResult,
    ExitReason,
    MAX_BUDGET_STATUS,
    deterministic_death,
    infra_death,
    offending_path,
)


def _ok(status="pr_opened", **kw):
    return AgentResult(is_error=False, status=status, **kw)


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

from issue_agent.domain.outcome import (
    CUT_OFF_ENDS, PROVIDER_REFUSED_SUBTYPE, LegClass, classify_leg, machine_end, resumable_session,
)


@pytest.mark.parametrize(
    "reason",
    [
        ExitReason.TRANSPORT_DEATH,
        ExitReason.KILLED_EXTERNALLY,
        ExitReason.RUNTIME_UNAVAILABLE,
        ExitReason.OPERATOR_CANCELLED,
    ],
)
def test_a_harness_or_provider_death_is_recorded_as_a_machine_end(reason):
    result = AgentResult(is_error=True, status="", exit_reason=reason)
    assert classify_leg(result) is LegClass.MACHINE


_WALL = AgentResult(is_error=True, status="", exit_reason=ExitReason.WALL_TIMEOUT, session_id="s-wall")
_SPENT = AgentResult(is_error=True, status=MAX_BUDGET_STATUS, envelope_subtype=MAX_BUDGET_STATUS,
                     exit_reason=ExitReason.BUDGET_EXHAUSTED, session_id="s-spent")


@pytest.mark.parametrize("capped", [_WALL, _SPENT], ids=["wall", "budget"])
def test_a_leg_cap_is_recorded_as_a_cut_off_machine_end(capped):
    # Owner, 2026-10-14: a per-leg budget hit is a wall timeout, one rule. Whether
    # it is an attempt is `domain.attempt`'s, from the legs before it (#441).
    assert classify_leg(capped) is LegClass.MACHINE
    assert machine_end(capped) in CUT_OFF_ENDS


@pytest.mark.parametrize("capped", [_WALL, _SPENT], ids=["wall", "budget"])
def test_a_leg_cap_leaves_its_session_resumable(capped):
    assert resumable_session(capped) == capped.session_id
    assert resumable_session(replace(capped, session_id="")) == ""


def test_a_blocked_return_is_a_question_and_an_error_is_agent_work():
    assert classify_leg(_ok("blocked", needs="which API?")) is LegClass.ASKED
    # The envelope wins over the claim: an error that claims `blocked` asked no one.
    assert classify_leg(AgentResult(is_error=True, status="blocked")) is LegClass.AGENT
    assert classify_leg(_ok("pushed")) is LegClass.AGENT
    assert classify_leg(_ok("failed")) is LegClass.AGENT


def test_unknown_or_garbage_status_counts_as_agent_work():
    for status in ("", "pr_opened", "x" * 100_000, "BLOCKED"):
        assert classify_leg(_ok(status)) is LegClass.AGENT


@pytest.mark.parametrize("reason", [ExitReason.TRANSPORT_DEATH, ExitReason.AGENT_ERROR])
def test_a_provider_refused_end_is_recorded_as_a_cut_off_machine_end(reason):
    # Claude's api_error and codex's provider failure share this marker (#418);
    # `domain.attempt` bounds its resume like a leg cap's.
    ended = AgentResult(is_error=True, exit_reason=reason, envelope_subtype=PROVIDER_REFUSED_SUBTYPE, session_id="s")
    assert classify_leg(ended) is LegClass.MACHINE
    assert machine_end(ended) in CUT_OFF_ENDS
    assert resumable_session(ended) == "s"
