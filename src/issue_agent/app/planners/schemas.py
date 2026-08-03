"""Structured-output JSON schemas — verbatim ports of the bash --json-schema blobs.

These are the contract between each agent and the harness (the decision is read
from structured_output, never prose). Kept as plain dicts so they pass straight
into RuntimePlan.schema → ClaudeAgentOptions.output_format.
"""

from __future__ import annotations

from collections.abc import Sequence

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
        "tier": {"type": "string", "enum": ["trivial", "standard", "complex"]},
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


def review_schema_with_exemptions(
    declarations: Sequence[tuple[str, str]],
) -> dict:
    """REVIEW_SCHEMA extended with `exemption_grants` (ADR 0014), constrained
    so an invalid grant is UNREPRESENTABLE: the field exists only when the PR
    actually declares exemptions, and each entry is an object-level enum over
    exactly the declared {rule, subject} pairs — the model cannot emit an
    undeclared rule, a cross-matched subject, or free text. Granting is a
    structured decision, never prose: the harness renders the authenticated
    grant lines itself, binding the patch id the model never sees (round-2
    adversarial review showed string-matching authority out of free text
    survives fence-quoting tricks). Harness-side validation in the review
    workflow stays as defense in depth for runtimes that enforce schemas
    loosely."""
    allowed = [
        {"rule": rule, "subject": subject} for rule, subject in declarations
    ]
    return {
        **REVIEW_SCHEMA,
        "properties": {
            **REVIEW_SCHEMA["properties"],
            "exemption_grants": {
                "type": "array",
                "items": {"enum": allowed},
                "description": (
                    "Gate-exemption declarations you GRANT, verbatim from "
                    "the declared list in the prompt. Only meaningful with "
                    "decision APPROVE. Omit or [] to refuse; refusal needs "
                    "no entry."
                ),
            },
        },
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


# steward.sh TRIAGE_SCHEMA — requirements triage. Gaps are split by AUDIENCE:
# creator_questions are plain-language and answerable by a (possibly non-technical)
# issue author; owner_questions are technical/architectural decisions a repo
# maintainer must make (e.g. "add a User to the domain model", pick an auth model,
# approve a schema change) and must NOT be put to the author. Either list non-empty
# → blocked. verdict="close" is the third disposition: the issue needs no work at
# all, and the steward closes it itself rather than paying a resolve leg to post a
# comment (keifu #143).
TRIAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ready", "blocked", "close"]},
        "plain_summary": {"type": "string"},
        # verdict="close" only — the issue needs no work at all. Both of these are
        # required BY THE STEWARD (not by the schema, which would only pressure a
        # ready/blocked verdict into inventing them): a close missing either one
        # is refused and handed back to a human.
        "close_reason": {"type": "string"},
        "reopen_if": {"type": "string"},
        # GitHub stateReason. "completed" claims the reported work actually
        # shipped; anything else (including absent) is treated as not_planned.
        "close_state_reason": {
            "type": "string",
            "enum": ["completed", "not_planned"],
        },
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        # Plain-language clarifications the issue author can answer: what the feature
        # should do, which behaviour they want, scope — no code/architecture terms.
        "creator_questions": {"type": "array", "items": {"type": "string"}},
        # Technical / architectural decisions only a repo owner should make: domain-
        # model changes, new cross-cutting concepts, API/auth model choices — anything
        # that commits the codebase's shape. Never asked of the author.
        "owner_questions": {"type": "array", "items": {"type": "string"}},
        # Numbers of ALREADY-OPEN issues this one is blocked by (a hard prerequisite
        # that must land first). VERIFIED via issue search — never invented. The
        # steward turns each into a native GitHub blocked_by link so the fixer's
        # dependency gate holds this issue until the blocker closes.
        "blocked_by_issues": {"type": "array", "items": {"type": "integer"}},
        # ADVISORY (harness #10): the number of a RECENTLY-MERGED issue (from the
        # list supplied in the prompt) that this issue re-reports — the same bug
        # back, or a shipped feature that does not work. Omitted/null when this is
        # not a recurrence, which is the default. Not `required`: a classifier that
        # must emit the field on every issue is a classifier under pressure to
        # invent one. The steward ignores any number not in the supplied list.
        "recurrence_of": {"type": "integer"},
        "rationale": {"type": "string"},
    },
    "required": [
        "verdict", "plain_summary", "acceptance_criteria",
        "creator_questions", "owner_questions",
    ],
}

# Steward grooming is deliberately a narrow advisory classifier. The optional
# fields keep the default verdict "no finding", avoiding pressure to invent a
# match from attacker-controlled issue text.
GROOM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "resolved_by": {"type": "integer"},
        "stale_reference": {"type": "string"},
    },
}

# JIRA_SYNC_SCHEMA — the steward mirrors a GitHub issue into a Jira ticket.
# created → a new ticket was made (ticket_key set); transitioned → an existing
# ticket moved status; nothing → already in the right state; failed → MCP/auth
# error (the dispatcher then persists nothing and retries next poll).
JIRA_SYNC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["created", "transitioned", "nothing", "failed"]},
        "ticket_key": {"type": "string", "description": "the Jira key, e.g. ABC-123 (set on create)"},
        "summary": {"type": "string"},
    },
    "required": ["status", "summary"],
}
