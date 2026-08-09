import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { IssueCard, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";

const card: IssueCard = {
  issue: 34,
  title: "CI colour on the PR chip",
  url: "https://github.com/o/r/issues/34",
  labels: [],
  pr: 179,
  pr_url: "https://github.com/o/r/pull/179",
  pr_state: "open",
  stage: "fixing CI",
  human_action: null,
  human_wait_since_ts: null,
  human_wait_s: null,
  done: false,
  steps: [],
  cost_usd: 2,
  estimated: false,
  projected_cost_usd: 2,
  projected_model: null,
  started_ts: "2026-08-02T07:00:00Z",
  last_ts: "2026-08-02T08:00:00Z",
  elapsed_s: 60,
  working_s: 45,
  idle_s: 15,
  idle_active: false,
  leg_running: false,
  projected_eta_s: null,
  models: [],
  model_badges: [],
  rounds: 1,
  flags: [],
  issue_verification: null,
  pr_verification: null,
};

function board(over: Partial<IssueCard>) {
  const issues: Polled<IssuesPayload> = {
    data: {
      in_flight: [{ ...card, ...over }],
      pending: [],
      done: [],
      backlog: [],
      gh_ok: true,
      notifications: [],
      warnings: [],
    },
    error: null,
    loading: false,
    updatedAt: 0,
    refresh: vi.fn(),
  };
  return render(
    <IssuesTab repo="demo" sub="inflight" issues={issues} onOpenIssue={vi.fn()} />,
  );
}

function prChip(container: HTMLElement): Element | null {
  return [...container.querySelectorAll(".chip")].find((c) =>
    c.textContent?.includes("PR #179"),
  ) ?? null;
}

describe("the PR control wears CI's verdict", () => {
  it("reads ok when every check passed", () => {
    const { container } = board({ pr_ci_state: "passing" });
    expect(prChip(container)).toHaveClass("chip-ok");
    expect(prChip(container)).toHaveTextContent("PR #179");
    expect(prChip(container)).not.toHaveTextContent("↗");
  });

  it("reads fail when any check failed", () => {
    const { container } = board({ pr_ci_state: "failing" });
    expect(prChip(container)).toHaveClass("chip-fail");
  });

  it("reads running while checks are still going", () => {
    const { container } = board({ pr_ci_state: "pending" });
    expect(prChip(container)).toHaveClass("chip-running");
  });

  it("claims nothing when CI state is unknown", () => {
    const { container } = board({ pr_ci_state: null });
    expect(prChip(container)).toHaveClass("chip-neutral");
    expect(prChip(container)).toHaveAttribute("data-tip", expect.stringContaining("CI unknown"));
  });
});
