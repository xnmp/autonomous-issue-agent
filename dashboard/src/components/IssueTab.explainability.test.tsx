import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { IssueTab } from "./IssueTab";
import {
  absentControl,
  availableControl,
  binding,
  ceilingGate,
  commandResult,
  controlResponse,
  inactiveControl,
  jsonResponse,
  OUTCOME_STATUS,
  rearm,
  unavailableControl,
} from "./resolve/test-fixtures";

const detail = {
  issue: 77,
  title: "Escalated issue",
  url: null,
  stage: "escalated",
  escalation_reason: "budget cap reached — approve more spend or close the issue",
  done: false,
  prs: [],
  pull_requests: [],
  cost_usd: 4,
  elapsed_s: 600,
  working_s: 300,
  idle_s: 300,
  idle_active: true,
  steps: [
    {
      idx: 0,
      kind: "REVIEW",
      role: "review",
      ts: "2026-08-01T07:10:00Z",
      state: "ok",
      status: "REQUEST_CHANGES",
      exit_reason: null,
      duration_s: 30,
      cost_usd: 1,
      estimated: false,
      model: "gpt-5.6-terra",
      effort: "medium",
      session_id: null,
      turns: null,
      usage: null,
      has_transcript: false,
      note: "REQUEST_CHANGES round 2",
    },
  ],
  events: [],
  warnings: [],
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("issue explainability", () => {
  it("banners why the issue was escalated and puts each task's note on its own line", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(detail)));

    render(<IssueTab repo="demo" issue={77} />);

    expect(
      await screen.findByText(/budget cap reached — approve more spend/),
    ).toBeVisible();
    expect(screen.getByText("REQUEST_CHANGES round 2")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(screen.getAllByText("REQUEST_CHANGES round 2")).toHaveLength(1);
    expect(screen.queryByText(/changed nothing visible/i)).not.toBeInTheDocument();
  });

  it("offers the legacy unblock only when resolve authority is witnessed inactive", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return json({ ok: true, detail: "unblocked" });
      if (String(input).includes("/resolve-control")) return controlResponse(inactiveControl);
      // The repository-scoped family read the server's legacy refusal also reads: none active.
      if (String(input).includes("/workflow-families")) return json({ status: "available", repository: "o/r", active_families: [] });
      return json(detail);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<IssueTab repo="demo" issue={77} />);

    await userEvent.click(await screen.findByRole("button", { name: "Review legacy unblock" }));
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit)?.method === "POST")).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Unblock #77" }));

    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit)?.method === "POST");
    expect(String(post?.[0])).toBe("/api/repos/demo/issues/77/unblock");
  });

  it.each([
    ["available", () => controlResponse(availableControl())],
    ["absent", () => controlResponse(absentControl)],
    ["unavailable", () => controlResponse(unavailableControl)],
    ["unreadable", () => new Response("not json", { status: 500 })],
  ])("never falls back to the legacy unblock when authority is %s", async (_label, reply) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/resolve-control") ? reply() : json(detail)));

    render(<IssueTab repo="demo" issue={77} />);

    expect(await screen.findByRole("heading", { name: /Resolve control/ })).toBeVisible();
    await screen.findByText(/No reset is available|Resolve held|Status unavailable/);
    expect(screen.queryByRole("button", { name: /legacy unblock/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unblock" })).not.toBeInTheDocument();
  });

  it("records a re-arm as pending intent, never as completed recovery", async () => {
    const pending = availableControl({
      gates: [ceilingGate({ state: "rearming", available_commands: ["cancel_rearm"] })],
      current_intent: rearm(),
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse(commandResult("accepted", pending, "re-arm recorded"), OUTCOME_STATUS.accepted);
      }
      if (String(input).includes("/resolve-control")) return controlResponse(availableControl());
      return json(detail);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<IssueTab repo="demo" issue={77} />);

    await userEvent.click(await screen.findByRole("button", { name: "Request re-arm" }));
    await userEvent.click(screen.getByRole("button", { name: "Review command" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm command" }));

    expect(await screen.findByRole("heading", { name: "Command recorded" })).toBeVisible();
    expect(screen.getByText(/not necessarily completed/)).toBeVisible();
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit)?.method === "POST");
    expect(String(post?.[0])).toBe("/api/repos/demo/issues/77/resolve-control/commands");
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toMatchObject({
      operation: "release",
      expected: binding,
      selection: { kind: "gate", gate_id: "gate-77", intent: { kind: "resume", guidance: { kind: "keep" } } },
    });
  });

});
