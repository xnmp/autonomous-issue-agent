"""Steward requirements-triage decision logic — pure port of steward.sh:triage_once.

Split into the three independently-testable concerns the bash tangles together:
  1. `triage_candidates` — which issues to triage this pass (the candidate set +
     the updatedAt dedupe that prevents re-triaging an unchanged issue).
  2. `TriageVerdict` — the structured output the agent returns (verdict + plain
     summary + AC + blocking gaps); pure data.
  3. `actions_for_verdict` — verdict → comment/relabel/close actions, gated by
     STEWARD_PROMOTE (and, for `close`, by evidence and by whether a human has
     already reopened the issue).
  4. `recurrence_actions` — the ADVISORY recurrence marker, kept separate from
     (3) so the triage verdict mapping is unchanged whether or not the recurrence
     context was available.

The feedback-loop lesson lives in `record_after`: the baseline written for an
issue must be its updatedAt AS OF AFTER the steward's own comment+relabel (which
themselves bump updatedAt), so only a LATER human reply re-triggers triage.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .actions import Action, AddDependency, CloseIssue, PostComment, Relabel, Target
from .gate import Adr, check_adr_references
from .labels import LIFECYCLE_LABELS, Label
from .lifecycle import AllowedEligibility, LifecycleOperation, issue_eligibility
from .world import IssueView, PRView, WorldState


_ADR_STEWARD_PREFIX = "**Architecture decision check:**"


@dataclass(frozen=True)
class TriageConfig:
    steward_bot: str
    enabled: bool = False
    promote: bool = False
    triage_label: Label = Label.AI_TRIAGE
    # Handle to @-mention for OWNER-level (technical/architectural) questions.
    # Resolved by the app layer (CODEOWNERS → STEWARD_OWNER → none). Blank → the
    # owner section is still posted but tagged generically ("a maintainer").
    owner_handle: str = ""


def adr_steward_actions(
    prs: tuple[PRView, ...],
    adrs: tuple[Adr, ...],
    steward_bot: str,
) -> list[Action]:
    """Flag governed PRs whose description omits the applicable ADR.

    The PR-create governor is the hard gate for harness publications. This
    advisory reconciler also covers human/external PRs and older open PRs. A
    prior comment suppresses repeats only when it came from the configured
    steward identity; repository text cannot forge the durable observation.
    """

    actions: list[Action] = []
    for pr in prs:
        if pr.state.upper() != "OPEN":
            continue
        result = check_adr_references(
            lambda: adrs,
            pr.changed_files,
            pr.body,
        )
        if result.ok:
            continue
        if any(
            comment.author.casefold() == steward_bot.casefold()
            and comment.body.startswith(_ADR_STEWARD_PREFIX)
            for comment in pr.comments
        ):
            continue
        actions.append(
            PostComment(
                Target.PR,
                pr.number,
                f"{_ADR_STEWARD_PREFIX} {result.reason}. "
                "Cite the governing ADR in the PR description or add a focused ADR.",
            )
        )
    return actions


def parse_codeowners_default(text: str) -> str:
    """Pure: extract the default (`*` pattern) owners from a CODEOWNERS file's
    contents, as a space-joined handle string ('@org/team @alice'), or '' if none.
    Only the catch-all `*` rule is used — triage runs BEFORE implementation, so it
    cannot know which specific paths an issue will touch; the repo-wide owners are
    the right default audience for an architectural decision. Later `*` rules win
    (CODEOWNERS is last-match-wins). Comments (#) and blank lines ignored."""
    owners = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "*" and len(parts) > 1:
            owners = " ".join(parts[1:])
    return owners


@dataclass(frozen=True)
class TriageState:
    """issue → recorded updatedAt baseline (SDIR/triage-N.at)."""

    seen_at: dict[int, str] = field(default_factory=dict)


def grooming_content(issue: IssueView) -> str:
    """Stable digest of user-authored fields grooming may act on.

    Store reads trim terminal whitespace and normalize newlines, so persisting
    raw issue text would make comment-only updates look like body edits. Hex
    output is immune to both transformations.
    """
    return grooming_content_signature(issue.title, issue.body)


def grooming_content_signature(title: str, body: str) -> str:
    source = f"{title}\0{body}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def grooming_candidates(
    world: WorldState, seen_at: Mapping[int, str], enabled: bool,
    seen_content: Mapping[int, str] | None = None, limit: int = 5,
) -> list[int]:
    """Bounded open-issue grooming candidates.

    Comments (including the harness's own triage/recurrence notices) change
    ``updated_at`` but do not alter the issue report. Once a content signature
    has been recorded, those unrelated updates must not buy another classifier
    run. The cap bounds first-enable spend; remaining unscanned issues retain an
    empty signature and are picked on later changed ticks.
    """
    if not enabled:
        return []
    content = seen_content or {}
    return [
        issue.number for issue in world.open_issues()
        if isinstance(issue_eligibility(issue, LifecycleOperation.GROOM), AllowedEligibility)
        and issue.updated_at != seen_at.get(issue.number, "")
        and (not content.get(issue.number) or content[issue.number] != grooming_content(issue))
    ][:max(0, limit)]


