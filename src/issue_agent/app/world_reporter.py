"""Change-only world observation events with control-store dedup authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.events import Correlation, JsonValue
from ..domain.ports import WorldObservationStore
from ..domain.world import IssueView, PRView, WorldState


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
            for issue in world.issues:
                self._report_issue(issue)
            for pr in world.prs:
                self._report_pr(pr)
            return world

        return reported

    def _report_issue(self, issue: IssueView) -> None:
        facts = {
            "title": issue.title,
            "state": issue.state,
            "state_reason": issue.state_reason,
            "labels": sorted(label.value for label in issue.labels),
            "updated_at": issue.updated_at,
            "blocked_by_open": issue.blocked_by_open,
            "open_blockers": list(issue.open_blocker_numbers),
        }
        self._emit("issue", issue.number, facts, Correlation(issue=issue.number))

    def _report_pr(self, pr: PRView) -> None:
        facts = {
            "title": pr.title,
            "state": pr.state,
            "labels": sorted(label.value for label in pr.labels),
            "head_sha": pr.head_ref_oid,
            "head_ref": pr.head_ref_name,
            "base_ref": pr.base_ref_name,
            "checks_known": pr.checks_known,
            "checks": [
                {"name": check.name, "state": check.state, "bucket": check.bucket}
                for check in pr.checks
            ],
            "mergeable": pr.mergeable,
            "merge_state": pr.merge_state,
            "draft": pr.is_draft,
            "closing_issue": pr.closing_issue,
        }
        self._emit("pr", pr.number, facts, Correlation(pr=pr.number))

    def _emit(
        self,
        subject_kind: str,
        number: int,
        facts: dict[str, JsonValue],
        correlation: Correlation,
    ) -> None:
        encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(encoded.encode()).hexdigest()
        try:
            self.control.record_world_observation(
                deployment=self.deployment,
                subject_kind=subject_kind,
                subject_number=number,
                content_hash=content_hash,
                facts=facts,
                correlation=correlation,
            )
        except Exception as exc:  # telemetry cannot fail the world read
            self.log(f"world observation event dropped: {exc}")
