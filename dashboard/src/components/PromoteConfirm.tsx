import { ConfirmModal } from "./ui/Modal";

/**
 * The one confirmation before an issue is promoted to ai-ready: the fixer
 * picks it up on its next tick and starts spending, wherever the operator
 * asked for it (the backlog).
 */
export function PromoteConfirm({
  issue,
  title,
  busy = false,
  error,
  onConfirm,
  onCancel,
}: {
  issue: number;
  title: string | null;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <ConfirmModal
      title="Promote to ai-ready"
      confirmLabel={`Promote #${issue}`}
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
      body={
        <>
          <p>Add the <span className="mono">ai-ready</span> label to <strong className="mono">#{issue}</strong>{title ? ` — ${title}` : ""}?</p>
          <p className="muted">The fixer picks up ai-ready issues on its next tick and will start spending.</p>
        </>
      }
    />
  );
}
