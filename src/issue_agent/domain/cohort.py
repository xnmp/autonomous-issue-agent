"""Cohort arithmetic over read-only admission and accounting facts."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from .measurement import text, Observation, observation_summary, WorkflowMeasurement, CohortCoverage, MergedOutcome, UnmergedOutcome, HumanIntervention, IncidentObservation, resolve_observations
from .repository import canonical_repository


@dataclass(frozen=True)
class CohortWindow:
    repository: str
    started_at: datetime
    ended_at: datetime
    observation_cutoff: datetime
    selection: str = "new_admissions"
    expected_config_version: str | None = None
    expected_profile: tuple[tuple[str, str], ...] = ()
    primary_family: str = "resolve"
    inherited_binding_refs: tuple[str, ...] = ()
    supported_runtimes: tuple[str, ...] = ()
    supported_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", canonical_repository(self.repository))
        if not isinstance(self.primary_family, str) or self.primary_family not in {"resolve", "review_execution", "issue_recovery", "pr_repair", "steward"}:
            raise ValueError("invalid primary family")
        if not isinstance(self.selection, str) or self.selection not in {"new_admissions", "include_inherited"}:
            raise ValueError("invalid cohort selection")
        if not isinstance(self.inherited_binding_refs, tuple) or len(self.inherited_binding_refs) > 4096 or any(not isinstance(ref, str) or len(ref) != 64 or any(char not in "0123456789abcdef" for char in ref) for ref in self.inherited_binding_refs):
            raise ValueError("inherited_binding_refs requires at most 4096 SHA256 references")
        if len(set(self.inherited_binding_refs)) != len(self.inherited_binding_refs):
            raise ValueError("duplicate inherited binding references")
        if self.selection == "new_admissions" and self.inherited_binding_refs:
            raise ValueError("inherited references require include_inherited selection")
        for values in (self.supported_runtimes, self.supported_models):
            if not isinstance(values, tuple) or len(values) > 16 or len(set(values)) != len(values):
                raise ValueError("supported runtime/model mix must be bounded and unique")
            for value in values:
                text(value, "supported runtime/model")
        if self.expected_config_version is not None:
            text(self.expected_config_version, "expected_config_version")
        if not isinstance(self.expected_profile, tuple) or len(self.expected_profile) > 16:
            raise ValueError("expected_profile requires bounded immutable fields")
        for pair in self.expected_profile:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("expected_profile requires key/value pairs")
            text(pair[0], "profile key")
            text(pair[1], "profile value")
        if len(dict(self.expected_profile)) != len(self.expected_profile):
            raise ValueError("duplicate expected profile fields")
        if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset().total_seconds() != 0 for value in (self.started_at, self.ended_at, self.observation_cutoff)):
            raise ValueError("cohort timestamps must be UTC")
        if not self.started_at < self.ended_at <= self.observation_cutoff:
            raise ValueError("cohort requires start < end <= observation cutoff")


@dataclass(frozen=True)
class AdmissionFact:
    run_id: str
    identity: str
    family: str
    admitted_at: datetime
    target: str | None = None
    generation: int | None = None
    authority_epoch: int | None = None
    episode_id: str | None = None
    parent_rejection_identity: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "identity", "family"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"invalid admission {name}")
        if not isinstance(self.admitted_at, datetime) or self.admitted_at.tzinfo is None or self.admitted_at.utcoffset().total_seconds() != 0:
            raise ValueError("admission timestamp requires UTC")


@dataclass(frozen=True)
class FailedExecution:
    reason: str
    evidence_ref: str

    def __post_init__(self) -> None:
        text(self.reason, "execution failure reason")
        text(self.evidence_ref, "execution failure evidence")


@dataclass(frozen=True)
class CompletedExecution:
    evidence_ref: str

    def __post_init__(self) -> None:
        text(self.evidence_ref, "execution completion evidence")


@dataclass(frozen=True)
class RunFact:
    run_id: str
    config_version: str
    owner_id: str
    kind: str
    state: str
    cost: tuple[str, str, int] | None
    runtime: str | None = None
    model: str | None = None
    effort: str | None = None
    outcome: str | None = None
    execution: FailedExecution | CompletedExecution | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "config_version", "owner_id", "kind", "state"):
            text(getattr(self, name), name)
        if self.cost is not None:
            if not isinstance(self.cost, tuple) or len(self.cost) != 3 or type(self.cost[2]) is not int or not 0 <= self.cost[2] <= 2**63 - 1:
                raise ValueError("run cost requires bounded nonnegative integer micros")
            text(self.cost[0], "currency")
            text(self.cost[1], "pricing basis")
        for name in ("runtime", "model", "effort", "outcome"):
            if getattr(self, name) is not None:
                text(getattr(self, name), name)
        if self.execution is not None and not isinstance(self.execution, (FailedExecution, CompletedExecution)):
            raise ValueError("execution requires observed completion or failure evidence")


def binding_reference(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


def cohort_fingerprint(window: CohortWindow) -> str:
    raw = (window.repository, window.started_at.isoformat(), window.ended_at.isoformat(), window.observation_cutoff.isoformat(), window.selection, window.expected_config_version, window.expected_profile, window.primary_family, window.inherited_binding_refs, window.supported_runtimes, window.supported_models)
    return hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest()


def cohort_readout(window: CohortWindow, admissions: tuple[AdmissionFact, ...], runs: tuple[RunFact, ...], records: tuple[Observation, ...], gaps: tuple[str, ...] = ()) -> dict:
    by_identity: dict[str, list[AdmissionFact]] = {}
    for fact in admissions:
        if fact.admitted_at <= window.observation_cutoff:
            by_identity.setdefault(fact.identity, []).append(fact)
    selected = {identity: facts for identity, facts in by_identity.items() if min(fact.admitted_at for fact in facts) < window.ended_at and (window.started_at <= min(fact.admitted_at for fact in facts) or (window.selection == "include_inherited" and binding_reference(identity) in window.inherited_binding_refs))}
    primary = {identity: facts for identity, facts in selected.items() if facts[0].family == window.primary_family}
    selected_runs = {fact.run_id for facts in primary.values() for fact in facts}
    all_selected_runs = {fact.run_id for facts in selected.values() for fact in facts}
    bindings_per_run: dict[str, set[str]] = {}
    for facts in by_identity.values():
        for fact in facts:
            bindings_per_run.setdefault(fact.run_id, set()).add(fact.identity)
    spend: dict[str, int] = {}
    all_spend: dict[str, int] = {}
    shared: dict[str, int] = {}
    unassigned: dict[str, int] = {}
    states: dict[str, int] = {}
    configs: set[str] = set()
    for run in runs:
        if run.run_id in selected_runs:
            states[run.state] = states.get(run.state, 0) + 1
            configs.add(run.config_version)
        if run.cost is None:
            continue
        currency, basis, amount = run.cost
        key = f"{currency}:{basis}"
        if run.run_id in all_selected_runs:
            all_spend[key] = all_spend.get(key, 0) + amount
        if run.run_id in selected_runs:
            spend[key] = spend.get(key, 0) + amount
            if len(bindings_per_run.get(run.run_id, ())) > 1:
                shared[key] = shared.get(key, 0) + amount
        elif run.run_id not in bindings_per_run:
            unassigned[key] = unassigned.get(key, 0) + amount
    observations = tuple(record for record in records if window.started_at <= record.occurred_at < window.ended_at)
    family_counts: dict[str, int] = {}
    for facts in selected.values():
        family = facts[0].family
        family_counts[family] = family_counts.get(family, 0) + 1
    fingerprint = cohort_fingerprint(window)
    applicable = tuple(record for record in records if record.occurred_at <= window.observation_cutoff)
    resolved, reference_gaps = resolve_observations(applicable)
    canonical = tuple(current for _original, current in resolved)
    activity = tuple(current for original, current in resolved if window.started_at <= original.occurred_at < window.ended_at)
    coverage = [record for record in canonical if isinstance(record, CohortCoverage) and record.cohort_fingerprint == fingerprint]
    latest_coverage = max(coverage, key=lambda record: (record.occurred_at, record.record_id), default=None)
    measurements: dict[str, dict] = {}
    observed_profiles: dict[tuple[str, str], object] = {}
    known_pairs = {(fact.run_id, binding_reference(fact.identity)) for fact in admissions}
    annotation_gaps: list[str] = list(reference_gaps)
    missing_inherited = set(window.inherited_binding_refs) - {binding_reference(identity) for identity in selected}
    annotation_gaps.extend(f"requested inherited binding absent: {ref}" for ref in sorted(missing_inherited))
    for record in sorted(canonical, key=lambda record: (record.occurred_at, record.record_id)):
        if not isinstance(record, WorkflowMeasurement):
            continue
        if (record.run_id, record.binding_ref) not in known_pairs:
            annotation_gaps.append(f"mismatched workflow measurement: {record.record_id}")
            continue
        facts = measurements.setdefault(record.binding_ref, {})
        for name in ("profile", "outcome", "first_pass_approved", "first_review_status", "recurrence_observed"):
            value = getattr(record, name)
            if name == "profile" and value is not None:
                pair = (record.binding_ref, record.run_id)
                if pair in observed_profiles and observed_profiles[pair] != value:
                    annotation_gaps.append(f"conflicting run profile for {record.binding_ref}:{record.run_id}")
                observed_profiles[pair] = value
                previous = facts.get("profile")
                if previous is not None and any(getattr(previous, field) != getattr(value, field) for field in ("deployment", "posture", "difficulty", "config_version")):
                    annotation_gaps.append(f"conflicting binding profile for {record.binding_ref}")
                facts["profile"] = value
                continue
            if value is not None:
                previous = facts.get(name)
                if previous is not None and previous != value:
                    if name in {"profile", "first_pass_approved", "recurrence_observed"} or (name == "outcome" and isinstance(previous, MergedOutcome) and isinstance(value, MergedOutcome)):
                        annotation_gaps.append(f"conflicting {name} observations for binding {record.binding_ref}")
                facts[name] = value
    expected = dict(window.expected_profile)
    profile_gaps: list[str] = []
    binding_rows = []
    merged_targets: dict[int, dict] = {}
    reviewed_targets: dict[int, bool] = {}
    known_primary_prs: set[int] = set()
    unreviewed_prs: set[int] = set()
    first_pass = []
    recurrence = []
    outcome_missing = 0
    held_states: dict[str, int] = {}
    by_run = {run.run_id: run for run in runs}
    difficulty: dict[str, int] = {}
    effective_profiles: dict[str, tuple[str, str, str]] = {}
    for identity, facts in sorted(selected.items()):
        reference = binding_reference(identity)
        annotation = measurements.get(reference, {})
        profile = annotation.get("profile")
        outcome = annotation.get("outcome")
        run_ids = sorted({fact.run_id for fact in facts})
        frozen_profiles = tuple(by_run[run_id] for run_id in run_ids if run_id in by_run)
        profile_gaps.extend(f"admitted run missing frozen inputs: {run_id}" for run_id in run_ids if run_id not in by_run)
        if profile is None:
            profile_gaps.append(f"missing observed supported profile: {reference}")
        else:
            actual = {name: getattr(profile, name) for name in ("deployment", "runtime", "model", "posture", "difficulty", "config_version")}
            if any(actual.get(key) != value for key, value in expected.items() if key not in {"runtime", "model"}):
                profile_gaps.append(f"supported binding profile mismatch: {reference}")
            for run in frozen_profiles:
                observed = observed_profiles.get((reference, run.run_id))
                runtime = run.runtime or (getattr(observed, "runtime") if observed is not None else None)
                model = run.model or (getattr(observed, "model") if observed is not None else None)
                allowed_runtimes = window.supported_runtimes or ((expected["runtime"],) if "runtime" in expected else ())
                allowed_models = window.supported_models or ((expected["model"],) if "model" in expected else ())
                if runtime is not None and model is not None:
                    effective = (runtime, model, run.kind)
                    if run.run_id in effective_profiles and effective_profiles[run.run_id] != effective:
                        profile_gaps.append(f"conflicting actual runtime/model observations: {run.run_id}")
                    effective_profiles[run.run_id] = effective
                if runtime is None or model is None:
                    profile_gaps.append(f"missing actual runtime/model: {run.run_id}")
                elif runtime not in allowed_runtimes or model not in allowed_models:
                    profile_gaps.append(f"runtime/model outside declared mix: {run.run_id}")
                if run.config_version != profile.config_version or (observed is not None and ((run.runtime is not None and run.runtime != getattr(observed, "runtime")) or (run.model is not None and run.model != getattr(observed, "model")))):
                    profile_gaps.append(f"observed profile conflicts with frozen admission inputs: {run.run_id}")
            difficulty[profile.difficulty] = difficulty.get(profile.difficulty, 0) + 1
        if identity in primary and isinstance(outcome, (MergedOutcome, UnmergedOutcome)) and outcome.pr_number is not None:
            known_primary_prs.add(outcome.pr_number)
            if annotation.get("first_review_status") == "not_reviewed":
                unreviewed_prs.add(outcome.pr_number)
        if identity in primary and isinstance(outcome, (MergedOutcome, UnmergedOutcome)) and outcome.pr_number is not None and "first_pass_approved" in annotation:
            if outcome.pr_number in reviewed_targets and reviewed_targets[outcome.pr_number] != annotation["first_pass_approved"]:
                annotation_gaps.append(f"conflicting first-pass observation for PR {outcome.pr_number}")
            reviewed_targets[outcome.pr_number] = annotation["first_pass_approved"]
        if isinstance(outcome, MergedOutcome):
            if identity in primary:
                target = merged_targets.setdefault(outcome.pr_number, {"merged_at": outcome.merged_at})
                if target["merged_at"] != outcome.merged_at:
                    annotation_gaps.append(f"conflicting merge times for PR {outcome.pr_number}")
                for name in ("first_pass_approved", "recurrence_observed"):
                    if name in annotation:
                        if name in target and target[name] != annotation[name]:
                            annotation_gaps.append(f"conflicting {name} for PR {outcome.pr_number}")
                        target[name] = annotation[name]
            state = "merged"
        elif isinstance(outcome, UnmergedOutcome):
            state = outcome.state
        else:
            outcome_missing += identity in primary
            state = "unknown"
        held_states[state] = held_states.get(state, 0) + 1

        binding_rows.append({"binding_ref": reference, "target": facts[0].target, "generation": facts[0].generation, "authority_epoch": facts[0].authority_epoch, "episode_id": facts[0].episode_id, "family": facts[0].family, "run_ids": run_ids, "first_admitted_at": min(fact.admitted_at for fact in facts).isoformat(), "outcome": state, "held_duration_seconds": int((window.observation_cutoff - outcome.held_since).total_seconds()) if isinstance(outcome, UnmergedOutcome) and outcome.held_since is not None else None, "unresolved_age_seconds": None if state == "merged" else int((window.observation_cutoff - min(fact.admitted_at for fact in facts)).total_seconds()), "frozen_profiles": [{"run_id": run.run_id, "runtime": run.runtime, "model": run.model, "effort": run.effort, "config_version": run.config_version, "owner_id": run.owner_id} for run in frozen_profiles]})
    if window.expected_config_version is not None and configs != {window.expected_config_version}:
        profile_gaps.append("observed configuration versions do not match expected profile")
    if (window.supported_models or window.supported_runtimes) and not (window.expected_config_version or expected.get("config_version")):
        profile_gaps.append("mixed-profile eligibility requires frozen configuration version")
    if not expected:
        profile_gaps.append("expected supported profile not specified")
    merged = len(merged_targets)
    first_pass = list(reviewed_targets.values())
    recurrence = [target["recurrence_observed"] for target in merged_targets.values() if "recurrence_observed" in target]
    human = observation_summary(applicable, started_at=window.started_at, ended_at=window.ended_at)
    complete_accounting = all(run_id in by_run and by_run[run_id].cost is not None for run_id in selected_runs)
    first_review_missing = known_primary_prs - set(reviewed_targets) - unreviewed_prs
    if unreviewed_prs & set(reviewed_targets):
        annotation_gaps.append("conflicting reviewed/unreviewed PR observations")
    complete_outcomes = latest_coverage is not None and latest_coverage.outcomes_complete and outcome_missing == 0 and not annotation_gaps
    complete_recurrence = complete_outcomes and latest_coverage is not None and latest_coverage.recurrence_complete and len(recurrence) == merged and not annotation_gaps and all((window.observation_cutoff - target["merged_at"]).total_seconds() >= latest_coverage.recurrence_horizon_seconds for target in merged_targets.values())
    human_complete = latest_coverage is not None and latest_coverage.human_complete and not human["reference_gaps"]
    touched = {record.workflow_ref for record in activity if isinstance(record, HumanIntervention) and record.workflow_ref in {binding_reference(identity) for identity in primary}}
    incident_records = tuple(record for record in activity if isinstance(record, IncidentObservation) and record.workflow_ref in {binding_reference(identity) for identity in primary})
    escaped_incidents = tuple(record for record in incident_records if record.finding_source == "escaped")
    audit_incidents = tuple(record for record in incident_records if record.finding_source == "audit")
    attributed_actions = tuple(record for record in activity if isinstance(record, HumanIntervention) and record.workflow_ref in {binding_reference(identity) for identity in primary})
    failed_spend: dict[str, int] = {}
    failed_runs = {run.run_id for run in runs if run.run_id in selected_runs and isinstance(run.execution, FailedExecution)}
    unknown_execution = sorted(run_id for run_id in selected_runs if run_id not in by_run or by_run[run_id].execution is None)
    for run in runs:
        if run.run_id in failed_runs and run.cost is not None:
            currency, basis, amount = run.cost
            key = f"{currency}:{basis}"
            failed_spend[key] = failed_spend.get(key, 0) + amount
    return {
        "repository": window.repository,
        "window": {"started_at": window.started_at.isoformat(), "ended_at": window.ended_at.isoformat(), "observation_cutoff": window.observation_cutoff.isoformat(), "selection": window.selection, "inherited_binding_refs": list(window.inherited_binding_refs)},
        "denominator": "distinct full admitted bindings including episode and authority epoch; review execution includes execution epoch/episode; versions excluded",
        "primary_family": window.primary_family,
        "primary_admitted_bindings": len(primary),
        "all_family_spend_micros_by_currency_basis": dict(sorted(all_spend.items())),
        "spend_scope": "primary-family run union; other families linked only by identical authoritative run admission",
        "parent_rejection_admitted_bindings": len({fact.parent_rejection_identity for facts in selected.values() for fact in facts if fact.parent_rejection_identity is not None}),
        "parent_rejection_binding_refs": sorted({binding_reference(fact.parent_rejection_identity) for facts in selected.values() for fact in facts if fact.parent_rejection_identity is not None}),
        "admitted_bindings_by_family": dict(sorted(family_counts.items())),
        "binding_overlap_runs": sum(len(bindings_per_run.get(run_id, ())) > 1 for run_id in selected_runs),
        "run_state_observation": "current SQL snapshot; outcomes at cutoff come only from explicit observations",
        "selected_run_states": dict(sorted(states.items())),
        "spend_micros_by_currency_basis": dict(sorted(spend.items())),
        "shared_binding_spend_micros": dict(sorted(shared.items())),
        "unassigned_spend_micros": dict(sorted(unassigned.items())),
        "configuration_versions": sorted(configs),
        "coverage_gaps": sorted(set((*gaps, *profile_gaps))),
        "cohort_fingerprint": fingerprint,
        "comparability": "eligible_within_declared_profile" if primary and not gaps and not profile_gaps and not annotation_gaps else "indeterminate",
        "profile_observation_provenance": "operator-reported; checked against available frozen admission inputs",
        "bindings": binding_rows,
        "observed_runtime_model_kind_mix": {key: sum("|".join(effective_profiles.get(run.run_id, (run.runtime or "unknown", run.model or "unknown", run.kind))) == key for run in runs if run.run_id in selected_runs) for key in sorted({"|".join(effective_profiles.get(run.run_id, (run.runtime or "unknown", run.model or "unknown", run.kind))) for run in runs if run.run_id in selected_runs})},
        "difficulty_strata": dict(sorted(difficulty.items())),
        "binding_outcomes": held_states,
        "failed_or_abandoned_spend_micros": failed_spend,
        "failed_or_abandoned_spend_coverage": {
            "scope": "primary-family run union; each run counted once",
            "basis": "observed execution evidence at cutoff; settlement alone proves no execution outcome",
            "complete": not unknown_execution and all(by_run[run_id].cost is not None for run_id in failed_runs),
            "unknown_execution_runs": unknown_execution,
            "known_failed_runs_without_charge": sorted(run_id for run_id in failed_runs if by_run[run_id].cost is None),
        },
        "run_execution_observations": [{
            "run_id": run.run_id,
            "outcome": "failed" if isinstance(run.execution, FailedExecution) else "completed_without_error" if isinstance(run.execution, CompletedExecution) else "unknown",
            "reason": run.execution.reason if isinstance(run.execution, FailedExecution) else None,
            "evidence_ref": run.execution.evidence_ref if run.execution is not None else None,
        } for run in runs if run.run_id in bindings_per_run],
        "coverage": {"provenance": "operator-reported completeness", "human_complete": human_complete, "outcomes_complete": complete_outcomes, "recurrence_complete": complete_recurrence, "accounting_complete": complete_accounting, "known_prs_missing_first_review": sorted(first_review_missing)},
        "first_pass_denominator": "distinct primary-cohort PRs with observed first-review outcomes, including unmerged/rejected PRs; rate requires declared outcome completeness",
        "first_pass_reviewed_prs": len(first_pass),
        "first_pass_approved_prs": sum(first_pass),
        "recurrence_horizon_seconds": latest_coverage.recurrence_horizon_seconds if latest_coverage is not None else None,
        "recurrence_denominator": "distinct merged primary-cohort PRs with declared observation coverage",
        "outcomes": {"merges_observed": merged, "merged": merged if complete_outcomes else None, "first_pass_approval": sum(first_pass) / len(first_pass) if complete_outcomes and not first_review_missing and first_pass and latest_coverage is not None and latest_coverage.first_review_complete else None, "recurrence": sum(recurrence) / merged if complete_recurrence and merged else None, "primary_family_spend_per_distinct_merge_micros_by_currency_basis": {key: amount / merged for key, amount in spend.items()} if complete_outcomes and complete_accounting and merged else None, "cost_per_merge": None},
        "observations": human,
        "primary_family_activity": {
            "escaped_incidents_observed": len(escaped_incidents),
            "audit_catches_observed": len(audit_incidents),
            "repeated_escaped_families": {family: sum(record.family == family for record in escaped_incidents) for family in sorted({record.family for record in escaped_incidents}) if sum(record.family == family for record in escaped_incidents) > 1},
            "attributed_interventions_observed": len(attributed_actions),
            "escaped_incidents_per_100_bindings_observed_floor": 100 * len(escaped_incidents) / len(primary) if primary else None,
            "interventions_by_category_per_100_bindings_observed_floor": {category: 100 * sum(isinstance(record, HumanIntervention) and record.category == category and record.workflow_ref in touched for record in activity) / len(primary) if primary else None for category in ("diagnosis", "mechanical", "product")},
            "interventions_per_100_bindings_observed_floor": 100 * len(attributed_actions) / len(primary) if primary else None,
            "touched_fraction_observed_floor": len(touched) / len(primary) if primary else None,
            "mechanical_interventions_per_distinct_merge_observed_floor": sum(isinstance(record, HumanIntervention) and record.category == "mechanical" and record.workflow_ref in touched for record in activity) / merged if merged else None,
        },
        "selected_runs_without_charge_at_cutoff": sum(run.run_id in selected_runs and run.cost is None for run in runs),
        "window_activity_note": "time-window observation counts are distinct from binding-attributed cohort outcomes",
        "annotation_gaps": sorted(annotation_gaps),
    }
