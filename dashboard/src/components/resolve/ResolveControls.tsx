import { useState } from "react";
import { api } from "../../api/client";
import { UnblockModal } from "../UnblockModal";
import { operationLocksActions } from "./model";
import { ResolveControlView } from "./ResolveControlView";
import { useResolveControl } from "./use-resolve-control";

interface ResolveControlsProps {
  deployment: string;
  issue: number;
  /** The issue is held for a human by labels or stage (legacy choreography). */
  heldForHuman: boolean;
  /** Set when a linked pull request's review control retires the label-based unblock. */
  legacyRefusal?: string | null;
  onChanged: () => void;
}

/**
 * The single owner of resolve mutations on the issue page. Keyed by
 * deployment and issue so a restored retry record or an in-flight command
 * can never surface under another issue's heading.
 */
export function ResolveControls(props: ResolveControlsProps) {
  return <ResolveControlsForIssue key={JSON.stringify([props.deployment, props.issue])} {...props} />;
}

function ResolveControlsForIssue({ deployment, issue, heldForHuman, legacyRefusal = null, onChanged }: ResolveControlsProps) {
  const controller = useResolveControl(deployment, issue);
  const [legacy, setLegacy] = useState<{ busy: boolean; error: string | null } | null>(null);
  const { control, readError, loading, operation } = controller;
  // Only a fresh, witnessed inactive family may use the label-based unblock.
  // Absent, unavailable, unknown and active authority never fall back to it,
  // and neither does an active review family (the server refuses it too).
  const legacyAllowed = heldForHuman && control?.status === "inactive" && !legacyRefusal
    && !readError && !loading && !operationLocksActions(operation);
  const quiet = operation.kind === "idle" && !readError && !controller.actionError && !heldForHuman
    && !controller.lookupId
    && (control === null || control.status === "inactive" || control.status === "absent");
  if (quiet && !legacy) return null;

  const unblock = async () => {
    if (!legacyAllowed) {
      setLegacy({ busy: false, error: "Resolve authority changed. Legacy unblock is no longer available; review the controls above." });
      return;
    }
    setLegacy({ busy: true, error: null });
    try {
      await api.unblock(deployment, issue);
      setLegacy(null);
      controller.onRefresh();
      onChanged();
    } catch (caught) {
      setLegacy({ busy: false, error: caught instanceof Error ? caught.message : String(caught) });
    }
  };

  return <>
    <ResolveControlView
      issue={issue}
      control={control}
      loading={loading}
      readError={readError}
      actionError={controller.actionError}
      operation={operation}
      legacyAllowed={legacyAllowed}
      legacyRefusal={heldForHuman && control?.status === "inactive" ? legacyRefusal : null}
      onPrepare={controller.onPrepare}
      onConfirm={controller.onConfirm}
      onDismiss={controller.onDismiss}
      onRetryCommand={controller.onRetryCommand}
      onRefresh={controller.onRefresh}
      lookupId={controller.lookupId}
      onHistoryLookup={controller.onHistoryLookup}
      onClearLookup={controller.onClearLookup}
      onLegacy={() => { if (legacyAllowed) setLegacy({ busy: false, error: null }); }}
    />
    {legacy ? <UnblockModal
      issue={issue}
      busy={legacy.busy}
      error={legacy.error}
      onConfirm={() => void unblock()}
      onCancel={() => { if (!legacy.busy) setLegacy(null); }}
    /> : null}
  </>;
}
