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

    const trigger = screen.getByRole("button", { name: /Quota/ });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Claude 5h")).not.toBeInTheDocument();

    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Claude 5h")).toBeVisible();
  });

  it("uses horizontal meters in the collapsed sidebar", async () => {
    const { container } = render(<UsageBlock usage={usage} collapsed />);

    await userEvent.click(screen.getByRole("button", { name: "Quota" }));

    expect(screen.getByRole("meter", { name: "5h quota usage" })).toHaveAttribute(
      "aria-valuenow",
      "12",
    );
    expect(
      container.querySelector(".compact-quota-track > span"),
    ).toHaveStyle({ width: "12%" });
    expect(container.querySelector(".quota-icon i")).toBeVisible();
  });

  it("clamps compact meter values while announcing the true utilization", async () => {
    const overCap: Polled<UsagePayload> = {
      ...usage,
      data: {
        ...usage.data!,
        harness: {
          ...usage.data!.harness!,
          global_daily_cap_usd: 20,
          today_all_repos_usd: 30,
        },
      },
    };
    render(<UsageBlock usage={overCap} collapsed />);

    await userEvent.click(screen.getByRole("button", { name: "Quota" }));

    expect(screen.getByRole("meter", { name: "$ quota usage" }))
      .toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByRole("meter", { name: "$ quota usage" }))
      .toHaveAttribute("aria-valuetext", "150%");
  });
});
