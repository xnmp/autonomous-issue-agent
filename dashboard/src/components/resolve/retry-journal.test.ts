import { afterEach, describe, expect, it } from "vitest";

import type { ResolveCommand } from "../../api/types";
import { parseResolveCommand } from "./model";
import { acknowledgeRequest, recoverRequest, retainRequest, retryKey } from "./retry-journal";
import { binding } from "./test-fixtures";

afterEach(() => {
  sessionStorage.clear();
});

// Built through parseResolveCommand, like every command the controller ever
// hands to retainRequest/acknowledgeRequest, so key order matches what
// decode() (which also parses) produces — retryRequest's callers never hand
// it a hand-rolled object.
const command = parseResolveCommand({
  operation: "request_rearm",
  expected: { ...binding },
  request_id: "request-1",
  gate_id: "gate-77",
  guidance: { kind: "keep" },
}) as Extract<ResolveCommand, { operation: "request_rearm" }>;

const authorization: ResolveCommand = parseResolveCommand({
  operation: "authorize_retry",
  expected: { ...binding },
  authorization_id: "grant-1",
  gate_id: "uncertain-77",
  command_id: "command-77",
  evidence_id: "absence-77",
});

const otherIssueCommand: ResolveCommand = parseResolveCommand({
  ...command,
  expected: { ...binding, issue: 999 },
});

describe("retryKey", () => {
  it("scopes the storage key by deployment and issue, not by request identity", () => {
    expect(retryKey("demo", 77)).toBe('issue-agent.resolve-request.v1:["demo",77]');
    expect(retryKey("demo", 77)).toBe(retryKey("demo", 77));
    expect(retryKey("demo", 78)).not.toBe(retryKey("demo", 77));
    expect(retryKey("other", 77)).not.toBe(retryKey("demo", 77));
  });
});

describe("recoverRequest", () => {
  it("is idle when nothing is saved for this deployment and issue", () => {
    expect(recoverRequest(sessionStorage, "demo", 77)).toEqual({ kind: "idle" });
  });

  it("restores an uncertain operation carrying the exact saved command, without minting a new ID", () => {
    retainRequest(sessionStorage, "demo", command);
    const restored = recoverRequest(sessionStorage, "demo", 77);
    expect(restored.kind).toBe("uncertain");
    if (restored.kind === "uncertain") {
      expect(restored.command).toEqual(command);
      expect(restored.reason).toMatch(/no confirmed response/i);
    }
  });

  it("is blocked, not idle or deleted, when the saved record is malformed JSON", () => {
    sessionStorage.setItem(retryKey("demo", 77), "{not json");
    const restored = recoverRequest(sessionStorage, "demo", 77);
    expect(restored).toEqual({ kind: "blocked", reason: expect.stringContaining("not valid JSON") });
    // Nothing was silently deleted.
    expect(sessionStorage.getItem(retryKey("demo", 77))).toBe("{not json");
  });

  it.each([[""], ["  \n"]])("is idle, not blocked, when the saved record is blank (%j)", (blank) => {
    sessionStorage.setItem(retryKey("demo", 77), blank);
    expect(recoverRequest(sessionStorage, "demo", 77)).toEqual({ kind: "idle" });
  });

  it("is blocked when the saved envelope is missing its version marker", () => {
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({ command }));
    expect(recoverRequest(sessionStorage, "demo", 77)).toEqual({ kind: "blocked", reason: expect.stringContaining("format is not recognized") });
  });

  it("is blocked when the saved envelope carries a future/unsupported version", () => {
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({ version: 2, command }));
    expect(recoverRequest(sessionStorage, "demo", 77)).toEqual({ kind: "blocked", reason: expect.stringContaining("format is not recognized") });
  });

  it("is blocked when the saved command fails schema validation", () => {
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({ version: 1, command: { operation: "request_rearm" } }));
    expect(recoverRequest(sessionStorage, "demo", 77)).toEqual({ kind: "blocked", reason: expect.stringContaining("does not match the command contract") });
  });

  it("is blocked when the saved command's issue does not match the key it was read under (tampered envelope)", () => {
    const key = retryKey("demo", 77);
    sessionStorage.setItem(key, JSON.stringify({ version: 1, command: otherIssueCommand }));
    expect(recoverRequest(sessionStorage, "demo", 77)).toEqual({ kind: "blocked", reason: expect.stringContaining("belongs to another issue") });
  });

  it("is blocked when the saved record exceeds the size cap", () => {
    const huge = { expected: { ...binding }, operation: "cancel_rearm", cancellation_id: "cancel-1", request_id: "r", reason: "x".repeat(300_000) };
    // Bypass retainRequest's own guard so we can exercise recoverRequest's cap directly.
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({ version: 1, command: huge }));
    expect(recoverRequest(sessionStorage, "demo", 77)).toEqual({
      kind: "blocked",
      reason: expect.stringContaining("Saved request is too large."),
    });
  });

  it("is blocked, without throwing, when storage.getItem itself throws", () => {
    const throwing: Storage = {
      length: 0,
      clear: () => {},
      key: () => null,
      getItem: () => { throw new Error("storage disabled"); },
      setItem: () => {},
      removeItem: () => {},
    };
    expect(recoverRequest(throwing, "demo", 77)).toEqual({
      kind: "blocked",
      reason: expect.stringContaining("could not read the saved request"),
    });
  });
});

