"""Post-run follow-up decisions — the script tails as pure Action lists."""

from issue_agent.domain.actions import PostComment, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import (
    AgentResult,
    ExitReason,
    address_review_actions,
    fix_ci_actions,
    resolve_actions,
    resume_actions,
)


def _ok(status="pr_opened", **kw):
    return AgentResult(is_error=False, status=status, **kw)


# ── resolve tail ──
def test_resolve_pr_opened_labels_issue_and_pr():
    acts = resolve_actions(5, _ok("pr_opened"), pr=100)
    assert Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_PR_OPEN) in acts
    assert Relabel(Target.PR, 100, None, Label.AI_PR_OPEN) in acts


def test_resolve_ground_truth_beats_blocked_prose():
    # model said blocked but a PR exists → treated as success.
    acts = resolve_actions(5, AgentResult(is_error=False, status="blocked", needs="x"), pr=100)
    assert any(isinstance(a, Relabel) and a.add is Label.AI_PR_OPEN for a in acts)


def test_resolve_blocked_comments_and_blocks():
    acts = resolve_actions(5, _ok("blocked", needs="which endpoint?"), pr=None)
    comment = next(a for a in acts if isinstance(a, PostComment))
    assert "which endpoint?" in comment.body
    assert Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_BLOCKED) in acts


def test_resolve_error_requeues_ready():
    acts = resolve_actions(5, AgentResult(is_error=True), pr=None)
    assert acts == [Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_READY)]


# ── resume tail (differs: error stays blocked, not ready) ──
def test_resume_error_stays_blocked():
    acts = resume_actions(5, AgentResult(is_error=True), pr=None)
    assert acts == [Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_BLOCKED)]


def test_resume_pr_opened_success():
    acts = resume_actions(5, _ok("pr_opened"), pr=100)
    assert Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_PR_OPEN) in acts


# ── address-review tail (retry-counter decisions) ──
def test_address_review_success_still_counts_attempt():
    # V2 lesson: a claimed push is NOT trusted as progress — only a superseding
    # review resets the counter (it re-keys to the new review's timestamp), so a
    # successful attempt bumps exactly like an error. Without this, clear-on-push
    # reset the breaker every round and it could never trip.
    out = address_review_actions(100, _ok("pushed"))
    assert out.bump_retry is True
    # review M22/D9: a successful leg writes NO label. The clear-ai-working
    # relabel that used to lead this list removed a label nothing ever added —
    # one wasted `gh pr edit` per leg, and a busy marker future readers would
    # have trusted. The counter, not a label, is what bounds this loop.
    assert out.actions == []


def test_address_review_blocked_no_retry_burn():
    out = address_review_actions(100, _ok("blocked", summary="unclear"))
    assert out.bump_retry is False
    assert any(isinstance(a, Relabel) and a.add is Label.AI_BLOCKED for a in out.actions)
    # Anti-vacuity twin for the M22 deletion above: blocked is the ONE address-review
    # outcome that still writes a label, so an empty list on the other paths means
    # "nothing to write", not "this tail stopped emitting actions".
    assert out.actions != []


def test_address_review_error_bumps_retry():
    out = address_review_actions(100, AgentResult(is_error=True))
    assert out.bump_retry is True


# ── budget exhaustion on PR-repair legs (#155: the #98 class on the PR path) ──
# A BUDGET_EXHAUSTED repair leg is frequently a DETERMINISTIC admission
# rejection (daily cap + full reservation no longer fit), so counting it walks
# a healthy PR to needs-human-review at $0 per strike — keifu PRs #171/#174
# did exactly that on 2026-08-02. The budget surface, not the breaker, owns it.
def _capped(**kw):
    return AgentResult(
        is_error=True, exit_reason=ExitReason.BUDGET_EXHAUSTED, **kw
    )


def test_address_review_budget_exhaustion_is_no_strike_and_blocks_for_cap_review():
    out = address_review_actions(100, _capped())
    assert out.bump_retry is False
    assert Relabel(Target.PR, 100, None, Label.AI_BLOCKED) in out.actions
    assert Relabel(Target.PR, 100, None, Label.NEEDS_BUDGET_REVIEW) in out.actions
    prompt = next(a.body for a in out.actions if isinstance(a, PostComment))
    assert "Budget review needed" in prompt


