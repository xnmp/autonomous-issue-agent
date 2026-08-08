/**
 * UI aliases over the generated OpenAPI contract.
 *
 * Run `npm run generate:api` after changing Python response models. No endpoint
 * payload is handwritten here; this file only adds UI-specific semantic unions
 * for open-shaped strings and journal events.
 */

import type { components } from "./generated";

type Schema = components["schemas"];

export type Nullable<T> = T | null;
export type Priority = "P1" | "P2" | "P3" | null;
export type RoleHealth = Schema["RoleHealth"];
export type RepoSummary = Schema["RepoSummary"];
export type ReposPayload = Schema["ReposPayload"];
export type ClaudeUsage = Schema["ClaudeUsage"];
export type CodexUsage = Schema["CodexUsage"];
export type HarnessRepoSpend = Schema["HarnessRepoSpend"];
export type HarnessUsage = Schema["HarnessUsage"];
export type UsagePayload = Schema["UsagePayload"];
export type VerificationRow = Schema["VerificationRow"];
export type VerificationPayload = Schema["VerificationPayload"];
export type KindCost = Schema["KindCost"];
export type Metrics = Schema["Metrics"];
export type CostMix = Schema["CostMix"];
export type Caps = Schema["Caps"];
export type HealthTrendDay = Schema["HealthTrendDay"];
export type InterventionEvent = Schema["InterventionEvent"];
export type OverviewPayload = Schema["OverviewPayload"];
export type CardStep = Schema["CardStep"];
export type IssueCard = Schema["IssueCard"];
export type PendingCard = Schema["PendingCard"];
export type BacklogCard = Schema["BacklogCard"];
export type ToastNotification = Schema["ToastNotification"];
export type IssuesPayload = Schema["IssuesPayload"];
export type TokenUsage = Schema["TokenUsage"];
export type IssueStep = Schema["IssueStep"];
export type IssueDetailPayload = Schema["IssueDetailPayload"];
export type StepEventsPayload = Schema["StepEventsPayload"];
export type ActionResult = Schema["ActionResult"];

export type RoleName = "fix" | "review" | "steward";
export type VerificationStatus =
  | "VERIFIED"
  | "MISBEHAVED"
  | "pending"
  | (string & {});
export type StepState = "ok" | "failed" | "running" | "advisory" | (string & {});
export type IssueFlag =
  | "escalated"
  | "recurred"
  | "spend-blocked"
  | "held"
  | (string & {});

/** Journal events deliberately remain open-shaped. */
export interface JournalEvent {
  ts?: string;
  kind?: string;
  event?: string;
  role?: string;
  issue?: number;
  number?: number;
  [key: string]: unknown;
}
