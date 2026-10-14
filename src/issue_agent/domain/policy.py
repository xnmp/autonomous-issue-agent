"""Map derived item states to Actions. Pure.

`needs-human` has one writer: this module. A stop adds the label first and
then one marked comment keyed for dedupe. That order matters: a crash between
the two leaves the item labelled with no stop comment, which derive re-stops
(both writes again; the label is idempotent and the comment keyed), rather than
a stop comment on an unlabelled item, which derive would read as released.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone

from .actions import (
    ISSUE_ACTS, Action, FileIssue, LinkBlockedBy, MergePullRequest, OpenPullRequest, PostComment, Relabel,
    RerunFailedJobs, RunAgent, SetIssueState, Target, UpdateBranch,
)
from .checks import BaseCI
from .ci import GREEN_CONCLUSIONS, base_verdicts, parse_timestamp
from .labels import Label
from .triage import normalise_guess, normalise_path
from .state import (
    Backoff, Busy, DerivedState, Dispatch, Harness, ItemKey, LinkBlocker, Merge, NeedsHuman, OpenPR, Refresh, Rerun,
    Release, StopReason, TriageNote, Wait, stop_tag, ATTEMPT_CAP, BLOCKER_TAG, REVIEW_ROUND_CAP, TRIAGE_TAG,
)

_WHY = {
    StopReason.WORKFLOW_PATH: "this PR changes files under `.github/`, which a human must judge.",
    StopReason.LIFETIME_CAP: "this issue has spent its lifetime budget; raising the cap is a grant.",
    StopReason.ATTEMPTS: (f"{{act}} made no progress since the last human activity ({ATTEMPT_CAP} attempts in a row, "
                          "or a CI repair left every failure on its head unrepaired)."),
    StopReason.REVIEW_ROUNDS: f"the reviewer requested changes {REVIEW_ROUND_CAP} times; the dispute needs a human.",
    StopReason.QUESTION: "the agent asked a question that needs a human's judgment:",
    StopReason.DUPLICATE: "steward suspects this issue duplicates #{dup}:",
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


# The overlap preference's ceiling: it defers an issue only while that issue
# has waited less than this; after that, age alone orders it. So no overlap
# guess, however broad, delays an issue past this, plus the ordinary wait for
# a slot.
OVERLAP_DEFER_CEILING = timedelta(hours=1)


def dispatch_order(
    keys: Iterable[ItemKey], waiting_since: Mapping[ItemKey, datetime],
    touches: Mapping[int, tuple[str, ...]] | None = None, busy: Iterable[str] = (),
    now: datetime | None = None,
) -> list[ItemKey]:
    """Who gets a scarce slot first: PR-stage items before issue-stage (finish
    work in progress before starting new), then an issue whose steward's
    `touches` guess overlaps none of `busy` (the files open harness PRs change)
    before one that does, then the item that has waited longest (its newest leg
    end; none = never served = oldest), then by number so the order is total.
    Ordering only: every key keeps its place in the list, so a slot never idles
    on it, and with every issue overlapping, or no guesses, it is plain FIFO.
    An issue that has waited OVERLAP_DEFER_CEILING (measured against `now`;
    without `now` the preference is off) is never deferred by it again.
    #1125 starved behind #1039/#1117/#1122 under issues-first, by-number order.
    A paid scheduler leg would replace this function (#388)."""
    never = datetime.min.replace(tzinfo=timezone.utc)
    guesses = touches or {}
    occupied = path_index(busy)

    def clash(key: ItemKey) -> bool:
        since = waiting_since.get(key)
        return (key.target is Target.ISSUE and now is not None and since is not None
                and now - since < OVERLAP_DEFER_CEILING and overlaps(guesses.get(key.number, ()), occupied))

    return sorted(keys, key=lambda k: (k.target is not Target.PR, clash(k), waiting_since.get(k, never), k.number))


def path_index(paths: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Paths for `overlaps`: the normalised files, and every directory above one."""
    files = frozenset(p for raw in paths if isinstance(raw, str) and (p := normalise_path(raw)))
    dirs = frozenset(f.rsplit("/", i)[0] for f in files for i in range(1, f.count("/") + 1))
    return files, dirs


def overlaps(touches: Iterable[str], index: tuple[frozenset[str], frozenset[str]]) -> bool:
    """Path-prefix overlap on whole segments: a guess overlaps a busy file it
    names, a directory holding one, or a file inside a busy path. `src/a`
    does not overlap `src/ab`. A guess that normalises to nothing never overlaps."""
    files, dirs = index
    for raw in touches:
        path = normalise_guess(raw) if isinstance(raw, str) else ""
        if not path:
            continue
        if path in files or path in dirs:
            return True
        parts = path.split("/")
        if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
            return True
    return False


def needs_human_key(key: ItemKey, stop: NeedsHuman) -> str:
    act = f"-{stop.act.name.lower()}" if stop.act is not None else ""
    return f"needs-human:{key.target.name.lower()}:{key.number}:{stop.reason.value}{act}:{stop.anchor}"


def triage_comment(state: TriageNote, harness: Harness) -> str:
    """The one triage comment: the tier, triage's rationale and the steward's
    acceptance criteria (for humans; legs read them from the steward's
    receipt), never an implementation plan. An issue that
    states its own criteria was asked for none (`triage.criteria_wanted`).
    Agent text was neutralised and bounded by `triage.parse_triage` /
    `parse_steward`; here it is also made inert to mentions and images under
    the harness's name."""
    triage = state.triage
    criteria = state.steward.acceptance_criteria
    why = f" {_inert(triage.rationale)}" if triage.rationale else ""
    head = f"{harness.marker}\n{TRIAGE_TAG}\n**Triage**: difficulty tier `{triage.tier.value}`.{why}"
    if not criteria:
        return head
    return head + "\n\n### Acceptance criteria\n" + "\n".join(f"- [ ] {_inert(c)}" for c in criteria)


def blocker_comment(marker: str, blocker: int, reason: str, refusal: str = "") -> str:
    """The one comment on a steward's "blocked by" claim: what was linked and
    why, or why the harness refused the link. `reason` is laundered agent text
    (`triage.parse_steward`), made inert here as in the triage comment, and
    quoted on its own line so it cannot run into the harness's sentence.
    `refusal` must not vary between retries of one claim (act's read-back
    matches the exact body)."""
    head = f"{marker}\n{BLOCKER_TAG}\n"
    why = f"> {_inert(reason)}\n\n"
    if refusal:
        return (f"{head}**Steward** proposed that this issue is blocked by #{blocker}:\n\n{why}"
                f"The harness did not link it: {refusal}.")
    return (f"{head}**Steward**: this issue is blocked by #{blocker}:\n\n{why}It starts once #{blocker} closes. "
            "If this is not a real dependency, delete the link on GitHub; the harness never re-adds it.")


def blocker_closed_comment(marker: str, blocker: int) -> str:
    """Posted when the harness's blocker comment is gone (a human deleted it):
    the claim on #`blocker` was handled once, whatever its outcome or the
    link's state now, and is never acted on again."""
    return (f"{marker}\n{BLOCKER_TAG}\n**Steward**'s blocked-by #{blocker} claim was already handled; "
            "the harness will not write that link again.")


def base_title(workflow: str, base: str) -> str:
    """A workflow's one base-CI issue is known by this title: the name is cut
    so the title keeps its searched suffix within GitHub's 256 characters."""
    return f"CI: {' '.join(workflow.split())[:max(1, min(200, 240 - len(base)))]} failing on {base}"


def base_ci_actions(base_ci: BaseCI | None, base: str, marker: str) -> tuple[Action, ...]:
    """One issue per workflow on the base branch, for life, required or not
    (#402 plans to fix forward through it). A red verdict files it (once:
    keyed) or reopens it if the red streak began after it was closed; green
    closes it. Unless the harness closed it, a streak whose start is past the
    listed runs reopens nothing, so a human's close during a red streak holds
    until the workflow passes and fails again."""
    if base_ci is None:
        return ()
    out: list[Action] = []
    for latest, since, bounded in base_verdicts(base_ci.runs):
        title = base_title(latest.workflow, base)
        issue, name = base_ci.issue(title), _inert(" ".join(latest.workflow.split())[:200])
        run = f"{since.url} (head `{since.head_sha}`)"
        if latest.conclusion in GREEN_CONCLUSIONS:
            if issue is not None and not issue.closed_at:
                out.append(SetIssueState(issue.number, False, f"{marker}\n**{name}** passes on `{base}` again: "
                                                              f"{latest.url} (head `{latest.head_sha}`)."))
        elif issue is None:
            out.append(FileIssue(title, f"{marker}\n**{name}** is failing on `{base}`. {'First' if bounded else 'Earliest listed'} failing run: {run}.\n\n"
                                        "The harness closes this issue when it passes, and reopens it when it fails "
                                        "again.", f"base-ci:{base}:{title}"))
        elif (bounded or issue.harness_closed) and (finished := parse_timestamp(since.completed_at)) \
                and (closed := parse_timestamp(issue.closed_at)) and finished > closed:
            out.append(SetIssueState(issue.number, True, f"{marker}\n**{name}** is failing on `{base}` again. "
                                                         f"First failing run: {run}."))
    return tuple(out)


def _inert(text: str) -> str:
    # A zero-width space breaks an @mention and Markdown image syntax, and an
    # escaped `<` any raw HTML (an <img>), without changing what a reader sees.
    return text.replace("<", "&lt;").replace("@", "@\u200b").replace("![", "!\u200b[")


def _code_span(text: str) -> str:
    # Agent text inside a code span renders no mention, link or image.
    return f"`{text.replace('`', '')}`" if text else ""


def _for(key: ItemKey, state: DerivedState, harness: Harness) -> tuple[Action, ...]:
    if isinstance(state, Dispatch):
        if (state.act in ISSUE_ACTS) != (key.target is Target.ISSUE):
            raise ValueError("issue acts dispatch exactly on issues")
        return (RunAgent(state.act, key.number, head=state.head, tier=state.tier, evidence=state.evidence),)
    if isinstance(state, TriageNote):
        if key.target is not Target.ISSUE:
            raise ValueError("a triage note is posted exactly on an issue")
        return (PostComment(key.target, key.number, triage_comment(state, harness),
                            key=f"triage:issue:{key.number}:{state.anchor}"),)
    if isinstance(state, LinkBlocker):
        if key.target is not Target.ISSUE:
            raise ValueError("a blocker is linked exactly on an issue")
        return (LinkBlockedBy(key.number, state.blocker, state.reason,
                              key=f"blocked-by:issue:{key.number}:{state.blocker}:{state.anchor}"),)
    if isinstance(state, Merge):
        return (MergePullRequest(key.number, state.head),)
    if isinstance(state, Refresh):
        return (UpdateBranch(key.number, state.head),)
    if isinstance(state, Rerun):
        return (RerunFailedJobs(key.number, state.head, state.runs),)
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


__all__ = ["OVERLAP_DEFER_CEILING", "QUESTION_LIMIT", "actions", "base_ci_actions", "base_title",
           "blocker_closed_comment", "blocker_comment", "dispatch_order", "needs_human_key", "overlaps", "path_index",
           "question_text", "triage_comment"]
