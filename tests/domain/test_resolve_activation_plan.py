"""Pure resolve-family activation planning and shadow comparison; ADR 0015."""

import hashlib
from dataclasses import replace

import pytest

from issue_agent.domain.resolve_activation import (
    MAX_LEGACY_RETRIES, ActivationBlocked, ActiveWithDifferences, AlreadyActive, DeploymentInventory,
    ExistingActivation, ImportedResolveTarget, LegacyAbsent, LegacyPresent,
    LegacyResolveRecord, LegacyUnreadable, ResolveActivationPlan, UnmappedLegacyEntry,
    imported_workflow, plan_resolve_activation, shadow_compare, source_digest,
)
from issue_agent.domain.workflow import MAX_DETAIL_CHARS, OpenGate, ResolveCeiling

REPO = "o/r"


def present(text: str) -> LegacyPresent:
    return LegacyPresent(hashlib.sha256(text.encode()).hexdigest(), text)


def record(issue=7, retries: object = "2", last_error: object = None) -> LegacyResolveRecord:
    def file(value):
        if value is None:
            return LegacyAbsent()
        return value if isinstance(value, (LegacyPresent, LegacyUnreadable, LegacyAbsent)) else present(value)
    return LegacyResolveRecord(issue, file(retries), file(last_error))


def quiet(**changes) -> DeploymentInventory:
    return replace(DeploymentInventory(REPO), **changes)


def plan(records, *, ceiling=3, inventory=None):
    return plan_resolve_activation(REPO, tuple(records), inventory or quiet(), ceiling=ceiling)


def blocked_reasons(result) -> str:
    assert isinstance(result, ActivationBlocked), result
    return "\n".join(result.reasons)


def test_valid_sources_produce_one_deterministic_plan():
    records = (record(7, "2", "tests failed\n  twice"), record(9, "3"))
    first = plan(records)
    assert isinstance(first, ResolveActivationPlan)
    assert first == plan(tuple(reversed(records)))
    assert first.epoch == 1 and first.ceiling == 3 and first.repository == REPO
    assert [(t.issue, t.failures, t.last_failure, t.held) for t in first.imports] == [
        (7, 2, "tests failed twice", False), (9, 3, "", True),
    ]
    assert all(t.episode_id.startswith("resolve-imported:") for t in first.imports)
    assert len({t.episode_id for t in first.imports}) == 2


def test_episode_identity_is_bound_to_repository_and_sources():
    base = plan((record(7, "2"),))
    changed = plan((record(7, "1"),))
    other_repo = plan_resolve_activation("o/other", (record(7, "2"),), DeploymentInventory("o/other"), ceiling=3)
    assert isinstance(base, ResolveActivationPlan) and isinstance(changed, ResolveActivationPlan)
    assert isinstance(other_repo, ResolveActivationPlan)
    assert base.source_digest != changed.source_digest
    assert base.imports[0].episode_id != changed.imports[0].episode_id
    assert base.imports[0].episode_id != other_repo.imports[0].episode_id


@pytest.mark.parametrize("raw", [
    "", "-1", " 3", "3 ", "3\n\n", "3\r\n", "1.5", "0x3", "+3", "３", "�",
    "99999999999", "9" * 5000, str(MAX_LEGACY_RETRIES + 1),
])
def test_malformed_or_out_of_range_counter_blocks_that_import(raw):
    reasons = blocked_reasons(plan((record(7, raw), record(8, "1"))))
    assert "issue #7" in reasons and "issue #8" not in reasons


@pytest.mark.parametrize("raw,expected", [("0", 0), ("3\n", 3), ("007", 7), (str(MAX_LEGACY_RETRIES), MAX_LEGACY_RETRIES)])
def test_counter_accepts_exact_retired_shape_and_boundary(raw, expected):
    result = plan((record(7, raw),))
    assert isinstance(result, ResolveActivationPlan)
    assert result.imports[0].failures == expected


def test_unreadable_sources_block_and_are_not_absent():
    for file in ("retries", "last_error"):
        broken = replace(record(7, "1", "x"), **{file: LegacyUnreadable("not valid UTF-8")})
        assert "unreadable" in blocked_reasons(plan((broken,)))


def test_last_error_without_retries_imports_zero_failures_with_its_text():
    result = plan((record(7, None, "  config\tmissing \n"),))
    assert isinstance(result, ResolveActivationPlan)
    assert (result.imports[0].failures, result.imports[0].last_failure, result.imports[0].held) == (0, "config missing", False)


