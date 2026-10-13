export const RECENCY_WINDOW_MS = 24 * 60 * 60 * 1000;

/**
 * How recently something finished, as an intensity: 1 at (or after) the
 * moment it finished, fading linearly to 0 at 24 h. Anything unknown (missing
 * or unparseable time, non-finite clock) is 0 — an unknown instant must not
 * look recent. A future timestamp (clock skew) clamps to 1.
 */
export function recencyIntensity(doneAtMs: number | null, nowMs: number): number {
  if (doneAtMs === null || !Number.isFinite(doneAtMs) || !Number.isFinite(nowMs)) return 0;
  const age = nowMs - doneAtMs;
  if (age <= 0) return 1;
  if (age >= RECENCY_WINDOW_MS) return 0;
  return 1 - age / RECENCY_WINDOW_MS;
}
