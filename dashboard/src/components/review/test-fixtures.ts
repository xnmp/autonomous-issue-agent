import type {
  AvailableReviewControl, ReviewCommandResult, ReviewControl, ReviewDaemonDecision, ReviewDispatch, ReviewGate, ReviewRearm,
} from "../../api/types";

/** Wire-shaped review-rejection fixtures shared by the review UI tests. */
export const reviewBinding = {
  repository: "o/r",
  pull_request: 12,
  family: "review_rejections",
  epoch: 1,
  generation: 2,
  version: 5,
  episode_id: "episode-12",
} as const;

const HEAD = "a".repeat(40);

export function counted(count: number) {
  return Array.from({ length: count }, (_, index) => ({ review_id: `review-${index}`, author: "review-bot[bot]", commit_id: HEAD }));
}

export function reviewCeilingGate(overrides: Partial<ReviewGate> = {}, count = 2): ReviewGate {
  return {
    gate_id: "ceiling-12",
    state: "open",
    opened_generation: 2,
    cause: {
      kind: "review_rejection_ceiling", episode_id: "episode-12", head_sha: HEAD, review_bot: "review-bot[bot]",
      ceiling: 2, counted_total: count, counted: counted(count), authorization_bound: 63, non_resetting_commits: [],
      hold: { state: "restoring", restores: 0, restore_limit: 3 },
    },
    remediation: { kind: "none_authorized", reason: "a counted-review dismissal needs an operator decision" },
    actions: [{ state: "offered", operation: "request_rearm", effect: "Authorizes dismissal of exactly the 2 counted bot reviews listed." }],
    pending_retry: null,
    ...overrides,
  };
}

export function reviewRetryGate(overrides: Partial<ReviewGate> = {}): ReviewGate {
  return {
    gate_id: "retry-12",
    state: "open",
    opened_generation: 2,
    cause: { kind: "retry_authorization_required", command_id: "command-12", absence_evidence_id: "absence-12", retry_authorizable: true },
    remediation: { kind: "attempted", operation: "reconcile_remote", evidence_ids: ["absence-12"], outcome: "verified_absent" },
    actions: [{ state: "offered", operation: "authorize_retry", effect: "Permits exactly one more execution of command command-12." }],
    pending_retry: null,
    ...overrides,
  };
}

export function decisionGate(overrides: Partial<ReviewGate> = {}): ReviewGate {
  return {
    gate_id: "rearm-stopped:request-12",
    state: "open",
    opened_generation: 3,
    cause: { kind: "rearm_needs_decision", stopped_request_id: "request-12", obligations: ["dismiss-1"], reason: "decision_changed" },
    remediation: { kind: "none_authorized", reason: "the stopped review re-arm requires a fresh operator decision" },
    actions: [{ state: "offered", operation: "continue_rearm", effect: "Re-plans stopped request request-12's remaining authorized dismissals." }],
    pending_retry: null,
    ...overrides,
  };
}

export function pendingRearm(overrides: Partial<Extract<ReviewRearm, { state: "pending" }>> = {}): ReviewRearm {
  return {
    state: "pending",
    request_id: "request-12",
    gate_id: "ceiling-12",
    generation: 3,
    authorized_reviews: ["review-0", "review-1"],
    commands: [
      { command_id: "dismiss-0", obligation: { kind: "dismiss_review", review_id: "review-0" }, status: "verified" },
      { command_id: "dismiss-1", obligation: { kind: "dismiss_review", review_id: "review-1" }, status: "pending" },
      { command_id: "projection", obligation: { kind: "projection", remove: "needs-human-review", add: "ai-pr-open" }, status: "pending" },
    ],
    ...overrides,
  };
}

export const DECIDED_AT = "2026-09-26T09:55:00+00:00";
export const AS_OF = "2026-09-26T10:00:00+00:00";

/** The review daemon's recorded decision, as its plan writes it (decided, then confirmed unchanged). */
export function reviewDaemon(
  dispatch: ReviewDispatch, overrides: Partial<Extract<ReviewDaemonDecision, { state: "recorded" }>> = {},
): ReviewDaemonDecision {
  return {
    state: "recorded", dispatch, read: "review_read", decided_at: DECIDED_AT, as_of: AS_OF, read_failure: null,
    pull_request: {
      head_sha: HEAD, state: dispatch.kind === "terminal" ? dispatch.state : "open", labels: ["needs-human-review"],
      external_holds: [],
    },
    ...overrides,
  };
}

export function reviewControl(overrides: Partial<AvailableReviewControl> = {}): AvailableReviewControl {
  return {
    status: "available",
    expected: { ...reviewBinding },
    workflow_held: true,
    gates: [reviewCeilingGate()],
    guidance: "",
    continuation: { kind: "fresh_review_required", owed_since_generation: 2 },
    daemon: reviewDaemon({ kind: "held", gate_ids: ["ceiling-12"] }),
    current_intent: null,
    requested_id: null,
    requested_intent: null,
    observation: null,
    ...overrides,
  };
}

type Variant<S extends ReviewControl["status"]> = Extract<ReviewControl, { status: S }>;

export const reviewInactive: Variant<"inactive"> = { status: "inactive", repository: "o/r", pull_request: 12 };
export const reviewAbsent: Variant<"absent"> = { status: "absent", repository: "o/r", pull_request: 12, epoch: 1 };
export const reviewMigrationRequired: Variant<"migration_required"> = {
  status: "migration_required", repository: "o/r", pull_request: 12, active_families: ["resolve"],
};
export const reviewUnavailable: Variant<"unavailable"> = { status: "unavailable", reason: "control store unreadable" };

export function reviewResult(outcome: ReviewCommandResult["outcome"], control: ReviewControl, reason = ""): ReviewCommandResult {
  return { outcome, control, reason };
}

export const REVIEW_OUTCOME_STATUS: Record<ReviewCommandResult["outcome"], number> = {
  accepted: 202, replayed: 200, conflict: 409, rejected: 422, unavailable: 503,
};

export function reviewJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function reviewControlResponse(control: ReviewControl): Response {
  const status = control.status === "absent" ? 404 : control.status === "unavailable" ? 503 : 200;
  return reviewJson(control, status);
}
