"""Offline review-rejection family activation planning; ADR 0015 "Migration and rollback".

Legacy per-PR review files (`pr-N.last-sha`, `pr-N.patch-id`,
`pr-N.operator-guidance`), one fresh harness read of every pull request that
matters and a verified deployment inventory become either one exact
activation plan or an explicit refusal. Nothing here guesses:

- a reviewed baseline is imported only when both baseline files are present
  and well formed AND a fresh COMPLETE review read shows the review bot's
  latest review, undismissed, at that head; otherwise a fresh review is owed;
- a rejection ceiling is imported only from a fresh complete read under the
  counting rule the runtime uses, and only for a pull request the legacy
  runtime holds at that ceiling (its attention label is on GitHub, or its
  escalation comment stands at the head with the trigger gone: the keifu #173
  dead state), so no pull request the legacy runtime would still review is
  held by activation;
- guidance moves verbatim into the episode, or refuses when it could not
  have been accepted as guidance;
- a closed or merged pull request's files are evidence only (plan decision 9);
- an unreadable, duplicated or unmapped legacy key, a pull request whose own
  read failed, an open-PR listing that is not complete, and a held pull
  request whose review history is only partially readable all refuse.

The shadow comparison reports, per pull request, what the legacy runtime and
the new authority would dispatch; shadow data never decides anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from .identity import login_eq
from .labels import Label
from .resolve_activation import (
    FIRST_EPOCH, MAX_LEGACY_FILE_BYTES, ActivationBlocked, DeploymentInventory, ExistingActivation, LegacyAbsent,
    LegacyFile, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry,
)
from .review import labels_holding_review
from .review_rejection import (
    MAX_OBSERVED_REVIEWS, CountedReview, CountingRule, PullRequestState, ReviewRejectionCeiling, is_commit_sha,
    validate_commit_sha,
)
from .review_workflow import (
    CompleteReviews, FreshReviewRequired, ImportedBaseline, ObservedPullRequest, PartialReviews,
    PullRequestUnavailable, ReviewedBaseline, ReviewEpisode, ReviewWorkflowSnapshot, SubmittedReviewState,
)
from .review_workflow_policy import review_ceiling_cause, review_ceiling_gate, review_owed
from .workflow import GUIDANCE_MAX_CHARS, MAX_DETAIL_CHARS
from .workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
    canonical_workflow_repository, validate_workflow_identifier, validate_workflow_integer, validate_workflow_text,
)

LEGACY_LAST_SHA = "last-sha"
LEGACY_PATCH_ID = "patch-id"
LEGACY_GUIDANCE = "operator-guidance"
LEGACY_SUFFIXES = (LEGACY_LAST_SHA, LEGACY_PATCH_ID, LEGACY_GUIDANCE)
# The review prompt fences guidance with this marker; a note carrying it could
# never have been accepted as guidance (app/dashweb/guidance.py FENCE_MARKER).
GUIDANCE_FENCE_MARKER = "<<<"
MAX_IMPORTED_PULL_REQUESTS = 4096

_DIGEST = re.compile(r"[0-9a-f]{64}")
_FAMILY = WorkflowFamily.REVIEW_REJECTIONS


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _root(value: str) -> None:
    validate_workflow_text(value, "legacy state root label", limit=4096)


@dataclass(frozen=True)
class LegacyReviewRecord:
    """One pull request's retired review files in one labelled state root."""

    pr: int
    last_sha: LegacyFile
    patch_id: LegacyFile
    guidance: LegacyFile
    root: str = "review"

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "legacy pull request number", minimum=1)
        _root(self.root)
        for value in (self.last_sha, self.patch_id, self.guidance):
            if not isinstance(value, (LegacyAbsent, LegacyPresent, LegacyUnreadable)):
                raise ValueError("legacy review record requires typed file observations")


LegacyReviewEntry = LegacyReviewRecord | UnmappedLegacyEntry


@dataclass(frozen=True)
class OpenPullRequestsListed:
    """Every open pull request number; the listing was complete."""

    numbers: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.numbers, tuple) or list(self.numbers) != sorted(set(self.numbers)):
            raise ValueError("open pull requests must be a sorted unique tuple")
        if len(self.numbers) > MAX_IMPORTED_PULL_REQUESTS:
            raise ValueError("open pull request listing exceeds the activation bound")
        for number in self.numbers:
            validate_workflow_integer(number, "open pull request number", minimum=1)


@dataclass(frozen=True)
class OpenPullRequestsUnavailable:
    """The listing failed or may be truncated: absence of a pull request is unproven."""

    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "open pull request listing failure", limit=MAX_DETAIL_CHARS)


OpenPullRequests = OpenPullRequestsListed | OpenPullRequestsUnavailable
PullRequestRead = ObservedPullRequest | PullRequestUnavailable


