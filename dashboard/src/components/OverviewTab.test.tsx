import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { OverviewTab } from "./OverviewTab";

function overview(interventionCount = 0) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify({
      metrics: null,
      cost_mix: null,
      caps: { issue_cap_usd: 5, daily_cap_usd: 10, today_usd: 2 },
      health: [],
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
  it("puts live attention and operator actions before historical metrics", async () => {
    const { container } = overview();

    const attention = (await screen.findByText("Needs attention")).closest("section");
    const actions = screen.getByText("Operator actions").closest("section");
    const metrics = container.querySelector(".overview-metrics");

    expect(attention).not.toBeNull();
    expect(actions).not.toBeNull();
    expect(metrics).not.toBeNull();
    expect(attention!.compareDocumentPosition(actions!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(actions!.compareDocumentPosition(metrics!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  });

  it("filters a long operator-action timeline without hiding its total", async () => {
    overview(12);

    const filter = await screen.findByRole("searchbox", { name: "Filter operator actions" });
    expect(screen.getByText("12/12")).toBeVisible();
    await userEvent.type(filter, "needle");

    expect(screen.getByText("promote needle")).toBeVisible();
    expect(screen.queryByText("rearm 0")).not.toBeInTheDocument();
    expect(screen.getByText("1/12")).toBeVisible();
  });
});
