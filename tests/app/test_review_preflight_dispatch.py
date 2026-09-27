"""The real dispatch claim spelling reaches unpaid review execution preflight."""

from dataclasses import replace

import pytest

from issue_agent.app.roles import DispatchPathReservations
from issue_agent.domain.actions import AgentKind, RunAgent
from tests.adapters.test_review_execution_control import OWNER, assert_unpaid, current, rig


@pytest.mark.parametrize("kind", [AgentKind.REVIEW, AgentKind.RE_REVIEW])
def test_dispatch_path_reservation_authorizes_its_exact_unpaid_review_round(rig, kind):
    store, snapshot, _, selection, control = rig
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    reservations = DispatchPathReservations(store, repository="o/r", role="review", owner_id=OWNER)
    assert reservations.reserve(RunAgent(kind, 9), -9, frozenset())
    claim = store.active_dispatch_claims("o/r", "review")[0]
    plan = control.preflight(replace(selection, claim_occurrence_id=claim.claim_key))
    assert plan.round.sequence == 1 and plan.round.binding == selection.binding
    after = current(store, selection)
    assert after.revision.version == snapshot.revision.version + 1
    assert after.execution.last_sequence == 1 and after.execution.state == snapshot.execution.state
    assert_unpaid(store)
