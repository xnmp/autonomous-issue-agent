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
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);
  const visible = useDocumentVisible();

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // A key change means a different resource: drop the previous resource's data
  // so a stale repo's numbers never appear under a new repo's heading.
  useEffect(() => {
    setData(null);
    setError(null);
    setUpdatedAt(null);
  }, [key]);

  useEffect(() => {
    if (key === null) return;
    let cancelled = false;
    const ac = new AbortController();

    const run = async () => {
      try {
        const value = await fetcherRef.current(ac.signal);
        if (cancelled) return;
        setData(value);
        setError(null);
        setUpdatedAt(Date.now());
      } catch (e) {
        if (cancelled || isAbort(e)) return;
        setError(e instanceof Error ? e.message : String(e));
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
  return { data, error, loading: data === null && error === null, updatedAt, refresh };
}

/* ------------------------------------------------------- live 1 s ticker */

let tickListeners = new Set<() => void>();
let tickTimer: number | null = null;
let tickValue = Date.now();

function subscribeTick(cb: () => void): () => void {
  tickListeners.add(cb);
  if (tickTimer === null) {
    tickTimer = window.setInterval(() => {
      tickValue = Date.now();
      tickListeners.forEach((l) => l());
    }, 1000);
  }
  return () => {
    tickListeners.delete(cb);
    if (tickListeners.size === 0 && tickTimer !== null) {
      window.clearInterval(tickTimer);
      tickTimer = null;
    }
  };
}

/**
 * Shared 1 Hz clock — one interval for the whole app, however many elapsed
 * timers are on screen. Returns the current wall-clock ms.
 */
export function useClock(): number {
  return useSyncExternalStore(
    subscribeTick,
    () => tickValue,
    () => tickValue,
  );
}

/**
 * Server-computed `elapsed_s` ticked forward locally. `anchor` is the ms
 * timestamp at which the server value was received, so a slow poll never makes
 * the timer jump backwards by more than one refresh.
 */
export function useLiveElapsed(
  elapsedS: number | null | undefined,
  anchorMs: number | null,
): number | null {
  const now = useClock();
  if (elapsedS === null || elapsedS === undefined) return null;
  if (anchorMs === null) return elapsedS;
  return elapsedS + Math.max(0, (now - anchorMs) / 1000);
}
