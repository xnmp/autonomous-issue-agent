/**
 * The per-tab retry journal every operator control shares (resolve and review).
 *
 * A command is persisted before its POST so navigation or a lost response can
 * never mint a second identity for the same decision; it is removed only once
 * the server's definite outcome is known. A record that cannot be read or
 * decoded blocks new commands rather than being discarded, because it may name
 * a command the server already accepted.
 */

const MAX_STORED_LENGTH = 262_144;

export type RecoveredRequest<C> =
  | { kind: "idle" }
  | { kind: "uncertain"; command: C; reason: string }
  | { kind: "blocked"; reason: string };

export interface JournalScope<C> {
  /** Storage namespace, versioned with the command contract. */
  prefix: string;
  /** The generated contract parser; throws on any mismatch. */
  parse: (value: unknown) => C;
  /** The issue or pull request number a command is bound to. */
  target: (command: C) => number;
  /** How the target is named in messages ("issue", "pull request"). */
  noun: string;
}

/** Key-order independent so a re-parsed record still matches its original. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function createRetryJournal<C>(scope: JournalScope<C>) {
  const key = (deployment: string, target: number) => scope.prefix + JSON.stringify([deployment, target]);

  const decode = (raw: string, target: number): C => {
    if (raw.length > MAX_STORED_LENGTH) throw new Error("Saved request is too large.");
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || !("version" in value) || value.version !== 1 || !("command" in value)) {
      throw new Error("Saved request format is not recognized.");
    }
    const command = scope.parse(value.command);
    if (scope.target(command) !== target) throw new Error(`Saved request belongs to another ${scope.noun}.`);
    return command;
  };

  const same = (left: C, right: C) => canonical(left) === canonical(right);

  return {
    key,

    recover(storage: Storage, deployment: string, target: number): RecoveredRequest<C> {
      let raw: string | null;
      try {
        raw = storage.getItem(key(deployment, target));
      } catch {
        return { kind: "blocked", reason: "The browser could not read the saved request. No new command can be sent safely. Restore access to this tab’s session storage and reload." };
      }
      if (raw === null) return { kind: "idle" };
      try {
        return { kind: "uncertain", command: decode(raw, target), reason: "A saved request has no confirmed response. Check its status or retry the exact same request." };
      } catch (error) {
        // Never delete it here: it may name a command the server already accepted.
        const detail = error instanceof SyntaxError ? "Saved request is not valid JSON." : error instanceof Error && !("issues" in error) ? error.message : "Saved request does not match the command contract.";
        return { kind: "blocked", reason: `${detail} No new command can be sent safely. Inspect this tab’s session storage entry before reloading.` };
      }
    },

    /** Persist before POST so navigation or a lost response cannot mint another ID. */
    retain(storage: Storage, deployment: string, command: C): void {
      const target = scope.target(command);
      const stored = key(deployment, target);
      const prior = storage.getItem(stored);
      if (prior !== null && !same(decode(prior, target), command)) {
        throw new Error("Another saved request needs a response. Reload to inspect it before sending a new command.");
      }
      const raw = JSON.stringify({ version: 1, command });
      storage.setItem(stored, raw);
      if (storage.getItem(stored) !== raw) throw new Error("The browser could not preserve this request for safe retry.");
    },

    acknowledge(storage: Storage, deployment: string, command: C): void {
      const target = scope.target(command);
      const stored = key(deployment, target);
      const prior = storage.getItem(stored);
      if (prior === null) return;
      if (!same(decode(prior, target), command)) throw new Error("Another saved request replaced this one. Reload to inspect it.");
      storage.removeItem(stored);
      if (storage.getItem(stored) !== null) throw new Error("The browser could not clear the acknowledged request.");
    },
  };
}
