import { describe, expect, it } from "vitest";
import { liveClocks, offsetSince, tickingClock } from "./clock";

describe("tickingClock", () => {
  it("gives the work clock to a running leg even when a human action is posted", () => {
    expect(tickingClock({ leg_running: true, human_action: "awaiting-reply" }))
      .toBe("work");
  });

  it("ticks idle when nothing is running and the row is accruing idleness", () => {
    expect(tickingClock({ leg_running: false, idle_active: true })).toBe("idle");
  });

  it("freezes a quiescent human-owned row", () => {
    expect(tickingClock({ human_action: "escalated" })).toBeNull();
  });

  it("freezes a finished row whatever else the payload claims", () => {
    expect(tickingClock({ done: true, leg_running: true, idle_active: true }))
      .toBeNull();
  });

  it("freezes a row that says nothing at all", () => {
    expect(tickingClock({})).toBeNull();
  });
});

describe("liveClocks", () => {
  const anchors = { elapsed_s: 600, working_s: 100, idle_s: 500 };

  it("advances only the clock that is ticking, and wall time with it", () => {
    expect(liveClocks({ ...anchors, leg_running: true }, 30))
      .toEqual({ elapsed: 630, working: 130, idle: 500 });
    expect(liveClocks({ ...anchors, idle_active: true }, 30))
      .toEqual({ elapsed: 630, working: 100, idle: 530 });
  });

  it("holds every clock still when neither side is moving", () => {
    expect(liveClocks({ ...anchors, human_action: "escalated" }, 30))
      .toEqual({ elapsed: 600, working: 100, idle: 500 });
  });

  it("keeps absent and unusable anchors absent rather than NaN", () => {
    expect(liveClocks({ leg_running: true }, 30))
      .toEqual({ elapsed: null, working: null, idle: null });
    expect(liveClocks(
      { elapsed_s: null, working_s: Number.NaN, idle_s: Number.POSITIVE_INFINITY,
        idle_active: true },
      30,
    )).toEqual({ elapsed: null, working: null, idle: null });
  });

  it("preserves fractional server values instead of rounding them itself", () => {
    expect(liveClocks({ working_s: 100.5, leg_running: true }, 0.25).working)
      .toBeCloseTo(100.75);
  });

  it("never runs backwards on a negative or unusable offset", () => {
    expect(liveClocks({ ...anchors, leg_running: true }, -30).working).toBe(100);
    expect(liveClocks({ ...anchors, leg_running: true }, Number.NaN).working)
      .toBe(100);
  });
});

describe("offsetSince", () => {
  it("measures seconds from the instant the snapshot was received", () => {
    expect(offsetSince(1_000_000, 1_030_000)).toBe(30);
  });

  it("is zero with no snapshot, and never negative on a clock that jumped", () => {
    expect(offsetSince(null, 1_030_000)).toBe(0);
    expect(offsetSince(1_030_000, 1_000_000)).toBe(0);
    expect(offsetSince(Number.NaN, 1_000_000)).toBe(0);
  });
});
