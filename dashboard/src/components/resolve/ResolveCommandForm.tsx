import { useEffect, useId, useRef, useState } from "react";
import type { AvailableResolveControl, ResolveGate, ResolveGuidance } from "../../api/types";
import type { ResolveDraft } from "./model";

export function ResolveCommandForm({ control, gate, disabled, onPrepare }: {
  control: AvailableResolveControl;
  gate: ResolveGate;
  disabled: boolean;
  onPrepare: (draft: ResolveDraft) => void;
}) {
  const id = useId();
  const container = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef(false);
  const [mode, setMode] = useState<ResolveDraft["operation"] | null>(null);
  const [guidance, setGuidance] = useState<ResolveGuidance["kind"]>("keep");
  const [text, setText] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!mode && restoreFocus.current) {
      container.current?.querySelector("button")?.focus();
      restoreFocus.current = false;
    }
  }, [mode]);
  const intent = control.current_intent;
  const allowed = gate.available_commands.filter((operation) => {
    if (operation === "request_rearm") return intent?.state !== "pending";
    if (operation === "authorize_retry") return gate.cause.kind === "retry_authorization_required";
    if (!intent || intent.gate_id !== gate.gate_id) return false;
    return operation === "cancel_rearm" ? intent.state === "pending" : ["cancelled", "superseded"].includes(intent.state);
  });
  if (allowed.length === 0) return null;
  const labels = { request_rearm: "Request re-arm", continue_rearm: "Continue existing re-arm", cancel_rearm: "Cancel pending re-arm", authorize_retry: "Authorize one retry" };
  const selected = mode && allowed.includes(mode) ? mode : null;
  const submit = () => {
    if (disabled || !selected) return;
    if (selected === "request_rearm") {
      if (guidance === "set" && !text.trim()) { setError("Enter guidance, or choose Keep or Clear."); return; }
      onPrepare({ operation: selected, gateId: gate.gate_id, guidance: guidance === "set" ? { kind: "set", text } : { kind: guidance } });
    } else if (selected === "continue_rearm" && intent) {
      onPrepare({ operation: selected, gateId: gate.gate_id, stoppedRequestId: intent.request_id });
    } else if (selected === "authorize_retry") {
      onPrepare({ operation: selected, gateId: gate.gate_id });
    } else if (selected === "cancel_rearm" && intent) {
      if (!reason.trim()) { setError("Enter a reason for cancellation."); return; }
      onPrepare({ operation: selected, gateId: gate.gate_id, requestId: intent.request_id, reason });
    }
  };
  return <div className="resolve-command-form" ref={container}>
    {!selected ? <div className="resolve-actions">{allowed.map((operation) =>
      <button key={operation} type="button" className="btn" disabled={disabled} onClick={() => {
        setMode(operation); setError(null);
      }}>{labels[operation]}</button>,
    )}</div> : <form onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <fieldset disabled={disabled}>
        <legend>{labels[selected]}</legend>
        {selected === "request_rearm" && <>
          <p>Renew this resolve allowance once and prepare its workspace. Other holds remain independent.</p>
          <label htmlFor={`${id}-guidance`}>Guidance for the next resolve attempt</label>
          <select autoFocus id={`${id}-guidance`} value={guidance} onChange={(event) => {
            setGuidance(event.target.value as ResolveGuidance["kind"]); setError(null);
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
        {selected === "continue_rearm" && <p>
          Continue stopped request <span className="mono">{intent?.request_id}</span> using its existing episode and workspace handoff.
          This does not renew the allowance again.
        </p>}
        {selected === "authorize_retry" && gate.cause.kind === "retry_authorization_required" && <>
          <p>
            Permit exactly one more execution of existing command <span className="mono">{gate.cause.command_id}</span> under
            its current generation and episode. This does not renew the resolve allowance, create a re-arm, or reset its
            unresolved history.
          </p>
          <p>
            The evidence is the harness’s own current read proving the command’s effect absent
            (<span className="mono">{gate.cause.absence_evidence_id}</span>). The daemon reads again before writing; if the
            effect has appeared meanwhile, nothing is written. A lost or unverified attempt needs a new authorization.
          </p>
        </>}
        {selected === "cancel_rearm" && <>
          <p>Stop request <span className="mono">{intent?.request_id}</span>. Cancellation does not undo workspace preparation or remote effects, erase receipts, or refund allowance.</p>
          <label htmlFor={`${id}-reason`}>Reason for cancellation</label>
          <textarea autoFocus id={`${id}-reason`} value={reason} maxLength={16384} rows={3}
            aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : undefined}
            onChange={(event) => { setReason(event.target.value); setError(null); }} />
        </>}
        {error && <p id={`${id}-error`} role="alert" className="resolve-error">{error}</p>}
        <div className="resolve-actions">
          <button autoFocus={selected === "continue_rearm" || selected === "authorize_retry"} className="btn btn-primary" type="submit">Review command</button>
          <button className="btn btn-ghost" type="button" onClick={() => {
            restoreFocus.current = true; setMode(null); setError(null);
          }}>Close form</button>
        </div>
      </fieldset>
    </form>}
  </div>;
}
