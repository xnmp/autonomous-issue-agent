"""Recover measured Codex spend after the owning daemon process is lost."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .codex_runner import (
    codex_cost_usd,
    discover_rollout_session,
    read_rollout_usage,
    remove_spend_evidence,
    spend_evidence_path,
    usage_delta,
)
from .control_store import (
    AccountingDisposition,
    SQLiteControlStore,
    SettlementRequest,
)
from ..domain.contracts import (
    Money,
    PricingBasis,
    RunId,
    TokenUsage,
    UsageSource,
)


def spend_evidence_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "spend-evidence") if state_dir else ""


@dataclass(frozen=True)
class CodexSpendReconciler:
    store: SQLiteControlStore
    evidence_dirs: tuple[str, ...]

    def reconcile(self, run_id: RunId) -> bool:
        for directory in self.evidence_dirs:
            evidence = self._read(directory, run_id)
            if evidence is None:
                continue
            try:
                session_id = self._session_id(evidence)
                usage = self._usage(evidence, session_id)
                if usage is None:
                    return False
                amount = Money(
                    "USD",
                    int(
                        (
                            Decimal(
                                str(
                                    codex_cost_usd(
                                        usage,
                                        cost_per_mtok_in=float(
                                            evidence["cost_per_mtok_in"]
                                        ),
                                        cost_per_mtok_out=float(
                                            evidence["cost_per_mtok_out"]
                                        ),
                                        cost_per_mtok_cached_in=float(
                                            evidence["cost_per_mtok_cached_in"]
                                        ),
                                    )
                                )
                            )
                            * 1_000_000
                        )
                        .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                    ),
                )
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return False
            result = self.store.settle(
                SettlementRequest(
                    run_id,
                    amount,
                    PricingBasis.RECONCILED,
                    f"codex-recovered:{session_id}:{run_id}",
                    _token_usage(usage),
                    "codex-config",
                    str(evidence.get("config_version") or "recovered"),
                )
            )
            if result.disposition is not AccountingDisposition.AVAILABLE:
                return False
            remove_spend_evidence(directory, run_id)
            return True
        return False

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

    def _usage(self, evidence: dict, session_id: str) -> dict | None:
        if not session_id:
            return None
        usage = read_rollout_usage(
            session_id,
            str(evidence.get("codex_home") or "") or None,
        )
        baseline = evidence.get("baseline_usage")
        return usage_delta(usage, baseline if isinstance(baseline, dict) else None)


def _token_usage(raw: dict) -> TokenUsage:
    input_total = max(0, int(raw.get("input_tokens", 0) or 0))
    cache_read = max(0, int(raw.get("cache_read_input_tokens", 0) or 0))
    cache_write = max(
        0, int(raw.get("cache_creation_input_tokens", 0) or 0)
    )
    return TokenUsage(
        max(0, input_total - cache_read - cache_write),
        max(0, int(raw.get("output_tokens", 0) or 0)),
        min(cache_read, input_total),
        min(cache_write, max(0, input_total - cache_read)),
        UsageSource.RECONCILED,
    )
