import { parseOperatorCommand, prepareOperatorCommand, type ReleaseDraft, type ReleaseOperation } from "../../lib/release-command";
import { schemas } from "../../api/generated-zod";
import type {
  AvailableReviewControl, ReviewCommand, ReviewCommandResult, ReviewGate, ReviewGateAction,
  WorkflowFamilies,
} from "../../api/types";

export { requestIdProblem } from "../resolve/model";

export type ReviewDraft = ReleaseDraft;

export type ReviewOperation = ReleaseOperation<ReviewCommand, ReviewCommandResult>;

export const parseReviewCommand = (value: unknown): ReviewCommand =>
  parseOperatorCommand<ReviewCommand>((body) => schemas.api_review_command_api_repos__name__pulls__pr__review_control_commands_post_Body.parse(body), value);

export { commandIdentity, commandRequestId, operationLocksActions, sameBinding } from "../../lib/release-command";

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
  return parseReviewCommand(prepareOperatorCommand(control, gate, draft, id));
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
