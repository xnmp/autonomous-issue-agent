"""Branch-name ↔ issue-number parsing.

The harness derives the issue number from the branch name when GitHub's
closing-reference graph is unavailable (it's only populated for PRs against the
DEFAULT branch; ours target `dev`). Keep the prefix set in sync with
check-branch-name.sh, agent/lib.sh (pr_for_issue / issue_from_branch), and
resume-issue.sh — they must all agree.
"""

from __future__ import annotations

import re

BRANCH_PREFIXES = ("feat", "fix", "chore", "refactor")

_BRANCH_RE = re.compile(r"^(feat|fix|chore|refactor)/(\d+)-")


def issue_from_branch(branch: str) -> int | None:
    """Issue number parsed from a feat|fix|chore|refactor/<n>-… branch, or None.
    Port of agent/lib.sh:issue_from_branch."""
    m = _BRANCH_RE.match(branch or "")
    return int(m.group(2)) if m else None


def is_governed_branch(branch: str) -> bool:
    """True iff the branch carries harness work (a governed prefix). Mirrors the
    case guard in check-push.sh / check-branch-name.sh."""
    return bool(_BRANCH_RE.match(branch or "")) or any(
        (branch or "").startswith(p + "/") for p in BRANCH_PREFIXES
    )


def slugify(title: str, maxlen: int = 40) -> str:
    """lowercase, spaces→-, keep [a-z0-9-], truncate. Port of the resolve-issue.sh
    slug pipeline (used to pre-compute the worktree branch name)."""
    s = title.lower().replace(" ", "-")
    s = re.sub(r"[^a-z0-9-]", "", s)
    return s[:maxlen]
