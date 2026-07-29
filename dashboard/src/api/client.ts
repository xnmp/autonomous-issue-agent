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
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Thrown-abort detection: callers treat this as "not an error, just stale". */
export function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

async function decodeError(res: Response): Promise<never> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail: unknown }).detail;
      if (typeof d === "string" && d) detail = d;
    }
  } catch {
    /* non-JSON body: keep the status line */
  }
  throw new ApiError(detail, res.status);
}

async function request<T>(
  path: string,
  init: RequestInit & { signal?: AbortSignal },
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers: { Accept: "application/json", ...init.headers } });
  } catch (e) {
    if (isAbort(e)) throw e;
    // The backend is loopback-only; the overwhelmingly likely cause is "not
    // running", so say that instead of the browser's opaque "Failed to fetch".
    throw new ApiError(
      "cannot reach the dashboard backend (is `issue-agent dashboard --serve` running?)",
      0,
    );
  }
  if (!res.ok) await decodeError(res);
  return (await res.json()) as T;
}

export function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: "GET", signal });
}

export function postJson<T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
}

const enc = encodeURIComponent;

/* -------------------------------------------------------------- endpoints */

export const api = {
  repos: (s?: AbortSignal) => getJson<ReposPayload>("/api/repos", s),

  usage: (s?: AbortSignal) => getJson<UsagePayload>("/api/usage", s),

  verification: (s?: AbortSignal) =>
    getJson<VerificationPayload>("/api/verification", s),

  overview: (name: string, s?: AbortSignal) =>
    getJson<OverviewPayload>(`/api/repos/${enc(name)}/overview`, s),

  issues: (name: string, s?: AbortSignal) =>
    getJson<IssuesPayload>(`/api/repos/${enc(name)}/issues`, s),

  issue: (name: string, n: number, s?: AbortSignal) =>
    getJson<IssueDetailPayload>(`/api/repos/${enc(name)}/issues/${n}`, s),

  stepEvents: (name: string, n: number, idx: number, s?: AbortSignal) =>
    getJson<StepEventsPayload>(
      `/api/repos/${enc(name)}/issues/${n}/steps/${idx}/events`,
      s,
    ),

  /** iframe src — the endpoint returns text/html, not JSON. */
  costChartUrl: (name: string, n: number, sessionId: string) =>
    `/api/repos/${enc(name)}/issues/${n}/cost-chart?session_id=${enc(sessionId)}`,

  startDaemons: (name: string, s?: AbortSignal) =>
    postJson<ActionResult>(`/api/repos/${enc(name)}/start`, {}, s),

  promote: (name: string, n: number, s?: AbortSignal) =>
    postJson<ActionResult>(`/api/repos/${enc(name)}/issues/${n}/promote`, {}, s),

  setPriority: (name: string, n: number, priority: Priority, s?: AbortSignal) =>
    postJson<ActionResult>(
      `/api/repos/${enc(name)}/issues/${n}/priority`,
      { priority },
      s,
    ),
};
