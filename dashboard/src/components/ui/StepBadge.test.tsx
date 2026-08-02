import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CardStep } from "../../api/types";
import { StepBadgeRow } from "./StepBadge";

const step = (over: Partial<CardStep>): CardStep => ({
  kind: "RESOLVE",
  state: "ok",
  status: null,
  cost_usd: 1,
  count: 1,
  ...over,
});

describe("the tasks strip as a condensed timeline", () => {
  it("keeps the backend's chronological order and separates episodes", () => {
    const { container } = render(
      <StepBadgeRow
        steps={[
          step({ kind: "RESOLVE" }),
          step({ kind: "REVIEW", status: "REQUEST_CHANGES" }),
          step({ kind: "ADDRESS_REVIEW" }),
          step({ kind: "REVIEW", status: "APPROVE" }),
        ]}
      />,
    );
    const labels = [...container.querySelectorAll(".chip")].map((chip) => chip.textContent);
    expect(labels).toEqual(["Resolve", "Review", "Address review", "Review"]);
    expect(container.querySelectorAll(".step-arrow")).toHaveLength(3);
  });

  it("shows an episode's retries as a multiplier, with the full story on hover", () => {
    render(<StepBadgeRow steps={[step({ kind: "FIX_CI", count: 3, cost_usd: 1.5 })]} />);
    const chip = screen.getByTitle(/FIX_CI · 3 attempts · ok · \$1\.50/);
    expect(chip).toHaveTextContent("×3");
  });

  it("tones an operator intervention as meta, never as an agent leg", () => {
    const { container } = render(
      <StepBadgeRow steps={[step({ kind: "unblock", state: "done", cost_usd: 0 })]} />,
    );
    expect(container.querySelector(".chip")).toHaveClass("chip-meta");
  });
});