def test_both_absent_imports_nothing():
    result = plan((record(7, None, None),))
    assert isinstance(result, ResolveActivationPlan) and result.imports == ()


def test_empty_sources_are_a_valid_rowless_activation():
    result = plan(())
    assert isinstance(result, ResolveActivationPlan) and result.imports == ()


def test_oversized_or_nul_last_error_blocks():
    assert "exceeds" in blocked_reasons(plan((record(7, "1", "x" * (MAX_DETAIL_CHARS + 1)),)))
    assert "NUL" in blocked_reasons(plan((record(7, "1", "bad\x00byte"),)))
    exact = plan((record(7, "1", "x" * MAX_DETAIL_CHARS),))
    assert isinstance(exact, ResolveActivationPlan)


def test_duplicate_and_unmapped_keys_block_without_choosing_a_default():
    duplicate = blocked_reasons(plan((record(7, "1"), record(7, "5"), record(8, "1"))))
    assert "issue #7: ambiguous duplicate" in duplicate
    unmapped = blocked_reasons(plan((record(8, "1"), UnmappedLegacyEntry("issue-07.resolve-retries", "not canonical"))))
    assert "issue-07.resolve-retries" in unmapped


@pytest.mark.parametrize("ceiling,failures,held", [(0, 50, False), (3, 2, False), (3, 3, True), (3, 9, True), (1, 0, False)])
def test_ceiling_boundaries_hold_exactly_the_exhausted_episodes(ceiling, failures, held):
    result = plan((record(7, str(failures)),), ceiling=ceiling)
    assert isinstance(result, ResolveActivationPlan)
    assert result.imports[0].held is held
    workflow = imported_workflow(result, result.imports[0])
    assert bool(workflow.gates) is held
    if held:
        gate = workflow.gates[0]
        assert isinstance(gate, OpenGate) and isinstance(gate.cause, ResolveCeiling)
        assert gate.cause.failures == failures and gate.cause.ceiling == ceiling


@pytest.mark.parametrize("field", ["active_runs", "leases", "dispatch_claims", "open_commands", "active_reservations"])
def test_non_quiescent_work_blocks_activation(field):
    inventory = quiet(**{field: ("work-1",)})
    assert not inventory.quiescent
    assert "work-1" in blocked_reasons(plan((record(),), inventory=inventory))


def test_huge_inventory_is_bounded_in_the_report():
    inventory = quiet(active_runs=tuple(sorted(f"run-{i:06d}" for i in range(10000))))
    reasons = blocked_reasons(plan((), inventory=inventory))
    assert "+9992 more" in reasons and len(reasons) < 1000


def test_inventory_digest_reflects_work_but_not_activation_evidence():
    base = quiet()
    assert base.digest != quiet(leases=("run-1",)).digest
    activation = ExistingActivation(1, "a" * 64, "b" * 64, "c" * 64, 3, "d" * 64)
    assert base.digest == quiet(activation=activation, retained_targets=("issue:7",)).digest


def test_retained_authority_without_activation_blocks():
    assert "retained" in blocked_reasons(plan((), inventory=quiet(retained_targets=("issue:7",))))


def test_existing_activation_is_recognised_by_evidence_or_diagnosed_precisely():
    candidate = plan((record(7, "3"),))
    assert isinstance(candidate, ResolveActivationPlan)
    existing = ExistingActivation(1, candidate.source_digest, candidate.inventory_digest, candidate.imports_digest, 3,
                                  candidate.plan_digest)
    after = quiet(activation=existing, retained_targets=("issue:7",))
    assert plan((record(7, "3"),), inventory=after) == AlreadyActive(candidate)
    busy = quiet(activation=existing, active_runs=("run-after",), retained_targets=("issue:7",))
    assert plan((record(7, "3"),), inventory=busy) == AlreadyActive(candidate)
    sources = plan((record(7, "2"),), inventory=busy)
    assert isinstance(sources, ActiveWithDifferences)
    assert "legacy sources changed since activation" in sources.reasons[0]
    ceiling = plan((record(7, "3"),), ceiling=4, inventory=after)
    assert isinstance(ceiling, ActiveWithDifferences)
    assert "configured resolve ceiling 4 differs from the ceiling 3" in ceiling.reasons[0]
    corrupt_now = plan((record(7, "oops"),), inventory=after)
    assert isinstance(corrupt_now, ActiveWithDifferences)


