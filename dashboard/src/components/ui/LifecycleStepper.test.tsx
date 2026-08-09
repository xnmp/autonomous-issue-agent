import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CardStep, IssueStep } from "../../api/types";
import { deriveLifecycleStages, LifecycleStepper } from "./LifecycleStepper";

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

  it("uses the same outcome tones and glyphs as task chips", () => {
    render(
      <LifecycleStepper
        compact
        steps={[
          cardStep({ kind: "REVIEW", status: "REQUEST_CHANGES" }),
          cardStep({ kind: "unblock", state: "done", status: null }),
        ]}
      />,
    );

    const review = screen.getByLabelText(/REVIEW · ok · REQUEST_CHANGES/).closest("li");
    const operator = screen.getByLabelText(/^unblock · done/).closest("li");
    expect(review).toHaveClass("life-tone-running");
    expect(review?.querySelector(".status-mark")).toHaveAttribute("data-status-glyph", "±");
    expect(operator).toHaveClass("life-tone-meta");
  });

  it("bounds long compact histories without dropping their earlier details", () => {
    render(
      <LifecycleStepper
        compact
        steps={Array.from({ length: 8 }, (_, index) => cardStep({
          kind: index % 2 === 0 ? "RESOLVE" : "REVIEW",
          status: index % 2 === 0 ? "done" : "APPROVE",
        }))}
      />,
    );

    expect(screen.getByText("+3")).toBeVisible();
    const earlier = document.querySelector(".life-elided .life-node");
    expect(earlier).toHaveAttribute("data-tip", expect.stringContaining("RESOLVE · ok · done"));
    expect(earlier).toHaveAttribute("data-tip", expect.stringContaining("REVIEW · ok · APPROVE"));
    expect(earlier).not.toHaveAttribute("title");
    expect(earlier).toHaveAttribute("tabindex", "0");
    expect(document.querySelectorAll(".lifecycle-task-compact .life-stage")).toHaveLength(6);
  });

  it("distinguishes waiting-on-human from failure without relying on color", () => {
    render(
      <LifecycleStepper
        compact
        steps={[
          cardStep({ kind: "RESOLVE" }),
          cardStep({ kind: "REVIEW", state: "blocked", status: "blocked" }),
        ]}
        stage="awaiting human reply"
        attention="blocked"
        hasPullRequest
      />,
    );

    expect(screen.getByLabelText(/REVIEW · blocked/).closest("li")).toHaveClass(
      "life-blocked",
      "life-tone-fail",
    );
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
});
