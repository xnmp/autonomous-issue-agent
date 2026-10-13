"""Recover measured Codex spend after the owning daemon process is lost."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .codex_runner import (
    discover_rollout_session,
    read_rollout_usage,
    remove_spend_evidence,
    spend_evidence_path,
    usage_delta,
)
from .codex_child_usage import CodexChildUsageMeter, NativeFailure, recovered_rate_table
from ..domain.pricing import ModelRates, RateTable, UnknownModelPolicy, usd_per_million_to_micros
from ..domain.run_usage import MeasuredRunUsage, RunUsage, EstimatedRunUsage, price_run_usage, total_usage
from .control_store import (
    AccountingDisposition,
    SQLiteControlStore,
    SettlementRequest,
)
from ..domain.contracts import (
    ChargeOrigin,
    PricingBasis,
    RunId,
)


def spend_evidence_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "spend-evidence") if state_dir else ""


@dataclass(frozen=True)
class CodexSpendReconciler:
    store: SQLiteControlStore
    evidence_dirs: tuple[str, ...]

    def reconcile(self, run_id: RunId) -> bool:
        if not self._belongs_to_codex(run_id):
            return False
        ended_at = self.store.run_completed_at(run_id)
        if ended_at is None:
            return False
        for directory in self.evidence_dirs:
            evidence = self._read(directory, run_id)
            if evidence is None:
                continue
            try:
                session_id = self._session_id(evidence)
                native = self._native_usage(evidence, session_id, ended_at)
                rates = self._rates(evidence)
                amount = price_run_usage(native, rates)
                if amount.micros > 2**63 - 1:
                    return self._unavailable(run_id, "request prices exceed SQLite's money bound; unusable requests charged zero")
                if isinstance(native, MeasuredRunUsage):
                    NativeFailure.parse(evidence.get("native_usage_failure"))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return self._unavailable(run_id, "Codex recovery evidence is malformed or unpriceable")
            request = SettlementRequest(
                run_id,
                amount,
                PricingBasis.ESTIMATE if isinstance(native, EstimatedRunUsage) else PricingBasis.RECONCILED,
                f"codex-recovered:{session_id}:{run_id}",
                total_usage(native.samples),
                rates.rate_card_id if rates is not None else None,
                rates.version if rates is not None else None,
                native.detail if isinstance(native, EstimatedRunUsage) else "",
                ChargeOrigin.RECOVERY_ESTIMATE,
            )
            result = self.store.settle(request)
            if result.disposition is not AccountingDisposition.AVAILABLE:
                return self._unavailable(run_id, "Codex measured recovery settlement was refused")
            remove_spend_evidence(directory, run_id)
            return True
        return self._unavailable(run_id, "Codex recovery evidence is missing or invalid")

    def _unavailable(self, run_id: RunId, reason: str) -> bool:
        return self.store.settle_terminal_estimate(run_id, reason)

    def _belongs_to_codex(self, run_id: RunId) -> bool:
        return self.store.leg_runtime(run_id) == "codex"

    def _read(self, directory: str, run_id: RunId) -> dict | None:
        if not directory:
            return None
        try:
            with open(
                spend_evidence_path(directory, str(run_id)), encoding="utf-8"
            ) as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        required = {
            "run_id",
            "cwd",
            "rollout_started_at",
            "cost_per_mtok_in",
            "cost_per_mtok_out",
            "cost_per_mtok_cached_in",
        }
        if (
            not isinstance(value, dict)
            or value.get("run_id") != str(run_id)
            or not required.issubset(value)
        ):
            return None
        version = value.get("version")
        if type(version) is not int or version not in (1, 2):
            return None
        native_fields = {"native_usage_started_at", "native_root_sessions",
                         "native_rate_table", "native_usage_failure"}
        if version == 2:
            if not (native_fields | {"codex_home", "model", "baseline_session_id", "baseline_usage"}).issubset(value):
                return None
            stamp, roots, failure = (value["native_usage_started_at"], value["native_root_sessions"],
                                     value["native_usage_failure"])
            if (type(stamp) not in (int, float) or not 0 < stamp <= 10**12
                    or not isinstance(roots, dict) or len(roots) > 65
                    or not all(isinstance(sid, str) and sid and isinstance(model, str)
                               for sid, model in roots.items())
                    or not isinstance(failure, str) or len(failure) > 2048
                    or not isinstance(value["model"], str) or not isinstance(value["codex_home"], str)):
                return None
        elif native_fields.intersection(value):
            return None
        baseline = value.get("baseline_usage")
        if baseline is not None and (not isinstance(baseline, dict)
                                     or not isinstance(value.get("baseline_session_id"), str)
                                     or not value["baseline_session_id"]):
            return None
        return value

    @staticmethod
    def _session_id(evidence: dict) -> str:
        configured = evidence.get("session_id")
        if isinstance(configured, str) and configured:
            return configured
        return discover_rollout_session(
            str(evidence["cwd"]),
            float(evidence["rollout_started_at"]),
            str(evidence.get("codex_home") or "") or None,
        )

    def _usage(self, evidence: dict, session_id: str, ended_at: float | None = None) -> dict | None:
        if not session_id:
            return None
        usage = read_rollout_usage(
            session_id,
            str(evidence.get("codex_home") or "") or None,
            until=ended_at,
        )
        baseline = evidence.get("baseline_usage")
        return usage_delta(usage, baseline if isinstance(baseline, dict)
                           and session_id == evidence.get("baseline_session_id") else None)

    def _native_usage(self, evidence: dict, session_id: str, ended_at: float | None = None) -> RunUsage:
        roots = evidence.get("native_root_sessions", {})
        if not isinstance(roots, dict) or len(roots) > 65:
            raise ValueError("native recovery roots are malformed")
        if session_id in roots and roots[session_id] != evidence.get("model", ""):
            return EstimatedRunUsage("native recovery root disagrees with its captured model")
        roots = {**roots, session_id: str(evidence.get("model") or "<legacy-parent>")}
        rates = self._rates(evidence)
        meter = CodexChildUsageMeter(str(evidence.get("codex_home") or os.path.expanduser("~/.codex")),
                                    float(evidence.get("native_usage_started_at", evidence["rollout_started_at"])),
                                    rates, str(evidence["cwd"]), ended_at=ended_at)
        meter.seed_baseline(evidence.get("baseline_session_id", ""), evidence.get("baseline_usage"))
        def usage(root: str) -> dict | None:
            try:
                return self._usage(evidence, root, ended_at)
            except (ValueError, TypeError, ArithmeticError):
                meter.failure = "root counter delta malformed; using in-window request evidence"
                return None

        for root, model in roots.items():
            if not isinstance(root, str) or not isinstance(model, str):
                raise ValueError("native recovery identity is malformed")
            meter.sample(root, model, usage(root))
        observed = meter.sample(session_id, roots[session_id], usage(session_id), final=True)
        if ended_at is None and any(r.usage for r in meter.rollouts.values()):
            # An unbounded receipt window could absorb a later leg's responses.
            return EstimatedRunUsage("native re-meter requires the run's recorded end")
        return (EstimatedRunUsage("legacy evidence lacks native child metadata; pricing usable parent usage",
                    tuple(sample for sample in observed.samples if sample.session_id in roots))
                if evidence["version"] == 1 else observed)

    @staticmethod
    def _rates(evidence: dict) -> RateTable | None:
        if evidence["version"] == 2 and evidence["native_rate_table"] is not None:
            return recovered_rate_table(evidence["native_rate_table"])
        inputs, outputs, cached = (usd_per_million_to_micros(evidence[key]) for key in (
            "cost_per_mtok_in", "cost_per_mtok_out", "cost_per_mtok_cached_in"))
        rates = ModelRates(inputs, outputs, cached, inputs)
        return RateTable(evidence.get("rate_card_id") or "codex-config",
            evidence.get("rate_card_version") or evidence.get("config_version") or "legacy",
            "USD", {}, UnknownModelPolicy.USE_FALLBACK, rates)
