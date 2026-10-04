from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.measurement import HumanIntervention, observation_summary, parse_observation
from issue_agent.domain.cohort import AdmissionFact, CohortWindow, RunFact, cohort_readout

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def intervention(**changes):
    values = dict(kind="intervention", record_id="one", repository="o/r", actor="operator", provenance="operator recorded", occurred_at=NOW.isoformat(), category="diagnosis", source="service", description="restart diagnosis")
    values.update(changes)
    result = parse_observation(values)
    assert isinstance(result, HumanIntervention)
    return result


@pytest.mark.parametrize("changes", [{"duration_seconds": True}, {"interruptions": -1}, {"duration_seconds": 10**13}, {"actor": None}, {"description": "x" * 2049}, {"category": "unknown"}, {"occurred_at": "2026-10-04"}, {"extra": 1}])
def test_invalid_external_observation_is_rejected(changes):
    with pytest.raises(ValueError):
        intervention(**changes)


def test_trusted_constructor_rejects_invalid_values():
    with pytest.raises(ValueError):
        replace(intervention(), duration_seconds=True)
    with pytest.raises(ValueError):
        replace(intervention(), occurred_at=NOW.replace(tzinfo=None))


def test_unknown_human_effort_and_external_actions_are_visible():
    result = observation_summary((intervention(), intervention(record_id="two", repository="a/b")))
    assert result["interventions_observed"] == 2
    assert result["duration_unknown"] == 2
    assert result["complete_duration_seconds"] is None
    assert result["human_coverage"] == "observed_floor"


def test_reference_chain_annotates_original_once_and_preserves_measurement():
    initial = intervention(event_ref="dashboard-event")
    annotation = intervention(record_id="two", original_record_id="one", duration_seconds=40, interruptions=1)
    correction = intervention(record_id="three", original_record_id="two", duration_seconds=30, interruptions=2)
    result = observation_summary((initial, annotation, correction, initial))
    assert result["interventions_observed"] == 1
    assert result["measured_duration_seconds"] == 30
    assert result["complete_interruptions"] == 2


def test_unresolved_and_cyclic_refs_remain_visible():
    result = observation_summary((intervention(original_record_id="missing"), intervention(record_id="a", original_record_id="b"), intervention(record_id="b", original_record_id="a")))
    assert result["interventions_observed"] == 0
    assert len(result["reference_gaps"]) == 3
    assert result["complete_duration_seconds"] is None


def test_cohort_deduplicates_admitted_bindings_and_conserves_shared_charge():
    window = CohortWindow("o/r", NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    facts = (AdmissionFact("r1", "resolve:a", "resolve", NOW), AdmissionFact("r1", "recovery:a", "issue_recovery", NOW), AdmissionFact("r2", "resolve:a", "resolve", NOW + timedelta(minutes=90)), AdmissionFact("old", "old", "resolve", NOW - timedelta(days=1)))
    runs = (RunFact("r1", "v1", "o/r:fix", "RESOLVE", "abandoned", ("USD", "rate_card", 12)), RunFact("r2", "v1", "o/r:fix", "RESOLVE", "completed", ("USD", "rate_card", 8)), RunFact("unassigned", "v1", "o/r:fix", "RESOLVE", "completed", ("USD", "provider", 3)))
    result = cohort_readout(window, facts, runs, ())
    assert result["admitted_bindings_by_family"] == {"resolve": 1, "issue_recovery": 1}
    assert result["spend_micros_by_currency_basis"] == {"USD:rate_card": 20}
    assert result["shared_binding_spend_micros"] == {"USD:rate_card": 12}
    assert result["unassigned_spend_micros"] == {"USD:provider": 3}
    assert result["outcomes"]["cost_per_merge"] is None
    inherited = cohort_readout(replace(window, selection="include_inherited", inherited_binding_refs=(__import__("hashlib").sha256(b"old").hexdigest(),)), facts, runs, ())
    assert inherited["admitted_bindings_by_family"]["resolve"] == 2


def measurement(identity, run_id, record_id, **changes):
    from issue_agent.domain.cohort import binding_reference
    raw = dict(kind="workflow_measurement", record_id=record_id, repository="o/r", actor="operator", provenance="observed remote outcomes", occurred_at=(NOW + timedelta(hours=1)).isoformat(), run_id=run_id, binding_ref=binding_reference(identity), evidence_refs=["verified local fixture"])
    raw.update(changes)
    return parse_observation(raw)


def profile():
    return dict(deployment="test", runtime="codex", model="gpt", posture="trusted inbound", difficulty="routine", config_version="v1")


def covered_window():
    expected = profile()
    expected.pop("difficulty")
    return CohortWindow("o/r", NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2), expected_profile=tuple(sorted(expected.items())))


