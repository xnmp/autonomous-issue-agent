import { describe, expect, it } from "vitest";
import { kindLabel, stageTone } from "./semantics";

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
});
