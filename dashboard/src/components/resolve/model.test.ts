import { describe, expect, it } from "vitest";

import type { ResolveCommand } from "../../api/types";
import { commandIdentity, operationLocksActions, prepareResolveCommand, sameBinding, type ResolveOperation } from "./model";
import { availableControl, binding, ceilingGate, rearm } from "./test-fixtures";

describe("prepareResolveCommand", () => {
  it("builds a request_rearm command scoped to the inspected gate and control's expected binding", () => {
    const control = availableControl();
    const command = prepareResolveCommand(control, { operation: "request_rearm", gateId: "gate-77", guidance: { kind: "keep" } }, "id-1");
    expect(command).toEqual({
      operation: "request_rearm",
      expected: binding,
      request_id: "id-1",
      gate_id: "gate-77",
      guidance: { kind: "keep" },
    });
  });

  it("refuses an action the gate does not currently offer", () => {
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["cancel_rearm"] })] });
    expect(() => prepareResolveCommand(control, { operation: "request_rearm", gateId: "gate-77", guidance: { kind: "keep" } }, "id-1"))
      .toThrow("This action is no longer available. Refresh the control status.");
  });

  it("refuses an action against a gate ID that no longer exists on the control", () => {
    const control = availableControl();
    expect(() => prepareResolveCommand(control, { operation: "request_rearm", gateId: "missing-gate", guidance: { kind: "keep" } }, "id-1"))
      .toThrow("This action is no longer available. Refresh the control status.");
  });

  it("refuses set guidance with blank text before building the command", () => {
    const control = availableControl();
    expect(() => prepareResolveCommand(control, { operation: "request_rearm", gateId: "gate-77", guidance: { kind: "set", text: "   " } }, "id-1"))
      .toThrow("Enter guidance or choose Keep or Clear.");
  });

  it("accepts set guidance with non-blank text", () => {
    const control = availableControl();
    const command = prepareResolveCommand(control, { operation: "request_rearm", gateId: "gate-77", guidance: { kind: "set", text: "watch the API" } }, "id-1");
    expect(command).toMatchObject({ operation: "request_rearm", guidance: { kind: "set", text: "watch the API" } });
  });

  it("builds a continue_rearm command for the current stopped intent", () => {
    const intent = rearm({ state: "cancelled" });
    const control = availableControl({
      gates: [ceilingGate({ available_commands: ["continue_rearm"] })],
      current_intent: intent,
    });
    const command = prepareResolveCommand(control, { operation: "continue_rearm", gateId: "gate-77", stoppedRequestId: intent.request_id }, "id-2");
    expect(command).toEqual({
      operation: "continue_rearm",
      expected: binding,
      request_id: "id-2",
      stopped_request_id: "request-original",
    });
  });

  it("refuses continue_rearm when there is no current intent on the gate", () => {
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["continue_rearm"] })], current_intent: null });
    expect(() => prepareResolveCommand(control, { operation: "continue_rearm", gateId: "gate-77", stoppedRequestId: "request-original" }, "id-2"))
      .toThrow("The recovery request changed. Refresh the control status.");
  });

  it("refuses continue_rearm when the current intent belongs to a different gate", () => {
    const intent = rearm({ state: "cancelled", gate_id: "other-gate" });
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["continue_rearm"] })], current_intent: intent });
    expect(() => prepareResolveCommand(control, { operation: "continue_rearm", gateId: "gate-77", stoppedRequestId: intent.request_id }, "id-2"))
      .toThrow("The recovery request changed. Refresh the control status.");
  });

  it("refuses continue_rearm when the stopped ID no longer matches the current intent", () => {
    const intent = rearm({ state: "cancelled" });
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["continue_rearm"] })], current_intent: intent });
    expect(() => prepareResolveCommand(control, { operation: "continue_rearm", gateId: "gate-77", stoppedRequestId: "some-other-request" }, "id-2"))
      .toThrow("Only the current stopped request can be continued.");
  });

  it("refuses continue_rearm when the intent is not in a stopped state", () => {
    const intent = rearm({ state: "pending" });
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["continue_rearm"] })], current_intent: intent });
    expect(() => prepareResolveCommand(control, { operation: "continue_rearm", gateId: "gate-77", stoppedRequestId: intent.request_id }, "id-2"))
      .toThrow("Only the current stopped request can be continued.");
  });

  it("builds a cancel_rearm command whose cancellation_id differs from the target request_id", () => {
    const intent = rearm({ state: "pending" });
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["cancel_rearm"] })], current_intent: intent });
    const command = prepareResolveCommand(control, { operation: "cancel_rearm", gateId: "gate-77", requestId: intent.request_id, reason: "tests still failing" }, "cancel-id-1");
    expect(command).toEqual({
      operation: "cancel_rearm",
      expected: binding,
      request_id: "request-original",
      cancellation_id: "cancel-id-1",
      reason: "tests still failing",
    });
    expect(command).not.toHaveProperty("cancellation_id", (command as { request_id: string }).request_id);
  });

  it("refuses cancel_rearm when the target request is not pending, even with a reason given", () => {
    const intent = rearm({ state: "completed" });
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["cancel_rearm"] })], current_intent: intent });
    expect(() => prepareResolveCommand(control, { operation: "cancel_rearm", gateId: "gate-77", requestId: intent.request_id, reason: "stop it" }, "cancel-id-1"))
      .toThrow("Only the current pending request can be cancelled.");
  });

  it("refuses cancel_rearm with a blank reason against a valid pending target", () => {
    const intent = rearm({ state: "pending" });
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["cancel_rearm"] })], current_intent: intent });
    expect(() => prepareResolveCommand(control, { operation: "cancel_rearm", gateId: "gate-77", requestId: intent.request_id, reason: "   " }, "cancel-id-1"))
      .toThrow("Enter a reason for cancellation.");
  });
});

