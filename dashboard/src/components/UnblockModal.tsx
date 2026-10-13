import { ConfirmModal } from "./ui/Modal";

/** Confirmation for Unblock: one label move on GitHub (refactor decision Q19). */
export function UnblockModal({
  issue,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  issue: number;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <ConfirmModal
      title="Unblock issue"
      confirmLabel={`Unblock #${issue}`}
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
      body={
        <>
          <p>
            Remove <span className="mono">needs-human</span> from{" "}
            <strong className="mono">#{issue}</strong> and its open harness PRs?
            No other label changes.
          </p>
          <p className="muted">
            The harness may resume paid work on it. If it stopped with a
            question, answer it on GitHub first.
          </p>
        </>
      }
    />
  );
}
