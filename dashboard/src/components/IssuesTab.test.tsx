import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { IssueCard, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";

const doneCard: IssueCard = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/o/r/issues/50",
  labels: [],
  pr: 75,
  pr_url: "https://github.com/o/r/pull/75",
  pr_state: "merged",
  stage: "done",
  done: true,
  steps: [{ kind: "RESOLVE", state: "ok", status: "ok", cost_usd: 2, count: 1 }],
  cost_usd: 2,
  estimated: false,
  projected_cost_usd: 2,
  projected_model: "gpt-5.6-terra",
  started_ts: "2026-07-29T07:00:00Z",
  last_ts: "2026-07-29T08:00:00Z",
  elapsed_s: 60,
  working_s: 45,
  idle_s: 15,
  idle_active: false,
  leg_running: false,
  models: ["gpt-5.6-terra", "claude-opus-4-1"],
  model_badges: [
    { model: "gpt-5.6-terra", effort: "medium" },
    { model: "claude-opus-4-1", effort: null },
  ],
  rounds: 2,
  issue_verification: "VERIFIED",
  pr_verification: "pending",
  merged_ts: "2026-07-29T08:00:00Z",
};

function polled(data: IssuesPayload): Polled<IssuesPayload> {
  return {
    data,
    error: null,
    loading: false,
    updatedAt: 0,
    refresh: vi.fn(),
  };
}

