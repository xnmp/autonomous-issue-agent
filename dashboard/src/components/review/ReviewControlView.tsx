import { useEffect, useId, useRef, useState } from "react";
import type {
  AvailableReviewControl, ReviewCommand, ReviewDaemonDecision, ReviewDispatch, ReviewGate, ReviewGateCause, ReviewRearm,
} from "../../api/types";
import { commandIdentity, operationLocksActions, requestIdProblem } from "./model";
import { ReviewCommandForm } from "./ReviewCommandForm";
import type { ReviewControlViewProps } from "./view-contract";
import "../resolve/resolve-control.css";
import "./review-control.css";

const STOP_REASONS: Record<string, string> = {
  target_terminal: "the pull request was closed or merged",
  decision_changed: "the head, labels or holds it was decided against changed",
  required_effect_rejected: "GitHub refused one of its required effects",
  operator_cancelled: "an operator cancelled it",
  ceiling_reached_again: "reviews outside its authorization reached the ceiling again",
};

const EVIDENCE: Record<Extract<ReviewGateCause, { kind: "evidence_unavailable" }>["evidence"], string> = {
  exemption_trailers: "Commit-trailer exemptions are unreadable",
  review_history: "The complete review history is unreadable",
  prewrite_read: "A command’s pre-write read is unavailable",
  rearm_observation: "The pending re-arm’s reconciliation reads keep failing",
  other: "Required evidence is unavailable",
};

const COMMAND_STATUS: Record<string, string> = {
  pending: "pending",
  verified: "verified",
  refused: "refused by its guard",
  uncertain: "outcome uncertain",
  held: "held behind its own gate",
  unavailable: "progress unreadable (its recorded facts disagree)",
};

const CAUSE_NAMES: Record<Extract<ReviewGateCause, { kind: "facts_unavailable" }>["underlying"], string> = {
  review_rejection_ceiling: "review rejection ceiling",
  remote_uncertain: "remote outcome",
  retry_authorization_required: "retry authorization",
  evidence_unavailable: "evidence",
  rearm_needs_decision: "stopped re-arm",
  review_projection_refused: "refused relabel",
};

const READS: Record<Extract<ReviewDaemonDecision, { state: "recorded" }>["read"], string> = {
  review_read: "its review read of trigger-labelled pull requests",
  departed_read: "its own read of this pull request after it left the review read",
};

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
const short = (sha: string) => sha.slice(0, 12);

function GateCause({ cause }: { cause: ReviewGateCause }) {
  switch (cause.kind) {
    case "review_rejection_ceiling": return <>
      <strong>Review rejection ceiling</strong>
      <p>{plural(cause.counted_total, "counted bot rejection")} / {cause.ceiling} allowed, by <span className="mono">{cause.review_bot}</span> at head <span className="mono">{short(cause.head_sha)}</span>.</p>
      {cause.hold.state === "contested"
        ? <p className="review-contested" role="note"><strong>Hold contested.</strong> The needs-human-review hold was removed again after {plural(cause.hold.restores, "restoration")} (limit {cause.hold.restore_limit}); the harness has stopped restoring it. This gate alone still holds every review until it is re-armed or its reviews are dismissed.</p>
        : cause.hold.state === "unreadable"
          ? <p className="resolve-error">The harness’s hold-restoration record is unreadable ({cause.hold.reason}); restorations cannot be shown. The gate and its command are unaffected.</p>
          : cause.hold.restores > 0 && <p>The harness restored a removed hold {plural(cause.hold.restores, "time")} of at most {cause.hold.restore_limit}.</p>}
      <details><summary>Counted reviews ({cause.counted_total})</summary>
        <ol className="review-counted">{cause.counted.map((review) => <li key={review.review_id}>
          <span className="mono">{review.review_id}</span> by <span className="mono">{review.author}</span> at <span className="mono">{review.commit_id ? short(review.commit_id) : "unknown commit"}</span>
        </li>)}</ol>
        {cause.counted_total > cause.authorization_bound && <p>One re-arm can dismiss at most {cause.authorization_bound} reviews.</p>}
        {cause.non_resetting_commits.length > 0 && <p>Commits that did not reset the count: {cause.non_resetting_commits.map((sha) => <span key={sha} className="mono">{short(sha)} </span>)}</p>}
      </details>
    </>;
    case "remote_uncertain": return <><strong>Remote outcome uncertain</strong><p>{cause.last_error || "The command outcome has not been verified."} A read failure does not prove the effect is absent; reconciliation continues on a bounded schedule.</p><p className="mono">{cause.command_id}</p></>;
    case "retry_authorization_required": return <><strong>Retry authorization required</strong><p>The harness proved the effect absent. A new write still requires explicit authorization.</p><p className="mono">{cause.command_id}</p></>;
    case "evidence_unavailable": return <><strong>{EVIDENCE[cause.evidence]}</strong><p>{cause.source}: {cause.last_error}</p><p>{plural(cause.failed_reads, "failed read")}; unresolved since <time>{cause.unresolved_since}</time>. The hold ends when a later read succeeds.</p></>;
    case "rearm_needs_decision": return <><strong>Re-arm needs a decision</strong><p>Request <span className="mono">{cause.stopped_request_id}</span> stopped because {STOP_REASONS[cause.reason] ?? cause.reason}. {plural(cause.obligations.length, "obligation")} did not complete.</p></>;
    case "review_projection_refused": return cause.projection === "escalation_hold"
      ? <><strong>Escalation hold exhausted</strong><p>The harness spent its bounded re-plans applying a verdict escalation’s hold: {cause.reason}. It will not try again. Apply the planned labels by hand (the next read verifies them) or push a new head, which makes the escalation moot.</p></>
      : <><strong>Ceiling release relabel refused</strong><p>{cause.reason}. Correct the labels on GitHub; the next harness read verifies the release. A new generation supersedes this hold.</p></>;
    case "facts_unavailable": return <><strong>Hold facts unreadable</strong><p>The harness’s own records for this {CAUSE_NAMES[cause.underlying]} hold disagree: {cause.reason}. The hold stays in force and reconciliation continues; no command is offered on it until its records agree. Other holds are unaffected.</p></>;
  }
}

