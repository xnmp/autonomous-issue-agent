"""Reviewer-granted test-immutability exemption (harness #124, revised design).

The commit/push gate (`gate.check_tests_immutable`) denies any edit to a test
file that existed at base. When the existing test IS the bug (a wrong
expectation frozen in an assertion — #89, #127), the fixer needs a path that is
explicit, auditable, and narrow. The design splits what PR #134 conflated:

* a DECLARATION is a request authored by the fixer, tracked in the change
  itself. It makes the edit visible in the diff and lets it into the PR.
  It authorizes nothing.
* a GRANT is a separate artifact the fixer cannot produce: a structured line
  authored by REVIEW_BOT in a review body or PR comment, bound to the issue,
  the file, and the rebase-invariant patch identity of the diff it approves
  (`review.patch_signature`).

Enforcement moves to MERGE: the reviewer must be able to SEE the proposed edit
to judge it, so the change may exist in the PR — what must not happen is that
it LANDS unexamined. `should_automerge` withholds until every declared file
carries a matching grant; a never-granted PR simply never auto-merges and the
normal rejection ceiling escalates it to a human.

Everything here is pure. Collecting grant text from GitHub, authenticating the
author, and computing the current patch signature are adapter/app work.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from .identity import login_eq
from .review import IdentityMode

# Scope bound on one PR's exemption request (from #134, the one part of it that
# survives): more declared files than this denies at the gate AND withholds the
# merge regardless of grants. The bound is about scope, not authorization — a
# change rewriting four existing test files is a rewrite, not a correction.
MAX_TEST_IMMUTABILITY_EXCEPTIONS = 3


@dataclass(frozen=True)
class ExemptionGrant:
    """One parsed reviewer grant: issue + file + the patch identity it binds to.

    Carrying a value of this type does NOT mean the grant is authenticated —
    `parse_exemption_grants` will parse anyone's text. Authorship filtering
    (grant authored by REVIEW_BOT, in a trust domain the fixer cannot write as)
    happens where grants are COLLECTED; see `exemption_permits_merge`.
    """

    issue: int
    file: str
    patch: str


# One grant per line, the line being nothing but the grant (leading/trailing
# whitespace allowed). `[0-9]` is deliberate where `\d` would be wrong: Python's
# `\d` matches Unicode digits and `int()` converts them, so `issue=١٢٣` would
# otherwise parse — the digit-spoofing adversarial case from PR #134's review.
# The issue field is bounded so a pathological digit run can't reach int()'s
# conversion limit; a longer run simply fails the match and the line is ignored
# (malformed ⇒ not a grant, fail closed).
_GRANT_RE = re.compile(
    r"^[ \t]*test-immutability-exemption:[ \t]+granted"
    r"[ \t]+issue=([0-9]{1,9})"
    r"[ \t]+file=(\S+)"
    r"[ \t]+patch=(\S+)"
    r"[ \t]*$"
)


def parse_exemption_grants(body: object) -> tuple[ExemptionGrant, ...]:
    """Every well-formed grant line in a comment/review body, in order.

    TOTAL: any non-str input reads as no grants. Malformed lines — wrong
    keyword, missing fields, out-of-order fields, non-ASCII digits in issue=,
    extra prose on the line — are ignored, never guessed at. Multiple grants
    per body are allowed (one PR may declare up to the cap in files).
    """
    if not isinstance(body, str):
        return ()
    grants: list[ExemptionGrant] = []
    for line in body.splitlines():
        m = _GRANT_RE.match(line)
        if m:
            grants.append(ExemptionGrant(int(m.group(1)), m.group(2), m.group(3)))
    return tuple(grants)


@dataclass(frozen=True)
class ExemptionFacts:
    """The per-PR facts the merge-side rule reads, assembled by the app layer.

    `declared` — test files the change's tracked declaration names.
    `grants` — grants ALREADY AUTHENTICATED as authored by REVIEW_BOT. This is
        the ADR 0012 provenance contract: grant text is below harness-owned
        (anyone can comment on a PR), so the collector must filter by author
        identity BEFORE anything reaches this type. A grant from any other
        author — explicitly including the operator — is dropped there; a human
        override is a hand merge, outside the harness.
    `patch_id` — the CURRENT rebase-invariant patch identity of the PR's diff
        (`review.patch_signature`). '' means it could not be computed.
    """

    declared: tuple[str, ...] = ()
    grants: tuple[ExemptionGrant, ...] = ()
    patch_id: str = ""


def exemption_permits_merge(
    facts: ExemptionFacts,
    *,
    issue_number: int | None,
    identity_mode: IdentityMode | str,
    fix_bot: str,
    review_bot: str,
) -> bool:
    """May auto-merge proceed, as far as test-immutability exemptions go?

    A PR with NO declarations is completely unaffected (True — the anti-vacuity
    twin). Otherwise every rule fails CLOSED, withholding the merge (never
    denying the PR — the rejection ceiling escalates it on the normal path):

    * `identity_mode` not three, or fix/review identities absent or equal →
      the trust anchor (REVIEW_BOT as a distinct GitHub App the fixer cannot
      author as) does not exist; NO exemption is available. Never degrade to
      self-authorization.
    * more declared files than MAX_TEST_IMMUTABILITY_EXCEPTIONS → withhold
      regardless of grants (scope bound).
    * unresolvable issue number or missing patch identity → unverifiable, so
      absent — a grant that cannot be checked is not a grant.
    * every declared file must have a grant matching issue AND file AND the
      CURRENT patch id. A new push changes the patch id and voids prior grants,
      so a reviewer cannot approve a small correction that is then swapped for
      a large one.

    `facts.grants` must already be authorship-authenticated — see
    `ExemptionFacts`.
    """
    if not facts.declared:
        return True
    if identity_mode != IdentityMode.THREE:
        return False
    if not fix_bot or not review_bot or login_eq(fix_bot, review_bot):
        return False
    declared = set(facts.declared)
    if len(declared) > MAX_TEST_IMMUTABILITY_EXCEPTIONS:
        return False
    if issue_number is None or not facts.patch_id:
        return False
    granted = {
        g.file
        for g in facts.grants
        if g.issue == issue_number and g.patch == facts.patch_id
    }
    return declared <= granted


def check_declaration_scope(declared: Collection[str]) -> bool:
    """True when the declaration set is within the scope bound. Shared by the
    commit/push gate (deny) and the merge rule (withhold) so the two never
    disagree about what 'too many' means."""
    return len(set(declared)) <= MAX_TEST_IMMUTABILITY_EXCEPTIONS
