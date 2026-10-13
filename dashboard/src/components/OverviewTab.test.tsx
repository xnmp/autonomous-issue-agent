import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Metrics, OverviewPayload } from "../api/types";
import { OverviewTab } from "./OverviewTab";

const metrics: Metrics = {
  merged_prs: 4, merged_prs_partial: false, open_items: 3, needs_human: 1, total_cost_usd: 20, cost_per_merged_pr_usd: 5,
  by_basis: [{ basis: "provider_reported", usd: 20 }],
  by_kind: [{ kind: "RESOLVE", usd: 12, runs: 4 }, { kind: "REVIEW", usd: 5, runs: 5 }, { kind: "FIX_CI", usd: 3, runs: 2 }],
  first_pass_approvals: 3, first_pass_changes: 1, first_pass_rate: 0.75, rounds_per_pr: 1.25,
  legs: 11, machine_legs: 1, today_usd: 2,
};

function overview(over: Partial<OverviewPayload> = {}) {
  const body: OverviewPayload = {
    metrics,
    caps: { issue_cap_usd: 5, daily_cap_usd: 10, global_daily_cap_usd: 40, today_usd: 2 },
    health: [{ role: "fix", alive: true, stale: false, heartbeat_ts: "2026-08-01T08:00:00Z" }],
    health_trend: [],
    needs_attention: [{
      issue: 50, title: "Needs a decision", reason: "question", headline: "Answer the agent's question",
      detail: "Which endpoint should retries use?",
    }],
    spend_per_hour: [],
    warnings: [],
    ...over,
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
    status: 200, headers: { "Content-Type": "application/json" },
  })));
  const onOpenIssues = vi.fn();
  const view = render(
    <OverviewTab repo="demo" onOpenIssues={onOpenIssues} showDaemonHealth showIssueHealthTrend />,
  );
  return { ...view, onOpenIssues };
}

describe("OverviewTab", () => {
  it("leads with the metrics folded from legs and merges, then the items waiting on a person", async () => {
    const { container } = overview();

    const health = (await screen.findByText("Daemon health")).closest("section");
    const attention = screen.getByText("Needs you", { selector: ".panel-title" }).closest("section");
    const tiles = container.querySelector(".overview-metrics");
    expect(tiles!.compareDocumentPosition(health!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(tiles!.compareDocumentPosition(attention!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Overview" })).toHaveClass("sr-only");

    expect(screen.getByText("$ per merged PR").closest(".tile")).toHaveTextContent("$5");
    expect(screen.getByText("$ per merged PR").closest(".tile")).toHaveTextContent("$20.00 total");
    expect(screen.getByText("First-pass rate").closest(".tile")).toHaveTextContent("3 approve / 1 changes");
    expect(screen.getByText("Legs").closest(".tile")).toHaveTextContent("resolve $12.00 · review $5.00 · repair $3.00");
    expect(screen.getByRole("region", { name: "Spending caps" })).toHaveTextContent("$2.00today / $10.00");
    expect(screen.getByRole("region", { name: "Spending caps" })).toHaveTextContent("All repos $40.00");
  });

  it("lists each item stopped for a person with its stop, as text", async () => {
    overview();

    expect(await screen.findByText("Answer the agent's question")).toBeVisible();
    expect(screen.getByText(/Which endpoint should retries use\?/)).toBeVisible();
    expect(screen.getByText("#50")).toBeVisible();
  });

  it("says when nothing waits on a person, and reports unknown merges as unknown, not zero", async () => {
    overview({ needs_attention: [], metrics: { ...metrics, merged_prs: null, cost_per_merged_pr_usd: null } });

    expect(await screen.findByText(/nothing is waiting on you/)).toBeVisible();
    expect(screen.getByText("Merged").closest(".tile")).toHaveTextContent("GitHub not read yet");
    expect(screen.getByText("Merged").closest(".tile")).not.toHaveTextContent(/\b0\b/);
  });

  it("marks merge totals as bounds when GitHub listed only the newest merged PRs", async () => {
    overview({ metrics: { ...metrics, merged_prs: 200, merged_prs_partial: true, cost_per_merged_pr_usd: 5 } });

    expect(await screen.findByText("≥200")).toBeVisible();
    expect(screen.getByText("Merged").closest(".tile")).toHaveTextContent("newest harness PRs only");
    expect(screen.getByText("$ per merged PR").closest(".tile")).toHaveTextContent("≤$5");
  });

  it("says the board is unavailable when the control store could not be read", async () => {
    overview({ store_error: "control store unavailable: IncompatibleSchema: control store predates the cutover" });

    expect(await screen.findByText(/Board unavailable — control store unavailable/)).toBeVisible();
  });

  it("opens the in-flight board from the Needs-you tile", async () => {
    const { onOpenIssues } = overview();

    await userEvent.click((await screen.findByText("Needs you", { selector: ".tile-label" })).closest("button")!);
    expect(onOpenIssues).toHaveBeenCalledWith("inflight");
  });

  it("shows each role's last heartbeat", async () => {
    overview();
    expect(await screen.findByText(/heartbeat/, { selector: ".health-row span" })).toBeVisible();
  });
});
