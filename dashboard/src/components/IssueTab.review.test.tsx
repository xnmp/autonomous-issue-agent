import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkflowFamilies } from "../api/types";
import { IssueTab } from "./IssueTab";
import { controlResponse, inactiveControl } from "./resolve/test-fixtures";
import { reviewAbsent, reviewControl, reviewControlResponse, reviewMigrationRequired, reviewJson } from "./review/test-fixtures";

const detail = {
  issue: 77, title: "Escalated issue", url: null, stage: "escalated", escalation_reason: "review ceiling reached",
  done: false, prs: [12], pull_requests: [{ number: 12, url: null, state: "open" }, { number: 9, url: null, state: "merged" }],
  cost_usd: 4, elapsed_s: 600, working_s: 300, idle_s: 300, idle_active: true, steps: [], events: [], warnings: [],
};

const families = (...active: ("resolve" | "review_rejections")[]): WorkflowFamilies =>
  ({ status: "available", repository: "o/r", active_families: active });
const familyResponse = (value: WorkflowFamilies) => reviewJson(value, value.status === "available" ? 200 : 503);

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

interface Routes {
  review?: (path: string) => Response;
  families?: () => Response;
  issue?: typeof detail;
}

function serve({ review = () => reviewControlResponse(reviewMigrationRequired), families: family = () => familyResponse(families()), issue = detail }: Routes) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), "http://localhost").pathname;
    if (path.endsWith("/resolve-control")) return controlResponse(inactiveControl);
    if (path.endsWith("/workflow-families")) return family();
    if (path.includes("/pulls/")) return review(path);
    return new Response(JSON.stringify(issue), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const reads = (fetchMock: ReturnType<typeof serve>, fragment: string) =>
  fetchMock.mock.calls.map(([input]) => new URL(String(input), "http://localhost").pathname).filter((path) => path.includes(fragment));

describe("linked pull request review control", () => {
  it("mounts a review panel per unmerged linked PR and retires the legacy unblock from the repository's family read", async () => {
    const fetchMock = serve({ review: () => reviewControlResponse(reviewControl()), families: () => familyResponse(families("review_rejections")) });
    render(<IssueTab repo="demo" issue={77} />);
    expect(await screen.findByRole("heading", { name: /Review control PR #12/ })).toBeVisible();
    expect(screen.getByRole("button", { name: "Request re-arm" })).toBeEnabled();
    const legacy = await screen.findByRole("button", { name: "Review legacy unblock" });
    expect(legacy).toBeDisabled();
    await waitFor(() => expect(legacy).toHaveAccessibleDescription(/label-based unblock is retired\. Review holds on PR #12 are shown in its review control below\./));
    // One poll per pull request (its own panel); the refusal reads the repository once, not each pull request again.
    expect(new Set(reads(fetchMock, "/pulls/"))).toEqual(new Set(["/api/repos/demo/pulls/12/review-control"]));
    expect(new Set(reads(fetchMock, "/workflow-families"))).toEqual(new Set(["/api/repos/demo/workflow-families"]));
  });

  it("offers the legacy unblock once the repository reads no family active", async () => {
    serve({});
    render(<IssueTab repo="demo" issue={77} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Review legacy unblock" })).toBeEnabled());
    expect(await screen.findByText(/Review rejection control is not activated/)).toBeVisible();
  });

  it("explains an absent pull request's panel, so the refusal never points at nothing", async () => {
    serve({ review: () => reviewControlResponse(reviewAbsent), families: () => familyResponse(families("review_rejections")) });
    render(<IssueTab repo="demo" issue={77} />);
    const legacy = await screen.findByRole("button", { name: "Review legacy unblock" });
    await waitFor(() => expect(legacy).toHaveAccessibleDescription(/PR #12 are shown in its review control below/));
    expect(legacy).toBeDisabled();
    expect(await screen.findByRole("heading", { name: /Review control PR #12/ })).toBeVisible();
    expect(screen.getByText(/has not recorded a review workflow for this pull request/)).toBeVisible();
  });

  it("disables the legacy unblock for an issue with no unmerged pull request while review control is active", async () => {
    const fetchMock = serve({
      review: () => { throw new Error("no review read expected"); },
      families: () => familyResponse(families("review_rejections")),
      // A distinct issue: the page restores another issue's cached detail under the same number.
      issue: { ...detail, issue: 78, prs: [9], pull_requests: [{ number: 9, url: null, state: "merged" }] },
    });
    render(<IssueTab repo="demo" issue={78} />);
    const legacy = await screen.findByRole("button", { name: "Review legacy unblock" });
    await waitFor(() => expect(legacy).toHaveAccessibleDescription(/retired\. No unmerged pull request is linked to this issue/));
    expect(legacy).toBeDisabled();
    expect(legacy).not.toHaveAccessibleDescription(/PR #/);
    expect(reads(fetchMock, "/pulls/")).toEqual([]);
  });

  it.each<[string, () => Response, RegExp]>([
    ["unavailable", () => familyResponse({ status: "unavailable", reason: "database is locked" }), /unavailable \(database is locked\)/],
    ["unreadable", () => new Response("not json", { status: 500 }), /not confirmed/],
  ])("keeps the legacy unblock disabled while the family status is %s", async (_label, reply, text) => {
    serve({ families: reply });
    render(<IssueTab repo="demo" issue={77} />);
    const legacy = await screen.findByRole("button", { name: "Review legacy unblock" });
    await waitFor(() => expect(legacy).toHaveAccessibleDescription(text));
    expect(legacy).toBeDisabled();
  });

  it("reads no family status for an issue that is not held for a human", async () => {
    const fetchMock = serve({ issue: { ...detail, issue: 79, stage: "reviewing", escalation_reason: null as unknown as string } });
    render(<IssueTab repo="demo" issue={79} />);
    expect(await screen.findByText("Escalated issue")).toBeVisible();
    await waitFor(() => expect(reads(fetchMock, "/pulls/").length).toBeGreaterThan(0));
    expect(reads(fetchMock, "/workflow-families")).toEqual([]);
  });
});
