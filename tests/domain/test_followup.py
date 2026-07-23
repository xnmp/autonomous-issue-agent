"""Post-run follow-up decisions — the script tails as pure Action lists."""

from issue_agent.domain.actions import PostComment, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import (
    AgentResult,
    Adjudication,
    AuthorFollowup,
    ImplementFollowup,
    address_review_actions,
    adjudication,
    author_actions,
    author_followup,
    contract_escalation_actions,
    fix_ci_actions,
    implement_followup,
    resolve_actions,
    resume_actions,
)
from issue_agent.domain.review import review_submission


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


# ── author tail (AUTHOR_TESTS_SPLIT) ──
def test_author_authored_stays_working_no_actions():
    # tests_authored → no label move (stay ai-working); the implementer runs next.
    assert author_followup(_ok("tests_authored")) is AuthorFollowup.AUTHORED
    assert author_actions(5, _ok("tests_authored")) == []


def test_author_blocked_asks_and_blocks():
    acts = author_actions(5, _ok("blocked", needs="which schema?"))
    comment = next(a for a in acts if isinstance(a, PostComment))
    assert "which schema?" in comment.body
    assert Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_BLOCKED) in acts


def test_author_error_requeues_ready():
    acts = author_actions(5, AgentResult(is_error=True))
    assert acts == [Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_READY)]


# ── implementer leg + contract renegotiation ──
def test_implement_pr_opened_ground_truth_wins():
    # even a contract_change status is overridden when a PR actually opened.
    assert implement_followup(_ok("contract_change"), pr_opened=True) is ImplementFollowup.PR_OPENED


def test_implement_contract_change_is_its_own_outcome():
    fu = implement_followup(_ok("contract_change", needs="test X asserts the wrong layer"), pr_opened=False)
    assert fu is ImplementFollowup.CONTRACT_CHANGE


def test_implement_blocked_vs_error():
    assert implement_followup(_ok("blocked"), False) is ImplementFollowup.BLOCKED
    assert implement_followup(AgentResult(is_error=True), False) is ImplementFollowup.RETRY


def test_adjudication_revised_only_on_explicit_verdict():
    assert adjudication(AgentResult(is_error=False, structured={"verdict": "revised"})) is Adjudication.REVISED
    assert adjudication(AgentResult(is_error=False, structured={"verdict": "upheld"})) is Adjudication.UPHELD
    # errored / missing verdict → UPHELD (never silently weaken the contract)
    assert adjudication(AgentResult(is_error=True, structured={"verdict": "revised"})) is Adjudication.UPHELD
    assert adjudication(AgentResult(is_error=False, structured=None)) is Adjudication.UPHELD


def test_contract_escalation_goes_to_human():
    acts = contract_escalation_actions(5, "test X is wrong", "round1: upheld; round2: upheld")
    comment = next(a for a in acts if isinstance(a, PostComment))
    assert "test X is wrong" in comment.body and "upheld" in comment.body
    assert Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.NEEDS_HUMAN_REVIEW) in acts


# ── resume tail (differs: error stays blocked, not ready) ──
def test_resume_error_stays_blocked():
    acts = resume_actions(5, AgentResult(is_error=True), pr=None)
    assert acts == [Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_BLOCKED)]


def test_resume_pr_opened_success():
    acts = resume_actions(5, _ok("pr_opened"), pr=100)
    assert Relabel(Target.ISSUE, 5, Label.AI_WORKING, Label.AI_PR_OPEN) in acts


# ── address-review tail (retry-counter decisions) ──
def test_address_review_success_clears_retry():
    out = address_review_actions(100, _ok("pushed"))
    assert out.clear_retry is True and out.bump_retry is False
    assert Relabel(Target.PR, 100, Label.AI_WORKING, None) in out.actions


def test_address_review_blocked_no_retry_burn():
    out = address_review_actions(100, _ok("blocked", summary="unclear"))
    assert out.clear_retry is False and out.bump_retry is False
    assert any(isinstance(a, Relabel) and a.add is Label.AI_BLOCKED for a in out.actions)


def test_address_review_error_bumps_retry():
    out = address_review_actions(100, AgentResult(is_error=True))
    assert out.bump_retry is True and out.clear_retry is False


# ── fix-ci tail ──
def test_fix_ci_pushed_clears_working():
    acts = fix_ci_actions(100, _ok("pushed"))
    assert acts == [Relabel(Target.PR, 100, Label.AI_WORKING, None)]


def test_fix_ci_blocked_adds_blocked():
    acts = fix_ci_actions(100, _ok("blocked"))
    assert Relabel(Target.PR, 100, None, Label.AI_BLOCKED) in acts


# ── review submission tail (args: decision, body, prior_rejections, max_rejections) ──
def test_review_approve():
    s = review_submission("APPROVE", "lgtm", 0, 4)
    assert s.event == "APPROVE" and s.escalate is False


def test_review_request_changes():
    s = review_submission("REQUEST_CHANGES", "fix x", 1, 4)
    assert s.event == "REQUEST_CHANGES" and s.escalate is False


def test_review_escalate_comments_and_flags():
    s = review_submission("ESCALATE", "still broken", 3, 4)
    assert s.event == "COMMENT" and s.escalate is True
    assert "rounds of requested changes" in s.body
