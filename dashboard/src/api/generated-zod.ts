import { makeApi, Zodios, type ZodiosOptions } from "@zodios/core";
import { z } from "zod";

const RoleHealth = z
  .object({
    alive: z.union([z.boolean(), z.null()]),
    events: z.number().int(),
    last_event: z.union([z.string(), z.null()]),
    last_event_ts: z.union([z.string(), z.null()]),
    log_path: z.union([z.string(), z.null()]),
    role: z.string(),
    stale: z.boolean(),
  })
  .passthrough();
const RepoSummary = z
  .object({
    active: z.union([z.boolean(), z.null()]),
    done: z.number().int(),
    frecency: z.number(),
    in_flight: z.number().int(),
    is_self: z.boolean(),
    last_activity_ts: z.union([z.string(), z.null()]),
    name: z.string(),
    repo: z.union([z.string(), z.null()]),
    roles: z.array(RoleHealth),
    target_dir: z.union([z.string(), z.null()]),
  })
  .passthrough();
const ReposPayload = z
  .object({ repos: z.array(RepoSummary), warnings: z.array(z.string()) })
  .passthrough();
const BacklogCard = z
  .object({
    age_days: z.union([z.number(), z.null()]),
    age_s: z.union([z.number(), z.null()]).optional(),
    body: z.union([z.string(), z.null()]),
    comments: z.union([z.number(), z.null()]),
    difficulty: z.union([z.string(), z.null()]).optional(),
    impact: z.union([z.string(), z.null()]).optional(),
    issue: z.number().int(),
    issue_type: z.union([z.string(), z.null()]).optional(),
    labels: z.array(z.string()),
    recurrence: z.boolean(),
    recurrence_count: z.number().int(),
    steward_state: z.enum(["raw", "processed", "blocked_on_human_reply"]),
    title: z.union([z.string(), z.null()]),
    url: z.union([z.string(), z.null()]),
  })
  .passthrough();
const ModelBadge = z
  .object({ effort: z.union([z.string(), z.null()]), model: z.string() })
  .passthrough();
const CardStep = z
  .object({
    cost_usd: z.union([z.number(), z.null()]),
    count: z.number().int().optional().default(1),
    kind: z.string(),
    state: z.string(),
    status: z.union([z.string(), z.null()]),
  })
  .passthrough();
const IssueCard = z
  .object({
    attention_reason: z.union([z.string(), z.null()]).optional(),
    cost_usd: z.union([z.number(), z.null()]),
    done: z.boolean(),
    elapsed_s: z.union([z.number(), z.null()]),
    escalation_reason: z.union([z.string(), z.null()]).optional(),
    estimated: z.boolean(),
    flags: z.array(z.string()),
    human_action: z
      .union([z.enum(["merge-ready", "awaiting-reply", "escalated"]), z.null()])
      .optional(),
    human_wait_s: z.union([z.number(), z.null()]).optional(),
    human_wait_since_ts: z.union([z.string(), z.null()]).optional(),
    idle_active: z.boolean(),
    idle_s: z.union([z.number(), z.null()]),
    issue: z.number().int(),
    issue_verification: z.union([z.string(), z.null()]).optional(),
    issue_verification_reason: z.union([z.string(), z.null()]).optional(),
    labels: z.array(z.string()).optional().default([]),
    last_ts: z.union([z.string(), z.null()]),
    leg_running: z.boolean().optional().default(false),
    merged_ts: z.union([z.string(), z.null()]).optional(),
    model_badges: z.array(ModelBadge).optional().default([]),
    models: z.array(z.string()),
    pr: z.union([z.number(), z.null()]),
    pr_ci_state: z
      .union([z.enum(["passing", "failing", "pending"]), z.null()])
      .optional(),
    pr_review_state: z
      .union([z.enum(["mergeable", "changes_requested"]), z.null()])
      .optional(),
    pr_state: z.union([z.string(), z.null()]),
    pr_url: z.union([z.string(), z.null()]),
    pr_verification: z.union([z.string(), z.null()]).optional(),
    pr_verification_reason: z.union([z.string(), z.null()]).optional(),
    projected_cost_usd: z.union([z.number(), z.null()]),
    projected_eta_s: z.union([z.number(), z.null()]),
    projected_model: z.union([z.string(), z.null()]).optional(),
    rounds: z.union([z.number(), z.null()]),
    stage: z.union([z.string(), z.null()]),
    started_ts: z.union([z.string(), z.null()]),
    steps: z.array(CardStep),
    title: z.union([z.string(), z.null()]),
    url: z.union([z.string(), z.null()]),
    working_s: z.union([z.number(), z.null()]),
  })
  .passthrough();
const ToastNotification = z
  .object({
    detail: z.union([z.string(), z.null()]).optional(),
    id: z.string(),
    issue: z.union([z.number(), z.null()]).optional(),
    pr: z.union([z.number(), z.null()]).optional(),
    severity: z.enum(["info", "attention"]),
    title: z.string(),
    ts: z.string(),
  })
  .passthrough();
const PendingCard = z
  .object({
    body: z.union([z.string(), z.null()]).optional(),
    held: z.boolean(),
    hold_reason: z.union([z.string(), z.null()]),
    issue: z.number().int(),
    labels: z.array(z.string()),
    overlap: z.union([z.unknown(), z.null()]),
    pr: z.union([z.number(), z.null()]).optional(),
    pr_ci_state: z
      .union([z.enum(["passing", "failing", "pending"]), z.null()])
      .optional(),
    pr_state: z.union([z.string(), z.null()]).optional(),
    pr_url: z.union([z.string(), z.null()]).optional(),
    priority: z.union([z.enum(["P1", "P2", "P3"]), z.null()]),
    title: z.union([z.string(), z.null()]),
    url: z.union([z.string(), z.null()]),
  })
  .passthrough();
const IssuesPayload = z
  .object({
    backlog: z.array(BacklogCard),
    done: z.array(IssueCard),
    gh_ok: z.boolean(),
    in_flight: z.array(IssueCard),
    notifications: z.array(ToastNotification).optional().default([]),
    pending: z.array(PendingCard),
    warnings: z.array(z.string()),
  })
  .passthrough();
