import { afterEach, describe, expect, it } from "vitest";
import { retryKey } from "../resolve/retry-journal";
import { parseReviewCommand } from "./model";
import { acknowledgeReviewRequest, recoverReviewRequest, retainReviewRequest, reviewRetryKey } from "./retry-journal";
import { reviewBinding } from "./test-fixtures";

afterEach(() => sessionStorage.clear());

const command = parseReviewCommand({
  operation: "request_rearm", expected: reviewBinding, request_id: "request-1", gate_id: "ceiling-12", guidance: { kind: "keep" },
});

describe("review retry journal", () => {
  it("keeps a pull request's record apart from an issue with the same number", () => {
    expect(reviewRetryKey("demo", 12)).not.toBe(retryKey("demo", 12));
    retainReviewRequest(sessionStorage, "demo", command);
    expect(sessionStorage.getItem(retryKey("demo", 12))).toBeNull();
    expect(recoverReviewRequest(sessionStorage, "demo", 12)).toMatchObject({ kind: "uncertain", command });
    expect(recoverReviewRequest(sessionStorage, "other", 12)).toEqual({ kind: "idle" });
  });

  it("clears only the acknowledged command", () => {
    retainReviewRequest(sessionStorage, "demo", command);
    acknowledgeReviewRequest(sessionStorage, "demo", command);
    expect(recoverReviewRequest(sessionStorage, "demo", 12)).toEqual({ kind: "idle" });
  });

  it("refuses a second command while an unconfirmed one is saved", () => {
    retainReviewRequest(sessionStorage, "demo", command);
    const other = parseReviewCommand({ ...command, request_id: "request-2" });
    expect(() => retainReviewRequest(sessionStorage, "demo", other)).toThrow(/Another saved request/);
  });

  it.each([
    ["for another pull request", JSON.stringify({ version: 1, command: { ...command, expected: { ...reviewBinding, pull_request: 13 } } }), /another pull request/],
    ["that is not JSON", "{", /not valid JSON/],
    ["that is huge", "x".repeat(262_145), /too large/],
    ["that breaks the contract", JSON.stringify({ version: 1, command: { ...command, expected: { ...reviewBinding, family: "resolve" } } }), /command contract/],
  ])("blocks new commands on a saved record %s, never deleting it", (_label, raw, reason) => {
    sessionStorage.setItem(reviewRetryKey("demo", 12), raw);
    const recovered = recoverReviewRequest(sessionStorage, "demo", 12);
    expect(recovered.kind).toBe("blocked");
    expect(recovered.kind === "blocked" && recovered.reason).toMatch(reason);
    expect(sessionStorage.getItem(reviewRetryKey("demo", 12))).toBe(raw);
  });
});
