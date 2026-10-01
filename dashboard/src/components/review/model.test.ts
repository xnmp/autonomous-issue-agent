import { describe, expect, it } from "vitest";
import type { ReviewRearm } from "../../api/types";
import {
  commandIdentity, commandRequestId, legacyUnblockRefusal, parseReviewCommand, prepareReviewCommand,
  reviewablePullRequests, sameBinding,
} from "./model";
import {
  currentExecution, decisionGate, executionGate, pendingRearm, reviewBinding, reviewCeilingGate, reviewControl, reviewRetryGate,
} from "./test-fixtures";

describe("prepareReviewCommand", () => {
  it("captures the exact inspected execution and revision when renewing, with no rejection authorization", () => {
    const command = prepareReviewCommand(reviewControl({ execution: currentExecution() }),
      { operation: "renew_execution", gateId: "execution-12", guidance: { kind: "keep" } }, "renew-1");
    expect(command).toEqual({ operation: "renew_execution", expected: reviewBinding, request_id: "renew-1",
      gate_id: "execution-12", guidance: { kind: "keep" }, execution_epoch: 7, execution_episode: "execution-episode-12" });
  });

  it("releases only the harness-selected confirmed verdict command", () => {
    const gate = executionGate({ gate_id: "verdict-12", cause: { kind: "review_verdict_hold",
      submission_command_id: "confirmed-submission", head_sha: "a".repeat(40) },
      actions: [{ state: "offered", operation: "release_verdict", effect: "Releases this verdict only." }] });
    const command = prepareReviewCommand(reviewControl({ execution: currentExecution([gate]) }),
      { operation: "release_verdict", gateId: gate.gate_id, guidance: { kind: "clear" } }, "release-1");
    expect(command).toMatchObject({ operation: "release_verdict", execution_epoch: 7,
      execution_episode: "execution-episode-12", submission_command_id: "confirmed-submission", expected: reviewBinding });
  });

  it("never prepares a renewal for blocked history or a known unfinished old episode", () => {
    const refused = executionGate({ actions: [{ state: "refused", operation: "renew_execution", reason: "old rounds must settle" }] });
    expect(() => prepareReviewCommand(reviewControl({ execution: currentExecution([refused]) }),
      { operation: "renew_execution", gateId: refused.gate_id, guidance: { kind: "keep" } }, "renew-1"))
      .toThrow("old rounds must settle");
    expect(() => prepareReviewCommand(reviewControl({ execution: { kind: "blocked", execution_epoch: 7,
      blockers: [{ kind: "origin", sources: [{ kind: "file", identity: "unassigned-counter", digest: "a".repeat(64) }] }] } }),
      { operation: "renew_execution", gateId: "execution-12", guidance: { kind: "keep" } }, "renew-1"))
      .toThrow(/no longer available/);
  });
  it("binds a re-arm request to the inspected revision and gate, never to a review set", () => {
    const command = prepareReviewCommand(reviewControl(), { operation: "request_rearm", gateId: "ceiling-12", guidance: { kind: "set", text: "Check the parser." } }, "id-1");
    expect(command).toEqual({
      operation: "request_rearm", expected: reviewBinding, request_id: "id-1", gate_id: "ceiling-12",
      guidance: { kind: "set", text: "Check the parser." },
    });
    expect(JSON.stringify(command)).not.toContain("review-0");
  });

  it("refuses a refused action with the server's reason instead of sending it", () => {
    const refused = reviewCeilingGate({ actions: [{ state: "refused", operation: "request_rearm", reason: "70 counted reviews exceed the 63-dismissal bound" }] }, 70);
    expect(() => prepareReviewCommand(reviewControl({ gates: [refused] }), { operation: "request_rearm", gateId: "ceiling-12", guidance: { kind: "keep" } }, "id"))
      .toThrow("70 counted reviews exceed the 63-dismissal bound");
  });

  it.each([
    ["an unknown gate", { operation: "request_rearm", gateId: "gone", guidance: { kind: "keep" } }],
    ["an action the gate never listed", { operation: "authorize_retry", gateId: "ceiling-12" }],
  ] as const)("refuses %s as stale", (_label, draft) => {
    expect(() => prepareReviewCommand(reviewControl(), draft, "id")).toThrow(/no longer available/);
  });

  it("requires text for set guidance", () => {
    expect(() => prepareReviewCommand(reviewControl(), { operation: "request_rearm", gateId: "ceiling-12", guidance: { kind: "set", text: "  " } }, "id"))
      .toThrow(/Enter guidance/);
  });

  it("takes a retry's command and evidence from the harness's own gate", () => {
    const command = prepareReviewCommand(reviewControl({ gates: [reviewRetryGate()] }), { operation: "authorize_retry", gateId: "retry-12" }, "grant-1");
    expect(command).toMatchObject({ operation: "authorize_retry", authorization_id: "grant-1", command_id: "command-12", evidence_id: "absence-12" });
  });

  it("continues only the current stopped request named by its decision gate", () => {
    const stopped: ReviewRearm = {
      state: "superseded", request_id: "request-12", gate_id: "ceiling-12", generation: 3, reason: "decision_changed",
      authorized_reviews: ["review-0"], verified_commands: [], retired_commands: ["dismiss-1"],
    };
    const control = reviewControl({ gates: [decisionGate()], current_intent: stopped });
    expect(prepareReviewCommand(control, { operation: "continue_rearm", gateId: "rearm-stopped:request-12", stoppedRequestId: "request-12" }, "next"))
      .toMatchObject({ operation: "continue_rearm", request_id: "next", stopped_request_id: "request-12" });
    expect(() => prepareReviewCommand(control, { operation: "continue_rearm", gateId: "rearm-stopped:request-12", stoppedRequestId: "other" }, "next"))
      .toThrow(/current stopped request/);
  });

  it("cancels only the pending request that owns the gate, with a reason", () => {
    const control = reviewControl({
      gates: [reviewCeilingGate({ state: "rearming", actions: [{ state: "offered", operation: "cancel_rearm", effect: "Stops request-12." }] })],
      current_intent: pendingRearm(),
    });
    expect(prepareReviewCommand(control, { operation: "cancel_rearm", gateId: "ceiling-12", requestId: "request-12", reason: "wrong" }, "cancel-1"))
      .toMatchObject({ operation: "cancel_rearm", request_id: "request-12", cancellation_id: "cancel-1", reason: "wrong" });
    expect(() => prepareReviewCommand(control, { operation: "cancel_rearm", gateId: "ceiling-12", requestId: "request-12", reason: " " }, "c"))
      .toThrow(/reason/);
    expect(() => prepareReviewCommand(control, { operation: "cancel_rearm", gateId: "ceiling-12", requestId: "other", reason: "x" }, "c"))
      .toThrow(/current pending request/);
  });
});