function Dispatch({ dispatch }: { dispatch: ReviewDispatch }) {
  switch (dispatch.kind) {
    case "terminal": return dispatch.state === "merged"
      ? <>The review daemon found this pull request merged: it dispatches no review, and no command applies.</>
      : <>The review daemon found this pull request closed: it dispatches no review while it stays closed. Its commands stay available for a reopen; accepting a re-arm, a continuation or a retry grant re-reads GitHub and refuses while it stays closed.</>;
    case "not_admitted": return <>Review dispatch leaves this pull request alone for holds outside the review workflow: {dispatch.holds.join("; ")}. No review-control command changes them.</>;
    case "deferred": return <>The review daemon has not decided: {dispatch.reason}.</>;
    case "held": return <>Review dispatch is held by {plural(dispatch.gate_ids.length, "gate")}.</>;
    case "submission_unresolved": return <>Review dispatch waits for {plural(dispatch.command_ids.length, "review submission")} whose outcome is not yet settled.</>;
    case "release_projection_pending": return <>Review dispatch waits for the ceiling release relabel to be verified.</>;
    case "owed": return <>{dispatch.review === "initial" ? "An initial review" : "A fresh review"} is owed and passed this tick’s dispatch admission; paid admission (the spend cap) still decides when it runs.</>;
    case "withheld": return <>{dispatch.review === "initial" ? "An initial review" : "A fresh review"} is owed, withheld by this tick’s dispatch admission: {WITHHELD[dispatch.reason]} ({dispatch.detail}). Each tick decides again.</>;
    case "in_flight": return <>{dispatch.review === "initial" ? "An initial review" : "A fresh review"} is running now: this pull request’s own dispatch still holds its reservation, so no second review is bought.</>;
    case "not_owed": return <>No review is owed: {dispatch.reason}.</>;
  }
}

const WITHHELD: Record<Extract<ReviewDispatch, { kind: "withheld" }>["reason"], string> = {
  fix_claim: "a fix run holds this pull request",
  reserved: "its dispatch target is already reserved",
};

/** The review daemon's own recorded decision, never re-derived here; its time says how old it is. */
function Daemon({ daemon }: { daemon: ReviewDaemonDecision }) {
  switch (daemon.state) {
    case "unrecorded": return <>The review daemon has not recorded a dispatch decision for this pull request yet.</>;
    case "unreadable": return <>The review daemon’s recorded decision is unreadable ({daemon.reason}); dispatch is not shown.</>;
    case "recorded": return <>Review daemon, as of <time dateTime={daemon.as_of}>{daemon.as_of}</time>: <Dispatch dispatch={daemon.dispatch} /></>;
  }
}