def coverage(window, **changes):
    from issue_agent.domain.cohort import cohort_fingerprint
    raw = dict(kind="cohort_coverage", record_id="coverage", repository="o/r", actor="operator", provenance="operator completeness report", occurred_at=window.observation_cutoff.isoformat(), cohort_fingerprint=cohort_fingerprint(window), evidence_refs=["complete external journal"], human_complete=True, outcomes_complete=True, first_review_complete=True, recurrence_complete=True, recurrence_horizon_seconds=60)
    raw.update(changes)
    return parse_observation(raw)


def test_prospective_cohort_deduplicates_real_merges_and_includes_unmerged_first_review():
    window = covered_window()
    facts = tuple(AdmissionFact(run_id, identity, "resolve", NOW) for run_id, identity in (("r1", "first"), ("r2", "second"), ("r3", "unmerged")))
    runs = tuple(RunFact(run_id, "v1", "o/r:fix", "RESOLVE", "completed", ("USD", "rate_card", 10), "codex", "gpt") for run_id in ("r1", "r2", "r3"))
    merged = dict(state="merged", pr_number=44, merged_at=(NOW + timedelta(minutes=30)).isoformat())
    records = (measurement("first", "r1", "m1", profile=profile(), outcome=merged, first_pass_approved=True, recurrence_observed=False), measurement("second", "r2", "m2", profile=profile(), outcome=merged, first_pass_approved=True, recurrence_observed=False), measurement("unmerged", "r3", "m3", profile=profile(), outcome=dict(state="abandoned", pr_number=45), first_pass_approved=False), coverage(window))
    result = cohort_readout(window, facts, runs, records)
    assert result["comparability"] == "eligible_within_declared_profile"
    assert result["primary_admitted_bindings"] == 3
    assert result["outcomes"]["merged"] == 1
    assert result["first_pass_reviewed_prs"] == 2
    assert result["first_pass_approved_prs"] == 1
    assert result["outcomes"]["first_pass_approval"] == .5
    assert result["outcomes"]["recurrence"] == 0
    assert result["outcomes"]["primary_family_spend_per_distinct_merge_micros_by_currency_basis"] == {"USD:rate_card": 30}


def test_profile_mismatch_missing_coverage_and_conflicting_merge_are_not_confident_rates():
    window = covered_window()
    facts = (AdmissionFact("r1", "first", "resolve", NOW), AdmissionFact("r2", "second", "resolve", NOW))
    merged = dict(state="merged", pr_number=44, merged_at=NOW.isoformat())
    records = (measurement("first", "r1", "one", profile=profile(), outcome=merged), measurement("second", "r2", "two", profile=dict(profile(), runtime="claude"), outcome=dict(merged, merged_at=(NOW + timedelta(minutes=1)).isoformat())))
    result = cohort_readout(window, facts, (), records)
    assert result["comparability"] == "indeterminate"
    assert result["outcomes"]["merged"] is None
    assert result["annotation_gaps"]
    assert result["coverage_gaps"]


