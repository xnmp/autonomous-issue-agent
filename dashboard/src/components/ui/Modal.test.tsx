import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmModal } from "./Modal";

function modal(onCancel: () => void) {
  return <ConfirmModal title="Confirm it" body="Sure?" confirmLabel="Do it" onConfirm={vi.fn()} onCancel={onCancel} />;
}

describe("ConfirmModal", () => {
  it("focuses the confirm button once, so a parent re-render never steals focus back", async () => {
    const { rerender } = render(modal(() => undefined));
    expect(screen.getByRole("button", { name: "Do it" })).toHaveFocus();

    await userEvent.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    // A parent passing a fresh inline handler on every render.
    rerender(modal(() => undefined));

    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("escape cancels through the newest handler", async () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { rerender } = render(modal(first));
    rerender(modal(latest));

    await userEvent.keyboard("{Escape}");

    expect(first).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledTimes(1);
  });
});
