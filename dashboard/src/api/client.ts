/**
 * The single typed HTTP client. Every network call in the app goes through
 * `getJson` / `postJson` so that error shaping, aborting and JSON decoding have
 * exactly one implementation.
 *
 * Errors are normalised to `ApiError` with a human-readable message: panels
 * render it in a banner rather than throwing to an error boundary (a dashboard
 * that goes blank when one collector fails is worse than one that says so).
 */

import type {
  ActionResult,
  IssueDetailPayload,
  IssuesPayload,
  OverviewPayload,
  Priority,
  ReposPayload,
  StepEventsPayload,
  UsagePayload,
  VerificationPayload,
  ResolveControl,
  ResolveCommand,
  ResolveCommandResult,
  ReviewControl,
  ReviewCommand,
  ReviewCommandResult,
  WorkflowFamilies,
} from "./types";
import { z } from "zod";
import { schemas } from "./generated-zod";

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** A validated pre-command rejection, not an ambiguous malformed reply. */
export class RejectedRequest extends ApiError {}

/** Thrown-abort detection: callers treat this as "not an error, just stale". */
export function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

async function decodeError(res: Response): Promise<never> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body: unknown = await res.json();
    detail = errorDetail(body) ?? detail;
  } catch {
    /* non-JSON body: keep the status line */
  }
  throw new ApiError(detail, res.status);
}

function errorDetail(body: unknown): string | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = body.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item: unknown) => item && typeof item === "object" && "msg" in item && typeof item.msg === "string" ? [item.msg] : []);
    if (messages.length) return messages.join("; ");
  }
  return null;
}

type ResponseOutcomes = Readonly<Record<number, z.ZodTypeAny>>;

/**
 * A closed enum in the contract must not be able to blank a whole tab.
 *
 * `pr_review_state` is a narrow `Literal` on the backend, so the generated
 * schema rejects any third value — and one unknown string in one card would
 * fail the whole payload's `safeParse` and leave the Issues tab showing an
 * error instead of fifty perfectly readable rows. A value this client does not
 * understand means exactly what a missing one means: no merge-readiness mark.
 * Widening happens here rather than in the generated file, which is only ever
 * written by `npm run generate:api`.
 */
const KNOWN_PR_REVIEW_STATES = new Set(["mergeable", "changes_requested"]);

function normaliseIssueCard(card: unknown): unknown {
  if (!card || typeof card !== "object") return card;
  const state = (card as { pr_review_state?: unknown }).pr_review_state;
  if (state === null || state === undefined || KNOWN_PR_REVIEW_STATES.has(state as string)) {
    return card;
  }
  return { ...card, pr_review_state: null };
}

export function normaliseIssuesPayload(payload: unknown): unknown {
  if (!payload || typeof payload !== "object") return payload;
  const body = payload as Record<string, unknown>;
  const lanes = ["in_flight", "done"] as const;
  const patched = Object.fromEntries(
    lanes
      .filter((lane) => Array.isArray(body[lane]))
      .map((lane) => [lane, (body[lane] as unknown[]).map(normaliseIssueCard)]),
  );
  return { ...body, ...patched };
}

async function request<T>(
  path: string,
  init: RequestInit & { signal?: AbortSignal },
  schema?: z.ZodTypeAny,
  normalise?: (payload: unknown) => unknown,
  outcomes?: ResponseOutcomes,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers: { Accept: "application/json", ...init.headers } });
  } catch (e) {
    if (isAbort(e)) throw e;
    if (e instanceof DOMException && e.name === "TimeoutError") {
      throw new ApiError("the dashboard backend did not respond in time", 0);
    }
    // The backend is loopback-only; the overwhelmingly likely cause is "not
    // running", so say that instead of the browser's opaque "Failed to fetch".
    throw new ApiError(
      "cannot reach the dashboard backend (is `issue-agent dashboard --serve` running?)",
      0,
    );
  }
  const responseSchema = outcomes ? outcomes[res.status] : schema;
  if (outcomes ? !responseSchema : !res.ok) await decodeError(res);
  let body: unknown;
  try {
    body = await res.json();
  } catch (error) {
    if (isAbort(error)) throw error;
    if (error instanceof DOMException && error.name === "TimeoutError") {
      throw new ApiError("the dashboard backend did not respond in time", res.status);
    }
    throw new ApiError(`dashboard backend returned an invalid response for ${path}`, res.status);
  }
  const payload = normalise ? normalise(body) : body;
  if (!responseSchema) return payload as T;
  const parsed = responseSchema.safeParse(payload);
  if (!parsed.success) {
    // A success body's `detail` is not an error message; only a failure
    // status may lend its detail to the error.
    const detail = res.ok ? null : errorDetail(body);
    if (res.status === 422 && detail && body && typeof body === "object" && !("outcome" in body)) {
      throw new RejectedRequest(detail, res.status);
    }
    throw new ApiError(
      detail ?? `dashboard backend returned an invalid response for ${path}`,
      res.status,
    );
  }
  return parsed.data as T;
}

export function getJson<T>(
  path: string,
  schema: z.ZodTypeAny,
  signal?: AbortSignal,
  normalise?: (payload: unknown) => unknown,
  outcomes?: ResponseOutcomes,
): Promise<T> {
  return request<T>(path, { method: "GET", signal }, schema, normalise, outcomes);
}

export function postJson<T>(
  path: string,
  body: unknown,
  schema: z.ZodTypeAny,
  signal?: AbortSignal,
  outcomes?: ResponseOutcomes,
): Promise<T> {
  return request<T>(path, {
    method: "POST",
    // The server rejects writes without this header: a cross-site form cannot
    // set one, and a cross-site fetch that does gets preflighted and refused.
    headers: { "Content-Type": "application/json", "X-Issue-Agent-Dashboard": "1" },
    body: JSON.stringify(body ?? {}),
    signal,
  }, schema, undefined, outcomes);
}

