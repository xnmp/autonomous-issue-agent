"""Reviewer-granted gate exemptions (harness #124, generalized; ADR 0014).

The publish gates deny by rule. For a subset of rules — QUALITY rules, where a
reviewer's judgment is exactly the right authority — the fixer may request an
exemption instead of complying. The design splits what PR #134 conflated:

* a DECLARATION is a request authored by the fixer, tracked in the change
  itself (a commit-message trailer). It makes the intent visible in the PR and
  lets the declared violation through the gate so the reviewer can SEE it.
  It authorizes nothing.
* a GRANT is a separate artifact the fixer cannot produce: a structured line
  authored by REVIEW_BOT in a review body or PR comment, bound to the rule,
  the issue, the subject, and the rebase-invariant patch identity of the diff
  it approves (`review.patch_signature`).

Enforcement sits at MERGE: `should_automerge` withholds until every
declaration carries a matching grant; a never-granted PR simply never
auto-merges and the normal rejection ceiling escalates it to a human.

WHICH rules are exemptable is a DOMAIN decision, made here, once:

* exemptable — `tests-immutable` (an existing test encodes a wrong
  expectation; the test is the bug), `required-test` (the reviewer agrees this
  change genuinely needs no new test).
* never exemptable — everything else, explicitly including the security and
  trust-boundary rules: `.harness/` state in the repo (keifu #167's gate
  retargeting), non-image evidence/, broker provenance and force-push fences,
  identity rules, and the test-EXECUTION gates (waiving "tests pass" would
  trade the recurrence guardrail for convenience — that is an operator call,
  not a review verdict). A reviewer grant is an agent-readable artifact in the
  same trust domain the fixer operates in; security fences are not negotiable
  there. A declaration naming a non-exemptable or unknown rule is INERT: no
  gate consults it, and the merge rule ignores it (the underlying violation
  was denied outright anyway).

Everything here is pure. Collecting grant text from GitHub, authenticating the
author, and computing the current patch signature are adapter/app work.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass
from typing import final

from .identity import IdentityMode, login_eq

# Stable rule ids. These appear verbatim in trailers, grant lines, and gate
# messages — changing one orphans every declaration in flight.
RULE_TESTS_IMMUTABLE = "tests-immutable"
RULE_REQUIRED_TEST = "required-test"

# The whole-change subject for rules that are not per-file.
SUBJECT_WHOLE_CHANGE = "*"

# rule id → maximum DISTINCT subjects one PR may declare for it. The bound is
# about scope, not authorization — a change rewriting four existing test files
# is a rewrite, not a correction (from #134, the one part of it that
# survives). Membership here IS the exemptable classification.
EXEMPTABLE_RULES: dict[str, int] = {
    RULE_TESTS_IMMUTABLE: 3,
    RULE_REQUIRED_TEST: 1,
}

# Kept under its historical name — the tests-immutable cap predates the
# generalization and the shell twin names it.
MAX_TEST_IMMUTABILITY_EXCEPTIONS = EXEMPTABLE_RULES[RULE_TESTS_IMMUTABLE]

# Total declarations one change may carry, across all rules. Declarations are
# fixer-authored and each one becomes a line in the reviewer's TRUSTED prompt
# tail plus an enum member in the review schema — unbounded count is a paid
# inflation surface (round-3 adversarial review: 5000 trailers → ~280KB of
# schema per round). Anything beyond the cap is dropped at parse, which fails
# CLOSED: an undeclared violation is still denied by its gate.
MAX_DECLARATIONS_PER_CHANGE = 8


@dataclass(frozen=True)
class ExemptionDeclaration:
    """One fixer request: `rule` names the gate rule, `subject` what the
    exemption covers — a repo-relative path for per-file rules, or
    `SUBJECT_WHOLE_CHANGE` for rules that cover the change as a whole."""

    rule: str
    subject: str


@dataclass(frozen=True)
class ExemptionGrant:
    """One parsed reviewer grant, binding rule + issue + subject + patch id.

    Carrying a value of this type does NOT mean the grant is authenticated —
    `parse_exemption_grants` will parse anyone's text. Authorship filtering
    (grant authored by REVIEW_BOT, in a trust domain the fixer cannot write
    as) happens where grants are COLLECTED; see `exemptions_permit_merge`.
    """

    rule: str
    issue: int
    subject: str
    patch: str
    mac: str


# One artifact per line, the line being nothing but the artifact
# (leading/trailing whitespace allowed). `[0-9]` is deliberate where `\d`
# would be wrong: Python's `\d` matches Unicode digits and `int()` converts
# them, so `issue=١٢٣` would otherwise parse — the digit-spoofing adversarial
# case from PR #134's review. The issue field is bounded so a pathological
# digit run can't reach int()'s conversion limit; a longer run simply fails
# the match and the line is ignored (malformed ⇒ not a grant, fail closed).
_RULE_TOKEN = r"[a-z][a-z0-9-]{0,40}"
# `subject` is a bounded path-ish charset, NOT `\S+`: subjects are
# fixer-authored text that reaches the reviewer prompt's trusted tail, and an
# unbounded arbitrary token is an injection/inflation surface (adversarial
# review of ADR 0014, MAJOR 3). `*` is the whole-change subject.
_SUBJECT_TOKEN = r"(?:\*|[A-Za-z0-9._/+-]{1,200})"
_DECLARATION_RE = re.compile(
    rf"^[ \t]*Gate-Exemption:[ \t]+({_RULE_TOKEN})[ \t]+({_SUBJECT_TOKEN})[ \t]*$"
)
# Grants are stricter than declarations about WHERE the line may sit: at
# column 0, outside any code fence. A reviewer model that QUOTES the grant
# line while refusing — fenced or indented, exactly how models quote — must
# not mint the grant (adversarial review of ADR 0014, BLOCKER 1: a refusal
# body embedding the line in a ``` block parsed as authorization). Emitting
# the line unquoted at column 0 is a decision, and that is the point.
_GRANT_RE = re.compile(
    rf"^gate-exemption:[ \t]+granted"
    rf"[ \t]+rule=({_RULE_TOKEN})"
    r"[ \t]+issue=([0-9]{1,9})"
    rf"[ \t]+subject=({_SUBJECT_TOKEN})"
    r"[ \t]+patch=(\S+)"
    r"[ \t]+mac=([0-9a-f]{64})"
    r"[ \t]*$"
)
# CommonMark-faithful fence matching, not a parity toggle: a fence opened by
# N marker characters closes only on a BARE line of ≥N of the SAME character
# (a closing fence cannot carry an info string — a ```text line inside an
# open ``` block is content that OPENS nothing and CLOSES nothing, round-3
# MINOR 5), and non-matching fence-looking lines inside are content. The
# naive toggle let a 4-backtick quotation of the (itself fenced) notice
# re-open the parser, and a `~~~` line "close" a backtick fence — both
# standard markdown a quoting model produces.
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(\S?)")


def parse_exemption_declarations(
    message: object,
) -> tuple[ExemptionDeclaration, ...]:
    """Every well-formed `Gate-Exemption: <rule> <subject>` trailer line in
    one commit message, in order, deduped. Unknown/non-exemptable rules are
    dropped HERE — an inert declaration must not reach the gate or the merge
    rule at all. TOTAL: non-str input reads as no declarations."""
    if not isinstance(message, str):
        return ()
    seen: dict[ExemptionDeclaration, None] = {}
    for line in message.splitlines():
        m = _DECLARATION_RE.match(line)
        if m and m.group(1) in EXEMPTABLE_RULES:
            seen.setdefault(ExemptionDeclaration(m.group(1), m.group(2)))
            if len(seen) >= MAX_DECLARATIONS_PER_CHANGE:
                # Anything past the cap is dropped — fail closed, the
                # undeclared violation is still denied by its gate — and a
                # capped set already exceeds every per-rule scope bound, so
                # the merge side withholds regardless.
                break
    return tuple(seen)


def parse_exemption_grants(body: object) -> tuple[ExemptionGrant, ...]:
    """Every well-formed grant line in a review body, in order.

    A line inside a markdown code fence (``` or ~~~) is NEVER a grant — a
    reviewer quoting the line while refusing must not mint it. An unclosed
    fence swallows the rest of the body: fail closed toward "quoted".

    TOTAL: any non-str input reads as no grants. Malformed lines — wrong
    keyword, missing fields, out-of-order fields, non-ASCII digits in issue=,
    indentation, extra prose on the line — are ignored, never guessed at."""
    if not isinstance(body, str):
        return ()
    grants: list[ExemptionGrant] = []
    open_fence: str | None = None
    for line in body.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker, info = fence.group(1), fence.group(2)
            if open_fence is None:
                open_fence = marker
                continue
            if (
                not info
                and marker[0] == open_fence[0]
                and len(marker) >= len(open_fence)
            ):
                open_fence = None
                continue
            # Inside an open fence, a non-matching fence line — or ANY fence
            # line carrying an info string (```text) — is content.
            continue
        if open_fence is not None:
            continue
        m = _GRANT_RE.match(line)
        if m:
            grants.append(
                ExemptionGrant(
                    m.group(1),
                    int(m.group(2)),
                    m.group(3),
                    m.group(4),
                    m.group(5),
                )
            )
    return tuple(grants)