describe("command identity and binding", () => {
  it("names each command by its own identity and selects re-arm history only for re-arm commands", () => {
    const grant = parseReviewCommand({ operation: "authorize_retry", expected: reviewBinding, authorization_id: "g", gate_id: "x", command_id: "c", evidence_id: "e" });
    const cancel = parseReviewCommand({ operation: "cancel_rearm", expected: reviewBinding, cancellation_id: "k", request_id: "r", reason: "x" });
    expect([commandIdentity(grant), commandIdentity(cancel)]).toEqual(["g", "k"]);
    expect([commandRequestId(grant), commandRequestId(cancel)]).toEqual([null, "r"]);
  });

  it("compares bindings field-wise and notices any changed field", () => {
    const reordered = Object.fromEntries(Object.entries(reviewBinding).reverse()) as typeof reviewBinding;
    expect(sameBinding(reviewBinding, reordered)).toBe(true);
    for (const field of Object.keys(reviewBinding) as (keyof typeof reviewBinding)[]) {
      const changed = { ...reviewBinding, [field]: typeof reviewBinding[field] === "number" ? 99 : "other" };
      expect(sameBinding(reviewBinding, changed as never)).toBe(false);
    }
  });

  it.each([
    { operation: "request_rearm", expected: reviewBinding, request_id: "r", gate_id: "g" },
    { operation: "request_rearm", expected: { ...reviewBinding, family: "resolve" }, request_id: "r", gate_id: "g", guidance: { kind: "keep" } },
    { operation: "request_rearm", expected: reviewBinding, request_id: "x".repeat(513), gate_id: "g", guidance: { kind: "keep" } },
    { operation: "cancel_rearm", expected: reviewBinding, cancellation_id: "k", request_id: "r", reason: "x".repeat(16385) },
    { operation: "dismiss_everything", expected: reviewBinding },
    null,
  ])("rejects a forged or oversized command %#", (value) => {
    expect(() => parseReviewCommand(value)).toThrow();
  });
});

describe("legacyUnblockRefusal", () => {
  const families = (...active: ("resolve" | "review_rejections")[]) => ({ status: "available" as const, repository: "o/r", active_families: active });

  it("allows the legacy unblock only when the repository read shows no family active", () => {
    expect(legacyUnblockRefusal(families(), [])).toBeNull();
    expect(legacyUnblockRefusal(families(), [3, 4])).toBeNull();
  });

  it("points at the linked pull requests' review controls when the review family is active", () => {
    expect(legacyUnblockRefusal(families("review_rejections"), [4])).toMatch(/retired\. Review holds on PR #4 are shown in its review control below/);
    expect(legacyUnblockRefusal(families("review_rejections"), [3, 4])).toMatch(/PR #3, PR #4 are shown in their review controls/);
  });

  it("refuses a pull-request-less issue under the review family without pointing at a missing control", () => {
    const refusal = legacyUnblockRefusal(families("review_rejections"), []);
    expect(refusal).toMatch(/retired/);
    expect(refusal).not.toMatch(/PR #/);
    expect(refusal).toMatch(/resolve control, which is not active/);
  });

  it("retires the legacy unblock when resolve control is active", () => {
    expect(legacyUnblockRefusal(families("resolve", "review_rejections"), [4])).toMatch(/Resolve control is active/);
  });

  it("never treats an unread or unavailable family status as none active", () => {
    expect(legacyUnblockRefusal(null, [])).toMatch(/not confirmed/);
    expect(legacyUnblockRefusal({ status: "unavailable", reason: "database is locked" }, [4])).toMatch(/unavailable \(database is locked\)/);
  });
});

describe("reviewablePullRequests", () => {
  it("merges both link lists, drops merged and malformed numbers, and orders them", () => {
    const links = [{ number: 9, state: "merged" }, { number: 7, state: "open" }, { number: 8, state: "closed" }, { number: 0, state: null }];
    expect(reviewablePullRequests(links, [7, 11, 9, -1, 2 ** 60])).toEqual([7, 8, 11]);
  });
});
