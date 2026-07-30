import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ToastNotification } from "../api/types";
import { ToastStack } from "./ToastStack";

function toast(id: string, title = `Toast ${id}`): ToastNotification {
  return {
    id,
    ts: "2026-07-30T00:00:00Z",
    severity: "info",
    title,
    detail: "Details",
    issue: null,
    pr: null,
  };
}

describe("ToastStack lifecycle", () => {
  it("deduplicates repeated notification IDs within one payload", () => {
    const repeated = toast("1", "Issue #7 dispatched");
    const { rerender } = render(
      <ToastStack repo="demo" notifications={[toast("0")]} roles={[]} />,
    );

    rerender(
      <ToastStack
        repo="demo"
        notifications={[toast("0"), repeated, repeated]}
        roles={[]}
      />,
    );

    expect(screen.getAllByText("Issue #7 dispatched")).toHaveLength(1);
  });

  it("automatically dismisses a toast after 15 seconds", () => {
    vi.useFakeTimers();
    try {
      const { rerender } = render(
        <ToastStack repo="demo" notifications={[toast("0")]} roles={[]} />,
      );
      rerender(
        <ToastStack
          repo="demo"
          notifications={[toast("0"), toast("1", "Issue #7 dispatched")]}
          roles={[]}
        />,
      );
      expect(screen.getByText("Issue #7 dispatched")).toBeVisible();

      act(() => vi.advanceTimersByTime(14_999));
      expect(screen.getByText("Issue #7 dispatched")).toBeVisible();

      act(() => vi.advanceTimersByTime(1));
      expect(screen.queryByText("Issue #7 dispatched")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
