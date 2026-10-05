"""Measurement persistence reuses the control store's transaction and outbox."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .control_store import SQLiteControlStore
from ..domain.events import EventEnvelope, MeasurementObservationRecorded, Producer
from ..domain.measurement import Observation, observation_json, parse_observation

NAMESPACE = "measurement-observation"
MAX_INPUT_BYTES = 65536


def read_input(path: str) -> dict:
    with open(path, "rb") as source:
        data = source.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("measurement input exceeds 65536 bytes")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    raw = json.loads(data, object_pairs_hook=unique_object)
    if not isinstance(raw, dict):
        raise ValueError("measurement input must be an object")
    return raw


def record_observation(store: SQLiteControlStore, record: Observation, *, deployment: str, now: datetime) -> bool:
    raw = observation_json(record)
    record = parse_observation(raw)
    if record.occurred_at > now:
        raise ValueError("observation cannot occur in the future")
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    # Stable event identity also makes retrying the atomic commit safe.
    import hashlib
    import os
    identity = hashlib.sha256(f"{record.repository}\0{record.record_id}".encode()).hexdigest()
    event = EventEnvelope(MeasurementObservationRecorded(raw), f"measurement:{identity}", 0,
                          record.occurred_at, now, 0,
                          Producer(deployment, record.repository, "measurement", os.getpid(), "operator-record"))
    with store.transaction() as db:
        from ..domain.measurement import WorkflowMeasurement
        if isinstance(record, WorkflowMeasurement):
            from ..domain.cohort import binding_reference
            facts, _gaps = admission_facts(db, record.repository)
            if not any(fact.run_id == record.run_id and binding_reference(fact.identity) == record.binding_ref for fact in facts):
                raise ValueError("workflow measurement does not match an authoritative admission")
        prior = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?", (NAMESPACE, record.repository, record.record_id)).fetchone()
        if prior is not None:
            if prior["payload_json"] != encoded:
                raise ValueError("record ID already contains a different observation")
            return False
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)", (NAMESPACE, record.repository, record.record_id, now.isoformat(), encoded))
        # Explicit recording must roll back on encoding/enqueue failure, unlike
        # best-effort operational telemetry.
        store.enqueue_event(event, db)
    return True


def readonly_connection(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def read_observations(path: str, repository: str) -> tuple[Observation, ...]:
    with closing(readonly_connection(path)) as db:
        rows = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? ORDER BY key", (NAMESPACE, repository)).fetchall()
    return tuple(parse_observation(json.loads(row[0])) for row in rows)


def admission_facts(db: sqlite3.Connection, repository: str):
    from .workflow_codec import decode_workflow, encode_workflow
    from ..domain.continuation_components import ContinuationPurchase
    from ..domain.cohort import AdmissionFact
    from ..domain.contracts import ContinuationAdmission
    from ..domain.review_execution import ReviewChildAdmission
    from ..domain.workflow import ResolveAdmission
    facts = []
    gaps = []
    rows = db.execute("SELECT namespace,key,claimed_at,payload_json FROM dedupe_keys WHERE scope=? AND namespace IN ('workflow-admission','review-execution-admission','continuation-admission') ORDER BY namespace,key", (repository,)).fetchall()
    for row in rows:
        parent_rejection_identity = None
        try:
            if row["namespace"] == "workflow-admission":
                receipt = decode_workflow(row["payload_json"], ResolveAdmission)
                binding = receipt.attempt.binding
                run_id = str(receipt.attempt.run_id)
                identity = encode_workflow(binding)
                family = binding.authority.family.value
            elif row["namespace"] == "continuation-admission":
                captured = decode_workflow(row["payload_json"], tuple)
                if (len(captured) != 2 or not isinstance(captured[0], ContinuationPurchase)
                        or not isinstance(captured[1], ContinuationAdmission)):
                    raise ValueError("continuation receipt requires a purchase and admission pair")
                purchase, receipt = captured
                if (purchase.capture.binding != receipt.attempt.binding
                        or purchase.capture.leg_id != receipt.attempt.leg_id
                        or receipt.admission_version != purchase.capture.revision.version + 1):
                    raise ValueError("continuation admission differs from its purchase capture")
                if row["key"] != SQLiteControlStore._continuation_receipt_key(
                        receipt.attempt.run_id, receipt.attempt.binding.authority.family):
                    raise ValueError("continuation receipt key differs from its admitted run and family")
                binding = receipt.attempt.binding
                run_id = str(receipt.attempt.run_id)
                identity = encode_workflow(binding)
                family = binding.authority.family.value
            else:
                receipt = decode_workflow(row["payload_json"], ReviewChildAdmission)
                binding = receipt.child.round.binding.rejection
                run_id = receipt.run_id
                identity = encode_workflow(receipt.child.round.binding)
                family = "review_execution"
                parent_rejection_identity = encode_workflow(binding)
            if binding.fence.key.repository != repository:
                raise ValueError("receipt repository differs from scope")
            admitted_at = datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00"))
            if admitted_at.tzinfo is None:
                raise ValueError("admission timestamp is naive")
            facts.append(AdmissionFact(run_id, identity, family, admitted_at.astimezone(timezone.utc), f"{type(binding.fence.key.target).__name__}:{binding.fence.key.target.number}", binding.fence.generation, binding.authority.epoch, binding.episode_id, parent_rejection_identity))
        except (ValueError, TypeError, KeyError) as exc:
            gaps.append(f"invalid admission {row['namespace']}:{row['key']}: {exc}")
    return tuple(facts), tuple(gaps)


def export_cohort(path: str, window):
    from .workflow_codec import decode_workflow, encode_workflow
    from ..domain.cohort import AdmissionFact, RunFact, cohort_readout
    from ..domain.contracts import ContinuationAdmission
    from ..domain.review_execution import ReviewChildAdmission
    from ..domain.workflow import ResolveAdmission

    facts = []
    gaps = []
    with closing(readonly_connection(path)) as db:
        db.execute("BEGIN")
        facts, admission_gaps = admission_facts(db, window.repository)
        gaps.extend(admission_gaps)
        # workload_repository was added after the original runs table. Avoid
        # deriving repository scope from a role owner's spelling.
        run_rows = db.execute("SELECT r.run_id,r.config_version,r.owner_id,r.kind,r.state,r.started_at,r.outcome,r.dispatch_id,b.model,b.workload_repository,c.currency,c.basis,c.amount_micros,c.settled_at FROM runs r JOIN budget_reservations b ON b.run_id=r.run_id LEFT JOIN charges c ON c.run_id=r.run_id WHERE b.workload_repository IN (?,'*') ORDER BY r.run_id", (window.repository,)).fetchall()
        observations = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? ORDER BY key", (NAMESPACE, window.repository)).fetchall()
    known_runs = {fact.run_id for fact in facts}
    runs = []
    unscoped_spend: dict[str, int] = {}
    for row in run_rows:
        if row["workload_repository"] == "*" and row["run_id"] not in known_runs:
            gaps.append(f"legacy run has unknown repository scope: {row['run_id']}")
            if row["settled_at"] and datetime.fromisoformat(row["settled_at"].replace("Z", "+00:00")) <= window.observation_cutoff:
                key = f"{row['currency']}:{row['basis']}"
                unscoped_spend[key] = unscoped_spend.get(key, 0) + row["amount_micros"]
            continue
        started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")) if row["started_at"] else None
        if row["run_id"] not in known_runs:
            if started is None:
                gaps.append(f"unattributed run has no exposure timestamp: {row['run_id']}")
                continue
            if not (started < window.ended_at and (window.selection == "include_inherited" or started >= window.started_at)):
                continue
            gaps.append(f"run lacks authoritative admission binding: {row['run_id']}")
        cost = None
        if row["settled_at"] and datetime.fromisoformat(row["settled_at"].replace("Z", "+00:00")) <= window.observation_cutoff:
            cost = (row["currency"], row["basis"], row["amount_micros"])
        try:
            dispatch = json.loads(row["dispatch_id"])
        except (TypeError, ValueError):
            dispatch = {}
        if not isinstance(dispatch, dict):
            dispatch = {}
        runs.append(RunFact(row["run_id"], row["config_version"], row["owner_id"], row["kind"], row["state"], cost, dispatch.get("runtime") if isinstance(dispatch.get("runtime"), str) else None, row["model"] if row["model"] != "<runtime-default>" else None, dispatch.get("effort") if isinstance(dispatch.get("effort"), str) else None, row["outcome"]))
    result = cohort_readout(window, tuple(facts), tuple(runs), tuple(parse_observation(json.loads(row[0])) for row in observations), tuple(gaps))
    result["legacy_unknown_repository_spend_micros"] = unscoped_spend
    return result