@pytest.mark.parametrize("outcome", [dict(state="merged"), dict(state="open", merged_at=NOW.isoformat()), dict(state="merged", merged_at=NOW.isoformat(), pr_number=True)])
def test_outcome_variant_rejects_contradictory_or_missing_target(outcome):
    with pytest.raises(ValueError):
        measurement("first", "r1", "one", outcome=outcome)


def test_first_review_requires_pr_and_complete_recurrence_requires_horizon():
    with pytest.raises(ValueError):
        measurement("first", "r1", "one", first_pass_approved=True)
    with pytest.raises(ValueError):
        coverage(covered_window(), recurrence_horizon_seconds=None)


def test_cohort_fingerprint_changes_for_cutoff_primary_family_and_profile():
    from issue_agent.domain.cohort import cohort_fingerprint
    window = covered_window()
    original = cohort_fingerprint(window)
    assert cohort_fingerprint(replace(window, primary_family="review_execution")) != original
    assert cohort_fingerprint(replace(window, observation_cutoff=window.observation_cutoff + timedelta(seconds=1))) != original
    assert cohort_fingerprint(replace(window, expected_profile=())) != original


def test_incomplete_outcomes_prevent_complete_recurrence_and_missing_charge_prevents_cost_ratio():
    window = covered_window()
    facts = (AdmissionFact("r1", "first", "resolve", NOW), AdmissionFact("r2", "unknown", "resolve", NOW))
    records = (measurement("first", "r1", "one", outcome=dict(state="merged", pr_number=44, merged_at=NOW.isoformat()), recurrence_observed=False), coverage(window, outcomes_complete=False))
    result = cohort_readout(window, facts, (), records)
    assert result["outcomes"]["recurrence"] is None
    single = cohort_readout(window, facts[:1], (RunFact("r1", "v1", "o/r:fix", "RESOLVE", "completed", None),), (records[0], coverage(window)))
    assert single["outcomes"]["merged"] == 1
    assert single["outcomes"]["primary_family_spend_per_distinct_merge_micros_by_currency_basis"] is None
    assert not single["coverage"]["accounting_complete"]


def test_known_unmerged_pr_missing_first_review_overrides_claimed_complete_coverage():
    window = covered_window()
    facts = (AdmissionFact("r1", "first", "resolve", NOW), AdmissionFact("r2", "unmerged", "resolve", NOW))
    reviewed = measurement("first", "r1", "one", outcome=dict(state="merged", pr_number=44, merged_at=NOW.isoformat()), first_pass_approved=True)
    unknown = measurement("unmerged", "r2", "two", outcome=dict(state="open", pr_number=45))
    result = cohort_readout(window, facts, (), (reviewed, unknown, coverage(window)))
    assert result["outcomes"]["first_pass_approval"] is None
    assert result["coverage"]["known_prs_missing_first_review"] == [45]
    unreviewed = measurement("unmerged", "r2", "two", outcome=dict(state="open", pr_number=45), first_review_status="not_reviewed")
    complete = cohort_readout(window, facts, (), (reviewed, unreviewed, coverage(window)))
    assert complete["outcomes"]["first_pass_approval"] == 1


def test_malformed_state_is_a_boundary_value_error():
    with pytest.raises(ValueError):
        measurement("first", "r1", "one", outcome=dict(state=[]))


def test_inherited_selection_is_explicit_and_missing_ref_is_visible():
    from issue_agent.domain.cohort import binding_reference
    window = replace(covered_window(), selection="include_inherited", inherited_binding_refs=(binding_reference("old"), binding_reference("missing")))
    facts = (AdmissionFact("old", "old", "resolve", NOW - timedelta(days=1)), AdmissionFact("past", "past", "resolve", NOW - timedelta(days=2)), AdmissionFact("new", "new", "resolve", NOW))
    result = cohort_readout(window, facts, (), ())
    assert result["primary_admitted_bindings"] == 2
    assert any("requested inherited binding absent" in gap for gap in result["annotation_gaps"])


