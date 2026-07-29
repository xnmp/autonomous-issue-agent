/**
 * TypeScript mirror of docs/reference/dashboard-web.md.
 *
 * Every payload carries `warnings: string[]` (the backend's read-only failure
 * posture: collectors degrade to null/empty + a warning, never a 500), so the
 * UI can always render *something* and say what is missing.
 *
 * Fields the contract documents as nullable are `| null` here rather than
 * optional: the backend emits the key with a null value.
 */

export type Nullable<T> = T | null;

export interface Warned {
  warnings: string[];
}

/* ------------------------------------------------------------------ repos */

export type RoleName = "fix" | "review" | "steward";

export interface RoleHealth {
  role: RoleName | string;
  alive: boolean;
  stale: boolean;
  last_event_ts: Nullable<string>;
  last_event: Nullable<string>;
  events: number;
  log_path: Nullable<string>;
}

export interface RepoSummary {
  name: string;
  repo: Nullable<string>;
  target_dir: Nullable<string>;
  is_self: boolean;
  active: boolean;
  roles: RoleHealth[];
  frecency: number;
  last_activity_ts: Nullable<string>;
  in_flight: number;
  done: number;
}

export interface ReposPayload extends Warned {
  repos: RepoSummary[];
  /** present only when /proc scanning failed and staleness was used instead */
  liveness?: "journal-heuristic" | string;
}

/* ------------------------------------------------------------------ usage */

export interface ClaudeUsage {
  five_hour_pct: Nullable<number>;
  five_hour_resets_at: Nullable<string>;
  seven_day_pct: Nullable<number>;
  seven_day_resets_at: Nullable<string>;
  source: Nullable<string>;
  error: Nullable<string>;
}

export interface CodexUsage {
  weekly_pct: Nullable<number>;
  window_minutes: Nullable<number>;
  resets_at: Nullable<string>;
  plan: Nullable<string>;
  source: Nullable<string>;
  sampled_at: Nullable<string>;
  error: Nullable<string>;
}

export interface HarnessRepoSpend {
  name: string;
  today_usd: Nullable<number>;
  daily_cap_usd: Nullable<number>;
}

export interface HarnessUsage {
  global_daily_cap_usd: Nullable<number>;
  today_all_repos_usd: Nullable<number>;
  per_repo: HarnessRepoSpend[];
}

export interface UsagePayload extends Warned {
  claude: Nullable<ClaudeUsage>;
  codex: Nullable<CodexUsage>;
  harness: Nullable<HarnessUsage>;
}

/* ----------------------------------------------------------- verification */

export type VerificationStatus =
  | "VERIFIED"
  | "MISBEHAVED"
  | "pending"
  | (string & {});

export interface VerificationRow {
  id: Nullable<string>;
  change: Nullable<string>;
  shipped: Nullable<string>;
  status: Nullable<VerificationStatus>;
  evidence: Nullable<string>;
  raw: string;
}

export interface VerificationPayload extends Warned {
  rows: VerificationRow[];
}

/* --------------------------------------------------------------- overview */

export interface KindCost {
  kind: string;
  usd: number;
  runs: number;
}

export interface Metrics {
  done_issues: number;
  in_flight_issues: number;
  total_cost_usd: Nullable<number>;
  cost_per_done_usd: Nullable<number>;
  first_pass_approvals: Nullable<number>;
  first_pass_changes: Nullable<number>;
  first_pass_rate: Nullable<number>;
  rounds_per_pr: Nullable<number>;
  escalations: number;
  recurrences: number;
  spend_blocks: number;
  today_usd: Nullable<number>;
  by_kind: KindCost[];
}

export interface CostMix {
  resolve_usd: Nullable<number>;
  review_usd: Nullable<number>;
  repair_usd: Nullable<number>;
  steward_usd: Nullable<number>;
}

