import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { IssueCard, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";

const card: IssueCard = {
  issue: 123,
  title: "Waiting for operator input",
  url: "https://github.com/o/r/issues/123",
  labels: [],
  pr: null,
  pr_url: null,
  pr_state: null,
  stage: "blocked",
  done: false,
  steps: [{ kind: "RESUME", state: "running", status: null, cost_usd: null }],
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
  projected_eta_s: 60,
  models: ["gpt-5.6-terra"],
  model_badges: [{ model: "gpt-5.6-terra", effort: "medium" }],
  rounds: 1,
  flags: [],
  issue_verification: null,
  pr_verification: null,
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

describe("blocked precedence", () => {
  it("marks a blocked row as a visual alert even while tasks are running", () => {
    render(
      <IssuesTab
        repo="demo"
        sub="inflight"
        issues={polled({
          in_flight: [card],
          pending: [],
          done: [],
          backlog: [],
          gh_ok: true,
          notifications: [],
          warnings: [],
        })}
        onOpenIssue={vi.fn()}
      />,
    );

    expect(screen.getByRole("row", { name: /Waiting for operator input/ })).toHaveClass("row-alert");
    expect(screen.getByText("blocked")).toBeVisible();
  });
});
