import { useEffect, useId, useRef, useState } from "react";
import type { ResolveCommand, ResolveGate, ResolveRearm } from "../../api/types";
import { commandIdentity, operationLocksActions, requestIdProblem } from "./model";
import { ResolveCommandForm } from "./ResolveCommandForm";
import type { ResolveControlViewProps } from "./view-contract";
import "./resolve-control.css";
import { FixRecoveryView } from "./FixRecoveryView";

function GateCause({ cause }: { cause: ResolveGate["cause"] }) {
  switch (cause.kind) {
    case "resolve_ceiling": return <><strong>Resolve retry ceiling</strong><p>{cause.failures} counted failures / {cause.ceiling} allowed. {cause.last_failure}</p></>;
    case "remote_uncertain": return <><strong>Remote outcome uncertain</strong><p>{cause.last_error || "The command outcome has not been verified."} A read failure does not prove the effect is absent.</p><p className="mono">{cause.command_id}</p></>;
    case "retry_authorization_required": return <><strong>Retry authorization required</strong><p>The current postcondition is absent. A new write still requires explicit authorization.</p><p className="mono">{cause.command_id}</p></>;
    case "evidence_unavailable": return <><strong>Evidence unavailable</strong><p>{cause.source}: {cause.last_error}</p><p>{cause.failed_reads} failed reads; unresolved since <time>{cause.unresolved_since}</time>.</p></>;
    case "rearm_needs_decision": return <><strong>Re-arm needs a decision</strong><p>{cause.reason}</p><p>Stopped request <span className="mono">{cause.stopped_request_id}</span></p></>;
  }
}

function IntentDetails({ intent, title }: { intent: ResolveRearm; title: string }) {
  return <section className="resolve-intent" aria-label={title}>
    <h3>{title} <span className="resolve-state">{intent.state}</span></h3>
    <p className="mono">{intent.request_id}</p>
    {intent.reason && <p>{intent.reason}</p>}
    {intent.state === "pending" && <p>Recorded, not completed. Workspace verification and required remote effects may still be pending.</p>}
    <details><summary>Workspace and command evidence</summary>
      <dl className="resolve-facts">
        <dt>Episode</dt><dd className="mono">{intent.episode_id}</dd>
        <dt>Generation</dt><dd className="mono">{intent.generation}</dd>
        <dt>Original grant</dt><dd className="mono">{intent.grant_request_id}</dd>
        <dt>Workspace handoff</dt><dd className="mono">{intent.handoff_id}</dd>
        <dt>Branch</dt><dd className="mono">{intent.branch}</dd>
        <dt>Pinned base</dt><dd className="mono">{intent.base_ref}<br />{intent.base_commit}</dd>
      </dl>
      <p>These are recorded obligations, not a live workspace check.</p>
      {([ ["Required commands", intent.required_commands], ["Verified commands", intent.verified_commands], ["Retired commands", intent.retired_commands] ] as const).map(([label, commands]) =>
        <div key={label}><strong>{label}</strong>{commands.length ? <ul className="resolve-command-ids">{commands.map((id) => <li className="mono" key={id}>{id}</li>)}</ul> : <p>None recorded.</p>}</div>,
      )}
    </details>
  </section>;
}

function CommandDetails({ command }: { command: ResolveCommand }) {
  const scope = command.expected;
  return <>
    <dl className="resolve-facts">
      <dt>{command.operation === "cancel_rearm" ? "Cancellation ID" : command.operation === "authorize_retry" ? "Authorization ID" : "Request ID"}</dt><dd className="mono">{commandIdentity(command)}</dd>
      <dt>Repository / issue</dt><dd className="mono">{scope.repository} #{scope.issue}</dd>
      <dt>Family / epoch</dt><dd className="mono">{scope.family} / {scope.epoch}</dd>
      <dt>Generation / version</dt><dd className="mono">{scope.generation} / {scope.version}</dd>
      <dt>Episode</dt><dd className="mono">{scope.episode_id}</dd>
    </dl>
    {command.operation === "request_rearm" && <p>Guidance: {command.guidance.kind === "keep" ? "Keep existing guidance" : command.guidance.kind === "clear" ? "Clear existing guidance" : <span className="resolve-prose">{command.guidance.text}</span>}</p>}
    {command.operation === "continue_rearm" && <p>Continue <span className="mono">{command.stopped_request_id}</span>. No new allowance.</p>}
    {command.operation === "cancel_rearm" && <><p>Cancel request <span className="mono">{command.request_id}</span>.</p><p className="resolve-prose">{command.reason}</p></>}
    {command.operation === "authorize_retry" && <><p>Authorize one execution of command <span className="mono">{command.command_id}</span>. No allowance is renewed.</p><p>Absence evidence (the harness’s own read): <span className="mono">{command.evidence_id}</span></p></>}
  </>;
}

