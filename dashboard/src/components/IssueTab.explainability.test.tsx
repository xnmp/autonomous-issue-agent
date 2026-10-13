import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { IssueTab } from "./IssueTab";

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

  it("unblocks a held issue as one confirmed label move", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "POST" ? json({ ok: true, detail: "ok" }) : json(detail));
    vi.stubGlobal("fetch", fetchMock);

    render(<IssueTab repo="demo" issue={77} />);

    await userEvent.click(await screen.findByRole("button", { name: "Unblock" }));
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit)?.method === "POST")).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Unblock #77" }));

    const posts = fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "POST");
    expect(posts.map(([input]) => String(input))).toEqual(["/api/repos/demo/issues/77/unblock"]);
  });

  it("offers no unblock for an issue nobody holds", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ ...detail, stage: "awaiting review", escalation_reason: null })));

    render(<IssueTab repo="demo" issue={77} />);

    expect(await screen.findByText("REQUEST_CHANGES round 2")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Unblock" })).not.toBeInTheDocument();
  });
});
