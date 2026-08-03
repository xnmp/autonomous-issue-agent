import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { IssueCard, IssuesPayload } from "../api/types";
import { IssuesTab } from "./IssuesTab";

/**
 * Which clock a row ticks is decided by what the SERVER says is happening, not
 * by whether a human action happens to be posted. keifu #142/#181
 * (2026-08-03): the fold believed rows were human-owned that were not, and the
 * board froze both clocks on issues that were visibly spending money.
 */

const base: IssueCard = {
  issue: 142,
  title: "A leg is still running on this one",
  url: "https://github.com/o/r/issues/142",
  labels: [],
  pr: 181,
  pr_url: "https://github.com/o/r/pull/181",
  pr_state: "open",
  stage: "resolving",
  done: false,
  steps: [{ kind: "RESOLVE", state: "running", status: null, cost_usd: 1, count: 1 }],
  cost_usd: 1,
  estimated: false,
  projected_cost_usd: 2,
  projected_model: "gpt-5.6-terra",
  started_ts: "2026-08-03T07:00:00Z",
  last_ts: "2026-08-03T08:00:00Z",
  elapsed_s: 600,
  working_s: 100,
  idle_s: 500,
  idle_active: false,
  leg_running: true,
  projected_eta_s: 900,
  models: ["gpt-5.6-terra"],
  model_badges: [{ model: "gpt-5.6-terra", effort: "medium" }],
  rounds: 1,
  flags: [],
  issue_verification: null,
  pr_verification: null,
};

function board(cards: IssueCard[]) {
  const issues: Polled<IssuesPayload> = {
    data: {
      in_flight: cards,
      pending: [],
      done: [],
      backlog: [],
      gh_ok: true,
      notifications: [],
      warnings: [],
    },
    error: null,
    loading: false,
    updatedAt: Date.now(),
    refresh: vi.fn(),
  };
  return render(
    <IssuesTab repo="demo" sub="inflight" issues={issues} onOpenIssue={vi.fn()} />,
  );
}

const workClock = () =>
  screen.getByTitle("time actively progressing through harness work");
const idleClock = () =>
  screen.getByTitle("time waiting on a dependency, hold, or human input");

function advance(seconds: number): void {
  act(() => {
    vi.advanceTimersByTime(seconds * 1000);
  });
}

describe("live clocks", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-03T08:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps the work clock moving on a held row whose leg is still running", () => {
    // The exact shape of the bug: the fold posted a human action from a stale
    // journal block while a paid leg was executing.
    board([{
      ...base,
      human_action: "awaiting-reply",
      flags: ["awaiting-reply"],
      stage: "awaiting human reply",
    }]);

    expect(workClock()).toHaveTextContent("work 1:40");
    advance(30);
    expect(workClock()).toHaveTextContent("work 2:10");
    expect(idleClock()).toHaveTextContent("idle 8:20");
  });

  it("ticks idle, and only idle, while the row waits with no leg running", () => {
    board([{ ...base, leg_running: false, idle_active: true, stage: "awaiting review" }]);

    advance(30);
    expect(idleClock()).toHaveTextContent("idle 8:50");
    expect(workClock()).toHaveTextContent("work 1:40");
  });

  it("freezes both clocks on a quiescent human-owned row", () => {
    board([{
      ...base,
      leg_running: false,
      idle_active: false,
      human_action: "escalated",
      flags: ["escalated"],
    }]);

    advance(120);
    expect(workClock()).toHaveTextContent("work 1:40");
    expect(idleClock()).toHaveTextContent("idle 8:20");
  });

  it("freezes both clocks on a finished row", () => {
    board([{ ...base, done: true, leg_running: false, idle_active: false, stage: "done" }]);

    advance(120);
    expect(workClock()).toHaveTextContent("work 1:40");
    expect(idleClock()).toHaveTextContent("idle 8:20");
  });

  it("renders absent durations as absent and fractional ones without noise", () => {
    board([
      { ...base, issue: 1, working_s: null, idle_s: null, elapsed_s: null },
      { ...base, issue: 2, working_s: 100.4, idle_s: 500.6 },
    ]);

    advance(30);
    const clocks = screen.getAllByTitle("time actively progressing through harness work");
    expect(clocks[0]).toHaveTextContent("work —");
    expect(clocks[1]).toHaveTextContent("work 2:10");
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
  });

  it("survives a payload that predates leg_running without inventing work", () => {
    // A card from an older daemon carries no leg_running at all: an idle row
    // must still tick idle, and nothing may read as NaN.
    const legacy = { ...base } as IssueCard;
    delete (legacy as { leg_running?: boolean }).leg_running;
    board([{ ...legacy, idle_active: true }]);

    advance(30);
    expect(idleClock()).toHaveTextContent("idle 8:50");
    expect(workClock()).toHaveTextContent("work 1:40");
  });
});