function DaemonFacts({ daemon }: { daemon: ReviewDaemonDecision }) {
  if (daemon.state !== "recorded") return <p>No review daemon read is recorded.</p>;
  const facts = daemon.pull_request;
  return <p>Decided at <time dateTime={daemon.decided_at}>{daemon.decided_at}</time> from {READS[daemon.read]}, last confirmed unchanged at <time dateTime={daemon.as_of}>{daemon.as_of}</time>. {facts
    ? <>It read the pull request {facts.state} at head <span className="mono">{short(facts.head_sha)}</span>, labels {facts.labels.length ? facts.labels.join(", ") : "none"}{facts.external_holds.length ? <>, with holds a re-arm keeps: {facts.external_holds.join("; ")}</> : null}.</>
    : <>Its read of the pull request failed, so no facts are recorded.</>}{daemon.read_failure
    ? <> <ReadFailure failure={daemon.read_failure} kept={facts !== null} /></>
    : null} This is the review daemon’s own read, not a live GitHub check.</p>;
}

function ReadFailure({ failure, kept }: {
  failure: NonNullable<Extract<ReviewDaemonDecision, { state: "recorded" }>["read_failure"]>; kept: boolean;
}) {
  return <>Its own read has failed {plural(failure.failed_reads, "consecutive time")} since <time dateTime={failure.first_failed_at}>{failure.first_failed_at}</time> (latest at <time dateTime={failure.last_failed_at}>{failure.last_failed_at}</time>: {failure.reason}){kept ? "; the facts above are from its last successful read" : ""}. Retries back off to at most one every six hours.</>;
}

function Obligation({ command }: { command: Extract<ReviewRearm, { state: "pending" }>["commands"][number] }) {
  const obligation = command.obligation;
  const label = obligation.kind === "dismiss_review" ? <>Dismiss counted review <span className="mono">{obligation.review_id}</span></>
    : obligation.kind === "projection" ? <>Relabel: {obligation.remove ? <>remove <span className="mono">{obligation.remove}</span>, </> : null}add <span className="mono">{obligation.add}</span></>
      : <>Recorded obligation</>;
  return <li><span>{label}</span> <span className={`review-command-status review-command-${command.status}`}>{COMMAND_STATUS[command.status] ?? command.status}</span>
    <br /><span className="mono review-command-id">{command.command_id}</span></li>;
}

function IntentDetails({ intent, title }: { intent: ReviewRearm; title: string }) {
  return <section className="resolve-intent" aria-label={title}>
    <h3>{title} <span className="resolve-state">{intent.state}</span></h3>
    <p className="mono">{intent.request_id}</p>
    {intent.state === "pending" && <>
      <p>Recorded, not completed. The review daemon executes each obligation; the pull request returns to review once every one is verified.</p>
      <ul className="review-obligations" aria-label="Re-arm obligations">{intent.commands.map((command) => <Obligation key={command.command_id} command={command} />)}</ul>
    </>}
    {intent.state === "completed" && <p>Every obligation is verified; the workflow owes a fresh review for generation {intent.generation}. Whether it is dispatched is the review daemon’s decision shown above.</p>}
    {(intent.state === "cancelled" || intent.state === "superseded") && <p>Stopped because {STOP_REASONS[intent.reason] ?? intent.reason}. Verified effects remain; retired obligations never execute.</p>}
    <details><summary>Authorization and command evidence</summary>
      <dl className="resolve-facts">
        <dt>Gate</dt><dd className="mono">{intent.gate_id}</dd>
        <dt>Generation</dt><dd className="mono">{intent.generation}</dd>
      </dl>
      {"authorized_reviews" in intent && <div><strong>Authorized reviews</strong><ul className="resolve-command-ids">{intent.authorized_reviews.map((id) => <li className="mono" key={id}>{id}</li>)}</ul></div>}
      {"verified_commands" in intent && <div><strong>Verified commands</strong>{intent.verified_commands.length ? <ul className="resolve-command-ids">{intent.verified_commands.map((id) => <li className="mono" key={id}>{id}</li>)}</ul> : <p>None recorded.</p>}</div>}
      {"retired_commands" in intent && <div><strong>Retired commands</strong>{intent.retired_commands.length ? <ul className="resolve-command-ids">{intent.retired_commands.map((id) => <li className="mono" key={id}>{id}</li>)}</ul> : <p>None recorded.</p>}</div>}
    </details>
  </section>;
}

