import { describe, expect, it } from "vitest";
import { schemas } from "./generated-zod";

describe("generated resolve contracts", () => {
  it("preserves literal discriminators across OpenAPI generation", () => {
    const command = {
      operation: "request_rearm",
      expected: {
        repository: "o/r", issue: 7, family: "resolve", epoch: 1,
        generation: 2, version: 3, episode_id: "episode-7",
      },
      request_id: "request-7", gate_id: "gate-7", guidance: { kind: "keep" },
    };
    expect(schemas.RequestRearm.safeParse(command).success).toBe(true);
    expect(schemas.RequestRearm.safeParse({ ...command, operation: "cancel_rearm" }).success).toBe(false);
    expect(schemas.RequestRearm.safeParse({ ...command, guidance: { kind: "invented" } }).success).toBe(false);
    expect(schemas.RequestRearm.safeParse({ ...command, expected: { ...command.expected, family: "review" } }).success).toBe(false);
  });

  it("validates nested response variants instead of crashing during import", () => {
    const result = {
      outcome: "unavailable", reason: "authority unavailable",
      control: { status: "unavailable", reason: "authority unavailable" },
    };
    expect(schemas.CommandResult.safeParse(result).success).toBe(true);
    expect(schemas.CommandResult.safeParse({ ...result, control: { status: "available" } }).success).toBe(false);
  });
});
