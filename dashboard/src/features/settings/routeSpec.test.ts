import { describe, expect, it } from "vitest";
import { formatRoute, parseRoute } from "./routeSpec";

const EFFORTS = { claude: ["low", "medium", "high", "max"], codex: ["low", "medium", "high", "xhigh"] };

describe("model route specs", () => {
  it.each([
    ["claude:sonnet:low", { runtime: "claude", model: "sonnet", effort: "low" }],
    ["codex:gpt-6-luna", { runtime: "codex", model: "gpt-6-luna", effort: "" }],
    ["claude::high", { runtime: "claude", model: "", effort: "high" }],
    // a final segment that is not this runtime's effort stays part of the model
    ["claude:vendor:model:xhigh", { runtime: "claude", model: "vendor:model:xhigh", effort: "" }],
    ["", { runtime: "", model: "", effort: "" }],
  ])("parses %j", (spec, parts) => {
    expect(parseRoute(spec, EFFORTS)).toEqual(parts);
  });

  it("round-trips every parsed route", () => {
    for (const spec of ["claude:sonnet:low", "codex:gpt-6-luna", "claude::high", "claude:"]) {
      expect(formatRoute(parseRoute(spec, EFFORTS))).toBe(spec);
    }
  });

  it("formats no runtime as unset and trims stray whitespace", () => {
    expect(formatRoute({ runtime: "", model: "opus", effort: "high" })).toBeNull();
    expect(formatRoute({ runtime: "codex", model: " gpt-6-sol ", effort: "" })).toBe("codex:gpt-6-sol");
  });

  it("keeps a malformed spec visible as a model rather than dropping it", () => {
    expect(parseRoute("gemini", EFFORTS)).toEqual({ runtime: "", model: "gemini", effort: "" });
  });
});
