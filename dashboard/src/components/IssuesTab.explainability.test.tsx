import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { IssueCard, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";

const base: IssueCard = {
  issue: 200,
  title: "Explain the board",
  url: "https://github.com/o/r/issues/200",
  labels: [],
  pr: null,
  pr_url: null,
  pr_state: null,
  stage: "escalated",
  done: false,
  steps: [{ kind: "RESOLVE", state: "ok", status: "ok", cost_usd: 1, count: 1 }],
  cost_usd: 1,
  estimated: false,
  projected_cost_usd: 2,
  projected_model: "gpt-5.6-terra",
  started_ts: "2026-08-01T07:00:00Z",
  last_ts: "2026-08-01T08:00:00Z",
  elapsed_s: 600,
  working_s: 45,
  idle_s: 555,
  idle_active: true,
  leg_running: false,
  projected_eta_s: 900,
  models: ["gpt-5.6-terra"],
  model_badges: [{ model: "gpt-5.6-terra", effort: "medium" }],
  rounds: 1,
  flags: [],
  issue_verification: null,
  pr_verification: null,
};

function polled(
  cards: IssueCard[],
  updatedAt: number | null = 0,
): Polled<IssuesPayload> {
  return {
    data: {
      in_flight: cards,
      pending: [],
      done: [],
      backlog: [],
      gh_ok: true,
      notifications: [],
      warnings: [],
    },
    error: null,
    loading: false,
    updatedAt,
    refresh: vi.fn(),
  };
}

function board(issues: Polled<IssuesPayload>) {
  return render(
    <IssuesTab repo="demo" sub="inflight" issues={issues} onOpenIssue={vi.fn()} />,
  );
}

describe("board explainability", () => {
  it("alerts the whole row and states why a human is needed", () => {
    const card: IssueCard = {
      ...base,
      human_action: "escalated",
      escalation_reason: "3 infra deaths in a row on the same PR",
      flags: ["escalated"],
    };
    const { container } = board(polled([card]));

    expect(container.querySelector("tbody > tr")).toHaveClass("row-alert");
    expect(screen.getByText("3 infra deaths in a row on the same PR")).toBeVisible();
  });

  it("offers Park beside Unblock only when a blocked issue has an open PR", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            issue: 200, title: "Explain the board", url: null,
            stage: "escalated", done: false, prs: [], pull_requests: [],
            cost_usd: 1, elapsed_s: 600, working_s: 45, idle_s: 555,
            idle_active: true, steps: [], events: [], warnings: [],
            leg_running: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    board(polled([{
      ...base,
      pr: 179,
      pr_url: "https://github.com/o/r/pull/179",
      pr_state: "open",
      human_action: "escalated",
      flags: ["escalated"],
    }]));
    await userEvent.click(screen.getByRole("row", { name: /Explain the board/ }));
    expect(await screen.findByRole("button", { name: "Park" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Unblock" })).toBeVisible();
    vi.unstubAllGlobals();
  });

  it("offers no Park on a blocked issue without an open PR", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            issue: 200, title: "Explain the board", url: null,
            stage: "escalated", done: false, prs: [], pull_requests: [],
            cost_usd: 1, elapsed_s: 600, working_s: 45, idle_s: 555,
            idle_active: true, steps: [], events: [], warnings: [],
            leg_running: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    board(polled([{ ...base, human_action: "escalated", flags: ["escalated"] }]));
    await userEvent.click(screen.getByRole("row", { name: /Explain the board/ }));
    expect(await screen.findByRole("button", { name: "Unblock" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Park" })).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("names an escalation with no recorded reason instead of showing nothing", () => {
    board(polled([{ ...base, human_action: "escalated", flags: ["escalated"] }]));

    expect(screen.getByText("escalated — see the issue comments")).toBeVisible();
  });

  it("asks the operator the agent's own question while awaiting a reply", () => {
    board(polled([{
      ...base,
      stage: "awaiting human reply",
      human_action: "awaiting-reply",
      attention_reason: "Should retries use endpoint A or endpoint B?",
    }]));

    expect(screen.getByText("Should retries use endpoint A or endpoint B?")).toBeVisible();
  });

  it("marks a merge-ready row for attention without alarming it", () => {
    const { container } = board(polled([{
      ...base,
      stage: "awaiting merge",
      human_action: "merge-ready",
      flags: ["merge-ready"],
    }]));

    const row = container.querySelector("tbody > tr");
    expect(row).toHaveClass("row-alert");
    expect(row).toHaveClass("row-alert-ready");
  });

  it("freezes the work clock once the issue belongs to a human", () => {
    // The poll is ten minutes stale: an un-frozen work clock would have
    // invented ten minutes of work nobody did.
    const stale = Date.now() - 600_000;
    board(polled([{ ...base, human_action: "escalated", idle_active: false }], stale));

    expect(workClock()).toHaveTextContent("work 0:45");
  });

  it("keeps ticking the work clock for an issue the harness still owns", () => {
    const stale = Date.now() - 600_000;
    board(polled(
      [{ ...base, stage: "resolving", idle_active: false, leg_running: true }],
      stale,
    ));

    // The shared 1 Hz clock may lag the anchor by a tick, so allow the second.
    expect(workClock()).toHaveTextContent(/work 10:4\d/);
  });

  it("keeps cost quiet while it stays under the projection", () => {
    const { container } = board(polled([{ ...base, cost_usd: 1 }]));

    expect(container.querySelector(".cost-cell")).not.toHaveClass("cost-over");
  });

  it("warns on cost only when it passes the projection", () => {
    const { container } = board(polled([{ ...base, cost_usd: 3 }]));

    expect(container.querySelector(".cost-cell")).toHaveClass("cost-over");
  });

  it("tells the story of the issue, notes and all, when the row is expanded", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-08-01T09:11:00Z"));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            issue: 200,
            title: "Explain the board",
            url: null,
            stage: "escalated",
            escalation_reason: "3 infra deaths in a row on the same PR",
            done: false,
            prs: [],
            pull_requests: [],
            cost_usd: 3,
            elapsed_s: 600,
            working_s: 45,
            idle_s: 555,
            idle_active: true,
            leg_running: false,
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
              {
                idx: 1,
                kind: "unblock",
                role: null,
                ts: "2026-08-01T07:20:00Z",
                state: "done",
                status: null,
                exit_reason: null,
                duration_s: null,
                cost_usd: 0,
                estimated: false,
                model: null,
                effort: null,
                session_id: null,
                turns: null,
                usage: null,
                has_transcript: false,
                note: "removed ai-blocked; reset retry counters",
              },
            ],
            events: [],
            warnings: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    board(polled([{ ...base, human_action: "escalated" }]));
    await userEvent.click(screen.getByRole("row", { name: /Explain the board/ }));

    expect(await screen.findByText("REQUEST_CHANGES round 2")).toBeVisible();
    expect(screen.getByText("removed ai-blocked; reset retry counters")).toBeVisible();
    expect(document.querySelector('[data-tip="unblock"]')).toHaveClass("chip-meta");
    // Successful steps stay quiet: no "ok"/"done" word next to every badge.
    expect(document.querySelector(".task-state")).toBeNull();
    const history = screen.getByRole("table", { name: "Issue #200 task history" });
    expect(within(history).getAllByRole("columnheader").map((cell) => cell.textContent))
      .toEqual(["Time", "Cost", "Time since"]);
    expect(within(history).getByText("took 0:30")).toBeVisible();
    expect(within(history).getByText("$1.00")).toBeVisible();
    expect(within(history).getByText("$0.00")).toBeVisible();
    expect(within(history).getByText("2h")).toBeVisible();
    expect(within(history).getByText("1h")).toBeVisible();
    now.mockRestore();
    vi.unstubAllGlobals();
  });

  it("gives simultaneous blocked and in-flight histories distinct table names", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            issue: 200,
            title: "Explain the board",
            url: null,
            stage: "resolving",
            escalation_reason: null,
            done: false,
            prs: [],
            pull_requests: [],
            cost_usd: 1,
            elapsed_s: 600,
            working_s: 45,
            idle_s: 555,
            idle_active: true,
            leg_running: true,
            steps: [
              {
                idx: 0,
                kind: "RESOLVE",
                role: "fix",
                ts: "2026-08-01T07:10:00Z",
                state: "running",
                status: null,
                exit_reason: null,
                duration_s: null,
                cost_usd: 1,
                estimated: false,
                model: "gpt-5.6-terra",
                effort: "medium",
                session_id: null,
                turns: null,
                usage: null,
                has_transcript: false,
                note: null,
              },
            ],
            events: [],
            warnings: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    board(polled([
      { ...base, human_action: "escalated" },
      { ...base, issue: 201, title: "Ordinary live work", stage: "resolving", leg_running: true },
    ]));
    await userEvent.click(screen.getByRole("row", { name: /Explain the board/ }));
    await userEvent.click(screen.getByRole("row", { name: /Ordinary live work/ }));

    const blockedHistory = await screen.findByRole("table", { name: "Issue #200 task history" });
    const liveHistory = await screen.findByRole("table", { name: "Issue #201 task history" });
    expect(within(blockedHistory).getAllByRole("columnheader").map((cell) => cell.textContent))
      .toEqual(["Time", "Cost", "Time since"]);
    expect(within(liveHistory).getAllByRole("columnheader").map((cell) => cell.textContent))
      .toEqual(["Time", "Cost", "Time since"]);
    expect(within(liveHistory).getByText("resolve")).toBeVisible();
    vi.unstubAllGlobals();
  });

  it("degrades malformed and absent values and clamps future time since", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            issue: 200,
            title: "Explain the board",
            url: null,
            stage: "resolving",
            escalation_reason: null,
            done: false,
            prs: [],
            pull_requests: [],
            cost_usd: null,
            elapsed_s: null,
            working_s: null,
            idle_s: null,
            idle_active: false,
            leg_running: false,
            steps: [
              {
                idx: 0, kind: "ROUTE", role: "fix", ts: "not-a-time",
                state: "ok", status: null, exit_reason: null, duration_s: null,
                cost_usd: null, estimated: false, model: null, effort: null,
                session_id: null, turns: null, usage: null,
                has_transcript: false, note: null,
              },
              {
                idx: 1, kind: "REVIEW", role: "review", ts: "2099-01-01T00:00:00Z",
                state: "ok", status: null, exit_reason: null, duration_s: null,
                cost_usd: Number.MAX_VALUE, estimated: false, model: null, effort: null,
                session_id: null, turns: null, usage: null,
                has_transcript: false, note: null,
              },
            ],
            events: [],
            warnings: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    board(polled([{ ...base, stage: "resolving" }]));
    await userEvent.click(screen.getByRole("row", { name: /Explain the board/ }));

    const history = await screen.findByRole("table", { name: "Issue #200 task history" });
    const malformed = within(history).getByText("route").closest("tr");
    const future = within(history).getByText("review").closest("tr");
    expect(malformed).not.toBeNull();
    expect(future).not.toBeNull();
    expect(within(malformed!).getAllByText("—")).toHaveLength(3);
    expect(malformed!.querySelector("time")).not.toHaveAttribute("datetime");
    expect(within(future!).getByText("0s")).toBeVisible();
    expect(future!.querySelector("time")).toHaveAttribute("datetime", "2099-01-01T00:00:00Z");
    expect(future!.querySelectorAll("td").item(1).textContent).toMatch(/^\$[\d.e+]+$/);
    vi.unstubAllGlobals();
  });
});

function workClock(): HTMLElement {
  return screen.getByTitle("time actively progressing through harness work");
}
