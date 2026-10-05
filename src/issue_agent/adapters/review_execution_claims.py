"""Exact SQL proof of one review parent claim occurrence."""

import json
import sqlite3
from dataclasses import replace

from ..domain.actions import AgentKind
from ..domain.review_preflight import ReviewPreflightSelection
from ..domain.review_rounds import ReviewRoundPlan
from .workflow_codec import decode_workflow
from .workflow_rows import WorkflowAuthorityError, require_review_stage_receipts

_CLAIM_NAMESPACE = "dispatch-claim-lifecycle"
_REVIEW_LEGS = frozenset({AgentKind.REVIEW.name, AgentKind.RE_REVIEW.name})
PREFLIGHT_NAMESPACE = "review-round-preflight"


def load_review_preflight_record(
    db: sqlite3.Connection, repository: str, owner_id: str, request_id: str,
) -> tuple[ReviewPreflightSelection, ReviewRoundPlan, ReviewRoundPlan, sqlite3.Row] | None:
    """One canonical preflight, its evolved plan and exact indexed row."""
    row = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (PREFLIGHT_NAMESPACE, repository, request_id),
    ).fetchone()
    if row is None:
        return None
    stored = decode_workflow(row["payload_json"], tuple)
    return _read_review_preflight_record(db, repository, owner_id, request_id, stored)


def _read_review_preflight_record(
    db: sqlite3.Connection, repository: str, owner_id: str, request_id: str, stored: tuple,
) -> tuple[ReviewPreflightSelection, ReviewRoundPlan, ReviewRoundPlan, sqlite3.Row]:
    """Validate a receipt already read and decoded in the caller's SQL snapshot."""
    if (len(stored) != 3 or not isinstance(stored[0], ReviewPreflightSelection)
            or stored[0].request_id != request_id or stored[1] != owner_id
            or not isinstance(stored[2], ReviewRoundPlan)):
        raise WorkflowAuthorityError("review preflight receipt contains conflicting facts")
    selection, plan = stored[0], stored[2]
    if (plan.round.round_id != request_id or plan.round.binding != selection.binding
            or selection.expected.fence.key.repository != repository):
        raise WorkflowAuthorityError("review preflight receipt disagrees with its round identity")
    round_row = db.execute(
        "SELECT * FROM review_execution_rounds WHERE round_id=?", (request_id,),
    ).fetchone()
    if round_row is None or round_row["claim_occurrence_id"] != selection.claim_occurrence_id:
        raise WorkflowAuthorityError("review preflight receipt lacks its exact durable round")
    current_plan = decode_workflow(round_row["plan_json"], ReviewRoundPlan)
    if replace(plan, stages=current_plan.stages, submissions=current_plan.submissions) != current_plan:
        raise WorkflowAuthorityError("review preflight receipt disagrees with its evolved round plan")
    identity = plan.round
    binding = identity.binding
    if (
        round_row["repository"] != selection.expected.fence.key.repository
        or round_row["target_kind"] != "pr"
        or round_row["target_number"] != selection.expected.fence.key.target.number
        or round_row["execution_epoch"] != binding.execution_epoch
        or round_row["execution_episode"] != binding.execution_episode
        or round_row["generation"] != binding.rejection.fence.generation
        or round_row["sequence"] != identity.sequence
    ):
        raise WorkflowAuthorityError("review preflight receipt disagrees with its round index")
    require_review_stage_receipts(db, repository, current_plan)
    return selection, plan, current_plan, round_row


def require_review_parent_claim(
    db: sqlite3.Connection, repository: str, owner_id: str, occurrence_id: str,
    claim_target: int, *, probed_identity: tuple[int, int] | None = None,
) -> sqlite3.Row:
    """Require the selected live row and immutable creation, without probing OS here."""
    target = f"issue:{claim_target}" if claim_target > 0 else f"pr:{-claim_target}"
    claim = db.execute(
        "SELECT * FROM dispatch_claims WHERE repository=? AND role='review' AND target=?",
        (repository, target),
    ).fetchone()
    if (claim is None or claim["occurrence_id"] != occurrence_id
            or claim["owner_id"] != owner_id or claim["leg"] not in _REVIEW_LEGS):
        raise WorkflowAuthorityError("review parent dispatch claim is absent, changed or unowned")
    if probed_identity is not None and (claim["owner_pid"], claim["owner_start_ticks"]) != probed_identity:
        raise WorkflowAuthorityError("review parent claim process identity changed")
    created = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (_CLAIM_NAMESPACE, repository, f"{occurrence_id}:created"),
    ).fetchone()
    try:
        fact = json.loads(created["payload_json"]) if created is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkflowAuthorityError("review parent claim creation receipt is unreadable") from exc
    if not isinstance(fact, dict) or fact != {
        "role": "review", "target": target, "leg": claim["leg"], "owner_id": claim["owner_id"],
        "owner_pid": claim["owner_pid"], "owner_start_ticks": claim["owner_start_ticks"],
        "claimed_at": claim["claimed_at"], "source": fact.get("source"),
    } or fact["source"] not in ("admission", "v24-migration"):
        raise WorkflowAuthorityError("review parent claim disagrees with its creation receipt")
    if db.execute(
        "SELECT 1 FROM dedupe_keys WHERE namespace=? AND scope=? AND key IN (?,?) LIMIT 1",
        (_CLAIM_NAMESPACE, repository, f"{occurrence_id}:released", f"{occurrence_id}:reclaimed"),
    ).fetchone() is not None:
        raise WorkflowAuthorityError("review parent claim has a contradictory terminal receipt")
    return claim
