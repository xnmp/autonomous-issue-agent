/**
 * Polling primitives.
 *
 * One poller per mounted view (contract: repos+usage 10 s, issues 5 s, issue
 * detail 5 s). Invariants this hook guarantees so no call site has to:
 *   - in-flight request is aborted on unmount and on key change;
 *   - polling pauses while `document.hidden` and fires immediately on return;
 *   - a failed refresh keeps the last good data (the panel shows a banner over
 *     stale numbers rather than blanking);
 *   - `key` — not the fetcher's identity — decides when to restart, so callers
 *     can pass inline lambdas.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { isAbort } from "./client";

interface CachedPoll {
  data: unknown;
  updatedAt: number;
}

interface PollState<T> {
  key: string | null;
  data: T | null;
  error: string | null;
  updatedAt: number | null;
}

const successfulPolls = new Map<string, CachedPoll>();
const MAX_CACHED_POLLS = 64;

function cachedPoll<T>(key: string | null): { data: T | null; updatedAt: number | null } {
  if (key === null) return { data: null, updatedAt: null };
  const cached = successfulPolls.get(key);
  return cached
    ? { data: cached.data as T, updatedAt: cached.updatedAt }
    : { data: null, updatedAt: null };
}

function rememberSuccessfulPoll(key: string, value: CachedPoll): void {
  successfulPolls.delete(key);
  successfulPolls.set(key, value);
  if (successfulPolls.size <= MAX_CACHED_POLLS) return;
  const oldest = successfulPolls.keys().next().value;
  if (oldest !== undefined) successfulPolls.delete(oldest);
}

/* --------------------------------------------------- document visibility */

function subscribeVisibility(cb: () => void): () => void {
  document.addEventListener("visibilitychange", cb);
  return () => document.removeEventListener("visibilitychange", cb);
}

export function useDocumentVisible(): boolean {
  return useSyncExternalStore(
    subscribeVisibility,
    () => !document.hidden,
    () => true,
  );
}

/* ------------------------------------------------------------- usePolled */

export interface Polled<T> {
  data: T | null;
  error: string | null;
  /** true only for the very first load (no data yet) */
  loading: boolean;
  /** ms since epoch of the last successful load */
  updatedAt: number | null;
  refresh: () => void;
}

export function usePolled<T>(
  key: string | null,
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
): Polled<T> {
  const initial = cachedPoll<T>(key);
  const [state, setState] = useState<PollState<T>>({
    key,
    data: initial.data,
    error: null,
    updatedAt: initial.updatedAt,
  });
  const [nonce, setNonce] = useState(0);
  const visible = useDocumentVisible();
  const current = state.key === key
    ? state
    : { key, ...cachedPoll<T>(key), error: null };

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // A key change may restore only that exact resource's last successful value.
  // This keeps tab navigation instant without ever showing one repo's data
  // under another repo's heading; the effect below still refreshes immediately.
  useEffect(() => {
    const cached = cachedPoll<T>(key);
    setState({ key, data: cached.data, error: null, updatedAt: cached.updatedAt });
  }, [key]);

  useEffect(() => {
    if (key === null) return;
    let cancelled = false;
    const ac = new AbortController();

    const run = async () => {
      try {
        const value = await fetcherRef.current(ac.signal);
        if (cancelled) return;
        const completedAt = Date.now();
        rememberSuccessfulPoll(key, { data: value, updatedAt: completedAt });
        setState({ key, data: value, error: null, updatedAt: completedAt });
      } catch (e) {
        if (cancelled || isAbort(e)) return;
        const cached = cachedPoll<T>(key);
        setState((previous) => ({
          key,
          data: previous.key === key ? previous.data : cached.data,
          error: e instanceof Error ? e.message : String(e),
          updatedAt: previous.key === key ? previous.updatedAt : cached.updatedAt,
        }));
      }
    };

    void run();
    if (!visible || intervalMs <= 0) {
      return () => {
        cancelled = true;
        ac.abort();
      };
    }
    const id = window.setInterval(run, intervalMs);
    return () => {
      cancelled = true;
      ac.abort();
      window.clearInterval(id);
    };
  }, [key, intervalMs, visible, nonce]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  return {
    data: current.data,
    error: current.error,
    loading: current.data === null && current.error === null,
    updatedAt: current.updatedAt,
    refresh,
  };
}

/* The 1 Hz ticker used to live here. It now lives in `lib/clock.ts` beside
   the pure tick rules it feeds, so the whole app has one clock and one place
   that decides which durations move. */
