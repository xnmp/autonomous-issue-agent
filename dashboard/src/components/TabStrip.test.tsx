import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TabStrip, type TabDef } from "./TabStrip";

const tab = (key: string, over: Partial<TabDef> = {}): TabDef => ({
  key, label: key, title: key, active: false, onSelect: vi.fn(), ...over,
});

describe("TabStrip", () => {
  it("exposes only tabs inside the tablist, even when a tab can be closed", () => {
    render(<TabStrip tabs={[tab("Overview", { active: true }), tab("#7", { onClose: vi.fn() })]} />);

    const list = screen.getByRole("tablist");
    expect(within(list).getAllByRole("tab")).toHaveLength(2);
    expect(within(list).queryByRole("button")).toBeNull();
  });

  it("closes a closeable tab with Delete from the keyboard, and leaves permanent tabs alone", async () => {
    const onClose = vi.fn();
    const onSelect = vi.fn();
    render(<TabStrip tabs={[tab("Overview", { active: true, onSelect }), tab("#7", { onClose })]} />);

    screen.getByRole("tab", { name: "Overview" }).focus();
    await userEvent.keyboard("{Delete}");
    expect(onClose).not.toHaveBeenCalled();

    screen.getByRole("tab", { name: "#7" }).focus();
    await userEvent.keyboard("{Delete}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
