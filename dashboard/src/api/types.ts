/**
 * UI aliases over the generated OpenAPI contract.
 *
 * Run `npm run generate:api` after changing Python response models. No endpoint
 * payload is handwritten here; this file only names the generated schemas.
 */

import type { components } from "./generated";

type Schema = components["schemas"];

export type Nullable<T> = T | null;
export type RoleHealth = Schema["RoleHealth"];
export type Dispatch = Schema["Dispatch"];
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
export type BasisCost = Schema["BasisCost"];
export type Metrics = Schema["Metrics"];
export type Caps = Schema["Caps"];
export type RoleHeadroom = Schema["RoleHeadroom"];
export type LeaseRow = Schema["LeaseRow"];
export type HealthTrendDay = Schema["HealthTrendDay"];
export type SpendBucket = Schema["SpendBucket"];
export type AttentionItem = Schema["AttentionItem"];
export type OverviewPayload = Schema["OverviewPayload"];
export type BoardNeedsYou = Schema["BoardNeedsYou"];
export type BoardWorking = Schema["BoardWorking"];
export type BoardWaiting = Schema["BoardWaiting"];
export type BoardStatus = BoardNeedsYou | BoardWorking | BoardWaiting;
export type StopReason = BoardNeedsYou["reason"];
export type StageRail = Schema["StageRail"];
export type StageId = StageRail["current"];
export type TrackChip = Schema["TrackChip"];
export type StageVisit = Schema["StageVisit"];
export type IssueCard = Schema["IssueCard"];
export type SlotUsage = Schema["SlotUsage"];
export type DoneCard = Schema["DoneCard"];
export type BacklogCard = Schema["BacklogCard"];
export type ToastNotification = Schema["ToastNotification"];
export type IssuesPayload = Schema["IssuesPayload"];
export type TokenUsage = Schema["TokenUsage"];
export type IssueStep = Schema["IssueStep"];
export type PullRequestLink = Schema["PullRequestLink"];
export type TriageSummary = Schema["TriageSummary"];
export type CarriedLegs = Schema["CarriedLegs"];
export type IssueDetailPayload = Schema["IssueDetailPayload"];
export type ActionResult = Schema["ActionResult"];

export type RoleName = "fix" | "review";
export type VerificationStatus =
  | "VERIFIED"
  | "MISBEHAVED"
  | "pending"
  | (string & {});
