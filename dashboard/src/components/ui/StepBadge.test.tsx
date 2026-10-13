import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CiRunningBadge, StepBadge, StepStatusChip } from "./StepBadge";

describe("CI activity", () => {
  it.each(["passing", "failing", null] as const)("disappears when pending becomes %s", (state) => {
    const { rerender } = render(<CiRunningBadge state="pending" hasPullRequest />);
    expect(screen.getByText("CI running")).toBeVisible();
    rerender(<CiRunningBadge state={state} hasPullRequest />);
    expect(screen.queryByText("CI running")).not.toBeInTheDocument();
  });

  it("claims nothing without a pull request", () => {
    render(<CiRunningBadge state="pending" hasPullRequest={false} />);
    expect(screen.queryByText("CI running")).not.toBeInTheDocument();
  });
});

describe("a leg badge", () => {
  it("names the operation and shows folded retries as a multiplier, with the story on hover", () => {
    render(<StepBadge step={{ kind: "FIX_CI", outcome: "failed", count: 3, cost_usd: 1.5 }} />);
    const chip = document.querySelector('[data-tip="Fix CI · 3 attempts · failed · $1.50"]');
    expect(chip).toHaveTextContent("Fix CI×3");
    expect(chip).toHaveClass("chip-fail");
  });

  it("colours a review that requested changes as still-going, an approval green", () => {
    const { container } = render(
      <>
        <StepBadge step={{ kind: "REVIEW", outcome: "changes_requested" }} />
        <StepBadge step={{ kind: "REVIEW", outcome: "passed" }} />
      </>,
    );
    const chips = [...container.querySelectorAll(".chip")];
    expect(chips[0]).toHaveClass("chip-running");
    expect(chips[1]).toHaveClass("chip-ok");
    expect(chips[0]?.querySelector(".status-mark")).toHaveAttribute("data-status-glyph", "±");
    expect(chips[1]?.querySelector(".status-mark")).toHaveAttribute("data-status-glyph", "✓");
  });

  it("pulses only while the leg runs, and reads an agent question as the operator's alarm", () => {
    const { container } = render(
      <>
        <StepBadge step={{ kind: "RESOLVE", outcome: "running" }} />
        <StepBadge step={{ kind: "RESOLVE", outcome: "asked" }} />
      </>,
    );
    const chips = [...container.querySelectorAll(".chip")];
    expect(chips[0]).toHaveClass("pulse");
    expect(chips[1]).not.toHaveClass("pulse");
    expect(chips[1]).toHaveClass("chip-fail");
  });

  it("words an outcome with the same mark as its badge", () => {
    render(<StepStatusChip outcome="asked" />);
    expect(screen.getByText("asked a question").querySelector(".status-mark"))
      .toHaveAttribute("data-status-glyph", "?");
  });
});
