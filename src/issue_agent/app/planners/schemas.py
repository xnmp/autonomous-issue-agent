"""Structured-output JSON schemas — verbatim ports of the bash --json-schema blobs.

These are the contract between each agent and the harness (the decision is read
from structured_output, never prose). Kept as plain dicts so they pass straight
into RuntimePlan.schema → ClaudeAgentOptions.output_format.
"""

from __future__ import annotations


# agent/lib.sh FIX_SCHEMA — resolve / resume / address-review / fix-ci.
FIX_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pushed", "blocked", "failed"]},
        "summary": {"type": "string"},
        "needs": {"type": "string", "description": "if blocked, exactly what is needed"},
        # Not a question for a human: the harness files it in its own repository (domain.gaps).
        "harness_issue": {
            "type": "object",
            "description": "a defect in what the harness gives you (sandbox, toolchain, credentials, rules), "
                           "never the repository's code; the harness files it",
            "properties": {
                "title": {"type": "string", "description": "one line naming the defect"},
                "body": {"type": "string", "description": "the reproduction: the command you ran and its output"},
            },
            "required": ["title", "body"],
        },
    },
    "required": ["status", "summary"],
}

# The REVIEW leg's verdict claim, parsed by `domain.review.parse_verdict`.
REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
        "body": {"type": "string"},
    },
    "required": ["decision", "body"],
}
