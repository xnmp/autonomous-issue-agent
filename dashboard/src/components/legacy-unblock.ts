import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import type { WorkflowFamilies } from "../api/types";

/**
 * Why the label-based legacy unblock must stay disabled, or null when the
 * repository's family status was freshly read with resolve control inactive.
 *
 * The server refuses the legacy unblock while resolve control is active (never
 * two authorities); this derives the same refusal from the same
 * repository-scoped read, so it is shown rather than offered and then refused.
 * A retained review-family epoch is inert since the review family was deleted.
 * `families` is null while no fresh read is witnessed: unknown is not evidence
 * that nothing is active.
 */
export function legacyUnblockRefusal(families: WorkflowFamilies | null): string | null {
  if (families === null) {
    return "The repository’s workflow family status is not confirmed, so the label-based unblock stays disabled. Refresh once the status reads.";
  }
  if (families.status === "unavailable") {
    return `The repository’s workflow family status is unavailable (${families.reason}), so the label-based unblock stays disabled.`;
  }
  if (families.active_families.includes("resolve")) {
    return "Resolve control is active for this repository, so the label-based unblock is retired. Use the resolve control’s typed commands.";
  }
  return null;
}

/**
 * The legacy unblock refusal from one repository-scoped family read. The read
 * runs only while the legacy lever could be offered (`needed`); a read in
 * flight or failed is unknown, which refuses.
 */
export function useLegacyUnblockRefusal(deployment: string, needed: boolean): string | null {
  const read = usePolled(
    needed ? JSON.stringify(["workflow-families", deployment]) : null,
    (signal) => api.workflowFamilies(deployment, signal),
    5_000, { cache: false },
  );
  return legacyUnblockRefusal(read.error ? null : read.data);
}
