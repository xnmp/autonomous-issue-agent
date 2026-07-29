/**
 * URL-hash routing (contract: `#/repo/<name>/issue/<n>` so reload restores the
 * open tab). No router dependency — the route space is five shapes.
 *
 * The hash is the single source of truth for "what is on screen"; the tab strip
 * renders from it. Which tabs are *open* is separate state (App), because a
 * closeable tab outlives navigating away from it.
 */

import { useCallback, useSyncExternalStore } from "react";

export type IssuesSub = "inflight" | "done" | "backlog";

export const ISSUES_SUBS: IssuesSub[] = ["inflight", "done", "backlog"];

export const ISSUES_SUB_LABELS: Record<IssuesSub, string> = {
  inflight: "In flight & pending",
  done: "Done",
  backlog: "Backlog",
};

export type Route =
  | { view: "home" }
  | { view: "verification" }
  | { view: "overview"; repo: string }
  | { view: "issues"; repo: string; sub: IssuesSub }
  | { view: "issue"; repo: string; issue: number };

function isSub(s: string | undefined): s is IssuesSub {
  return s === "inflight" || s === "done" || s === "backlog";
}

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts.length === 0) return { view: "home" };
  if (parts[0] === "verification") return { view: "verification" };
  if (parts[0] === "repo" && parts[1]) {
    const repo = parts[1];
    const section = parts[2];
    if (section === "issues") {
      return { view: "issues", repo, sub: isSub(parts[3]) ? parts[3] : "inflight" };
    }
    if (section === "issue" && parts[3]) {
      const n = Number.parseInt(parts[3], 10);
      if (Number.isFinite(n)) return { view: "issue", repo, issue: n };
    }
    return { view: "overview", repo };
  }
  return { view: "home" };
}

export function hashFor(route: Route): string {
  const e = encodeURIComponent;
  switch (route.view) {
    case "home":
      return "#/";
    case "verification":
      return "#/verification";
    case "overview":
      return `#/repo/${e(route.repo)}`;
    case "issues":
      return `#/repo/${e(route.repo)}/issues/${route.sub}`;
    case "issue":
      return `#/repo/${e(route.repo)}/issue/${route.issue}`;
  }
}

/** The repo a route belongs to, if any (drives sidebar selection). */
export function routeRepo(route: Route): string | null {
  return route.view === "overview" || route.view === "issues" || route.view === "issue"
    ? route.repo
    : null;
}

function subscribe(cb: () => void): () => void {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

export function useRoute(): { route: Route; navigate: (r: Route, replace?: boolean) => void } {
  const hash = useSyncExternalStore(
    subscribe,
    () => window.location.hash,
    () => "",
  );
  const navigate = useCallback((r: Route, replace = false) => {
    const next = hashFor(r);
    if (window.location.hash === next) return;
    if (replace) {
      window.history.replaceState(null, "", next);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    } else {
      window.location.hash = next;
    }
  }, []);
  return { route: parseHash(hash), navigate };
}
