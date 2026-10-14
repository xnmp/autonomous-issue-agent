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
  legs: 11, machine_legs: 1,
};

function overview(over: Partial<OverviewPayload> = {}) {
  const body: OverviewPayload = {
    metrics,
    caps: {
      issue_cap_usd: 5, daily_cap_usd: 10, global_daily_cap_usd: 40, today_usd: 2, global_today_usd: 30,
      headroom: [{ role: "fix", leg_usd: 10, fits: true }, { role: "review", leg_usd: 15, fits: false }],
    },
    health: [{
      role: "fix", alive: true, stale: false, heartbeat_ts: "2026-08-01T08:00:00Z", state: "running",
      in_flight: [{ kind: "REBASE", number: 999 }],
    }],
    leases: [{
      role: "fix", issue: 755, pr: 999, leg_id: "L1", kind: "REBASE", leg_state: "running", pid: 4242,
      acquired_ts: "2026-08-01T08:00:00Z",
    }],
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
    <OverviewTab repo="demo" onOpenIssues={onOpenIssues} showIssueHealthTrend />,
  );
  return { ...view, onOpenIssues };
}

describe("OverviewTab", () => {
  it("says once while the first GitHub observation is still being read, and not after", async () => {
    const pending = overview({ observed_at: null });
    expect(await screen.findAllByText("Reading GitHub…")).toHaveLength(1);
    pending.unmount();
    overview({ observed_at: "2026-08-01T08:00:00Z" });
    await screen.findByText("Merged");
    expect(screen.queryByText("Reading GitHub…")).toBeNull();
  });

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
    expect(screen.getByText("Legs").closest(".tile")).toHaveTextContent("resolve $12.00 · fix ci $3.00 · review $5.00");
  });

  it("shows committed spend against both daily caps and each role's headroom", async () => {
    overview();

    const caps = await screen.findByRole("region", { name: "Spending caps" });
    expect(caps).toHaveTextContent("$2.00today / $10.00");
    expect(caps).toHaveTextContent("$30.00 / $40.00");
    expect(caps).toHaveTextContent("fix fits");
    expect(caps).toHaveTextContent("review waits");
    expect(caps).toHaveClass("tile-alert");
  });

  it("lists the leases the roles hold, with their legs", async () => {
    overview();

    const leases = await screen.findByRole("region", { name: "Leases" });
    expect(leases).toHaveTextContent("#755");
    expect(leases).toHaveTextContent("PR #999");
    expect(leases).toHaveTextContent("Rebase");
    expect(leases).toHaveTextContent("4242");
  });

  it("says no role holds an item when there are no leases", async () => {
    overview({ leases: [] });

    expect(await screen.findByText("no role holds an item")).toBeVisible();
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

  it("shows each role's heartbeat, its state and what it holds", async () => {
    overview();
    const row = (await screen.findByText(/heartbeat/, { selector: ".health-row span" })).closest(".health-row");
    expect(row).toHaveTextContent("running");
    expect(row).toHaveTextContent("Rebase #999");
  });

  it("lists each workflow failing on the base branch with its run and issue in the health panel", async () => {
    overview({
      base_failures: [
        {
          workflow: "Windows smoke", since_ts: "2026-08-01T07:00:00Z",
          run_url: "https://github.com/o/r/actions/runs/81", issue_url: "https://github.com/o/r/issues/41",
        },
        { workflow: "macOS Smoke", since_ts: "2026-08-01T08:00:00Z", run_url: "https://github.com/o/r/actions/runs/71" },
      ],
    });

    const health = (await screen.findByText("Daemon health")).closest("section")!;
    const windows = (await screen.findByText("Windows smoke")).closest(".health-row")!;
    expect(health).toContainElement(windows as HTMLElement);
    expect(windows).toHaveTextContent("failing on base");
    expect(windows).toHaveTextContent("first failed");
    expect(screen.getAllByRole("link", { name: "latest run" }).map((a) => a.getAttribute("href"))).toEqual([
      "https://github.com/o/r/actions/runs/81", "https://github.com/o/r/actions/runs/71",
    ]);
    expect(screen.getByRole("link", { name: "issue" })).toHaveAttribute("href", "https://github.com/o/r/issues/41");
    expect(screen.getByText("macOS Smoke").closest(".health-row")).toHaveTextContent("no open issue");
  });

  it("shows no base-CI row when the base branch is green or unread", async () => {
    overview({ base_failures: null });
    await screen.findByText("Daemon health");
    expect(screen.queryByText("failing on base")).toBeNull();
  });
});
