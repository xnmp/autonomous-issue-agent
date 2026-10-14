"""Executable registry of accepted security residuals.

Every test states the desired closed behavior and is a strict expected failure
while the matching entry remains accepted in ``docs/open/residuals.md``. A fix
therefore becomes an XPASS and fails the suite until code and registry move
together.

CRED-HOME (a leg reading the operator's HOME) closed with the leg sandbox
(#413); `tests/adapters/test_leg_sandbox.py` asserts it on a real sandbox.
"""

from __future__ import annotations

import pytest

from issue_agent.domain.sandbox import Access, GitLink, LegFacts, leg_mount_plan


@pytest.mark.xfail(strict=True, reason="GIT-OBJECTS: a fix leg writes the object store review checkouts read")
def test_a_fix_leg_cannot_rewrite_objects_a_review_checkout_reads() -> None:
    common = "/r/target/.git"
    plan = leg_mount_plan(LegFacts(home="/h", cwd="/r/target/.claude/worktrees/issue-5", writable=True,
                                   git=GitLink(common, f"{common}/worktrees/issue-5"), runtime_binary="/usr/bin/x"))

    assert next(m.access for m in plan.mounts if m.path == f"{common}/objects") is not Access.READ_WRITE
