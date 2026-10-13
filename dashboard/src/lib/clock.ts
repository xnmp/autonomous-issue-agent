/**
 * The board's one clock: a single shared ticker per interval. Every live
 * duration on screen reads the same `now`, so a hundred timers cost one
 * interval and never disagree by a frame.
 */

import { useSyncExternalStore } from "react";

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
