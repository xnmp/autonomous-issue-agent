import { fireEvent, render, screen, within } from "@testing-library/react";
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
                status: "done",
                exit_reason: "ok",
                duration_s: 60,
                cost_usd: 2,
                estimated: false,
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
    expect(detail).toHaveAttribute("colspan", "6");
    expect(detail).not.toHaveClass("step-operation");
    expect(screen.queryByRole("columnheader", { name: /Role/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /Model/ })).not.toBeInTheDocument();

    detailEventsTab?.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Cost chart" })).toHaveFocus();
    expect(within(stepDetail as HTMLElement).getByRole("tabpanel")).toHaveAccessibleName(
      "Cost chart",
    );
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

  it("gives compact task columns their own resize floors", async () => {
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
    const operation = await screen.findByRole("columnheader", { name: /Operation/ });
    fireEvent.contextMenu(operation);
    await userEvent.click(screen.getByRole("checkbox", { name: "Model" }));

    expect(screen.getByRole("separator", { name: "Resize Model column" }))
      .toHaveAttribute("aria-valuemin", "100");
    expect(screen.getByRole("separator", { name: "Resize Status column" }))
      .toHaveAttribute("aria-valuemin", "96");
    expect(screen.getByRole("separator", { name: "Resize Duration column" }))
      .toHaveAttribute("aria-valuemin", "84");
    expect(screen.getByRole("separator", { name: "Resize Cost column" }))
      .toHaveAttribute("aria-valuemin", "76");
    expect(screen.getAllByText("1:17")).toHaveLength(2);
  });
});
