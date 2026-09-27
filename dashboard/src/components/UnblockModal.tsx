import { ConfirmModal } from "./ui/Modal";

/**
 * Confirmation for legacy breaker recovery while workflow families are inactive.
 */
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
            Remove the holds on <strong className="mono">#{issue}</strong> —{" "}
            <span className="mono">needs-human-review</span>,{" "}
            <span className="mono">needs-budget-review</span> and{" "}
            <span className="mono">ai-blocked</span>?
          </p>
          <p className="muted">
            This resets legacy retry breakers and may allow paid work to resume.
            Review rejection holds require review control activation. If the
            issue escalated with a question, answer it on GitHub first.
          </p>
        </>
      }
    />
  );
}
