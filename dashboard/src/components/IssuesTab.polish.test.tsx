import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { BacklogCard, IssueCard, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";

const done: IssueCard = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/o/r/issues/50",
  pr: 75,
  pr_url: "https://github.com/o/r/pull/75",
  pr_state: "merged",
  stage: "done",
  done: true,
  steps: [],
  cost_usd: 2,
  estimated: false,
  projected_cost_usd: 2,
  started_ts: "2026-07-29T07:00:00Z",
  last_ts: "2026-07-29T08:00:00Z",
  merged_ts: "2026-07-29T08:00:00Z",
  elapsed_s: 3600,
  working_s: 3600,
  idle_s: 0,
  idle_active: false,
  projected_eta_s: 3600,
  models: [],
  model_badges: [],
  rounds: 1,
  flags: [],
  issue_verification: "VERIFIED",
  pr_verification: "VERIFIED",
};

const backlog: BacklogCard = {
  issue: 63,
  title: "The board is hard to read",
  url: "https://github.com/o/r/issues/63",
  body: "long body",
  labels: ["type:enhancement", "difficulty:complex", "ai-triaged"],
  recurrence: false,
  recurrence_count: 0,
  age_days: 1,
  comments: 0,
  issue_type: "enhancement",
  difficulty: "complex",
  impact: "high",
};

function polled(payload: Partial<IssuesPayload>): Polled<IssuesPayload> {
  return {
    data: {
      in_flight: [],
      pending: [],
      done: [],
      backlog: [],
      gh_ok: true,
      notifications: [],
      warnings: [],
      ...payload,
    } as IssuesPayload,
    error: null,
    loading: false,
    updatedAt: 0,
    refresh: vi.fn(),
  };
}

function board(sub: "inflight" | "done" | "backlog", payload: Partial<IssuesPayload>) {
  return render(
    <IssuesTab repo="demo" sub={sub} issues={polled(payload)} onOpenIssue={vi.fn()} />,
  );
}

describe("merge readiness on the PR chip", () => {
  it("marks a mergeable PR", () => {
    board("done", { done: [{ ...done, pr_review_state: "mergeable" }] });

    expect(screen.getByRole("img", { name: "approved and mergeable" })).toHaveTextContent("✓");
  });

  it("marks a PR whose review asked for changes", () => {
    board("done", { done: [{ ...done, pr_review_state: "changes_requested" }] });

    expect(screen.getByRole("img", { name: "review requested changes" })).toHaveTextContent("±");
  });

  it("says nothing about merge readiness when the backend could not tell", () => {
    board("done", { done: [{ ...done, pr_review_state: null }] });

    expect(screen.queryByRole("img", { name: /merge|changes/ })).not.toBeInTheDocument();
    expect(screen.getByText("PR #75")).toBeVisible();
  });
});

describe("done verification badge", () => {
  it("shows the ledger's reason for both verdicts on hover", () => {
    board("done", {
      done: [{
        ...done,
        issue_verification_reason: "probe stayed green for a week",
        pr_verification_reason: "no follow-up defect filed",
      }],
    });

    expect(screen.getByText("verified")).toHaveAttribute(
      "title",
      "verified\nIssue: VERIFIED — probe stayed green for a week\nPR: VERIFIED — no follow-up defect filed",
    );
  });

  it("still names the verdict when the ledger recorded no reason", () => {
    board("done", { done: [done] });

    expect(screen.getByText("verified")).toHaveAttribute(
      "title",
      "verified\nIssue: VERIFIED\nPR: VERIFIED",
    );
  });

  it("collapses to a glyph on request and keeps the verdict readable and remembered", async () => {
    const { unmount } = board("done", { done: [done] });

    await userEvent.click(screen.getByRole("button", { name: /Badges/ }));

    const glyph = screen.getByRole("img", { name: "verified" });
    expect(glyph).toHaveTextContent("✓");
    expect(screen.queryByText("verified")).not.toBeInTheDocument();
    expect(glyph.closest(".chip")).toHaveAttribute("title", expect.stringContaining("VERIFIED"));

    unmount();
    board("done", { done: [done] });
    expect(screen.getByRole("img", { name: "verified" })).toBeVisible();
  });
});

describe("done timing columns", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-01T08:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows how long ago by default and hides the absolute date", () => {
    board("done", { done: [done] });

    expect(screen.getByRole("columnheader", { name: /Since done/ })).toBeVisible();
    expect(screen.queryByRole("columnheader", { name: /Date/ })).not.toBeInTheDocument();
    expect(screen.getByText("3d")).toBeVisible();
  });

  it("renders an unknown completion instant as absent, never as just now", () => {
    board("done", { done: [{ ...done, merged_ts: null, last_ts: null }] });

    const cells = screen.getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toContain("—");
  });
});

describe("backlog triage taxonomy", () => {
  it("colours each axis value and its matching label with one mapping", () => {
    board("backlog", { backlog: [backlog] });

    expect(screen.getByTitle("type: enhancement")).toHaveClass("chip-ok");
    expect(screen.getByTitle("difficulty: complex")).toHaveClass("chip-fail");
    expect(screen.getByTitle("impact: high")).toHaveClass("chip-ok");
  });

  it("renders an unclassified axis as an absence rather than a coloured value", () => {
    board("backlog", {
      backlog: [{ ...backlog, issue_type: null, difficulty: null, impact: null }],
    });

    expect(screen.queryByTitle(/^type:/)).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("gives a taxonomy label chip the same tone as its column", () => {
    board("inflight", {
      pending: [{
        issue: 9,
        title: "Waiting to start",
        url: null,
        priority: null,
        labels: ["type:bug", "ai-ready"],
        held: false,
        hold_reason: null,
        overlap: null,
      }],
    });

    expect(screen.getByText("type:bug")).toHaveClass("chip-fail");
    expect(screen.getByText("ai-ready")).toHaveClass("chip-neutral");
  });
});
