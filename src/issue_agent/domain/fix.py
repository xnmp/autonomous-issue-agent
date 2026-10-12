"""PR linkage helpers the review role still reads (refactor slice 5b deleted
the fix decision itself; the fix role now runs on `state`/`policy`)."""

from __future__ import annotations

from .identity import IdentityMode
from .refs import is_governed_branch


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


