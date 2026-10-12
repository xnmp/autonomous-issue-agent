"""Structured-output JSON schemas — verbatim ports of the bash --json-schema blobs.

These are the contract between each agent and the harness (the decision is read
from structured_output, never prose). Kept as plain dicts so they pass straight
into RuntimePlan.schema → ClaudeAgentOptions.output_format.
"""

from __future__ import annotations

from ...domain.effort import TIER_LADDER

# agent/lib.sh FIX_SCHEMA — resolve / resume / address-review / fix-ci.
FIX_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pr_opened", "pushed", "blocked", "failed"]},
        "summary": {"type": "string"},
        "needs": {"type": "string", "description": "if blocked, exactly what is needed"},
    },
    "required": ["status", "ticket_key", "summary"],
}

# Author leg (test/interface writer, when AUTHOR_TESTS_SPLIT is on). The author
# writes interfaces + failing tests and STOPS before the implementation — so its
# terminal status is tests_authored, never pr_opened.
AUTHOR_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["tests_authored", "blocked", "failed"]},
        "summary": {"type": "string"},
        "needs": {"type": "string", "description": "if blocked, exactly what is needed"},
    },
    "required": ["status", "summary"],
}

# Implementer leg (AUTHOR_TESTS_SPLIT). FIX_SCHEMA + a contract_change terminal:
# the implementer may petition that the author's contract (a test / acceptance
# criterion) is genuinely wrong — `needs` carries the structured objection. This
# is adjudicated by a fresh author, NOT honoured blindly (the implementer is not
# judge of its own case).
IMPLEMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["pr_opened", "blocked", "failed", "contract_change"],
        },
        "summary": {"type": "string"},
        "needs": {
            "type": "string",
            "description": (
                "if blocked, what is needed; if contract_change, WHICH test/criterion "
                "is wrong, the correct observable, and why"
            ),
        },
    },
    "required": ["status", "summary"],
}

# Adjudicator leg — a fresh author rules on the implementer's contract objection.
ADJUDICATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["revised", "upheld"]},
        "summary": {"type": "string"},
        "rationale": {"type": "string", "description": "why revised, or why the contract stands"},
    },
    "required": ["verdict", "summary"],
}

# agent/lib.sh ROUTER_SCHEMA — difficulty classifier.
ROUTER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": list(TIER_LADDER)},
        "predicted_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Likely repository-relative files touched; [] when uncertain.",
        },
    },
    "required": ["tier"],
}

# review/lib.sh REVIEW_SCHEMA — review / re-review.
REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "ESCALATE"]},
        "body": {"type": "string"},
    },
    "required": ["decision", "body"],
}


# ── multi-agent review (REVIEW_MODE=multi) ───────────────────────────────────
# The /code-review pattern ported as dispatcher-level orchestration: a cheap
# skip-gate, N parallel dimension specialists that each emit raw candidate
# findings, a per-finding adversarial validator, then a PURE synthesis into the
# same APPROVE/REQUEST_CHANGES/ESCALATE verdict the single-pass reviewer returns.
# These schemas are the contracts for the three LLM legs; the synthesis is code.

# Skip-gate: a cheap classifier deciding whether the PR needs the full review.
# reviewable=False short-circuits to an APPROVE (nothing substantive to review) or
# is used to scale effort. `reason` is surfaced in the log, never to the PR.
REVIEW_GATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reviewable": {"type": "boolean"},
        "tier": {"type": "string", "enum": ["trivial", "standard", "complex"]},
        "reason": {"type": "string"},
    },
    "required": ["reviewable", "tier"],
}

# One dimension specialist's raw candidate findings (pre-validation). Each finding
# is a concrete, refutable claim: a location + the assertion it would fail + a
# severity the synthesis maps to a verdict. `blocking` distinguishes a must-fix
# from an advisory nit so the gate doesn't bounce a PR on style.
REVIEW_FINDINGS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "file:line or file:symbol"},
                    "claim": {"type": "string", "description": "the defect + the assertion it would fail"},
                    "severity": {"type": "string", "enum": ["blocking", "advisory"]},
                },
                "required": ["location", "claim", "severity"],
            },
        },
    },
    "required": ["findings"],
}

# Per-finding adversarial validator: a fresh agent tries to REFUTE one candidate
# finding. `confirmed=True` only if it could NOT refute it (the false-positive
# killer). `verdict` carries the one-line reasoning for the synthesis body.
REVIEW_VALIDATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "verdict": {"type": "string", "description": "why it stands, or why it was refuted"},
    },
    "required": ["confirmed"],
}
