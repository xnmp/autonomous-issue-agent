import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LifecycleStepper, type RailNode } from "./LifecycleStepper";

describe("LifecycleStepper", () => {
  const rail = (overrides: Partial<Record<string, RailNode>> = {}): RailNode[] => [
    { id: "resolve", label: "Resolve", state: "done", count: null, hot: false, hotTip: null },
    { id: "ci", label: "CI", state: "current", count: "run 3", hot: true, hotTip: "3 or more CI runs: above typical" },
    { id: "review", label: "Review", state: "rewound", count: "2 rounds", hot: false, hotTip: null },
    { id: "merge", label: "Merge", state: "future", count: null, hot: false, hotTip: null },
  ].map((node) => overrides[node.id] ?? node) as RailNode[];

  it("always draws the four stages and names the current one with every count", () => {
    render(<LifecycleStepper rail={rail()} tone="ok" live={false} label="Issue #7 stage" />);

    const list = screen.getByRole("list", { name: "Issue #7 stage: CI, run 3; Review 2 rounds" });
    expect(Array.from(list.querySelectorAll("li")).map((node) => node.textContent)).toEqual([
      "Resolve", "CIrun 3", "Review2 rounds", "Merge",
    ]);
    expect(screen.getByText("CI").closest("li")).toHaveAttribute("aria-current", "step");
  });

  it("explains an amber count and a rewound stage", () => {
    render(<LifecycleStepper rail={rail()} tone="fail" live={false} />);

    expect(screen.getByText("run 3")).toHaveClass("rail-hot");
    expect(screen.getByText("run 3")).toHaveAttribute("data-tip", "3 or more CI runs: above typical");
    expect(screen.getByText("2 rounds")).not.toHaveClass("rail-hot");
    expect(screen.getByText("Review").closest("li")).toHaveClass("rail-rewound");
    expect(screen.getByText("Review").closest("li")).toHaveAttribute("data-tip", "Reached earlier; now back at CI");
  });

  it("pulses the current node only while a leg is running", () => {
    const { rerender } = render(<LifecycleStepper rail={rail()} tone="ok" live />);
    expect(screen.getByText("CI").closest("li")).toHaveClass("rail-live");

    rerender(<LifecycleStepper rail={rail()} tone="ok" live={false} />);
    expect(screen.getByText("CI").closest("li")).not.toHaveClass("rail-live");
  });
});