@dataclass(frozen=True)
class TriageVerdict:
    """Structured output of the triage agent (TRIAGE_SCHEMA).

    Gaps are split by audience: `creator_questions` (plain, for the issue author)
    and `owner_questions` (technical/architectural, for a repo maintainer). Either
    non-empty → blocked. `needs_owner` distinguishes a blocker that only a MAINTAINER
    can clear (e.g. a domain-model decision) from one the author can."""

    verdict: str  # "ready" | "blocked" | "close"
    plain_summary: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    creator_questions: tuple[str, ...] = ()
    owner_questions: tuple[str, ...] = ()
    # Numbers of already-open issues this one is blocked by (verified prerequisites).
    # The steward wires each as a native GitHub blocked_by link.
    blocked_by_issues: tuple[int, ...] = ()
    rationale: str = ""
    # ADVISORY: the harness-merged issue this one appears to re-report (the same bug
    # back, or a shipped feature that does not work). None = no recurrence claimed.
    # Never acted on beyond a label + comment — see `recurrence_actions`.
    recurrence_of: int | None = None
    # verdict="close" only. Why this issue needs no work, and what would make it
    # matter again. BOTH must be non-empty for a close to be acted on: a close is
    # the one triage disposition that ends an issue's life without a human, so an
    # unexplained one is treated as no close at all (see `is_close`).
    close_reason: str = ""
    reopen_if: str = ""
    # GitHub stateReason the classifier asked for; normalized by `close_state`.
    close_state_reason: str = ""

    @property
    def is_close(self) -> bool:
        """Whether this verdict is a close the steward may execute autonomously.

        A bare verdict="close" is NOT enough. The incident this exists for
        (keifu #143) was a triage agent concluding a merged PR had already
        resolved the issue — evidence a human can check. A close with no stated
        reason, or no condition under which it should come back, is a claim with
        nothing behind it and degrades to the ordinary human-disposed path.
        """
        return (
            self.verdict == "close"
            and bool(self.close_reason.strip())
            and bool(self.reopen_if.strip())
        )

    @property
    def close_state(self) -> str:
        """GitHub `stateReason` for the close.

        "completed" asserts the reported work actually shipped, so only the exact
        token earns it; every other shape — absent, misspelt, huge, non-string
        upstream — reads as "not_planned", which claims nothing.
        """
        return (
            "completed"
            if self.close_state_reason.strip().casefold() == "completed"
            else "not_planned"
        )

    @property
    def is_blocked(self) -> bool:
        # blocked iff there is at least one genuine open question, for EITHER
        # audience. verdict="blocked" alone (with no questions) is not enough — a
        # gap must be concrete to block.
        return self.verdict == "blocked" and bool(
            self.creator_questions or self.owner_questions
        )

    @property
    def needs_owner(self) -> bool:
        """A maintainer must answer before this can proceed (architectural/domain
        decision). The author cannot clear it."""
        return bool(self.owner_questions)


def is_untriaged(issue: IssueView) -> bool:
    """Open issue carrying NONE of the lifecycle labels. ai-triage is NOT a
    lifecycle label, so a previously-triaged issue is eligible again — but the
    updatedAt dedupe (below) keeps it out until the author actually replies.
    Port of the jq select() in triage_once."""
    return issue.state == "open" and not (issue.labels & LIFECYCLE_LABELS)