def test_plan_digest_binds_ceiling_and_hold_decisions_not_only_sources():
    held = plan((record(7, "3"),), ceiling=3)
    unbounded = plan((record(7, "3"),), ceiling=0)
    assert isinstance(held, ResolveActivationPlan) and isinstance(unbounded, ResolveActivationPlan)
    assert held.source_digest == unbounded.source_digest
    assert held.plan_digest != unbounded.plan_digest
    busy = plan((record(7, "3"),), inventory=quiet(leases=("x",)))
    assert isinstance(busy, ActivationBlocked)


def test_same_issue_in_two_roots_is_ambiguous_and_roots_bind_the_digest():
    fix = record(7, "3")
    review = LegacyResolveRecord(7, LegacyAbsent(), present("elsewhere"), "review")
    assert "ambiguous duplicate legacy records in fix, review" in blocked_reasons(plan((fix, review)))
    moved = LegacyResolveRecord(7, fix.retries, fix.last_error, "extra:/old")
    assert source_digest(REPO, (fix,)) != source_digest(REPO, (moved,))


def test_source_digest_distinguishes_absent_unreadable_and_present():
    variants = [
        source_digest(REPO, (record(7, None, "x"),)),
        source_digest(REPO, (record(7, LegacyUnreadable("symbolic link"), "x"),)),
        source_digest(REPO, (record(7, "0", "x"),)),
        source_digest(REPO, (UnmappedLegacyEntry("issue-0.last-error", "not canonical"),)),
    ]
    assert len(set(variants)) == len(variants)


def test_shadow_rows_agree_with_legacy_semantics_through_the_real_policy():
    records = (record(7, "3", "boom"), record(8, "1"), record(9, None, "only text"))
    result = plan(records)
    assert isinstance(result, ResolveActivationPlan)
    rows = {row.issue: row for row in shadow_compare(result, records, 3)}
    assert all(row.agrees for row in rows.values())
    assert (rows[7].legacy_exhausted, rows[7].new_held, rows[7].legacy_remaining, rows[7].new_eligible) == (True, True, 0, False)
    assert (rows[8].legacy_remaining, rows[8].new_remaining, rows[8].new_eligible) == (2, 2, True)
    assert (rows[9].legacy_failures, rows[9].new_failures) == (0, 0)
    unbounded = plan(records, ceiling=0)
    assert isinstance(unbounded, ResolveActivationPlan)
    assert all(row.agrees and row.new_remaining is None for row in shadow_compare(unbounded, records, 0))


def test_shadow_detects_a_plan_that_would_change_eligibility():
    records = (record(7, "3"),)
    result = plan(records)
    assert isinstance(result, ResolveActivationPlan)
    tampered = replace(result, imports=(replace(result.imports[0], failures=1, held=False),))
    (row,) = shadow_compare(tampered, records, 3)
    assert not row.agrees and row.legacy_exhausted and row.new_eligible


def test_import_decision_round_trips_only_in_canonical_form():
    result = plan((record(7, "3", "boom"),))
    assert isinstance(result, ResolveActivationPlan)
    decision = result.imports[0]
    assert ImportedResolveTarget.parse(decision.canonical()) == decision
    for raw in (decision.canonical().replace(":", ": ", 1), "{}", "[]", '{"issue": true}',
                decision.canonical().replace('"held":true', '"held":false')):
        with pytest.raises((ValueError, TypeError)):
            ImportedResolveTarget.parse(raw)


def test_largest_non_ascii_failure_text_survives_its_canonical_round_trip():
    result = plan((record(7, "3", "\U0001f525" * MAX_DETAIL_CHARS),))
    assert isinstance(result, ResolveActivationPlan)
    assert ImportedResolveTarget.parse(result.imports[0].canonical()) == result.imports[0]


def test_contradictory_values_are_unrepresentable():
    with pytest.raises(ValueError):
        LegacyPresent("not-a-digest", "3")
    with pytest.raises(ValueError):
        LegacyResolveRecord(0, LegacyAbsent(), LegacyAbsent())
    with pytest.raises(ValueError):
        DeploymentInventory(REPO, active_runs=("b", "a"))
    with pytest.raises(ValueError):
        DeploymentInventory("O/R")
    with pytest.raises(ValueError):
        ActivationBlocked(REPO, ())
    with pytest.raises(ValueError):
        plan_resolve_activation(REPO, (), DeploymentInventory("o/other"), ceiling=3)
    with pytest.raises(ValueError):
        plan_resolve_activation(REPO, (), quiet(), ceiling=-1)