def open_fence_marker(body: object) -> str | None:
    """The marker of a code fence left OPEN at the end of `body`, or None.

    The harness appends its own grant lines after the reviewer's prose; a
    model body that ends inside an unclosed fence (a common truncation shape)
    would swallow them, and the PR would then escalate with a false "the
    reviewer did not grant" (round-3 MINOR 7). The appender uses this to
    close the fence first. Same scan as `parse_exemption_grants`, so the two
    can never disagree about what "inside a fence" means."""
    if not isinstance(body, str):
        return None
    open_fence: str | None = None
    for line in body.splitlines():
        fence = _FENCE_RE.match(line)
        if not fence:
            continue
        marker, info = fence.group(1), fence.group(2)
        if open_fence is None:
            open_fence = marker
        elif (
            not info
            and marker[0] == open_fence[0]
            and len(marker) >= len(open_fence)
        ):
            open_fence = None
    return open_fence


def grant_mac(
    key: str, rule: str, issue: int, subject: str, patch: str
) -> str:
    """The authentication tag a genuine grant line must carry.

    The patch id is NOT a secret — it is a deterministic hash of the public
    diff, computable by the fixer (round-3 adversarial review, BLOCKER 1) —
    so binding a grant to it proves freshness, not authorship. Authorship is
    this MAC: HMAC-SHA256 over the grant's identity fields with a key held
    only by the harness daemons (derived from harness credentials, never
    placed in any agent's environment or prompt). A grant line composed in
    prose — by a prompt-injected reviewer or by anyone else writing on the
    PR — cannot carry a valid tag without the key. Empty key → empty string,
    which the 64-hex grammar can never match: no key, no grants, fail
    closed."""
    if not key:
        return ""
    message = f"{rule}|{issue}|{subject}|{patch}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


