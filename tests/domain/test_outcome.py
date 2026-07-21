"""Agent-result interpretation — envelope over prose, ground truth over status."""

from issue_agent.domain.outcome import (
    AgentResult,
    Outcome,
    ResolveFollowup,
    classify_outcome,
    resolve_followup,
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
