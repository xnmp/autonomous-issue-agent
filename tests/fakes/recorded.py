"""Worlds whose open PRs the harness opened.

Most derive tests are about what happens to a harness PR, not whether it is
one: `derive` here records every open `fix/<issue>-` PR of the world as the
act layer would have on opening it (ADR 0018). The ownership rule itself is
tested against the real `derive` in test_pr_ownership.py.
"""

from __future__ import annotations

from dataclasses import replace

from issue_agent.domain.refs import harness_branch_issue
from issue_agent.domain.state import Harness, Local
from issue_agent.domain.state import derive as derive_owned
from issue_agent.domain.world import WorldState


def opened(world: WorldState, local: Local) -> Local:
    branches = {p.head_ref_name for p in world.prs if p.state == "OPEN" and harness_branch_issue(p.head_ref_name)}
    return replace(local, owned=local.owned | frozenset(branches))


def derive(world: WorldState, local: Local, harness: Harness):
    return derive_owned(world, opened(world, local), harness)


def record_opened(control, repository: str, prs) -> None:
    """Record each open `fix/<issue>-` PR of `prs` in `control` as the act
    layer does before opening one (`adapters.act.GitHubAct.open_pr`)."""
    for p in prs:
        if p.state == "OPEN" and harness_branch_issue(p.head_ref_name):
            key = f"fix-pr:{repository}:{p.head_ref_name}"
            control.claim_key(key)
            control.confirm_key(key)
