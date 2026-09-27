import { api } from "../../api/client";
import { usePolled } from "../../api/hooks";
import { legacyUnblockRefusal } from "./model";

/**
 * Why the label-based unblock is refused for an issue whose linked
 * pull requests are `prs`, from one repository-scoped family read. The read
 * runs only while the legacy lever could be offered (`needed`); a read in
 * flight or failed is unknown, which refuses.
 */
export function useLegacyUnblockRefusal(deployment: string, prs: readonly number[], needed: boolean): string | null {
  const read = usePolled(
    needed ? JSON.stringify(["workflow-families", deployment]) : null,
    (signal) => api.workflowFamilies(deployment, signal),
    5_000, { cache: false },
  );
  return legacyUnblockRefusal(read.error ? null : read.data, prs);
}
