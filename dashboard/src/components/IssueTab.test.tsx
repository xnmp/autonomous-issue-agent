import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IssueDetailPayload, IssueStep } from "../api/types";
import { IssueTab } from "./IssueTab";

function leg(idx: number, over: Partial<IssueStep> = {}): IssueStep {
  return {
    idx, leg_id: `L${idx}`, kind: "RESOLVE", role: "fix", runtime: "codex", state: "ended", outcome: "done",
    started_ts: `2026-07-29T08:${String(idx).padStart(2, "0")}:00Z`, ended_ts: null, duration_s: 60, cost_usd: 0.2,
    estimated: false, pricing_basis: "provider_reported", end_reason: "normal_return", session_id: `session-${idx}`,
    usage: null, head: null, pr: null, ...over,
  };
}

function detail(over: Partial<IssueDetailPayload> = {}): IssueDetailPayload {
  return {
    issue: 50, title: "An issue", url: "https://github.com/o/r/issues/50", labels: [], board: null, action: null,
    done: false, held_for_human: false, pull_requests: [], cost_usd: 2, estimated: false,
    rail: { current: "resolve", furthest: "resolve", resolve_attempts: 1, review_rounds: 0, ci_runs: 0, steps: 1, live: false },
    history: [], steps: [], warnings: [], ...over,
  };
}

