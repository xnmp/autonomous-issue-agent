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
  ReposPayload,
  UsagePayload,
  VerificationPayload,
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
    // A non-JSON 502/503/504 is a restarting or proxied-away backend.
    if ([502, 503, 504].includes(res.status)) {
      detail = `cannot reach the dashboard backend (it may be restarting): ${detail.trim()}`;
    }
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

async function request<T>(
  path: string,
  init: RequestInit & { signal?: AbortSignal },
  schema?: z.ZodTypeAny,
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
      "cannot reach the dashboard backend (is `python -m issue_agent.app.dashweb.main` running?)",
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
    // An empty or cut-off body is what a restarting backend sends.
    throw new ApiError(
      `cannot reach the dashboard backend (it may be restarting): empty or unreadable reply for ${path}`,
      res.status,
    );
  }
  if (!responseSchema) return body as T;
  const parsed = responseSchema.safeParse(body);
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
  outcomes?: ResponseOutcomes,
): Promise<T> {
  return request<T>(path, { method: "GET", signal }, schema, outcomes);
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
  }, schema, outcomes);
}

const enc = encodeURIComponent;
/* -------------------------------------------------------------- endpoints */

export const api = {
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
    ),

  issue: (name: string, n: number, s?: AbortSignal) =>
    getJson<IssueDetailPayload>(
      `/api/repos/${enc(name)}/issues/${n}`,
      schemas.IssueDetailPayload,
      s,
    ),

  promote: (name: string, n: number, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/promote`,
      {},
      schemas.ActionResult,
      s,
    ),

  /** Removes `needs-human` from the issue and its open harness PRs. */
  unblock: (name: string, n: number, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/unblock`,
      {},
      schemas.ActionResult,
      s,
    ),

  /** Adds `needs-human` to the issue and its open harness PRs. */
  park: (name: string, n: number, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/park`,
      {},
      schemas.ActionResult,
      s,
    ),
};
