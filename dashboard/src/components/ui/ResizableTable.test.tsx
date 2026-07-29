import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ResizableTable } from "./ResizableTable";

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

    expect(container.querySelector("col")).toHaveStyle({ width: "112px" });
    expect(screen.getByRole("cell", { name: "#1" })).toBeVisible();
  });
});
