"""Deterministic full-stack server for Playwright."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import uvicorn

from fastapi import HTTPException

from issue_agent.app.dashweb import resolve_contracts as rc
from issue_agent.app.dashweb import review_contracts as wc
from issue_agent.app.dashweb.server import create_app

# Fixture-only repository slug for the resolve-control state machine; must be
# canonical (see canonical_workflow_repository) or every ResolveExpected
# constructed below fails validation.
RESOLVE_REPO = "openai/autonomous-issue-agent"

# Issue numbers with a fixed, non-default resolve_control()/resolve_command()
# shape. Any other issue number (e.g. 60, 63-69 used by the e2e spec) gets a
# fresh, lazily-created "available" state machine keyed by that number, so
# parallel Playwright tests stay isolated just by using distinct issue numbers.
INACTIVE_ISSUE = 61
UNAVAILABLE_ISSUE = 62
# An expired command whose harness read proved current absence: its gate
# offers exactly one retry authorization until one is recorded.
RETRY_ISSUE = 71


@dataclass
class _ResolveState:
    """One issue's in-memory resolve authority. Frozen contract objects are
    replaced wholesale (model_copy) rather than mutated in place."""

    number: int
    epoch: int = 1
    generation: int = 1
    version: int = 0
    guidance: str = ""
    failures: int = 2
    current_intent: rc.Rearm | None = None
    # All Rearm records ever produced, keyed by their own request_id — the
    # historical-lookup index (GET ?request_id=...) and the source continue_rearm
    # reads from (it flips the STOPPED record's state; it never mints a new one).
    rearms: dict = field(default_factory=dict)
    # The authorization_id recorded for the retry gate, if any (RETRY_ISSUE).
    retry_authorization: str | None = None
    # Command identity (request_id, or cancellation_id for cancel_rearm) ->
    # (dumped request body, prior CommandResult) for exact-retry replay.
    processed: dict = field(default_factory=dict)

    @property
    def episode_id(self) -> str:
        return f"episode-{self.number}"

    @property
    def gate_id(self) -> str:
        return f"gate-{self.number}"


class ResolveFixture:
    """Deterministic resolve-control/command state machine, per docs/... none —
    behavior specified inline by dashboard/e2e/resolve-control.spec.ts. Kept
    intentionally simple: a test double, not production logic."""

    def __init__(self) -> None:
        self._states: dict[int, _ResolveState] = {}

    def _state(self, number: int) -> _ResolveState:
        return self._states.setdefault(number, _ResolveState(number=number))

    def _expected(self, state: _ResolveState) -> rc.ResolveExpected:
        return rc.ResolveExpected(
            repository=RESOLVE_REPO, issue=state.number, family="resolve",
            epoch=state.epoch, generation=state.generation, version=state.version,
            episode_id=state.episode_id,
        )

    def _gate(self, state: _ResolveState) -> rc.Gate:
        intent = state.current_intent
        if intent is None:
            available = ["request_rearm"]
            gate_state = "open"
        elif intent.state == "pending":
            available = ["cancel_rearm"]
            gate_state = "rearming"
        else:  # cancelled or superseded
            available = ["continue_rearm", "request_rearm"]
            gate_state = "open"
        cause = rc.ResolveCeiling(
            kind="resolve_ceiling", episode_id=state.episode_id,
            failures=state.failures, ceiling=3,
            last_failure="probe timed out waiting for workspace readiness",
        )
        return rc.Gate(
            gate_id=state.gate_id, state=gate_state, opened_generation=state.generation,
            cause=cause,
            remediation=rc.NoRemediation(kind="none_authorized", reason="Awaiting an operator decision."),
            available_commands=available, pending_retry=None,
        )

    def _retry_gate(self, state: _ResolveState) -> rc.Gate:
        return rc.Gate(
            gate_id=f"uncertain-{state.number}", state="open", opened_generation=state.generation,
            cause=rc.RetryAuthorizationRequired(
                kind="retry_authorization_required", command_id=f"command-{state.number}",
                absence_evidence_id=f"absence-{state.number}",
            ),
            remediation=rc.AttemptedRemediation(
                kind="attempted", operation="reconcile_remote",
                evidence_ids=[f"absence-{state.number}"], outcome="verified_absent",
            ),
            available_commands=[] if state.retry_authorization else ["authorize_retry"],
            pending_retry=rc.PendingRetry(
                authorization_id=state.retry_authorization, command_id=f"command-{state.number}",
            ) if state.retry_authorization else None,
        )

    def _control(self, state: _ResolveState, request_id: str | None) -> rc.AvailableControl:
        requested_intent = state.rearms.get(request_id) if request_id else None
        gates = [self._retry_gate(state)] if state.number == RETRY_ISSUE else [self._gate(state)]
        return rc.AvailableControl(
            status="available", expected=self._expected(state), workflow_held=True,
            gates=gates, guidance=state.guidance, failures=state.failures,
            current_intent=state.current_intent, requested_id=request_id,
            requested_intent=requested_intent, observation=None,
        )

    def control(self, number: int, request_id: str | None) -> rc.ResolveControl:
        if number == INACTIVE_ISSUE:
            return rc.InactiveControl(status="inactive", repository=RESOLVE_REPO, issue=number)
        if number == UNAVAILABLE_ISSUE:
            return rc.UnavailableControl(status="unavailable", reason="resolve control unavailable (fixture)")
        return self._control(self._state(number), request_id)

    def _matches_expected(self, state: _ResolveState, expected: rc.ResolveExpected) -> bool:
        return (
            expected.repository == RESOLVE_REPO and expected.issue == state.number
            and expected.family == "resolve" and expected.epoch == state.epoch
            and expected.generation == state.generation and expected.version == state.version
            and expected.episode_id == state.episode_id
        )

    def command(self, number: int, body: rc.ResolveCommand) -> rc.CommandResult:
        if number == INACTIVE_ISSUE:
            return rc.CommandResult(outcome="unavailable", reason="Resolve authority is inactive.",
                control=rc.InactiveControl(status="inactive", repository=RESOLVE_REPO, issue=number))
        if number == UNAVAILABLE_ISSUE:
            return rc.CommandResult(outcome="unavailable", reason="resolve control unavailable (fixture)",
                control=rc.UnavailableControl(status="unavailable", reason="resolve control unavailable (fixture)"))

        state = self._state(number)
        identity = (body.cancellation_id if body.operation == "cancel_rearm"
                    else body.authorization_id if body.operation == "authorize_retry" else body.request_id)
        dumped = body.model_dump(mode="json")

        prior = state.processed.get(identity)
        if prior is not None:
            prior_body, prior_result = prior
            if prior_body == dumped:
                return rc.CommandResult(outcome="replayed", reason="This exact command was already recorded.",
                    control=self._control(state, None))
            return rc.CommandResult(outcome="conflict",
                reason="This identifier was already used for a different command.",
                control=self._control(state, None))

        if not self._matches_expected(state, body.expected):
            return rc.CommandResult(outcome="conflict",
                reason="Resolve status changed since this command was prepared.",
                control=self._control(state, None))

        if body.operation == "authorize_retry":
            gate = self._retry_gate(state) if state.number == RETRY_ISSUE else None
            if gate is None or "authorize_retry" not in gate.available_commands:
                outcome = "conflict" if state.retry_authorization else "rejected"
                return rc.CommandResult(outcome=outcome, reason="No retry authorization is available for this gate.",
                    control=self._control(state, None))
            cause = gate.cause
            assert isinstance(cause, rc.RetryAuthorizationRequired)
            if (body.gate_id, body.command_id, body.evidence_id) != (gate.gate_id, cause.command_id, cause.absence_evidence_id):
                return rc.CommandResult(outcome="rejected", reason="The gate does not await this retry.",
                    control=self._control(state, None))
            # One execution of the existing command; generation, episode and
            # allowance are unchanged.
            state.retry_authorization = body.authorization_id
            state.version += 1
            result = rc.CommandResult(outcome="accepted",
                reason="One retry authorized; the daemon executes it after a fresh read.",
                control=self._control(state, None))
            state.processed[identity] = (dumped, result)
            return result

        intent = state.current_intent
        if body.operation == "request_rearm":
            if intent is not None and intent.state == "pending":
                return rc.CommandResult(outcome="conflict", reason="A re-arm request is already pending.",
                    control=self._control(state, None))
            guidance = body.guidance
            if guidance.kind == "set":
                state.guidance = guidance.text
            elif guidance.kind == "clear":
                state.guidance = ""
            new_intent = rc.Rearm(
                request_id=body.request_id, state="pending", gate_id=body.gate_id,
                generation=state.generation, grant_request_id=body.request_id,
                episode_id=state.episode_id, handoff_id=f"handoff-{body.request_id}",
                branch=f"resolve/{number}/{body.request_id[:8]}",
                base_ref="refs/heads/main", base_commit="0" * 40,
                required_commands=["prepare_workspace"], verified_commands=[],
                retired_commands=[], reason="",
            )
            state.current_intent = new_intent
            state.rearms[new_intent.request_id] = new_intent
            state.version += 1
            result = rc.CommandResult(outcome="accepted",
                reason="Re-arm requested; not yet completed.", control=self._control(state, None))
            state.processed[identity] = (dumped, result)
            return result

        if body.operation == "cancel_rearm":
            if intent is None or intent.request_id != body.request_id or intent.state != "pending":
                return rc.CommandResult(outcome="conflict", reason="No matching pending request to cancel.",
                    control=self._control(state, None))
            cancelled = intent.model_copy(update={"state": "cancelled", "reason": body.reason})
            state.current_intent = cancelled
            state.rearms[cancelled.request_id] = cancelled
            state.version += 1
            result = rc.CommandResult(outcome="accepted", reason="Cancellation recorded.",
                control=self._control(state, None))
            state.processed[identity] = (dumped, result)
            return result

        # continue_rearm: revives the STOPPED record under its own original
        # request_id; the continue command's own request_id is only a command
        # identity for replay dedup, never a new intent identity.
        stopped = state.rearms.get(body.stopped_request_id)
        if (
            stopped is None or intent is None or intent.request_id != body.stopped_request_id
            or intent.state not in ("cancelled", "superseded")
        ):
            return rc.CommandResult(outcome="conflict", reason="No matching stopped request to continue.",
                control=self._control(state, None))
        revived = intent.model_copy(update={"state": "pending"})
        state.current_intent = revived
        state.rearms[revived.request_id] = revived
        state.version += 1
        result = rc.CommandResult(outcome="accepted", reason="Stopped request continued; no new allowance.",
            control=self._control(state, None))
        state.processed[identity] = (dumped, result)
        return result


# Review-control scenarios (see e2e/review-control.spec.ts): issues in this
# range link one open pull request numbered issue + 100, whose review
# authority is ReviewFixture's; their resolve authority reads inactive so the
# page shows the legacy unblock retired by the active review family.
REVIEW_ISSUES = range(80, 100)
REVIEW_PR_OFFSET = 100
# A ceiling whose counted set exceeds one re-arm's authorization bound.
HUGE_CEILING_PR = 183
# An expired dismissal whose harness read proved it absent: one retry grant.
REVIEW_RETRY_PR = 182
REVIEW_HEAD = "c" * 40
REVIEW_BOT = "review-bot[bot]"


@dataclass
class _ReviewState:
    """One pull request's in-memory review authority; wire objects are replaced, never mutated."""

    pr: int
    generation: int = 1
    version: int = 4
    guidance: str = ""
    counted: int = 2
    current_intent: wc.ReviewPendingRearm | wc.ReviewCompletedRearm | wc.ReviewStoppedRearm | None = None
    rearms: dict = field(default_factory=dict)
    retry_authorization: str | None = None
    retry_settled: bool = False
    drained: bool = False
    processed: dict = field(default_factory=dict)

    @property
    def episode_id(self) -> str:
        return f"review-episode-{self.pr}"

    @property
    def gate_id(self) -> str:
        return f"review-ceiling-{self.pr}"


