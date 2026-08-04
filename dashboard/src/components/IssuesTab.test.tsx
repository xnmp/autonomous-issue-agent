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
  projected_eta_s: 60,
  models: ["gpt-5.6-terra", "claude-opus-4-1"],
  model_badges: [
    { model: "gpt-5.6-terra", effort: "medium" },
    { model: "claude-opus-4-1", effort: null },
  ],
  rounds: 2,
  flags: [],
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
    expect(screen.getByRole("columnheader", { name: /Verification/ })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: /Since done/ })).toBeVisible();
    const verification = screen.getByRole("cell", { name: "pending" });
    expect(verification).toBeVisible();
    expect(screen.getByText("pending", { exact: true })).toHaveAttribute(
      "data-tip",
      "pending\nIssue: VERIFIED\nPR: pending",
    );
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

  it("expands a pending row to show the issue text, same as backlog", async () => {
    render(
      <IssuesTab
        repo="demo"
        sub="inflight"
        issues={polled({
          in_flight: [],
          pending: [{
            issue: 81,
            title: "Waiting its turn",
            url: "https://github.com/o/r/issues/81",
            body: "## Context\n\nthe queue is long",
            priority: "P2",
            labels: ["ai-ready"],
            held: true,
            hold_reason: "waiting on PR #179",
            overlap: null,
          }],
          done: [],
          backlog: [],
          gh_ok: true,
          notifications: [],
          warnings: [],
        })}
        onOpenIssue={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("row", { name: /Waiting its turn/ }));
    // MarkdownBody arrives via React.lazy — same load-tolerant timeout as the
    // backlog expansion test.
    expect(
      await screen.findByRole("heading", { name: "Context" }, { timeout: 10_000 }),
    ).toBeVisible();
    expect(screen.getByText("the queue is long")).toBeVisible();
  });

  it("badges a parked pending row and offers Unpark in its expansion", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return new Response(JSON.stringify({ ok: true, detail: "ok" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    render(
      <IssuesTab
        repo="demo"
        sub="inflight"
        issues={polled({
          in_flight: [],
          pending: [{
            issue: 34,
            title: "Parked work",
            url: null,
            body: "text",
            priority: null,
            labels: ["ai-parked"],
            held: true,
            hold_reason: "parked",
            overlap: null,
          }],
          done: [],
          backlog: [],
          gh_ok: true,
          notifications: [],
          warnings: [],
        })}
        onOpenIssue={vi.fn()}
      />,
    );

    expect(screen.getByText("parked")).toBeVisible();
    await userEvent.click(screen.getByRole("row", { name: /Parked work/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Unpark" }));
    expect(calls.some((url) => url.endsWith("/issues/34/unpark"))).toBe(true);
    vi.unstubAllGlobals();
  });

  it("does not toggle a pending row when a priority button is clicked", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ ok: true, detail: "" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    render(
      <IssuesTab
        repo="demo"
        sub="inflight"
        issues={polled({
          in_flight: [],
          pending: [{
            issue: 81,
            title: "Waiting its turn",
            url: null,
            body: "text",
            priority: null,
            labels: ["ai-ready"],
            held: false,
            hold_reason: null,
            overlap: null,
          }],
          done: [],
          backlog: [],
          gh_ok: true,
          notifications: [],
          warnings: [],
        })}
        onOpenIssue={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "P3" }));
    expect(screen.queryByText("Issue context")).not.toBeInTheDocument();
    vi.unstubAllGlobals();
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
