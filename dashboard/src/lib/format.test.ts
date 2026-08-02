import { afterEach, describe, expect, it } from "vitest";
import { duration, localHour } from "./format";

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

describe("localHour", () => {
  const original = process.env.TZ;
  afterEach(() => {
    process.env.TZ = original;
  });

  it("labels a UTC hour with the reader's real local minutes", () => {
    // Half of India's population reads every UTC hour boundary at :30. A label
    // that hardcoded ":00" put the bucket in the wrong half-hour for them.
    process.env.TZ = "Asia/Kolkata";

    const label = localHour("2026-08-02T14:00:00+00:00");

    expect(label).toContain("19:30");
    expect(label).not.toContain("19:00");
  });

  it("labels a whole-offset hour on the hour", () => {
    process.env.TZ = "UTC";

    expect(localHour("2026-08-02T14:00:00+00:00")).toContain("14:00");
  });

  it.each([[null], [undefined], [""], ["not a timestamp"]])(
    "renders %s as an absence rather than an invalid date",
    (value) => {
      expect(localHour(value)).toBe("—");
    },
  );
});
