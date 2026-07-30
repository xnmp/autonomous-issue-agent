import { describe, expect, it } from "vitest";
import {
  combinedVerificationStatus,
  kindLabel,
  modelTone,
  roleHealth,
  stageTone,
} from "./semantics";

describe("dashboard semantics", () => {
  it.each([
    ["RESOLVE", "Resolve"],
    ["ROUTE", "Route"],
    ["ADDRESS_REVIEW", "Address review"],
    ["RE_REVIEW", "Re-review"],
  ])("renders %s without an operator-only abbreviation", (kind, label) => {
    expect(kindLabel(kind)).toBe(label);
  });

  it("treats done as terminal success", () => {
    expect(stageTone("done")).toBe("ok");
  });

  it.each([
    ["VERIFIED", "VERIFIED", "verified"],
    ["VERIFIED", "pending", "pending"],
    ["VERIFIED", null, "pending"],
    ["MISBEHAVED", "VERIFIED", "misbehaved"],
    ["RETIRED", "RETIRED", "retired"],
    ["VERIFIED", "RETIRED", "retired"],
    ["RETIRED", null, "retired"],
    ["RETIRED", "pending", "pending"],
    ["RETIRED", "MISBEHAVED", "misbehaved"],
    [null, null, null],
  ])(
    "combines issue %s and PR %s into one verification state",
    (issue, pr, expected) => {
      expect(combinedVerificationStatus(issue, pr)).toBe(expected);
    },
  );

  it.each([
    ["gpt-5.6-luna", null, "tier-luna"],
    ["gpt-5.6-terra", "medium", "tier-terra"],
    ["gpt-5.6-sol", "medium", "tier-sol"],
    ["gpt-5.6-sol", "high", "tier-high"],
    ["gpt-5.6-sol", "xhigh", "tier-high"],
    ["fable", null, "tier-sol"],
    ["claude-opus-5", null, "tier-sol"],
    ["claude-opus-5", "high", "tier-high"],
    ["claude-opus-5", "xhigh", "tier-high"],
  ])("colors %s at %s by capability tier", (model, effort, tone) => {
    expect(modelTone(model, effort)).toBe(tone);
  });

  it("keeps unavailable process liveness distinct from dead", () => {
    expect(
      roleHealth({
        role: "fix",
        alive: null,
        stale: false,
        last_event_ts: null,
        last_event: null,
        events: 1,
        log_path: null,
      }),
    ).toBe("unknown");
  });
});
