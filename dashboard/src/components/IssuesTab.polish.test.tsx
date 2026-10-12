import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type {
  BacklogCard,
  IssueCard,
  IssuesPayload,
  PendingCard,
} from "../api/types";
import { IssuesTab } from "./IssuesTab";

const done: IssueCard = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/o/r/issues/50",
  labels: [],
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
  leg_running: false,
  models: [],
  model_badges: [],
  rounds: 1,
  issue_verification: "VERIFIED",
  pr_verification: "VERIFIED",
};

const backlog: BacklogCard = {
  issue: 63,
  title: "The board is hard to read",
  url: "https://github.com/o/r/issues/63",
  body: "long body",
  labels: ["type:enhancement", "difficulty:complex", "ai-triage"],
  recurrence: false,
  recurrence_count: 0,
  steward_state: "processed",
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
    expect(screen.getByText(/PR #75/)).toBeVisible();
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

  it("keeps ageing the time-since cell from its own clock", () => {
    // The subscription lives in the cell, not the table: the age must still
    // advance without the whole done list re-rendering every second.
    board("done", { done: [{ ...done, merged_ts: "2026-08-01T07:58:00Z" }] });

    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(screen.getByText("2m")).toBeVisible();

    act(() => {
      vi.advanceTimersByTime(120_000);
    });
    expect(screen.getByText("4m")).toBeVisible();
  });
});

describe("done labels column", () => {
  const tagged = { ...done, labels: ["type:bug", "ai-done"] };

  async function showLabels() {
    await userEvent.pointer({
      keys: "[MouseRight]",
      target: screen.getByRole("columnheader", { name: /Issue \/ PR/ }),
    });
    await userEvent.click(screen.getByRole("menuitemcheckbox", { name: "Labels" }));
  }

  it("stays out of the way until an operator asks for it", () => {
    board("done", { done: [tagged] });

    expect(screen.queryByRole("columnheader", { name: /Labels/ })).not.toBeInTheDocument();
    expect(screen.queryByText("type:bug")).not.toBeInTheDocument();
  });

  it("shows every label as a toned chip once enabled", async () => {
    board("done", { done: [tagged] });

    await showLabels();

    expect(screen.getByRole("columnheader", { name: /Labels/ })).toBeVisible();
    expect(screen.getByText("type:bug")).toHaveClass("chip-fail");
    expect(screen.getByText("ai-done")).toHaveClass("chip-neutral");
  });

  it("renders an unlabelled issue as an absence rather than an empty cell", async () => {
    board("done", { done: [{ ...done, labels: [] }] });

    await showLabels();

    const column = screen
      .getAllByRole("columnheader")
      .findIndex((header) => header.textContent?.includes("Labels"));
    expect(screen.getAllByRole("cell")[column]).toHaveTextContent("—");
  });
});

describe("long-table filtering", () => {
  it("filters a long done table by issue-facing text and reports the result count", async () => {
    const cards = Array.from({ length: 12 }, (_, index) => ({
      ...done,
      issue: 100 + index,
      title: index === 7 ? "Needle migration" : `Routine change ${index}`,
    }));
    board("done", { done: cards });

    const filter = screen.getByRole("searchbox", { name: "Filter done issues" });
    expect(filter).toBeVisible();
    expect(screen.getByText("12/12")).toBeVisible();

    await userEvent.type(filter, "needle");

    expect(screen.getByText("Needle migration")).toBeVisible();
    expect(screen.queryByText("Routine change 0")).not.toBeInTheDocument();
    expect(screen.getByText("1/12")).toBeVisible();
  });

  it("finds a visible parked PR by its number", async () => {
    const pending: PendingCard[] = Array.from({ length: 11 }, (_, index) => ({
      issue: 200 + index,
      title: index === 6 ? "Parked pull request" : `Waiting issue ${index}`,
      url: null,
      body: null,
      priority: null,
      labels: index === 6 ? ["ai-parked"] : ["ai-ready"],
      held: index === 6,
      hold_reason: index === 6 ? "parked" : null,
      overlap: null,
      pr: index === 6 ? 179 : null,
      pr_url: index === 6 ? "https://github.com/o/r/pull/179" : null,
      pr_state: index === 6 ? "open" : null,
      pr_ci_state: index === 6 ? "passing" : null,
    }));
    board("inflight", { pending });

    await userEvent.type(
      screen.getByRole("searchbox", { name: "Filter active issues" }),
      "179",
    );

    expect(screen.getByText("Parked pull request")).toBeVisible();
    expect(screen.getByText("PR #179")).toBeVisible();
    expect(screen.queryByText("Waiting issue 0")).not.toBeInTheDocument();
    expect(screen.getByText("1/11")).toBeVisible();
  });
});

describe("page chrome", () => {
  it("omits the redundant Issues page heading and shown-count line", () => {
    board("inflight", { in_flight: [{ ...done, done: false, merged_ts: null }] });

    expect(screen.queryByRole("heading", { name: "Issues" })).not.toBeInTheDocument();
    expect(screen.queryByText(/shown$/)).not.toBeInTheDocument();
  });
});

describe("backlog triage taxonomy", () => {
  it("keeps steward state explicit without row-level highlighting", () => {
    const { container } = board("backlog", {
      backlog: [
        backlog,
        {
          ...backlog,
          issue: 64,
          title: "Unprocessed intake",
          labels: [],
          steward_state: "raw",
        },
        {
          ...backlog,
          issue: 65,
          title: "Needs clarification",
          steward_state: "blocked_on_human_reply",
        },
      ],
    });

    expect(screen.getByText("AC + test plan written")).toBeVisible();
    expect(screen.getByText("awaiting steward")).toBeVisible();
    expect(screen.getByText("awaiting human reply")).toHaveClass("chip-fail");
    expect(screen.queryByText("steward questions need answers")).not.toBeInTheDocument();
    expect(screen.getAllByText("awaiting human reply")).toHaveLength(1);
    expect(container.querySelector(".backlog-processed")).not.toBeInTheDocument();
    expect(container.querySelector(".backlog-raw")).not.toBeInTheDocument();
    expect(container.querySelector(".backlog-blocked")).not.toBeInTheDocument();
  });

  it("colours each axis value and its matching label with one mapping", () => {
    board("backlog", { backlog: [backlog] });

    expect(document.querySelector('[data-tip="type: enhancement"]')).toHaveClass("chip-ok");
    expect(document.querySelector('[data-tip="difficulty: complex"]')).toHaveClass("chip-fail");
    expect(document.querySelector('[data-tip="impact: high"]')).toHaveClass("chip-ok");
  });

  it("renders an unclassified axis as an absence rather than a coloured value", () => {
    board("backlog", {
      backlog: [{ ...backlog, issue_type: null, difficulty: null, impact: null }],
    });

    expect(document.querySelector('[data-tip^="type:"]')).toBeNull();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
