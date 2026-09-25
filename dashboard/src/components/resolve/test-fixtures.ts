import type { AvailableResolveControl, ResolveCommandResult, ResolveGate, ResolveRearm } from "../../api/types";

/** Wire-shaped resolve fixtures shared by the resolve UI tests. */
export const binding = {
  repository: "o/r",
  issue: 77,
  family: "resolve",
  epoch: 1,
  generation: 4,
  version: 9,
  episode_id: "episode-77",
} as const;

export function ceilingGate(overrides: Partial<ResolveGate> = {}): ResolveGate {
  return {
    gate_id: "gate-77",
    state: "open",
    opened_generation: 4,
    available_commands: ["request_rearm"],
    pending_retry: null,
    cause: { kind: "resolve_ceiling", episode_id: "episode-77", failures: 3, ceiling: 3, last_failure: "tests still red" },
    remediation: { kind: "none_authorized", reason: "operator decision required" },
    ...overrides,
  };
}

export function retryGate(overrides: Partial<ResolveGate> = {}): ResolveGate {
  return {
    gate_id: "uncertain-77",
    state: "open",
    opened_generation: 4,
    available_commands: ["authorize_retry"],
    pending_retry: null,
    cause: { kind: "retry_authorization_required", command_id: "command-77", absence_evidence_id: "absence-77" },
    remediation: { kind: "attempted", operation: "reconcile_remote", evidence_ids: ["absence-77"], outcome: "verified_absent" },
    ...overrides,
  };
}

export function rearm(overrides: Partial<ResolveRearm> = {}): ResolveRearm {
  return {
    request_id: "request-original",
    gate_id: "gate-77",
    state: "pending",
    reason: "",
    episode_id: "episode-77",
    generation: 4,
    grant_request_id: "request-original",
    handoff_id: "handoff-1",
    branch: "fix/77",
    base_ref: "origin/dev",
    base_commit: "0123456789abcdef0123456789abcdef01234567",
    required_commands: ["cmd-a"],
    verified_commands: [],
    retired_commands: [],
    ...overrides,
  };
}

export function availableControl(overrides: Partial<AvailableResolveControl> = {}): AvailableResolveControl {
  return {
    status: "available",
    expected: { ...binding },
    workflow_held: true,
    gates: [ceilingGate()],
    guidance: "keep the public API",
    failures: 3,
    current_intent: null,
    requested_id: null,
    requested_intent: null,
    observation: null,
    ...overrides,
  };
}

export const inactiveControl = { status: "inactive", repository: "o/r", issue: 77 } as const;
export const absentControl = { status: "absent", repository: "o/r", issue: 77, epoch: 1 } as const;
export const unavailableControl = { status: "unavailable", reason: "control store unreadable" } as const;

export function commandResult(outcome: ResolveCommandResult["outcome"], control: ResolveCommandResult["control"], reason = ""): ResolveCommandResult {
  return { outcome, control, reason };
}

export const OUTCOME_STATUS: Record<ResolveCommandResult["outcome"], number> = {
  accepted: 202, replayed: 200, conflict: 409, rejected: 422, unavailable: 503,
};

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function controlResponse(control: ResolveCommandResult["control"]): Response {
  const status = control.status === "absent" ? 404 : control.status === "unavailable" ? 503 : 200;
  return jsonResponse(control, status);
}