describe("commandIdentity", () => {
  it("uses cancellation_id for cancel_rearm and request_id for the other operations", () => {
    const control = availableControl({ gates: [ceilingGate({ available_commands: ["request_rearm", "cancel_rearm"] })], current_intent: rearm({ state: "pending" }) });
    const request = prepareResolveCommand(control, { operation: "request_rearm", gateId: "gate-77", guidance: { kind: "keep" } }, "req-1");
    const cancel = prepareResolveCommand(control, { operation: "cancel_rearm", gateId: "gate-77", requestId: "request-original", reason: "stop" }, "cancel-1");
    expect(commandIdentity(request)).toBe("req-1");
    expect(commandIdentity(cancel)).toBe("cancel-1");
  });
});

describe("operationLocksActions", () => {
  it.each<[ResolveOperation["kind"], boolean]>([
    ["idle", false],
    ["resolved", false],
    ["review", true],
    ["sending", true],
    ["uncertain", true],
    ["blocked", true],
  ])("returns %s -> locked=%s", (kind, locked) => {
    const command = prepareResolveCommand(availableControl(), { operation: "request_rearm", gateId: "gate-77", guidance: { kind: "keep" } }, "id");
    const operation: ResolveOperation = kind === "idle" ? { kind: "idle" }
      : kind === "resolved" ? { kind: "resolved", command, result: { outcome: "accepted", reason: "", control: availableControl() } }
      : kind === "review" ? { kind: "review", command }
      : kind === "sending" ? { kind: "sending", command }
      : kind === "uncertain" ? { kind: "uncertain", command, reason: "network error" }
      : { kind: "blocked", reason: "storage unavailable" };
    expect(operationLocksActions(operation)).toBe(locked);
  });
});

describe("sameBinding", () => {
  it("is true for structurally equal bindings regardless of object identity", () => {
    expect(sameBinding({ ...binding }, { ...binding })).toBe(true);
  });

  it.each([
    { repository: "other/repo" },
    { issue: binding.issue + 1 },
    { epoch: binding.epoch + 1 },
    { generation: binding.generation + 1 },
    { version: binding.version + 1 },
    { episode_id: "other-episode" },
  ] as const)("is false when a binding field differs: %j", (change) => {
    const other: ResolveCommand["expected"] = { ...binding, ...change };
    expect(sameBinding(binding, other)).toBe(false);
  });
});
