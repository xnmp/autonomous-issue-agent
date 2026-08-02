import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EventTimeline } from "./EventTimeline";

describe("EventTimeline", () => {
  it("answers who performed an open-shaped action and what they did", () => {
    render(
      <EventTimeline
        events={[
          {
            ts: "2026-07-29T01:00:00Z",
            event: "action",
            role: "steward",
            desc: "comment issue #50: deployment verified",
          },
        ]}
      />,
    );

    expect(screen.getByText("steward")).toBeVisible();
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
});
