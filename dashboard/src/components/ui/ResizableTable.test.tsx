import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ResizableTable, useSortedRows } from "./ResizableTable";

describe("ResizableTable", () => {
  it("resizes a column from the keyboard and keeps a real table structure", async () => {
    const { container } = render(
      <ResizableTable
        storageKey="test-table"
        columns={[{ key: "issue", label: "Issue", width: 100 }]}
      >
        <tbody><tr><td>#1</td></tr></tbody>
      </ResizableTable>,
    );

    const separator = screen.getByRole("separator", { name: "Resize Issue column" });
    separator.focus();
    await userEvent.keyboard("{ArrowRight}");

    expect(container.querySelector("col")).toHaveStyle({ width: "124px" });
    expect(screen.getByRole("cell", { name: "#1" })).toBeVisible();
  });

  it("shrinks columns to a readable floor", async () => {
    const { container } = render(
      <ResizableTable
        storageKey="shrink-table"
        columns={[{ key: "issue", label: "Issue", width: 180 }]}
      >
        <tbody><tr><td>#1</td></tr></tbody>
      </ResizableTable>,
    );

    const separator = screen.getByRole("separator", { name: "Resize Issue column" });
    separator.focus();
    await userEvent.keyboard("{ArrowLeft}");

    expect(container.querySelector("col")).toHaveStyle({ width: "168px" });
  });

  it("tracks pointer movement in both resize directions", () => {
    const { container } = render(
      <ResizableTable
        storageKey="pointer-table"
        columns={[{ key: "issue", label: "Issue", width: 180 }]}
      >
        <tbody><tr><td>#1</td></tr></tbody>
      </ResizableTable>,
    );
    const separator = screen.getByRole("separator", { name: "Resize Issue column" });

    fireEvent(separator, new MouseEvent("pointerdown", { bubbles: true, clientX: 200 }));
    fireEvent(window, new MouseEvent("pointermove", { bubbles: true, clientX: 140 }));
    fireEvent(window, new MouseEvent("pointerup", { bubbles: true }));

    expect(container.querySelector("col")).toHaveStyle({ width: "120px" });
  });

  it("sorts rows in both directions from an accessible header button", async () => {
    function Fixture() {
      const sorted = useSortedRows(
        [{ issue: 20 }, { issue: 3 }],
        { issue: (row) => row.issue },
        { key: "issue", direction: "asc" },
      );
      return (
        <ResizableTable
          storageKey="sort-table"
          columns={[{ key: "issue", label: "Issue", width: 180 }]}
          sort={sorted.sort}
          onSort={sorted.onSort}
        >
          <tbody>
            {sorted.rows.map((row) => <tr key={row.issue}><td>#{row.issue}</td></tr>)}
          </tbody>
        </ResizableTable>
      );
    }

    render(<Fixture />);
    const cells = () => screen.getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells()).toEqual(["#3", "#20"]);
    expect(screen.getByRole("columnheader", { name: /Issue/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );

    await userEvent.click(screen.getByRole("button", { name: "Sort by Issue" }));
    expect(cells()).toEqual(["#20", "#3"]);
    expect(screen.getByRole("columnheader", { name: /Issue/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });
});
