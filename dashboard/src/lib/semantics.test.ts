import { describe, expect, it } from "vitest";
import { kindLabel, modelTone, stageTone } from "./semantics";

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
    ["gpt-5.6-luna", null, "tier-luna"],
    ["gpt-5.6-terra", "medium", "tier-terra"],
    ["gpt-5.6-sol", "medium", "tier-sol"],
    ["gpt-5.6-sol", "high", "tier-high"],
    ["gpt-5.6-sol", "xhigh", "tier-high"],
    ["fable", null, "tier-high"],
    ["claude-opus-5", null, "tier-high"],
  ])("colors %s at %s by capability tier", (model, effort, tone) => {
    expect(modelTone(model, effort)).toBe(tone);
  });
});
