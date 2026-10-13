import { describe, expect, it } from "vitest";
import {
  difficultyTone,
  impactTone,
  issueTypeTone,
  kindLabel,
  labelTone,
  legNote,
  actCosts,
  roleHealth,
} from "./semantics";

describe("dashboard semantics", () => {
  it.each([
    ["STEWARD", "Steward"],
    ["TRIAGE", "Triage"],
    ["RESOLVE", "Resolve"],
    ["ADDRESS_REVIEW", "Address review"],
    ["FIX_CI", "Fix CI"],
    ["SOMETHING_NEW", "something new"],
  ])("renders %s without an operator-only abbreviation", (kind, label) => {
    expect(kindLabel(kind)).toBe(label);
  });

  // Not the tables themselves — restating a lookup map asserts nothing. What is
  // worth pinning is the behaviour around them: an unrecognised or absent value
  // is never coloured as if it were understood, and the reader's casing and
  // whitespace do not decide a chip's colour.
  it.each([
    ["bug", "fail"],
    ["  BUG  ", "fail"],
    ["speculative", "neutral"],
    [null, "neutral"],
    [undefined, "neutral"],
    ["", "neutral"],
  ])("tones the %s type as %s", (value, tone) => {
    expect(issueTypeTone(value)).toBe(tone);
  });

  it("scales difficulty and impact in opposite directions", () => {
    // Difficulty is what a task costs us, so more is worse; impact is what it
    // is worth, so more is better. The two "high"s must not share a colour.
    expect(difficultyTone("high")).toBe("fail");
    expect(impactTone("high")).toBe("ok");
  });

  it.each([["banana"], [null], [undefined], [""]])(
    "leaves the unrecognised difficulty or impact %s uncoloured",
    (value) => {
      expect(difficultyTone(value)).toBe("neutral");
      expect(impactTone(value)).toBe("neutral");
    },
  );

  it.each([
    // A label encoding a triage axis takes that axis's colour, prefixed or bare.
    ["type:bug", "fail"],
    ["effort:trivial", "ok"],
    ["bug", "fail"],
    // A prefix with nothing after it names no value, and most labels are not
    // triage axes at all.
    ["type:", "neutral"],
    ["ai-ready", "neutral"],
    ["", "neutral"],
  ])("colours the %s label to match its column", (label, tone) => {
    expect(labelTone(label)).toBe(tone);
  });

  it("orders spend by the design's acts, the carried total last, dropping empty and non-finite rows", () => {
    const rows = actCosts([
      { kind: "carried", usd: 9, runs: 4 },
      { kind: "REVIEW", usd: 1.5, runs: 3 },
      { kind: "ADDRESS_REVIEW", usd: Number.NaN, runs: 1 },
      { kind: "REBASE", usd: 0, runs: 0 },
      { kind: "FIX_CI", usd: 1, runs: 1 },
      { kind: "RESOLVE", usd: 4, runs: 2 },
    ]);
    expect(rows.map((row) => [row.kind, row.usd])).toEqual([
      ["RESOLVE", 4], ["FIX_CI", 1], ["REVIEW", 1.5], ["carried", 9],
    ]);
    expect(actCosts(null)).toEqual([]);
  });

  it("takes a leg's note from its question first, then the review, then its summary", () => {
    expect(legNote({ question: "Which API?", verdict_excerpt: "fix it", summary: "did it" })).toBe("Which API?");
    expect(legNote({ question: null, verdict_excerpt: "fix it", summary: "did it" })).toBe("fix it");
    expect(legNote({ question: null, verdict_excerpt: null, summary: "  did it " })).toBe("did it");
    expect(legNote({ question: "   ", verdict_excerpt: null, summary: null })).toBeNull();
  });

  it("keeps unavailable process liveness distinct from dead", () => {
    expect(roleHealth({ role: "fix", alive: null, stale: false, heartbeat_ts: null, in_flight: [] })).toBe("unknown");
    expect(roleHealth({ role: "fix", alive: false, stale: true, heartbeat_ts: null, in_flight: [] })).toBe("dead");
    expect(roleHealth({ role: "fix", alive: true, stale: true, heartbeat_ts: null, in_flight: [] })).toBe("stale");
  });
});