describe("retainRequest", () => {
  it("persists a version-1 envelope wrapping the exact command, keyed by deployment and the command's own issue", () => {
    retainRequest(sessionStorage, "demo", command);
    const raw = sessionStorage.getItem(retryKey("demo", 77));
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!)).toEqual({ version: 1, command });
  });

  it("allows retaining the exact same command again (idempotent retry save)", () => {
    retainRequest(sessionStorage, "demo", command);
    expect(() => retainRequest(sessionStorage, "demo", command)).not.toThrow();
  });

  it("saves over a blank record instead of failing on it", () => {
    sessionStorage.setItem(retryKey("demo", 77), "");
    retainRequest(sessionStorage, "demo", command);
    expect(recoverRequest(sessionStorage, "demo", 77)).toMatchObject({ kind: "uncertain", command });
  });

  it("explains a truncated saved record without exposing the JSON engine's message", () => {
    sessionStorage.setItem(retryKey("demo", 77), '{"version":1,"comm');
    expect(() => retainRequest(sessionStorage, "demo", command)).toThrow("Saved request is not valid JSON.");
    expect(() => acknowledgeRequest(sessionStorage, "demo", command)).toThrow("Saved request is not valid JSON.");
  });

  it("refuses to replace a different saved command awaiting a response", () => {
    retainRequest(sessionStorage, "demo", command);
    const different: ResolveCommand = { ...command, request_id: "request-2" };
    expect(() => retainRequest(sessionStorage, "demo", different)).toThrow(
      "Another saved request needs a response. Reload to inspect it before sending a new command.",
    );
    // The original record must survive the refused write.
    expect(JSON.parse(sessionStorage.getItem(retryKey("demo", 77))!)).toEqual({ version: 1, command });
  });

  it("does not mint or accept a new ID to escape an existing uncertain record", () => {
    retainRequest(sessionStorage, "demo", command);
    const sameIdentityDifferentShape: ResolveCommand = { ...(command as Extract<ResolveCommand, { operation: "request_rearm" }>), gate_id: "different-gate" };
    expect(() => retainRequest(sessionStorage, "demo", sameIdentityDifferentShape)).toThrow();
  });

  it("throws, without silently proceeding, when the write cannot be verified", () => {
    const storeValues = new Map<string, string>();
    const lying: Storage = {
      length: 0,
      clear: () => {},
      key: () => null,
      getItem: (key) => storeValues.get(key) ?? null,
      setItem: () => { /* quota: silently drop the write */ },
      removeItem: (key) => { storeValues.delete(key); },
    };
    expect(() => retainRequest(lying, "demo", command)).toThrow(
      "The browser could not preserve this request for safe retry.",
    );
  });

  it("throws and sends nothing when setItem itself throws (quota exceeded)", () => {
    const quotaExceeded: Storage = {
      length: 0,
      clear: () => {},
      key: () => null,
      getItem: () => null,
      setItem: () => { throw new Error("QuotaExceededError"); },
      removeItem: () => {},
    };
    expect(() => retainRequest(quotaExceeded, "demo", command)).toThrow("QuotaExceededError");
  });
});

describe("acknowledgeRequest", () => {
  it("clears the saved record once the matching command has a definite outcome", () => {
    retainRequest(sessionStorage, "demo", command);
    acknowledgeRequest(sessionStorage, "demo", command);
    expect(sessionStorage.getItem(retryKey("demo", 77))).toBeNull();
  });

  it("is a no-op when the saved record is blank", () => {
    sessionStorage.setItem(retryKey("demo", 77), "");
    expect(() => acknowledgeRequest(sessionStorage, "demo", command)).not.toThrow();
  });

  it("is a no-op when nothing is saved", () => {
    expect(() => acknowledgeRequest(sessionStorage, "demo", command)).not.toThrow();
  });

  it("refuses to acknowledge a different command than the one saved", () => {
    retainRequest(sessionStorage, "demo", command);
    const different: ResolveCommand = { ...command, request_id: "request-2" };
    expect(() => acknowledgeRequest(sessionStorage, "demo", different)).toThrow(
      "Another saved request replaced this one. Reload to inspect it.",
    );
    // Acknowledging the wrong command must not delete the real saved one.
    expect(sessionStorage.getItem(retryKey("demo", 77))).not.toBeNull();
  });

  it("throws when the removal cannot be verified", () => {
    const storeValues = new Map<string, string>([[retryKey("demo", 77), JSON.stringify({ version: 1, command })]]);
    const stubborn: Storage = {
      length: 0,
      clear: () => {},
      key: () => null,
      getItem: (key) => storeValues.get(key) ?? null,
      setItem: (key, value) => { storeValues.set(key, value); },
      removeItem: () => { /* refuses to actually remove */ },
    };
    expect(() => acknowledgeRequest(stubborn, "demo", command)).toThrow(
      "The browser could not clear the acknowledged request.",
    );
  });
});

describe("authorize_retry records", () => {
  it("round-trips the exact grant so a lost reply can only retry the same authorization ID", () => {
    retainRequest(sessionStorage, "demo", authorization);
    const recovered = recoverRequest(sessionStorage, "demo", 77);
    expect(recovered).toMatchObject({ kind: "uncertain", command: authorization });
    const second: ResolveCommand = { ...authorization, authorization_id: "grant-2" } as ResolveCommand;
    expect(() => retainRequest(sessionStorage, "demo", second)).toThrow("Another saved request needs a response");
    acknowledgeRequest(sessionStorage, "demo", authorization);
    expect(recoverRequest(sessionStorage, "demo", 77)).toEqual({ kind: "idle" });
  });

  it("blocks, rather than re-minting, a saved grant that lost its harness evidence reference", () => {
    const { evidence_id: _dropped, ...partial } = authorization as Extract<ResolveCommand, { operation: "authorize_retry" }>;
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({ version: 1, command: partial }));
    expect(recoverRequest(sessionStorage, "demo", 77)).toMatchObject({ kind: "blocked" });
    expect(sessionStorage.getItem(retryKey("demo", 77))).not.toBeNull();
  });
});
