import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Sidebar } from "./Sidebar";
import type { Polled } from "../api/hooks";
import type { ReposPayload, UsagePayload } from "../api/types";
import type { DashboardOptions } from "./ui/OptionsMenu";

const WIDTH_KEY = "issue-agent.dashboard.sidebar-width";

const polled = <T,>(data: T): Polled<T> => ({
  data,
  error: null,
  loading: false,
  updatedAt: 0,
  refresh: () => {},
});

const repos: ReposPayload = { repos: [], warnings: [], liveness: "known" } as ReposPayload;
const usage: UsagePayload = {
  claude: null,
  codex: null,
  harness: { global_daily_cap_usd: null, today_all_repos_usd: 0, per_repo: [] },
  warnings: [],
} as unknown as UsagePayload;
const options: DashboardOptions = {
  showProjectedCost: false,
  showPerRepoQuotas: false,
  showDurationSeconds: false,
  theme: "cosmic-dusk" as DashboardOptions["theme"],
};

function renderSidebar() {
  return render(
    <Sidebar
      repos={polled(repos)}
      usage={polled(usage)}
      activeRepo={null}
      verificationActive={false}
      options={options}
      onOptionsChange={() => {}}
      onSelectRepo={() => {}}
      onOpenVerification={() => {}}
    />,
  );
}

const resizer = () => screen.getByRole("separator", { name: "Resize sidebar" });

describe("sidebar width", () => {
  it("remembers the width the operator asked for, not a measurement of the rail", async () => {
    renderSidebar();
    resizer().focus();
    await userEvent.keyboard("{ArrowRight}{ArrowRight}");

    expect(resizer()).toHaveAttribute("aria-valuenow", "288");
    expect(localStorage.getItem(WIDTH_KEY)).toBe("288");
  });

  it("keeps the remembered width whole and inside the range the stylesheet supports", async () => {
    renderSidebar();
    resizer().focus();
    // Twenty steps outward asks for far more than the rail may occupy.
    await userEvent.keyboard("{ArrowRight>20/}");

    expect(localStorage.getItem(WIDTH_KEY)).toBe("480");

    await userEvent.keyboard("{ArrowLeft>40/}");
    expect(localStorage.getItem(WIDTH_KEY)).toBe("220");
  });

  it.each([
    ["a fractional width from a zoomed browser", "263.991455078125", "264"],
    ["a width wider than the rail may be", "9000", "480"],
    ["a width narrower than the rail may be", "12", "220"],
    ["a corrupt entry", "not-a-number", "264"],
    ["an empty entry", "", "264"],
  ])("recovers from %s", (_name, stored, expected) => {
    localStorage.setItem(WIDTH_KEY, stored);
    renderSidebar();
    expect(resizer()).toHaveAttribute("aria-valuenow", expected);
  });

  it("starts at the default width when nothing is remembered", () => {
    renderSidebar();
    expect(resizer()).toHaveAttribute("aria-valuenow", "264");
  });
});