# -- one pull request's import decision ------------------------------------------------


@dataclass(frozen=True)
class ImportedBaselineFacts:
    head_sha: str
    patch_id: str

    def __post_init__(self) -> None:
        validate_commit_sha(self.head_sha, "imported reviewed head")
        _digest(self.patch_id, "imported reviewed patch")


@dataclass(frozen=True)
class ImportedCeilingFacts:
    """The rejection ceiling a complete read proved, captured exactly as a gate cause needs it."""

    head_sha: str
    counted: tuple[CountedReview, ...]
    non_resetting_commits: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_commit_sha(self.head_sha, "imported ceiling head")
        # Empty only at ceiling 1, where the first rejection is itself the escalation.
        if not isinstance(self.counted, tuple) or len(self.counted) > MAX_OBSERVED_REVIEWS or any(
            not isinstance(review, CountedReview) for review in self.counted
        ):
            raise ValueError("an imported ceiling carries its bounded counted reviews")
        if len({review.review_id for review in self.counted}) != len(self.counted):
            raise ValueError("an imported ceiling counts each review once")
        CountingRule("activation-bot", self.non_resetting_commits)


class LegacyHoldProvenance(str, Enum):
    CEILING = "ceiling"
    VERDICT = "verdict"
    UNPROVEN = "unproven"


@dataclass(frozen=True)
class ReviewActivationFacts:
    """Fresh runtime eligibility and authenticated counting evidence for one PR.

    The adapter hashes every evidence input, including counter and grant
    sources, so an unchanged decision cannot hide a changed source.
    """

    pr: int
    excluded: tuple[str, ...]
    non_resetting_commits: tuple[str, ...]
    evidence_digest: str
    hold_provenance: LegacyHoldProvenance = LegacyHoldProvenance.UNPROVEN
    linked_holds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "activation facts pull request", minimum=1)
        if not isinstance(self.excluded, tuple):
            raise ValueError("activation exclusion reasons must be immutable")
        for reason in self.excluded:
            validate_workflow_text(reason, "activation exclusion", limit=MAX_DETAIL_CHARS)
        CountingRule("activation-bot", self.non_resetting_commits)
        _digest(self.evidence_digest, "activation evidence digest")
        if not isinstance(self.hold_provenance, LegacyHoldProvenance):
            raise ValueError("activation hold provenance must be typed")
        if not isinstance(self.linked_holds, tuple) or len(self.linked_holds) > 32:
            raise ValueError("activation linked holds must be bounded and immutable")
        for hold in self.linked_holds:
            validate_workflow_text(hold, "linked issue hold", limit=MAX_DETAIL_CHARS)


def _source(file: LegacyFile) -> str:
    if isinstance(file, LegacyPresent):
        return file.digest
    return "absent" if isinstance(file, LegacyAbsent) else "unreadable"


