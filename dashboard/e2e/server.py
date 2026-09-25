"""Deterministic full-stack server for Playwright."""

from dataclasses import dataclass, field
from pathlib import Path

import uvicorn

from issue_agent.app.dashweb import resolve_contracts as rc
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
        return self._resolve.control(number, request_id)

    def resolve_command(self, _name: str, number: int, body: rc.ResolveCommand) -> rc.CommandResult:
        return self._resolve.command(number, body)

    def unblock(
        self, _name: str, _number: int, _channel: str, _guidance: str | None
    ) -> tuple[bool, str]:
        return True, "unblocked"


if __name__ == "__main__":
    harness_root = Path(__file__).resolve().parents[2]
    uvicorn.run(
        create_app(FixtureBackend(), harness_root=str(harness_root)),
        host="127.0.0.1",
        port=4174,
        log_level="warning",
    )
