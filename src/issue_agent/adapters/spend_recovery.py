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
from .codex_child_usage import CodexChildUsageMeter, NativeFailure, RemeterOutcome, recovered_rate_table
from ..domain.run_usage import MeasuredRunUsage, RunUsage, UnavailableRunUsage, price_run_usage, total_usage
from .control_store import (
    AccountingDisposition,
    SQLiteControlStore,
    SettlementRequest,
)
from ..domain.contracts import (
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
        if self.store.reconcile_review_preflight_stop(run_id):
            return True
        if not self._belongs_to_codex(run_id):
            return False
        for directory in self.evidence_dirs:
            evidence = self._read(directory, run_id)
            if evidence is None:
                continue
            try:
                session_id = self._session_id(evidence)
                ended_at = self.store.run_completed_at(run_id)
                native = self._native_usage(evidence, session_id, ended_at)
                if not isinstance(native, MeasuredRunUsage):
                    return self._unavailable(run_id, "Codex recovery cannot establish complete run usage")
                rates = recovered_rate_table(evidence["native_rate_table"])
                if rates is None:
                    return self._unavailable(run_id, "Codex recovery has no captured price card")
                amount = price_run_usage(native, rates)
                remetered = NativeFailure.parse(evidence.get("native_usage_failure"))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return self._unavailable(run_id, "Codex recovery evidence is malformed or unpriceable")
            request = SettlementRequest(
                run_id,
                amount,
                PricingBasis.RECONCILED,
                f"codex-recovered:{session_id}:{run_id}",
                total_usage(native.samples),
                rates.rate_card_id,
                rates.version,
            )
            result = (self.store.settle(request) if remetered is None
                      else self.store.settle_remetered(request, remetered, RemeterOutcome.MEASURED))
            if result.disposition is not AccountingDisposition.AVAILABLE:
                return self._unavailable(run_id, "Codex measured recovery settlement was refused")
            remove_spend_evidence(directory, run_id)
            return True
        return self._unavailable(run_id, "Codex recovery evidence is missing or invalid")

    def _unavailable(self, run_id: RunId, reason: str) -> bool:
        self.store.record_accounting_unavailable(run_id, reason)
        return False

    def _belongs_to_codex(self, run_id: RunId) -> bool:
        with self.store.transaction() as db:
            row = db.execute("SELECT dispatch_id FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
        if row is None:
            return False
        try:
            metadata = json.loads(row["dispatch_id"])
        except (TypeError, ValueError):
            metadata = None
        runtime = metadata.get("runtime") if isinstance(metadata, dict) else None
        if runtime in ("claude", "glm", "codex"):
            return runtime == "codex"
        # Older runs lack structured dispatch metadata. A per-run file in
        # this harness-owned Codex directory establishes only its provider,
        # never usable counters from malformed content.
        return any(os.path.isfile(spend_evidence_path(directory, str(run_id))) for directory in self.evidence_dirs if directory)

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

    def _native_usage(self, evidence: dict, session_id: str, ended_at: float | None = None) -> RunUsage | None:
        if evidence["version"] == 1:
            return UnavailableRunUsage("legacy evidence lacks native child rates and invocation baselines")
        frozen = evidence.get("native_usage_failure")
        # Ambiguous token counts are determined by the retained rollouts alone,
        # so a meter fix can resolve them; any other frozen failure stays final.
        if frozen and NativeFailure.parse(frozen) is not None and ended_at is None:
            # That class always belongs to a completed run; without the
            # harness-owned end an unbounded re-read could absorb a later leg.
            return UnavailableRunUsage("native re-meter requires the run's recorded end")
        if frozen and NativeFailure.parse(frozen) is None:
            return UnavailableRunUsage(str(evidence["native_usage_failure"])[:2048])
        roots = evidence.get("native_root_sessions")
        if not isinstance(roots, dict) or len(roots) > 65:
            raise ValueError("native recovery roots are malformed")
        if session_id in roots and roots[session_id] != evidence["model"]:
            return UnavailableRunUsage("native recovery root disagrees with its captured model")
        roots = {**roots, session_id: str(evidence.get("model") or "")}
        rates = recovered_rate_table(evidence.get("native_rate_table"))
        if rates is None:
            return UnavailableRunUsage("native recovery lacks its frozen price card")
        meter = CodexChildUsageMeter(str(evidence.get("codex_home") or os.path.expanduser("~/.codex")),
                                    float(evidence["native_usage_started_at"]),
                                    rates, str(evidence["cwd"]), ended_at=ended_at)
        for root, model in roots.items():
            if not isinstance(root, str) or not isinstance(model, str):
                raise ValueError("native recovery identity is malformed")
            usage = self._usage(evidence, root, ended_at)
            observed = meter.sample(root, model, usage)
            if isinstance(observed, UnavailableRunUsage):
                return observed
            # A receipted root needs no counter; the meter records it in `roots` when it is priced.
            if not model or root not in meter.roots:
                return UnavailableRunUsage("native recovery root usage or model is missing")
        observed = meter.sample(session_id, roots[session_id], self._usage(evidence, session_id, ended_at), final=True)
        if ended_at is None and any(r.usage for r in meter.rollouts.values()):
            # An unbounded receipt window could absorb a later leg's responses.
            return UnavailableRunUsage("native re-meter requires the run's recorded end")
        return observed if observed is not None else MeasuredRunUsage(tuple(meter.roots.values()))
