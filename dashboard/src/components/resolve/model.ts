import { schemas } from "../../api/generated-zod";
import type { AvailableResolveControl, ResolveCommand, ResolveCommandResult, ResolveGuidance } from "../../api/types";

export type ResolveDraft =
  | { operation: "request_rearm"; gateId: string; guidance: ResolveGuidance }
  | { operation: "continue_rearm"; gateId: string; stoppedRequestId: string }
  | { operation: "cancel_rearm"; gateId: string; requestId: string; reason: string }
  | { operation: "authorize_retry"; gateId: string };

export type ResolveOperation =
  | { kind: "idle" }
  | { kind: "review"; command: ResolveCommand }
  | { kind: "sending"; command: ResolveCommand }
  | { kind: "uncertain"; command: ResolveCommand; reason: string }
  | { kind: "resolved"; command: ResolveCommand; result: ResolveCommandResult }
  | { kind: "blocked"; reason: string };

export function parseResolveCommand(value: unknown): ResolveCommand {
  return schemas.api_resolve_command_api_repos__name__issues__number__resolve_control_commands_post_Body.parse(value);
}

/** A draft can only name an action offered by the currently inspected gate. */
export function prepareResolveCommand(control: AvailableResolveControl, draft: ResolveDraft, id: string): ResolveCommand {
  const gate = control.gates.find((item) => item.gate_id === draft.gateId);
  if (!gate?.available_commands.includes(draft.operation)) throw new Error("This action is no longer available. Refresh the control status.");
  const expected = control.expected;
  if (draft.operation === "request_rearm") {
    if (draft.guidance.kind === "set" && !draft.guidance.text.trim()) throw new Error("Enter guidance or choose Keep or Clear.");
    return parseResolveCommand({ operation: draft.operation, expected, request_id: id, gate_id: draft.gateId, guidance: draft.guidance });
  }
  if (draft.operation === "authorize_retry") {
    // The evidence is the harness's own read as reported by the gate; the
    // operator only authorizes, never supplies proof.
    if (gate.cause.kind !== "retry_authorization_required") throw new Error("This action is no longer available. Refresh the control status.");
    return parseResolveCommand({
      operation: draft.operation, expected, authorization_id: id, gate_id: draft.gateId,
      command_id: gate.cause.command_id, evidence_id: gate.cause.absence_evidence_id,
    });
  }
  const intent = control.current_intent;
  if (!intent || intent.gate_id !== draft.gateId) throw new Error("The recovery request changed. Refresh the control status.");
  if (draft.operation === "continue_rearm") {
    if (intent.request_id !== draft.stoppedRequestId || !["cancelled", "superseded"].includes(intent.state)) throw new Error("Only the current stopped request can be continued.");
    return parseResolveCommand({ operation: draft.operation, expected, request_id: id, stopped_request_id: draft.stoppedRequestId });
  }
  if (intent.request_id !== draft.requestId || intent.state !== "pending") throw new Error("Only the current pending request can be cancelled.");
  if (!draft.reason.trim()) throw new Error("Enter a reason for cancellation.");
  return parseResolveCommand({ operation: draft.operation, expected, request_id: draft.requestId, cancellation_id: id, reason: draft.reason });
}

export function commandIdentity(command: ResolveCommand): string {
  switch (command.operation) {
    case "cancel_rearm": return command.cancellation_id;
    case "authorize_retry": return command.authorization_id;
    default: return command.request_id;
  }
}

/** The re-arm history a command's status read should select, if it names one. */
/** The re-arm history a command displays; null when it names none (keep the selection). */
export function commandRequestId(command: ResolveCommand): string | null {
  return command.operation === "authorize_retry" ? null : command.request_id;
}

export function operationLocksActions(operation: ResolveOperation): boolean {
  return operation.kind !== "idle" && operation.kind !== "resolved";
}

const BINDING_FIELDS = ["repository", "issue", "family", "epoch", "generation", "version", "episode_id"] as const;

/** Field-wise so server key order cannot make an identical binding look stale. */
export function sameBinding(left: ResolveCommand["expected"], right: ResolveCommand["expected"]): boolean {
  return BINDING_FIELDS.every((field) => left[field] === right[field]);
}

/** Mirrors the server identifier rule so a lookup it would refuse is never sent. */
export function requestIdProblem(id: string): string | null {
  if (!id.trim()) return "Enter a request ID.";
  // The server counts code points, not UTF-16 units.
  if ([...id].length > 512) return "Request IDs are at most 512 characters.";
  if ([...id].some((character) => character.charCodeAt(0) < 32)) return "Request IDs cannot contain control characters such as tabs.";
  if (/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(id)) return "Request ID contains an invalid character.";
  return null;
}
