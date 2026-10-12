"""PR linkage helpers the review role still reads (refactor slice 5b deleted
the fix decision itself; the fix role now runs on `state`/`policy`)."""

from __future__ import annotations

from .identity import IdentityMode
from .refs import is_governed_branch
from .world import PRView, issue_for_pr


def harness_merges_branch(
    head_ref_name: str, *, auto_merge: bool, identity_mode: IdentityMode,
) -> bool:
    """Whether the fixer, not an operator, merges a PR on this branch.

    Merge ONLY our own work: the reviewer adopts unlabelled PRs, and adoption
    widens what we REVIEW, never what we MERGE. Keyed on the branch, which only
    the fixer creates, not on closing references a stranger can write.
    """
    return (
        auto_merge
        and identity_mode == IdentityMode.THREE
        and is_governed_branch(head_ref_name)
    )


def _issue_for_pr(pr: PRView) -> int | None:
    """Linked issue: the shared linkage rule (closing-ref, then branch number)."""
    return issue_for_pr(pr)


def breaker_key(pr: PRView) -> int:
    """Stable per-PR key: the linked issue number, or the NEGATED PR number
    for a PR with no resolvable issue, so it cannot collide with issue N."""
    n = _issue_for_pr(pr)
    return n if n is not None else -pr.number
