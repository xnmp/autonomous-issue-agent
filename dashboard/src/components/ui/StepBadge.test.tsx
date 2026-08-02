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

  it("compresses a long history into dots and keeps labels on the tail", () => {
    // keifu #34: ~25 labelled episodes wrapped the cell to twenty lines.
    const long = [
      step({ kind: "TRIAGE" }),
      step({ kind: "ROUTE" }),
      step({ kind: "RESOLVE", state: "failed" }),
      step({ kind: "unblock", state: "done", cost_usd: 0 }),
      step({ kind: "RESOLVE" }),
      step({ kind: "REVIEW", status: "REQUEST_CHANGES" }),
      step({ kind: "ADDRESS_REVIEW" }),
      step({ kind: "RE_REVIEW", status: "REQUEST_CHANGES" }),
      step({ kind: "FIX_CI", count: 2 }),
      step({ kind: "RE_REVIEW", state: "running" }),
    ];
    const { container } = render(<StepBadgeRow steps={long} />);
    const chips = [...container.querySelectorAll(".chip")].map((c) => c.textContent);
    expect(chips).toEqual(["Re-review", "Fix CI×2", "Re-review"]);
    expect(container.querySelectorAll(".step-dot")).toHaveLength(7);
    expect(container.querySelector(".step-dot-fail")).not.toBeNull();
    expect(container.querySelector(".step-dot-op")).not.toBeNull();
  });

  it("counts what even the dots cannot hold instead of dropping it silently", () => {
    const epic = Array.from({ length: 30 }, (_, i) =>
      step({ kind: i % 2 ? "ADDRESS_REVIEW" : "RE_REVIEW" }),
    );
    const { container } = render(<StepBadgeRow steps={epic} />);
    expect(container.querySelectorAll(".step-dot")).toHaveLength(14);
    expect(container.querySelector(".step-elided")).toHaveTextContent("+13");
  });
});
