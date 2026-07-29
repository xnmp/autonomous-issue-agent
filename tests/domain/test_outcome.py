"""Agent-result interpretation — envelope over prose, ground truth over status."""

from issue_agent.domain.outcome import (
    AgentResult,
    ExitReason,
    Outcome,
    ResolveFollowup,
    classify_outcome,
    deterministic_death,
    infra_death,
    offending_path,
    pr_resume_actions,
    resolve_followup,
)
from issue_agent.domain.actions import Relabel, Target
from issue_agent.domain.labels import Label


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


def test_ground_truth_pr_open_beats_blocked_prose():
    # The model said blocked, but a PR actually opened → success (ground truth).
    res = AgentResult(is_error=False, status="blocked", needs="???")
    assert resolve_followup(res, pr_opened=True) is ResolveFollowup.PR_OPENED


def test_blocked_without_pr_asks_human():
    res = AgentResult(is_error=False, status="blocked", needs="which endpoint?")
    assert resolve_followup(res, pr_opened=False) is ResolveFollowup.BLOCKED


def test_error_without_pr_retries():
    res = AgentResult(is_error=True)
    assert resolve_followup(res, pr_opened=False) is ResolveFollowup.RETRY


# ── field_bool: structured boolean accessor + fail-safe default ───────────────
def test_field_bool_reads_present_true_and_false():
    r = AgentResult(is_error=False, structured={"reviewable": True})
    assert r.field_bool("reviewable") is True
    r2 = AgentResult(is_error=False, structured={"reviewable": False})
    assert r2.field_bool("reviewable") is False


def test_field_bool_missing_key_uses_default():
    # A degenerate but non-error envelope (empty dict) must fall back to the
    # caller's default — the gate passes default=True so it fails SAFE (runs the
    # full review) rather than silently approving on a missing field.
    r = AgentResult(is_error=False, structured={})
    assert r.field_bool("reviewable", default=True) is True
    assert r.field_bool("reviewable") is False        # default default is False
    assert AgentResult(is_error=False, structured=None).field_bool("x", default=True) is True


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
    assert infra_death(ExitReason.NO_RESULT) is False


def test_unclassified_result_is_never_an_infra_death():
    # None = a legacy/hand-built result. Every consumer must keep pre-taxonomy
    # behavior, so adopting the field is additive and can't silently change policy.
    assert infra_death(None) is False


def test_pr_resume_infra_death_keeps_the_blocked_pr_unchanged():
    actions = pr_resume_actions(
        100, AgentResult(is_error=True, exit_reason=ExitReason.TRANSPORT_DEATH)
    )
    assert actions == []
    assert AgentResult(is_error=True).exit_reason is None
    assert AgentResult(is_error=True).usage is None


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
    d = deterministic_death(
        _BUFFER_DEATH, "AssistantMessage → Read(evidence/ac-3-edge-case-titles.png)"
    )
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