@dataclass(frozen=True)
class ImportedReviewTarget:
    """One open pull request's immutable import decision (the only trusted form is canonical JSON)."""

    pr: int
    episode_id: str
    guidance: str
    baseline: ImportedBaselineFacts | None
    fresh_reason: str
    ceiling: ImportedCeilingFacts | None
    last_sha_source: str
    patch_id_source: str
    guidance_source: str
    observation: str

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "imported pull request", minimum=1)
        validate_workflow_identifier(self.episode_id, "imported episode")
        validate_workflow_text(self.guidance, "imported guidance", limit=GUIDANCE_MAX_CHARS, empty=True)
        if self.baseline is not None and not isinstance(self.baseline, ImportedBaselineFacts):
            raise ValueError("an imported baseline must be typed")
        if self.ceiling is not None and not isinstance(self.ceiling, ImportedCeilingFacts):
            raise ValueError("an imported ceiling must be typed")
        # Exactly one of: an imported baseline, or the reason a fresh review is owed.
        if (self.baseline is None) == (self.fresh_reason == ""):
            raise ValueError("an import either establishes a baseline or names why a fresh review is owed")
        validate_workflow_text(self.fresh_reason, "fresh review reason", limit=MAX_DETAIL_CHARS, empty=True)
        for source in (self.last_sha_source, self.patch_id_source, self.guidance_source):
            if source != "absent":
                _digest(source, "imported source")
        _digest(self.observation, "imported observation")

    def canonical(self) -> str:
        return _canonical_json({
            "pr": self.pr, "episode_id": self.episode_id, "guidance": self.guidance,
            "baseline": None if self.baseline is None else {
                "head_sha": self.baseline.head_sha, "patch_id": self.baseline.patch_id},
            "fresh_reason": self.fresh_reason,
            "ceiling": None if self.ceiling is None else {
                "head_sha": self.ceiling.head_sha,
                "counted": [[review.review_id, review.commit_id] for review in self.ceiling.counted],
                "non_resetting_commits": list(self.ceiling.non_resetting_commits)},
            "last_sha_source": self.last_sha_source, "patch_id_source": self.patch_id_source,
            "guidance_source": self.guidance_source, "observation": self.observation,
        })

    @classmethod
    def parse(cls, raw: str) -> ImportedReviewTarget:
        """Validate a retained decision; only its exact canonical form is trusted."""
        # ASCII-escaped JSON spends up to 12 characters per astral code point.
        if not isinstance(raw, str) or len(raw) > 12 * (GUIDANCE_MAX_CHARS + MAX_DETAIL_CHARS) + 64 * MAX_OBSERVED_REVIEWS + 8192:
            raise ValueError("import decision must be bounded text")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "pr", "episode_id", "guidance", "baseline", "fresh_reason", "ceiling", "last_sha_source",
            "patch_id_source", "guidance_source", "observation",
        }:
            raise ValueError("import decision has the wrong fields")
        baseline, ceiling = value["baseline"], value["ceiling"]
        if baseline is not None:
            if not isinstance(baseline, dict) or set(baseline) != {"head_sha", "patch_id"}:
                raise ValueError("imported baseline has the wrong fields")
            baseline = ImportedBaselineFacts(**baseline)
        if ceiling is not None:
            if not isinstance(ceiling, dict) or set(ceiling) != {"head_sha", "counted", "non_resetting_commits"} or not isinstance(
                ceiling["counted"], list
            ) or not isinstance(ceiling["non_resetting_commits"], list) or any(
                not isinstance(item, list) or len(item) != 2 for item in ceiling["counted"]):
                raise ValueError("imported ceiling has the wrong fields")
            ceiling = ImportedCeilingFacts(ceiling["head_sha"], tuple(CountedReview(*item) for item in ceiling["counted"]),
                                          tuple(ceiling["non_resetting_commits"]))
        decision = cls(**{**value, "baseline": baseline, "ceiling": ceiling})
        if decision.canonical() != raw:
            raise ValueError("import decision is not canonical")
        return decision


def imports_digest(imports: tuple[ImportedReviewTarget, ...]) -> str:
    return _sha256([item.canonical() for item in sorted(imports, key=lambda item: item.pr)])


@dataclass(frozen=True)
class ReviewActivationPlan:
    repository: str
    epoch: int
    ceiling: int
    review_bot: str
    trigger_label: Label
    source_digest: str
    inventory_digest: str
    imports: tuple[ImportedReviewTarget, ...]
    facts: tuple[ReviewActivationFacts, ...] = ()

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("activation repository must be canonical")
        validate_workflow_integer(self.epoch, "activation epoch", minimum=1)
        validate_workflow_integer(self.ceiling, "activation ceiling")
        validate_workflow_identifier(self.review_bot, "activation review bot")
        if not isinstance(self.trigger_label, Label) or self.trigger_label is Label.NEEDS_HUMAN_REVIEW:
            raise ValueError("activation trigger must be a label other than the attention hold")
        _digest(self.source_digest, "activation source digest")
        _digest(self.inventory_digest, "activation inventory digest")
        if not isinstance(self.imports, tuple) or any(not isinstance(i, ImportedReviewTarget) for i in self.imports):
            raise ValueError("activation imports must be typed")
        numbers = [item.pr for item in self.imports]
        if numbers != sorted(set(numbers)) or len(numbers) > MAX_IMPORTED_PULL_REQUESTS:
            raise ValueError("activation imports must be sorted by unique pull request")
        if self.ceiling <= 0 and any(item.ceiling is not None for item in self.imports):
            raise ValueError("a disabled ceiling imports no ceiling gate")
        if not isinstance(self.facts, tuple) or any(not isinstance(item, ReviewActivationFacts) for item in self.facts):
            raise ValueError("activation facts must be immutable and typed")
        if [item.pr for item in self.facts] != sorted({item.pr for item in self.facts}):
            raise ValueError("activation facts must be sorted and unique")

    @property
    def authority(self) -> FamilyAuthority:
        return FamilyAuthority(_FAMILY, self.epoch)

    @property
    def rule(self) -> CountingRule:
        return CountingRule(self.review_bot)

    @property
    def imports_digest(self) -> str:
        return imports_digest(self.imports)

    @property
    def plan_digest(self) -> str:
        """What an operator reviews and confirms: every committed decision."""
        return _sha256({
            "repository": self.repository, "family": _FAMILY.value, "epoch": self.epoch,
            "ceiling": self.ceiling, "review_bot": self.review_bot, "trigger_label": self.trigger_label.value,
            "source_digest": self.source_digest, "inventory_digest": self.inventory_digest,
            "imports": [item.canonical() for item in self.imports],
        })

    def matches(self, existing: ExistingActivation) -> bool:
        return (existing.epoch, existing.source_digest, existing.inventory_digest, existing.imports_digest,
                existing.ceiling, existing.plan_digest) == (
            self.epoch, self.source_digest, self.inventory_digest, self.imports_digest,
            self.ceiling, self.plan_digest)