_AUTHENTICATED_GRANTS_PROOF = object()


@final
class AuthenticatedGrantPairs(Collection[tuple[str, str]]):
    """Opaque MAC-verified grant pairs bound to one issue and patch."""

    __slots__ = ("_issue_number", "_patch_id", "_pairs")

    def __init__(
        self,
        issue_number: int | None,
        patch_id: str,
        pairs: frozenset[tuple[str, str]],
        proof: object,
    ) -> None:
        if proof is not _AUTHENTICATED_GRANTS_PROOF:
            raise TypeError("authenticated grants must come from MAC verification")
        object.__setattr__(self, "_issue_number", issue_number)
        object.__setattr__(self, "_patch_id", patch_id)
        object.__setattr__(self, "_pairs", pairs)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("authenticated grant proof is immutable")

    @property
    def issue_number(self) -> int | None:
        return self._issue_number

    @property
    def patch_id(self) -> str:
        return self._patch_id

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        return self._pairs

    def __contains__(self, item: object) -> bool:
        return item in self._pairs

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._pairs)

    def __len__(self) -> int:
        return len(self._pairs)


def authenticated_grant_pairs(
    grants: Iterable[ExemptionGrant],
    *,
    issue_number: int | None,
    patch_id: str,
    mac_key: str,
) -> AuthenticatedGrantPairs:
    """The declaration identities authorized for one issue and patch.

    Parsing proves only syntax. This helper applies the freshness and
    harness-authorship checks shared by merge enforcement and review
    reconciliation, so an external or recovered approval cannot be accepted
    under a weaker rule than the eventual merge.
    """
    if issue_number is None or not patch_id or not mac_key:
        pairs: frozenset[tuple[str, str]] = frozenset()
    else:
        pairs = frozenset(
            (grant.rule, grant.subject)
            for grant in grants
            if grant.issue == issue_number
            and grant.patch == patch_id
            and hmac.compare_digest(
                grant.mac,
                grant_mac(
                    mac_key,
                    grant.rule,
                    grant.issue,
                    grant.subject,
                    grant.patch,
                ),
            )
        )
    return AuthenticatedGrantPairs(
        issue_number,
        patch_id,
        pairs,
        _AUTHENTICATED_GRANTS_PROOF,
    )


