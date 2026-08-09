import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Histogram } from "./Charts";

function bar(label: string, value: number) {
  return { key: label, label, value };
}

describe("Histogram", () => {
  it("draws one bar per non-empty bucket and labels every bucket with its value", () => {
    const { container } = render(
      <Histogram
        bars={[bar("Aug 02, 09:00", 1), bar("Aug 02, 10:00", 0), bar("Aug 02, 11:00", 4)]}
        unit="spend"
      />,
    );

    expect(container.querySelectorAll(".hist-bar")).toHaveLength(2);
    expect(screen.getByTitle("Aug 02, 09:00 · $1.00")).toBeInTheDocument();
    expect(screen.getByTitle("Aug 02, 10:00 · $0.00")).toBeInTheDocument();
    expect(screen.getByTitle("Aug 02, 11:00 · $4.00")).toBeInTheDocument();
    expect(screen.getByTitle("Aug 02, 11:00 · $4.00")).toHaveAttribute(
      "data-tip",
      "Aug 02, 11:00 · $4.00 spent",
    );
  });

  it("scales bars against the peak, not against the total", () => {
    const { container } = render(
      <Histogram bars={[bar("a", 5), bar("b", 10)]} />,
    );

    const bars = container.querySelectorAll<HTMLElement>(".hist-bar");
    expect(bars[0]).toHaveStyle({ height: "50%" });
    expect(bars[1]).toHaveStyle({ height: "100%" });
  });

  it("states the window, the total and the peak for a screen reader", () => {
    render(
      <Histogram
        bars={[bar("Aug 02, 09:00", 1.5), bar("Aug 02, 10:00", 2.5)]}
        unit="spend"
      />,
    );

    expect(
      screen.getByRole("img", {
        name: "spend across 2 buckets from Aug 02, 09:00 to Aug 02, 10:00: $4.00 in total, peaking at $2.50 in Aug 02, 10:00",
      }),
    ).toBeInTheDocument();
  });

  it("collapses to an explanation rather than a flat chart when every bucket is empty", () => {
    const { container } = render(
      <Histogram
        bars={[bar("a", 0), bar("b", 0)]}
        emptyText="no journalled spend in the last 48 hours"
      />,
    );

    expect(screen.getByText("no journalled spend in the last 48 hours")).toBeVisible();
    expect(container.querySelector(".hist-plot")).toBeNull();
  });

  it("collapses when the series is absent entirely", () => {
    render(<Histogram bars={[]} emptyText="nothing yet" />);

    expect(screen.getByText("nothing yet")).toBeVisible();
  });
});
