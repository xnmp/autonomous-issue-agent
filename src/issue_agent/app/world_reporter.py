"""Change-only world observation events with control-store dedup authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.events import Correlation, JsonValue
from ..domain.ports import WorldObservationStore
from ..domain.world import IssueView, PRView, WorldState

_MAX_INTERACTIONS = 20
_MAX_INTERACTION_BODY_CHARS = 750
_MAX_PATHS = 100
_MAX_PATH_CHARS = 300
_MAX_CHECKS = 100
_MAX_CHECK_TEXT_CHARS = 200


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
        return {
            "present": True,
            "title": issue.title,
            "state": issue.state,
            "state_reason": issue.state_reason,
            "labels": sorted(label.value for label in issue.labels),
            "updated_at": issue.updated_at,
            "author": issue.author,
            "blocked_by_open": issue.blocked_by_open,
            "open_blockers": list(issue.open_blocker_numbers),
            "declared_paths": _bounded_paths(issue.declared_paths),
            "predicted_paths": _bounded_paths(issue.predicted_paths),
            "comments_total": len(issue.comments),
            "comments": [
                {
                    "author": comment.author,
                    "body": _bounded_body(comment.body),
                    "created_at": comment.created_at,
                }
                for comment in issue.comments[-_MAX_INTERACTIONS:]
            ],
        }

    @staticmethod
    def _pr_facts(pr: PRView) -> dict[str, JsonValue]:
        facts: dict[str, JsonValue] = {
            "present": True,
            "title": pr.title,
            "state": pr.state,
            "labels": sorted(label.value for label in pr.labels),
            "head_sha": pr.head_ref_oid,
            "head_ref": pr.head_ref_name,
            "base_ref": pr.base_ref_name,
            "base_sha": pr.base_ref_oid,
            "draft": pr.is_draft,
            "author": pr.author,
            "closing_issue": pr.closing_issue,
            "changed_files_total": len(pr.changed_files),
            "changed_files": _bounded_paths(pr.changed_files),
            "comments_total": len(pr.comments),
            "comments": [
                {
                    "author": comment.author,
                    "body": _bounded_body(comment.body),
                    "created_at": comment.created_at,
                }
                for comment in pr.comments[-_MAX_INTERACTIONS:]
            ],
            "reviews_total": len(pr.reviews),
            "reviews": [
                {
                    "author": review.author,
                    "state": review.state,
                    "body": _bounded_body(review.body),
                    "submitted_at": review.submitted_at,
                    "commit_id": review.commit_id,
                }
                for review in pr.reviews[-_MAX_INTERACTIONS:]
            ],
        }
        if pr.checks_known:
            facts["checks_known"] = True
            facts["checks_total"] = len(pr.checks)
            facts["checks"] = [
                {
                    "name": check.name[:_MAX_CHECK_TEXT_CHARS],
                    "state": check.state[:_MAX_CHECK_TEXT_CHARS],
                    "bucket": check.bucket[:_MAX_CHECK_TEXT_CHARS],
                    "started_at": check.started_at,
                    "advisory": check.advisory,
                    "run_id": check.run_id,
                }
                for check in pr.checks[:_MAX_CHECKS]
            ]
        if pr.mergeable:
            facts["mergeable"] = pr.mergeable
        if pr.merge_state:
            facts["merge_state"] = pr.merge_state
        return facts

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
    return body[:_MAX_INTERACTION_BODY_CHARS]


def _bounded_paths(paths: frozenset[str]) -> list[str]:
    return [
        path[:_MAX_PATH_CHARS]
        for path in sorted(paths)[:_MAX_PATHS]
    ]


def _content_hash(facts: dict[str, JsonValue]) -> str:
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