def render_grant_line(
    key: str, rule: str, issue: int, subject: str, patch: str
) -> str:
    """The canonical authenticated grant line — the ONLY minting point."""
    tag = grant_mac(key, rule, issue, subject, patch)
    return (
        f"gate-exemption: granted rule={rule} issue={issue} "
        f"subject={subject} patch={patch} mac={tag}"
    )


_SUBJECT_FULL_RE = re.compile(rf"^{_SUBJECT_TOKEN}$")


def valid_subject(subject: object) -> bool:
    """Can this subject appear in a grant line? Auto-declared subjects come
    from diff paths, which are not bounded by the trailer grammar — a path
    outside the charset would render an UNPARSEABLE grant line, silently
    losing a granted exemption and escalating with a false reason. The
    renderer checks first and reports the real problem instead."""
    return isinstance(subject, str) and bool(_SUBJECT_FULL_RE.match(subject))


def count_malformed_declarations(message: object) -> int:
    """Lines that LOOK like a Gate-Exemption trailer but don't parse — bad
    charset in the subject (`@scope/pkg`, unicode), missing fields, or an
    unknown/non-exemptable rule. The parser rightly ignores them (fail
    closed), but silently: an agent that typo'd its declaration would see
    the gate deny with no hint its trailer was the problem — the #126 loop
    with extra steps. The gate surfaces this count in the denial."""
    if not isinstance(message, str):
        return 0
    malformed = 0
    for line in message.splitlines():
        if not re.match(r"^[ \t]*Gate-Exemption:", line):
            continue
        m = _DECLARATION_RE.match(line)
        if m is None or m.group(1) not in EXEMPTABLE_RULES:
            malformed += 1
    return malformed


def declared_subjects(
    declarations: Iterable[ExemptionDeclaration], rule: str
) -> tuple[str, ...]:
    """The subjects declared for ONE rule — what that rule's gate check
    consults. Order-preserving, deduped."""
    seen: dict[str, None] = {}
    for d in declarations:
        if d.rule == rule:
            seen.setdefault(d.subject)
    return tuple(seen)


def check_declaration_scope(rule: str, subjects: Collection[str]) -> bool:
    """True when `rule`'s declared subject set is within its scope bound.
    Shared by the gate (deny) and the merge rule (withhold) so the two never
    disagree about what 'too many' means. An unknown rule has no bound to be
    within — False, matching its declarations being inert."""
    cap = EXEMPTABLE_RULES.get(rule)
    return cap is not None and len(set(subjects)) <= cap