const enc = encodeURIComponent;
const resolveReads = {
  200: z.discriminatedUnion("status", [schemas.AvailableControl, schemas.InactiveControl]),
  404: schemas.AbsentControl,
  503: schemas.UnavailableControl,
};
const resolveWrites = Object.fromEntries(
  [[202, "accepted"], [200, "replayed"], [409, "conflict"], [422, "rejected"], [503, "unavailable"]]
    .map(([status, outcome]) => [status, schemas.CommandResult.refine((value) => value.outcome === outcome)]),
);

const reviewReads = {
  200: z.discriminatedUnion("status", [
    schemas.ReviewAvailableControl, schemas.ReviewInactiveControl, schemas.ReviewMigrationRequiredControl,
  ]),
  404: schemas.ReviewAbsentControl,
  503: schemas.ReviewUnavailableControl,
};
const familyReads = {
  200: schemas.WorkflowFamiliesAvailable,
  503: schemas.WorkflowFamiliesUnavailable,
};
const reviewWrites = Object.fromEntries(
  [[202, "accepted"], [200, "replayed"], [409, "conflict"], [422, "rejected"], [503, "unavailable"]]
    .map(([status, outcome]) => [status, schemas.ReviewCommandResult.refine((value) => value.outcome === outcome)]),
);

/* -------------------------------------------------------------- endpoints */

export const api = {
  resolveControl: (name: string, n: number, requestId?: string, s?: AbortSignal) =>
    getJson<ResolveControl>(
      `/api/repos/${enc(name)}/issues/${n}/resolve-control${requestId ? `?request_id=${enc(requestId)}` : ""}`,
      schemas.CommandResult.shape.control, s, undefined, resolveReads,
    ),

  resolveCommand: (name: string, n: number, command: ResolveCommand, s?: AbortSignal) =>
    postJson<ResolveCommandResult>(
      `/api/repos/${enc(name)}/issues/${n}/resolve-control/commands`, command,
      schemas.CommandResult, s, resolveWrites,
    ),

  reviewControl: (name: string, pr: number, requestId?: string, s?: AbortSignal) =>
    getJson<ReviewControl>(
      `/api/repos/${enc(name)}/pulls/${pr}/review-control${requestId ? `?request_id=${enc(requestId)}` : ""}`,
      schemas.ReviewCommandResult.shape.control, s, undefined, reviewReads,
    ),

  /** The repository's activated workflow families: the authority the legacy unblock refusal reads. */
  workflowFamilies: (name: string, s?: AbortSignal) =>
    getJson<WorkflowFamilies>(`/api/repos/${enc(name)}/workflow-families`, schemas.WorkflowFamiliesAvailable, s, undefined, familyReads),

  reviewCommand: (name: string, pr: number, command: ReviewCommand, s?: AbortSignal) =>
    postJson<ReviewCommandResult>(
      `/api/repos/${enc(name)}/pulls/${pr}/review-control/commands`, command,
      schemas.ReviewCommandResult, s, reviewWrites,
    ),

  repos: (s?: AbortSignal) =>
    getJson<ReposPayload>("/api/repos", schemas.ReposPayload, s),

  usage: (s?: AbortSignal) =>
    getJson<UsagePayload>("/api/usage", schemas.UsagePayload, s),

  verification: (s?: AbortSignal) =>
    getJson<VerificationPayload>("/api/verification", schemas.VerificationPayload, s),

  overview: (name: string, s?: AbortSignal) =>
    getJson<OverviewPayload>(
      `/api/repos/${enc(name)}/overview?timezone=${enc(
        Intl.DateTimeFormat().resolvedOptions().timeZone,
      )}`,
      schemas.OverviewPayload,
      s,
    ),

  issues: (name: string, s?: AbortSignal) =>
    getJson<IssuesPayload>(
      `/api/repos/${enc(name)}/issues`,
      schemas.IssuesPayload,
      s,
      normaliseIssuesPayload,
    ),

  issue: (name: string, n: number, s?: AbortSignal) =>
    getJson<IssueDetailPayload>(
      `/api/repos/${enc(name)}/issues/${n}`,
      schemas.IssueDetailPayload,
      s,
    ),

  stepEvents: (name: string, n: number, idx: number, s?: AbortSignal) =>
    getJson<StepEventsPayload>(
      `/api/repos/${enc(name)}/issues/${n}/steps/${idx}/events`,
      schemas.StepEventsPayload,
      s,
    ),

  /** iframe src — the endpoint returns text/html, not JSON. */
  costChartUrl: (name: string, n: number, sessionId: string) =>
    `/api/repos/${enc(name)}/issues/${n}/cost-chart?session_id=${enc(sessionId)}`,

  startDaemons: (name: string, s?: AbortSignal) =>
    postJson<ActionResult>(`/api/repos/${enc(name)}/start`, {}, schemas.ActionResult, s),

  promote: (name: string, n: number, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/promote`,
      {},
      schemas.ActionResult,
      s,
    ),

  /** Drops the labels that hold an issue for a human, so the daemons resume. */
  unblock: (name: string, n: number, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/unblock`,
      {},
      schemas.ActionResult,
      s,
    ),

  /** Hands an issue's PR(s) to a human and frees the overlap queue. */
  park: (name: string, n: number, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/park`,
      {},
      schemas.ActionResult,
      s,
    ),

  unpark: (name: string, n: number, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/unpark`,
      {},
      schemas.ActionResult,
      s,
    ),

  setPriority: (name: string, n: number, priority: Priority, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/priority`,
      { priority },
      schemas.ActionResult,
      s,
    ),
};
