"""Map derived item states to Actions. Pure.

`needs-human` has one writer: this module. A stop adds the label first and
then one marked comment keyed for dedupe. That order matters: a crash between
the two leaves the item labelled with no stop comment, which derive re-stops
(both writes again; the label is idempotent and the comment keyed), rather than
a stop comment on an unlabelled item, which derive would read as released.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from types import MappingProxyType

from .actions import (
    ISSUE_ACTS, Action, FileIssue, LinkBlockedBy, MergePullRequest, OpenPullRequest, PostComment, Relabel,
    ReportGap, RerunFailedJobs, RunAgent, SetIssueState, Target, UpdateBranch,
)
from .backoff import STALL_STREAK, LegEnd, machine_streak
from .architecture import PassEnded, PassInFlight, architecture_owed
from .checks import BaseHealth, BaseIssue, BaseRun
from .ci import RED_CONCLUSIONS, base_fix, base_issues, base_title, base_verdicts, parse_timestamp
from .gaps import GapTarget
from .outcome import OUTSIDE_ENDS
from .labels import Label
from .world import WorldState
from .state import (
    AttemptsStop, Backoff, Blocked, Busy, DerivedState, Dispatch, DuplicateStop, Harness, ItemKey, LinkBlocker, Merge,
    NeedsHuman, OpenPR, QuestionStop, Refresh, Rerun, Release, StopReason, TriageNote, Wait, stop_tag, ATTEMPT_CAP,
    BLOCKER_TAG, HARNESS_GAP_PREFIX, REVIEW_ROUND_CAP, TRIAGE_TAG,
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
    # A reply is not routed to the agent here: it approves the change (tauri
    # PR #1155, 2026-10-15, where an operator's "revert it" reply released it).
    StopReason.WORKFLOW_PATH: ("Replying here or removing the `needs-human` label approves the `.github/` change and "
                               "sends the PR on to review. To have it changed instead, say so in that reply: the "
                               "reviewer reads the thread and can request the change."),
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


def dispatch_order(keys: Iterable[ItemKey], waiting_since: Mapping[ItemKey, datetime],
                   first: frozenset[ItemKey] = frozenset()) -> list[ItemKey]:
    """Who gets a scarce slot first: base-fix work (`first`, #402), then PR-stage items before issue-stage (finish
    work in progress before starting new), then the item that has waited
    longest (its newest leg end; none = never served = oldest), then by number
    so the order is total. Ordering only: every key keeps its place in the list.
    #1125 starved behind #1039/#1117/#1122 under issues-first, by-number order.
    A file-overlap preference (#394) was removed (#450): conflicts are the
    rebase act's job. A paid scheduler leg would replace this function (#388)."""
    never = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(keys, key=lambda k: (k not in first, k.target is not Target.PR, waiting_since.get(k, never), k.number))


def needs_human_key(key: ItemKey, stop: NeedsHuman) -> str:
    act = f"-{stop.act.name.lower()}" if isinstance(stop, AttemptsStop) else ""
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


_ARCHITECTURE_TASK = """Review architectural pressure in the target repository.

- Read `docs/architecture-pressure.md` and its line `Covered through: <full commit sha on base>`.
  Inspect all changes from that SHA (exclusive) to the current base tip (inclusive);
  when the line or ledger is absent, inspect from the beginning of the base history.
  Read the related PRs' review threads and residual sections. Record the full base commit SHA
  actually inspected in `Covered through`; never advance it to uninspected changes or use
  the previous pass's completion time as the coverage boundary.
- Update the ledger with these fields for each pressured seam: seam; evidence links
  (commits, PRs, review threads, residuals); trend; candidate intervention (the smallest
  abstraction that removes several special cases); reconsideration trigger (an explicit
  intervention threshold); expiry. Ground each entry and intervention in that evidence.
  Merge overlapping entries, drop expired entries and keep the ledger small. "defer" is
  a valid verdict; do not invent refactors to fill a quota.
- For entries past their intervention threshold, use `gh` to file at most **3** issues
  labelled `tech-debt` across all legs of this pass. Check for existing filings first and
  reuse them. Link this pass issue from each filing and link each from the ledger.
  Never mark these issues `ai-ready`; a human chooses which debt to work. This is the
  only exception to the fixer's issue-filing prohibition.
- Commit and push your normal fix branch with the ledger update, even when the verdict
  is defer. Follow the normal publication workflow and end the summary with `Closes #<issue>`;
  the harness opens the PR and runs independent review and merge.
"""


def architecture_actions(world: WorldState, harness: Harness) -> tuple[Action, ...]:
    """File and ready an ordinary pass issue through the base-CI path (ADR 0017)."""
    observed = world.architecture
    if isinstance(observed, PassInFlight):
        issue = observed.issue
        return ((Relabel(Target.ISSUE, issue.number, None, Label.AI_READY),)
                if not issue.labels & {Label.AI_READY, Label.NEEDS_HUMAN} else ())
    if not architecture_owed(observed):
        return ()
    title = f"Architecture pass (after #{observed.number})" if isinstance(observed, PassEnded) else "Architecture pass (first)"
    return (FileIssue(title, f"{harness.marker}\n{_ARCHITECTURE_TASK}", f"architecture:{title}"),)


def base_ci_actions(health: BaseHealth | None, marker: str,
                    labels: Mapping[int, frozenset[Label]] = MappingProxyType({}),
                    merged: Mapping[int, str] = MappingProxyType({})) -> tuple[Action, ...]:
    """One branch-wide red streak issue (ADR 0017, #486).

    A merged repair that still fails hands off to one successor, observed
    before its predecessor closes. All workflows share the same close anchor
    and off-switch; no per-workflow issue lifecycle remains.
    """
    if health is None or health.runs is None:
        return ()
    base, issues = health.ref, base_issues(health)
    verdicts = base_verdicts(health.runs)
    red = [(latest, since, bounded) for latest, since, bounded in verdicts
           if latest.conclusion in RED_CONCLUSIONS]
    opened = [i for i in issues if not i.closed_at]
    if not red:
        # A newer queued/run/re-run can still fail; an empty listing is not green.
        green = [latest for latest, _, _ in verdicts]
        if (not green or any(not r.conclusion and (not any(g.workflow == r.workflow for g in green)
                           or _created(r) > max(_created(g) for g in green if g.workflow == r.workflow))
                            for r in health.runs)):
            return ()
        scoped = _base_scope(issues, opened, marker)
        named = {name for i in scoped for name in re.findall(
            r"^- \*\*(.+)\*\*: (?:First|Earliest listed) failing run:", i.body, re.M)}
        named.update(_inert(' '.join(found[1].split())[:200]) for i in scoped
                     if (found := re.fullmatch(rf"CI: (.+) failing on {re.escape(base)}(?: since \S+)?", i.title)))
        # Keep the filing's evidence when its workflow ages out of the run
        # listing. An unrelated nightly workflow is not part of that scope.
        missing = named - {_inert(' '.join(g.workflow.split())[:200]) for g in green}
        if missing and (health.workflows is None or missing & {
                _inert(' '.join(w.split())[:200]) for w in health.workflows}):
            return ()
        latest = min(green, key=_created)
        return tuple(SetIssueState(i.number, False, f"{marker}\nBase CI passes on `{base}` again: {latest.url} "
                                   f"(head `{latest.head_sha}`, created `{latest.created_at}`).") for i in opened)
    failures = sorted((r for latest, since, _ in red for r in health.runs
                       if r.workflow == latest.workflow and r.conclusion in RED_CONCLUSIONS
                       and _created(r) >= _created(since)), key=_created)
    ended = {i.number: next((r for r in failures if _created(r)[0] >= at), None) for i in opened
             if (at := parse_timestamp(merged.get(i.number, "")))
             and Label.NEEDS_HUMAN not in labels.get(i.number, frozenset())}
    ended = {n: r for n, r in ended.items() if r is not None}
    out: list[Action] = []
    if any(i.number not in ended for i in opened):
        keeper = next(i for i in opened if i.number not in ended)
        out.extend(SetIssueState(i.number, False, f"{marker}\nBase CI is tracked by #{keeper.number}.")
                   for i in opened if i != keeper and Label.NEEDS_HUMAN not in labels.get(i.number, frozenset()))
    else:
        previous = max((i for i in issues if i.closed_at or i.number in ended),
                       key=lambda i: (parse_timestamp(i.closed_at or merged[i.number])
                                      or datetime.min.replace(tzinfo=timezone.utc), i.number), default=None)
        after = (_anchor(previous, parse_timestamp(previous.closed_at) or datetime.min.replace(tzinfo=timezone.utc),
                         marker, base) if previous and previous.closed_at else None)
        first = min(ended.values(), key=_created) if ended else next((r for r in failures
            if after is None or _created(r)[0] >= after or previous and previous.closed_by == r.head_sha), None)
        if first is not None:
            title = base_title(base, previous.number if previous else None)
            details = "\n".join(f"- **{_inert(' '.join(latest.workflow.split())[:200])}**: "
                                 f"{'First' if bounded else 'Earliest listed'} failing run: {since.url} "
                                 f"(head `{since.head_sha}`)." for latest, since, bounded in red)
            out.append(FileIssue(title, f"{marker}\nBase CI is failing on `{base}`.\n\n{details}\n\n"
                "Fix the red base across all workflows. Read the current base runs as well: another workflow "
                "may finish after this snapshot. The harness closes this issue only when base CI passes; "
                "a later red streak gets a new issue.", f"base-ci:{base}:{title}"))
    admitted = {n for n, held in labels.items() if held & {Label.AI_READY, Label.NEEDS_HUMAN}}
    closing = {a.number for a in out if isinstance(a, SetIssueState)}
    out.extend(Relabel(Target.ISSUE, n, remove=None, add=Label.AI_READY)
               for n in sorted(base_fix(health) - admitted - closing))
    return tuple(out)


def _base_scope(issues: tuple[BaseIssue, ...], opened: list[BaseIssue], marker: str) -> tuple[BaseIssue, ...]:
    # Consolidation changes the owner, not the workflows owed green evidence.
    # Follow existing close comments, including a predecessor handed to a successor.
    scoped = {i.number for i in opened}
    while True:
        notes = {f"{marker}\nBase CI is tracked by #{n}." for n in scoped}
        expanded = scoped | {i.number for i in issues if i.note in notes}
        if expanded == scoped:
            return tuple(i for i in issues if i.number in scoped)
        scoped = expanded


def _anchor(issue: BaseIssue, closed_at: datetime, marker: str, base: str) -> datetime:
    """What a red run must be created at or after to file once `issue`
    closed. The harness's green close, known by the comment it closed with:
    its green run's creation, since a run created between the tick's read and
    the close landing must still file. The harness's other closes need no
    care: a merged fix's issue closes only once the next is open. Any other
    close anchors on the close itself (#442)."""
    green = None if issue.closed_by else re.fullmatch(
        rf"{re.escape(marker)}\n.* passes on `{re.escape(base)}` again: \S+ \(head `\S*`, created `([^`]+)`\)\.",
        issue.note, re.S)
    return (parse_timestamp(green[1]) if green else None) or closed_at


def _created(run: BaseRun) -> tuple[datetime, int]:
    return (parse_timestamp(run.created_at) or datetime.min.replace(tzinfo=timezone.utc), run.run_id)


_GAP_FOOTER = ("Filed by the harness from its own records: no agent-written text is published here. No automatic "
               "pickup takes this issue up; a human judges it and may mark it `ai-ready`.")


def gap_actions(
    legs: Mapping[ItemKey, tuple[LegEnd, ...]], live: Iterable[ItemKey], target: GapTarget, harness_repo: str,
    marker: str, reported: frozenset[str] = frozenset(),
) -> tuple[Action, ...]:
    """The harness gaps to report into `harness_repo` ("" = off; `domain.gaps`):
    each agent end on an item `legs` holds that reported a harness defect,
    and each act of a `live` item whose machine ends (teardowns and provider
    windows aside) reached STALL_STREAK in a row. One issue per gap kind and
    target (its generated title), so several items share it; one report per
    item per gap, ever: a key in `reported` (act's confirmed keys) is not
    asked for again. Nothing here looks at a judgment stop. Harness facts
    only: the item, the leg's id, kind and runtime, an end class from a fixed
    vocabulary; a non-public target unnamed and unlinked."""
    if not harness_repo:
        return ()
    out: dict[str, ReportGap] = {}

    def report(item: ItemKey, slug: str, kind: str, leg: LegEnd, what: str, details: str) -> None:
        key = f"gap:{harness_repo}:{target.repo}:{item.target.name.lower()}:{item.number}:{slug}"
        if key in out or key in reported:
            return
        link = target.link(item.number)
        facts = (f"`{target.label}#{item.number}` ({'issue' if item.target is Target.ISSUE else 'PR'})"
                 f"{f', {link}' if link else ''}: the {leg.kind.name} leg `{leg.leg_id or 'unrecorded'}` "
                 f"({leg.runtime or 'runtime unrecorded'}), ended {leg.ended_at.isoformat()}: {what}.")
        title = f"{HARNESS_GAP_PREFIX} {kind} in {target.label}"
        out[key] = ReportGap(harness_repo, title, key,
                             f"{marker}\n**Harness gap**: {kind}.\n\n- {facts}\n\n{details}\n\n{_GAP_FOOTER}",
                             f"{marker}\n**Seen again**: {facts}\n\n{details}")

    for item in sorted(legs, key=lambda k: k.sort_key):
        for leg in legs[item]:
            if leg.harness_gap:
                ended = "it asked a human too" if leg.question is not None else "returned"
                report(item, "agent-report", "agent-reported harness defect", leg, ended,
                       f"The agent's report is in the leg receipt `{leg.leg_id or 'unrecorded'}` on the harness "
                       "host (the control store's `harness_issue`) and in that deployment's journal.")
    for item in sorted(live, key=lambda k: k.sort_key):
        ends = legs.get(item, ())
        for act in sorted({end.kind for end in ends}, key=lambda k: k.name):
            run = [end for end in machine_streak(ends, act) if end.end not in OUTSIDE_ENDS]  # newest first
            if len(run) >= STALL_STREAK:
                # The leg that reached the streak, and the streak's start: stable while it grows.
                report(item, f"machine-{act.name.lower()}", f"{act.name}: {STALL_STREAK} consecutive machine ends",
                       run[-STALL_STREAK], f"machine end `{run[-STALL_STREAK].end or 'no receipt'}`",
                       f"Its {act.name} legs have ended without an agent result since {run[-1].ended_at.isoformat()}, "
                       "so their retry backoff is at its one-hour cap. Each leg's end class and receipt are on "
                       "the harness host (control store and journal).")
    return tuple(out.values())


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
        why = _WHY[state.reason].format(act=state.act.name if isinstance(state, AttemptsStop) else "",
                                        dup=state.duplicate_of if isinstance(state, DuplicateStop) else 0)
        asked = (f"\n\n{question_text(state.question)}" if isinstance(state, QuestionStop)
                 else f" {_code_span(state.detail)}" if isinstance(state, DuplicateStop) and state.detail else "")
        body = (f"{harness.marker}\n{stop_tag(state.reason)}\n"
                f"**needs-human**: {why}{asked}\n\n{_HOW.get(state.reason, _DEFAULT_HOW)}")
        return (
            Relabel(key.target, key.number, remove=None, add=Label.NEEDS_HUMAN),
            PostComment(key.target, key.number, body, key=needs_human_key(key, state)),
        )
    if isinstance(state, (Wait, Blocked, Busy, Backoff)):
        return ()
    raise TypeError(f"unknown derived state {state!r}")


__all__ = ["QUESTION_LIMIT", "actions", "architecture_actions", "base_ci_actions",
           "blocker_closed_comment", "blocker_comment", "dispatch_order", "gap_actions", "needs_human_key",
           "question_text", "triage_comment"]
