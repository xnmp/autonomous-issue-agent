import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { IssueTab } from "./IssueTab";

describe("IssueTab", () => {
  it("links the PR badge to GitHub and omits the redundant issue link", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            issue: 50,
            title: "Done issue",
            url: "https://github.com/o/r/issues/50",
            stage: "done",
            done: true,
            prs: [75],
            pull_requests: [
              { number: 75, url: "https://github.com/o/r/pull/75", state: "merged" },
            ],
            cost_usd: 2,
            elapsed_s: 60,
            working_s: 45,
            idle_s: 15,
            idle_active: false,
            steps: [],
            events: [],
            warnings: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    render(<IssueTab repo="demo" issue={50} />);

    const pr = await screen.findByRole("link", { name: /PR #75/ });
    expect(pr).toHaveAttribute("href", "https://github.com/o/r/pull/75");
    expect(screen.queryByText(/issue on GitHub/i)).not.toBeInTheDocument();
    expect(screen.getAllByText("done")).toHaveLength(1);
    expect(screen.getByRole("tab", { name: /Tasks/ })).toBeVisible();
    expect(screen.getByRole("tab", { name: /Events/ })).toBeVisible();
    expect(screen.queryByText(/one row per leg/i)).not.toBeInTheDocument();
    expect(screen.getByText("no tasks recorded for this issue yet")).toBeVisible();
  });

  it("keeps the expanded task detail in its spanning table cell", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const url = String(input);
        const body = url.includes("/steps/0/events")
          ? { events: [], warnings: [] }
          : {
              issue: 50,
              title: "Task detail layout",
              url: null,
              stage: "done",
              done: true,
              prs: [],
              pull_requests: [],
              cost_usd: 2,
              elapsed_s: 60,
              working_s: 45,
              idle_s: 15,
              idle_active: false,
              steps: [{
                idx: 0,
                kind: "RESOLVE",
                role: "fix",
                state: "ok",
                model: "claude-sonnet-5",
                effort: "high",
                runtime: "codex",
                status: "done",
                exit_reason: "ok",
                duration_s: 60,
                cost_usd: 2,
                estimated: false,
                pricing_basis: "configured_rate_card",
                budget_cap_usd: 10,
                escalation: {
                  notches: 1,
                  from_effort: "medium",
                  to_effort: "high",
                  after_review_round: 2,
                },
                change: {
                  pr: 75,
                  branch: "feat/50-detail",
                  head_sha: "abc1234",
                  commit_subjects: [
                    "red: reproduce empty summary",
                    "green: expose execution facts",
                  ],
                  files_changed_count: 4,
                },
                session_id: "session-50",
                has_transcript: false,
                turns: 3,
                usage: null,
                ts: "2026-07-29T08:00:00Z",
              }],
              events: [],
              warnings: [],
            };
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(<IssueTab repo="demo" issue={50} />);

    const disclosure = await screen.findByRole("button", { name: "Resolve" });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "true");

    const stepDetail = document.querySelector(".stepdetail");
    expect(stepDetail).toBeInstanceOf(HTMLElement);
    const detailEventsTab = within(stepDetail as HTMLElement).getByRole("tab", { name: "Events" });
    const detail = detailEventsTab.closest("td");
    expect(detail).toHaveAttribute("colspan", "7");
    expect(detail).not.toHaveClass("step-operation");
    expect(screen.queryByRole("columnheader", { name: /Role/ })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Model/ })).toBeVisible();
    const executionDetails = within(stepDetail as HTMLElement).getByRole("group", {
      name: "Task execution details",
    });
    expect(within(executionDetails).getByText("claude-sonnet-5")).toBeVisible();
    expect(within(executionDetails).getByText("high")).toBeVisible();
    expect(within(executionDetails).getByText("codex")).toBeVisible();
    expect(within(executionDetails).getByText("+1 medium→high after r2")).toBeVisible();
    expect(within(executionDetails).getByText("session-50")).toBeVisible();
    expect(within(executionDetails).queryByText("ok")).not.toBeInTheDocument();
    expect(within(stepDetail as HTMLElement).getByText("leg $2.00 / $10.00 cap")).toBeVisible();
    expect(within(stepDetail as HTMLElement).getByText("configured rate card")).toBeVisible();
    expect(within(stepDetail as HTMLElement).getByText("pushed feat/50-detail · 4 files changed"))
      .toBeVisible();
    expect(within(stepDetail as HTMLElement).getByText("reproduce empty summary")).toBeVisible();
    expect(within(stepDetail as HTMLElement).getByText("expose execution facts")).toBeVisible();

    detailEventsTab?.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Cost chart" })).toHaveFocus();
    expect(within(stepDetail as HTMLElement).getByRole("tabpanel")).toHaveAccessibleName(
      "Cost chart",
    );
  });

  it("renders requested changes as bounded untrusted text with round context", async () => {
    const verdict = "<script>operator input is not executable</script>";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const body = String(input).includes("/steps/0/events")
          ? { events: [], warnings: [] }
          : {
              issue: 50,
              title: "Review substance",
              url: null,
              stage: "changes requested",
              done: false,
              prs: [75],
              pull_requests: [],
              cost_usd: 0.4,
              elapsed_s: 60,
              working_s: 45,
              idle_s: 15,
              idle_active: false,
              steps: [{
                idx: 0,
                kind: "RE_REVIEW",
                role: "review",
                state: "ok",
                model: "gpt-5.6-terra",
                status: "REQUEST_CHANGES",
                exit_reason: "ok",
                duration_s: 60,
                cost_usd: 0.4,
                estimated: true,
                pricing_basis: "learned_estimate",
                budget_cap_usd: 8,
                session_id: "review-50",
                has_transcript: false,
                turns: 3,
                usage: null,
                ts: "2026-07-29T08:00:00Z",
                note: "REQUEST_CHANGES round 2",
                review: {
                  verdict_excerpt: verdict,
                  rejection: 2,
                  unproductive_rounds: 0,
                  unproductive_cap: 5,
                },
              }],
              events: [],
              warnings: [],
            };
        return Promise.resolve(new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }),
    );

    render(<IssueTab repo="demo" issue={50} />);
    await userEvent.click(await screen.findByRole("button", { name: "Re-review" }));

    expect(screen.getByText("untrusted")).toBeVisible();
    expect(screen.getByText(verdict)).toBeVisible();
    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect(screen.getByText("change-request verdicts observed 2")).toBeVisible();
    expect(screen.getByText("unproductive rounds 0/5")).toBeVisible();
    expect(screen.getByText("leg $0.40 / $8.00 cap")).toBeVisible();
    expect(screen.getByText("learned estimate · estimated")).toBeVisible();
  });

  it("expands from a click anywhere on the row and leads with a human summary", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const url = String(input);
        const body = url.includes("/steps/0/events")
          ? {
              events: [
                { ts: "2026-08-02T13:55:00Z", event: "action",
                  desc: "relabel issue #34: +difficulty:medium +impact:high" },
                { ts: "2026-08-02T13:55:01Z", kind: "control.transition.committed.v1" },
              ],
              warnings: [],
            }
          : {
              issue: 34,
              title: "Triaged issue",
              url: null,
              stage: "done",
              done: true,
              prs: [],
              pull_requests: [],
              cost_usd: 1,
              elapsed_s: 60,
              working_s: 45,
              idle_s: 15,
              idle_active: false,
              steps: [{
                idx: 0,
                kind: "TRIAGE",
                role: "steward",
                state: "ok",
                model: "gpt-5.6-terra",
                status: "done",
                exit_reason: "ok",
                duration_s: 142,
                cost_usd: 0.75,
                estimated: false,
                session_id: null,
                has_transcript: false,
                turns: 14,
                usage: null,
                ts: "2026-08-02T13:55:00Z",
                note: null,
              }],
              events: [],
              warnings: [],
            };
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(<IssueTab repo="demo" issue={34} />);

    // Click the status cell, not the disclosure chevron — the whole row toggles.
    await screen.findByRole("button", { name: "Triage" });
    await userEvent.click(screen.getByText("2:22"));
    // The Summary pane is the default and narrates what the task DID —
    // the steward's triage labels — not the raw event soup.
    expect(
      await screen.findByText("relabel issue #34: +difficulty:medium +impact:high"),
    ).toBeVisible();
    expect(screen.getByRole("tab", { name: "Summary" })).toHaveAttribute(
      "aria-selected", "true",
    );
    expect(screen.queryByText("control.transition.committed.v1")).not.toBeInTheDocument();
  });

  it("shows a sortable model column by default and keeps compact resize floors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({
          issue: 50,
          title: "Compact columns",
          url: null,
          stage: "done",
          done: true,
          prs: [],
          pull_requests: [],
          cost_usd: 2,
          elapsed_s: 4_638,
          working_s: 4_638,
          idle_s: 0,
          idle_active: false,
          steps: [{
            idx: 0,
            kind: "RESOLVE",
            role: "fix",
            state: "ok",
            model: "claude-sonnet-5",
            status: "done",
            exit_reason: "ok",
            duration_s: 4_638,
            cost_usd: 2,
            estimated: false,
            session_id: null,
            has_transcript: false,
            turns: 3,
            usage: null,
            ts: "2026-07-29T08:00:00Z",
          }, {
            idx: 1,
            kind: "REVIEW",
            role: "review",
            state: "ok",
            model: "gpt-5.6-luna",
            status: "APPROVE",
            exit_reason: "ok",
            duration_s: 60,
            cost_usd: 0.2,
            estimated: false,
            session_id: null,
            has_transcript: false,
            turns: 2,
            usage: null,
            ts: "2026-07-29T08:01:00Z",
          }],
          events: [],
          warnings: [],
        }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    render(<IssueTab repo="demo" issue={50} showDurationSeconds={false} />);
    const modelHeader = await screen.findByRole("columnheader", { name: /Model/ });
    await userEvent.click(within(modelHeader).getByRole("button", { name: "Sort by Model" }));
    expect(modelHeader).toHaveAttribute("aria-sort", "ascending");
    await userEvent.click(within(modelHeader).getByRole("button", { name: "Sort by Model" }));
    expect(modelHeader).toHaveAttribute("aria-sort", "descending");
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByText("gpt-5.6-luna")).toBeVisible();
    expect(within(rows[1]!).getByText("sonnet-5")).toBeVisible();

    expect(screen.getByRole("separator", { name: "Resize Model column" }))
      .toHaveAttribute("aria-valuemin", "100");
    expect(screen.getByRole("separator", { name: "Resize Operation column" }))
      .toHaveAttribute("aria-valuemin", "142");
    expect(screen.getByRole("separator", { name: "Resize Status column" }))
      .toHaveAttribute("aria-valuemin", "96");
    expect(screen.getByRole("separator", { name: "Resize Duration column" }))
      .toHaveAttribute("aria-valuemin", "84");
    expect(screen.getByRole("separator", { name: "Resize Cost column" }))
      .toHaveAttribute("aria-valuemin", "76");
    expect(screen.getAllByText("1:17")).toHaveLength(2);
  });

  it("filters a long task history across model and task context", async () => {
    const steps = Array.from({ length: 11 }, (_, idx) => ({
      idx,
      kind: idx === 8 ? "REVIEW" : "RESOLVE",
      role: idx === 8 ? "review" : "fix",
      state: "ok",
      model: idx === 8 ? "gpt-5.6-needle" : "gpt-5.6-terra",
      status: idx === 8 ? "REQUEST_CHANGES" : "done",
      exit_reason: "ok",
      duration_s: 60,
      cost_usd: 0.2,
      estimated: false,
      session_id: `session-${idx}`,
      has_transcript: false,
      turns: 2,
      usage: null,
      ts: `2026-07-29T08:${String(idx).padStart(2, "0")}:00Z`,
    }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      issue: 50,
      title: "Long task history",
      url: null,
      stage: "done",
      done: true,
      prs: [],
      pull_requests: [],
      cost_usd: 2,
      elapsed_s: 660,
      working_s: 660,
      idle_s: 0,
      idle_active: false,
      steps,
      events: [],
      warnings: [],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));

    render(<IssueTab repo="demo" issue={50} />);
    const filter = await screen.findByRole("searchbox", { name: "Filter tasks" });
    expect(screen.getByText("11/11")).toBeVisible();
    await userEvent.type(filter, "needle");

    expect(screen.getByText("gpt-5.6-needle")).toBeVisible();
    expect(screen.queryByText("gpt-5.6-terra")).not.toBeInTheDocument();
    expect(screen.getByText("1/11")).toBeVisible();
  });
});
