import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  BacklogCard,
  DoneCard,
  IssueCard,
  IssuesPayload,
} from "../api/types";
import type { Polled } from "../api/hooks";
import { IssuesTab } from "./IssuesTab";

const done: DoneCard = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/o/r/issues/50",
  pr: 75,
  pr_url: "https://github.com/o/r/pull/75",
  merged_ts: "2026-07-29T08:00:00Z",
  cost_usd: 2,
  legs: 2,
  review_rounds: 1,
  labels: [],
};

function inflight(issue: number, title: string, over: Partial<IssueCard> = {}): IssueCard {
  return {
    issue, title, url: null, labels: ["ai-ready"], pr: null, pr_url: null, pr_ci_state: null,
    board: { status: "waiting", group: "slot", on: "slot", headline: "Resolve next", reason: "Dispatches when a slot is free.", since_ts: null },
    action: null,
    rail: { current: "resolve", furthest: "resolve", resolve_attempts: 0, review_rounds: 0, ci_runs: 0, steps: 0, live: false },
    history: [], cost_usd: 0, estimated: false, legs: 0, started_ts: null, last_ts: null, leg_running: false,
    ...over,
  };
}

const backlog: BacklogCard = {
  issue: 63,
  title: "The board is hard to read",
  url: "https://github.com/o/r/issues/63",
  body: "long body",
  labels: ["type:enhancement", "difficulty:complex"],
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
      slots: { capacity: 3, heavy: 0, light: 0 },
      done: [],
      backlog: [],
      gh_ok: true, merged_partial: false,
      observed_at: "2026-07-29T09:00:00Z",
      notifications: [],
      warnings: [],
      ...payload,
    },
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
    board("done", { done: [{ ...done, merged_ts: null }] });

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

  it("finds an in-flight item by its PR number", async () => {
    const cards = Array.from({ length: 11 }, (_, index) => inflight(
      200 + index,
      index === 6 ? "Item with a pull request" : `Waiting issue ${index}`,
      index === 6 ? { pr: 179, pr_url: "https://github.com/o/r/pull/179" } : {},
    ));
    board("inflight", { in_flight: cards });

    await userEvent.type(
      screen.getByRole("searchbox", { name: "Filter active issues" }),
      "179",
    );

    expect(screen.getByText("Item with a pull request")).toBeVisible();
    expect(screen.getByRole("link", { name: "PR #179" })).toBeVisible();
    expect(screen.queryByText("Waiting issue 0")).not.toBeInTheDocument();
    expect(screen.getByText("1/11")).toBeVisible();
  });
});

describe("page chrome", () => {
  it("omits the redundant Issues page heading and shown-count line", () => {
    board("inflight", { in_flight: [inflight(50, "Explain completed work")] });

    expect(screen.queryByRole("heading", { name: "Issues" })).not.toBeInTheDocument();
    expect(screen.queryByText(/shown$/)).not.toBeInTheDocument();
  });
});

describe("backlog triage taxonomy", () => {
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
