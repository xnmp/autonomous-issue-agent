"""Map derived item states to Actions. Pure.

`needs-human` has one writer: this module. A stop adds the label first and
then one marked comment keyed for dedupe. That order matters: a crash between
the two leaves the item labelled with no stop comment, which derive re-stops
(both writes again; the label is idempotent and the comment keyed), rather than
a stop comment on an unlabelled item, which derive would read as released.
"""

from __future__ import annotations

from collections.abc import Mapping

from .actions import (
    ISSUE_ACTS, Action, MergePullRequest, OpenPullRequest, PostComment, Relabel, RunAgent, Target, UpdateBranch,
)
from .labels import Label
from .state import (
    Backoff, Busy, DerivedState, Dispatch, Harness, ItemKey, Merge, NeedsHuman, OpenPR, Refresh, Release,
    StopReason, TriageNote, Wait, stop_tag, ATTEMPT_CAP, REVIEW_ROUND_CAP, TRIAGE_TAG,
)

_WHY = {
    StopReason.WORKFLOW_PATH: "this PR changes files under `.github/`, which a human must judge.",
    StopReason.LIFETIME_CAP: "this issue has spent its lifetime budget; raising the cap is a grant.",
    StopReason.ATTEMPTS: f"{{act}} ran {ATTEMPT_CAP} times without progress since the last human activity.",
    StopReason.REVIEW_ROUNDS: f"the reviewer requested changes {REVIEW_ROUND_CAP} times; the dispute needs a human.",
    StopReason.QUESTION: "the agent asked a question that needs a human's judgment:",
    StopReason.DUPLICATE: "triage suspects this issue duplicates #{dup}:",
}
_HOW = {
    StopReason.LIFETIME_CAP: "Raise the per-issue budget, then reply here or remove the `needs-human` label.",
    StopReason.QUESTION: "Reply here with the answer (the agent resumes with it), or remove the `needs-human` label.",
    StopReason.DUPLICATE: ("If it is a duplicate, close this issue. If not, reply here or remove the "
                           "`needs-human` label and the harness resolves it."),
}
_DEFAULT_HOW = "Reply here or remove the `needs-human` label to release it."
QUESTION_LIMIT = 4000


def question_text(needs: object) -> str:
    """The agent's question as text a stop comment may embed: bounded, and with
    hidden-comment openers neutralised so it can never carry a stop tag or the
    harness marker. Idempotent, so the writer and the embedder can both apply it."""
    text = needs if isinstance(needs, str) else ""
    return text.replace("<!--", "<\u200b!--").strip()[:QUESTION_LIMIT].strip() or "(no question text)"


def actions(states: Mapping[ItemKey, DerivedState], harness: Harness) -> tuple[Action, ...]:
    """The actions for every item, ordered issues first then PRs, by number."""
    out: list[Action] = []
    for key in sorted(states, key=lambda k: k.sort_key):
        out.extend(_for(key, states[key], harness))
    return tuple(out)


def needs_human_key(key: ItemKey, stop: NeedsHuman) -> str:
    act = f"-{stop.act.name.lower()}" if stop.act is not None else ""
    return f"needs-human:{key.target.name.lower()}:{key.number}:{stop.reason.value}{act}:{stop.anchor}"


def triage_comment(state: TriageNote, harness: Harness) -> str:
    """The one triage comment: the tier, triage's rationale and the acceptance
    criteria (a heading `pr_body.issue_acceptance_criteria` finds, so they
    reach the PR body), never an implementation plan. An issue that states
    its own criteria was asked for none (`triage.criteria_wanted`). Agent text
    was neutralised and bounded by `triage.parse_triage`; here it is also made
    inert to mentions and images under the harness's name."""
    triage = state.triage
    why = f" {_inert(triage.rationale)}" if triage.rationale else ""
    head = f"{harness.marker}\n{TRIAGE_TAG}\n**Triage**: difficulty tier `{triage.tier.value}`.{why}"
    if not triage.acceptance_criteria:
        return head
    return head + "\n\n### Acceptance criteria\n" + "\n".join(f"- [ ] {_inert(c)}" for c in triage.acceptance_criteria)


def _inert(text: str) -> str:
    # A zero-width space breaks an @mention and image syntax without changing
    # what a reader sees.
    return text.replace("@", "@\u200b").replace("![", "!\u200b[")


def _code_span(text: str) -> str:
    # Agent text inside a code span renders no mention, link or image.
    return f"`{text.replace('`', '')}`" if text else ""


def _for(key: ItemKey, state: DerivedState, harness: Harness) -> tuple[Action, ...]:
    if isinstance(state, Dispatch):
        if (state.act in ISSUE_ACTS) != (key.target is Target.ISSUE):
            raise ValueError("issue acts dispatch exactly on issues")
        return (RunAgent(state.act, key.number, head=state.head, tier=state.tier),)
    if isinstance(state, TriageNote):
        if key.target is not Target.ISSUE:
            raise ValueError("a triage note is posted exactly on an issue")
        return (PostComment(key.target, key.number, triage_comment(state, harness),
                            key=f"triage:issue:{key.number}:{state.anchor}"),)
    if isinstance(state, Merge):
        return (MergePullRequest(key.number, state.head),)
    if isinstance(state, Refresh):
        return (UpdateBranch(key.number, state.head),)
    if isinstance(state, OpenPR):
        if key.target is not Target.ISSUE:
            raise ValueError("a PR is opened exactly for an issue")
        return (OpenPullRequest(key.number, state.branch),)
    if isinstance(state, Release):
        return (Relabel(key.target, key.number, remove=Label.NEEDS_HUMAN, add=None),)
    if isinstance(state, NeedsHuman):
        why = _WHY[state.reason].format(act=state.act.name if state.act else "", dup=state.duplicate_of)
        asked = (f"\n\n{question_text(state.question)}" if state.reason is StopReason.QUESTION
                 else f" {_code_span(state.detail)}" if state.reason is StopReason.DUPLICATE and state.detail else "")
        body = (f"{harness.marker}\n{stop_tag(state.reason)}\n"
                f"**needs-human**: {why}{asked}\n\n{_HOW.get(state.reason, _DEFAULT_HOW)}")
        return (
            Relabel(key.target, key.number, remove=None, add=Label.NEEDS_HUMAN),
            PostComment(key.target, key.number, body, key=needs_human_key(key, state)),
        )
    if isinstance(state, (Wait, Busy, Backoff)):
        return ()
    raise TypeError(f"unknown derived state {state!r}")


__all__ = ["QUESTION_LIMIT", "actions", "needs_human_key", "question_text", "triage_comment"]
