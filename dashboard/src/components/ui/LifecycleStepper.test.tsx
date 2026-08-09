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

  it("surfaces repeated review and CI attempts as circle-back counts", () => {
    render(
      <LifecycleStepper
        steps={[
          cardStep({ kind: "RESOLVE" }),
          cardStep({ kind: "REVIEW", status: "REQUEST_CHANGES", count: 2 }),
          cardStep({ kind: "FIX_CI", state: "running", status: null, count: 3 }),
        ]}
        stage="ci"
        hasPullRequest
      />,
    );

    expect(screen.getByLabelText("2 attempts")).toHaveTextContent("×2");
    expect(screen.getByLabelText("3 attempts")).toHaveTextContent("×3");
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
    expect(review).toHaveAttribute("data-tip", expect.stringContaining("verdict REQUEST_CHANGES → APPROVE"));
    expect(review).toHaveAttribute("data-tip", expect.stringContaining("duration 2:46"));
    expect(review).toHaveAttribute("data-tip", expect.stringContaining("cost $0.73"));
  });

  it("distinguishes waiting-on-human from failure without relying on color", () => {
    render(
      <LifecycleStepper
        compact
        steps={[cardStep({ kind: "RESOLVE" }), cardStep({ kind: "REVIEW" })]}
        stage="awaiting human reply"
        attention="blocked"
        hasPullRequest
      />,
    );

    expect(screen.getByLabelText(/Review · blocked/).closest("li")).toHaveClass("life-blocked");
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
