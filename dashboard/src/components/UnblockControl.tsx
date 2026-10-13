import { useState } from "react";
import { api } from "../api/client";
import { UnblockModal } from "./UnblockModal";

interface UnblockControlProps {
  deployment: string;
  issue: number;
  /** The issue or its PR carries `needs-human`. */
  heldForHuman: boolean;
  onChanged: () => void;
}

/**
 * Unblock is a label move on GitHub (refactor decision Q19): it removes
 * `needs-human` from the issue and its open harness PRs, and nothing else.
 * Keyed by deployment and issue so an in-flight unblock
 * never surfaces under another issue's heading.
 */
export function UnblockControl(props: UnblockControlProps) {
  return <UnblockControlForIssue key={JSON.stringify([props.deployment, props.issue])} {...props} />;
}

function UnblockControlForIssue({ deployment, issue, heldForHuman, onChanged }: UnblockControlProps) {
  const [state, setState] = useState<{ busy: boolean; error: string | null } | null>(null);
  if (!heldForHuman && !state) return null;

  const unblock = async () => {
    setState({ busy: true, error: null });
    try {
      await api.unblock(deployment, issue);
      setState(null);
      onChanged();
    } catch (caught) {
      setState({ busy: false, error: caught instanceof Error ? caught.message : String(caught) });
    }
  };

  return <>
    <div className="board-actions">
      <button type="button" className="btn btn-primary" disabled={state?.busy ?? false}
        onClick={() => setState({ busy: false, error: null })}>
        Unblock
      </button>
    </div>
    {state ? <UnblockModal
      issue={issue}
      busy={state.busy}
      error={state.error}
      onConfirm={() => void unblock()}
      onCancel={() => { if (!state.busy) setState(null); }}
    /> : null}
  </>;
}
