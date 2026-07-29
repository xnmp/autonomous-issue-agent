import { render, screen } from "@testing-library/react";
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
    expect(screen.getByText("Runs", { selector: ".section-title span" })).toBeVisible();
    expect(screen.queryByText(/one row per leg/i)).not.toBeInTheDocument();
    expect(screen.getByText("no runs recorded for this issue yet")).toBeVisible();
  });
});