function Continuation({ control }: { control: AvailableReviewControl }) {
  const continuation = control.continuation;
  if (continuation.kind === "fresh_review_required") {
    return <p>A fresh review is required (owed since generation {continuation.owed_since_generation}).</p>;
  }
  return <p>Reviewed baseline: head <span className="mono">{short(continuation.head_sha)}</span>, generation {continuation.reviewed_generation}, from {continuation.source.kind === "bound_submission"
    ? <>harness submission <span className="mono">{continuation.source.command_id}</span></>
    : <>imported evidence <span className="mono">{continuation.source.evidence_id}</span></>}.</p>;
}

function CommandDetails({ command }: { command: ReviewCommand }) {
  const scope = command.expected;
  return <>
    <dl className="resolve-facts">
      <dt>{command.operation === "cancel_rearm" ? "Cancellation ID" : command.operation === "authorize_retry" ? "Authorization ID" : "Request ID"}</dt><dd className="mono">{commandIdentity(command)}</dd>
      <dt>Repository / pull request</dt><dd className="mono">{scope.repository} #{scope.pull_request}</dd>
      <dt>Family / epoch</dt><dd className="mono">{scope.family} / {scope.epoch}</dd>
      <dt>Generation / version</dt><dd className="mono">{scope.generation} / {scope.version}</dd>
      <dt>Episode</dt><dd className="mono">{scope.episode_id}</dd>
    </dl>
    {command.operation === "request_rearm" && <p>Guidance: {command.guidance.kind === "keep" ? "Keep existing guidance" : command.guidance.kind === "clear" ? "Clear existing guidance" : <span className="resolve-prose">{command.guidance.text}</span>}</p>}
    {command.operation === "continue_rearm" && <p>Continue <span className="mono">{command.stopped_request_id}</span>. No new authorization.</p>}
    {command.operation === "cancel_rearm" && <><p>Cancel request <span className="mono">{command.request_id}</span>.</p><p className="resolve-prose">{command.reason}</p></>}
    {command.operation === "authorize_retry" && <><p>Authorize one execution of command <span className="mono">{command.command_id}</span>.</p><p>Absence evidence (the harness’s own read): <span className="mono">{command.evidence_id}</span></p></>}
  </>;
}

function statusHeading(control: AvailableReviewControl): string {
  if (!control.workflow_held) return "No recorded review holds";
  return `Review held · ${control.gates.length} ${control.gates.length === 1 ? "hold" : "independent holds"}`;
}