def test_fix_ci_budget_exhaustion_blocks_for_cap_review():
    acts = fix_ci_actions(100, _capped())
    assert Relabel(Target.PR, 100, None, Label.AI_BLOCKED) in acts
    assert Relabel(Target.PR, 100, None, Label.NEEDS_BUDGET_REVIEW) in acts


# ── fix-ci tail ──
def test_fix_ci_pushed_writes_no_label():
    # review M22/D9: a pushed fix-CI leg leaves the PR's labels alone — CI re-runs
    # and the watcher re-evaluates next poll. (Was a clear-ai-working relabel for a
    # label nothing ever added.)
    assert fix_ci_actions(100, _ok("pushed")) == []


def test_fix_ci_blocked_adds_blocked():
    acts = fix_ci_actions(100, _ok("blocked"))
    assert Relabel(Target.PR, 100, None, Label.AI_BLOCKED) in acts
    prompt = next(a.body for a in acts if isinstance(a, PostComment))
    assert "_Reply to unblock." in prompt
    # Anti-vacuity twin for the deletion above: blocked still writes, so [] on the
    # other paths is a decision, not a dead function.
    assert acts != []


# ── review submission tail (args: decision, body, prior_rejections, max_rejections) ──
# ── infra death: the session never happened, so no leg may act on it ──────────
def _killed(**kw):
    return AgentResult(is_error=True, exit_reason=ExitReason.KILLED_EXTERNALLY, **kw)


def test_resolve_infra_death_leaves_issue_working_for_the_reaper():
    # #65: the old path relabelled ai-working→ai-ready on a forcefully killed
    # session, racing an operator's manual ai-blocked. Emitting NOTHING keeps the
    # issue ai-working; the reaper requeues it once on the next idle tick.
    assert resolve_actions(5, _killed(), pr=None) == []


def test_resolve_infra_death_still_labels_a_pr_that_actually_opened():
    # Ground truth outranks cause-of-death: how the session ended says nothing
    # about whether a PR exists, and a real PR must never be stranded unlabelled.
    acts = resolve_actions(5, _killed(), pr=100)
    assert Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_PR_OPEN) in acts
    assert Relabel(Target.PR, 100, None, Label.AI_PR_OPEN) in acts


def test_resume_infra_death_does_not_consume_the_human_reply():
    # Contrast test_resume_error_stays_blocked: an ordinary error moves to
    # ai-blocked, a killed session moves nothing at all.
    assert resume_actions(5, _killed(), pr=None) == []


def test_transport_death_is_also_an_infra_death_on_the_issue_legs():
    r = AgentResult(is_error=True, exit_reason=ExitReason.TRANSPORT_DEATH)
    assert resolve_actions(5, r, pr=None) == []
    assert resume_actions(5, r, pr=None) == []


def test_address_review_infra_death_burns_no_retry_and_writes_nothing():
    # #58: six SIGKILLs on one PR walked the breaker to needs-human-review on work
    # that never ran. The counter must not move, and (M22/D9) no label is written:
    # an infra death must leave the PR exactly as it found it.
    out = address_review_actions(100, _killed())
    assert out.bump_retry is False
    assert out.actions == []


def test_address_review_timeout_still_burns_a_retry():
    # The boundary of the exemption: a hang IS an attempt (see infra_death).
    out = address_review_actions(
        100, AgentResult(is_error=True, status="timeout", exit_reason=ExitReason.IDLE_TIMEOUT)
    )
    assert out.bump_retry is True


def test_fix_ci_infra_death_writes_nothing_not_even_blocked():
    # Never add ai-blocked for a session that was killed rather than stuck —
    # ai-blocked would stop re-dispatch and strand the PR on a human. Contrast
    # test_fix_ci_blocked_adds_blocked: the SAME 'blocked' status DOES relabel
    # when the session actually reached that decision itself.
    assert fix_ci_actions(100, _killed(status="blocked")) == []
    assert fix_ci_actions(100, _ok("blocked")) != []
