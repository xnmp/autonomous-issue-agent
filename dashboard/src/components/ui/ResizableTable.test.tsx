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

  it("supports a narrower floor for compact data columns", async () => {
    const { container } = render(
      <ResizableTable
        storageKey="compact-table"
        columns={[{ key: "cost", label: "Cost", width: 92, minWidth: 72 }]}
      >
        <tbody><tr><td>$1.00</td></tr></tbody>
      </ResizableTable>,
    );

    const separator = screen.getByRole("separator", { name: "Resize Cost column" });
    separator.focus();
    await userEvent.keyboard("{ArrowLeft}{ArrowLeft}");

    expect(separator).toHaveAttribute("aria-valuemin", "72");
    expect(container.querySelector("col")).toHaveStyle({ width: "72px" });
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

  it("rearranges headers and their cells with drag or keyboard controls", () => {
    render(
      <ResizableTable
        storageKey="reorder-table"
        columns={[
          { key: "issue", label: "Issue", width: 180 },
          { key: "status", label: "Status", width: 140 },
          { key: "cost", label: "Cost", width: 120 },
        ]}
      >
        <tbody>
          <tr>
            <td>#20</td>
            <td>done</td>
            <td>$1.20</td>
          </tr>
        </tbody>
      </ResizableTable>,
    );

    const headers = () => screen.getAllByRole("columnheader");
    const headerAt = (index: number) => {
      const header = headers()[index];
      expect(header).toBeDefined();
      return header!;
    };
    const cells = () => screen.getAllByRole("cell").map((cell) => cell.textContent);
    fireEvent.dragStart(headerAt(0));
    fireEvent.dragOver(headerAt(2));
    fireEvent.drop(headerAt(2));
    fireEvent.dragEnd(headerAt(0));

    expect(headers().map((header) => header.dataset.columnKey)).toEqual(["status", "cost", "issue"]);
    expect(cells()).toEqual(["done", "$1.20", "#20"]);

    fireEvent.keyDown(headerAt(2), { key: "ArrowLeft", altKey: true });
    expect(headers().map((header) => header.dataset.columnKey)).toEqual(["status", "issue", "cost"]);
    expect(cells()).toEqual(["done", "#20", "$1.20"]);
  });

  it("does not reorder or resize when Alt+Arrow is pressed on a resize handle", () => {
    const { container } = render(
      <ResizableTable
        storageKey="resize-not-reorder"
        columns={[
          { key: "issue", label: "Issue", width: 180 },
          { key: "cost", label: "Cost", width: 120 },
        ]}
      >
        <tbody><tr><td>#20</td><td>$1.20</td></tr></tbody>
      </ResizableTable>,
    );

    fireEvent.keyDown(
      screen.getByRole("separator", { name: "Resize Cost column" }),
      { key: "ArrowLeft", altKey: true },
    );

    expect(screen.getAllByRole("columnheader").map((header) => header.dataset.columnKey))
      .toEqual(["issue", "cost"]);
    expect(container.querySelectorAll("col")[1]).toHaveStyle({ width: "120px" });
  });

  it("preserves hidden columns in their saved order while visible columns move", () => {
    const allColumns = [
      { key: "issue", label: "Issue", width: 180 },
      { key: "status", label: "Status", width: 140 },
      { key: "cost", label: "Cost", width: 120 },
    ];
    const { rerender } = render(
      <ResizableTable storageKey="hidden-order" columns={allColumns}>
        <tbody><tr><td>#20</td><td>done</td><td>$1.20</td></tr></tbody>
      </ResizableTable>,
    );
    fireEvent.keyDown(screen.getAllByRole("columnheader")[1]!, {
      key: "ArrowLeft",
      altKey: true,
    });

    rerender(
      <ResizableTable storageKey="hidden-order" columns={[allColumns[0]!, allColumns[2]!]}>
        <tbody><tr><td>#20</td><td>$1.20</td></tr></tbody>
      </ResizableTable>,
    );
    fireEvent.keyDown(screen.getAllByRole("columnheader")[1]!, {
      key: "ArrowLeft",
      altKey: true,
    });

    rerender(
      <ResizableTable storageKey="hidden-order" columns={allColumns}>
        <tbody><tr><td>#20</td><td>done</td><td>$1.20</td></tr></tbody>
      </ResizableTable>,
    );
    expect(screen.getAllByRole("columnheader").map((header) => header.dataset.columnKey))
      .toEqual(["status", "cost", "issue"]);
    expect(screen.getAllByRole("cell").map((cell) => cell.textContent))
      .toEqual(["done", "$1.20", "#20"]);
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
