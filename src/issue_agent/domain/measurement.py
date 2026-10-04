"""Prospective observations are facts, never lifecycle authorization."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Literal, Mapping

from .repository import canonical_repository

MAX_TEXT = 2048


def text(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > MAX_TEXT:
        raise ValueError(f"{name} must be nonempty text of at most {MAX_TEXT} bytes")
    return value


def stamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a UTC timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be a UTC timestamp")
    return parsed


def count(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError(f"{name} must be an integer between 0 and 1000000000000")
    return value


@dataclass(frozen=True)
class IncidentObservation:
    record_id: str
    repository: str
    actor: str
    provenance: str
    occurred_at: datetime
    family: str
    finding_source: Literal["audit", "escaped"]
    classification: Literal["systemic", "incidental", "mixed", "unknown"]
    severity: Literal["low", "medium", "high", "critical"]
    first_seen: datetime
    evidence_refs: tuple[str, ...]
    resolved_at: datetime | None = None
    introduced_revision: str | None = None
    fixed_revision: str | None = None
    workflow_ref: str | None = None
    original_record_id: str | None = None

    def __post_init__(self) -> None:
        validate_record(self)


@dataclass(frozen=True)
class HumanIntervention:
    record_id: str
    repository: str
    actor: str
    provenance: str
    occurred_at: datetime
    category: Literal["diagnosis", "mechanical", "product"]
    source: Literal["dashboard", "GitHub", "service", "config", "other"]
    description: str
    duration_seconds: int | None = None
    interruptions: int | None = None
    incident_ref: str | None = None
    workflow_ref: str | None = None
    event_ref: str | None = None
    original_record_id: str | None = None

    def __post_init__(self) -> None:
        validate_record(self)





def validate_record(record: Observation) -> None:
    for field in fields(record):
        value = getattr(record, field.name)
        if value is None:
            if field.name not in {"resolved_at", "introduced_revision", "fixed_revision", "workflow_ref", "original_record_id", "incident_ref", "event_ref", "duration_seconds", "interruptions"}:
                raise ValueError(f"missing {field.name}")
        elif field.name in {"occurred_at", "first_seen", "resolved_at"}:
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
                raise ValueError(f"{field.name} must be a UTC timestamp")
        elif field.name in {"duration_seconds", "interruptions"}:
            count(value, field.name)
        elif field.name == "evidence_refs":
            if not isinstance(value, tuple) or not 1 <= len(value) <= 16:
                raise ValueError("evidence_refs requires 1 to 16 immutable references")
            for reference in value:
                text(reference, field.name)
        else:
            text(value, field.name)
    object.__setattr__(record, "repository", canonical_repository(record.repository))
    if record.original_record_id == record.record_id:
        raise ValueError("an observation cannot reference itself")
    if isinstance(record, IncidentObservation):
        if record.finding_source not in {"audit", "escaped"} or record.classification not in {"systemic", "incidental", "mixed", "unknown"} or record.severity not in {"low", "medium", "high", "critical"}:
            raise ValueError("invalid incident vocabulary")
        if record.first_seen > record.occurred_at or (record.resolved_at is not None and not record.first_seen <= record.resolved_at <= record.occurred_at):
            raise ValueError("incident timestamps must be ordered")
    elif record.category not in {"diagnosis", "mechanical", "product"} or record.source not in {"dashboard", "GitHub", "service", "config", "other"}:
        raise ValueError("invalid intervention vocabulary")


def parse_observation(raw: object) -> Observation:
    if not isinstance(raw, Mapping):
        raise ValueError("observation must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str):
        raise ValueError("kind must be a supported observation name")
    if kind in {"workflow_measurement", "cohort_coverage"}:
        return parse_measurement_record(raw)
    cls = IncidentObservation if kind == "incident" else HumanIntervention if kind == "intervention" else None
    if cls is None:
        raise ValueError("kind must be incident or intervention")
    names = {field.name for field in fields(cls)}
    if set(raw) - names - {"kind"}:
        raise ValueError("unknown observation fields")
    values: dict = {}
    timestamps = {"occurred_at", "first_seen", "resolved_at"}
    optional = {"resolved_at", "introduced_revision", "fixed_revision", "workflow_ref", "original_record_id", "incident_ref", "event_ref", "duration_seconds", "interruptions"}
    choices = {"finding_source": {"audit", "escaped"}, "classification": {"systemic", "incidental", "mixed", "unknown"}, "severity": {"low", "medium", "high", "critical"}, "category": {"diagnosis", "mechanical", "product"}, "source": {"dashboard", "GitHub", "service", "config", "other"}}
    for name in names:
        value = raw.get(name)
        if value is None and name in optional:
            values[name] = None
        elif name in timestamps:
            values[name] = stamp(value, name)
        elif name in {"duration_seconds", "interruptions"}:
            values[name] = count(value, name)
        elif name == "evidence_refs":
            if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 16:
                raise ValueError("evidence_refs requires 1 to 16 references")
            values[name] = tuple(text(item, name) for item in value)
        else:
            values[name] = text(value, name)
            if name in choices and values[name] not in choices[name]:
                raise ValueError(f"invalid {name}")
    values["repository"] = canonical_repository(values["repository"])
    record = cls(**values)
    if isinstance(record, IncidentObservation):
        if record.first_seen > record.occurred_at or (record.resolved_at is not None and not record.first_seen <= record.resolved_at <= record.occurred_at):
            raise ValueError("incident timestamps must be ordered")
    if record.original_record_id == record.record_id:
        raise ValueError("an observation cannot reference itself")
    return record


def observation_json(record: Observation) -> dict:
    def encode(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, MergedOutcome):
            return {"state": "merged", "merged_at": value.merged_at.isoformat(), "pr_number": value.pr_number}
        if isinstance(value, (ObservedProfile, UnmergedOutcome)):
            return {field.name: encode(getattr(value, field.name)) for field in fields(value)}
        if isinstance(value, tuple):
            return list(value)
        return value
    result = {field.name: encode(getattr(record, field.name)) for field in fields(record)}
    result["kind"] = "incident" if isinstance(record, IncidentObservation) else "intervention" if isinstance(record, HumanIntervention) else "workflow_measurement" if isinstance(record, WorkflowMeasurement) else "cohort_coverage"
    return result


def resolve_observations(records: tuple[Observation, ...]) -> tuple[tuple[tuple[Observation, Observation], ...], tuple[str, ...]]:
    unique: dict[tuple[str, str], Observation] = {}
    gaps: set[str] = set()
    for record in records:
        key = (record.repository, record.record_id)
        if key in unique and unique[key] != record:
            gaps.add(f"conflicting observation: {record.repository}:{record.record_id}")
            continue
        unique[key] = record
    resolved: list[tuple[int, Observation, Observation]] = []
    for record in unique.values():
        seen: set[str] = set()
        original = record
        while original.original_record_id is not None:
            if original.record_id in seen:
                gaps.add(f"cyclic observation reference: {record.repository}:{record.record_id}")
                break
            seen.add(original.record_id)
            parent = unique.get((record.repository, original.original_record_id))
            if parent is None or type(parent) is not type(record):
                gaps.add(f"unresolved observation reference: {record.repository}:{record.record_id}")
                break
            original = parent
        else:
            resolved.append((len(seen), original, record))
    latest: dict[tuple[str, str, str, str], tuple[Observation, Observation]] = {}
    for _depth, original, record in sorted(resolved, key=lambda item: (item[0], item[2].occurred_at, item[2].record_id)):
        event_ref = getattr(original, "event_ref", None)
        key = (original.repository, type(original).__name__, "event" if event_ref else "record", event_ref or original.record_id)
        prior = latest.get(key)
        if prior is not None:
            retained = {field.name: getattr(prior[1], field.name) for field in fields(record) if getattr(record, field.name) is None and getattr(prior[1], field.name) is not None and field.name != "original_record_id"}
            if isinstance(record, WorkflowMeasurement):
                previous_outcome = prior[1].outcome
                current_outcome = record.outcome or previous_outcome
                same_pr = previous_outcome is not None and current_outcome is not None and previous_outcome.pr_number is not None and previous_outcome.pr_number == current_outcome.pr_number
                if not same_pr:
                    for name in ("first_pass_approved", "first_review_status", "recurrence_observed"):
                        retained.pop(name, None)
                if isinstance(current_outcome, UnmergedOutcome):
                    retained.pop("recurrence_observed", None)
                if record.first_review_status == "not_reviewed":
                    retained.pop("first_pass_approved", None)
            record = replace(record, **retained)
        latest[key] = (original, record)
    return tuple(latest.values()), tuple(sorted(gaps))


def observation_summary(records: tuple[Observation, ...], *, started_at: datetime | None = None, ended_at: datetime | None = None) -> dict:
    resolved, gaps = resolve_observations(records)
    included = tuple(current for original, current in resolved if (started_at is None or original.occurred_at >= started_at) and (ended_at is None or original.occurred_at < ended_at))
    actions = tuple(record for record in included if isinstance(record, HumanIntervention))
    incidents = tuple(record for record in included if isinstance(record, IncidentObservation))
    unknown_duration = sum(record.duration_seconds is None for record in actions)
    unknown_interruptions = sum(record.interruptions is None for record in actions)
    duration = sum(record.duration_seconds or 0 for record in actions)
    interruptions = sum(record.interruptions or 0 for record in actions)
    return {
        "incident_observations": len({(record.repository, record.record_id) for record in records if isinstance(record, IncidentObservation) and (started_at is None or record.occurred_at >= started_at) and (ended_at is None or record.occurred_at < ended_at)}),
        "distinct_incidents_observed": len(incidents),
        "incidents_by_source": {source: sum(record.finding_source == source for record in incidents) for source in ("audit", "escaped")},
        "incident_families": sorted({record.family for record in incidents}),
        "interventions_observed": len(actions),
        "by_category": {category: sum(record.category == category for record in actions) for category in ("diagnosis", "mechanical", "product")},
        "measured_duration_seconds": duration,
        "duration_unknown": unknown_duration,
        "complete_duration_seconds": None if unknown_duration or gaps else duration,
        "measured_interruptions": interruptions,
        "interruptions_unknown": unknown_interruptions,
        "complete_interruptions": None if unknown_interruptions or gaps else interruptions,
        "human_coverage": "observed_floor",
        "reference_gaps": list(gaps),
    }


@dataclass(frozen=True)
class ObservedProfile:
    deployment: str
    runtime: str
    model: str
    posture: str
    difficulty: str
    config_version: str

    def __post_init__(self) -> None:
        for field in fields(self):
            text(getattr(self, field.name), field.name)


@dataclass(frozen=True)
class UnmergedOutcome:
    state: Literal["open", "held", "parked", "abandoned"]
    pr_number: int | None = None
    held_since: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, str):
            raise ValueError("outcome state must be text")
        if self.pr_number is not None and (type(self.pr_number) is not int or not 1 <= self.pr_number <= 2**31 - 1):
            raise ValueError("PR number must be positive")
        if self.held_since is not None:
            if self.state not in {"held", "parked"} or not isinstance(self.held_since, datetime) or self.held_since.tzinfo is None or self.held_since.utcoffset() != timezone.utc.utcoffset(self.held_since):
                raise ValueError("held_since requires a UTC time on a held/parked outcome")
        if not isinstance(self.state, str) or self.state not in {"open", "held", "parked", "abandoned"}:
            raise ValueError("invalid unmerged outcome")


@dataclass(frozen=True)
class MergedOutcome:
    merged_at: datetime
    pr_number: int

    def __post_init__(self) -> None:
        if type(self.pr_number) is not int or not 1 <= self.pr_number <= 2**31 - 1:
            raise ValueError("merged outcome requires a positive PR number")
        if not isinstance(self.merged_at, datetime) or self.merged_at.tzinfo is None or self.merged_at.utcoffset() != timezone.utc.utcoffset(self.merged_at):
            raise ValueError("merged_at must be UTC")


@dataclass(frozen=True)
class WorkflowMeasurement:
    record_id: str
    repository: str
    actor: str
    provenance: str
    occurred_at: datetime
    run_id: str
    binding_ref: str
    evidence_refs: tuple[str, ...]
    profile: ObservedProfile | None = None
    outcome: UnmergedOutcome | MergedOutcome | None = None
    first_pass_approved: bool | None = None
    first_review_status: Literal["not_reviewed", "reviewed"] | None = None
    recurrence_observed: bool | None = None
    original_record_id: str | None = None

    def __post_init__(self) -> None:
        validate_measurement_record(self)
        if self.profile is not None and not isinstance(self.profile, ObservedProfile):
            raise ValueError("profile requires a complete typed profile observation")
        if self.outcome is not None and not isinstance(self.outcome, (UnmergedOutcome, MergedOutcome)):
            raise ValueError("outcome requires a typed variant")
        if isinstance(self.outcome, MergedOutcome) and self.outcome.merged_at > self.occurred_at:
            raise ValueError("merge cannot follow observation")
        if isinstance(self.outcome, UnmergedOutcome) and self.outcome.held_since is not None and self.outcome.held_since > self.occurred_at:
            raise ValueError("hold cannot follow observation")
        if self.first_review_status is not None and (not isinstance(self.first_review_status, str) or self.first_review_status not in {"not_reviewed", "reviewed"}):
            raise ValueError("invalid first_review_status")
        if self.first_review_status == "not_reviewed" and self.first_pass_approved is not None:
            raise ValueError("unreviewed PR cannot have an approval outcome")
        if self.first_pass_approved is not None:
            object.__setattr__(self, "first_review_status", "reviewed")
        if self.first_review_status is not None and (self.outcome is None or self.outcome.pr_number is None):
            raise ValueError("first review status requires a proven PR identity")
        if self.first_pass_approved is not None and (self.outcome is None or self.outcome.pr_number is None):
            raise ValueError("first review observation requires a proven PR identity")
        if self.recurrence_observed is not None and not isinstance(self.outcome, MergedOutcome):
            raise ValueError("recurrence observation requires a merged PR identity")
        for name in ("first_pass_approved", "recurrence_observed"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be an observed boolean")


@dataclass(frozen=True)
class CohortCoverage:
    record_id: str
    repository: str
    actor: str
    provenance: str
    occurred_at: datetime
    cohort_fingerprint: str
    evidence_refs: tuple[str, ...]
    human_complete: bool
    outcomes_complete: bool
    recurrence_complete: bool
    first_review_complete: bool
    recurrence_horizon_seconds: int | None = None
    original_record_id: str | None = None

    def __post_init__(self) -> None:
        validate_measurement_record(self)
        count(self.recurrence_horizon_seconds, "recurrence_horizon_seconds")
        if self.recurrence_complete and not self.recurrence_horizon_seconds:
            raise ValueError("complete recurrence coverage requires an explicit positive horizon")
        for name in ("human_complete", "outcomes_complete", "recurrence_complete", "first_review_complete"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} requires explicit boolean coverage")


def validate_measurement_record(record: WorkflowMeasurement | CohortCoverage) -> None:
    for name in ("record_id", "repository", "actor", "provenance"):
        text(getattr(record, name), name)
    object.__setattr__(record, "repository", canonical_repository(record.repository))
    if not isinstance(record.occurred_at, datetime) or record.occurred_at.tzinfo is None or record.occurred_at.utcoffset() != timezone.utc.utcoffset(record.occurred_at):
        raise ValueError("occurred_at requires UTC")
    if not isinstance(record.evidence_refs, tuple) or not 1 <= len(record.evidence_refs) <= 16:
        raise ValueError("evidence_refs requires 1 to 16 immutable references")
    for reference in record.evidence_refs:
        text(reference, "evidence_ref")
    if record.original_record_id is not None:
        text(record.original_record_id, "original_record_id")
        if record.original_record_id == record.record_id:
            raise ValueError("an observation cannot reference itself")
    if isinstance(record, WorkflowMeasurement):
        text(record.run_id, "run_id")
        reference = record.binding_ref
    else:
        reference = record.cohort_fingerprint
    if not isinstance(reference, str) or len(reference) != 64 or any(char not in "0123456789abcdef" for char in reference):
        raise ValueError("measurement identity must be a SHA256 reference")


def parse_measurement_record(raw: Mapping) -> WorkflowMeasurement | CohortCoverage:
    kind = raw.get("kind")
    cls = WorkflowMeasurement if kind == "workflow_measurement" else CohortCoverage
    if set(raw) - {field.name for field in fields(cls)} - {"kind"}:
        raise ValueError("unknown measurement fields")
    values = {key: value for key, value in raw.items() if key != "kind"}
    values["occurred_at"] = stamp(raw.get("occurred_at"), "occurred_at")
    refs = raw.get("evidence_refs")
    if not isinstance(refs, (list, tuple)) or not 1 <= len(refs) <= 16:
        raise ValueError("evidence_refs requires 1 to 16 references")
    values["evidence_refs"] = tuple(refs)
    if cls is WorkflowMeasurement:
        profile = raw.get("profile")
        if profile is not None:
            if not isinstance(profile, Mapping) or set(profile) != {field.name for field in fields(ObservedProfile)}:
                raise ValueError("profile requires exactly the supported profile fields")
            values["profile"] = ObservedProfile(**profile)
        outcome = raw.get("outcome")
        if outcome is not None:
            if not isinstance(outcome, Mapping):
                raise ValueError("outcome must be an object")
            if not isinstance(outcome.get("state"), str):
                raise ValueError("outcome state must be text")
            if outcome.get("state") == "merged" and set(outcome) == {"state", "merged_at", "pr_number"}:
                values["outcome"] = MergedOutcome(stamp(outcome["merged_at"], "merged_at"), outcome["pr_number"])
            elif set(outcome) <= {"state", "pr_number", "held_since"} and "state" in outcome:
                values["outcome"] = UnmergedOutcome(outcome["state"], outcome.get("pr_number"), stamp(outcome["held_since"], "held_since") if outcome.get("held_since") is not None else None)
            else:
                raise ValueError("contradictory outcome fields")
    try:
        return cls(**values)
    except TypeError as exc:
        raise ValueError(f"missing measurement fields: {exc}") from exc

Observation = IncidentObservation | HumanIntervention | WorkflowMeasurement | CohortCoverage