def test_held_duration_requires_observed_start_and_never_invents_zero():
    window = covered_window()
    facts = (AdmissionFact("r1", "first", "resolve", NOW),)
    held = measurement("first", "r1", "one", outcome=dict(state="held", held_since=NOW.isoformat()))
    assert cohort_readout(window, facts, (), (held,))["bindings"][0]["held_duration_seconds"] == 7200
    unknown = measurement("first", "r1", "one", outcome=dict(state="held"))
    assert cohort_readout(window, facts, (), (unknown,))["bindings"][0]["held_duration_seconds"] is None
    with pytest.raises(ValueError):
        measurement("first", "r1", "one", outcome=dict(state="held", held_since=(NOW + timedelta(days=1)).isoformat()))


def test_audit_catches_do_not_become_escaped_incidents_and_actions_deduplicate():
    from issue_agent.domain.cohort import binding_reference
    window = covered_window()
    ref = binding_reference("first")
    facts = (AdmissionFact("r1", "first", "resolve", NOW),)
    base = dict(kind="incident", repository="o/r", actor="operator", provenance="test evidence", occurred_at=NOW.isoformat(), family="held-target-dispatch", classification="systemic", severity="high", first_seen=NOW.isoformat(), evidence_refs=["baseline audit"], workflow_ref=ref)
    audit = parse_observation(dict(base, record_id="audit", finding_source="audit"))
    escaped = parse_observation(dict(base, record_id="escaped", finding_source="escaped"))
    corrected = parse_observation(dict(base, record_id="correction", finding_source="escaped", original_record_id="escaped"))
    action = intervention(record_id="action", event_ref="same-event", category="mechanical", workflow_ref=ref)
    annotation = intervention(record_id="annotation", event_ref="same-event", category="mechanical", workflow_ref=ref)
    outcome = measurement("first", "r1", "outcome", outcome=dict(state="merged", pr_number=44, merged_at=NOW.isoformat()))
    result = cohort_readout(window, facts, (), (audit, escaped, corrected, action, annotation, outcome))
    metric = result["primary_family_activity"]
    assert metric["audit_catches_observed"] == 1
    assert metric["escaped_incidents_observed"] == 1
    assert metric["attributed_interventions_observed"] == 1
    assert metric["mechanical_interventions_per_distinct_merge_observed_floor"] == 1


def test_mixed_route_resolve_profile_uses_frozen_run_inputs_without_per_leg_annotations():
    window = replace(covered_window(), expected_profile=tuple(sorted({key: value for key, value in profile().items() if key not in {"runtime", "model", "difficulty"}}.items())), supported_runtimes=("claude", "codex"), supported_models=("cheap-router", "gpt"))
    facts = (AdmissionFact("route", "first", "resolve", NOW), AdmissionFact("resolve", "first", "resolve", NOW))
    runs = (RunFact("route", "v1", "o/r:fix", "ROUTE", "completed", ("USD", "rate_card", 1), "claude", "cheap-router"), RunFact("resolve", "v1", "o/r:fix", "RESOLVE", "completed", ("USD", "rate_card", 10), "codex", "gpt"))
    # One evidenced observation supplies binding difficulty/posture; known SQL
    # inputs cover both run models without another operator recording action.
    records = (measurement("first", "resolve", "profile", profile=profile()),)
    result = cohort_readout(window, facts, runs, records)
    assert result["comparability"] == "eligible_within_declared_profile"
    assert result["observed_runtime_model_kind_mix"] == {"claude|cheap-router|ROUTE": 1, "codex|gpt|RESOLVE": 1}


