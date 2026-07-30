import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ToastNotification } from "../api/types";
import { ToastStack } from "./ToastStack";

function toast(id: string, title = `Transition ${id}`): ToastNotification {
  return {
    id,
    ts: `2026-07-30T12:00:0${id}Z`,
    severity: "info",
    title,
    detail: null,
    issue: null,
    pr: null,
  };
}

describe("ToastStack", () => {
  it("sets a history high-water mark, announces once, and keeps dismissal across polls", async () => {
    const history = [toast("1", "Already happened")];
    const { rerender } = render(
      <ToastStack repo="demo" notifications={null} roles={[]} />,
    );
    rerender(<ToastStack repo="demo" notifications={history} roles={[]} />);
    expect(screen.queryByText("Already happened")).not.toBeInTheDocument();

    const merged = toast("2", "Issue #7 completed");
    rerender(<ToastStack repo="demo" notifications={[...history, merged]} roles={[]} />);
    expect(screen.getByText("Issue #7 completed")).toBeVisible();

    rerender(<ToastStack repo="demo" notifications={[...history, merged]} roles={[]} />);
    expect(screen.getAllByText("Issue #7 completed")).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Dismiss Issue #7 completed" }));
    rerender(<ToastStack repo="demo" notifications={[...history, merged]} roles={[]} />);
    expect(screen.queryByText("Issue #7 completed")).not.toBeInTheDocument();
  });

  it("bounds a burst and collapses its overflow", () => {
    const { rerender } = render(
      <ToastStack repo="demo" notifications={[toast("0")]} roles={[]} />,
    );
    rerender(
      <ToastStack
        repo="demo"
        notifications={[toast("0"), ...["1", "2", "3", "4", "5", "6"].map((id) => toast(id))]}
        roles={[]}
      />,
    );

    expect(screen.getAllByRole("status")).toHaveLength(4);
    expect(screen.getByText("+2 more")).toBeVisible();
  });

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

  it("announces daemon health transitions without replaying the initial state", () => {
    const role = {
      role: "fix",
      alive: true,
      stale: false,
      last_event_ts: null,
      last_event: "",
      events: 0,
      journal_path: "",
      log_path: "",
    };
    const { rerender } = render(
      <ToastStack repo="demo" notifications={[]} roles={[role]} />,
    );
    expect(screen.queryByText("fix daemon is down")).not.toBeInTheDocument();

    rerender(
      <ToastStack repo="demo" notifications={[]} roles={[{ ...role, alive: false }]} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("fix daemon is down");
  });

  it("drops the previous repository's visible notifications synchronously on switch", () => {
    const { rerender } = render(
      <ToastStack key="one" repo="one" notifications={[]} roles={[]} />,
    );
    rerender(
      <ToastStack key="one" repo="one" notifications={[toast("1", "Repo one merged")]} roles={[]} />,
    );
    expect(screen.getByText("Repo one merged")).toBeVisible();

    rerender(
      <ToastStack
        key="two"
        repo="two"
        notifications={[toast("2", "Repo two history")]}
        roles={[]}
      />,
    );
    expect(screen.queryByText("Repo one merged")).not.toBeInTheDocument();
    expect(screen.queryByText("Repo two history")).not.toBeInTheDocument();
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
