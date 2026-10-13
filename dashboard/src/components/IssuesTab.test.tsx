import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { DoneCard, IssueDetailPayload, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";

const doneCard: DoneCard = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/o/r/issues/50",
  pr: 75,
  pr_url: "https://github.com/o/r/pull/75",
  merged_ts: "2026-07-29T08:00:00Z",
  cost_usd: 2.5,
  legs: 3,
  review_rounds: 2,
  labels: [],
};

const detail: IssueDetailPayload = {
  issue: 50, title: "Explain completed work", url: null, labels: [], board: null, action: null, done: true,
  held_for_human: false, pull_requests: [], cost_usd: 2.5, estimated: false, rail: null, history: [], warnings: [],
  steps: [{
    idx: 0, leg_id: "L1", kind: "RESOLVE", role: "fix", runtime: "codex", state: "ended", outcome: "done",
    started_ts: "2026-07-29T07:00:00Z", ended_ts: "2026-07-29T07:10:00Z", duration_s: 600, cost_usd: 1.5,
    estimated: false, pricing_basis: "provider_reported", end_reason: "normal_return", session_id: "s", usage: null,
    head: null, pr: null, summary: "Wrote the fix and its test.",
  }, {
    idx: 1, leg_id: "L2", kind: "REVIEW", role: "review", runtime: "claude", state: "ended", outcome: "changes_requested",
    started_ts: "2026-07-29T07:20:00Z", ended_ts: "2026-07-29T07:25:00Z", duration_s: 300, cost_usd: 1,
    estimated: false, pricing_basis: "provider_reported", end_reason: "normal_return", session_id: "s2", usage: null,
    head: "abc", pr: 75, verdict: "REQUEST_CHANGES", verdict_excerpt: "Add the missing edge case.",
  }],
};

