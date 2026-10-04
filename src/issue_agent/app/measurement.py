"""Local operator recording and deterministic read-only measurement export."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

from ..adapters.control_store import SQLiteControlStore
from ..adapters.measurement import export_cohort, read_input, record_observation
from ..domain.cohort import CohortWindow
from ..domain.measurement import parse_observation, stamp


def make_window(repository: str, raw: dict) -> CohortWindow:
    allowed = {"started_at", "ended_at", "observation_cutoff", "selection", "expected_config_version", "supported_profile", "primary_family", "inherited_binding_refs"}
    if set(raw) - allowed:
        raise ValueError("unknown cohort specification fields")
    profile = raw.get("supported_profile")
    if not isinstance(profile, dict) or not profile:
        raise ValueError("supported_profile must explicitly describe the expected deployment posture")
    required_profile = {"deployment", "posture", "config_version"}
    if not required_profile <= set(profile) or set(profile) - required_profile - {"difficulty", "runtime", "model", "runtimes", "models"}:
        raise ValueError("supported_profile requires deployment/posture/config_version and runtime/model selectors")
    mixes = {}
    scalars = dict(profile)
    for singular, plural in (("runtime", "runtimes"), ("model", "models")):
        if (singular in profile) == (plural in profile):
            raise ValueError(f"select exactly one of {singular}/{plural}")
        if plural in profile:
            values = profile[plural]
            if not isinstance(values, list) or not 1 <= len(values) <= 16 or any(not isinstance(value, str) or not value.strip() or len(value.encode()) > 2048 for value in values) or len(set(values)) != len(values):
                raise ValueError(f"{plural} requires 1 to 16 unique text values")
            mixes[plural] = tuple(sorted(values))
            scalars.pop(plural)
    for key, value in scalars.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or not value or len(key) > 100 or len(value.encode()) > 2048:
            raise ValueError("supported_profile requires bounded text fields")
    if len(profile) > 16:
        raise ValueError("supported_profile allows at most 16 fields")
    inherited = raw.get("inherited_binding_refs", [])
    if not isinstance(inherited, list):
        raise ValueError("inherited_binding_refs must be a list")
    return CohortWindow(repository, stamp(raw.get("started_at"), "started_at"), stamp(raw.get("ended_at"), "ended_at"), stamp(raw.get("observation_cutoff"), "observation_cutoff"), raw.get("selection", "new_admissions"), raw.get("expected_config_version"), tuple(sorted(scalars.items())), raw.get("primary_family", "resolve"), tuple(inherited), mixes.get("runtimes", ()), mixes.get("models", ()))


def local_export(database: str, repository: str, specification: dict) -> dict:
    window = make_window(repository, specification)
    result = export_cohort(database, window)
    result["expected_supported_profile"] = specification["supported_profile"]
    result["expected_configuration_version"] = window.expected_config_version
    return result


def cmd_measurement(args: argparse.Namespace) -> int:
    try:
        if args.measurement_action == "record":
            record = parse_observation(read_input(args.input))
            store = SQLiteControlStore(args.database)
            # Recording requires an already initialized authority database;
            # measurement must never activate or migrate it.
            inserted = record_observation(store, record, deployment=args.deployment, now=datetime.now(timezone.utc))
            result = {"record_id": record.record_id, "repository": record.repository, "recorded": inserted, "telemetry": "durable_outbox"}
        else:
            result = local_export(args.database, args.repo, read_input(args.specification))
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"measurement: {exc}", file=__import__("sys").stderr)
        return 1


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("measurement", help="record observations or export a local cohort")
    actions = parser.add_subparsers(dest="measurement_action", required=True)
    record = actions.add_parser("record", help="append one incident/intervention observation")
    record.add_argument("--database", required=True)
    record.add_argument("--deployment", required=True)
    record.add_argument("--input", required=True, help="bounded JSON observation")
    record.set_defaults(func=cmd_measurement)
    export = actions.add_parser("export", help="export an explicit comparable-cohort readout")
    export.add_argument("--database", required=True)
    export.add_argument("--repo", required=True)
    export.add_argument("--specification", required=True, help="UTC window and expected supported profile JSON")
    export.set_defaults(func=cmd_measurement)