@dataclass(frozen=True)
class ReviewFamilyActive:
    """The review family is already activated: its recorded plan stands, never re-planned.

    Unlike resolve, review imports depend on live GitHub reads, so an
    activated plan is recognised from its recorded digest and decisions, not
    recomputed from today's sources (which are evidence only).
    """

    activation: ExistingActivation
    imports: tuple[ImportedReviewTarget, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.activation, ExistingActivation):
            raise ValueError("an active review family carries its recorded activation")
        if imports_digest(self.imports) != self.activation.imports_digest:
            raise ValueError("recorded review imports disagree with their activation")


ReviewActivationDecision = ReviewActivationPlan | ReviewFamilyActive | ActivationBlocked


# -- sources -----------------------------------------------------------------------------


def observation_digest(read: PullRequestRead) -> str:
    """A pull request read's decision facts; the read time and failure wording are excluded
    so an unchanged remote reproduces the reviewed plan."""
    if isinstance(read, PullRequestUnavailable):
        return _sha256({"pr": read.key.target.number, "read": "unavailable"})
    reviews: object
    if isinstance(read.reviews, CompleteReviews):
        reviews = [[r.review_id, r.author, r.state.value, r.commit_id, r.submitted_at.isoformat(), r.review_command]
                   for r in read.reviews.reviews]
    else:
        reviews = "partial"
    return _sha256({
        "pr": read.key.target.number, "state": read.state.value, "head": read.head_sha,
        "labels": sorted(label.value for label in read.labels), "reviews": reviews,
    })


def _entry_sources(entry: LegacyReviewEntry) -> list[list[object]]:
    if isinstance(entry, UnmappedLegacyEntry):
        return [["unmapped", entry.root, entry.name, entry.reason]]
    return [[entry.root, entry.pr, suffix, _source(file)] for suffix, file in (
        (LEGACY_LAST_SHA, entry.last_sha), (LEGACY_PATCH_ID, entry.patch_id), (LEGACY_GUIDANCE, entry.guidance))]


def source_digest(
    repository: str, entries: tuple[LegacyReviewEntry, ...], reads: tuple[PullRequestRead, ...],
    listing: OpenPullRequests,
    facts: tuple[ReviewActivationFacts, ...] = (),
) -> str:
    rows = sorted((row for entry in entries for row in _entry_sources(entry)), key=_canonical_json)
    return _sha256({
        "repository": repository, "family": _FAMILY.value, "sources": rows,
        "observations": sorted(observation_digest(read) for read in reads),
        "open_pull_requests": list(listing.numbers) if isinstance(listing, OpenPullRequestsListed) else "unavailable",
        "runtime_facts": [[f.pr, f.excluded, f.non_resetting_commits, f.evidence_digest,
                           f.hold_provenance.value, f.linked_holds] for f in facts],
    })


# -- parsing the retired files ---------------------------------------------------------


class _Blocked(ValueError):
    pass


def _scalar(file: LegacyFile, suffix: str) -> str | None:
    """A retired scalar marker's text, None when absent; unreadable refuses."""
    if isinstance(file, LegacyAbsent):
        return None
    if isinstance(file, LegacyUnreadable):
        raise _Blocked(f"{suffix} is unreadable: {file.reason}")
    return file.text.strip()


def parse_legacy_guidance(file: LegacyFile) -> str:
    """Preserve the original note verbatim, with the current guidance bounds."""
    if isinstance(file, LegacyAbsent):
        return ""
    if isinstance(file, LegacyUnreadable):
        raise _Blocked(f"{LEGACY_GUIDANCE} is unreadable: {file.reason}")
    text = file.text
    if "\x00" in text:
        raise _Blocked(f"{LEGACY_GUIDANCE} contains NUL")
    if len(text) > GUIDANCE_MAX_CHARS:
        raise _Blocked(f"{LEGACY_GUIDANCE} exceeds {GUIDANCE_MAX_CHARS} characters")
    if GUIDANCE_FENCE_MARKER in text:
        raise _Blocked(f"{LEGACY_GUIDANCE} contains the prompt fence marker")
    return text


