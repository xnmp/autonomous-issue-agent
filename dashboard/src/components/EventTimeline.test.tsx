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
});
