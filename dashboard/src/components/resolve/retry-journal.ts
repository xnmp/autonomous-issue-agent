import type { ResolveCommand } from "../../api/types";
import { parseResolveCommand, type ResolveOperation } from "./model";

const PREFIX = "issue-agent.resolve-request.v1:";
const MAX_STORED_LENGTH = 262_144;

export function retryKey(deployment: string, issue: number): string {
  return PREFIX + JSON.stringify([deployment, issue]);
}

function decode(raw: string, issue: number): ResolveCommand {
  if (raw.length > MAX_STORED_LENGTH) throw new Error("Saved request is too large.");
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object" || !("version" in value) || value.version !== 1 || !("command" in value)) {
    throw new Error("Saved request format is not recognized.");
  }
  const command = parseResolveCommand(value.command);
  if (command.expected.issue !== issue) throw new Error("Saved request belongs to another issue.");
  return command;
}

export function recoverRequest(storage: Storage, deployment: string, issue: number): ResolveOperation {
  let raw: string | null;
  try {
    raw = storage.getItem(retryKey(deployment, issue));
  } catch {
    return { kind: "blocked", reason: "The browser could not read the saved request. No new command can be sent safely. Restore access to this tab’s session storage and reload." };
  }
  if (raw === null) return { kind: "idle" };
  try {
    return { kind: "uncertain", command: decode(raw, issue), reason: "A saved request has no confirmed response. Check its status or retry the exact same request." };
  } catch (error) {
    // Never delete it here: it may name a command the server already accepted.
    const detail = error instanceof SyntaxError ? "Saved request is not valid JSON." : error instanceof Error && !("issues" in error) ? error.message : "Saved request does not match the command contract.";
    return { kind: "blocked", reason: `${detail} No new command can be sent safely. Inspect this tab’s session storage entry before reloading.` };
  }
}

/** Key-order independent so a re-parsed record still matches its original. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sameCommand(left: ResolveCommand, right: ResolveCommand): boolean {
  return canonical(left) === canonical(right);
}

/** Persist before POST so navigation or a lost response cannot mint another ID. */
export function retainRequest(storage: Storage, deployment: string, command: ResolveCommand): void {
  const key = retryKey(deployment, command.expected.issue);
  const prior = storage.getItem(key);
  if (prior !== null && !sameCommand(decode(prior, command.expected.issue), command)) {
    throw new Error("Another saved request needs a response. Reload to inspect it before sending a new command.");
  }
  const raw = JSON.stringify({ version: 1, command });
  storage.setItem(key, raw);
  if (storage.getItem(key) !== raw) throw new Error("The browser could not preserve this request for safe retry.");
}

export function acknowledgeRequest(storage: Storage, deployment: string, command: ResolveCommand): void {
  const key = retryKey(deployment, command.expected.issue);
  const prior = storage.getItem(key);
  if (prior === null) return;
  if (!sameCommand(decode(prior, command.expected.issue), command)) throw new Error("Another saved request replaced this one. Reload to inspect it.");
  storage.removeItem(key);
  if (storage.getItem(key) !== null) throw new Error("The browser could not clear the acknowledged request.");
}