def _baseline(
    record: LegacyReviewRecord | None, read: ObservedPullRequest, review_bot: str,
) -> tuple[ImportedBaselineFacts | None, str]:
    """(baseline, "") or (None, why a fresh review is owed); never a guessed baseline."""
    if record is None:
        return None, "no legacy reviewed head"
    last_sha = _scalar(record.last_sha, LEGACY_LAST_SHA)
    patch_id = _scalar(record.patch_id, LEGACY_PATCH_ID)
    if not last_sha:
        return None, "no legacy reviewed head"
    if not is_commit_sha(last_sha):
        return None, "legacy reviewed head is not a full commit SHA"
    if not patch_id:
        return None, "no legacy reviewed patch identity"
    if _DIGEST.fullmatch(patch_id) is None:
        return None, "legacy reviewed patch identity is not a SHA-256 digest"
    if not isinstance(read.reviews, CompleteReviews):
        return None, "review history is only partially readable"
    mine = [review for review in read.reviews.reviews if login_eq(review.author, review_bot)]
    if not mine:
        return None, "the review bot has no review on the pull request"
    latest = mine[-1]
    if latest.state is SubmittedReviewState.DISMISSED:
        return None, "the review bot's latest review was dismissed"
    if latest.commit_id != last_sha:
        return None, "the review bot's latest review is not at the legacy reviewed head"
    return ImportedBaselineFacts(last_sha, patch_id), ""


def _escalated_at_head(read: ObservedPullRequest, reviews: CompleteReviews, review_bot: str) -> bool:
    """The legacy escalation stands: the bot's newest undismissed review is its COMMENT at the head."""
    mine = [review for review in reviews.reviews
            if login_eq(review.author, review_bot) and review.state is not SubmittedReviewState.DISMISSED]
    return bool(mine) and mine[-1].state is SubmittedReviewState.COMMENTED and mine[-1].commit_id == read.head_sha


def legacy_ceiling_hold(read: ObservedPullRequest, trigger_label: Label, review_bot: str) -> bool:
    """Whether the legacy runtime holds this pull request for a human (never reviews it)."""
    if Label.NEEDS_HUMAN_REVIEW in read.labels:
        return True
    if not isinstance(read.reviews, CompleteReviews):
        return False
    return trigger_label not in read.labels and _escalated_at_head(read, read.reviews, review_bot)


def legacy_hold_provenance(
    read: ObservedPullRequest, review_bot: str, bodies: tuple[tuple[str, str], ...],
) -> LegacyHoldProvenance:
    """Classify the harness preamble on its bot's current-head escalation.

    Bodies come from the same complete authenticated bot-review read as the
    identity facts. Quoted rationale cannot match a complete first paragraph.
    """
    if not isinstance(read.reviews, CompleteReviews):
        return LegacyHoldProvenance.UNPROVEN
    mine = [review for review in read.reviews.reviews
            if login_eq(review.author, review_bot) and review.state is not SubmittedReviewState.DISMISSED]
    if not mine or mine[-1].state is not SubmittedReviewState.COMMENTED or mine[-1].commit_id != read.head_sha:
        return LegacyHoldProvenance.UNPROVEN
    body = dict(bodies).get(mine[-1].review_id, "")
    preamble = body.split("\n\n", 1)[0]
    if re.fullmatch(r"Through [1-9][0-9]{0,18} rounds of requested changes without resolution\. Requesting human review\.", preamble):
        return LegacyHoldProvenance.CEILING
    if re.fullmatch(r"The reviewer returned ESCALATE on rejection [1-9][0-9]{0,18} of -?[0-9]{1,19}\. Requesting human review\.", preamble):
        return LegacyHoldProvenance.VERDICT
    return LegacyHoldProvenance.UNPROVEN


def _ceiling(
    read: ObservedPullRequest, *, episode_id: str, review_bot: str, ceiling: int, trigger_label: Label,
    non_resetting_commits: tuple[str, ...] = (),
) -> ImportedCeilingFacts | None:
    """The counted set of a pull request the legacy runtime holds at the rejection ceiling, else None."""
    if ceiling <= 0 or not isinstance(read.reviews, CompleteReviews):
        return None
    if not legacy_ceiling_hold(read, trigger_label, review_bot):
        return None
    cause = review_ceiling_cause(read, episode_id=episode_id,
                                rule=CountingRule(review_bot, non_resetting_commits), ceiling=ceiling)
    if cause is None:
        return None
    return ImportedCeilingFacts(cause.head_sha, cause.counted, non_resetting_commits)


def imported_episode_id(repository: str, pr: int, epoch: int, digest: str) -> str:
    material = f"{repository}\0{pr}\0{_FAMILY.value}\0{epoch}\0{digest}"
    return "review-imported:" + hashlib.sha256(material.encode()).hexdigest()


def import_evidence_id(plan: ReviewActivationPlan, target: ImportedReviewTarget) -> str:
    return "review-activation-import:" + hashlib.sha256(
        f"{plan.repository}\0{target.pr}\0{plan.epoch}\0{plan.source_digest}".encode()
    ).hexdigest()