describe("IssuesTab", () => {
  it("expands done rows before opening the full issue and hides rounds by default", async () => {
    const onOpenIssue = vi.fn();
    const { container } = render(
      <IssuesTab
        repo="demo"
        sub="done"
        issues={polled({
          in_flight: [],
          pending: [],
          done: [doneCard],
          backlog: [],
          gh_ok: true,
          notifications: [],
          warnings: [],
        })}
        onOpenIssue={onOpenIssue}
      />,
    );

    expect(screen.queryByRole("columnheader", { name: "Rounds" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /Verification/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Verification" })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Since done/ })).toBeVisible();
    expect(screen.queryByLabelText("Issue #50 lifecycle")).not.toBeInTheDocument();
    const row = screen.getByRole("row", { name: /Explain completed work.*#50.*PR #75/ });
    await userEvent.click(row);
    expect(onOpenIssue).not.toHaveBeenCalled();
    expect(screen.getByText("resolve")).toBeVisible();
    expect(container.querySelector("ol.task-story")).toBeVisible();
    expect(screen.queryByRole("table", { name: /task history/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Open full issue" }));
    expect(onOpenIssue).toHaveBeenCalledWith(50);

    fireEvent.contextMenu(screen.getByRole("columnheader", { name: /Issue \/ PR/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Rounds" }));
    expect(screen.getByRole("columnheader", { name: /Rounds/ })).toBeVisible();
  });

  it("uses clock durations and can omit seconds", () => {
    render(
      <IssuesTab
        repo="demo"
        sub="done"
        issues={polled({
          in_flight: [],
          pending: [],
          done: [{ ...doneCard, working_s: 4_638, idle_s: 62 }],
          backlog: [],
          gh_ok: true,
          notifications: [],
          warnings: [],
        })}
        onOpenIssue={vi.fn()}
        showDurationSeconds={false}
      />,
    );

    expect(screen.getByText("1:17")).toBeVisible();
    expect(screen.getByText("1m")).toBeVisible();
  });

  it("expands formatted backlog context and keeps noisy columns disabled by default", async () => {
    render(
      <IssuesTab
        repo="demo"
        sub="backlog"
        issues={polled({
          in_flight: [],
          pending: [],
          done: [],
          backlog: [{
            issue: 63,
            title: "The incident came back",
            url: "https://github.com/o/r/issues/63",
            body: "## What happened\n\n- first symptom\n- second symptom\n\n- [x] mitigated\n\nSee **incident notes** and `logs`.",
            labels: ["bug", "recurrence"],
            recurrence: true,
            recurrence_count: 2,
            steward_state: "raw",
            age_days: 1,
            comments: 7,
            issue_type: "bug",
            difficulty: "medium",
            impact: "high",
          }],
          gh_ok: true,
          notifications: [],
          warnings: [],
        })}
        onOpenIssue={vi.fn()}
      />,
    );

    expect(screen.queryByRole("columnheader", { name: "Recurrence" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "# comments" })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Type/ })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: /Difficulty/ })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: /Impact/ })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: /Steward state/ })).toBeVisible();
    expect(screen.getByText("medium")).toBeVisible();
    expect(screen.getByText("high")).toBeVisible();
    expect(screen.getByText("The incident came back")).toHaveClass("recurrence-2");

    await userEvent.click(screen.getByRole("row", { name: /The incident came back/ }));
    // MarkdownBody arrives via React.lazy: on a loaded machine the dynamic
    // import can outlive findBy's default 1s and flake the whole suite.
    expect(
      await screen.findByRole("heading", { name: "What happened" }, { timeout: 10_000 }),
    ).toBeVisible();
    expect(screen.getByText("first symptom")).toBeVisible();
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(screen.getByText("incident notes").tagName).toBe("STRONG");
    expect(screen.getByText("logs").tagName).toBe("CODE");
    expect(screen.getByRole("button", { name: "Promote #63 to ai-ready" })).toBeVisible();
  });

  it("does not toggle a backlog row when its promote button is keyboard-activated", async () => {
    render(
      <IssuesTab
        repo="demo"
        sub="backlog"
        issues={polled({
          in_flight: [],
          pending: [],
          done: [],
          backlog: [{
            issue: 63,
            title: "Keyboard-safe controls",
            url: "https://github.com/o/r/issues/63",
            body: "## Context",
            labels: [],
            recurrence: false,
            recurrence_count: 0,
            steward_state: "raw",
            age_days: 1,
            comments: 0,
          }],
          gh_ok: true,
          notifications: [],
          warnings: [],
        })}
        onOpenIssue={vi.fn()}
      />,
    );

    const promote = screen.getByRole("button", { name: "Promote #63 to ai-ready" });
    promote.focus();
    await userEvent.keyboard("{Enter}");

    expect(screen.getByRole("dialog")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Context" })).not.toBeInTheDocument();
  });
});

describe("Untriaged tab", () => {
  const backlog = (issue: number, steward_state: string, title: string) => ({
    issue, title, url: null, body: null, labels: [], recurrence: false, recurrence_count: 0,
    steward_state, age_days: 1, comments: 0,
  });

  it("lists only issues the steward has not seen, each promotable to ai-ready after a confirmation", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${String(input)}`);
      return new Response(JSON.stringify({ ok: true, detail: "ok" }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const issues = polled({
      in_flight: [], pending: [], done: [],
      backlog: [
        backlog(71, "raw", "Nobody has looked at this"),
        backlog(72, "processed", "Already triaged"),
        backlog(73, "blocked_on_human_reply", "Steward asked a question"),
      ],
      gh_ok: true, notifications: [], warnings: [],
    } as unknown as IssuesPayload);
    render(<IssuesTab repo="demo" sub="untriaged" issues={issues} onOpenIssue={vi.fn()} />);

    expect(screen.getByText("Nobody has looked at this")).toBeVisible();
    expect(screen.queryByText("Already triaged")).not.toBeInTheDocument();
    expect(screen.queryByText("Steward asked a question")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Promote to ai-ready #71" }));
    expect(calls).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Promote #71" }));

    expect(calls).toEqual(["POST /api/repos/demo/issues/71/promote"]);
    expect(issues.refresh).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("says so when no backlog issue is untriaged", () => {
    render(
      <IssuesTab
        repo="demo"
        sub="untriaged"
        issues={polled({
          in_flight: [], pending: [], done: [], backlog: [backlog(72, "processed", "Already triaged")],
          gh_ok: true, notifications: [], warnings: [],
        } as unknown as IssuesPayload)}
        onOpenIssue={vi.fn()}
      />,
    );

    expect(screen.getByText(/no untriaged backlog issues/)).toBeVisible();
  });
});
