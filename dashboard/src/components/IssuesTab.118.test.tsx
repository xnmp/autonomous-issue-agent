import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { IssueCard, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";

const card: IssueCard = {
  issue: 118,
  title: "Ready for the operator",
  url: "https://github.com/o/r/issues/118",
  labels: [],
  pr: 75,
  pr_url: "https://github.com/o/r/pull/75",
  pr_state: "open",
  stage: "awaiting merge",
  human_action: "merge-ready",
  human_wait_since_ts: new Date(Date.now() - 310_000).toISOString(),
  human_wait_s: 310,
  done: false,
  steps: [{ kind: "REVIEW", state: "ok", status: "APPROVE", cost_usd: 2, count: 1 }],
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
  models: ["gpt-5.6-terra"],
  model_badges: [{ model: "gpt-5.6-terra", effort: "medium" }],
  rounds: 1,
  flags: ["merge-ready"],
  issue_verification: null,
  pr_verification: null,
};

function polled(data: IssuesPayload): Polled<IssuesPayload> {
  return {
    data,
    error: null,
    loading: false,
    // A real receive instant: every live duration is measured from it.
    updatedAt: Date.now(),
    refresh: vi.fn(),
  };
}

describe("human-owned actions", () => {
  it("surfaces the action and wait duration without offering merge", () => {
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

    expect(screen.getByText(/merge ready · 5m/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /merge/i })).not.toBeInTheDocument();
  });

  it("alerts on a blocked row and renders its blocked leg as a failure", () => {
    const blocked: IssueCard = {
      ...card,
      stage: "blocked",
      human_action: null,
      human_wait_since_ts: null,
      human_wait_s: null,
      flags: ["blocked"],
      steps: [{
        kind: "ADDRESS_REVIEW",
        state: "blocked",
        status: "blocked",
        cost_usd: 2,
        count: 1,
      }],
    };

    const { container } = render(
      <IssuesTab
        repo="demo"
        sub="inflight"
        issues={polled({
          in_flight: [blocked],
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

    expect(container.querySelector("tbody > tr")).toHaveClass("row-alert");
    expect(document.querySelector('[data-tip^="ADDRESS_REVIEW · blocked"]')).toHaveClass("chip-fail");
  });
});

describe("historical flags do not alarm", () => {
  it("keeps a re-armed, approved row calm despite a lifetime escalated flag", () => {
    // keifu #25 (2026-08-02): escalated twice, re-armed, approved, awaiting
    // merge — `escalated` is a fold-lifetime counter, so the row stayed red
    // for life and buried the real alarms.
    const rearmed: IssueCard = {
      ...card,
      stage: "awaiting merge",
      human_action: null,
      human_wait_since_ts: null,
      human_wait_s: null,
      flags: ["escalated", "spend-blocked"],
    };

    const { container } = render(
      <IssuesTab
        repo="demo"
        sub="inflight"
        issues={polled({
          in_flight: [rearmed],
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

    expect(container.querySelector("tbody > tr")).not.toHaveClass("row-alert");
  });
});