def test_conflicting_review_status_invalidates_declared_outcome_completeness():
    window = covered_window()
    facts = (AdmissionFact("r1", "first", "resolve", NOW), AdmissionFact("r2", "second", "resolve", NOW))
    records = (measurement("first", "r1", "a", outcome=dict(state="open", pr_number=44), first_pass_approved=True), measurement("second", "r2", "b", outcome=dict(state="open", pr_number=44), first_review_status="not_reviewed"), coverage(window))
    result = cohort_readout(window, facts, (), records)
    assert result["coverage"]["outcomes_complete"] is False
    assert result["outcomes"]["first_pass_approval"] is None


def test_independent_unknown_run_profile_assertions_cannot_overwrite_conflict():
    window = covered_window()
    facts = (AdmissionFact("r1", "first", "resolve", NOW),)
    runs = (RunFact("r1", "v1", "o/r:fix", "RESOLVE", "completed", ("USD", "rate_card", 10), None, None),)
    records = (measurement("first", "r1", "a", profile=dict(profile(), runtime="unsupported", model="bad")), measurement("first", "r1", "b", profile=profile()))
    result = cohort_readout(window, facts, runs, records)
    assert result["comparability"] == "indeterminate"
    assert any("conflicting run profile" in gap for gap in result["annotation_gaps"])


def test_correcting_merge_to_open_discards_dependent_recurrence():
    a = measurement("first", "r1", "a", outcome=dict(state="merged", pr_number=44, merged_at=NOW.isoformat()), recurrence_observed=False)
    b = measurement("first", "r1", "b", outcome=dict(state="open", pr_number=44), original_record_id="a")
    result = cohort_readout(covered_window(), (AdmissionFact("r1", "first", "resolve", NOW),), (), (a, b))
    assert result["outcomes"]["merges_observed"] == 0
    assert result["outcomes"]["recurrence"] is None


def test_event_reference_namespace_does_not_collide_with_incident_record_identity():
    incident = parse_observation(dict(kind="incident", record_id="same-event", repository="o/r", actor="x", provenance="x", occurred_at=NOW.isoformat(), family="f", finding_source="escaped", classification="systemic", severity="high", first_seen=NOW.isoformat(), evidence_refs=["x"]))
    result = observation_summary((intervention(record_id="action", event_ref="same-event"), incident))
    assert result["distinct_incidents_observed"] == 1


@pytest.mark.parametrize("outcome", [dict(state="merged", pr_number=45, merged_at=NOW.isoformat()), dict(state="open")])
def test_changed_or_missing_pr_correction_does_not_inherit_quality_facts(outcome):
    a = measurement("first", "r1", "a", outcome=dict(state="merged", pr_number=44, merged_at=NOW.isoformat()), first_pass_approved=True, recurrence_observed=False)
    b = measurement("first", "r1", "b", outcome=outcome, original_record_id="a")
    result = cohort_readout(covered_window(), (AdmissionFact("r1", "first", "resolve", NOW),), (), (a, b, coverage(covered_window())))
    assert result["outcomes"]["first_pass_approval"] is None
    assert result["outcomes"]["recurrence"] is None


def test_not_reviewed_correction_drops_previous_approval():
    a = measurement("first", "r1", "a", outcome=dict(state="open", pr_number=44), first_pass_approved=True)
    b = measurement("first", "r1", "b", outcome=dict(state="open", pr_number=44), first_review_status="not_reviewed", original_record_id="a")
    result = cohort_readout(covered_window(), (AdmissionFact("r1", "first", "resolve", NOW),), (), (a, b))
    assert result["outcomes"]["first_pass_approval"] is None


def test_cohort_attributed_actions_share_tagged_summary_identity():
    from issue_agent.domain.cohort import binding_reference
    ref = binding_reference("first")
    result = cohort_readout(covered_window(), (AdmissionFact("r1", "first", "resolve", NOW),), (), (intervention(record_id="foo", workflow_ref=ref), intervention(record_id="bar", event_ref="foo", workflow_ref=ref)))
    assert result["primary_family_activity"]["attributed_interventions_observed"] == 2