def triage_candidates(
    world: WorldState, cfg: TriageConfig, state: TriageState
) -> list[int]:
    """Issue numbers to triage this pass: untriaged AND whose updatedAt differs
    from the recorded baseline (unchanged since last triage → skip). Empty when
    triage is disabled."""
    if not cfg.enabled:
        return []
    out: list[int] = []
    for issue in world.open_issues():
        if not isinstance(issue_eligibility(issue, LifecycleOperation.TRIAGE), AllowedEligibility):
            continue
        if not is_untriaged(issue):
            continue
        if issue.updated_at == state.seen_at.get(issue.number, ""):
            continue  # already triaged this exact version
        out.append(issue.number)
    return out


def _owner_mention(cfg: TriageConfig) -> str:
    """How to address the maintainer in the owner section: the resolved handle if
    set (already carries its own @/@org form), else a generic phrase."""
    return cfg.owner_handle.strip() or "a maintainer"


# Ceiling on classifier prose the harness republishes. The close comment is the
# first agent-authored text that accompanies a DESTRUCTIVE act, and its source is
# an attacker-writable issue body: an unbounded body is a spam vector, and a bare
# @handle in it is a mass ping GitHub delivers on the harness's behalf. Backticks
# neutralize the mention without hiding what the classifier actually said.
_CLOSE_PROSE_LIMIT = 1500
_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9-]*)")


def _quoted_prose(text: str) -> str:
    trimmed = text.strip()[:_CLOSE_PROSE_LIMIT]
    return _MENTION_RE.sub(r"`@\1`", trimmed)


def _close_body(author: str, verdict: TriageVerdict) -> str:
    return (
        "**Closing this issue 🗂️**\n\n"
        f"@{author} {_quoted_prose(verdict.close_reason)}\n\n"
        f"**Reopen this issue if:** {_quoted_prose(verdict.reopen_if)}\n\n"
        "_Closed by the triage steward. If this is wrong, just reopen it — a "
        "reopened issue is triaged again from scratch and is never auto-closed "
        "a second time._"
    )


def close_actions(
    issue_number: int,
    author: str,
    verdict: TriageVerdict,
    cfg: TriageConfig,
) -> list[Action]:
    """One durable disposition owns its labels, explanation and issue closure."""
    return [
        CloseIssue(issue_number, verdict.close_state, _close_body(author, verdict),
                   remove_labels=(cfg.triage_label,), add_labels=(Label.AI_CLOSED,)),
    ]


def declined_close_actions(
    issue_number: int,
    author: str,
    cfg: TriageConfig,
    reason: str,
) -> list[Action]:
    """A close the steward refuses to execute — hand the issue back to a human.

    Two callers, one shape: a close verdict missing its reason/reopen condition,
    and a close verdict on an issue a human already reopened. Both post one
    comment and leave the issue OPEN under `ai-triage`; neither may fall through
    to the ready path, because "ready" would queue a paid resolve leg for an
    issue the classifier just said needs no work.
    """
    return [
        PostComment(
            Target.ISSUE,
            issue_number,
            "**Triage needs a human here** 👀\n\n"
            f"@{author} the triage pass suggested closing this issue, but "
            f"{reason} — so it stays open for a maintainer to decide.",
        ),
        Relabel(Target.ISSUE, issue_number, None, cfg.triage_label),
    ]


def actions_for_verdict(
    issue_number: int,
    author: str,
    verdict: TriageVerdict,
    cfg: TriageConfig,
    *,
    state_reason: str = "",
    labels: frozenset[Label] = frozenset(),
) -> list[Action]:
    """Triage disposition actions, with the terminal close marker retired first.

    An issue that is being triaged at all is back in the pipeline, so a leftover
    `ai-closed` from an earlier steward close is stale the moment any other
    disposition is reached. Clearing it here — rather than in each branch — keeps
    the marker from outliving the close it recorded and reading as terminal to
    every consumer that maps labels to a phase.
    """
    actions = _disposition_actions(
        issue_number, author, verdict, cfg, state_reason=state_reason
    )
    if Label.AI_CLOSED in labels and not any(
        isinstance(a, CloseIssue) for a in actions
    ):
        return [Relabel(Target.ISSUE, issue_number, Label.AI_CLOSED, None), *actions]
    return actions