export interface Caps {
  issue_cap_usd: Nullable<number>;
  daily_cap_usd: Nullable<number>;
  today_usd: Nullable<number>;
}

/** Journal events are open-shaped; `kind`/`ts` are the two we rely on. */
export interface JournalEvent {
  ts?: string;
  kind?: string;
  event?: string;
  role?: string;
  issue?: number;
  number?: number;
  [k: string]: unknown;
}

export interface OverviewPayload extends Warned {
  metrics: Nullable<Metrics>;
  cost_mix: Nullable<CostMix>;
  caps: Nullable<Caps>;
  health: RoleHealth[];
  unattributed: JournalEvent[];
}

/* ----------------------------------------------------------------- issues */

export type StepState = "ok" | "failed" | "running" | (string & {});

export interface CardStep {
  kind: string;
  state: StepState;
  status: Nullable<string>;
  cost_usd: Nullable<number>;
}

export type IssueFlag =
  | "escalated"
  | "recurred"
  | "spend-blocked"
  | "held"
  | (string & {});

export interface IssueCard {
  issue: number;
  title: Nullable<string>;
  url: Nullable<string>;
  pr: Nullable<number>;
  pr_url: Nullable<string>;
  pr_state: Nullable<"open" | "merged" | "closed">;
  stage: Nullable<string>;
  done: boolean;
  steps: CardStep[];
  cost_usd: Nullable<number>;
  estimated: boolean;
  projected_cost_usd: Nullable<number>;
  started_ts: Nullable<string>;
  last_ts: Nullable<string>;
  elapsed_s: Nullable<number>;
  projected_eta_s: Nullable<number>;
  models: string[];
  rounds: Nullable<number>;
  flags: IssueFlag[];
  /** done cards only, when gh knows it */
  merged_ts?: Nullable<string>;
}

export type Priority = "P1" | "P2" | "P3" | null;

export interface PendingCard {
  issue: number;
  title: Nullable<string>;
  url: Nullable<string>;
  priority: Priority;
  labels: string[];
  held: boolean;
  hold_reason: Nullable<string>;
  /** reserved: steward overlap prediction not built yet — always null in v1 */
  overlap: Nullable<unknown>;
}

export interface BacklogCard {
  issue: number;
  title: Nullable<string>;
  url: Nullable<string>;
  labels: string[];
  recurrence: boolean;
  age_days: Nullable<number>;
  comments: Nullable<number>;
}

export interface IssuesPayload extends Warned {
  in_flight: IssueCard[];
  pending: PendingCard[];
  done: IssueCard[];
  backlog: BacklogCard[];
  gh_ok: boolean;
}

/* ----------------------------------------------------------- issue detail */

export interface TokenUsage {
  input_tokens: Nullable<number>;
  output_tokens: Nullable<number>;
  cache_read_input_tokens: Nullable<number>;
  cache_creation_input_tokens: Nullable<number>;
}

export interface IssueStep {
  idx: number;
  kind: string;
  role: Nullable<string>;
  ts: Nullable<string>;
  state: StepState;
  status: Nullable<string>;
  exit_reason: Nullable<string>;
  duration_s: Nullable<number>;
  cost_usd: Nullable<number>;
  estimated: boolean;
  model: Nullable<string>;
  session_id: Nullable<string>;
  turns: Nullable<number>;
  usage: Nullable<TokenUsage>;
  has_transcript: boolean;
}

export interface IssueDetailPayload extends Warned {
  issue: number;
  title: Nullable<string>;
  url: Nullable<string>;
  stage: Nullable<string>;
  done: boolean;
  prs: number[];
  cost_usd: Nullable<number>;
  elapsed_s: Nullable<number>;
  steps: IssueStep[];
  events: JournalEvent[];
}

export interface StepEventsPayload extends Warned {
  events: JournalEvent[];
}

/* ---------------------------------------------------------------- actions */

export interface ActionResult {
  ok: boolean;
  detail: string;
}
