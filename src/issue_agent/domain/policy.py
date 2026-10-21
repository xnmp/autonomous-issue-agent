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
from .ci import GREEN_CONCLUSIONS, RED_CONCLUSIONS, base_fix, base_unsettled, base_title, base_verdicts, parse_timestamp, workflow_issues
from .gaps import GapTarget
from .outcome import OUTSIDE_ENDS
from .labels import Label
from .refs import harness_branch_issue
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
    """One issue per workflow per red streak on the base branch, required or
    not (#402, ADR 0017). Green closes the workflow's open issues. A red
    verdict with none open files one, named and keyed by the streak's first
    failing run created at or after the close of the workflow's last closed
    issue (any close, legacy titles too), or of the commit that closed it: a
    run that tested pre-fix code never files. Green closes only when settled:
    no newer run of the workflow is still running (#442). An open issue whose harness PR merged (`merged`: issue -> merged at;
    a fix into a non-default base that `Closes #` does not close) has ended
    once a failing run created at or after the merge has completed: that run
    files the next issue, and the ended issue is closed only once another
    issue of the workflow is seen open. The close never precedes the filing,
    so a lost write (an error, a kill) leaves the next tick to derive what is
    left. A run from before the merge counts for neither, and `needs-human`
    keeps the issue open. A required
    workflow's open issue is readied (`ai-ready`) unless it carries
    `ai-ready` or `needs-human` (`labels`, of the open issues the fix role
    reads). An open issue whose workflow is neither listed in the base's runs
    nor among the repository's workflows is closed (#449) unless it carries
    `needs-human`: renamed or removed, it would never pass."""
    if health is None or health.runs is None:
        return ()
    base, unsettled = health.ref, base_unsettled(health.runs)
    out: list[Action] = []
    for latest, since, bounded in base_verdicts(health.runs):
        issues, name = workflow_issues(health, latest.workflow), _inert(" ".join(latest.workflow.split())[:200])
        opened = [i for i in issues if not i.closed_at]
        if latest.conclusion in GREEN_CONCLUSIONS:
            out.extend(SetIssueState(i.number, False, f"{marker}\n**{name}** passes on `{base}` again: {latest.url} "
                                                      f"(head `{latest.head_sha}`, created `{latest.created_at}`).")
                       for i in opened if latest.workflow not in unsettled)
            continue
        streak = sorted((r for r in health.runs if r.workflow == latest.workflow and r.conclusion in RED_CONCLUSIONS
                         and _created(r) >= _created(since)), key=_created)
        closed = {i.title for i in issues if i.closed_at}
        failed = {i.number: next((r for r in streak if _created(r)[0] >= at), None) for i in opened
                  if (at := parse_timestamp(merged.get(i.number, ""))) and Label.NEEDS_HUMAN not in labels.get(
                      i.number, frozenset())}
        ended = {n: r for n, r in failed.items() if r is not None}  # its fix merged, and a later run still fails
        if len(ended) < len(opened):  # another issue stays open for the red base: the ended ones may close
            out.extend(SetIssueState(n, False, f"{marker}\nIts fix merged, but **{name}** still fails on `{base}` "
                                               f"after it: {r.url}. That failure has an issue of its own.")
                       for n, r in sorted(ended.items()))
            continue
        # The commit whose merge or push closed the last issue tested the fix, whenever GitHub dates the close.
        after, closer = max(((_anchor(i, at, marker, base), i.closed_by) for i in issues
                             if (at := parse_timestamp(i.closed_at))), default=(None, ""))
        first = min(ended.values(), key=_created) if ended else next((
            r for r in streak if base_title(latest.workflow, base, r.head_sha) not in closed
            and (after is None or _created(r)[0] >= after or closer and r.head_sha == closer)), None)
        if first is None:
            continue  # every listed failure predates what ended the last issue: the next failing run files
        title = base_title(latest.workflow, base, first.head_sha)
        lead = "Earliest listed" if first == since and not bounded else "First"
        out.append(FileIssue(title, f"{marker}\n**{name}** is failing on `{base}`. {lead} failing run: {first.url} "
                                    f"(head `{first.head_sha}`).\n\nThe harness closes this issue when it passes; a "
                                    "later failure gets an issue of its own.", f"base-ci:{base}:{title}"))
    if health.workflows is not None:  # the 100 listed runs span hours: a path-filtered workflow may be absent
        live = {i.number for w in health.workflows | {r.workflow for r in health.runs}
                for i in workflow_issues(health, w)}
        shape = re.compile(rf"CI: .+ failing on {re.escape(base)}(?: since \S+)?")
        out.extend(SetIssueState(i.number, False, f"{marker}\nNo workflow of this repository runs under this issue's "
                                                  f"name on `{base}` any more (renamed, removed or disabled), so it "
                                                  "would never pass here.")
                   for i in health.issues if not i.closed_at and i.number not in live and shape.fullmatch(i.title)
                   and Label.NEEDS_HUMAN not in labels.get(i.number, frozenset()))
    admitted = {n for n, held in labels.items() if Label.AI_READY in held or Label.NEEDS_HUMAN in held}
    out.extend(Relabel(Target.ISSUE, n, remove=None, add=Label.AI_READY)
               for n in sorted(base_fix(health) - admitted))
    return tuple(out)


def _anchor(issue: BaseIssue, closed_at: datetime, marker: str, base: str) -> datetime:
    """What a red run must be created at or after to file once `issue`
    closed. The harness's green close, known by the comment it closed with:
    its green run's creation, since a run created between the tick's read and
    the close landing must still file. The harness's other closes need no
    care: a merged fix's issue closes only once the next is open, and an
    orphan's workflow no longer runs. Any other close: the close itself (#442)."""
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
        if (key.target is Target.PR) != bool(state.head):
            raise ValueError("a PR is opened for an issue, and recorded on a PR at its head")
        issue = key.number if key.target is Target.ISSUE else harness_branch_issue(state.branch)
        return (OpenPullRequest(issue or 0, state.branch, state.head),)
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