def _disposition_actions(
    issue_number: int,
    author: str,
    verdict: TriageVerdict,
    cfg: TriageConfig,
    *,
    state_reason: str = "",
) -> list[Action]:
    """Map a triage verdict to comment + relabel (+ close) actions.

    CLOSE → the issue needs no work at all (already fixed, duplicate, invalid,
    resolved by a merged PR): comment with the reason and the reopen condition,
    close it, and mark it `ai-closed`. Refused — and handed back to a human —
    when the evidence is incomplete, or when `state_reason` says a human already
    reopened this issue: `IssueView.state_reason` is the deterministic record
    that its lifecycle ended once and someone deliberately put it back, and the
    harness must never overrule that by closing it again. That is also the loop
    guard: reopen → fresh triage → at most one comment, never a second close.
    BLOCKED → ONE comment with up to two clearly-separated sections: a plain,
    non-technical section addressed to the author (creator_questions), and — when
    there are owner_questions — a technical section addressed to the repo owner
    (@-mentioned) for architectural/domain decisions the author must NOT be asked
    to make. The owner sees BOTH sections (the whole thread), so the creator's
    plain Q&A stays visible to them. Add ai-triage.
    READY + promote → "Ready to build" comment (AC tucked in <details>) and
    relabel ai-triage→ai-ready (autonomous front door).
    READY + no promote → same comment but only ADD ai-triage; a human promotes.
    """
    if verdict.verdict == "close":
        if not cfg.promote:
            # Same authority switch that gates ai-ready promotion. Closing an
            # issue is strictly more destructive than queueing one, so a
            # deployment that still wants a human to make the state-changing
            # call must not acquire autonomous closing by hot-reload.
            return declined_close_actions(
                issue_number,
                author,
                cfg,
                "this deployment leaves lifecycle decisions to a human "
                "(STEWARD_PROMOTE is off)",
            )
        if not verdict.is_close:
            return declined_close_actions(
                issue_number,
                author,
                cfg,
                "it did not say why, or under what condition the issue should "
                "come back",
            )
        if state_reason.strip().casefold() == "reopened":
            return declined_close_actions(
                issue_number,
                author,
                cfg,
                "this issue was already closed once and reopened by a human",
            )
        return close_actions(issue_number, author, verdict, cfg)

    ac = "\n".join(f"- {c}" for c in verdict.acceptance_criteria)

    # Verified cross-issue prerequisites → native blocked_by links. Skip a self-
    # reference and dedupe. The fixer's dependency gate holds this issue until each
    # blocker closes; a note in the comment makes the linkage visible to humans.
    blockers = []
    for b in verdict.blocked_by_issues:
        if b != issue_number and b not in blockers:
            blockers.append(b)
    dep_actions: list[Action] = [
        AddDependency(issue_number, b) for b in blockers
    ]
    dep_note = (
        "\n\n### Depends on\n"
        "This is blocked until these land first (linked as GitHub dependencies):\n"
        + "\n".join(f"- #{b}" for b in blockers)
        if blockers else ""
    )

    if verdict.is_blocked:
        parts = [
            "**A quick check before we start** 🛠️\n\n"
            f"Hi @{author} — thanks for raising this. "
            f"{verdict.plain_summary or 'I need a little more detail before this can be picked up.'}"
        ]
        if verdict.creator_questions:
            cq = "\n".join(f"- {q}" for q in verdict.creator_questions)
            parts.append(
                f"### For you, @{author}\n"
                "In plain terms, could you help with:\n\n"
                f"{cq}\n\n"
                "_Just reply here in your own words — no technical detail needed._"
            )
        if verdict.owner_questions:
            oq = "\n".join(f"- {q}" for q in verdict.owner_questions)
            parts.append(
                f"### For a maintainer — {_owner_mention(cfg)}\n"
                "These are technical/architectural calls that need an owner's "
                "decision (please don't ask the issue author to make them):\n\n"
                f"{oq}"
            )
        body = "\n\n".join(parts) + dep_note
        return [
            PostComment(Target.ISSUE, issue_number, body),
            Relabel(Target.ISSUE, issue_number, None, cfg.triage_label),
            *dep_actions,
        ]

    tech_block = (
        "<details><summary>Technical acceptance criteria (for the engineer)"
        f"</summary>\n\n{ac}\n</details>"
    )
    # A verified prerequisite means the issue is not truly ready to START even if
    # its spec is clear — do NOT auto-promote to ai-ready while a blocker is open
    # (the fixer's dependency gate would hold it regardless, but the label should
    # not read "ready"). Promote only when there are no blockers.
    if cfg.promote and not blockers:
        body = (
            "**Ready to build ✅**\n\n"
            f"@{author} {verdict.plain_summary or 'This is clear enough to start — queuing it up now.'}\n\n"
            f"{tech_block}"
        )
        return [
            PostComment(Target.ISSUE, issue_number, body),
            Relabel(Target.ISSUE, issue_number, cfg.triage_label, Label.AI_READY),
        ]
    tail = (
        "Queued once its prerequisites land." if blockers
        else "A maintainer will queue it up."
    )
    body = (
        "**Ready to build ✅**\n\n"
        f"@{author} {verdict.plain_summary or 'This looks clear enough to start.'} "
        f"{tail}\n\n"
        f"{tech_block}{dep_note}"
    )
    return [
        PostComment(Target.ISSUE, issue_number, body),
        Relabel(Target.ISSUE, issue_number, None, cfg.triage_label),
        *dep_actions,
    ]


