import { schemas } from "../../api/generated-zod";
import type {
  AvailableReviewControl, ReviewCommand, ReviewCommandResult, ReviewGate, ReviewGateAction, ReviewGuidance,
  WorkflowFamilies,
} from "../../api/types";

export { requestIdProblem } from "../resolve/model";

export type ReviewDraft =
  | { operation: "request_rearm" | "renew_execution" | "release_verdict"; gateId: string; guidance: ReviewGuidance }
  | { operation: "continue_rearm"; gateId: string; stoppedRequestId: string }
  | { operation: "cancel_rearm"; gateId: string; requestId: string; reason: string }
  | { operation: "authorize_retry"; gateId: string };

export type ReviewOperation =
  | { kind: "idle" }
  | { kind: "review"; command: ReviewCommand }
  | { kind: "sending"; command: ReviewCommand }
  | { kind: "uncertain"; command: ReviewCommand; reason: string }
  | { kind: "resolved"; command: ReviewCommand; result: ReviewCommandResult }
  | { kind: "blocked"; reason: string };

export function parseReviewCommand(value: unknown): ReviewCommand {
  return schemas.api_review_command_api_repos__name__pulls__pr__review_control_commands_post_Body.parse(value);
}

const STALE = "This action is no longer available. Refresh the review control status.";

/** The server's verdict on one operation for one gate: offered, refused with its reason, or absent. */
export function gateAction(gate: ReviewGate, operation: ReviewDraft["operation"]): ReviewGateAction | undefined {
  return gate.actions.find((action) => action.operation === operation);
}

/**
 * A draft can only name an action the inspected gate offers. A refused action
 * never becomes a command: its reason is already known, so sending it would
 * only buy a 409 or 422 for something the operator was shown.
 */
export function prepareReviewCommand(control: AvailableReviewControl, draft: ReviewDraft, id: string): ReviewCommand {
  const gates = control.execution?.kind === "current"
    ? [...control.gates, ...control.execution.recovery_choices] : control.gates;
  const gate = gates.find((item) => item.gate_id === draft.gateId);
  const action = gate && gateAction(gate, draft.operation);
  if (!gate || !action) throw new Error(STALE);
  if (action.state === "refused") throw new Error(action.reason);
  const expected = control.expected;
  switch (draft.operation) {
    case "renew_execution":
    case "release_verdict": {
      const execution = control.execution;
      if (execution?.kind !== "current" || (draft.operation === "renew_execution"
        ? gate.cause.kind !== "review_execution_ceiling" : gate.cause.kind !== "review_verdict_hold")) throw new Error(STALE);
      if (draft.guidance.kind === "set" && !draft.guidance.text.trim()) throw new Error("Enter guidance or choose Keep or Clear.");
      return parseReviewCommand({ operation: draft.operation, expected, request_id: id, gate_id: gate.gate_id,
        guidance: draft.guidance, execution_epoch: execution.execution_epoch, execution_episode: execution.execution_episode,
        ...(gate.cause.kind === "review_verdict_hold" ? { submission_command_id: gate.cause.submission_command_id } : {}) });
    }
    case "request_rearm":
      if (draft.guidance.kind === "set" && !draft.guidance.text.trim()) throw new Error("Enter guidance or choose Keep or Clear.");
      return parseReviewCommand({ operation: draft.operation, expected, request_id: id, gate_id: gate.gate_id, guidance: draft.guidance });
    case "authorize_retry":
      // The evidence is the harness's own read as reported by the gate; the
      // operator only authorizes, never supplies proof.
      if (gate.cause.kind !== "retry_authorization_required") throw new Error(STALE);
      return parseReviewCommand({
        operation: draft.operation, expected, authorization_id: id, gate_id: gate.gate_id,
        command_id: gate.cause.command_id, evidence_id: gate.cause.absence_evidence_id,
      });
    case "continue_rearm": {
      const intent = control.current_intent;
      if (gate.cause.kind !== "rearm_needs_decision" || gate.cause.stopped_request_id !== draft.stoppedRequestId
        || !intent || intent.request_id !== draft.stoppedRequestId || (intent.state !== "cancelled" && intent.state !== "superseded")) {
        throw new Error("Only the current stopped request can be continued.");
      }
      return parseReviewCommand({ operation: draft.operation, expected, request_id: id, stopped_request_id: draft.stoppedRequestId });
    }
    case "cancel_rearm": {
      const intent = control.current_intent;
      if (!intent || intent.state !== "pending" || intent.request_id !== draft.requestId || intent.gate_id !== gate.gate_id) {
        throw new Error("Only the current pending request can be cancelled.");
      }
      if (!draft.reason.trim()) throw new Error("Enter a reason for cancellation.");
      return parseReviewCommand({ operation: draft.operation, expected, request_id: draft.requestId, cancellation_id: id, reason: draft.reason });
    }
  }
}

