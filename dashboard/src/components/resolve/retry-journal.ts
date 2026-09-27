import type { ResolveCommand } from "../../api/types";
import { createRetryJournal } from "../../lib/retry-journal";
import { parseResolveCommand, type ResolveOperation } from "./model";

const journal = createRetryJournal<ResolveCommand>({
  prefix: "issue-agent.resolve-request.v1:",
  parse: parseResolveCommand,
  target: (command) => command.expected.issue,
  noun: "issue",
});

export function retryKey(deployment: string, issue: number): string {
  return journal.key(deployment, issue);
}

export function recoverRequest(storage: Storage, deployment: string, issue: number): ResolveOperation {
  return journal.recover(storage, deployment, issue);
}

/** Persist before POST so navigation or a lost response cannot mint another ID. */
export function retainRequest(storage: Storage, deployment: string, command: ResolveCommand): void {
  journal.retain(storage, deployment, command);
}

export function acknowledgeRequest(storage: Storage, deployment: string, command: ResolveCommand): void {
  journal.acknowledge(storage, deployment, command);
}
