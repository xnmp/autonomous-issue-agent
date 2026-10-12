"""Branch-name ↔ issue-number parsing.

The harness derives the issue number from the branch name when GitHub's
closing-reference graph is unavailable (it's only populated for PRs against the
DEFAULT branch; ours target `dev`). Python consumers use this policy directly;
the shell policy is generated from it and checked by make test.
"""

from __future__ import annotations

import re

BRANCH_PREFIXES = ("feat", "fix", "chore", "refactor", "test", "docs", "build", "ci", "perf", "revert", "style")

BRANCH_PREFIX_PATTERN = "(" + "|".join(BRANCH_PREFIXES) + ")"
ISSUE_BRANCH_PATTERN = "^" + BRANCH_PREFIX_PATTERN + r"/([0-9]+)-"
_BRANCH_RE = re.compile(ISSUE_BRANCH_PATTERN)


def issue_from_branch(branch: str) -> int | None:
    """Issue number from a supported type/<n>-… branch, or None."""
    m = _BRANCH_RE.match(branch or "")
    return int(m.group(2)) if m else None


_HARNESS_BRANCH_RE = re.compile(r"^fix/([1-9][0-9]{0,9})-")


def harness_branch_issue(branch: str) -> int | None:
    """The issue a harness PR works on, or None if the branch is not the
    harness's. Harness PR identity is the `fix/<issue>-` head branch, not the
    author: FIX_BOT can be the operator's own login (refactor decision Q1)."""
    m = _HARNESS_BRANCH_RE.match(branch) if isinstance(branch, str) else None
    return int(m.group(1)) if m else None


def is_governed_branch(branch: str) -> bool:
    """True iff the branch carries harness work (a governed prefix)."""
    return bool(_BRANCH_RE.match(branch or "")) or any(
        (branch or "").startswith(p + "/") for p in BRANCH_PREFIXES
    )


def slugify(title: str, maxlen: int = 40) -> str:
    """lowercase, spaces→-, keep [a-z0-9-], truncate. Port of the resolve-issue.sh
    slug pipeline (used to pre-compute the worktree branch name)."""
    s = title.lower().replace(" ", "-")
    s = re.sub(r"[^a-z0-9-]", "", s)
    return s[:maxlen]