@dataclass(frozen=True)
class ExemptionFacts:
    """The per-PR facts the merge-side rule reads, assembled by the app layer.

    `declarations` — parsed from the PR's commit messages.
    `grants` — grants ALREADY AUTHENTICATED as authored by REVIEW_BOT. This is
        the ADR 0012 provenance contract: grant text is below harness-owned
        (anyone can comment on a PR), so the collector must filter by author
        identity BEFORE anything reaches this type. A grant from any other
        author — explicitly including the operator — is dropped there; a human
        override is a hand merge, outside the harness.
    `patch_id` — the CURRENT rebase-invariant patch identity of the PR's diff
        (`review.patch_signature`). '' means it could not be computed.
    `unverifiable` — the collector could not READ the facts at all (commit
        messages unavailable, or more commits than one fetch returns). Marks
        the whole record transiently unusable: merge stays withheld, but the
        condition is retried next tick, never escalated as "ungranted" —
        conflating a gh outage with a reviewer refusal escalates healthy PRs
        with a false reason (adversarial review of ADR 0014, round 2).
    """

    declarations: tuple[ExemptionDeclaration, ...] = ()
    grants: tuple[ExemptionGrant, ...] = ()
    patch_id: str = ""
    unverifiable: bool = False
    # The commit list was TRUNCATED by the fetch ceiling (≥100 commits) — a
    # DURABLE condition, unlike `unverifiable`: retrying can never clear it,
    # so it maps to WITHHELD (escalate with an accurate reason), not to
    # unverifiable-retry-forever (round-3 MAJOR 2: the old sentinel silently
    # unmerged every ≥100-commit PR, declared or not, with no escalation).
    truncated: bool = False
    # The diff carried a record whose touched path could not be resolved, so
    # the auto-declared subject set is not trustworthy. DURABLE for this head
    # (the same diff re-derives the same way), so it escalates rather than
    # retrying forever — "unreadable shape" must never merge as "touched no
    # tests" (round-4 adversarial review, BLOCKER 2).
    undecidable_diff: bool = False


# Merge-side verdicts from `exemption_status`. Distinct because they demand
# distinct handling: PERMIT merges, WITHHELD is durable for this head (a human
# decision — escalate once), UNVERIFIABLE is transient (withhold and retry;
# the collector journals the cause).
EXEMPTIONS_PERMIT = "permit"
EXEMPTIONS_WITHHELD = "withheld"
EXEMPTIONS_UNVERIFIABLE = "unverifiable"


def exemptions_permit_merge(
    facts: ExemptionFacts,
    *,
    issue_number: int | None,
    identity_mode: IdentityMode | str,
    fix_bot: str,
    review_bot: str,
    mac_key: str = "",
) -> bool:
    """May auto-merge proceed, as far as gate exemptions go?

    A PR with NO declarations is completely unaffected (True — the
    anti-vacuity twin). Otherwise every rule fails CLOSED, withholding the
    merge (never denying the PR — the rejection ceiling escalates it on the
    normal path):

    * `identity_mode` not three, or fix/review identities absent or equal →
      the trust anchor (REVIEW_BOT as a distinct identity the fixer cannot
      author as) does not exist; NO exemption is available. Never degrade to
      self-authorization.
    * any rule declaring more subjects than its scope bound → withhold
      regardless of grants.
    * unresolvable issue number or missing patch identity → unverifiable, so
      absent — a grant that cannot be checked is not a grant.
    * every declaration must have a grant matching rule AND issue AND subject
      AND the CURRENT patch id. A new push changes the patch id and voids
      prior grants, so a reviewer cannot approve a small correction that is
      then swapped for a large one.

    `facts.grants` must already be authorship-authenticated — see
    `ExemptionFacts`.
    """
    status, _ = exemption_status(
        facts,
        issue_number=issue_number,
        identity_mode=identity_mode,
        fix_bot=fix_bot,
        review_bot=review_bot,
        mac_key=mac_key,
    )
    return status == EXEMPTIONS_PERMIT


