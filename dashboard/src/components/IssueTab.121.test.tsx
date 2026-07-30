import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { IssueTab } from "./IssueTab";

describe("blocked issue detail", () => {
  it("shows the persisted human-input reason in the issue header", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            issue: 121,
            title: "Clarify the API contract",
            url: null,
            stage: "awaiting human reply",
            attention_reason: "Choose whether retries use endpoint A or endpoint B.",
            done: false,
            prs: [],
            pull_requests: [],
            cost_usd: 1,
            elapsed_s: 120,
            working_s: 60,
            idle_s: 60,
            idle_active: true,
            steps: [],
            events: [],
            warnings: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    render(<IssueTab repo="demo" issue={121} />);

    expect(
      await screen.findByText("Choose whether retries use endpoint A or endpoint B."),
    ).toBeVisible();
    expect(screen.getByText("awaiting human reply")).toBeVisible();
  });
});