export function ReviewControlView(props: ReviewControlViewProps) {
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
    const focused = document.activeElement;
    if (previous === "sending" && focused && focused !== document.body && !operationRegion.current?.contains(focused)) return;
    if (operation.kind === "idle") panelHeading.current?.focus();
    else operationHeading.current?.focus();
  }, [operation.kind]);
  const locked = loading || Boolean(readError) || operationLocksActions(operation);
  return <section className="panel resolve-control review-control" aria-labelledby={`${id}-title`}>
    <div className="panel-hd resolve-header">
      <h2 id={`${id}-title`} ref={panelHeading} tabIndex={-1}>Review control <span className="mono">PR #{props.pr}</span></h2>
      <button type="button" className="btn btn-ghost" disabled={loading} onClick={props.onRefresh}>{loading && control ? "Refreshing…" : "Refresh status"}</button>
    </div>
    <div className="panel-bd resolve-body">
      <div aria-live="polite" aria-atomic="true">
        {loading && !control && <p role="status">Loading review control…</p>}
        {readError && <p className="resolve-error">Status unavailable: {readError}. {control ? "Showing last known data, read-only." : "Refresh to try again."}</p>}
        {props.lookupId && (readError || (control && control.status !== "available")) && <p>The lookup of <span className="mono">{props.lookupId}</span> could not be shown. <button type="button" className="btn btn-ghost" disabled={operationLocksActions(operation)} onClick={props.onClearLookup}>Clear request lookup</button></p>}
        {control?.status === "unavailable" && <p className="resolve-error">Review control unavailable: {control.reason}. No reset is available. Refresh to try again.</p>}
        {control?.status === "migration_required" && <p className="resolve-held"><strong>Review rejection control is not activated.</strong> Reviews and review re-arms require activation of the review family. Inventory and inspect the activation plan before activating resolve and review control together.</p>}
        {control?.status === "absent" && <p>The review daemon has not recorded a review workflow for this pull request, so it holds no review-rejection gate and there is nothing to re-arm here. A label on GitHub is not such a hold; the daemon records the pull request when its review read first returns it.</p>}
        {control?.status === "available" && <p className={control.workflow_held ? "resolve-held" : ""}>
          <strong>{statusHeading(control)}</strong>
          <span className="resolve-status-note"><Daemon daemon={control.daemon} /> Acceptance re-reads GitHub.</span>
        </p>}
      </div>
      {control?.status === "available" && <>
        {control.gates.length > 0 && <ul className="resolve-gates">{control.gates.map((gate: ReviewGate) => <li key={gate.gate_id}>
          <div className="resolve-gate-heading"><GateCause cause={gate.cause} /><span className="resolve-state">{gate.state}</span></div>
          {gate.pending_retry && <p className="resolve-pending" role="status">One retry authorized, waiting for the executor. <span className="mono">{gate.pending_retry.authorization_id}</span></p>}
          {gate.actions.length === 0 && <p className="review-no-lever">No operator command applies: this hold ends through harness reconciliation or a corrective change on GitHub.</p>}
          <details><summary>Hold evidence</summary><p className="mono">{gate.gate_id}</p><p>Opened in generation {gate.opened_generation}.</p>
            {gate.remediation.kind === "attempted" ? <><p>{gate.remediation.operation}: {gate.remediation.outcome}</p><ul>{gate.remediation.evidence_ids.map((evidence) => <li className="mono" key={evidence}>{evidence}</li>)}</ul></> : <p>{gate.remediation.reason}</p>}
          </details>
          {/* Hidden rather than unmounted so a draft survives review and "Back to controls". */}
          <div hidden={operationLocksActions(operation)}><ReviewCommandForm key={`${gate.gate_id}:${control.expected.generation}:${control.expected.episode_id}`} control={control} gate={gate} disabled={locked} onPrepare={props.onPrepare} /></div>
        </li>)}</ul>}
        {control.current_intent && <IntentDetails intent={control.current_intent} title="Current re-arm request" />}
        <details className="resolve-history"><summary>Review baseline, observation, history and guidance</summary>
          <Continuation control={control} />
          <DaemonFacts daemon={control.daemon} />
          <p className="resolve-prose">{control.guidance || "No review guidance recorded."}</p>
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
        {control.requested_id && control.requested_id !== control.current_intent?.request_id && (control.requested_intent
          ? <IntentDetails intent={control.requested_intent} title="Requested historical re-arm" />
          : <p>No request found for <span className="mono">{control.requested_id}</span>.</p>)}
        {control.observation && <details><summary>Re-arm reconciliation reads</summary><p>{control.observation.last_error || "No read error recorded."}</p><p>{plural(control.observation.failed_reads, "failed read")}; next read <time>{control.observation.next_read_at}</time>.</p></details>}
      </>}
      {actionError && <p role="alert" className="resolve-error">{actionError}</p>}
      {operation.kind !== "idle" && <div className="resolve-operation" ref={operationRegion}>
        <h3 ref={operationHeading} tabIndex={-1}>{operation.kind === "review" ? "Review command" : operation.kind === "sending" ? "Sending review command…" : operation.kind === "uncertain" ? "Command outcome not confirmed" : operation.kind === "blocked" ? "Review actions unavailable" : ["accepted", "replayed"].includes(operation.result.outcome) ? "Command recorded" : "Command not accepted"}</h3>
        {operation.kind !== "blocked" && <CommandDetails command={operation.command} />}
        {operation.kind === "review" && <><p>This records durable intent. Acceptance does not mean the re-arm has completed or that a review has been bought.</p><div className="resolve-actions"><button type="button" className="btn btn-primary" disabled={Boolean(readError) || loading || control?.status !== "available"} onClick={props.onConfirm}>Confirm command</button><button type="button" className="btn btn-ghost" onClick={props.onDismiss}>Back to controls</button></div></>}
        {operation.kind === "sending" && <p role="status">Waiting for the server. Do not submit another command.</p>}
        {operation.kind === "uncertain" && <><p role="alert">{operation.reason}</p><p>The request may have been accepted. Retry only this exact command and ID; do not create a replacement request.</p><button type="button" className="btn" onClick={props.onRetryCommand}>Retry same command</button></>}
        {operation.kind === "blocked" && <p role="alert">{operation.reason}</p>}
        {operation.kind === "resolved" && <><p role="status">{operation.result.outcome === "replayed" ? "Original command replayed. " : ""}{operation.result.reason}{["accepted", "replayed"].includes(operation.result.outcome) ? " Durable intent is recorded, not necessarily completed. Check the request state above." : " Refresh status before preparing another command."}</p><button type="button" className="btn btn-ghost" onClick={props.onDismiss}>Close result</button></>}
      </div>}
    </div>
  </section>;
}
