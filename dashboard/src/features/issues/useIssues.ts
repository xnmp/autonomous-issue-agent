import { api } from "../../api/client";
import { usePolled, type Polled } from "../../api/hooks";
import type { IssuesPayload } from "../../api/types";

const POLL_MS = 5_000;

/** One server-state owner shared by the issue view and open issue tabs. */
export function useIssues(repo: string | null): Polled<IssuesPayload> {
  return usePolled(
    repo === null ? null : `issues:${repo}`,
    (signal) => api.issues(repo as string, signal),
    POLL_MS,
  );
}
