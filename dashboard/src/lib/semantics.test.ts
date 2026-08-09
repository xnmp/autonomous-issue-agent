import { describe, expect, it } from "vitest";
import {
  combinedVerificationStatus,
  commitSubjectTone,
  difficultyTone,
  impactTone,
  issueTypeTone,
  kindLabel,
  labelTone,
  modelTone,
  prReviewMark,
  roleHealth,
  stageTone,
  stepKindTone,
  verificationSummary,
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
    ["red: reproduce the race", "fail"],
    ["green: serialize watcher writes", "ok"],
    ["docs: explain the invariant", "neutral"],
  ])("maps commit subject %s into the shared status vocabulary", (subject, tone) => {
    expect(commitSubjectTone(subject)).toBe(tone);
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

  it.each([
    ["gpt-5.6-luna", "high"],
    ["gpt-5.6-terra", "xhigh"],
    ["claude-haiku-5", "HIGH"],
    ["some-unknown-model", "max"],
  ])("alarms %s at %s effort whatever tier it runs on", (model, effort) => {
    expect(modelTone(model, effort)).toBe("tier-high");
  });

  it.each([
    ["gpt-5.6-terra", "  medium  ", "tier-terra"],
    ["gpt-5.6-luna", "hgih", "tier-luna"],
    ["gpt-5.6-luna", "", "tier-luna"],
    ["gpt-5.6-terra", undefined, "tier-terra"],
  ])(
    "keeps %s at %s off the alarm colour when the stance is not elevated",
    (model, effort, tone) => {
      expect(modelTone(model, effort)).toBe(tone);
    },
  );

  it.each([
    ["mergeable", "✓"],
    ["changes_requested", "±"],
  ])("marks a %s PR with %s", (state, glyph) => {
    expect(prReviewMark(state)?.glyph).toBe(glyph);
  });

  it.each([[null], [undefined], ["MERGEABLE"], ["unknown"]])(
    "shows no merge-readiness mark for %s, because absent data is not a verdict",
    (state) => {
      expect(prReviewMark(state)).toBeNull();
    },
  );

  it("puts both verdicts and their reasons in one hover text", () => {
    expect(
      verificationSummary({
        issueStatus: "VERIFIED",
        issueReason: "probe stayed green",
        prStatus: "MISBEHAVED",
        prReason: "reverted next day",
      }),
    ).toBe("Issue: VERIFIED — probe stayed green\nPR: MISBEHAVED — reverted next day");
  });

  it("names a verdict that has no recorded reason instead of reading as missing", () => {
    expect(verificationSummary({ issueStatus: "VERIFIED", issueReason: "  " })).toBe(
      "Issue: VERIFIED\nPR: unavailable",
    );
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

  it.each([
    ["unblock", "done", "meta"],
    ["relabel (operator)", "done", "meta"],
    ["RESOLVE", "ok", "ok"],
    ["FIX_CI", "failed", "fail"],
    ["REVIEW", "running", "running"],
  ])("tones %s (%s) by who ran it, then how it ended", (kind, state, tone) => {
    expect(stepKindTone(kind, state)).toBe(tone);
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
