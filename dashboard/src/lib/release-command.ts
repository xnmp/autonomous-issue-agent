import type { components } from "../api/generated";

type Schemas = components["schemas"];
type Guidance = Schemas["ResumeGate"]["guidance"];
type Rearm<E> = { expected: E; request_id: string; gate_id: string; guidance: Guidance };
type Execution = { execution_epoch: number; execution_episode: string };
export type ReleaseCommand<E> =
  | (Rearm<E> & { operation: "request_rearm" })
  | (Rearm<E> & Execution & { operation: "renew_execution" })
  | (Rearm<E> & Execution & { operation: "release_verdict"; submission_command_id: string })
  | { operation: "continue_rearm"; expected: E; request_id: string; stopped_request_id: string }
  | { operation: "authorize_retry"; expected: E; authorization_id: string; gate_id: string; command_id: string; evidence_id: string };
export type OperatorCommand<E> = ReleaseCommand<E>
  | { operation: "cancel_rearm"; expected: E; cancellation_id: string; request_id: string; reason: string };

/** Only HTTP changes shape; browser journals and command identities stay stable. */
export function releasePayload<E>(command: OperatorCommand<E>) {
  if (command.operation === "cancel_rearm") return command;
  if (command.operation === "continue_rearm") {
    return { operation: "release", expected: command.expected, request_id: command.request_id,
      selection: { kind: "stopped_grant", stopped_request_id: command.stopped_request_id } };
  }
  if (command.operation === "authorize_retry") {
    return { operation: "release", expected: command.expected, request_id: command.authorization_id,
      selection: { kind: "gate", gate_id: command.gate_id,
        intent: { kind: "retry_absent", command_id: command.command_id, evidence_id: command.evidence_id } } };
  }
  const { operation, expected, request_id, gate_id, ...intent } = command;
  return { operation: "release", expected, request_id, selection: operation === "release_verdict"
    ? { kind: "verdict", gate_id, ...intent }
    : { kind: "gate", gate_id, intent: { kind: operation === "request_rearm" ? "resume" : operation, ...intent } } };
}

/** Validate the unchanged journal contract through the closed HTTP schema. */
export function parseOperatorCommand<C extends OperatorCommand<unknown>>(parse: (value: unknown) => unknown, value: unknown): C {
  if (!value || typeof value !== "object" || !("operation" in value)) throw new Error("Input does not match the command contract.");
  const command = value as C;
  const fields: Record<string, string[]> = {
    request_rearm: ["request_id", "gate_id", "guidance"],
    renew_execution: ["request_id", "gate_id", "guidance", "execution_epoch", "execution_episode"],
    release_verdict: ["request_id", "gate_id", "guidance", "execution_epoch", "execution_episode", "submission_command_id"],
    continue_rearm: ["request_id", "stopped_request_id"],
    authorize_retry: ["authorization_id", "gate_id", "command_id", "evidence_id"],
    cancel_rearm: ["cancellation_id", "request_id", "reason"],
  };
  const keys = fields[command.operation]?.concat(["operation", "expected"]);
  if (!keys || keys.length !== Object.keys(value).length || Object.keys(value).some((key) => !keys.includes(key))) throw new Error("Input does not match the command contract.");
  parse(releasePayload(command));
  return command;
}

export type ReleaseDraft<O extends string = "request_rearm" | "renew_execution" | "release_verdict"> =
  | { operation: Extract<"request_rearm" | "renew_execution" | "release_verdict", O>; gateId: string; guidance: Guidance }
  | { operation: "continue_rearm"; gateId: string; stoppedRequestId: string }
  | { operation: "cancel_rearm"; gateId: string; requestId: string; reason: string }
  | { operation: "authorize_retry"; gateId: string };
export type ReleaseOperation<C, R> =
  | { kind: "idle" }
  | { kind: "review"; command: C }
  | { kind: "sending"; command: C }
  | { kind: "uncertain"; command: C; reason: string }
  | { kind: "resolved"; command: C; result: R }
  | { kind: "blocked"; reason: string };

export const commandIdentity = (command: OperatorCommand<unknown>): string =>
  command.operation === "cancel_rearm" ? command.cancellation_id : command.operation === "authorize_retry" ? command.authorization_id : command.request_id;
export const commandRequestId = (command: OperatorCommand<unknown>): string | null =>
  command.operation === "authorize_retry" ? null : command.request_id;
export const operationLocksActions = (operation: ReleaseOperation<unknown, unknown>): boolean =>
  operation.kind !== "idle" && operation.kind !== "resolved";

type Expected = Schemas["ResolveExpected"] | Schemas["ReviewExpected"];
export const sameBinding = (left: Expected, right: Expected): boolean =>
  Object.keys(left).every((field) => Reflect.get(left, field) === Reflect.get(right, field));

type Control = Schemas["AvailableControl"] | Schemas["ReviewAvailableControl"];
type Gate = Schemas["Gate"] | Schemas["ReviewGate"];
const STALE = "This action is no longer available. Refresh the control status.";

export function prepareOperatorCommand(control: Control, gate: Gate, draft: ReleaseDraft, id: string): unknown {
  const expected = control.expected;
  if (draft.operation === "authorize_retry") {
    if (gate.cause.kind !== "retry_authorization_required") throw new Error(STALE);
    return { operation: draft.operation, expected, authorization_id: id, gate_id: gate.gate_id,
      command_id: gate.cause.command_id, evidence_id: gate.cause.absence_evidence_id };
  }
  if (draft.operation === "continue_rearm" || draft.operation === "cancel_rearm") {
    const intent = control.current_intent;
    if ("available_commands" in gate && (!intent || intent.gate_id !== gate.gate_id)) throw new Error("The recovery request changed. Refresh the control status.");
    if (draft.operation === "continue_rearm") {
      if (!intent || intent.request_id !== draft.stoppedRequestId || (intent.state !== "cancelled" && intent.state !== "superseded")
        || ("actions" in gate && (gate.cause.kind !== "rearm_needs_decision" || gate.cause.stopped_request_id !== draft.stoppedRequestId))) throw new Error("Only the current stopped request can be continued.");
      return { operation: draft.operation, expected, request_id: id, stopped_request_id: draft.stoppedRequestId };
    }
    if (!intent || intent.state !== "pending" || intent.request_id !== draft.requestId || intent.gate_id !== gate.gate_id) throw new Error("Only the current pending request can be cancelled.");
    if (!draft.reason.trim()) throw new Error("Enter a reason for cancellation.");
    return { operation: draft.operation, expected, request_id: intent.request_id, cancellation_id: id, reason: draft.reason };
  }
  if (draft.guidance.kind === "set" && !draft.guidance.text.trim()) throw new Error("Enter guidance or choose Keep or Clear.");
  const command = { operation: draft.operation, expected, request_id: id, gate_id: gate.gate_id, guidance: draft.guidance };
  if (draft.operation === "request_rearm") return command;
  const execution = "execution" in control ? control.execution : null;
  if (execution?.kind !== "current" || (draft.operation === "renew_execution"
    ? gate.cause.kind !== "review_execution_ceiling" : gate.cause.kind !== "review_verdict_hold")) throw new Error(STALE);
  return { ...command, execution_epoch: execution.execution_epoch, execution_episode: execution.execution_episode,
    ...(gate.cause.kind === "review_verdict_hold" ? { submission_command_id: gate.cause.submission_command_id } : {}) };
}
