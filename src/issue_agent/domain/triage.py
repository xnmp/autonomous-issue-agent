"""Steward requirements-triage decision logic — pure port of steward.sh:triage_once.

Split into the three independently-testable concerns the bash tangles together:
  1. `triage_candidates` — which issues to triage this pass (the candidate set +
     the updatedAt dedupe that prevents re-triaging an unchanged issue).
  2. `TriageVerdict` — the structured output the agent returns (verdict + plain
     summary + AC + blocking gaps); pure data.
  3. `actions_for_verdict` — verdict → comment/relabel actions, gated by
     STEWARD_PROMOTE.
  4. `recurrence_actions` — the ADVISORY recurrence marker, kept separate from
     (3) so the triage verdict mapping is unchanged whether or not the recurrence
     context was available.

The feedback-loop lesson lives in `record_after`: the baseline written for an
issue must be its updatedAt AS OF AFTER the steward's own comment+relabel (which
themselves bump updatedAt), so only a LATER human reply re-triggers triage.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .actions import Action, AddDependency, PostComment, Relabel, Target
from .labels import LIFECYCLE_LABELS, Label
from .world import IssueView, WorldState


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
    """The user-authored issue fields grooming may act on, excluding comments."""
    return f"{issue.title}\0{issue.body}"


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
        if issue.updated_at != seen_at.get(issue.number, "")
        and (not content.get(issue.number) or content[issue.number] != grooming_content(issue))
    ][:max(0, limit)]


@dataclass(frozen=True)
class TriageVerdict:
    """Structured output of the triage agent (TRIAGE_SCHEMA).

    Gaps are split by audience: `creator_questions` (plain, for the issue author)
    and `owner_questions` (technical/architectural, for a repo maintainer). Either
    non-empty → blocked. `needs_owner` distinguishes a blocker that only a MAINTAINER
    can clear (e.g. a domain-model decision) from one the author can."""

    verdict: str  # "ready" | "blocked"
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


def actions_for_verdict(
    issue_number: int,
    author: str,
    verdict: TriageVerdict,
    cfg: TriageConfig,
) -> list[Action]:
    """Map a triage verdict to comment + relabel actions.

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
