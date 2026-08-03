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
    cost_usd: z.union([z.number(), z.null()]),
    duration_s: z.union([z.number(), z.null()]),
    effort: z.union([z.string(), z.null()]).optional(),
    estimated: z.boolean(),
    exit_reason: z.union([z.string(), z.null()]),
    has_transcript: z.boolean(),
    idx: z.number().int(),
    kind: z.string(),
    model: z.union([z.string(), z.null()]),
    note: z.union([z.string(), z.null()]).optional(),
    role: z.union([z.string(), z.null()]),
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
const StepEventsPayload = z
  .object({
    events: z.array(z.object({}).partial().passthrough()),
    warnings: z.array(z.string()),
  })
  .passthrough();
const timezone = z.union([z.string(), z.null()]).optional();
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
const SpendBucket = z
  .object({ hour_start_iso: z.string(), usd: z.number() })
  .passthrough();
const OverviewPayload = z
  .object({
    caps: z.union([Caps, z.null()]),
    cost_mix: z.union([CostMix, z.null()]),
    first_observed_day: z.union([z.string(), z.null()]).optional(),
    health: z.array(RoleHealth),
    health_trend: z.array(HealthTrendDay),
    metrics: z.union([Metrics, z.null()]),
    spend_per_hour: z.array(SpendBucket).optional().default([]),
    unattributed: z.array(z.object({}).partial().passthrough()),
    warnings: z.array(z.string()),
  })
  .passthrough();
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
  TokenUsage,
  IssueStep,
  IssueDetailPayload,
  ActionResult,
  StepEventsPayload,
  timezone,
  Caps,
  CostMix,
  HealthTrendDay,
  KindCost,
  InterventionCount,
  Metrics,
  SpendBucket,
  OverviewPayload,
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
        schema: timezone,
      },
    ],
    response: z
      .object({
        caps: z.union([Caps, z.null()]),
        cost_mix: z.union([CostMix, z.null()]),
        first_observed_day: z.union([z.string(), z.null()]).optional(),
        health: z.array(RoleHealth),
        health_trend: z.array(HealthTrendDay),
        metrics: z.union([Metrics, z.null()]),
        spend_per_hour: z.array(SpendBucket).optional().default([]),
        unattributed: z.array(z.object({}).partial().passthrough()),
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