def imported_snapshot(plan: ReviewActivationPlan, target: ImportedReviewTarget) -> ReviewWorkflowSnapshot:
    """The row an import installs: generation 0, version 0, the imported episode and ceiling gate.

    No dispatch decision is recorded: the review daemon's first plan records
    one from its own read, so the view never shows an import-time "owed".
    """
    key = WorkflowKey(plan.repository, PullRequestRef(target.pr))
    revision = WorkflowRevision(WorkflowFence(key, 0), 0)
    continuation = FreshReviewRequired(0) if target.baseline is None else ReviewedBaseline(
        target.baseline.head_sha, target.baseline.patch_id, 0, ImportedBaseline(import_evidence_id(plan, target)),
    )
    gates = () if target.ceiling is None else (review_ceiling_gate(key, ReviewRejectionCeiling(
        target.episode_id, target.ceiling.head_sha, plan.review_bot, target.ceiling.counted, plan.ceiling,
        target.ceiling.non_resetting_commits,
    ), revision),)
    return ReviewWorkflowSnapshot(
        revision, plan.authority, ReviewEpisode(target.episode_id, target.guidance, continuation), gates,
    )


# -- the plan ----------------------------------------------------------------------------


def _reads_by_number(repository: str, reads: tuple[PullRequestRead, ...]) -> dict[int, PullRequestRead]:
    by_number: dict[int, PullRequestRead] = {}
    for read in reads:
        if not isinstance(read, (ObservedPullRequest, PullRequestUnavailable)):
            raise ValueError("activation requires typed pull request reads")
        if read.key.repository != repository:
            raise ValueError("a pull request read belongs to another repository")
        if read.key.target.number in by_number:
            raise ValueError("a pull request was read twice")
        by_number[read.key.target.number] = read
    return by_number


def _subsuming_record(records: list[LegacyReviewRecord]) -> LegacyReviewRecord | None:
    if len(records) == 1:
        return records[0]
    fields = ('last_sha', 'patch_id', 'guidance')
    if any(isinstance(getattr(record, field), LegacyUnreadable) for record in records for field in fields):
        return None
    candidate = min(records, key=lambda record: (
        -sum(isinstance(getattr(record, field), LegacyPresent) for field in fields), record.root))
    # Select an original complete record; fragments cannot manufacture a baseline.
    return candidate if all(isinstance(getattr(record, field), LegacyAbsent)
                            or getattr(record, field) == getattr(candidate, field)
                            for record in records for field in fields) else None


def _imports(
    repository: str, entries: tuple[LegacyReviewEntry, ...], reads: dict[int, PullRequestRead],
    listing: OpenPullRequests, digest: str, *, ceiling: int, review_bot: str, trigger_label: Label,
    facts: tuple[ReviewActivationFacts, ...] = (),
) -> tuple[tuple[ImportedReviewTarget, ...], tuple[str, ...]]:
    blockers: list[str] = []
    if isinstance(listing, OpenPullRequestsUnavailable):
        blockers.append(f"the open pull request listing is not complete: {listing.reason}")
    records: dict[int, list[LegacyReviewRecord]] = {}
    for entry in entries:
        if isinstance(entry, UnmappedLegacyEntry):
            blockers.append(f"unmapped legacy key {entry.name!r} in {entry.root}: {entry.reason}")
        else:
            records.setdefault(entry.pr, []).append(entry)
    selected = {pr: _subsuming_record(found) for pr, found in records.items()}
    blockers.extend(
        f"pull request #{pr}: ambiguous duplicate legacy records in {', '.join(sorted(r.root for r in found))}"
        for pr, found in sorted(records.items()) if len(found) > 1 and selected[pr] is None
    )
    listed = listing.numbers if isinstance(listing, OpenPullRequestsListed) else ()
    imports: list[ImportedReviewTarget] = []
    per_pr = {item.pr: item for item in facts}
    for pr in sorted(set(records) | set(listed)):
        found = records.get(pr, [])
        if len(found) > 1 and selected[pr] is None:
            continue
        record = selected[pr] if found else None
        read = reads.get(pr)
        if read is None:
            blockers.append(f"pull request #{pr} was not read")
            continue
        if isinstance(read, PullRequestUnavailable):
            blockers.append(f"pull request #{pr}: its read failed: {read.reason}")
            continue
        if read.state is not PullRequestState.OPEN:
            continue  # decision 9: a terminal pull request's files are evidence only
        try:
            guidance = parse_legacy_guidance(record.guidance) if record is not None else ""
            baseline, fresh = _baseline(record, read, review_bot)
        except _Blocked as exc:
            blockers.append(f"pull request #{pr}: {exc}")
            continue
        episode_id = imported_episode_id(repository, pr, FIRST_EPOCH, digest)
        if ceiling > 0 and isinstance(read.reviews, PartialReviews) and (
            Label.NEEDS_HUMAN_REVIEW in read.labels or trigger_label not in read.labels
        ):
            # Without the trigger, an unread escalation may be the legacy
            # dead state. Absence of NHR cannot prove that no ceiling held it.
            blockers.append(f"pull request #{pr} may be held, but its review history is only partially readable, so "
                            "whether a rejection ceiling holds it is unknown")
            continue
        held = _ceiling(read, episode_id=episode_id, review_bot=review_bot, ceiling=ceiling,
                        trigger_label=trigger_label,
                        non_resetting_commits=per_pr[pr].non_resetting_commits if pr in per_pr else ())
        if held is not None:
            provenance = per_pr[pr].hold_provenance if pr in per_pr else LegacyHoldProvenance.UNPROVEN
            if provenance is LegacyHoldProvenance.VERDICT and Label.NEEDS_HUMAN_REVIEW in read.labels:
                held = None  # the independent verdict hold remains external
            elif provenance is not LegacyHoldProvenance.CEILING:
                blockers.append(f"pull request #{pr}: held review provenance is {provenance.value}; "
                                "a rejection ceiling cannot replace an independent or unproven human-review hold")
                continue
        if record is None and held is None:
            continue  # nothing to import: the review daemon observes it as a new pull request
        imports.append(ImportedReviewTarget(
            pr, episode_id, guidance, baseline, fresh, held,
            _source(record.last_sha) if record is not None else "absent",
            _source(record.patch_id) if record is not None else "absent",
            _source(record.guidance) if record is not None else "absent",
            observation_digest(read),
        ))
    return tuple(imports), tuple(blockers)