def recurrence_actions(
    issue_number: int,
    verdict: TriageVerdict,
    merged_by_issue: Mapping[int, int],
) -> list[Action]:
    """The ADVISORY half of harness issue #10: this new issue re-reports something
    the harness already shipped → one comment naming the original + its PR, and the
    `recurrence` label. Never a reopen, never a dispatch, never a blocking link.

    `merged_by_issue` maps a RECENTLY-MERGED issue number to the PR that shipped it
    — a plain int→int map rather than an adapter type, so the domain stays pure.
    It is also the WHITELIST: a `recurrence_of` naming anything the harness did not
    just merge yields NO actions. That is the containment for a prompt-injected or
    hallucinated verdict — the classifier reads attacker-writable issue bodies, so
    it must not be able to label and comment on an arbitrary issue of its choosing.
    A self-reference is dropped for the same reason (an issue cannot recur as
    itself, and the merged map could contain it if the fixer just shipped it).

    Empty list = say nothing, which is the right default for an advisory signal:
    a missed recurrence costs a metric, a false one costs a human's attention.
    """
    of = verdict.recurrence_of
    if of is None or of == issue_number or of not in merged_by_issue:
        return []
    pr = merged_by_issue[of]
    body = (
        "**Possible recurrence** ♻️\n\n"
        f"Possible recurrence of #{of} (shipped in PR #{pr}). "
        "Advisory — remove the `recurrence` label if unrelated."
    )
    return [
        PostComment(Target.ISSUE, issue_number, body),
        Relabel(Target.ISSUE, issue_number, None, Label.RECURRENCE),
    ]


def grooming_actions(
    issue_number: int,
    *,
    resolved_by: int | None,
    stale_reference: str,
    merged_by_issue: Mapping[int, int],
) -> list[Action]:
    """Map a validated grooming finding to advisory GitHub writes.

    The classifier reads issue text, so a claimed resolver is accepted only when
    it is in the gateway-provided recent-merge whitelist. Neither finding can
    close, dispatch, or otherwise move an issue through the lifecycle.
    """
    actions: list[Action] = []
    if resolved_by is not None and resolved_by != issue_number and resolved_by in merged_by_issue:
        pr = merged_by_issue[resolved_by]
        actions.extend((
            PostComment(
                Target.ISSUE, issue_number,
                "**Possibly resolved by merged work** ✅\n\n"
                f"Recent work for #{resolved_by} shipped in PR #{pr} and may already "
                "cover this report. Advisory only — please confirm and close this issue "
                "if it is resolved, or remove `maybe-resolved` if it is not.",
            ),
            Relabel(Target.ISSUE, issue_number, None, Label.MAYBE_RESOLVED),
        ))
    ref = stale_reference.strip()
    if ref:
        actions.append(PostComment(
            Target.ISSUE, issue_number,
            "**Possible stale issue context** 🧹\n\n"
            f"The referenced artifact `{ref}` no longer appears to exist in the repository. "
            "Advisory only — please re-scope this issue if the intended behavior still matters.",
        ))
    return actions
