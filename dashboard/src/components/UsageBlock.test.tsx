import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { UsageBlock } from "./UsageBlock";
import type { Polled } from "../api/hooks";
import type { UsagePayload } from "../api/types";

const usage: Polled<UsagePayload> = {
  data: {
    claude: {
      five_hour_pct: 12,
      five_hour_resets_at: null,
      seven_day_pct: 20,
      seven_day_resets_at: null,
      source: "oauth",
      error: null,
    },
    codex: {
      weekly_pct: 30,
      window_minutes: 10080,
      resets_at: null,
      plan: "plus",
      source: "rollout",
      sampled_at: null,
      error: null,
    },
    harness: {
      global_daily_cap_usd: 20,
      today_all_repos_usd: 2,
      per_repo: [],
    },
    warnings: [],
  },
  error: null,
  loading: false,
  updatedAt: 0,
  refresh: vi.fn(),
};

describe("UsageBlock", () => {
  it("is collapsed by default and expands on demand", async () => {
    render(<UsageBlock usage={usage} />);

    const summary = screen.getByText("Quota").closest("summary");
    expect(summary?.parentElement).not.toHaveAttribute("open");
    expect(screen.getByText("Claude 5h")).not.toBeVisible();

    await userEvent.click(screen.getByText("Quota"));
    expect(screen.getByText("Claude 5h")).toBeVisible();
  });
});