def plan_review_activation(
    repository: str, entries: tuple[LegacyReviewEntry, ...], reads: tuple[PullRequestRead, ...],
    listing: OpenPullRequests, inventory: DeploymentInventory, *,
    ceiling: int, review_bot: str, trigger_label: Label,
    recorded: tuple[ImportedReviewTarget, ...] = (),
    facts: tuple[ReviewActivationFacts, ...] = (),
) -> ReviewActivationDecision:
    """`recorded` is the activated family's retained decisions (read only when active)."""
    repository = canonical_workflow_repository(repository)
    validate_workflow_integer(ceiling, "review ceiling")
    validate_workflow_identifier(review_bot, "review bot")
    if not isinstance(trigger_label, Label) or trigger_label is Label.NEEDS_HUMAN_REVIEW:
        raise ValueError("activation trigger must be a label other than the attention hold")
    if not isinstance(entries, tuple) or any(not isinstance(e, (LegacyReviewRecord, UnmappedLegacyEntry)) for e in entries):
        raise ValueError("activation requires typed legacy observations")
    if not isinstance(listing, (OpenPullRequestsListed, OpenPullRequestsUnavailable)):
        raise ValueError("activation requires a typed open pull request listing")
    if not isinstance(inventory, DeploymentInventory) or inventory.repository != repository:
        raise ValueError("activation inventory belongs to another repository")
    if inventory.activation is not None:
        return ReviewFamilyActive(inventory.activation, recorded)
    by_number = _reads_by_number(repository, reads)
    if not isinstance(facts, tuple) or any(not isinstance(item, ReviewActivationFacts) for item in facts):
        raise ValueError("activation requires immutable typed runtime facts")
    if len({item.pr for item in facts}) != len(facts):
        raise ValueError("activation runtime facts repeat a pull request")
    if any(item.pr not in by_number for item in facts):
        raise ValueError("activation runtime facts name an unread pull request")
    facts = tuple(sorted(facts, key=lambda item: item.pr))
    digest = source_digest(repository, entries, reads, listing, facts)
    imports, blockers = _imports(repository, entries, by_number, listing, digest, ceiling=ceiling,
                                 review_bot=review_bot, trigger_label=trigger_label, facts=facts)
    reasons = list(blockers) + list(inventory.blockers())
    if inventory.retained_targets:
        reasons.append("workflow authority is retained without an activation: "
                       + ", ".join(inventory.retained_targets[:8]))
    if reasons:
        return ActivationBlocked(repository, tuple(reasons))
    return ReviewActivationPlan(repository, FIRST_EPOCH, ceiling, review_bot, trigger_label, digest,
                                inventory.digest, imports, facts)


@dataclass(frozen=True)
class ReviewActivationCommitted:
    """`idempotent` reports that this exact plan was already the authority."""

    plan: ReviewActivationPlan
    idempotent: bool = False


# -- shadow comparison -----------------------------------------------------------------


