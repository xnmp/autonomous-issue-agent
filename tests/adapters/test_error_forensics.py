"""Diagnostics for a failed agent run.

Issue #501's first resolve ran 290s, spent $1.93, committed nothing, returned
is_error=True — and the only trace was `exit_reason=agent_error`. Everything that
would have explained it (the SDK's errors list, permission_denials, num_turns,
stop_reason, api_error_status, the model's own result text) was mapped away.
Worse, the agent had declared status="success" in its structured output, and the
log line preferred that claim to the envelope, printing:

    agent #501: resolve — done (status=success, $1.93)

Two contracts pinned here: an errored run never renders as a bare success, and
the envelope's diagnostic fields survive into the log.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from issue_agent.adapters.agent_runner import error_forensics, result_to_agent_result
from issue_agent.domain.outcome import (
    AgentResult,
    ExitReason,
)


@dataclass
class _Result:
    """Stand-in for the SDK ResultMessage (duck-typed at the boundary)."""
    is_error: bool = True
    subtype: str = "error_during_execution"
    structured_output: dict | None = None
    result: str = ""
    num_turns: int = 0
    stop_reason: str = ""
    terminal_reason: str = ""
    api_error_status: str = ""
    errors: list = field(default_factory=list)
    permission_denials: list = field(default_factory=list)
    session_id: str = "s1"
    total_cost_usd: float = 0.0
    usage: dict | None = None


# ── the envelope's error kind is no longer shadowed by the agent's claim ─────
def test_envelope_subtype_survives_a_self_declared_status():
    """`if not status` used to gate this, so a claimed status erased the kind."""
    msg = _Result(is_error=True, subtype="error_max_turns",
                  structured_output={"status": "success"})
    res = result_to_agent_result(msg)
    assert res.status == "success"              # the claim is preserved verbatim
    assert res.envelope_subtype == "error_max_turns"   # and so is the truth
    assert res.is_error is True


def test_envelope_subtype_is_captured_on_success_too():
    msg = _Result(is_error=False, subtype="success",
                  structured_output={"status": "pr_opened"})
    assert result_to_agent_result(msg).envelope_subtype == "success"


# ── forensics ────────────────────────────────────────────────────────────────
def test_forensics_surface_permission_denials():
    """A permission-wedged agent is invisible in every other surface."""
    msg = _Result(permission_denials=[{"tool": "Bash", "cmd": "git push --no-verify"}],
                  num_turns=40, subtype="error_max_turns")
    line = error_forensics(msg)
    assert "permission_denials=1" in line
    assert "no-verify" in line
    assert "num_turns=40" in line
    assert "error_max_turns" in line


def test_forensics_surface_the_errors_list_and_result_text():
    msg = _Result(errors=["tool 'Edit' failed: file not found"],
                  result="I was unable to locate the keybinding module.")
    line = error_forensics(msg)
    assert "file not found" in line
    assert "unable to locate" in line


def test_forensics_are_one_line_and_bounded():
    """A diagnostic that floods the log gets turned off."""
    msg = _Result(result="x" * 5000, errors=["y" * 5000])
    line = error_forensics(msg)
    assert "\n" not in line
    assert len(line) < 1500


def test_forensics_never_raise_on_a_hostile_envelope():
    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    assert isinstance(error_forensics(Hostile()), str)
    assert error_forensics(object()) != ""


def test_forensics_say_so_when_the_envelope_is_bare():
    """Silence would read as "nothing went wrong"; say the envelope was empty."""
    assert "no diagnostic fields" in error_forensics(_Result(subtype=""))
