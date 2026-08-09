import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OverviewTab } from "./OverviewTab";

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");

afterEach(() => {
  if (originalClipboard) {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
});

function overview(interventionCount = 0) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify({
      metrics: null,
      cost_mix: null,
      caps: { issue_cap_usd: 5, daily_cap_usd: 10, today_usd: 2 },
      health: [{
        role: "fix",
        alive: true,
        stale: false,
        last_event_ts: "2026-08-01T08:00:00Z",
        last_event: "tick",
        events: 4,
        log_path: "/logs/fix.log",
      }],
      health_trend: [],
      needs_attention: [{ issue: 50, kind: "blocked", title: "Needs a decision" }],
      interventions: Array.from({ length: interventionCount }, (_, index) => ({
        ts: `2026-08-01T08:${String(index).padStart(2, "0")}:00Z`,
        action_class: index === 7 ? "policy" : "guidance",
        issue: 50 + index,
        kind: index === 7 ? "promote needle" : `rearm ${index}`,
        detail: null,
      })),
      spend_per_hour: [],
      warnings: [],
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })),
  );
  return render(<OverviewTab repo="demo" onOpenIssues={vi.fn()} />);
}

describe("OverviewTab hierarchy", () => {
  it("leads with compact metrics and removes redundant page and attention headings", async () => {
    const { container } = overview();

    const health = (await screen.findByText("Daemon health")).closest("section");
    const actions = screen.getByText("Operator actions").closest("section");
    const metrics = container.querySelector(".overview-metrics");

    expect(health).not.toBeNull();
    expect(actions).not.toBeNull();
    expect(metrics).not.toBeNull();
    expect(metrics!.compareDocumentPosition(health!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(metrics!.compareDocumentPosition(actions!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Overview" })).not.toBeInTheDocument();
    expect(screen.queryByText("Needs attention")).not.toBeInTheDocument();
    expect(screen.queryByText(/Reconciles every/)).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Spending caps" })).toHaveTextContent(
      "$2.00today / $10.00",
    );
    expect(screen.queryByText("$ today")).not.toBeInTheDocument();
    expect(screen.queryByText("Active lifecycle")).not.toBeInTheDocument();
    expect(screen.queryByText("Cost mix by task family")).not.toBeInTheDocument();
    expect(screen.queryByText("Spend by task type")).not.toBeInTheDocument();
  });

  it("shows the operator-action timeline without a filter control", async () => {
    overview(12);

    expect(await screen.findByText("promote needle")).toBeVisible();
    expect(screen.getByText("rearm 0")).toBeVisible();
    expect(screen.queryByRole("searchbox", { name: "Filter operator actions" }))
      .not.toBeInTheDocument();
  });

  it("copies a daemon log path from its icon and confirms the action", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    overview();

    await userEvent.click(await screen.findByRole("button", { name: "Copy fix log path" }));

    expect(writeText).toHaveBeenCalledWith("/logs/fix.log");
    expect(screen.getByRole("status")).toHaveTextContent("Log path copied");
    expect(document.querySelector(".health-row")).not.toHaveTextContent("/logs/fix.log");
  });
});