def exemption_status(
    facts: ExemptionFacts,
    *,
    issue_number: int | None,
    identity_mode: IdentityMode | str,
    fix_bot: str,
    review_bot: str,
    mac_key: str = "",
) -> tuple[str, str]:
    """(verdict, reason) for the merge-side exemption decision.

    PERMIT / WITHHELD / UNVERIFIABLE — see the constants. The reason is the
    human-facing sentence an escalation embeds; it must state what is actually
    true, so each failure names ITS cause rather than collapsing into
    "the reviewer did not grant":

    * UNVERIFIABLE — the facts could not be read (transient) or the current
      patch id could not be computed. Withhold and retry; not a human problem
      yet.
    * WITHHELD — the facts are readable and the answer is durably no for this
      head: no three-identity trust anchor, scope bound exceeded, branch
      carries no issue number, or a declaration lacks a matching grant.
    """
    if facts.unverifiable:
        return (
            EXEMPTIONS_UNVERIFIABLE,
            "its gate-exemption declarations could not be read this tick",
        )
    if facts.undecidable_diff:
        return (
            EXEMPTIONS_WITHHELD,
            "its diff contains a change record whose touched path the "
            "harness cannot resolve (an unusual rename or path encoding), "
            "so it cannot prove which existing tests the change modifies — "
            "merge by hand after checking, or simplify the change's file "
            "moves",
        )
    if facts.truncated:
        return (
            EXEMPTIONS_WITHHELD,
            "it carries more commits than the harness can verify "
            "gate-exemption declarations across (the commit fetch "
            "truncates at 100), so auto-merge cannot prove the change "
            "declares nothing — merge by hand after checking, or squash "
            "the branch",
        )
    if not facts.declarations:
        return (EXEMPTIONS_PERMIT, "")
    if identity_mode != IdentityMode.THREE:
        return (
            EXEMPTIONS_WITHHELD,
            "gate exemptions require IDENTITY_MODE=three (a reviewer "
            "identity distinct from the fixer); this deployment has none",
        )
    if not fix_bot or not review_bot or login_eq(fix_bot, review_bot):
        return (
            EXEMPTIONS_WITHHELD,
            "gate exemptions require distinct fix and review identities; "
            "this deployment's are absent or identical",
        )
    for rule in {d.rule for d in facts.declarations}:
        if not check_declaration_scope(
            rule, declared_subjects(facts.declarations, rule)
        ):
            return (
                EXEMPTIONS_WITHHELD,
                f"it declares more `{rule}` exemption subjects than the "
                f"rule's scope bound ({EXEMPTABLE_RULES[rule]}) allows",
            )
    if issue_number is None:
        return (
            EXEMPTIONS_WITHHELD,
            "its branch name carries no issue number, so a grant cannot "
            "be bound to it",
        )
    if not facts.patch_id:
        return (
            EXEMPTIONS_UNVERIFIABLE,
            "its current patch identity could not be computed this tick",
        )
    if not mac_key:
        return (
            EXEMPTIONS_WITHHELD,
            "the harness has no grant-authentication key configured, so "
            "no reviewer grant can be verified",
        )
    # A grant counts only with a valid MAC: the patch id is a public hash
    # the fixer can compute, so patch-binding alone proves freshness, not
    # that the HARNESS minted the line (round-3 BLOCKER 1). compare_digest
    # keeps the check constant-time.
    granted = authenticated_grant_pairs(
        facts.grants,
        issue_number=issue_number,
        patch_id=facts.patch_id,
        mac_key=mac_key,
    )
    ungranted = [
        d for d in facts.declarations if (d.rule, d.subject) not in granted
    ]
    if ungranted:
        listing = ", ".join(f"`{d.rule} {d.subject}`" for d in ungranted)
        return (
            EXEMPTIONS_WITHHELD,
            "it declares gate exemptions the reviewer did not grant "
            f"for the current head: {listing}",
        )
    return (EXEMPTIONS_PERMIT, "")
