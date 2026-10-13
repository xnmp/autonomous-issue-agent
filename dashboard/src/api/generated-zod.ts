import { makeApi, Zodios, type ZodiosOptions } from "@zodios/core";
import { z } from "zod";

const Dispatch = z
  .object({ kind: z.string(), number: z.number().int() })
  .passthrough();
const RoleHealth = z
  .object({
    alive: z.union([z.boolean(), z.null()]),
    heartbeat_ts: z.union([z.string(), z.null()]),
    in_flight: z.array(Dispatch).optional().default([]),
    role: z.string(),
    stale: z.boolean(),
    state: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const RepoSummary = z
  .object({
    active: z.union([z.boolean(), z.null()]),
    done: z.union([z.number(), z.null()]),
    frecency: z.number(),
    in_flight: z.union([z.number(), z.null()]),
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
    title: z.union([z.string(), z.null()]),
    url: z.union([z.string(), z.null()]),
  })
  .passthrough();
const DoneCard = z
  .object({
    cost_usd: z.number(),
    issue: z.number().int(),
    labels: z.array(z.string()).optional().default([]),
    legs: z.number().int(),
    merged_ts: z.union([z.string(), z.null()]),
    pr: z.number().int(),
    pr_url: z.union([z.string(), z.null()]),
    review_rounds: z.number().int(),
    title: z.union([z.string(), z.null()]),
    url: z.union([z.string(), z.null()]),
  })
  .passthrough();
const BoardNeedsYou = z
  .object({
    detail: z.union([z.string(), z.null()]),
    headline: z.string(),
    hint: z.string(),
    reason: z.enum([
      "question",
      "attempts",
      "review-rounds",
      "lifetime-cap",
      "workflow-path",
      "duplicate",
      "labelled",
    ]),
    since_ts: z.union([z.string(), z.null()]),
    status: z.literal("needs_you"),
    target: z.enum(["issue", "pr"]),
  })
  .passthrough();
const BoardWorking = z
  .object({
    act: z.union([z.string(), z.null()]),
    activity: z.enum([
      "agent",
      "queued",
      "ci",
      "ci_queued",
      "merge",
      "update",
      "open_pr",
      "triage_note",
      "release",
    ]),
    headline: z.string(),
    reason: z.string(),
    since_ts: z.union([z.string(), z.null()]),
    status: z.literal("working"),
  })
  .passthrough();
const BoardWaiting = z
  .object({
    headline: z.string(),
    on: z.enum([
      "branch-protection",
      "human-review",
      "headroom",
      "backoff",
      "github",
      "slot",
    ]),
    reason: z.string(),
    since_ts: z.union([z.string(), z.null()]),
    status: z.literal("waiting"),
    until_ts: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const TrackChip = z
  .object({
    count: z.number().int(),
    kind: z.string(),
    outcome: z.enum([
      "running",
      "passed",
      "changes_requested",
      "asked",
      "failed",
      "done",
    ]),
  })
  .passthrough();
const StageVisit = z
  .object({
    stage: z.enum(["triage", "resolve", "ci", "review", "merge"]),
    steps: z.array(TrackChip),
  })
  .passthrough();
const StageRail = z
  .object({
    ci_runs: z.number().int(),
    current: z.enum(["triage", "resolve", "ci", "review", "merge"]),
    furthest: z.enum(["triage", "resolve", "ci", "review", "merge"]),
    live: z.boolean(),
    resolve_attempts: z.number().int(),
    review_rounds: z.number().int(),
    steps: z.number().int(),
  })
  .passthrough();
const IssueCard = z
  .object({
    action: z.union([z.enum(["unblock", "open"]), z.null()]).optional(),
    board: z.discriminatedUnion("status", [
      BoardNeedsYou,
      BoardWorking,
      BoardWaiting,
    ]),
    cost_usd: z.number(),
    estimated: z.boolean(),
    history: z.array(StageVisit).optional().default([]),
    issue: z.number().int(),
    labels: z.array(z.string()).optional().default([]),
    last_ts: z.union([z.string(), z.null()]),
    leg_running: z.boolean(),
    legs: z.number().int(),
    model: z.union([z.string(), z.null()]).optional(),
    pr: z.union([z.number(), z.null()]),
    pr_ci_state: z
      .union([z.enum(["passing", "failing", "pending"]), z.null()])
      .optional(),
    pr_url: z.union([z.string(), z.null()]),
    rail: StageRail,
    routed_tier: z.union([z.string(), z.null()]).optional(),
    runtime: z.union([z.string(), z.null()]).optional(),
    started_ts: z.union([z.string(), z.null()]),
    title: z.union([z.string(), z.null()]),
    triage_tier: z
      .union([z.enum(["low", "med", "high", "xhigh"]), z.null()])
      .optional(),
    url: z.union([z.string(), z.null()]),
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
const IssuesPayload = z
  .object({
    backlog: z.array(BacklogCard),
    done: z.array(DoneCard),
    gh_ok: z.boolean(),
    in_flight: z.array(IssueCard),
    merged_partial: z.boolean().optional().default(false),
    notifications: z.array(ToastNotification).optional().default([]),
    observed_at: z.union([z.string(), z.null()]).optional(),
    store_error: z.union([z.string(), z.null()]).optional(),
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
const CarriedLegs = z
  .object({ cost_usd: z.number(), legs: z.number().int() })
  .passthrough();
const PullRequestLink = z
  .object({
    ci_state: z
      .union([z.enum(["passing", "failing", "pending"]), z.null()])
      .optional(),
    number: z.number().int(),
    state: z.enum(["open", "merged"]),
    url: z.union([z.string(), z.null()]),
  })
  .passthrough();
const TokenUsage = z
  .object({
    cache_read_tokens: z.number().int(),
    cache_write_tokens: z.number().int(),
    input_tokens: z.number().int(),
    output_tokens: z.number().int(),
  })
  .passthrough();
const IssueStep = z
  .object({
    cost_usd: z.number(),
    duration_s: z.union([z.number(), z.null()]),
    end_reason: z.union([z.string(), z.null()]),
    ended_ts: z.union([z.string(), z.null()]),
    estimated: z.boolean(),
    head: z.union([z.string(), z.null()]),
    idx: z.number().int(),
    kind: z.string(),
    leg_id: z.string(),
    model: z.union([z.string(), z.null()]).optional(),
    outcome: z.enum([
      "running",
      "passed",
      "changes_requested",
      "asked",
      "failed",
      "done",
    ]),
    pr: z.union([z.number(), z.null()]),
    pricing_basis: z.union([z.string(), z.null()]),
    question: z.union([z.string(), z.null()]).optional(),
    role: z.union([z.string(), z.null()]),
    runtime: z.union([z.string(), z.null()]),
    session_id: z.union([z.string(), z.null()]),
    started_ts: z.union([z.string(), z.null()]),
    state: z.enum(["reserved", "running", "ended", "released"]),
    summary: z.union([z.string(), z.null()]).optional(),
    tier: z
      .union([z.enum(["low", "med", "high", "xhigh"]), z.null()])
      .optional(),
    usage: z.union([TokenUsage, z.null()]),
    verdict: z.union([z.string(), z.null()]).optional(),
    verdict_excerpt: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const TriageSummary = z
  .object({
    acceptance_criteria: z.array(z.string()).optional().default([]),
    duplicate_of: z.union([z.number(), z.null()]).optional(),
    duplicate_reason: z.union([z.string(), z.null()]).optional(),
    rationale: z.union([z.string(), z.null()]).optional(),
    tier: z.enum(["low", "med", "high", "xhigh"]),
  })
  .passthrough();
const IssueDetailPayload = z
  .object({
    action: z.union([z.enum(["unblock", "open"]), z.null()]).optional(),
    board: z
      .union([
        z.discriminatedUnion("status", [
          BoardNeedsYou,
          BoardWorking,
          BoardWaiting,
        ]),
        z.null(),
      ])
      .optional(),
    carried: z.union([CarriedLegs, z.null()]).optional(),
    cost_usd: z.number(),
    done: z.boolean(),
    estimated: z.boolean(),
    held_for_human: z.boolean().optional().default(false),
    history: z.array(StageVisit).optional().default([]),
    issue: z.number().int(),
    labels: z.array(z.string()).optional().default([]),
    observed_at: z.union([z.string(), z.null()]).optional(),
    pull_requests: z.array(PullRequestLink),
    rail: z.union([StageRail, z.null()]).optional(),
    steps: z.array(IssueStep),
    store_error: z.union([z.string(), z.null()]).optional(),
    title: z.union([z.string(), z.null()]),
    triage: z.union([TriageSummary, z.null()]).optional(),
    url: z.union([z.string(), z.null()]),
    warnings: z.array(z.string()),
  })
  .passthrough();
const ActionResult = z
  .object({ detail: z.string(), ok: z.boolean() })
  .passthrough();
const timezone = z.union([z.string(), z.null()]).optional();
const RoleHeadroom = z
  .object({
    fits: z.union([z.boolean(), z.null()]),
    leg_usd: z.number(),
    role: z.string(),
  })
  .passthrough();
const Caps = z
  .object({
    daily_cap_usd: z.union([z.number(), z.null()]),
    global_daily_cap_usd: z.union([z.number(), z.null()]),
    global_today_usd: z.union([z.number(), z.null()]).optional(),
    headroom: z.array(RoleHeadroom).optional().default([]),
    issue_cap_usd: z.union([z.number(), z.null()]),
    today_usd: z.union([z.number(), z.null()]),
  })
  .passthrough();
const HealthTrendDay = z
  .object({
    closed: z.number().int(),
    day: z.string(),
    net_open: z.number().int(),
    opened: z.number().int(),
  })
  .passthrough();
const LeaseRow = z
  .object({
    acquired_ts: z.union([z.string(), z.null()]),
    issue: z.union([z.number(), z.null()]),
    kind: z.union([z.string(), z.null()]),
    leg_id: z.union([z.string(), z.null()]),
    leg_state: z.union([z.string(), z.null()]),
    pid: z.number().int(),
    pr: z.union([z.number(), z.null()]),
    role: z.string(),
  })
  .passthrough();
const BasisCost = z
  .object({ basis: z.string(), usd: z.number() })
  .passthrough();
const KindCost = z
  .object({ kind: z.string(), runs: z.number().int(), usd: z.number() })
  .passthrough();
const Metrics = z
  .object({
    by_basis: z.array(BasisCost),
    by_kind: z.array(KindCost),
    cost_per_merged_pr_usd: z.union([z.number(), z.null()]),
    first_pass_approvals: z.number().int(),
    first_pass_changes: z.number().int(),
    first_pass_rate: z.union([z.number(), z.null()]),
    legs: z.number().int(),
    machine_legs: z.number().int(),
    merged_prs: z.union([z.number(), z.null()]),
    merged_prs_partial: z.boolean().optional().default(false),
    needs_human: z.union([z.number(), z.null()]),
    open_items: z.union([z.number(), z.null()]),
    rounds_per_pr: z.union([z.number(), z.null()]),
    total_cost_usd: z.number(),
  })
  .passthrough();
const AttentionItem = z
  .object({
    detail: z.union([z.string(), z.null()]).optional(),
    headline: z.string(),
    issue: z.number().int(),
    reason: z.enum([
      "question",
      "attempts",
      "review-rounds",
      "lifetime-cap",
      "workflow-path",
      "duplicate",
      "labelled",
    ]),
    title: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const SpendBucket = z
  .object({ hour_start_iso: z.string(), usd: z.number() })
  .passthrough();
const OverviewPayload = z
  .object({
    caps: z.union([Caps, z.null()]),
    health: z.array(RoleHealth),
    health_trend: z.array(HealthTrendDay).optional().default([]),
    leases: z.array(LeaseRow).optional().default([]),
    metrics: z.union([Metrics, z.null()]),
    needs_attention: z.array(AttentionItem).optional().default([]),
    observed_at: z.union([z.string(), z.null()]).optional(),
    spend_per_hour: z.array(SpendBucket).optional().default([]),
    store_error: z.union([z.string(), z.null()]).optional(),
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
  Dispatch,
  RoleHealth,
  RepoSummary,
  ReposPayload,
  BacklogCard,
  DoneCard,
  BoardNeedsYou,
  BoardWorking,
  BoardWaiting,
  TrackChip,
  StageVisit,
  StageRail,
  IssueCard,
  ToastNotification,
  IssuesPayload,
  ValidationError,
  HTTPValidationError,
  CarriedLegs,
  PullRequestLink,
  TokenUsage,
  IssueStep,
  TriageSummary,
  IssueDetailPayload,
  ActionResult,
  timezone,
  RoleHeadroom,
  Caps,
  HealthTrendDay,
  LeaseRow,
  BasisCost,
  KindCost,
  Metrics,
  AttentionItem,
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
        done: z.array(DoneCard),
        gh_ok: z.boolean(),
        in_flight: z.array(IssueCard),
        merged_partial: z.boolean().optional().default(false),
        notifications: z.array(ToastNotification).optional().default([]),
        observed_at: z.union([z.string(), z.null()]).optional(),
        store_error: z.union([z.string(), z.null()]).optional(),
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
        action: z.union([z.enum(["unblock", "open"]), z.null()]).optional(),
        board: z
          .union([
            z.discriminatedUnion("status", [
              BoardNeedsYou,
              BoardWorking,
              BoardWaiting,
            ]),
            z.null(),
          ])
          .optional(),
        carried: z.union([CarriedLegs, z.null()]).optional(),
        cost_usd: z.number(),
        done: z.boolean(),
        estimated: z.boolean(),
        held_for_human: z.boolean().optional().default(false),
        history: z.array(StageVisit).optional().default([]),
        issue: z.number().int(),
        labels: z.array(z.string()).optional().default([]),
        observed_at: z.union([z.string(), z.null()]).optional(),
        pull_requests: z.array(PullRequestLink),
        rail: z.union([StageRail, z.null()]).optional(),
        steps: z.array(IssueStep),
        store_error: z.union([z.string(), z.null()]).optional(),
        title: z.union([z.string(), z.null()]),
        triage: z.union([TriageSummary, z.null()]).optional(),
        url: z.union([z.string(), z.null()]),
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
        health: z.array(RoleHealth),
        health_trend: z.array(HealthTrendDay).optional().default([]),
        leases: z.array(LeaseRow).optional().default([]),
        metrics: z.union([Metrics, z.null()]),
        needs_attention: z.array(AttentionItem).optional().default([]),
        observed_at: z.union([z.string(), z.null()]).optional(),
        spend_per_hour: z.array(SpendBucket).optional().default([]),
        store_error: z.union([z.string(), z.null()]).optional(),
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
