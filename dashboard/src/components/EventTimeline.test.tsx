import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EventTimeline } from "./EventTimeline";

describe("EventTimeline", () => {
  const event = (minute: number, kind: string) => ({
    ts: `2026-07-29T01:${String(minute).padStart(2, "0")}:00Z`,
    kind,
  });

  it("answers who performed an open-shaped action and what they did", () => {
    render(
      <EventTimeline
        events={[
          {
            ts: "2026-07-29T01:00:00Z",
            event: "action",
            role: "fix",
            desc: "comment issue #50: deployment verified",
          },
        ]}
      />,
    );

    expect(screen.getByText("fix")).toBeVisible();
    expect(screen.getByText("comment issue #50: deployment verified")).toBeVisible();
  });

  it("folds runs of world-observed heartbeats into one countable entry", () => {
    const obs = (minute: number) => ({
      ts: `2026-07-29T01:0${minute}:00Z`,
      kind: "world.observed.v1",
    });
    render(
      <EventTimeline
        events={[
          obs(0),
          obs(1),
          obs(2),
          { ts: "2026-07-29T01:03:00Z", kind: "control.transition.committed.v1" },
          obs(4),
        ]}
      />,
    );

    // The run of three collapses; the lone trailing observation stays put so
    // its position relative to the transition is not lost.
    expect(screen.getByText("world.observed.v1 ×3")).toBeVisible();
    expect(screen.getByText("control.transition.committed.v1")).toBeVisible();
    expect(screen.getByText("world.observed.v1")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("factorizes an alternating pair repeated three times", () => {
    render(
      <EventTimeline
        events={[
          event(0, "route.failed"),
          event(1, "resolve.failed"),
          event(2, "route.failed"),
          event(3, "resolve.failed"),
          event(4, "route.failed"),
          event(5, "resolve.failed"),
        ]}
      />,
    );

    expect(screen.getByText("(route.failed / resolve.failed) ×3")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("factorizes a run of the same kind", () => {
    render(
      <EventTimeline
        events={[event(0, "route.failed"), event(1, "route.failed"), event(2, "route.failed")]}
      />,
    );

    expect(screen.getByText("route.failed ×3")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("leaves a sequence without repetition unchanged", () => {
    render(
      <EventTimeline
        events={[event(0, "route.failed"), event(1, "resolve.failed"), event(2, "review.failed")]}
      />,
    );

    expect(screen.getByText("route.failed")).toBeVisible();
    expect(screen.getByText("resolve.failed")).toBeVisible();
    expect(screen.getByText("review.failed")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("does not factorize a single occurrence of a pair", () => {
    render(<EventTimeline events={[event(0, "route.failed"), event(1, "resolve.failed")]} />);

    expect(screen.queryByText(/×/)).not.toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("renders the empty state for an empty event list", () => {
    render(<EventTimeline events={[]} />);

    expect(screen.getByText("no events in this window")).toBeVisible();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("leaves a trailing partial cycle ungrouped", () => {
    render(
      <EventTimeline
        events={[
          event(0, "route.failed"),
          event(1, "resolve.failed"),
          event(2, "route.failed"),
          event(3, "resolve.failed"),
          event(4, "route.failed"),
        ]}
      />,
    );

    expect(screen.getByText("(route.failed / resolve.failed) ×2")).toBeVisible();
    expect(screen.getByText("route.failed")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });
});
