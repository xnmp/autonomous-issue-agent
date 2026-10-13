import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { BacklogCard, DoneCard, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";
import { paginate } from "./ui/Pager";

const done = (issue: number): DoneCard => ({
  issue, title: `Done item ${issue}`, url: `https://github.com/o/r/issues/${issue}`, pr: 1, pr_url: null,
  merged_ts: new Date(Date.UTC(2026, 0, 1, 0, 0, 0) + issue * 1000).toISOString(),
  cost_usd: 1, legs: 1, review_rounds: 0, labels: [],
});
const doneRows = (n: number) => Array.from({ length: n }, (_, i) => done(i + 1));

function polled(data: Partial<IssuesPayload>): Polled<IssuesPayload> {
  return {
    data: { in_flight: [], slots: { capacity: 3, heavy: 0, light: 0 }, done: [], backlog: [], gh_ok: true, merged_partial: false, observed_at: "2026-07-29T09:00:00Z", notifications: [], warnings: [], ...data },
    error: null, loading: false, updatedAt: 0, refresh: vi.fn(),
  };
}

const view = (cards: DoneCard[]) => (
  <IssuesTab repo="demo" sub="done" onOpenIssue={vi.fn()} issues={polled({ done: cards })} />
);
// Default sort is newest first, so issue N is at the top when N is highest.
const next = () => screen.getAllByRole("button", { name: "Next" })[0]!;
const bodyRows = () => screen.getAllByRole("row").slice(1);

describe("IssuesTab pagination", () => {
  it("shows 50 rows, then the next 50 with a matching range, then the remainder", async () => {
    render(view(doneRows(120)));
    expect(bodyRows()).toHaveLength(50);
    expect(screen.getAllByText("1–50 of 120").length).toBeGreaterThan(0);
    expect(screen.getByText("Done item 120")).toBeVisible();

    await userEvent.click(next());
    expect(bodyRows()).toHaveLength(50);
    expect(screen.getAllByText("51–100 of 120").length).toBeGreaterThan(0);
    expect(screen.getByText("Done item 70")).toBeVisible();
    expect(screen.queryByText("Done item 120")).not.toBeInTheDocument();

    await userEvent.click(next());
    expect(bodyRows()).toHaveLength(20);
    expect(screen.getAllByText("101–120 of 120").length).toBeGreaterThan(0);
    for (const next of screen.getAllByRole("button", { name: "Next" })) expect(next).toBeDisabled();
  });

  it("disables Prev on the first page", () => {
    render(view(doneRows(120)));
    for (const prev of screen.getAllByRole("button", { name: "Prev" })) expect(prev).toBeDisabled();
  });

  it("returns to page 1 when the filter changes", async () => {
    render(view(doneRows(150)));
    await userEvent.click(next());
    expect(screen.getAllByText("51–100 of 150").length).toBeGreaterThan(0);
    await userEvent.type(screen.getByRole("searchbox"), "Done item 1");
    // 1, 10-19 and 100-150 match: 62 rows.
    expect(screen.getAllByText("1–50 of 62").length).toBeGreaterThan(0);
    expect(bodyRows()).toHaveLength(50);
  });

  it("clamps to the last page when a refresh shrinks the list", async () => {
    const { rerender } = render(view(doneRows(120)));
    await userEvent.click(next());
    await userEvent.click(next());
    rerender(view(doneRows(60)));
    expect(screen.getAllByText("51–60 of 60").length).toBeGreaterThan(0);
    expect(bodyRows()).toHaveLength(10);
  });

  it("hides the controls when everything fits on one page", () => {
    render(view(doneRows(50)));
    expect(bodyRows()).toHaveLength(50);
    expect(screen.queryByRole("navigation", { name: "Pagination" })).not.toBeInTheDocument();
  });

  it("shows the empty state and no controls for an empty list", () => {
    render(view([]));
    expect(screen.getByText(/nothing merged/)).toBeVisible();
    expect(screen.queryByRole("navigation", { name: "Pagination" })).not.toBeInTheDocument();
  });

  it("remembers the page per sub-tab across switching", async () => {
    const issues = polled({ done: doneRows(120), backlog: [] as BacklogCard[] });
    const { rerender } = render(<IssuesTab repo="demo" sub="done" onOpenIssue={vi.fn()} issues={issues} />);
    await userEvent.click(next());
    rerender(<IssuesTab repo="demo" sub="backlog" onOpenIssue={vi.fn()} issues={issues} />);
    rerender(<IssuesTab repo="demo" sub="done" onOpenIssue={vi.fn()} issues={issues} />);
    expect(screen.getAllByText("51–100 of 120").length).toBeGreaterThan(0);
  });
});

describe("paginate", () => {
  it("clamps out-of-range and malformed page requests", () => {
    expect(paginate([1, 2, 3], 99, 2).rows).toEqual([3]);
    expect(paginate([1, 2, 3], -4, 2).rows).toEqual([1, 2]);
    expect(paginate([1, 2, 3], Number.NaN, 2).page).toBe(0);
    expect(paginate([], 5)).toMatchObject({ rows: [], page: 0, pages: 1, from: 0, to: 0 });
  });
});