class ReviewFixture:
    """Deterministic review-control state machine for the dashboard E2E; a test double.

    Production semantics are covered against real SQLite authority by
    tests/app/test_review_operator_api.py; this double only feeds the SPA the
    same wire contract, and `drain` stands in for the review daemon.
    """

    def __init__(self) -> None:
        self._states: dict[int, _ReviewState] = {}

    def _state(self, pr: int) -> _ReviewState:
        return self._states.setdefault(pr, _ReviewState(pr=pr, counted=70 if pr == HUGE_CEILING_PR else 2))

    def _expected(self, state: _ReviewState) -> wc.ReviewExpected:
        return wc.ReviewExpected(
            repository=RESOLVE_REPO, pull_request=state.pr, family="review_rejections", epoch=1,
            generation=state.generation, version=state.version, episode_id=state.episode_id,
        )

    def _reviews(self, state: _ReviewState) -> list[str]:
        return [f"PRR_kwDOfixture{state.pr}review{index:03d}" for index in range(state.counted)]

    def _ceiling(self, state: _ReviewState) -> wc.ReviewGate:
        intent = state.current_intent
        reviews = self._reviews(state)
        if isinstance(intent, wc.ReviewPendingRearm):
            actions = [wc.ReviewActionOffered(state="offered", operation="cancel_rearm", effect=(
                f"Stops request {intent.request_id}. Verified effects remain facts and its unexecuted obligations "
                "are retired."))]
        elif len(reviews) > 63:
            actions = [wc.ReviewActionRefused(state="refused", operation="request_rearm", reason=(
                f"{len(reviews)} counted reviews exceed the 63-dismissal bound of one re-arm; dismiss some on "
                "GitHub so reconciliation can refresh the ceiling"))]
        else:
            actions = [wc.ReviewActionOffered(state="offered", operation="request_rearm", effect=(
                f"Authorizes dismissal of exactly the {len(reviews)} counted bot reviews listed (never a human or "
                "later review), owes a fresh review and returns the pull request to review once the dismissals "
                "are verified. Other holds stay independent."))]
        return wc.ReviewGate(
            gate_id=state.gate_id, state="rearming" if isinstance(intent, wc.ReviewPendingRearm) else "open",
            opened_generation=state.generation,
            cause=wc.ReviewRejectionCeilingCause(
                kind="review_rejection_ceiling", episode_id=state.episode_id, head_sha=REVIEW_HEAD,
                review_bot=REVIEW_BOT, ceiling=2, counted_total=len(reviews),
                counted=[wc.ReviewCountedReview(review_id=review, author=REVIEW_BOT, commit_id=REVIEW_HEAD)
                         for review in reviews],
                authorization_bound=63, non_resetting_commits=[],
                hold=wc.ReviewHoldUncontested(state="restoring", restores=0, restore_limit=3),
            ),
            remediation=wc.ReviewNoRemediation(kind="none_authorized",
                                               reason="a counted-review dismissal needs an operator decision"),
            actions=actions, pending_retry=None,
        )

    def _retry_gate(self, state: _ReviewState) -> wc.ReviewGate:
        granted = state.retry_authorization
        return wc.ReviewGate(
            gate_id=f"review-retry-{state.pr}", state="open", opened_generation=state.generation,
            cause=wc.ReviewRetryAuthorizationRequiredCause(
                kind="retry_authorization_required", command_id=f"dismiss-{state.pr}",
                absence_evidence_id=f"absence-{state.pr}", retry_authorizable=granted is None),
            remediation=wc.ReviewAttemptedRemediation(kind="attempted", operation="reconcile_remote",
                                                      evidence_ids=[f"absence-{state.pr}"], outcome="verified_absent"),
            actions=[wc.ReviewActionRefused(state="refused", operation="authorize_retry",
                                            reason=f"authorization {granted} is already waiting for the executor")
                     if granted else
                     wc.ReviewActionOffered(state="offered", operation="authorize_retry", effect=(
                         f"Permits exactly one more execution of command dismiss-{state.pr} under its current "
                         "generation. The executor re-reads before writing; nothing is written if the effect has "
                         "appeared meanwhile."))],
            pending_retry=wc.ReviewPendingRetry(authorization_id=granted, command_id=f"dismiss-{state.pr}")
            if granted else None,
        )

    def _gates(self, state: _ReviewState) -> list[wc.ReviewGate]:
        if state.pr == REVIEW_RETRY_PR:
            return [] if state.retry_settled else [self._retry_gate(state)]
        return [] if isinstance(state.current_intent, wc.ReviewCompletedRearm) else [self._ceiling(state)]

    def _daemon(self, state: _ReviewState, gates: list[wc.ReviewGate]) -> wc.ReviewDaemonRecorded:
        """What the review daemon's plan would have recorded for this state (the drain stands in for its tick).

        A held ceiling swapped the trigger for needs-human-review, so the
        pull request left the trigger-labelled review read and its own read
        found it held by the gate that label projects; a retry gate holds a
        still-listed one.
        """
        labels = ["ai-pr-open"]
        dispatch: wc.ReviewDispatch = wc.ReviewDispatchOwed(kind="owed", review="re_review")
        read: Literal["review_read", "departed_read"] = "review_read"
        if gates and state.pr != REVIEW_RETRY_PR:
            labels, read = ["needs-human-review"], "departed_read"
        if gates:
            dispatch = wc.ReviewDispatchHeld(kind="held", gate_ids=[gate.gate_id for gate in gates])
        decided = "2026-09-26T10:05:00+00:00" if state.drained else "2026-09-26T09:55:00+00:00"
        return wc.ReviewDaemonRecorded(
            state="recorded", dispatch=dispatch, read=read, decided_at=decided, as_of=decided, read_failure=None,
            pull_request=wc.ReviewDecisionBasis(head_sha=REVIEW_HEAD, state="open", labels=labels, external_holds=[]),
        )

    def _control(self, state: _ReviewState, request_id: str | None) -> wc.ReviewAvailableControl:
        gates = self._gates(state)
        return wc.ReviewAvailableControl(
            status="available", expected=self._expected(state), workflow_held=bool(gates), gates=gates,
            guidance=state.guidance,
            continuation=wc.ReviewFreshReviewRequired(kind="fresh_review_required",
                                                      owed_since_generation=state.generation),
            daemon=self._daemon(state, gates), current_intent=state.current_intent, requested_id=request_id,
            requested_intent=state.rearms.get(request_id) if request_id else None, observation=None,
        )

    def control(self, pr: int, request_id: str | None) -> wc.ReviewControl:
        if pr - REVIEW_PR_OFFSET not in REVIEW_ISSUES:
            return wc.ReviewMigrationRequiredControl(status="migration_required", repository=RESOLVE_REPO,
                                                     pull_request=pr, active_families=[])
        return self._control(self._state(pr), request_id)

    def _pending(self, state: _ReviewState, request_id: str) -> wc.ReviewPendingRearm:
        reviews = self._reviews(state)
        commands = [wc.ReviewRearmCommand(command_id=f"dismiss-{request_id[:8]}-{index}", status="pending",
                                          obligation=wc.ReviewDismissalObligation(kind="dismiss_review",
                                                                                  review_id=review))
                    for index, review in enumerate(reviews)]
        commands.append(wc.ReviewRearmCommand(
            command_id=f"projection-{request_id[:8]}", status="pending",
            obligation=wc.ReviewProjectionObligation(kind="projection", remove="needs-human-review",
                                                     add="ai-pr-open")))
        return wc.ReviewPendingRearm(state="pending", request_id=request_id, gate_id=state.gate_id,
                                     generation=state.generation + 1, authorized_reviews=reviews, commands=commands)

    def command(self, pr: int, body: wc.ReviewCommandBody) -> wc.ReviewCommandResult:
        if pr - REVIEW_PR_OFFSET not in REVIEW_ISSUES:
            return wc.ReviewCommandResult(outcome="rejected", reason="review rejection control is not active",
                                          control=self.control(pr, None))
        state = self._state(pr)
        identity = (body.cancellation_id if body.operation == "cancel_rearm"
                    else body.authorization_id if body.operation == "authorize_retry" else body.request_id)
        dumped = body.model_dump(mode="json")
        prior = state.processed.get(identity)
        if prior is not None:
            outcome = "replayed" if prior == dumped else "conflict"
            return wc.ReviewCommandResult(outcome=outcome, reason="" if outcome == "replayed" else
                                          "This identifier was already used for a different command.",
                                          control=self._control(state, None))
        if body.expected != self._expected(state):
            return wc.ReviewCommandResult(outcome="conflict", reason="the workflow revision changed",
                                          control=self._control(state, None))
        gate = next((item for item in self._gates(state) if item.gate_id == body.gate_id), None) \
            if body.operation in ("request_rearm", "authorize_retry") else None
        offered = gate is not None and any(action.state == "offered" and action.operation == body.operation
                                           for action in gate.actions)
        if body.operation in ("request_rearm", "authorize_retry") and not offered:
            refusal = next((action.reason for action in (gate.actions if gate else [])
                            if isinstance(action, wc.ReviewActionRefused)), "the gate does not offer this command")
            return wc.ReviewCommandResult(outcome="rejected", reason=refusal, control=self._control(state, None))
        if body.operation == "request_rearm":
            state.guidance = (body.guidance.text if body.guidance.kind == "set" else
                              "" if body.guidance.kind == "clear" else state.guidance)
            state.current_intent = self._pending(state, body.request_id)
        elif body.operation == "authorize_retry":
            state.retry_authorization = body.authorization_id
        elif body.operation == "cancel_rearm":
            intent = state.current_intent
            if not isinstance(intent, wc.ReviewPendingRearm) or intent.request_id != body.request_id:
                return wc.ReviewCommandResult(outcome="conflict", reason="no matching pending request",
                                              control=self._control(state, None))
            state.current_intent = wc.ReviewStoppedRearm(
                state="cancelled", request_id=intent.request_id, gate_id=intent.gate_id,
                generation=intent.generation, reason=body.reason, authorized_reviews=intent.authorized_reviews,
                verified_commands=[], retired_commands=[command.command_id for command in intent.commands])
        else:
            return wc.ReviewCommandResult(outcome="rejected", reason="no stopped request can be continued here",
                                          control=self._control(state, None))
        if state.current_intent is not None:
            state.rearms[state.current_intent.request_id] = state.current_intent
        state.version += 1
        result = wc.ReviewCommandResult(outcome="accepted", reason="", control=self._control(state, None))
        state.processed[identity] = dumped
        return result

    def drain(self, pr: int) -> None:
        """What the review daemon's drain and reconciliation would do next for this pull request."""
        state = self._state(pr)
        intent = state.current_intent
        if isinstance(intent, wc.ReviewPendingRearm):
            state.current_intent = wc.ReviewCompletedRearm(
                state="completed", request_id=intent.request_id, gate_id=intent.gate_id,
                generation=intent.generation,
                verified_commands=[command.command_id for command in intent.commands])
            state.rearms[intent.request_id] = state.current_intent
            state.generation = intent.generation
            state.version += 1
        elif state.retry_authorization and not state.retry_settled:
            state.retry_settled = True
            state.version += 1
        state.drained = True