export function ResolveControlView(props: ResolveControlViewProps) {
  const { control, operation, loading, readError, actionError } = props;
  const id = useId();
  const operationHeading = useRef<HTMLHeadingElement>(null);
  const panelHeading = useRef<HTMLHeadingElement>(null);
  const operationRegion = useRef<HTMLDivElement>(null);
  const previousKind = useRef(operation.kind);
  const [historyId, setHistoryId] = useState("");
  const [historyError, setHistoryError] = useState<string | null>(null);
  // Every operation change unmounts the control the operator just used, so
  // focus must move explicitly rather than falling back to <body>.
  useEffect(() => {
    const previous = previousKind.current;
    previousKind.current = operation.kind;
    if (previous === operation.kind) return;
    // A reply settles on its own schedule: never pull focus away from wherever
    // the operator went while it was in flight.
    const focused = document.activeElement;
    if (previous === "sending" && focused && focused !== document.body && !operationRegion.current?.contains(focused)) return;
    if (operation.kind === "idle") panelHeading.current?.focus();
    else operationHeading.current?.focus();
  }, [operation.kind]);
  const locked = loading || Boolean(readError) || operationLocksActions(operation);
  return <section className="panel resolve-control" aria-labelledby={`${id}-title`}>
    <div className="panel-hd resolve-header">
      <h2 id={`${id}-title`} ref={panelHeading} tabIndex={-1}>Resolve control <span className="mono">#{props.issue}</span></h2>
      <button type="button" className="btn btn-ghost" disabled={loading} onClick={props.onRefresh}>{loading && control ? "Refreshing…" : "Refresh status"}</button>
    </div>
    <div className="panel-bd resolve-body">
      <div aria-live="polite" aria-atomic="true">
        {loading && !control && <p role="status">Loading resolve control…</p>}
        {readError && <p className="resolve-error">Status unavailable: {readError}. {control ? "Showing last known data, read-only." : "Refresh to try again."}</p>}
        {props.lookupId && (readError || (control && control.status !== "available")) && <p>The lookup of <span className="mono">{props.lookupId}</span> could not be shown. <button type="button" className="btn btn-ghost" disabled={operationLocksActions(operation)} onClick={props.onClearLookup}>Clear request lookup</button></p>}
        {control?.status === "unavailable" && <p className="resolve-error">Resolve control unavailable: {control.reason}. No reset is available. Refresh to try again.</p>}
        {control?.status === "inactive" && <p>Resolve authority is inactive for this deployment.</p>}
        {control?.status === "absent" && <p>No resolve workflow is recorded for this issue. No reset is available.</p>}
        {control?.status === "available" && <p className={control.workflow_held ? "resolve-held" : ""}>
          <strong>{control.workflow_held ? `Resolve held · ${control.gates.length} ${control.gates.length === 1 ? "hold" : "independent holds"}` : "No recorded workflow holds"}</strong>
          <span className="resolve-status-note">SQLite workflow state only. GitHub, budget and other checks still apply; this is not a readiness guarantee.</span>
        </p>}
      </div>
      {control?.status === "inactive" && props.legacyAllowed && !locked && <button className="btn" type="button" onClick={props.onLegacy}>Review legacy unblock</button>}
      {control?.status === "inactive" && props.legacyRefusal && <div className="resolve-legacy-refused">
        <button className="btn" type="button" disabled aria-describedby={`${id}-legacy-refused`}>Review legacy unblock</button>
        <p id={`${id}-legacy-refused`} className="resolve-status-note">{props.legacyRefusal}</p>
      </div>}
      {control && <FixRecoveryView recovery={control.fix_recovery} />}
      {control?.status === "available" && <>
        {control.gates.length > 0 && <ul className="resolve-gates">{control.gates.map((gate) => <li key={gate.gate_id}>
          <div className="resolve-gate-heading"><GateCause cause={gate.cause} /><span className="resolve-state">{gate.state}</span></div>
          {gate.pending_retry && <p className="resolve-pending" role="status">One retry authorized, waiting for the executor. <span className="mono">{gate.pending_retry.authorization_id}</span></p>}
          <details><summary>Hold evidence</summary><p className="mono">{gate.gate_id}</p><p>Opened in generation {gate.opened_generation}.</p>
            {gate.remediation.kind === "attempted" ? <><p>{gate.remediation.operation}: {gate.remediation.outcome}</p><ul>{gate.remediation.evidence_ids.map((evidence) => <li className="mono" key={evidence}>{evidence}</li>)}</ul></> : <p>No remediation recorded.</p>}
          </details>
          {/* Hidden rather than unmounted so a draft survives review and "Back to controls". */}
          <div hidden={operationLocksActions(operation)}><ResolveCommandForm key={`${gate.gate_id}:${control.expected.generation}:${control.expected.episode_id}`} control={control} gate={gate} disabled={locked} onPrepare={props.onPrepare} /></div>
        </li>)}</ul>}
        {control.current_intent && <IntentDetails intent={control.current_intent} title="Current re-arm request" />}
        <details className="resolve-history"><summary>Request history and guidance</summary>
          <p className="resolve-prose">{control.guidance || "No resolve guidance recorded."}</p>
          <form className="resolve-history-form" onSubmit={(event) => {
            event.preventDefault();
            const problem = requestIdProblem(historyId);
            if (problem) { setHistoryError(problem); return; }
            setHistoryError(null); props.onHistoryLookup(historyId);
          }}>
            <label htmlFor={`${id}-history`}>Look up request ID</label>
            <input id={`${id}-history`} className="mono" value={historyId} onChange={(event) => { setHistoryId(event.target.value); setHistoryError(null); }} aria-invalid={Boolean(historyError)} aria-describedby={historyError ? `${id}-history-error` : undefined} />
            <button className="btn" disabled={locked} type="submit">Look up request</button>
            {props.lookupId && <button className="btn btn-ghost" type="button" disabled={locked} onClick={props.onClearLookup}>Show current request only</button>}
            {historyError && <p id={`${id}-history-error`} className="resolve-error" role="alert">{historyError}</p>}
          </form>
        </details>
        {control.requested_id && (control.requested_intent ? <IntentDetails intent={control.requested_intent} title="Requested historical re-arm" /> : <p>No request found for <span className="mono">{control.requested_id}</span>.</p>)}
        {control.observation && <details><summary>Recovery observations</summary><p>{control.observation.last_error || "No observation error recorded."}</p><p>{control.observation.failed_reads} failed reads; next read <time>{control.observation.next_read_at}</time>.</p></details>}
      </>}
      {actionError && <p role="alert" className="resolve-error">{actionError}</p>}
      {operation.kind !== "idle" && <div className="resolve-operation" ref={operationRegion}>
        <h3 ref={operationHeading} tabIndex={-1}>{operation.kind === "review" ? "Review resolve command" : operation.kind === "sending" ? "Sending resolve command…" : operation.kind === "uncertain" ? "Command outcome not confirmed" : operation.kind === "blocked" ? "Resolve actions unavailable" : ["accepted", "replayed"].includes(operation.result.outcome) ? "Command recorded" : "Command not accepted"}</h3>
        {operation.kind !== "blocked" && <CommandDetails command={operation.command} />}
        {operation.kind === "review" && <><p>This records durable intent. Acceptance does not mean the re-arm has completed or that paid work can start.</p><div className="resolve-actions"><button type="button" className="btn btn-primary" disabled={Boolean(readError) || loading || control?.status !== "available"} onClick={props.onConfirm}>Confirm command</button><button type="button" className="btn btn-ghost" onClick={props.onDismiss}>Back to controls</button></div></>}
        {operation.kind === "sending" && <p role="status">Waiting for the server. Do not submit another command.</p>}
        {operation.kind === "uncertain" && <><p role="alert">{operation.reason}</p><p>The request may have been accepted. Retry only this exact command and ID; do not create a replacement request.</p><button type="button" className="btn" onClick={props.onRetryCommand}>Retry same command</button></>}
        {operation.kind === "blocked" && <p role="alert">{operation.reason}</p>}
        {operation.kind === "resolved" && <><p role="status">{operation.result.outcome === "replayed" ? "Original command replayed. " : ""}{operation.result.reason}{["accepted", "replayed"].includes(operation.result.outcome) ? " Durable intent is recorded, not necessarily completed. Check the request state above." : " Refresh status before preparing another command."}</p><button type="button" className="btn btn-ghost" onClick={props.onDismiss}>Close result</button></>}
      </div>}
    </div>
  </section>;
}
