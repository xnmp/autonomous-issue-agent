"""Change-only world observation events with control-store dedup authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass

from ..domain.ci import ci_state, pending_checks, reduce_ci, Waiting, MissingRequired
from ..domain.checks import Observed, Unavailable
from ..domain.events import Correlation, JsonValue, MAX_EVENT_LINE_BYTES
from ..domain.ports import WorldObservationStore
from ..domain.world import IssueView, PRView, WorldState

_MAX_INTERACTIONS = 20
_MAX_INTERACTION_BODY_CHARS = 750
_MAX_PATHS = 100
_MAX_PATH_CHARS = 300
_MAX_LABELS = 100
_MAX_OPEN_BLOCKERS = 100
_MAX_FACTS_BYTES = MAX_EVENT_LINE_BYTES - 4 * 1024
_MAX_GENERAL_TEXT_JSON_BYTES = 1_024
_MAX_BODY_JSON_BYTES = 768
_MAX_PATH_JSON_BYTES = 384
_COMPACT_COLLECTION_ITEMS = 5
_COLLECTION_KEYS = (
    "comments",
    "reviews",
    "changed_files",
    "declared_paths",
    "predicted_paths",
    "open_blockers",
    "labels",
    "checks",
)


def _ignore_log(_message: str) -> None:
    pass


@dataclass
class WorldReporter:
    control: WorldObservationStore
    deployment: str
    log: Callable[[str], None] = _ignore_log

    def wrap(self, observe: Callable[[], WorldState]) -> Callable[[], WorldState]:
        def reported() -> WorldState:
            world = observe()
            self._report_snapshot(
                "issue",
                {
                    issue.number: (
                        self._issue_facts(issue),
                        Correlation(issue=issue.number),
                    )
                    for issue in world.issues
                },
            )
            self._report_snapshot(
                "pr",
                {
                    pr.number: (
                        self._pr_facts(pr),
                        Correlation(pr=pr.number),
                    )
                    for pr in world.prs
                },
            )
            return world

        return reported

    @staticmethod
    def _issue_facts(issue: IssueView) -> dict[str, JsonValue]:
        return _fit_facts({
            "present": True,
            "title": _bounded_text(issue.title),
            "state": _bounded_text(issue.state),
            "state_reason": _bounded_text(issue.state_reason),
            "labels": [
                _bounded_text(label.value)
                for label in sorted(issue.labels, key=lambda item: item.value)[
                    :_MAX_LABELS
                ]
            ],
            "updated_at": _bounded_text(issue.updated_at),
            "author": _bounded_text(issue.author),
            "blocked_by_open": issue.blocked_by_open,
            "open_blockers": list(
                issue.open_blocker_numbers[:_MAX_OPEN_BLOCKERS]
            ),
            "declared_paths": _bounded_paths(issue.declared_paths),
            "predicted_paths": _bounded_paths(issue.predicted_paths),
            "comments_total": len(issue.comments),
            "comments": [
                {
                    "author": _bounded_text(comment.author),
                    "body": _bounded_body(comment.body),
                    "created_at": _bounded_text(comment.created_at),
                }
                for comment in issue.comments[-_MAX_INTERACTIONS:]
            ],
        })

    def _pr_facts(self, pr: PRView) -> dict[str, JsonValue]:
        facts: dict[str, JsonValue] = {
            "present": True,
            "title": _bounded_text(pr.title),
            "state": _bounded_text(pr.state),
            "labels": [
                _bounded_text(label.value)
                for label in sorted(pr.labels, key=lambda item: item.value)[
                    :_MAX_LABELS
                ]
            ],
            "head_sha": _bounded_text(pr.head_ref_oid),
            "head_ref": _bounded_text(pr.head_ref_name),
            "base_ref": _bounded_text(pr.base_ref_name),
            "base_sha": _bounded_text(pr.base_ref_oid),
            "draft": pr.is_draft,
            "author": _bounded_text(pr.author),
            "closing_issue": pr.closing_issue,
        }
        # Absent ≠ empty, exactly as for checks below: a terminal PR built from
        # the list payload (#163) never had these read, and publishing zeros for
        # them would rewrite a merged PR's history on the dashboard as "no
        # files, no comments, no reviews" (adversarial review F8).
        if pr.content_known:
            facts["content_known"] = True
            facts["changed_files_total"] = len(pr.changed_files)
            facts["changed_files"] = _bounded_paths(pr.changed_files)
            facts["comments_total"] = len(pr.comments)
            facts["comments"] = [
                {
                    "author": _bounded_text(comment.author),
                    "body": _bounded_body(comment.body),
                    "created_at": _bounded_text(comment.created_at),
                }
                for comment in pr.comments[-_MAX_INTERACTIONS:]
            ]
            facts["reviews_total"] = len(pr.reviews)
            facts["reviews"] = [
                {
                    "author": _bounded_text(review.author),
                    "state": _bounded_text(review.state),
                    "body": _bounded_body(review.body),
                    "submitted_at": _bounded_text(review.submitted_at),
                    "commit_id": _bounded_text(review.commit_id),
                }
                for review in pr.reviews[-_MAX_INTERACTIONS:]
            ]
        if isinstance(pr.ci, Observed):
            # Classification and rerun identity must survive lossy payload compaction.
            facts["ci_summary"] = {
                "state": ci_state(pr.ci).value,
                "basis": "required-checks",
                "tested_revision": pr.ci.tested_revision,
                "activity": "pending" if pending_checks(pr.checks) else "settled",
                "running": _content_hash({"runs": sorted(
                    json.dumps([check.name, check.started_at, check.run_id])
                    for check in pending_checks(pr.checks)
                )}),
            }
            facts["checks_total"] = len(pr.checks)
            facts["checks"] = [
                {key: value for key, value in asdict(check).items()
                 if key in {"name", "state", "bucket", "required", "app_id", "kind"}}
                for check in pr.checks
            ]
            reduction = reduce_ci(pr.ci)
            if isinstance(reduction, Waiting) and isinstance(reduction.reason, MissingRequired):
                facts["missing_required"] = [
                    f"{r.context} (app {r.app_id})" if r.app_id else r.context
                    for r in reduction.reason.requirements[:_MAX_PATHS]]
        elif pr.ci.reason != "checks not requested":
            facts["ci_unavailable"] = pr.ci.reason
        if pr.mergeable:
            facts["mergeable"] = _bounded_text(pr.mergeable)
        if pr.merge_state:
            facts["merge_state"] = _bounded_text(pr.merge_state)
        return _fit_facts(facts)

    def _report_snapshot(
        self,
        subject_kind: str,
        values: dict[
            int,
            tuple[dict[str, JsonValue], Correlation],
        ],
    ) -> None:
        observations = {
            number: (_content_hash(facts), facts, correlation)
            for number, (facts, correlation) in values.items()
        }
        try:
            self.control.record_world_snapshot(
                deployment=self.deployment,
                subject_kind=subject_kind,
                observations=observations,
            )
        except Exception as exc:  # telemetry cannot fail the world read
            self.log(f"world observation event dropped: {exc}")


def _bounded_body(body: str) -> str:
    return _bounded_text(
        body[:_MAX_INTERACTION_BODY_CHARS],
        _MAX_BODY_JSON_BYTES,
    )


def _bounded_paths(paths: frozenset[str]) -> list[str]:
    return [
        _bounded_text(path[:_MAX_PATH_CHARS], _MAX_PATH_JSON_BYTES)
        for path in sorted(paths)[:_MAX_PATHS]
    ]


def _bounded_text(
    value: str,
    maximum_json_bytes: int = _MAX_GENERAL_TEXT_JSON_BYTES,
) -> str:
    if len(json.dumps(value).encode()) <= maximum_json_bytes:
        return value
    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(value[:middle]).encode()) <= maximum_json_bytes:
            low = middle
        else:
            high = middle - 1
    return value[:low]


def _fit_facts(facts: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if _encoded_size(facts) <= _MAX_FACTS_BYTES:
        return facts
    compacted = dict(facts)
    compacted["observation_truncated"] = True
    for key in _COLLECTION_KEYS:
        value = compacted.get(key)
        if isinstance(value, list) and len(value) > _COMPACT_COLLECTION_ITEMS:
            if key in {"comments", "reviews"}:
                compacted[key] = value[-_COMPACT_COLLECTION_ITEMS:]
            else:
                compacted[key] = value[:_COMPACT_COLLECTION_ITEMS]
        if _encoded_size(compacted) <= _MAX_FACTS_BYTES:
            return compacted
    for key in _COLLECTION_KEYS:
        if isinstance(compacted.get(key), list):
            compacted[key] = []
        if _encoded_size(compacted) <= _MAX_FACTS_BYTES:
            return compacted
    raise ValueError("bounded world facts exceed the event safety budget")


def _encoded_size(facts: dict[str, JsonValue]) -> int:
    return len(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode())


def _content_hash(facts: dict[str, JsonValue]) -> str:
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