export function commandIdentity(command: ReviewCommand): string {
  switch (command.operation) {
    case "cancel_rearm": return command.cancellation_id;
    case "authorize_retry": return command.authorization_id;
    default: return command.request_id;
  }
}

/** The re-arm history a command displays; null when it names none (keep the selection). */
export function commandRequestId(command: ReviewCommand): string | null {
  return command.operation === "authorize_retry" ? null : command.request_id;
}

export function operationLocksActions(operation: ReviewOperation): boolean {
  return operation.kind !== "idle" && operation.kind !== "resolved";
}

const BINDING_FIELDS = ["repository", "pull_request", "family", "epoch", "generation", "version", "episode_id"] as const;

/** Field-wise so server key order cannot make an identical binding look stale. */
export function sameBinding(left: ReviewCommand["expected"], right: ReviewCommand["expected"]): boolean {
  return BINDING_FIELDS.every((field) => left[field] === right[field]);
}

const prList = (prs: readonly number[]) => prs.map((pr) => `PR #${pr}`).join(", ");

/**
 * Why the label-based legacy unblock must stay disabled, or null when the
 * repository's family status was freshly read with no family active.
 *
 * The server refuses the legacy unblock while any workflow family is active
 * for the repository (never two authorities); this derives the same refusal
 * from the same repository-scoped read, so it is shown rather than offered and
 * then refused. `families` is null while no fresh read is witnessed: unknown
 * is not evidence that nothing is active. `linkedPulls` only words the pointer.
 */
export function legacyUnblockRefusal(families: WorkflowFamilies | null, linkedPulls: readonly number[]): string | null {
  if (families === null) {
    return "The repository’s workflow family status is not confirmed, so the label-based unblock stays disabled. Refresh once the status reads.";
  }
  if (families.status === "unavailable") {
    return `The repository’s workflow family status is unavailable (${families.reason}), so the label-based unblock stays disabled.`;
  }
  const active = families.active_families;
  if (active.includes("resolve")) {
    return "Resolve control is active for this repository, so the label-based unblock is retired. Use the resolve control’s typed commands.";
  }
  if (active.includes("review_rejections")) {
    const pointer = linkedPulls.length
      ? ` Review holds on ${prList(linkedPulls)} are shown in ${linkedPulls.length === 1 ? "its review control" : "their review controls"} below.`
      : " No unmerged pull request is linked to this issue, and its issue holds need resolve control, which is not active for this repository.";
    return `Review rejection control is active for this repository, so the label-based unblock is retired.${pointer}`;
  }
  return null;
}

/**
 * The linked pull requests whose review control the issue page mounts. A
 * merged pull request is never reviewed again; a closed one may still carry
 * holds, so it stays visible until its control reads as quiet.
 */
export function reviewablePullRequests(
  links: readonly { number: number; state: string | null }[], numbers: readonly number[],
): number[] {
  const merged = new Set(links.filter((link) => link.state === "merged").map((link) => link.number));
  const all = [...links.map((link) => link.number), ...numbers];
  return [...new Set(all)].filter((pr) => Number.isSafeInteger(pr) && pr > 0 && !merged.has(pr)).sort((a, b) => a - b);
}
