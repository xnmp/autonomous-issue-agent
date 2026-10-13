import { describe, expect, it } from "vitest";
import { RECENCY_WINDOW_MS as DAY, recencyIntensity } from "./recency";

const NOW = Date.parse("2026-10-14T12:00:00Z");
const ago = (ms: number) => recencyIntensity(NOW - ms, NOW);

describe("recencyIntensity", () => {
  it("is 1 for something that just finished", () => expect(ago(0)).toBe(1));
  it("is half-strength at 12 h", () => expect(ago(DAY / 2)).toBeCloseTo(0.5, 10));
  it("fades monotonically", () => expect(ago(1_000_000)).toBeGreaterThan(ago(2_000_000)));
  it("is 0 at exactly 24 h", () => expect(ago(DAY)).toBe(0));
  it.each([DAY + 1, 7 * DAY, 1e15])("is 0 beyond 24 h (%d ms)", (ms) => expect(ago(ms)).toBe(0));
  it("is 0 for a missing time", () => expect(recencyIntensity(null, NOW)).toBe(0));
  it.each([NaN, Infinity, -Infinity])("is 0 for a non-finite time (%d)", (t) =>
    expect(recencyIntensity(t, NOW)).toBe(0));
  it("is 0 when the clock is not finite", () => expect(recencyIntensity(NOW, NaN)).toBe(0));
  it.each([1, 3_600_000, 1e15])("clamps a future time (+%d ms) to 1", (ms) =>
    expect(recencyIntensity(NOW + ms, NOW)).toBe(1));
});