@dataclass(frozen=True)
class ReviewShadowRow:
    """What each authority would do for one open pull request under the same read.

    Dispatch values: "review" (a paid review), "reviewed" (its head is
    covered), "held: <reasons>", "not_candidate" (no trigger label) or
    "unknown" (unread). A new-authority "review" at activation is a paid
    review activation causes; the goal is none where a baseline imports.
    """

    pr: int
    legacy_dispatch: str
    new_dispatch: str
    continuation: str
    ceiling_gate: bool
    guidance: bool

    @property
    def paid_review_on_activation(self) -> bool:
        return self.new_dispatch == "review"

    @property
    def agrees(self) -> bool:
        return self.legacy_dispatch == self.new_dispatch


def _candidacy(read: ObservedPullRequest, trigger_label: Label) -> str | None:
    holds = labels_holding_review(read.labels)
    if holds:
        return "held: " + ", ".join(holds)
    return None if trigger_label in read.labels else "not_candidate"


def _dispatch_exclusion(read: ObservedPullRequest, plan: ReviewActivationPlan) -> str | None:
    facts = next((facts for facts in plan.facts if facts.pr == read.key.target.number), None)
    if facts is None:
        return _candidacy(read, plan.trigger_label)
    return "held: " + ", ".join(facts.excluded) if facts.excluded else None


def _legacy_dispatch(read: ObservedPullRequest, record: LegacyReviewRecord | None, plan: ReviewActivationPlan) -> str:
    excluded = _dispatch_exclusion(read, plan)
    if excluded is not None:
        return excluded
    try:
        last_sha = _scalar(record.last_sha, LEGACY_LAST_SHA) if record is not None else None
    except _Blocked:
        return "unknown"
    if last_sha and last_sha == read.head_sha:
        return "reviewed"
    # The legacy retry settled on an existing verdict at the head without a paid run.
    if isinstance(read.reviews, CompleteReviews) and any(
        login_eq(r.author, plan.review_bot) and r.commit_id == read.head_sha
        and r.state in (SubmittedReviewState.APPROVED, SubmittedReviewState.CHANGES_REQUESTED)
        for r in read.reviews.reviews
    ):
        return "reviewed"
    return "review"


def _new_dispatch(read: ObservedPullRequest, snapshot: ReviewWorkflowSnapshot | None, plan: ReviewActivationPlan) -> str:
    if snapshot is not None and snapshot.gates:
        return "held: review rejection ceiling"
    excluded = _dispatch_exclusion(read, plan)
    if excluded is not None:
        return excluded
    facts = next((facts for facts in plan.facts if facts.pr == read.key.target.number), None)
    if facts is not None and facts.linked_holds:
        return "held: " + ", ".join(facts.linked_holds)
    if snapshot is None or review_owed(snapshot, read.head_sha, None):
        return "review"
    return "reviewed"


def shadow_compare(
    plan: ReviewActivationPlan, entries: tuple[LegacyReviewEntry, ...], reads: tuple[PullRequestRead, ...],
) -> tuple[ReviewShadowRow, ...]:
    """Legacy review dispatch versus the authority the plan would install, per open pull request."""
    records = {entry.pr: entry for entry in entries if isinstance(entry, LegacyReviewRecord)}
    imports = {target.pr: target for target in plan.imports}
    rows: list[ReviewShadowRow] = []
    for read in sorted((r for r in reads if isinstance(r, ObservedPullRequest)), key=lambda r: r.key.target.number):
        if read.state is not PullRequestState.OPEN:
            continue
        pr = read.key.target.number
        target = imports.get(pr)
        snapshot = imported_snapshot(plan, target) if target is not None else None
        continuation = ("fresh review required" if target is None else
                        "reviewed baseline" if target.baseline is not None else f"fresh review: {target.fresh_reason}")
        rows.append(ReviewShadowRow(
            pr, _legacy_dispatch(read, records.get(pr), plan), _new_dispatch(read, snapshot, plan),
            continuation, bool(snapshot is not None and snapshot.gates), bool(target is not None and target.guidance),
        ))
    return tuple(rows)


__all__ = [
    "LEGACY_GUIDANCE", "LEGACY_LAST_SHA", "LEGACY_PATCH_ID", "LEGACY_SUFFIXES", "MAX_LEGACY_FILE_BYTES",
    "ImportedBaselineFacts", "ImportedCeilingFacts", "ImportedReviewTarget", "LegacyReviewEntry",
    "LegacyReviewRecord", "OpenPullRequests", "OpenPullRequestsListed", "OpenPullRequestsUnavailable",
    "PullRequestRead", "ReviewActivationCommitted", "ReviewActivationDecision", "ReviewActivationFacts", "ReviewActivationPlan",
    "ReviewFamilyActive", "ReviewShadowRow", "import_evidence_id", "imported_episode_id", "imported_snapshot",
    "imports_digest", "legacy_ceiling_hold", "observation_digest", "parse_legacy_guidance", "plan_review_activation",
    "shadow_compare", "source_digest",
]
