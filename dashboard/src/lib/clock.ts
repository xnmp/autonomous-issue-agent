/**
 * The board's one clock, and the pure rules that decide what it moves.
 *
 * Two separate concerns live here, deliberately:
 *
 *   - `useNow` — a single shared ticker per interval. Every live duration on
 *     screen reads the same `now`, so a hundred timers cost one interval and
 *     never disagree by a frame.
 *   - `liveClocks` / `tickingClock` — pure functions of (server anchors,
 *     offset). Nothing here reads the wall clock, so the tick rules are
 *     testable without rendering anything.
 *
 * Skew: the offset is measured from the instant the CLIENT received the
 * snapshot (`Polled.updatedAt`), never from a server-authored timestamp, so a
 * client whose clock disagrees with the daemon's still ticks from zero. The
 * payload carries no `generated_at`; if one is ever added, the receive instant
 * remains the correct anchor and it can only serve as a staleness display.
 */

import { createContext, useContext, useSyncExternalStore } from "react";

/* ------------------------------------------------------------ the ticker */

interface Ticker {
  listeners: Set<() => void>;
  timer: number | null;
  value: number;
}

const tickers = new Map<number, Ticker>();

function tickerFor(intervalMs: number): Ticker {
  const existing = tickers.get(intervalMs);
  if (existing) return existing;
  const created: Ticker = { listeners: new Set(), timer: null, value: Date.now() };
  tickers.set(intervalMs, created);
  return created;
}

interface Binding {
  subscribe: (cb: () => void) => () => void;
  read: () => number;
}

// One stable pair of callbacks per interval. `useSyncExternalStore` treats a
// new `subscribe` identity as a new store and resubscribes: minting one per
// render tore the interval down and rebuilt it every commit, which re-read the
// clock, which re-rendered — an infinite loop, not a slow one.
const bindings = new Map<number, Binding>();

function bindingFor(intervalMs: number): Binding {
  const existing = bindings.get(intervalMs);
  if (existing) return existing;
  const created: Binding = {
    subscribe: subscriber(intervalMs),
    read: () => tickerFor(intervalMs).value,
  };
  bindings.set(intervalMs, created);
  return created;
}

function subscriber(intervalMs: number) {
  return (cb: () => void): (() => void) => {
    const ticker = tickerFor(intervalMs);
    // The stored value goes stale whenever no interval is running: the first
    // subscriber after an idle stretch (or after fake timers replace the clock
    // in tests) would otherwise render an age computed from import time.
    if (ticker.listeners.size === 0) ticker.value = Date.now();
    ticker.listeners.add(cb);
    if (ticker.timer === null) {
      ticker.timer = window.setInterval(() => {
        ticker.value = Date.now();
        ticker.listeners.forEach((listener) => listener());
      }, intervalMs);
    }
    return () => {
      ticker.listeners.delete(cb);
      if (ticker.listeners.size === 0 && ticker.timer !== null) {
        window.clearInterval(ticker.timer);
        ticker.timer = null;
      }
    };
  };
}

/**
 * Current wall-clock ms, re-rendering the caller every `intervalMs`.
 * One interval is shared by every component asking for the same cadence.
 */
export function useNow(intervalMs = 1000): number {
  const { subscribe, read } = bindingFor(intervalMs);
  return useSyncExternalStore(subscribe, read, read);
}

/* ------------------------------------------------- the snapshot's anchor */

/**
 * Ms since epoch at which the currently rendered snapshot was RECEIVED.
 * `null` means "no snapshot yet" — every derived clock then stands still.
 */
export const SnapshotAnchor = createContext<number | null>(null);

/** Seconds elapsed since the rendered snapshot arrived. Never negative. */
export function offsetSince(anchorMs: number | null, nowMs: number): number {
  if (anchorMs === null || !Number.isFinite(anchorMs) || !Number.isFinite(nowMs)) {
    return 0;
  }
  return Math.max(0, (nowMs - anchorMs) / 1000);
}

/** `offsetSince` bound to the surrounding snapshot and the shared ticker. */
export function useSnapshotOffset(intervalMs = 1000): number {
  return offsetSince(useContext(SnapshotAnchor), useNow(intervalMs));
}

/* ------------------------------------------------------ the tick predicate */

/** The server-authored fields the tick rules are a function of. */
export interface ClockAnchors {
  elapsed_s?: number | null;
  working_s?: number | null;
  idle_s?: number | null;
  idle_active?: boolean;
  /** A paid leg is executing right now. */
  leg_running?: boolean;
  /** Non-null when the next move belongs to a person. */
  human_action?: string | null;
  done?: boolean;
}

export interface LiveClocks {
  elapsed: number | null;
  working: number | null;
  idle: number | null;
}

/**
 * Which of the two clocks is still moving.
 *
 * A running leg owns the work clock EVEN WHEN a human action is posted: the
 * fold can believe a row is human-owned (a journalled hold nobody journalled
 * clearing) while the harness is demonstrably still spending on it, and
 * freezing both clocks there hid live work behind a dead row (keifu
 * #142/#181, 2026-08-03). `human_action` alone — with no leg running and no
 * idle accrual — is genuine quiescence, and freezing is then correct.
 */
export function tickingClock(anchors: ClockAnchors): "work" | "idle" | null {
  if (anchors.done) return null;
  if (anchors.leg_running) return "work";
  if (anchors.idle_active) return "idle";
  return null;
}

function finite(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function advance(base: number | null | undefined, by: number): number | null {
  const value = finite(base);
  return value === null ? null : value + by;
}

/**
 * The three durations a row displays, ticked forward from the server's values.
 *
 * Absent, null, or non-finite anchors stay absent — a missing work clock reads
 * as "—", never as NaN. Fractional server values are preserved; the formatter
 * owns rounding.
 */
export function liveClocks(anchors: ClockAnchors, offsetS: number): LiveClocks {
  const ticking = tickingClock(anchors);
  const by = Number.isFinite(offsetS) ? Math.max(0, offsetS) : 0;
  return {
    // Wall time only advances while one of its two components does.
    elapsed: advance(anchors.elapsed_s, ticking === null ? 0 : by),
    working: advance(anchors.working_s, ticking === "work" ? by : 0),
    idle: advance(anchors.idle_s, ticking === "idle" ? by : 0),
  };
}