function serve(body: IssueDetailPayload) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => new Response(
    JSON.stringify(init?.method === "POST" ? { ok: true, detail: "ok" } : body),
    { status: 200, headers: { "Content-Type": "application/json" } },
  ));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("IssueTab", () => {
  it("says once while the first GitHub observation is still being read, and not after", async () => {
    serve(detail({ observed_at: null }));
    const first = render(<IssueTab repo="demo" issue={50} />);
    expect(await screen.findAllByText("Reading GitHub…")).toHaveLength(1);
    first.unmount();
    serve(detail({ observed_at: "2026-08-01T08:00:00Z" }));
    render(<IssueTab repo="demo" issue={50} />);
    await screen.findByText("An issue");
    expect(screen.queryByText("Reading GitHub…")).toBeNull();
  });

  it("shows CI activity on the open PR beside the issue's status", async () => {
    serve(detail({
      board: { status: "working", group: "ci", activity: "ci", act: null, headline: "CI running", reason: "Checks are running on the PR head.", since_ts: null },
      pull_requests: [{ number: 75, url: null, state: "open", ci_state: "pending" }],
    }));
    render(<IssueTab repo="ci-demo" issue={50} />);

    expect(await screen.findByText("CI running", { selector: ".chip" })).toHaveClass("chip-running", "pulse");
    expect(screen.getByText("CI & GitHub")).toBeVisible();
    expect(screen.getByText("CI running: Checks are running on the PR head.")).toBeVisible();
  });

  it("links both issue reference and PR badge to GitHub in new tabs", async () => {
    serve(detail({
      done: true,
      pull_requests: [{ number: 75, url: "https://github.com/o/r/pull/75", state: "merged" }],
    }));
    render(<IssueTab repo="o/r" issue={50} />);

    const pr = await screen.findByRole("link", { name: /PR #75/ });
    expect(pr).toHaveAttribute("href", "https://github.com/o/r/pull/75");
    const issue = screen.getByRole("link", { name: "#50" });
    expect(issue).toHaveAttribute("href", "https://github.com/o/r/issues/50");
    expect(issue).toHaveAttribute("target", "_blank");
    expect(issue).toHaveAttribute("rel", "noopener");
    expect(screen.getAllByText("done")).toHaveLength(1);
    expect(screen.getByText("no legs recorded for this issue yet")).toBeVisible();
  });

  it("shows a stop as Needs you, with what releases it and the stop comment as text", async () => {
    serve(detail({
      board: {
        status: "needs_you", group: "needs_you", reason: "question", headline: "Answer the agent's question",
        hint: "Reply on GitHub with the answer; the agent resumes with it.",
        detail: "**needs-human**: Should retries use endpoint A or B?", target: "issue", since_ts: null,
      },
      action: "open",
      held_for_human: true,
    }));
    render(<IssueTab repo="demo" issue={50} />);

    expect(await screen.findByText("Needs you")).toHaveClass("chip-fail");
    expect(screen.getByText(/Reply on GitHub with the answer/)).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("needs-human: Should retries use endpoint A or B?");
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it("unblocks a held issue as one confirmed label move", async () => {
    const fetchMock = serve(detail({ held_for_human: true }));
    render(<IssueTab repo="demo" issue={50} />);

    await userEvent.click(await screen.findByRole("button", { name: "Unblock" }));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Unblock #50" }));

    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts.map(([input]) => String(input))).toEqual(["/api/repos/demo/issues/50/unblock"]);
  });

  it("offers no unblock for an issue nobody holds, or one that is done", async () => {
    serve(detail({ steps: [leg(0)] }));
    const { unmount } = render(<IssueTab repo="demo" issue={50} />);
    expect(await screen.findByRole("button", { name: "Resolve" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Unblock" })).not.toBeInTheDocument();
    unmount();

    serve(detail({ done: true, held_for_human: true, steps: [leg(0)] }));
    render(<IssueTab repo="demo" issue={50} />);
    expect(await screen.findByRole("button", { name: "Resolve" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Unblock" })).not.toBeInTheDocument();
  });

  it("puts each leg's report on its own line and keeps the expanded detail in its spanning cell", async () => {
    serve(detail({
      steps: [leg(0, {
        kind: "REVIEW", role: "review", runtime: "claude", outcome: "changes_requested", pr: 75, head: "abc1234def5678",
        verdict: "REQUEST_CHANGES", verdict_excerpt: "<script>reviewer text is not executable</script>",
        pricing_basis: "estimate", estimated: true,
      })],
    }));
    render(<IssueTab repo="demo" issue={50} />);

    const disclosure = await screen.findByRole("button", { name: "Review" });
    expect(screen.getByText("<script>reviewer text is not executable</script>", { selector: ".task-note" })).toBeVisible();
    await userEvent.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "true");

    const stepDetail = document.querySelector(".stepdetail") as HTMLElement;
    expect(stepDetail.closest("td")).toHaveAttribute("colspan", "8");
    expect(within(stepDetail).getByText("untrusted")).toBeVisible();
    expect(document.querySelector("script")).not.toBeInTheDocument();
    const facts = within(stepDetail).getByRole("group", { name: "Leg execution details" });
    expect(within(facts).getByText("claude")).toBeVisible();
    expect(within(facts).getByText("estimate · estimated")).toBeVisible();
    expect(within(facts).getByText("#75")).toBeVisible();
    expect(within(facts).getByText("abc1234def56")).toBeVisible();

    within(stepDetail).getByRole("tab", { name: "Summary" }).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(within(stepDetail).getByRole("tab", { name: "Raw" })).toHaveFocus();
  });

  it("expands from a click anywhere on the row", async () => {
    serve(detail({ steps: [leg(0, { duration_s: 142, summary: "Wrote the fix." })] }));
    render(<IssueTab repo="demo" issue={50} />);

    await screen.findByRole("button", { name: "Resolve" });
    await userEvent.click(screen.getByText("2:22"));
    expect(screen.getByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByText("Wrote the fix.")).toHaveLength(2);
  });

  it("sorts by runtime and keeps compact resize floors", async () => {
    serve(detail({
      steps: [leg(0, { runtime: "codex", duration_s: 4_638 }), leg(1, { kind: "REVIEW", runtime: "claude", outcome: "passed", duration_s: 4_638 })],
    }));
    render(<IssueTab repo="demo" issue={50} showDurationSeconds={false} />);

    const header = await screen.findByRole("columnheader", { name: /Runtime/ });
    await userEvent.click(within(header).getByRole("button", { name: "Sort by Runtime / model" }));
    expect(header).toHaveAttribute("aria-sort", "ascending");
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByText("claude")).toBeVisible();
    expect(within(rows[1]!).getByText("codex")).toBeVisible();

    expect(screen.getByRole("separator", { name: "Resize Operation column" })).toHaveAttribute("aria-valuemin", "142");
    expect(screen.getByRole("separator", { name: "Resize Outcome column" })).toHaveAttribute("aria-valuemin", "96");
    expect(screen.getByRole("separator", { name: "Resize Cost column" })).toHaveAttribute("aria-valuemin", "76");
    expect(screen.getAllByText("1:17")).toHaveLength(2);
  });

  it("filters a long leg history across runtime and report text", async () => {
    serve(detail({
      steps: Array.from({ length: 11 }, (_, idx) => leg(idx, idx === 8
        ? { kind: "REVIEW", runtime: "claude", outcome: "changes_requested", verdict_excerpt: "needle in the review" }
        : {})),
    }));
    render(<IssueTab repo="demo" issue={50} />);

    const filter = await screen.findByRole("searchbox", { name: "Filter legs" });
    expect(screen.getByText("11/11")).toBeVisible();
    await userEvent.type(filter, "claude");

    expect(screen.getByText("needle in the review")).toBeVisible();
    expect(screen.getByText("1/11")).toBeVisible();
  });

  it("shows each leg's runtime with its model and its pricing basis", async () => {
    serve(detail({
      steps: [
        leg(0, { model: "gpt-6.1-sol", pricing_basis: "configured_rate_card" }),
        leg(1, { state: "running", outcome: "running", pricing_basis: null, model: null, estimated: true }),
      ],
    }));
    render(<IssueTab repo="demo" issue={50} />);

    const rows = (await screen.findAllByRole("row")).slice(1);
    expect(rows[0]).toHaveTextContent("codex · gpt-6.1-sol");
    expect(rows[0]).toHaveTextContent("configured rate card");
    expect(rows[1]).not.toHaveTextContent("·");
    expect(rows[1]).toHaveTextContent("reserved");
  });

  it("shows the issue's triage as plain text, with the suspected duplicate called out", async () => {
    serve(detail({
      triage: {
        tier: "high",
        acceptance_criteria: ["Retries stop after 3 attempts", "**Bold** is not rendered"],
        duplicate_of: 12,
        duplicate_reason: "Same crash in the retry loop",
        rationale: "Touches the scheduler",
      },
      steps: [leg(0, { tier: "xhigh" })],
    }));
    render(<IssueTab repo="demo" issue={50} />);

    const triage = await screen.findByRole("region", { name: "Triage" });
    expect(triage).toHaveTextContent("tier: high");
    expect(triage).toHaveTextContent("Touches the scheduler");
    expect(within(triage).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Retries stop after 3 attempts", "**Bold** is not rendered",
    ]);
    expect(triage).toHaveTextContent("Suspected duplicate of #12: Same crash in the retry loop");
    expect((await screen.findAllByRole("row"))[1]).toHaveTextContent("tier: xhigh");
  });

  it("omits the triage section, and the duplicate line, when there is nothing to show", async () => {
    serve(detail({ triage: null, steps: [leg(0)] }));
    render(<IssueTab repo="demo" issue={50} />);
    await screen.findAllByRole("row");
    expect(screen.queryByRole("region", { name: "Triage" })).not.toBeInTheDocument();

    vi.unstubAllGlobals();
    serve(detail({ triage: { tier: "low", acceptance_criteria: [], duplicate_of: null }, steps: [leg(0)] }));
    render(<IssueTab repo="demo" issue={51} />);
    const triage = await screen.findByRole("region", { name: "Triage" });
    expect(triage).not.toHaveTextContent("duplicate");
  });

  it("states pre-cutover legs as one total and counts them in the header", async () => {
    serve(detail({ steps: [leg(0)], carried: { legs: 8, cost_usd: 1.7 } }));
    render(<IssueTab repo="demo" issue={50} />);

    expect(await screen.findByText(/8 legs before the cutover/)).toHaveTextContent("$1.70");
    expect(screen.getByText("legs").parentElement).toHaveTextContent("legs 9");
    expect(screen.getAllByRole("row")).toHaveLength(2);
  });
});
