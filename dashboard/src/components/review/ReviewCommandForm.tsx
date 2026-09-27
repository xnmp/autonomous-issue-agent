import { useEffect, useId, useRef, useState } from "react";
import type { AvailableReviewControl, ReviewGate, ReviewGuidance } from "../../api/types";
import type { ReviewDraft } from "./model";

const LABELS: Record<ReviewDraft["operation"], string> = {
  request_rearm: "Request re-arm",
  continue_rearm: "Continue stopped re-arm",
  cancel_rearm: "Cancel pending re-arm",
  authorize_retry: "Authorize one retry",
};

/**
 * Every operator command that applies to one gate. Offered commands open a
 * form; refused ones stay visible but disabled, with the server's reason.
 */
export function ReviewCommandForm({ control, gate, disabled, onPrepare }: {
  control: AvailableReviewControl;
  gate: ReviewGate;
  disabled: boolean;
  onPrepare: (draft: ReviewDraft) => void;
}) {
  const id = useId();
  const container = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef(false);
  const [mode, setMode] = useState<ReviewDraft["operation"] | null>(null);
  const [guidance, setGuidance] = useState<ReviewGuidance["kind"]>("keep");
  const [text, setText] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!mode && restoreFocus.current) {
      container.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
      restoreFocus.current = false;
    }
  }, [mode]);
  if (gate.actions.length === 0) return null;
  const intent = control.current_intent;
  const selected = gate.actions.find(
    (action): action is Extract<ReviewGate["actions"][number], { state: "offered" }> =>
      action.state === "offered" && action.operation === mode,
  );
  const submit = () => {
    if (disabled || !selected) return;
    const operation = selected.operation;
    if (operation === "request_rearm") {
      if (guidance === "set" && !text.trim()) { setError("Enter guidance, or choose Keep or Clear."); return; }
      onPrepare({ operation, gateId: gate.gate_id, guidance: guidance === "set" ? { kind: "set", text } : { kind: guidance } });
    } else if (operation === "continue_rearm" && gate.cause.kind === "rearm_needs_decision") {
      onPrepare({ operation, gateId: gate.gate_id, stoppedRequestId: gate.cause.stopped_request_id });
    } else if (operation === "authorize_retry") {
      onPrepare({ operation, gateId: gate.gate_id });
    } else if (operation === "cancel_rearm" && intent) {
      if (!reason.trim()) { setError("Enter a reason for cancellation."); return; }
      onPrepare({ operation, gateId: gate.gate_id, requestId: intent.request_id, reason });
    }
  };
  return <div className="resolve-command-form" ref={container}>
    {!selected ? <div className="resolve-actions review-actions">{gate.actions.map((action) => action.state === "offered"
      ? <button key={action.operation} type="button" className="btn" disabled={disabled}
        onClick={() => { setMode(action.operation); setError(null); }}>{LABELS[action.operation]}</button>
      : <div key={action.operation} className="review-refused">
        <button type="button" className="btn" disabled aria-describedby={`${id}-${action.operation}-refused`}>{LABELS[action.operation]}</button>
        <p id={`${id}-${action.operation}-refused`} className="review-refusal">Unavailable: {action.reason}</p>
      </div>,
    )}</div> : <form onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <fieldset disabled={disabled}>
        <legend>{LABELS[selected.operation]}</legend>
        <p>{selected.effect}</p>
        {selected.operation === "request_rearm" && <>
          <label htmlFor={`${id}-guidance`}>Guidance for the next paid review</label>
          <select autoFocus id={`${id}-guidance`} value={guidance} onChange={(event) => {
            setGuidance(event.target.value as ReviewGuidance["kind"]); setError(null);
          }}>
            <option value="keep">Keep existing guidance</option>
            <option value="clear">Clear existing guidance</option>
            <option value="set">Set new guidance</option>
          </select>
          {guidance === "set" && <>
            <label htmlFor={`${id}-text`}>New guidance</label>
            <textarea id={`${id}-text`} value={text} maxLength={4000} rows={4}
              aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : undefined}
              onChange={(event) => { setText(event.target.value); setError(null); }} />
          </>}
        </>}
        {selected.operation === "authorize_retry" && gate.cause.kind === "retry_authorization_required" && <p>
          The evidence is the harness’s own read proving the effect absent
          (<span className="mono">{gate.cause.absence_evidence_id}</span>). A lost or unverified attempt needs a new
          authorization.
        </p>}
        {selected.operation === "cancel_rearm" && <>
          <label htmlFor={`${id}-reason`}>Reason for cancellation</label>
          <textarea autoFocus id={`${id}-reason`} value={reason} maxLength={16384} rows={3}
            aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : undefined}
            onChange={(event) => { setReason(event.target.value); setError(null); }} />
        </>}
        {error && <p id={`${id}-error`} role="alert" className="resolve-error">{error}</p>}
        <div className="resolve-actions">
          <button autoFocus={selected.operation === "continue_rearm" || selected.operation === "authorize_retry"} className="btn btn-primary" type="submit">Review command</button>
          <button className="btn btn-ghost" type="button" onClick={() => {
            restoreFocus.current = true; setMode(null); setError(null);
          }}>Close form</button>
        </div>
      </fieldset>
    </form>}
  </div>;
}