const ValidationError = z
  .object({
    ctx: z.object({}).partial().passthrough().optional(),
    input: z.unknown().optional(),
    loc: z.array(z.union([z.string(), z.number()])),
    msg: z.string(),
    type: z.string(),
  })
  .passthrough();
const HTTPValidationError = z
  .object({ detail: z.array(ValidationError) })
  .partial()
  .passthrough();
const PullRequestLink = z
  .object({
    number: z.number().int(),
    state: z.union([z.string(), z.null()]),
    url: z.union([z.string(), z.null()]),
  })
  .passthrough();
const IssueStepChange = z
  .object({
    branch: z.union([z.string(), z.null()]).optional(),
    commit_subjects: z.array(z.string()).optional(),
    files_changed_count: z.union([z.number(), z.null()]).optional(),
    head_sha: z.union([z.string(), z.null()]).optional(),
    pr: z.number().int(),
  })
  .passthrough();
const IssueStepEscalation = z
  .object({
    after_review_round: z.union([z.number(), z.null()]).optional(),
    from_effort: z.union([z.string(), z.null()]).optional(),
    notches: z.number().int(),
    to_effort: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const IssueStepReview = z
  .object({
    rejection: z.union([z.number(), z.null()]),
    unproductive_cap: z.union([z.number(), z.null()]),
    unproductive_rounds: z.union([z.number(), z.null()]),
    verdict_excerpt: z.union([z.string(), z.null()]),
  })
  .partial()
  .passthrough();
const TokenUsage = z
  .object({
    cache_creation_input_tokens: z.union([z.number(), z.null()]),
    cache_read_input_tokens: z.union([z.number(), z.null()]),
    input_tokens: z.union([z.number(), z.null()]),
    output_tokens: z.union([z.number(), z.null()]),
  })
  .passthrough();
const IssueStep = z
  .object({
    budget_cap_usd: z.union([z.number(), z.null()]).optional(),
    change: z.union([IssueStepChange, z.null()]).optional(),
    cost_usd: z.union([z.number(), z.null()]),
    duration_s: z.union([z.number(), z.null()]),
    effort: z.union([z.string(), z.null()]).optional(),
    escalation: z.union([IssueStepEscalation, z.null()]).optional(),
    estimated: z.boolean(),
    exit_reason: z.union([z.string(), z.null()]),
    has_transcript: z.boolean(),
    idx: z.number().int(),
    kind: z.string(),
    model: z.union([z.string(), z.null()]),
    note: z.union([z.string(), z.null()]).optional(),
    pricing_basis: z.union([z.string(), z.null()]).optional(),
    review: z.union([IssueStepReview, z.null()]).optional(),
    role: z.union([z.string(), z.null()]),
    runtime: z.union([z.string(), z.null()]).optional(),
    session_id: z.union([z.string(), z.null()]),
    state: z.string(),
    status: z.union([z.string(), z.null()]),
    ts: z.union([z.string(), z.null()]),
    turns: z.union([z.number(), z.null()]),
    usage: z.union([TokenUsage, z.null()]),
  })
  .passthrough();
const IssueDetailPayload = z
  .object({
    attention_reason: z.union([z.string(), z.null()]).optional(),
    cost_usd: z.union([z.number(), z.null()]),
    done: z.boolean(),
    elapsed_s: z.union([z.number(), z.null()]),
    escalation_reason: z.union([z.string(), z.null()]).optional(),
    events: z.array(z.object({}).partial().passthrough()),
    idle_active: z.boolean(),
    idle_s: z.union([z.number(), z.null()]),
    issue: z.number().int(),
    leg_running: z.boolean().optional().default(false),
    pr_ci_state: z
      .union([z.enum(["passing", "failing", "pending"]), z.null()])
      .optional(),
    prs: z.array(z.number().int()),
    pull_requests: z.array(PullRequestLink),
    stage: z.union([z.string(), z.null()]),
    steps: z.array(IssueStep),
    title: z.union([z.string(), z.null()]),
    url: z.union([z.string(), z.null()]),
    warnings: z.array(z.string()),
    working_s: z.union([z.number(), z.null()]),
  })
  .passthrough();
const ActionResult = z
  .object({ detail: z.string(), ok: z.boolean() })
  .passthrough();
const request_id = z.union([z.string(), z.null()]).optional();
const Rearm = z.object({
  base_commit: z.string(),
  base_ref: z.string(),
  branch: z.string(),
  episode_id: z.string(),
  gate_id: z.string(),
  generation: z.number().int(),
  grant_request_id: z.string(),
  handoff_id: z.string(),
  reason: z.string(),
  request_id: z.string(),
  required_commands: z.array(z.string()),
  retired_commands: z.array(z.string()),
  state: z.enum(["pending", "completed", "cancelled", "superseded"]),
  verified_commands: z.array(z.string()),
});
const ResolveExpected = z.object({
  episode_id: z.string().min(1).max(512),
  epoch: z.number().int().gte(1).lte(9007199254740991),
  family: z.literal("resolve"),
  generation: z.number().int().gte(0).lte(9007199254740991),
  issue: z.number().int().gte(1).lte(9007199254740991),
  repository: z.string().min(1).max(512),
  version: z.number().int().gte(0).lte(9007199254740991),
});
const ResolveCeiling = z.object({
  ceiling: z.number().int(),
  episode_id: z.string(),
  failures: z.number().int(),
  kind: z.literal("resolve_ceiling"),
  last_failure: z.string(),
});
const RemoteUncertain = z.object({
  command_id: z.string(),
  failed_reads: z.number().int(),
  kind: z.literal("remote_uncertain"),
  last_error: z.string(),
  unresolved_since: z.string(),
});
const RetryAuthorizationRequired = z.object({
  absence_evidence_id: z.string(),
  command_id: z.string(),
  kind: z.literal("retry_authorization_required"),
});
const EvidenceUnavailable = z.object({
  failed_reads: z.number().int(),
  kind: z.literal("evidence_unavailable"),
  last_error: z.string(),
  source: z.string(),
  unresolved_since: z.string(),
});
const RearmNeedsDecision = z.object({
  kind: z.literal("rearm_needs_decision"),
  obligations: z.array(z.string()),
  reason: z.string(),
  stopped_request_id: z.string(),
});
const PendingRetry = z.object({
  authorization_id: z.string(),
  command_id: z.string(),
});
const NoRemediation = z.object({
  kind: z.literal("none_authorized"),
  reason: z.string(),
});
const AttemptedRemediation = z.object({
  evidence_ids: z.array(z.string()),
  kind: z.literal("attempted"),
  operation: z.string(),
  outcome: z.string(),
});
const Gate = z.object({
  available_commands: z.array(
    z.enum([
      "request_rearm",
      "continue_rearm",
      "cancel_rearm",
      "authorize_retry",
    ])
  ),
  cause: z.discriminatedUnion("kind", [
    ResolveCeiling,
    RemoteUncertain,
    RetryAuthorizationRequired,
    EvidenceUnavailable,
    RearmNeedsDecision,
  ]),
  gate_id: z.string(),
  opened_generation: z.number().int(),
  pending_retry: z.union([PendingRetry, z.null()]),
  remediation: z.discriminatedUnion("kind", [
    NoRemediation,
    AttemptedRemediation,
  ]),
  state: z.enum(["open", "rearming"]),
});
const Observation = z.object({
  failed_reads: z.number().int(),
  last_error: z.string(),
  next_read_at: z.string(),
  unresolved_since: z.string(),
});
const AvailableControl = z.object({
  current_intent: z.union([Rearm, z.null()]),
  expected: ResolveExpected,
  failures: z.number().int(),
  gates: z.array(Gate),
  guidance: z.string(),
  observation: z.union([Observation, z.null()]),
  requested_id: z.union([z.string(), z.null()]),
  requested_intent: z.union([Rearm, z.null()]),
  status: z.literal("available"),
  workflow_held: z.boolean(),
});
const InactiveControl = z.object({
  issue: z.number().int(),
  repository: z.string(),
  status: z.literal("inactive"),
});
const AbsentControl = z.object({
  epoch: z.number().int(),
  issue: z.number().int(),
  repository: z.string(),
  status: z.literal("absent"),
});
const UnavailableControl = z.object({
  reason: z.string(),
  status: z.literal("unavailable"),
});
const KeepGuidance = z.object({ kind: z.literal("keep") });
const ClearGuidance = z.object({ kind: z.literal("clear") });
const SetGuidance = z.object({
  kind: z.literal("set"),
  text: z.string().min(1).max(4000),
});
const RequestRearm = z.object({
  expected: ResolveExpected,
  gate_id: z.string().min(1).max(512),
  guidance: z.discriminatedUnion("kind", [
    KeepGuidance,
    ClearGuidance,
    SetGuidance,
  ]),
  operation: z.literal("request_rearm"),
  request_id: z.string().min(1).max(512),
});
const ContinueRearm = z.object({
  expected: ResolveExpected,
  operation: z.literal("continue_rearm"),
  request_id: z.string().min(1).max(512),
  stopped_request_id: z.string().min(1).max(512),
});
const CancelRearm = z.object({
  cancellation_id: z.string().min(1).max(512),
  expected: ResolveExpected,
  operation: z.literal("cancel_rearm"),
  reason: z.string().min(1).max(16384),
  request_id: z.string().min(1).max(512),
});
const AuthorizeRetry = z.object({
  authorization_id: z.string().min(1).max(512),
  command_id: z.string().min(1).max(512),
  evidence_id: z.string().min(1).max(512),
  expected: ResolveExpected,
  gate_id: z.string().min(1).max(512),
  operation: z.literal("authorize_retry"),
});
const api_resolve_command_api_repos__name__issues__number__resolve_control_commands_post_Body =
  z.discriminatedUnion("operation", [
    RequestRearm,
    ContinueRearm,
    CancelRearm,
    AuthorizeRetry,
  ]);
const CommandResult = z.object({
  control: z.discriminatedUnion("status", [
    AvailableControl,
    InactiveControl,
    AbsentControl,
    UnavailableControl,
  ]),
  outcome: z.enum([
    "accepted",
    "replayed",
    "conflict",
    "rejected",
    "unavailable",
  ]),
  reason: z.string(),
});
const StepEventsPayload = z
  .object({
    events: z.array(z.object({}).partial().passthrough()),
    warnings: z.array(z.string()),
  })
  .passthrough();
const UnblockRequest = z.object({}).partial();
const api_unblock_api_repos__name__issues__number__unblock_post_Body = z.union([
  UnblockRequest,
  z.null(),
]);
const Caps = z
  .object({
    daily_cap_usd: z.union([z.number(), z.null()]),
    issue_cap_usd: z.union([z.number(), z.null()]),
    today_usd: z.union([z.number(), z.null()]),
  })
  .passthrough();
const CostMix = z
  .object({
    repair_usd: z.union([z.number(), z.null()]),
    resolve_usd: z.union([z.number(), z.null()]),
    review_usd: z.union([z.number(), z.null()]),
    steward_usd: z.union([z.number(), z.null()]),
  })
  .passthrough();
const EscalationReason = z
  .object({
    issue: z.number().int(),
    reason: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const HealthTrendDay = z
  .object({
    closed: z.number().int(),
    day: z.string(),
    first_observation: z.boolean(),
    net_open: z.number().int(),
    opened: z.number().int(),
    recurrences: z.number().int(),
  })
  .passthrough();
const InterventionEvent = z
  .object({
    action_class: z.string(),
    detail: z.union([z.string(), z.null()]).optional(),
    issue: z.union([z.number(), z.null()]).optional(),
    kind: z.string(),
    pr: z.union([z.number(), z.null()]).optional(),
    ts: z.union([z.string(), z.null()]),
  })
  .passthrough();
const KindCost = z
  .object({ kind: z.string(), runs: z.number().int(), usd: z.number() })
  .passthrough();
const InterventionCount = z
  .object({ count: z.number().int(), name: z.string() })
  .passthrough();
const Metrics = z
  .object({
    by_kind: z.array(KindCost),
    cost_per_done_usd: z.union([z.number(), z.null()]),
    done_issues: z.number().int(),
    escalations: z.number().int(),
    first_pass_approvals: z.union([z.number(), z.null()]),
    first_pass_changes: z.union([z.number(), z.null()]),
    first_pass_rate: z.union([z.number(), z.null()]),
    in_flight_issues: z.number().int(),
    interventions_by_class: z.array(InterventionCount).optional().default([]),
    interventions_per_merged_pr: z.union([z.number(), z.null()]).optional(),
    interventions_total: z.number().int().optional().default(0),
    recurrences: z.number().int(),
    rounds_per_pr: z.union([z.number(), z.null()]),
    spend_blocks: z.number().int(),
    today_usd: z.union([z.number(), z.null()]),
    total_cost_usd: z.union([z.number(), z.null()]),
  })
  .passthrough();
const AttentionItem = z
  .object({
    detail: z.union([z.string(), z.null()]).optional(),
    issue: z.number().int(),
    kind: z.string(),
    title: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const SpendBucket = z
  .object({ hour_start_iso: z.string(), usd: z.number() })
  .passthrough();
const OverviewPayload = z
  .object({
    caps: z.union([Caps, z.null()]),
    cost_mix: z.union([CostMix, z.null()]),
    escalation_reasons: z.array(EscalationReason).optional().default([]),
    first_observed_day: z.union([z.string(), z.null()]).optional(),
    health: z.array(RoleHealth),
    health_trend: z.array(HealthTrendDay),
    interventions: z.array(InterventionEvent).optional().default([]),
    metrics: z.union([Metrics, z.null()]),
    needs_attention: z.array(AttentionItem).optional().default([]),
    spend_per_hour: z.array(SpendBucket).optional().default([]),
    unattributed_count: z.number().int().optional().default(0),
    warnings: z.array(z.string()),
  })
  .passthrough();
const ReviewFreshReviewRequired = z.object({
  kind: z.literal("fresh_review_required"),
  owed_since_generation: z.number().int(),
});
const ReviewBoundSubmission = z.object({
  command_id: z.string(),
  generation: z.number().int(),
  kind: z.literal("bound_submission"),
});
const ReviewImportedBaseline = z.object({
  evidence_id: z.string(),
  kind: z.literal("imported"),
});
const ReviewReviewedBaseline = z.object({
  head_sha: z.string(),
  kind: z.literal("reviewed_baseline"),
  patch_id: z.string(),
  reviewed_generation: z.number().int(),
  source: z.discriminatedUnion("kind", [
    ReviewBoundSubmission,
    ReviewImportedBaseline,
  ]),
});
const ReviewDismissalObligation = z.object({
  kind: z.literal("dismiss_review"),
  review_id: z.string(),
});
const ReviewProjectionObligation = z.object({
  add: z.string(),
  kind: z.literal("projection"),
  remove: z.union([z.string(), z.null()]),
});
const ReviewUnlabelledObligation = z.object({ kind: z.literal("unlabelled") });
const ReviewRearmCommand = z.object({
  command_id: z.string(),
  obligation: z.discriminatedUnion("kind", [
    ReviewDismissalObligation,
    ReviewProjectionObligation,
    ReviewUnlabelledObligation,
  ]),
  status: z.enum([
    "pending",
    "verified",
    "refused",
    "uncertain",
    "held",
    "unavailable",
  ]),
});
const ReviewPendingRearm = z.object({
  authorized_reviews: z.array(z.string()),
  commands: z.array(ReviewRearmCommand),
  gate_id: z.string(),
  generation: z.number().int(),
  request_id: z.string(),
  state: z.literal("pending"),
});
const ReviewCompletedRearm = z.object({
  gate_id: z.string(),
  generation: z.number().int(),
  request_id: z.string(),
  state: z.literal("completed"),
  verified_commands: z.array(z.string()),
});
const ReviewStoppedRearm = z.object({
  authorized_reviews: z.array(z.string()),
  gate_id: z.string(),
  generation: z.number().int(),
  reason: z.string(),
  request_id: z.string(),
  retired_commands: z.array(z.string()),
  state: z.enum(["cancelled", "superseded"]),
  verified_commands: z.array(z.string()),
});
const ReviewDispatchTerminal = z.object({
  kind: z.literal("terminal"),
  state: z.enum(["closed", "merged"]),
});
const ReviewDispatchNotAdmitted = z.object({
  holds: z.array(z.string()),
  kind: z.literal("not_admitted"),
});
const ReviewDispatchDeferred = z.object({
  kind: z.literal("deferred"),
  reason: z.string(),
});
const ReviewDispatchHeld = z.object({
  gate_ids: z.array(z.string()),
  kind: z.literal("held"),
});
const ReviewDispatchSubmissionUnresolved = z.object({
  command_ids: z.array(z.string()),
  kind: z.literal("submission_unresolved"),
});
const ReviewDispatchReleasePending = z.object({
  command_id: z.string(),
  kind: z.literal("release_projection_pending"),
});
const ReviewDispatchOwed = z.object({
  kind: z.literal("owed"),
  review: z.enum(["initial", "re_review"]),
});
const ReviewDispatchWithheld = z.object({
  detail: z.string(),
  kind: z.literal("withheld"),
  reason: z.enum(["fix_claim", "reserved"]),
  review: z.enum(["initial", "re_review"]),
});
const ReviewDispatchInFlight = z.object({
  kind: z.literal("in_flight"),
  review: z.enum(["initial", "re_review"]),
});
const ReviewDispatchNotOwed = z.object({
  kind: z.literal("not_owed"),
  reason: z.string(),
});
const ReviewDecisionBasis = z.object({
  external_holds: z.array(z.string()),
  head_sha: z.string(),
  labels: z.array(z.string()),
  state: z.enum(["open", "closed", "merged"]),
});
const ReviewReadFailure = z.object({
  failed_reads: z.number().int(),
  first_failed_at: z.string(),
  last_failed_at: z.string(),
  reason: z.string(),
});
const ReviewDaemonRecorded = z.object({
  as_of: z.string(),
  decided_at: z.string(),
  dispatch: z.discriminatedUnion("kind", [
    ReviewDispatchTerminal,
    ReviewDispatchNotAdmitted,
    ReviewDispatchDeferred,
    ReviewDispatchHeld,
    ReviewDispatchSubmissionUnresolved,
    ReviewDispatchReleasePending,
    ReviewDispatchOwed,
    ReviewDispatchWithheld,
    ReviewDispatchInFlight,
    ReviewDispatchNotOwed,
  ]),
  pull_request: z.union([ReviewDecisionBasis, z.null()]),
  read: z.enum(["review_read", "departed_read"]),
  read_failure: z.union([ReviewReadFailure, z.null()]),
  state: z.literal("recorded"),
});
const ReviewDaemonUnrecorded = z.object({ state: z.literal("unrecorded") });
const ReviewDaemonUnreadable = z.object({
  reason: z.string(),
  state: z.literal("unreadable"),
});
const ReviewExpected = z.object({
  episode_id: z.string().min(1).max(512),
  epoch: z.number().int().gte(1).lte(9007199254740991),
  family: z.literal("review_rejections"),
  generation: z.number().int().gte(0).lte(9007199254740991),
  pull_request: z.number().int().gte(1).lte(9007199254740991),
  repository: z.string().min(1).max(512),
  version: z.number().int().gte(0).lte(9007199254740991),
});
const ReviewActionOffered = z.object({
  effect: z.string(),
  operation: z.enum([
    "request_rearm",
    "continue_rearm",
    "cancel_rearm",
    "authorize_retry",
  ]),
  state: z.literal("offered"),
});
const ReviewActionRefused = z.object({
  operation: z.enum([
    "request_rearm",
    "continue_rearm",
    "cancel_rearm",
    "authorize_retry",
  ]),
  reason: z.string(),
  state: z.literal("refused"),
});
const ReviewCountedReview = z.object({
  author: z.string(),
  commit_id: z.union([z.string(), z.null()]),
  review_id: z.string(),
});
const ReviewHoldUncontested = z.object({
  restore_limit: z.number().int(),
  restores: z.number().int(),
  state: z.literal("restoring"),
});
const ReviewHoldContested = z.object({
  restore_limit: z.number().int(),
  restores: z.number().int(),
  state: z.literal("contested"),
});
const ReviewHoldUnreadable = z.object({
  reason: z.string(),
  state: z.literal("unreadable"),
});
const ReviewRejectionCeilingCause = z.object({
  authorization_bound: z.number().int(),
  ceiling: z.number().int(),
  counted: z.array(ReviewCountedReview),
  counted_total: z.number().int(),
  episode_id: z.string(),
  head_sha: z.string(),
  hold: z.discriminatedUnion("state", [
    ReviewHoldUncontested,
    ReviewHoldContested,
    ReviewHoldUnreadable,
  ]),
  kind: z.literal("review_rejection_ceiling"),
  non_resetting_commits: z.array(z.string()),
  review_bot: z.string(),
});
const ReviewRemoteUncertainCause = z.object({
  command_id: z.string(),
  failed_reads: z.number().int(),
  kind: z.literal("remote_uncertain"),
  last_error: z.string(),
  unresolved_since: z.string(),
});
const ReviewRetryAuthorizationRequiredCause = z.object({
  absence_evidence_id: z.string(),
  command_id: z.string(),
  kind: z.literal("retry_authorization_required"),
  retry_authorizable: z.boolean(),
});
const ReviewEvidenceUnavailableCause = z.object({
  evidence: z.enum([
    "exemption_trailers",
    "review_history",
    "prewrite_read",
    "rearm_observation",
    "other",
  ]),
  failed_reads: z.number().int(),
  kind: z.literal("evidence_unavailable"),
  last_error: z.string(),
  source: z.string(),
  unresolved_since: z.string(),
});
const ReviewRearmNeedsDecisionCause = z.object({
  kind: z.literal("rearm_needs_decision"),
  obligations: z.array(z.string()),
  reason: z.string(),
  stopped_request_id: z.string(),
});
const ReviewProjectionRefusedCause = z.object({
  command_id: z.string(),
  generation: z.number().int(),
  kind: z.literal("review_projection_refused"),
  projection: z.enum(["ceiling_release", "escalation_hold"]),
  reason: z.string(),
});
const ReviewGateFactsUnavailableCause = z.object({
  kind: z.literal("facts_unavailable"),
  reason: z.string(),
  underlying: z.enum([
    "review_rejection_ceiling",
    "remote_uncertain",
    "retry_authorization_required",
    "evidence_unavailable",
    "rearm_needs_decision",
    "review_projection_refused",
  ]),
});
const ReviewPendingRetry = z.object({
  authorization_id: z.string(),
  command_id: z.string(),
});
const ReviewNoRemediation = z.object({
  kind: z.literal("none_authorized"),
  reason: z.string(),
});
const ReviewAttemptedRemediation = z.object({
  evidence_ids: z.array(z.string()),
  kind: z.literal("attempted"),
  operation: z.string(),
  outcome: z.string(),
});
const ReviewGate = z.object({
  actions: z.array(
    z.discriminatedUnion("state", [ReviewActionOffered, ReviewActionRefused])
  ),
  cause: z.discriminatedUnion("kind", [
    ReviewRejectionCeilingCause,
    ReviewRemoteUncertainCause,
    ReviewRetryAuthorizationRequiredCause,
    ReviewEvidenceUnavailableCause,
    ReviewRearmNeedsDecisionCause,
    ReviewProjectionRefusedCause,
    ReviewGateFactsUnavailableCause,
  ]),
  gate_id: z.string(),
  opened_generation: z.number().int(),
  pending_retry: z.union([ReviewPendingRetry, z.null()]),
  remediation: z.discriminatedUnion("kind", [
    ReviewNoRemediation,
    ReviewAttemptedRemediation,
  ]),
  state: z.enum(["open", "rearming"]),
});
const ReviewObservation = z.object({
  failed_reads: z.number().int(),
  last_error: z.string(),
  next_read_at: z.string(),
  unresolved_since: z.string(),
});
const ReviewAvailableControl = z.object({
  continuation: z.discriminatedUnion("kind", [
    ReviewFreshReviewRequired,
    ReviewReviewedBaseline,
  ]),
  current_intent: z.union([
    z.discriminatedUnion("state", [
      ReviewPendingRearm,
      ReviewCompletedRearm,
      ReviewStoppedRearm,
    ]),
    z.null(),
  ]),
  daemon: z.discriminatedUnion("state", [
    ReviewDaemonRecorded,
    ReviewDaemonUnrecorded,
    ReviewDaemonUnreadable,
  ]),
  expected: ReviewExpected,
  gates: z.array(ReviewGate),
  guidance: z.string(),
  observation: z.union([ReviewObservation, z.null()]),
  requested_id: z.union([z.string(), z.null()]),
  requested_intent: z.union([
    z.discriminatedUnion("state", [
      ReviewPendingRearm,
      ReviewCompletedRearm,
      ReviewStoppedRearm,
    ]),
    z.null(),
  ]),
  status: z.literal("available"),
  workflow_held: z.boolean(),
});
const ReviewMigrationRequiredControl = z.object({
  active_families: z.array(z.string()),
  pull_request: z.number().int(),
  repository: z.string(),
  status: z.literal("migration_required"),
});
const ReviewAbsentControl = z.object({
  epoch: z.number().int(),
  pull_request: z.number().int(),
  repository: z.string(),
  status: z.literal("absent"),
});
const ReviewUnavailableControl = z.object({
  reason: z.string(),
  status: z.literal("unavailable"),
});
const ReviewRequestRearm = z.object({
  expected: ReviewExpected,
  gate_id: z.string().min(1).max(512),
  guidance: z.discriminatedUnion("kind", [
    KeepGuidance,
    ClearGuidance,
    SetGuidance,
  ]),
  operation: z.literal("request_rearm"),
  request_id: z.string().min(1).max(512),
});
const ReviewContinueRearm = z.object({
  expected: ReviewExpected,
  operation: z.literal("continue_rearm"),
  request_id: z.string().min(1).max(512),
  stopped_request_id: z.string().min(1).max(512),
});
const ReviewCancelRearm = z.object({
  cancellation_id: z.string().min(1).max(512),
  expected: ReviewExpected,
  operation: z.literal("cancel_rearm"),
  reason: z.string().min(1).max(16384),
  request_id: z.string().min(1).max(512),
});
const ReviewAuthorizeRetry = z.object({
  authorization_id: z.string().min(1).max(512),
  command_id: z.string().min(1).max(512),
  evidence_id: z.string().min(1).max(512),
  expected: ReviewExpected,
  gate_id: z.string().min(1).max(512),
  operation: z.literal("authorize_retry"),
});
const api_review_command_api_repos__name__pulls__pr__review_control_commands_post_Body =
  z.discriminatedUnion("operation", [
    ReviewRequestRearm,
    ReviewContinueRearm,
    ReviewCancelRearm,
    ReviewAuthorizeRetry,
  ]);
const ReviewCommandResult = z.object({
  control: z.discriminatedUnion("status", [
    ReviewAvailableControl,
    ReviewMigrationRequiredControl,
    ReviewAbsentControl,
    ReviewUnavailableControl,
  ]),
  outcome: z.enum([
    "accepted",
    "replayed",
    "conflict",
    "rejected",
    "unavailable",
  ]),
  reason: z.string(),
});
const WorkflowFamiliesAvailable = z.object({
  active_families: z.array(z.enum(["resolve", "review_rejections"])),
  repository: z.string(),
  status: z.literal("available"),
});
const WorkflowFamiliesUnavailable = z.object({
  reason: z.string(),
  status: z.literal("unavailable"),
});
const ClaudeUsage = z
  .object({
    error: z.union([z.string(), z.null()]),
    five_hour_pct: z.union([z.number(), z.null()]),
    five_hour_resets_at: z.union([z.string(), z.null()]),
    seven_day_pct: z.union([z.number(), z.null()]),
    seven_day_resets_at: z.union([z.string(), z.null()]),
    source: z.union([z.string(), z.null()]),
  })
  .passthrough();
const CodexUsage = z
  .object({
    error: z.union([z.string(), z.null()]),
    plan: z.union([z.string(), z.null()]),
    resets_at: z.union([z.string(), z.null()]),
    sampled_at: z.union([z.string(), z.null()]),
    source: z.union([z.string(), z.null()]),
    weekly_pct: z.union([z.number(), z.null()]),
    window_minutes: z.union([z.number(), z.null()]),
  })
  .passthrough();
const HarnessRepoSpend = z
  .object({
    daily_cap_usd: z.union([z.number(), z.null()]),
    name: z.string(),
    repo: z.union([z.string(), z.null()]).optional(),
    today_usd: z.union([z.number(), z.null()]),
  })
  .passthrough();
const HarnessUsage = z
  .object({
    global_daily_cap_usd: z.union([z.number(), z.null()]),
    per_repo: z.array(HarnessRepoSpend),
    today_all_repos_usd: z.union([z.number(), z.null()]),
  })
  .passthrough();
const UsagePayload = z
  .object({
    claude: z.union([ClaudeUsage, z.null()]),
    codex: z.union([CodexUsage, z.null()]),
    harness: z.union([HarnessUsage, z.null()]),
    warnings: z.array(z.string()),
  })
  .passthrough();
const VerificationRow = z
  .object({
    change: z.union([z.string(), z.null()]),
    evidence: z.union([z.string(), z.null()]),
    id: z.union([z.string(), z.null()]),
    raw: z.string(),
    reason: z.union([z.string(), z.null()]).optional(),
    shipped: z.union([z.string(), z.null()]),
    status: z.union([z.string(), z.null()]),
  })
  .passthrough();
const VerificationPayload = z
  .object({ rows: z.array(VerificationRow), warnings: z.array(z.string()) })
  .passthrough();

export const schemas = {
  RoleHealth,
  RepoSummary,
  ReposPayload,
  BacklogCard,
  ModelBadge,
  CardStep,
  IssueCard,
  ToastNotification,
  PendingCard,
  IssuesPayload,
  ValidationError,
  HTTPValidationError,
  PullRequestLink,
  IssueStepChange,
  IssueStepEscalation,
  IssueStepReview,
  TokenUsage,
  IssueStep,
  IssueDetailPayload,
  ActionResult,
  request_id,
  Rearm,
  ResolveExpected,
  ResolveCeiling,
  RemoteUncertain,
  RetryAuthorizationRequired,
  EvidenceUnavailable,
  RearmNeedsDecision,
  PendingRetry,
  NoRemediation,
  AttemptedRemediation,
  Gate,
  Observation,
  AvailableControl,
  InactiveControl,
  AbsentControl,
  UnavailableControl,
  KeepGuidance,
  ClearGuidance,
  SetGuidance,
  RequestRearm,
  ContinueRearm,
  CancelRearm,
  AuthorizeRetry,
  api_resolve_command_api_repos__name__issues__number__resolve_control_commands_post_Body,
  CommandResult,
  StepEventsPayload,
  UnblockRequest,
  api_unblock_api_repos__name__issues__number__unblock_post_Body,
  Caps,
  CostMix,
  EscalationReason,
  HealthTrendDay,
  InterventionEvent,
  KindCost,
  InterventionCount,
  Metrics,
  AttentionItem,
  SpendBucket,
  OverviewPayload,
  ReviewFreshReviewRequired,
  ReviewBoundSubmission,
  ReviewImportedBaseline,
  ReviewReviewedBaseline,
  ReviewDismissalObligation,
  ReviewProjectionObligation,
  ReviewUnlabelledObligation,
  ReviewRearmCommand,
  ReviewPendingRearm,
  ReviewCompletedRearm,
  ReviewStoppedRearm,
  ReviewDispatchTerminal,
  ReviewDispatchNotAdmitted,
  ReviewDispatchDeferred,
  ReviewDispatchHeld,
  ReviewDispatchSubmissionUnresolved,
  ReviewDispatchReleasePending,
  ReviewDispatchOwed,
  ReviewDispatchWithheld,
  ReviewDispatchInFlight,
  ReviewDispatchNotOwed,
  ReviewDecisionBasis,
  ReviewReadFailure,
  ReviewDaemonRecorded,
  ReviewDaemonUnrecorded,
  ReviewDaemonUnreadable,
  ReviewExpected,
  ReviewActionOffered,
  ReviewActionRefused,
  ReviewCountedReview,
  ReviewHoldUncontested,
  ReviewHoldContested,
  ReviewHoldUnreadable,
  ReviewRejectionCeilingCause,
  ReviewRemoteUncertainCause,
  ReviewRetryAuthorizationRequiredCause,
  ReviewEvidenceUnavailableCause,
  ReviewRearmNeedsDecisionCause,
  ReviewProjectionRefusedCause,
  ReviewGateFactsUnavailableCause,
  ReviewPendingRetry,
  ReviewNoRemediation,
  ReviewAttemptedRemediation,
  ReviewGate,
  ReviewObservation,
  ReviewAvailableControl,
  ReviewMigrationRequiredControl,
  ReviewAbsentControl,
  ReviewUnavailableControl,
  ReviewRequestRearm,
  ReviewContinueRearm,
  ReviewCancelRearm,
  ReviewAuthorizeRetry,
  api_review_command_api_repos__name__pulls__pr__review_control_commands_post_Body,
  ReviewCommandResult,
  WorkflowFamiliesAvailable,
  WorkflowFamiliesUnavailable,
  ClaudeUsage,
  CodexUsage,
  HarnessRepoSpend,
  HarnessUsage,
  UsagePayload,
  VerificationRow,
  VerificationPayload,
};

const endpoints = makeApi([
  {
    method: "get",
    path: "/:full_path",
    alias: "spa__full_path__get",
    requestFormat: "json",
    parameters: [
      {
        name: "full_path",
        type: "Path",
        schema: z.string(),
      },
    ],
    response: z.unknown(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/repos",
    alias: "api_repos_api_repos_get",
    requestFormat: "json",
    response: z
      .object({ repos: z.array(RepoSummary), warnings: z.array(z.string()) })
      .passthrough(),
  },
  {
    method: "get",
    path: "/api/repos/:name/issues",
    alias: "api_issues_api_repos__name__issues_get",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
    ],
    response: z
      .object({
        backlog: z.array(BacklogCard),
        done: z.array(IssueCard),
        gh_ok: z.boolean(),
        in_flight: z.array(IssueCard),
        notifications: z.array(ToastNotification).optional().default([]),
        pending: z.array(PendingCard),
        warnings: z.array(z.string()),
      })
      .passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/repos/:name/issues/:number",
    alias: "api_issue_api_repos__name__issues__number__get",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: z
      .object({
        attention_reason: z.union([z.string(), z.null()]).optional(),
        cost_usd: z.union([z.number(), z.null()]),
        done: z.boolean(),
        elapsed_s: z.union([z.number(), z.null()]),
        escalation_reason: z.union([z.string(), z.null()]).optional(),
        events: z.array(z.object({}).partial().passthrough()),
        idle_active: z.boolean(),
        idle_s: z.union([z.number(), z.null()]),
        issue: z.number().int(),
        leg_running: z.boolean().optional().default(false),
        pr_ci_state: z
          .union([z.enum(["passing", "failing", "pending"]), z.null()])
          .optional(),
        prs: z.array(z.number().int()),
        pull_requests: z.array(PullRequestLink),
        stage: z.union([z.string(), z.null()]),
        steps: z.array(IssueStep),
        title: z.union([z.string(), z.null()]),
        url: z.union([z.string(), z.null()]),
        warnings: z.array(z.string()),
        working_s: z.union([z.number(), z.null()]),
      })
      .passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/repos/:name/issues/:number/cost-chart",
    alias: "api_cost_chart_api_repos__name__issues__number__cost_chart_get",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
      {
        name: "session_id",
        type: "Query",
        schema: z.string().optional().default(""),
      },
    ],
    response: z.unknown(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "post",
    path: "/api/repos/:name/issues/:number/park",
    alias: "api_park_api_repos__name__issues__number__park_post",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: z.object({ detail: z.string(), ok: z.boolean() }).passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "post",
    path: "/api/repos/:name/issues/:number/priority",
    alias: "api_priority_api_repos__name__issues__number__priority_post",
    requestFormat: "json",
    parameters: [
      {
        name: "body",
        type: "Body",
        schema: z.object({}).partial().passthrough().default({}),
      },
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: z.object({ detail: z.string(), ok: z.boolean() }).passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "post",
    path: "/api/repos/:name/issues/:number/promote",
    alias: "api_promote_api_repos__name__issues__number__promote_post",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: z.object({ detail: z.string(), ok: z.boolean() }).passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/repos/:name/issues/:number/resolve-control",
    alias:
      "api_resolve_control_api_repos__name__issues__number__resolve_control_get",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
      {
        name: "request_id",
        type: "Query",
        schema: request_id,
      },
    ],
    response: z.discriminatedUnion("status", [
      AvailableControl,
      InactiveControl,
      AbsentControl,
      UnavailableControl,
    ]),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "post",
    path: "/api/repos/:name/issues/:number/resolve-control/commands",
    alias:
      "api_resolve_command_api_repos__name__issues__number__resolve_control_commands_post",
    requestFormat: "json",
    parameters: [
      {
        name: "body",
        type: "Body",
        schema:
          api_resolve_command_api_repos__name__issues__number__resolve_control_commands_post_Body,
      },
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: CommandResult,
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/repos/:name/issues/:number/steps/:idx/events",
    alias:
      "api_step_events_api_repos__name__issues__number__steps__idx__events_get",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
      {
        name: "idx",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: z
      .object({
        events: z.array(z.object({}).partial().passthrough()),
        warnings: z.array(z.string()),
      })
      .passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "post",
    path: "/api/repos/:name/issues/:number/unblock",
    alias: "api_unblock_api_repos__name__issues__number__unblock_post",
    requestFormat: "json",
    parameters: [
      {
        name: "body",
        type: "Body",
        schema: api_unblock_api_repos__name__issues__number__unblock_post_Body,
      },
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: z.object({ detail: z.string(), ok: z.boolean() }).passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "post",
    path: "/api/repos/:name/issues/:number/unpark",
    alias: "api_unpark_api_repos__name__issues__number__unpark_post",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "number",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: z.object({ detail: z.string(), ok: z.boolean() }).passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/repos/:name/overview",
    alias: "api_overview_api_repos__name__overview_get",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "timezone",
        type: "Query",
        schema: request_id,
      },
    ],
    response: z
      .object({
        caps: z.union([Caps, z.null()]),
        cost_mix: z.union([CostMix, z.null()]),
        escalation_reasons: z.array(EscalationReason).optional().default([]),
        first_observed_day: z.union([z.string(), z.null()]).optional(),
        health: z.array(RoleHealth),
        health_trend: z.array(HealthTrendDay),
        interventions: z.array(InterventionEvent).optional().default([]),
        metrics: z.union([Metrics, z.null()]),
        needs_attention: z.array(AttentionItem).optional().default([]),
        spend_per_hour: z.array(SpendBucket).optional().default([]),
        unattributed_count: z.number().int().optional().default(0),
        warnings: z.array(z.string()),
      })
      .passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/repos/:name/pulls/:pr/review-control",
    alias: "api_review_control_api_repos__name__pulls__pr__review_control_get",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "pr",
        type: "Path",
        schema: z.number().int(),
      },
      {
        name: "request_id",
        type: "Query",
        schema: request_id,
      },
    ],
    response: z.discriminatedUnion("status", [
      ReviewAvailableControl,
      ReviewMigrationRequiredControl,
      ReviewAbsentControl,
      ReviewUnavailableControl,
    ]),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "post",
    path: "/api/repos/:name/pulls/:pr/review-control/commands",
    alias:
      "api_review_command_api_repos__name__pulls__pr__review_control_commands_post",
    requestFormat: "json",
    parameters: [
      {
        name: "body",
        type: "Body",
        schema:
          api_review_command_api_repos__name__pulls__pr__review_control_commands_post_Body,
      },
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
      {
        name: "pr",
        type: "Path",
        schema: z.number().int(),
      },
    ],
    response: ReviewCommandResult,
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "post",
    path: "/api/repos/:name/start",
    alias: "api_start_api_repos__name__start_post",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
    ],
    response: z.object({ detail: z.string(), ok: z.boolean() }).passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/repos/:name/workflow-families",
    alias: "api_workflow_families_api_repos__name__workflow_families_get",
    requestFormat: "json",
    parameters: [
      {
        name: "name",
        type: "Path",
        schema: z.string(),
      },
    ],
    response: z.discriminatedUnion("status", [
      WorkflowFamiliesAvailable,
      WorkflowFamiliesUnavailable,
    ]),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/api/usage",
    alias: "api_usage_api_usage_get",
    requestFormat: "json",
    response: z
      .object({
        claude: z.union([ClaudeUsage, z.null()]),
        codex: z.union([CodexUsage, z.null()]),
        harness: z.union([HarnessUsage, z.null()]),
        warnings: z.array(z.string()),
      })
      .passthrough(),
  },
  {
    method: "get",
    path: "/api/verification",
    alias: "api_verification_api_verification_get",
    requestFormat: "json",
    response: z
      .object({ rows: z.array(VerificationRow), warnings: z.array(z.string()) })
      .passthrough(),
  },
]);

export const api = new Zodios(endpoints);

export function createApiClient(baseUrl: string, options?: ZodiosOptions) {
  return new Zodios(baseUrl, endpoints, options);
}
