import { ConfirmModal } from "./ui/Modal";

/**
 * Confirmation for the one operator write that resumes a held issue.
 *
 * It removes the labels that hold the issue for a human (`needs-human-review`,
 * `needs-budget-review`, `ai-blocked`) and nothing else, so the daemons
 * re-derive the stage on their next tick. Shared by the board row and the issue
 * page so both spell out the same consequence before firing.
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
            The harness picks the issue back up on its next tick and will start
            spending again. If it escalated with a question, answer the question
            on GitHub first.
          </p>
        </>
      }
    />
  );
}
