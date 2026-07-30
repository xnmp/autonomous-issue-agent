import { describe, expect, it } from "vitest";
import { duration } from "./format";

describe("duration", () => {
  it.each([
    [4638, true, "1:17:18"],
    [252, true, "4:12"],
    [12, true, "0:12"],
    [4638, false, "1:17"],
    [252, false, "4m"],
    [12, false, "<1m"],
  ])("formats %s seconds with seconds=%s", (seconds, showSeconds, expected) => {
    expect(duration(seconds, showSeconds)).toBe(expected);
  });

  it("degrades invalid durations", () => {
    expect(duration(null)).toBe("—");
    expect(duration(-1)).toBe("—");
    expect(duration(Number.POSITIVE_INFINITY)).toBe("—");
  });
});
