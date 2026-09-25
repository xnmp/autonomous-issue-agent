import { useEffect, useRef, useState } from "react";
import { api, RejectedRequest } from "../../api/client";
import { usePolled } from "../../api/hooks";
import type { ResolveCommand, ResolveControl } from "../../api/types";
import { operationLocksActions, prepareResolveCommand, requestIdProblem, sameBinding, type ResolveDraft, type ResolveOperation } from "./model";
import { acknowledgeRequest, recoverRequest, retainRequest } from "./retry-journal";

function restore(deployment: string, issue: number): ResolveOperation {
  try {
    return recoverRequest(sessionStorage, deployment, issue);
  } catch {
    return { kind: "blocked", reason: "Browser session storage is unavailable. Restore access and reload before sending a command." };
  }
}

function reviewStillCurrent(control: ResolveControl | null, readError: string | null, command: ResolveCommand): boolean {
  return !readError && control?.status === "available"
    && sameBinding(control.expected, command.expected);
}

const message = (error: unknown) => error instanceof Error ? error.message : String(error);

/** The parent keys this controller by deployment and issue; it never crosses scope. */
export function useResolveControl(deployment: string, issue: number) {
  const [operation, setOperation] = useState<ResolveOperation>(() => restore(deployment, issue));
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [readVersion, setReadVersion] = useState(0);
  const active = useRef(true);
  const inFlight = useRef<Promise<void> | null>(null);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const requestId = "command" in operation && operation.kind !== "review" ? operation.command.request_id : selectedId;
  const read = usePolled(
    JSON.stringify(["resolve-control", deployment, issue, requestId, readVersion]),
    (signal) => api.resolveControl(deployment, issue, requestId || undefined, signal),
    5_000, { cache: false },
  );

  // A key change (command, lookup, dismissal) starts an uncached read. Until it
  // lands, keep showing the last witnessed status so the panel, its focus and
  // its drafts survive; `loading` keeps every action locked on that stale view.
  const [witnessed, setWitnessed] = useState<ResolveControl | null>(null);
  if (read.data !== null && read.data !== witnessed) setWitnessed(read.data);

  // After a command the key changes so replies begun before it can never
  // become current; a manual refresh keeps the key (and the open drafts).
  const invalidate = () => setReadVersion((value) => value + 1);
  const prepare = (draft: ResolveDraft) => {
    if (inFlight.current || operationLocksActions(operation) || read.error || read.data?.status !== "available") return;
    try {
      const command = prepareResolveCommand(read.data, draft, crypto.randomUUID());
      setOperation({ kind: "review", command });
      setActionError(null);
    } catch (error) { setActionError(message(error)); }
  };

  const submit = () => {
    if (inFlight.current || (operation.kind !== "review" && operation.kind !== "uncertain")) return;
    const command: ResolveCommand = operation.command;
    // A reviewed command is bound to the status it was prepared from. An exact
    // retry is exempt: its identity must survive any later status change.
    if (operation.kind === "review" && !reviewStillCurrent(read.data, read.error, command)) {
      setOperation({ kind: "idle" });
      setActionError("The resolve status changed after this command was prepared. Review the current status and prepare it again.");
      return;
    }
    const pending = Promise.resolve().then(async () => {
      try {
        retainRequest(sessionStorage, deployment, command);
      } catch (error) {
        if (active.current) setActionError(`No request was sent. ${message(error)}`);
        return;
      }
      if (active.current) { setOperation({ kind: "sending", command }); setActionError(null); }
      try {
        const result = await api.resolveCommand(deployment, issue, command);
        if (result.outcome === "unavailable") {
          if (active.current) setOperation({ kind: "uncertain", command, reason: result.reason || "The request outcome is unavailable. Retry the same request." });
        } else {
          if (active.current) setOperation({ kind: "resolved", command, result });
          try {
            acknowledgeRequest(sessionStorage, deployment, command);
          } catch (storageError) {
            // The outcome is definite; a leftover record only replays on reload.
            if (active.current) setActionError(`The result is recorded, but the saved retry record could not be cleared. ${message(storageError)}`);
          }
        }
      } catch (error) {
        if (error instanceof RejectedRequest) {
          try {
            acknowledgeRequest(sessionStorage, deployment, command);
            if (active.current) { setOperation({ kind: "idle" }); setActionError(message(error)); }
          } catch (storageError) {
            if (active.current) setOperation({ kind: "uncertain", command, reason: message(storageError) });
          }
        } else if (active.current) setOperation({ kind: "uncertain", command, reason: message(error) });
      } finally {
        // Change the read key: pre-command replies cannot become current authority.
        if (active.current) invalidate();
      }
    });
    inFlight.current = pending;
    void pending.finally(() => { inFlight.current = null; });
  };

  return {
    control: read.data ?? witnessed, loading: read.loading, readError: read.error, actionError, operation,
    lookupId: selectedId,
    onPrepare: prepare, onConfirm: submit, onRetryCommand: submit, onRefresh: read.refresh,
    onDismiss: () => {
      if (inFlight.current || (operation.kind !== "review" && operation.kind !== "resolved")) return;
      setOperation({ kind: "idle" }); setActionError(null);
    },
    onHistoryLookup: (id: string) => {
      if (inFlight.current || operationLocksActions(operation) || requestIdProblem(id)) return;
      // Sent exactly as typed: the server treats padded identifiers as distinct.
      setOperation({ kind: "idle" }); setSelectedId(id); invalidate();
    },
    onClearLookup: () => {
      if (inFlight.current || operationLocksActions(operation)) return;
      setSelectedId(""); invalidate();
    },
  };
}