def role(name: str) -> dict:
    return {
        "role": name,
        "alive": True,
        "stale": False,
        "last_event_ts": "2026-07-29T08:00:00Z",
        "last_event": "tick",
        "events": 4,
        "log_path": f"/logs/{name}.log",
    }


ISSUE_CARD = {
    "issue": 50,
    "title": "Explain completed work",
    "url": "https://github.com/openai/autonomous-issue-agent/issues/50",
    "pr": 75,
    "pr_url": "https://github.com/openai/autonomous-issue-agent/pull/75",
    "pr_state": "merged",
    "stage": "done",
    "done": True,
    "cost_usd": 2,
    "estimated": False,
    "elapsed_s": 60,
    "working_s": 45,
    "idle_s": 15,
    "idle_active": False,
    "started_ts": "2026-07-29T07:00:00Z",
    "last_ts": "2026-07-29T08:00:00Z",
    "merged_ts": "2026-07-29T08:00:00Z",
    "projected_cost_usd": 2,
    "projected_eta_s": 60,
    "rounds": 1,
    "models": ["claude-sonnet-5"],
    "flags": [],
    "steps": [{"kind": "RESOLVE", "state": "ok", "status": "done", "cost_usd": 2}],
}


class FixtureBackend:
    def __init__(self) -> None:
        self._resolve = ResolveFixture()
        self._review = ReviewFixture()

    def known(self, name: str) -> bool:
        return name == "self"

    def repos(self) -> dict:
        return {
            "repos": [{
                "name": "self",
                "repo": "openai/autonomous-issue-agent",
                "target_dir": "/tmp/self",
                "is_self": True,
                "active": True,
                "roles": [role("fix"), role("review"), role("steward")],
                "frecency": 1,
                "last_activity_ts": "2026-07-29T08:00:00Z",
                "in_flight": 0,
                "done": 1,
            }],
            "warnings": [],
        }

    def usage(self) -> dict:
        return {
            "claude": None,
            "codex": None,
            "harness": {
                "global_daily_cap_usd": 50,
                "today_all_repos_usd": 2,
                "per_repo": [{
                    "name": "self",
                    "repo": "openai/autonomous-issue-agent",
                    "daily_cap_usd": 10,
                    "today_usd": 2,
                }],
            },
            "warnings": [],
        }

    def verification(self) -> dict:
        return {
            "rows": [{
                "id": "V-1",
                "change": "Dashboard",
                "status": "verified",
                "evidence": "E2E",
                "shipped": "yes",
                "raw": "",
            }],
            "warnings": [],
        }

    def overview(self, _name: str, _timezone_name: str | None = None) -> dict:
        return {
            "metrics": None,
            "cost_mix": None,
            "caps": {"issue_cap_usd": 5, "daily_cap_usd": 10, "today_usd": 2},
            "health": [],
            "unattributed": [],
            "health_trend": [{
                "day": "2026-07-29",
                "opened": 3,
                "closed": 1,
                "net_open": 2,
                "recurrences": 0,
                "first_observation": True,
            }],
            "warnings": [],
        }

    def issues(self, _name: str) -> dict:
        return {
            "in_flight": [],
            "pending": [],
            "done": [ISSUE_CARD],
            "backlog": [],
            "gh_ok": True,
            "warnings": [],
        }

    def issue(self, _name: str, _number: int) -> dict:
        if _number in REVIEW_ISSUES:
            pr = _number + REVIEW_PR_OFFSET
            return {
                "issue": _number,
                "title": f"Review fixture issue #{_number}",
                "url": f"https://github.com/openai/autonomous-issue-agent/issues/{_number}",
                "stage": "escalated",
                "escalation_reason": "The reviewer rejected the pull request at the ceiling.",
                "done": False,
                "prs": [pr],
                "pull_requests": [{
                    "number": pr, "url": f"https://github.com/openai/autonomous-issue-agent/pull/{pr}",
                    "state": "open",
                }],
                "cost_usd": 1,
                "elapsed_s": 30,
                "working_s": 20,
                "idle_s": 10,
                "idle_active": False,
                "steps": [],
                "events": [],
                "warnings": [],
            }
        if _number != 50:
            # Resolve-control scenarios (see e2e/resolve-control.spec.ts): not
            # done and held for a human, so IssueTab.heldForHuman is true and
            # ResolveControls renders. The resolve authority itself (available
            # / inactive / unavailable) is decided by ResolveFixture, keyed on
            # the same issue number.
            return {
                "issue": _number,
                "title": f"Resolve fixture issue #{_number}",
                "url": f"https://github.com/openai/autonomous-issue-agent/issues/{_number}",
                "stage": "escalated",
                "escalation_reason": "Held for a human by the resolve-control fixture.",
                "done": False,
                "prs": [],
                "pull_requests": [],
                "cost_usd": 1,
                "elapsed_s": 30,
                "working_s": 20,
                "idle_s": 10,
                "idle_active": False,
                "steps": [],
                "events": [],
                "warnings": [],
            }
        return {
            "issue": 50,
            "title": "Explain completed work",
            "url": "https://github.com/openai/autonomous-issue-agent/issues/50",
            "stage": "done",
            "done": True,
            "prs": [75],
            "pull_requests": [{
                "number": 75,
                "url": "https://github.com/openai/autonomous-issue-agent/pull/75",
                "state": "merged",
            }],
            "cost_usd": 2,
            "elapsed_s": 60,
            "working_s": 45,
            "idle_s": 15,
            "idle_active": False,
            "steps": [{
                "idx": 0,
                "kind": "RESOLVE",
                "role": "fix",
                "state": "ok",
                "model": "claude-sonnet-5",
                "status": "done",
                "exit_reason": "ok",
                "duration_s": 60,
                "cost_usd": 2,
                "estimated": False,
                "session_id": "session-50",
                "has_transcript": False,
                "turns": 3,
                "usage": None,
                "ts": "2026-07-29T08:00:00Z",
            }],
            "events": [{
                "ts": "2026-07-29T07:59:00Z",
                "event": "action",
                "role": "steward",
                "desc": "comment issue #50: verification recorded",
            }],
            "warnings": [],
        }

    def step_events(self, _name: str, _number: int, _idx: int) -> dict:
        return {"events": [], "warnings": []}

    def cost_chart(self, _session_id: str) -> tuple[str, str]:
        return "<html></html>", ""

    def start(self, _name: str) -> tuple[bool, str]:
        return True, "started"

    def promote(self, _name: str, _number: int) -> tuple[bool, str]:
        return True, "promoted"

    def priority(
        self, _name: str, _number: int, _priority: str | None
    ) -> tuple[bool, str]:
        return True, "updated"

    def resolve_control(self, _name: str, number: int, request_id: str | None = None) -> rc.ResolveControl:
        if number in REVIEW_ISSUES:
            return rc.InactiveControl(status="inactive", repository=RESOLVE_REPO, issue=number)
        return self._resolve.control(number, request_id)

    def resolve_command(self, _name: str, number: int, body: rc.ResolveCommand) -> rc.CommandResult:
        return self._resolve.command(number, body)

    def review_control(self, _name: str, pr: int, request_id: str | None = None) -> wc.ReviewControl:
        return self._review.control(pr, request_id)

    def review_command(self, _name: str, pr: int, body: wc.ReviewCommandBody) -> wc.ReviewCommandResult:
        return self._review.command(pr, body)

    def workflow_families(self, _name: str) -> wc.WorkflowFamilies:
        # No family active: the resolve specs' legacy unblock stays offered; the
        # review spec routes this read to show the review family active.
        return wc.WorkflowFamiliesAvailable(status="available", repository=RESOLVE_REPO, active_families=[])

    def unblock(
        self, _name: str, _number: int, _channel: str
    ) -> tuple[bool, str]:
        return True, "unblocked"


def fixture_app():
    """The real dashboard app over the fixture backend, plus the E2E-only drain trigger."""
    harness_root = Path(__file__).resolve().parents[2]
    backend = FixtureBackend()
    app = create_app(backend, harness_root=str(harness_root))

    # Stands in for the review daemon's next drain; guarded like every write.
    @app.post("/fixture/review-drain/{pr}")
    async def review_drain(pr: int):
        if pr - REVIEW_PR_OFFSET not in REVIEW_ISSUES:
            raise HTTPException(404, "no review fixture for this pull request")
        backend._review.drain(pr)
        return {"ok": True}

    return app


if __name__ == "__main__":
    uvicorn.run(
        fixture_app(),
        host="127.0.0.1",
        port=4174,
        log_level="warning",
    )
