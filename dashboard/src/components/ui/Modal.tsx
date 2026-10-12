import { useEffect, useRef, type ReactNode } from "react";

interface ConfirmModalProps {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * In-app confirm dialog. `window.confirm`/`alert` are forbidden — they block
 * the poll loop, cannot be themed, and are unstyleable in a kiosk dashboard.
 *
 * Focus is moved to the confirm button once, on open (a parent re-render must
 * not pull focus back from Cancel), Escape cancels, and a click on the
 * backdrop cancels; the panel itself stops propagation.
 */
export function ConfirmModal({
  title,
  body,
  confirmLabel,
  danger,
  busy,
  error,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;

  useEffect(() => {
    confirmRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") cancelRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="modal-backdrop" onClick={onCancel} role="presentation">
      <div
        className="modal panel"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="panel-hd">
          <span className="panel-title">{title}</span>
        </div>
        <div className="panel-bd modal-body">{body}</div>
        {error ? <div className="modal-error">{error}</div> : null}
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            ref={confirmRef}
            className={`btn ${danger ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
