import { parseOperatorCommand, prepareOperatorCommand, type ReleaseDraft, type ReleaseOperation } from "../../lib/release-command";
import { schemas } from "../../api/generated-zod";
import type { AvailableResolveControl, ResolveCommand, ResolveCommandResult } from "../../api/types";

export type ResolveDraft = ReleaseDraft;

export type ResolveOperation = ReleaseOperation<ResolveCommand, ResolveCommandResult>;

export const parseResolveCommand = (value: unknown): ResolveCommand =>
  parseOperatorCommand<ResolveCommand>((body) => schemas.api_resolve_command_api_repos__name__issues__number__resolve_control_commands_post_Body.parse(body), value);

export { commandIdentity, commandRequestId, operationLocksActions, sameBinding } from "../../lib/release-command";

/** A draft can only name an action offered by the currently inspected gate. */
export function prepareResolveCommand(control: AvailableResolveControl, draft: ResolveDraft, id: string): ResolveCommand {
  const gate = control.gates.find((item) => item.gate_id === draft.gateId);
  if (!gate?.available_commands.includes(draft.operation)) throw new Error("This action is no longer available. Refresh the control status.");
  return parseResolveCommand(prepareOperatorCommand(control, gate, draft, id));
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