function polled(data: Partial<IssuesPayload>): Polled<IssuesPayload> {
  return {
    data: { in_flight: [], done: [], backlog: [], gh_ok: true, observed_at: "2026-07-29T09:00:00Z", notifications: [], warnings: [], ...data },
    error: null,
    loading: false,
    updatedAt: 0,
    refresh: vi.fn(),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("IssuesTab", () => {
  it("says the board is unavailable when the control store could not be read, not a guessed board", () => {
    render(<IssuesTab repo="demo" sub="inflight" onOpenIssue={vi.fn()} issues={polled({
      store_error: "control store unavailable: ControlStoreError: control store does not exist: /x/control.db",
    })} />);

    expect(screen.getByText(/Board unavailable — control store unavailable/)).toBeVisible();
    expect(screen.queryByText("GitHub data unavailable")).not.toBeInTheDocument();
  });

  it("shows pre-cutover legs as one money-history total, not legacy leg rows or failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ ...detail, steps: [], carried: { legs: 3, cost_usd: 4.5 } }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )));
    render(<IssuesTab repo="demo" sub="done" issues={polled({ done: [doneCard] })} onOpenIssue={vi.fn()} />);

    await userEvent.click(screen.getByRole("row", { name: /Explain completed work/ }));
    expect(await screen.findByText("3 legs before the cutover · $4.50")).toBeVisible();
    expect(screen.queryByText("failed")).not.toBeInTheDocument();
    expect(screen.queryByText("no legs recorded")).not.toBeInTheDocument();
  });

  it("expands a done row into its legs before opening the full issue, and hides rounds by default", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(detail), {
      status: 200, headers: { "Content-Type": "application/json" },
    })));
    const onOpenIssue = vi.fn();
    const { container } = render(
      <IssuesTab repo="demo" sub="done" issues={polled({ done: [doneCard] })} onOpenIssue={onOpenIssue} />,
    );

    expect(screen.queryByRole("columnheader", { name: /Rounds/ })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Since done/ })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: /Legs/ })).toBeVisible();
    expect(screen.getByText("$2.50")).toBeVisible();
    const row = screen.getByRole("row", { name: /Explain completed work.*#50.*PR #75/ });
    await userEvent.click(row);
    expect(onOpenIssue).not.toHaveBeenCalled();
    expect(await screen.findByText("Wrote the fix and its test.")).toBeVisible();
    expect(screen.getByText("Add the missing edge case.")).toBeVisible();
    expect(screen.getByText("changes requested")).toBeVisible();
    expect(container.querySelector("ol.task-story")).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Open full issue" }));
    expect(onOpenIssue).toHaveBeenCalledWith(50);

    fireEvent.contextMenu(screen.getByRole("columnheader", { name: /Issue \/ PR/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Review rounds" }));
    expect(screen.getByRole("columnheader", { name: /Rounds/ })).toBeVisible();
  });

  it("says while the first GitHub observation is still being read", () => {
    render(<IssuesTab repo="demo" sub="inflight" issues={polled({ observed_at: null })} onOpenIssue={vi.fn()} />);
    expect(screen.getByText("Reading GitHub…")).toBeVisible();
  });

  it("expands formatted backlog context and keeps noisy columns disabled by default", async () => {
    render(
      <IssuesTab
        repo="demo"
        sub="backlog"
        issues={polled({
          backlog: [{
            issue: 63,
            title: "The incident came back",
            url: "https://github.com/o/r/issues/63",
            body: "## What happened\n\n- first symptom\n- second symptom\n\n- [x] mitigated\n\nSee **incident notes** and `logs`.",
            labels: ["bug"],
            age_days: 1,
            comments: 7,
            issue_type: "bug",
            difficulty: "medium",
            impact: "high",
          }],
        })}
        onOpenIssue={vi.fn()}
      />,
    );

    expect(screen.queryByRole("columnheader", { name: "# comments" })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Type/ })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: /Difficulty/ })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: /Impact/ })).toBeVisible();
    expect(screen.getByText("medium")).toBeVisible();
    expect(screen.getByText("high")).toBeVisible();

    await userEvent.click(screen.getByRole("row", { name: /The incident came back/ }));
    // MarkdownBody arrives via React.lazy: on a loaded machine the dynamic
    // import can outlive findBy's default 1s and flake the whole suite.
    expect(
      await screen.findByRole("heading", { name: "What happened" }, { timeout: 10_000 }),
    ).toBeVisible();
    expect(screen.getByText("first symptom")).toBeVisible();
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(screen.getByText("incident notes").tagName).toBe("STRONG");
    expect(screen.getByText("logs").tagName).toBe("CODE");
    expect(screen.getByRole("button", { name: "Promote #63 to ai-ready" })).toBeVisible();
  });

  it("does not toggle a backlog row when its promote button is keyboard-activated", async () => {
    render(
      <IssuesTab
        repo="demo"
        sub="backlog"
        issues={polled({
          backlog: [{
            issue: 63,
            title: "Keyboard-safe controls",
            url: "https://github.com/o/r/issues/63",
            body: "## Context",
            labels: [],
            age_days: 1,
            comments: 0,
          }],
        })}
        onOpenIssue={vi.fn()}
      />,
    );

    const promote = screen.getByRole("button", { name: "Promote #63 to ai-ready" });
    promote.focus();
    await userEvent.keyboard("{Enter}");

    expect(screen.getByRole("dialog")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Context" })).not.toBeInTheDocument();
  });
});

describe("Backlog promotion", () => {
  const backlog = (issue: number, title: string) => ({
    issue, title, url: null, body: null, labels: [], age_days: 1, comments: 0,
  });

  it("lists backlog issues, each promotable to ai-ready after a confirmation", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${String(input)}`);
      return new Response(JSON.stringify({ ok: true, detail: "ok" }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const issues = polled({ backlog: [backlog(71, "Nobody has looked at this")] });
    render(<IssuesTab repo="demo" sub="backlog" issues={issues} onOpenIssue={vi.fn()} />);

    expect(screen.getByText("Nobody has looked at this")).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Promote #71 to ai-ready" }));
    expect(calls).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: /^Promote #71$/ }));

    expect(calls).toEqual(["POST /api/repos/demo/issues/71/promote"]);
    expect(issues.refresh).toHaveBeenCalled();
  });
});
