import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CardStep, IssueStep } from "../../api/types";
import { deriveLifecycleStages, LifecycleStepper, type RailNode } from "./LifecycleStepper";

const cardStep = (overrides: Partial<CardStep>): CardStep => ({
  kind: "RESOLVE",
  state: "ok",
  status: "done",
  cost_usd: 0.4,
  count: 1,
  ...overrides,
});

const issueStep = (idx: number, overrides: Partial<IssueStep>): IssueStep => ({
  idx,
  kind: "RESOLVE",
  role: "fix",
  state: "ok",
  model: "gpt-5.6-terra",
  status: "done",
  exit_reason: "ok",
  duration_s: 30,
  cost_usd: 0.4,
  estimated: false,
  session_id: `session-${idx}`,
  has_transcript: false,
  turns: 1,
  usage: null,
  ts: "2026-08-09T01:00:00Z",
  ...overrides,
});

describe("LifecycleStepper", () => {
  it("derives the canonical path and keeps inapplicable triage out", () => {
    const stages = deriveLifecycleStages({
      steps: [cardStep({ kind: "RESOLVE" })],
      stage: "reviewing",
      hasPullRequest: true,
    });

    expect(stages.map(({ id, state }) => [id, state])).toEqual([
      ["resolve", "completed"],
      ["review", "active"],
      ["ci", "future"],
      ["merge", "future"],
    ]);
  });

  it("surfaces retry counts for every task family", () => {
    render(
      <LifecycleStepper
        steps={[
          cardStep({ kind: "RESOLVE", count: 4 }),
          cardStep({ kind: "REVIEW", status: "REQUEST_CHANGES", count: 2 }),
          cardStep({ kind: "FIX_CI", state: "running", status: null, count: 3 }),
        ]}
        stage="ci"
        hasPullRequest
      />,
    );

    expect(screen.getByLabelText("4 attempts")).toHaveTextContent("×4");
    expect(screen.getByLabelText("2 attempts")).toHaveTextContent("×2");
    expect(screen.getByLabelText("3 attempts")).toHaveTextContent("×3");
  });

  it("marks the currently running stage for bold label treatment", () => {
    render(
      <LifecycleStepper
        steps={[cardStep({ state: "running", status: null })]}
        stage="resolving"
      />,
    );

    const label = screen.getByText("Resolve");
    expect(label).toHaveClass("life-label-active");
  });

  it("does not complete CI from a successful repair leg while review is still active", () => {
    const steps = [
      cardStep({ kind: "RESOLVE" }),
      cardStep({ kind: "REVIEW", status: "REQUEST_CHANGES", count: 4 }),
      cardStep({ kind: "ADDRESS_REVIEW" }),
      cardStep({ kind: "RE_REVIEW", status: "REQUEST_CHANGES", count: 3 }),
      cardStep({ kind: "FIX_CI", state: "ok", status: "done" }),
      cardStep({ kind: "RE_REVIEW", state: "running", status: null }),
    ];

    const withoutGreenChecks = deriveLifecycleStages({
      steps,
      stage: "reviewing",
      hasPullRequest: true,
      ciState: null,
    });
    expect(withoutGreenChecks.find(({ id }) => id === "review")?.state).toBe("active");
    expect(withoutGreenChecks.find(({ id }) => id === "ci")?.state).toBe("future");

    const withGreenChecks = deriveLifecycleStages({
      steps,
      stage: "reviewing",
      hasPullRequest: true,
      ciState: "passing",
    });
    expect(withGreenChecks.find(({ id }) => id === "ci")?.state).toBe("completed");
  });

  it.each([
    ["failing", "failed"],
    ["pending", "active"],
  ] as const)("maps a %s current-head check rollup to %s CI", (ciState, expected) => {
    const stages = deriveLifecycleStages({
      steps: [cardStep({ kind: "FIX_CI", state: "ok", status: "done" })],
      stage: "reviewing",
      hasPullRequest: true,
      ciState,
    });

    expect(stages.find(({ id }) => id === "ci")?.state).toBe(expected);
  });

  it("exposes model, verdict, duration, and cost in detailed node help", () => {
    render(
      <LifecycleStepper
        steps={[
          issueStep(0, {
            kind: "REVIEW",
            status: "REQUEST_CHANGES",
            duration_s: 92,
            cost_usd: 0.42,
          }),
          issueStep(1, {
            kind: "RE_REVIEW",
            status: "APPROVE",
            duration_s: 74,
            cost_usd: 0.31,
          }),
        ]}
        stage="merge-ready"
        hasPullRequest
      />,
    );

    const review = screen.getByRole("button", { name: /^Review/ });
    expect(review).toHaveAttribute("data-tip", expect.stringContaining("model gpt-5.6-terra"));
    expect(review).toHaveAttribute(
      "data-tip",
      expect.stringContaining("tasks REVIEW · ok · REQUEST_CHANGES → RE_REVIEW · ok · APPROVE"),
    );
    expect(review).toHaveAttribute("data-tip", expect.stringContaining("verdict REQUEST_CHANGES → APPROVE"));
    expect(review).toHaveAttribute("data-tip", expect.stringContaining("duration 2:46"));
    expect(review).toHaveAttribute("data-tip", expect.stringContaining("cost $0.73"));
  });

  it("distinguishes waiting-on-human from failure without relying on color", () => {
    const stages = deriveLifecycleStages({
      steps: [
        cardStep({ kind: "RESOLVE" }),
        cardStep({ kind: "REVIEW", state: "blocked", status: "blocked" }),
      ],
      stage: "awaiting human reply",
      attention: "blocked",
      hasPullRequest: true,
    });

    expect(stages.find(({ id }) => id === "review")).toMatchObject({ state: "blocked", tone: "fail" });
  });

  it("selects the latest detailed task when a full node is clicked", async () => {
    const onSelectStep = vi.fn();
    render(
      <LifecycleStepper
        steps={[
          issueStep(3, { kind: "REVIEW", status: "REQUEST_CHANGES" }),
          issueStep(5, { kind: "RE_REVIEW", status: "APPROVE" }),
        ]}
        stage="reviewing"
        hasPullRequest
        onSelectStep={onSelectStep}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /^Review/ }));
    expect(onSelectStep).toHaveBeenCalledWith(5);
  });

  describe("with a projected rail", () => {
    const rail = (overrides: Partial<Record<string, RailNode>> = {}): RailNode[] => [
      { id: "triage", label: "Triage", state: "done", count: null, hot: false, hotTip: null },
      { id: "resolve", label: "Resolve", state: "done", count: null, hot: false, hotTip: null },
      { id: "review", label: "Review", state: "current", count: "round 5", hot: true, hotTip: "4 or more review rounds: above typical" },
      { id: "ci", label: "CI", state: "rewound", count: "2 runs", hot: false, hotTip: null },
      { id: "merge", label: "Merge", state: "future", count: null, hot: false, hotTip: null },
    ].map((node) => overrides[node.id] ?? node) as RailNode[];

    it("always draws the five stages and names the current one with every count", () => {
      render(<LifecycleStepper rail={rail()} tone="ok" live={false} label="Issue #7 stage" />);

      const list = screen.getByRole("list", { name: "Issue #7 stage: Review, round 5; CI 2 runs" });
      expect(Array.from(list.querySelectorAll("li")).map((node) => node.textContent)).toEqual([
        "Triage", "Resolve", "Reviewround 5", "CI2 runs", "Merge",
      ]);
      expect(screen.getByText("Review").closest("li")).toHaveAttribute("aria-current", "step");
    });

    it("explains an amber count and a rewound stage", () => {
      render(<LifecycleStepper rail={rail()} tone="fail" live={false} />);

      expect(screen.getByText("round 5")).toHaveClass("rail-hot");
      expect(screen.getByText("round 5")).toHaveAttribute("data-tip", "4 or more review rounds: above typical");
      expect(screen.getByText("2 runs")).not.toHaveClass("rail-hot");
      expect(screen.getByText("CI").closest("li")).toHaveClass("rail-rewound");
      expect(screen.getByText("CI").closest("li")).toHaveAttribute("data-tip", "Reached earlier; now back at Review");
    });

    it("pulses the current node only while a leg is running", () => {
      const { rerender } = render(<LifecycleStepper rail={rail()} tone="ok" live />);
      expect(screen.getByText("Review").closest("li")).toHaveClass("rail-live");

      rerender(<LifecycleStepper rail={rail()} tone="ok" live={false} />);
      expect(screen.getByText("Review").closest("li")).not.toHaveClass("rail-live");
    });
  });
});
