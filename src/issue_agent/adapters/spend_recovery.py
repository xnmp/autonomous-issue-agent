"""Recover measured Codex spend after the owning daemon process is lost."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .codex_runner import (
    discover_rollout_session,
    leg_usage,
    read_rollout_usage,
    recovered_rate_table,
    remove_spend_evidence,
    spend_evidence_path,
    usage_delta,
)
from ..domain.run_usage import EstimatedRunUsage, price_run_usage, total_usage
from .control_store import AccountingDisposition, SQLiteControlStore, SettlementRequest
from ..domain.contracts import ChargeOrigin, PricingBasis, RunId

# The evidence fields recovery prices from. Version 2 (written before native
# sub-agents were disabled) carries the same root coordinates.
_REQUIRED = frozenset({"run_id", "cwd", "rollout_started_at", "session_id", "model", "codex_home",
                       "baseline_session_id", "baseline_usage"})


def spend_evidence_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "spend-evidence") if state_dir else ""


@dataclass(frozen=True)
class CodexSpendReconciler:
    store: SQLiteControlStore
    evidence_dirs: tuple[str, ...]

    def reconcile(self, run_id: RunId) -> bool:
        """Settle a lost Codex run from its rollout, bounded by its recorded
        end; without usable evidence, settle a marked zero estimate."""
        if self.store.leg_runtime(run_id) != "codex":
            return False
        ended_at = self.store.run_completed_at(run_id)
        if ended_at is None:
            return False
        for directory in self.evidence_dirs:
            evidence = self._read(directory, run_id)
            if evidence is None:
                continue
            try:
                session_id = evidence["session_id"] or discover_rollout_session(
                    evidence["cwd"], float(evidence["rollout_started_at"]), evidence["codex_home"] or None)
                rates = recovered_rate_table(evidence.get("rate_table", evidence.get("native_rate_table")))
                usage = read_rollout_usage(session_id, evidence["codex_home"] or None, until=ended_at) \
                    if session_id else None
                baseline = evidence["baseline_usage"] if session_id == evidence["baseline_session_id"] else None
                observed = leg_usage(session_id, evidence["model"], usage_delta(usage, baseline), rates)
                amount = price_run_usage(observed, rates)
                if amount.micros > 2**63 - 1:
                    return self._unavailable(run_id, "request prices exceed SQLite's money bound; unusable requests charged zero")
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return self._unavailable(run_id, "Codex recovery evidence is malformed or unpriceable")
            estimated = isinstance(observed, EstimatedRunUsage)
            request = SettlementRequest(
                run_id,
                amount,
                PricingBasis.ESTIMATE if estimated else PricingBasis.RECONCILED,
                f"codex-recovered:{session_id}:{run_id}",
                total_usage(observed.samples),
                rates.rate_card_id if rates is not None else None,
                rates.version if rates is not None else None,
                observed.detail if isinstance(observed, EstimatedRunUsage) else "",
                ChargeOrigin.RECOVERY_ESTIMATE,
            )
            if self.store.settle(request).disposition is not AccountingDisposition.AVAILABLE:
                return self._unavailable(run_id, "Codex measured recovery settlement was refused")
            remove_spend_evidence(directory, run_id)
            return True
        return self._unavailable(run_id, "Codex recovery evidence is missing or invalid")

    def _unavailable(self, run_id: RunId, reason: str) -> bool:
        return self.store.settle_terminal_estimate(run_id, reason)

    @staticmethod
    def _read(directory: str, run_id: RunId) -> dict | None:
        if not directory:
            return None
        try:
            with open(spend_evidence_path(directory, str(run_id)), encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError):
            return None
        if (not isinstance(value, dict) or value.get("run_id") != str(run_id)
                or value.get("version") not in (2, 3) or not _REQUIRED.issubset(value)
                or not all(isinstance(value[key], str) for key in ("cwd", "session_id", "model", "codex_home",
                                                                   "baseline_session_id"))
                or not (value["baseline_usage"] is None or isinstance(value["baseline_usage"], dict))):
            return None
        return value
