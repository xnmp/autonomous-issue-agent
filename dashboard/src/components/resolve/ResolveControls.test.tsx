import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ResolveControl } from "../../api/types";
import { retryKey } from "./retry-journal";
import { ResolveControls } from "./ResolveControls";
import {
  absentControl,
  availableControl,
  binding,
  ceilingGate,
  commandResult,
  controlResponse,
  inactiveControl,
  jsonResponse,
  OUTCOME_STATUS,
  rearm,
  unavailableControl,
} from "./test-fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  vi.useRealTimers();
});

/**
 * Routes fetch calls for the deployment/issue under test.
 *
 * `control` is consumed as a queue (each GET pops the next entry, repeating
 * the last one once exhausted) so a test can script "first read available,
 * then read gives a different generation" without timers. `command` and
 * `unblock` are one-shot-or-repeat handlers a test overrides per case.
 */
function router(deployment: string, issue: number) {
  const controlResponses: (() => Response)[] = [];
  let controlIndex = 0;
  const commandCalls: unknown[] = [];
  let commandHandler: (body: unknown) => Response | Promise<Response> = () => {
    throw new Error("no command handler installed");
  };
  let unblockHandler: () => Response | Promise<Response> = () =>
    jsonResponse({ ok: true, detail: "unblocked" }, 200);

  const controlPath = `/api/repos/${encodeURIComponent(deployment)}/issues/${issue}/resolve-control`;
  const commandsPath = `${controlPath}/commands`;
  const unblockPath = `/api/repos/${encodeURIComponent(deployment)}/issues/${issue}/unblock`;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === commandsPath) {
      const body: unknown = JSON.parse(String(init.body));
      commandCalls.push(body);
      return commandHandler(body);
    }
    if (url.pathname === unblockPath) return unblockHandler();
    if (url.pathname === controlPath) {
      if (controlResponses.length === 0) throw new Error("no control response queued");
      // Advances through queued responses in order; once exhausted, keeps
      // returning the last one (so background polling doesn't need endless
      // re-queueing once a test has reached the state it cares about).
      const i = Math.min(controlIndex, controlResponses.length - 1);
      controlIndex += 1;
      const factory = controlResponses[i]!;
      return factory();
    }
    throw new Error(`unrouted fetch: ${url.pathname}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  return {
    fetchMock,
    get commandCalls() { return commandCalls; },
    /** Appends the next control response the GET endpoint should return, in order. */
    queueControl(control: ResolveControl) {
      controlResponses.push(() => controlResponse(control));
    },
    setCommand(handler: (body: unknown) => Response | Promise<Response>) {
      commandHandler = handler;
    },
    setUnblock(handler: () => Response | Promise<Response>) {
      unblockHandler = handler;
    },
  };
}

async function openRequestRearmReview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Request re-arm" }));
  await user.click(screen.getByRole("button", { name: "Review command" }));
  return screen.findByRole("heading", { name: "Review resolve command" });
}

describe("quiet rendering and scope", () => {
  it("renders nothing when the deployment is quiet: no hold, not held-for-human, no saved retry", async () => {
    const r = router("demo", 77);
    r.queueControl(inactiveControl);
    const { container } = render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await waitFor(() => expect(r.fetchMock).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(container).toBeEmptyDOMElement();
  });

  it("never surfaces a saved uncertain record for one issue under a different issue or deployment", async () => {
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({
      version: 1,
      command: { operation: "request_rearm", expected: { ...binding }, request_id: "leftover-77", gate_id: "gate-77", guidance: { kind: "keep" } },
    }));

    const r78 = router("demo", 78);
    r78.queueControl(inactiveControl);
    const { rerender, container } = render(<ResolveControls deployment="demo" issue={78} heldForHuman={false} onChanged={vi.fn()} />);
    await waitFor(() => expect(r78.fetchMock).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("leftover-77")).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();

    const rOther = router("other-deployment", 77);
    rOther.queueControl(inactiveControl);
    rerender(<ResolveControls deployment="other-deployment" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await waitFor(() => expect(rOther.fetchMock).toHaveBeenCalled());
    expect(screen.queryByText("leftover-77")).not.toBeInTheDocument();
  });

  it("restores the exact saved record when re-rendered back onto the issue it belongs to", async () => {
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({
      version: 1,
      command: { operation: "request_rearm", expected: { ...binding }, request_id: "leftover-77", gate_id: "gate-77", guidance: { kind: "keep" } },
    }));
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Command outcome not confirmed" })).toBeInTheDocument();
    expect(screen.getByText("leftover-77")).toBeInTheDocument();
    // Restoring must not auto-send: no POST to the commands endpoint yet.
    expect(r.fetchMock.mock.calls.every(([input]) => !String(input).includes("/commands"))).toBe(true);
  });
});

describe("preparing and confirming a request_rearm command", () => {
  it("shows durable-intent language, not completion, while reviewing, and clears storage once accepted", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);

    await openRequestRearmReview(user);
    expect(screen.getByText(/does not mean the re-arm has completed/i)).toBeInTheDocument();
    expect(screen.getByText(/paid work can start/i)).toBeInTheDocument();

    let sessionSnapshotAtPost: string | null = null;
    r.setCommand((body) => {
      sessionSnapshotAtPost = sessionStorage.getItem(retryKey("demo", 77));
      const control = availableControl({ current_intent: rearm({ request_id: (body as { request_id: string }).request_id }) });
      return jsonResponse(commandResult("accepted", control, "re-arm accepted"), OUTCOME_STATUS.accepted);
    });
    r.queueControl(availableControl({ current_intent: rearm() }));

    await user.click(screen.getByRole("button", { name: "Confirm command" }));

    expect(await screen.findByRole("heading", { name: "Command recorded" })).toBeInTheDocument();
    expect(screen.getByText(/not necessarily completed/i)).toBeInTheDocument();
    // Identity was persisted to storage before the POST was made.
    expect(sessionSnapshotAtPost).not.toBeNull();
    expect(JSON.parse(sessionSnapshotAtPost!).command.request_id).toBe(r.commandCalls[0] && (r.commandCalls[0] as { request_id: string }).request_id);
    // A definite outcome clears the saved record.
    expect(sessionStorage.getItem(retryKey("demo", 77))).toBeNull();
  });

  it('returns to the controls, not a dead end, after "Close result"', async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);
    r.setCommand(() => jsonResponse(commandResult("accepted", availableControl({ current_intent: rearm() })), OUTCOME_STATUS.accepted));
    r.queueControl(availableControl({
      gates: [ceilingGate({ state: "rearming", available_commands: ["cancel_rearm"] })],
      current_intent: rearm(),
    }));
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    await screen.findByRole("heading", { name: "Command recorded" });

    await user.click(screen.getByRole("button", { name: "Close result" }));

    expect(screen.queryByRole("heading", { name: "Command recorded" })).not.toBeInTheDocument();
    // The actions the fresh status offers are usable again.
    expect(await screen.findByRole("button", { name: "Cancel pending re-arm" })).toBeEnabled();
  });

  it('"Back to controls" from review returns without ever posting', async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    await user.click(screen.getByRole("button", { name: "Back to controls" }));

    expect(screen.queryByRole("heading", { name: "Review resolve command" })).not.toBeInTheDocument();
    expect(r.commandCalls).toHaveLength(0);
  });

  it("refuses set guidance with blank text locally, without preparing a command", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Request re-arm" }));
    await user.selectOptions(screen.getByLabelText(/Guidance for the next resolve attempt/), "set");
    await user.click(screen.getByRole("button", { name: "Review command" }));
    expect(await screen.findByText("Enter guidance, or choose Keep or Clear.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Review resolve command" })).not.toBeInTheDocument();
    expect(r.commandCalls).toHaveLength(0);
  });

  it("sends the guidance kind=set text once filled in", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Request re-arm" }));
    await user.selectOptions(screen.getByLabelText(/Guidance for the next resolve attempt/), "set");
    await user.type(screen.getByLabelText(/New guidance/), "watch the schema");
    await user.click(screen.getByRole("button", { name: "Review command" }));
    const review = await screen.findByRole("heading", { name: "Review resolve command" });
    expect(within(review.closest(".resolve-operation") as HTMLElement).getByText("watch the schema")).toBeVisible();

    r.setCommand(() => jsonResponse(commandResult("accepted", availableControl({ current_intent: rearm() })), OUTCOME_STATUS.accepted));
    r.queueControl(availableControl({ current_intent: rearm() }));
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    await screen.findByRole("heading", { name: "Command recorded" });
    expect(r.commandCalls[0]).toMatchObject({ guidance: { kind: "set", text: "watch the schema" } });
  });
});

describe("lost responses and retry", () => {
  it("keeps the exact command on a network failure and retries with a byte-identical body", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    r.setCommand(() => { throw new Error("network down"); });
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    expect(await screen.findByRole("heading", { name: "Command outcome not confirmed" })).toBeInTheDocument();
    expect(screen.getByText(/cannot reach the dashboard backend/i)).toBeInTheDocument();
    const savedAfterFailure = sessionStorage.getItem(retryKey("demo", 77));
    expect(savedAfterFailure).not.toBeNull();
    const firstBody = r.commandCalls[0];

    r.setCommand(() => jsonResponse(commandResult("accepted", availableControl({ current_intent: rearm() })), OUTCOME_STATUS.accepted));
    r.queueControl(availableControl({ current_intent: rearm() }));
    await user.click(screen.getByRole("button", { name: "Retry same command" }));

    await screen.findByRole("heading", { name: "Command recorded" });
    expect(r.commandCalls).toHaveLength(2);
    expect(r.commandCalls[1]).toEqual(firstBody);
    expect(sessionStorage.getItem(retryKey("demo", 77))).toBeNull();
  });

  it("treats a malformed 2xx body as uncertain, not as success", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    r.setCommand(() => jsonResponse({ outcome: "accepted" }, OUTCOME_STATUS.accepted)); // missing required fields
    await user.click(screen.getByRole("button", { name: "Confirm command" }));

    expect(await screen.findByRole("heading", { name: "Command outcome not confirmed" })).toBeInTheDocument();
    expect(sessionStorage.getItem(retryKey("demo", 77))).not.toBeNull();
  });

  it("treats a typed unavailable outcome as uncertain and offers retry of the same command", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    r.setCommand(() => jsonResponse(commandResult("unavailable", unavailableControl, "authority unavailable"), OUTCOME_STATUS.unavailable));
    await user.click(screen.getByRole("button", { name: "Confirm command" }));

    expect(await screen.findByRole("heading", { name: "Command outcome not confirmed" })).toBeInTheDocument();
    expect(screen.getByText("authority unavailable")).toBeInTheDocument();
    expect(sessionStorage.getItem(retryKey("demo", 77))).not.toBeNull();
  });

  it("restores the uncertain state on reload (unmount+remount) without auto-sending", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    const { unmount } = render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);
    r.setCommand(() => { throw new Error("network down"); });
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    await screen.findByRole("heading", { name: "Command outcome not confirmed" });
    const savedRaw = sessionStorage.getItem(retryKey("demo", 77));
    unmount();

    const r2 = router("demo", 77);
    r2.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "Command outcome not confirmed" })).toBeInTheDocument();
    // No POST fired automatically on restore.
    expect(r2.commandCalls).toHaveLength(0);
    expect(sessionStorage.getItem(retryKey("demo", 77))).toBe(savedRaw);
  });
});

describe("definite outcomes that are not acceptance", () => {
  it.each(["conflict", "rejected"] as const)("shows outcome=%s as not accepted and clears the saved record", async (outcome) => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    r.setCommand(() => jsonResponse(commandResult(outcome, availableControl(), "not accepted"), OUTCOME_STATUS[outcome]));
    r.queueControl(availableControl());
    await user.click(screen.getByRole("button", { name: "Confirm command" }));

    expect(await screen.findByRole("heading", { name: "Command not accepted" })).toBeInTheDocument();
    expect(sessionStorage.getItem(retryKey("demo", 77))).toBeNull();
  });

  it("clears the saved record on a framework 422 (RejectedRequest) and reports it, without claiming acceptance", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    r.setCommand(() => jsonResponse({ detail: [{ loc: ["body", "gate_id"], msg: "gate is stale" }] }, 422));
    r.queueControl(availableControl());
    await user.click(screen.getByRole("button", { name: "Confirm command" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("gate is stale"));
    expect(screen.queryByRole("heading", { name: "Review resolve command" })).not.toBeInTheDocument();
    expect(sessionStorage.getItem(retryKey("demo", 77))).toBeNull();
  });
});

describe("duplicate submits and staleness", () => {
  it("sends exactly one POST for duplicate confirm clicks", async () => {
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    const user = userEvent.setup();
    await openRequestRearmReview(user);

    let resolvePost!: (value: Response) => void;
    r.setCommand(() => new Promise<Response>((resolve) => { resolvePost = resolve; }));
    const confirmButton = screen.getByRole("button", { name: "Confirm command" });
    fireEvent.click(confirmButton);
    fireEvent.click(confirmButton);
    fireEvent.click(confirmButton);

    await waitFor(() => expect(r.commandCalls.length).toBeGreaterThan(0));
    expect(r.commandCalls).toHaveLength(1);

    r.queueControl(availableControl({ current_intent: rearm() }));
    resolvePost(jsonResponse(commandResult("accepted", availableControl({ current_intent: rearm() })), OUTCOME_STATUS.accepted));
    await screen.findByRole("heading", { name: "Command recorded" });
    expect(r.commandCalls).toHaveLength(1);
  });

  it("refuses to post a review whose binding changed since it was prepared, and tells the operator to prepare again", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    // The status changes (a new generation, no more holds) after review but
    // before confirm. Wait for something the refreshed read actually changes
    // in the DOM, not just for the fetch call to have been issued, so the
    // click below is guaranteed to see the updated read.
    r.queueControl(availableControl({ expected: { ...binding, generation: binding.generation + 1 }, gates: [], workflow_held: false }));
    await userEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByText("No recorded workflow holds");

    await user.click(screen.getByRole("button", { name: "Confirm command" }));

    expect(r.commandCalls).toHaveLength(0);
    expect(await screen.findByText(/status changed after this command was prepared/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Review resolve command" })).not.toBeInTheDocument();
  });

  it("still allows retrying the exact same command after status changed (uncertain retry is exempt from the staleness check)", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);
    r.setCommand(() => { throw new Error("network down"); });
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    await screen.findByRole("heading", { name: "Command outcome not confirmed" });
    const firstBody = r.commandCalls[0];

    // Status changes while the command is uncertain.
    r.queueControl(availableControl({ expected: { ...binding, generation: binding.generation + 5 } }));

    r.setCommand(() => jsonResponse(commandResult("accepted", availableControl({ current_intent: rearm() })), OUTCOME_STATUS.accepted));
    r.queueControl(availableControl({ current_intent: rearm() }));
    await user.click(screen.getByRole("button", { name: "Retry same command" }));

    await screen.findByRole("heading", { name: "Command recorded" });
    expect(r.commandCalls).toHaveLength(2);
    expect(r.commandCalls[1]).toEqual(firstBody);
  });

  it("refuses to post when the status read has errored since review, telling the operator to prepare again", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    r.fetchMock.mockImplementationOnce(async () => { throw new TypeError("Failed to fetch"); });
    await userEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByText(/Status unavailable/i);

    // The confirm button is disabled while the read has errored, so the
    // operator cannot even attempt to post a review prepared against stale data.
    expect(screen.getByRole("button", { name: "Confirm command" })).toBeDisabled();
    expect(r.commandCalls).toHaveLength(0);
  });
});

describe("blocked state from an unsafe saved record", () => {
  it("blocks all commands and preserves the corrupt record when it cannot be parsed", async () => {
    sessionStorage.setItem(retryKey("demo", 77), "{ this is not json");
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "Resolve actions unavailable" })).toBeInTheDocument();
    // No prepare buttons are offered while blocked.
    expect(screen.queryByRole("button", { name: "Request re-arm" })).not.toBeInTheDocument();
    // Nothing was silently deleted.
    expect(sessionStorage.getItem(retryKey("demo", 77))).toBe("{ this is not json");
    expect(r.commandCalls).toHaveLength(0);
  });

  it("blocks on an oversized saved record", async () => {
    const bloated = JSON.stringify({
      version: 1,
      command: { operation: "cancel_rearm", expected: { ...binding }, request_id: "r", cancellation_id: "c", reason: "x".repeat(300_000) },
    });
    sessionStorage.setItem(retryKey("demo", 77), bloated);
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Resolve actions unavailable" })).toBeInTheDocument();
    expect(r.commandCalls).toHaveLength(0);
  });

  it("blocks on a saved record whose command belongs to a different issue", async () => {
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({
      version: 1,
      command: { operation: "request_rearm", expected: { ...binding, issue: 999 }, request_id: "r", gate_id: "g", guidance: { kind: "keep" } },
    }));
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Resolve actions unavailable" })).toBeInTheDocument();
  });

  it("blocks on an unsupported envelope version", async () => {
    sessionStorage.setItem(retryKey("demo", 77), JSON.stringify({
      version: 2,
      command: { operation: "request_rearm", expected: { ...binding }, request_id: "r", gate_id: "g", guidance: { kind: "keep" } },
    }));
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Resolve actions unavailable" })).toBeInTheDocument();
  });

  it("blocks, without minting a new ID, when session storage throws on read", async () => {
    const original = Object.getOwnPropertyDescriptor(window, "sessionStorage");
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: { getItem: () => { throw new Error("storage disabled"); }, setItem: () => {}, removeItem: () => {}, clear: () => {}, key: () => null, length: 0 },
    });
    try {
      const r = router("demo", 77);
      r.queueControl(availableControl());
      render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
      expect(await screen.findByRole("heading", { name: "Resolve actions unavailable" })).toBeInTheDocument();
      expect(r.commandCalls).toHaveLength(0);
    } finally {
      if (original) Object.defineProperty(window, "sessionStorage", original);
    }
  });

  it("tells the operator nothing was sent when persisting the command fails (quota) before the POST", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);

    const original = Object.getOwnPropertyDescriptor(window, "sessionStorage");
    const values = new Map<string, string>();
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => values.get(key) ?? null,
        setItem: () => { throw new Error("QuotaExceededError"); },
        removeItem: (key: string) => values.delete(key),
        clear: () => values.clear(),
        key: () => null,
        length: 0,
      },
    });
    try {
      await user.click(screen.getByRole("button", { name: "Confirm command" }));
      expect(await screen.findByText(/No request was sent/i)).toBeInTheDocument();
      expect(r.commandCalls).toHaveLength(0);
    } finally {
      if (original) Object.defineProperty(window, "sessionStorage", original);
    }
  });
});

describe("legacy unblock", () => {
  it("is offered only for inactive + held-for-human with no read error and no locked operation, and posts /unblock on confirm", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(inactiveControl);
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={true} onChanged={vi.fn()} />);

    const button = await screen.findByRole("button", { name: "Review legacy unblock" });
    r.setUnblock(() => jsonResponse({ ok: true, detail: "unblocked" }, 200));
    await user.click(button);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Unblock #77/ }));

    await waitFor(() => expect(r.fetchMock.mock.calls.some(([input]) => String(input).endsWith("/issues/77/unblock"))).toBe(true));
  });

  it.each([
    ["available", availableControl()],
    ["absent", absentControl],
    ["unavailable", unavailableControl],
  ] as const)("is never offered when the control is %s, even when held for a human", async (_label, control) => {
    const r = router("demo", 77);
    r.queueControl(control);
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={true} onChanged={vi.fn()} />);
    await waitFor(() => expect(r.fetchMock).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole("button", { name: "Review legacy unblock" })).not.toBeInTheDocument();
  });

  it("is not offered when the status read errored, even though the issue is held for a human", async () => {
    const r = router("demo", 77);
    r.fetchMock.mockImplementation(async () => { throw new TypeError("Failed to fetch"); });
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={true} onChanged={vi.fn()} />);
    await screen.findByText(/cannot reach the dashboard backend/i);
    expect(screen.queryByRole("button", { name: "Review legacy unblock" })).not.toBeInTheDocument();
  });

  it("is not offered while a command is locked (in review), even for inactive + held", async () => {
    // Prepare a command while control is available, then have the deployment go
    // inactive+held on the next read: legacy must still stay hidden while locked.
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={true} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);
    expect(screen.queryByRole("button", { name: "Review legacy unblock" })).not.toBeInTheDocument();
  });
});

describe("history lookup", () => {
  it("issues a GET with the encoded request_id and shows the requested intent separately from current", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    const requested = rearm({ request_id: "historical id?/1", state: "completed" });
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await screen.findByRole("heading", { name: /Resolve control/ });

    r.queueControl(availableControl({ requested_id: requested.request_id, requested_intent: requested }));
    await user.type(screen.getByLabelText("Look up request ID"), "historical id?/1");
    await user.click(screen.getByRole("button", { name: "Look up request" }));

    await waitFor(() => {
      const call = r.fetchMock.mock.calls.find(([input]) => String(input).includes("request_id="));
      expect(call).toBeDefined();
      expect(String(call![0])).toContain(`request_id=${encodeURIComponent("historical id?/1")}`);
    });

    // The heading's accessible name includes the intent's state ("completed"),
    // appended by the shared IntentDetails renderer.
    expect(await screen.findByRole("heading", { name: /^Requested historical re-arm/ })).toBeInTheDocument();
    const requestedSection = screen.getByRole("region", { name: "Requested historical re-arm" });
    expect(within(requestedSection).getByText("historical id?/1")).toBeInTheDocument();
    // The current intent section (none here) stays distinct from the requested one.
    expect(screen.queryByRole("region", { name: "Current re-arm request" })).not.toBeInTheDocument();
  });

  it("disables the lookup button while a command is under review", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);
    expect(screen.getByRole("button", { name: "Look up request" })).toBeDisabled();
  });
});

describe("continue and cancel operations", () => {
  it("sends a continue_rearm command whose request_id renews and stopped_request_id targets the stopped intent", async () => {
    const user = userEvent.setup();
    const stopped = rearm({ state: "superseded" });
    const r = router("demo", 77);
    r.queueControl(availableControl({
      gates: [ceilingGate({ available_commands: ["continue_rearm"] })],
      current_intent: stopped,
    }));
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Continue existing re-arm" }));
    await user.click(screen.getByRole("button", { name: "Review command" }));
    await screen.findByRole("heading", { name: "Review resolve command" });

    r.setCommand(() => jsonResponse(commandResult("accepted", availableControl()), OUTCOME_STATUS.accepted));
    r.queueControl(availableControl());
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    await screen.findByRole("heading", { name: "Command recorded" });

    expect(r.commandCalls[0]).toMatchObject({ operation: "continue_rearm", stopped_request_id: stopped.request_id });
    expect((r.commandCalls[0] as { request_id: string }).request_id).not.toBe(stopped.request_id);
  });

  it("requires a non-blank reason for cancel_rearm and mints a cancellation_id different from the target request_id", async () => {
    const user = userEvent.setup();
    const pending = rearm({ state: "pending" });
    const r = router("demo", 77);
    r.queueControl(availableControl({
      gates: [ceilingGate({ available_commands: ["cancel_rearm"] })],
      current_intent: pending,
    }));
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Cancel pending re-arm" }));
    await user.click(screen.getByRole("button", { name: "Review command" }));
    expect(await screen.findByText("Enter a reason for cancellation.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Review resolve command" })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Reason for cancellation"), "tests still red");
    await user.click(screen.getByRole("button", { name: "Review command" }));
    await screen.findByRole("heading", { name: "Review resolve command" });

    r.setCommand(() => jsonResponse(commandResult("accepted", availableControl()), OUTCOME_STATUS.accepted));
    r.queueControl(availableControl());
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    await screen.findByRole("heading", { name: "Command recorded" });

    const sent = r.commandCalls[0] as { operation: string; request_id: string; cancellation_id: string; reason: string };
    expect(sent).toMatchObject({ operation: "cancel_rearm", request_id: pending.request_id, reason: "tests still red" });
    expect(sent.cancellation_id).not.toBe(sent.request_id);
  });
});

describe("review-finding regressions", () => {
  it("keeps a typed guidance draft across Back to controls and returns focus to the panel", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Request re-arm" }));
    await user.selectOptions(screen.getByLabelText(/Guidance for the next resolve attempt/), "set");
    await user.type(screen.getByLabelText(/New guidance/), "keep this draft");
    await user.click(screen.getByRole("button", { name: "Review command" }));
    await user.click(await screen.findByRole("button", { name: "Back to controls" }));

    expect(screen.getByLabelText(/New guidance/)).toHaveValue("keep this draft");
    expect(screen.getByRole("heading", { name: /Resolve control/ })).toHaveFocus();
    expect(r.commandCalls).toHaveLength(0);
  });

  it("keeps a typed draft when the operator refreshes the status", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Request re-arm" }));
    await user.selectOptions(screen.getByLabelText(/Guidance for the next resolve attempt/), "set");
    await user.type(screen.getByLabelText(/New guidance/), "survives refresh");
    const reads = r.fetchMock.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Refresh status" }));

    await waitFor(() => expect(r.fetchMock.mock.calls.length).toBeGreaterThan(reads));
    expect(screen.getByLabelText(/New guidance/)).toHaveValue("survives refresh");
  });

  it("moves focus to the operation heading once a command is sent and resolved", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    r.setCommand(() => jsonResponse(commandResult("accepted", availableControl({ current_intent: rearm() })), OUTCOME_STATUS.accepted));
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);
    await user.click(screen.getByRole("button", { name: "Confirm command" }));

    expect(await screen.findByRole("heading", { name: "Command recorded" })).toHaveFocus();
  });

  it("refuses a request ID the server would reject without sending a lookup", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await screen.findByRole("button", { name: "Request re-arm" });
    await user.click(screen.getByText("Request history and guidance"));
    const input = screen.getByLabelText("Look up request ID");
    fireEvent.change(input, { target: { value: "abc\tdef" } });
    await user.click(screen.getByRole("button", { name: "Look up request" }));

    expect(await screen.findByText(/cannot contain control characters/)).toBeVisible();
    expect(r.fetchMock.mock.calls.some(([url]) => String(url).includes("request_id="))).toBe(false);
  });

  it("recovers from a lookup the server cannot read without reloading", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await screen.findByRole("button", { name: "Request re-arm" });
    const plain = r.fetchMock.getMockImplementation()!;
    r.fetchMock.mockImplementation(async (input, init) =>
      String(input).includes("request_id=") ? jsonResponse({ detail: "identifier refused" }, 422) : plain(input, init));
    await user.click(screen.getByText("Request history and guidance"));
    await user.type(screen.getByLabelText("Look up request ID"), "unknown-request");
    await user.click(screen.getByRole("button", { name: "Look up request" }));

    await user.click(await screen.findByRole("button", { name: "Clear request lookup" }));
    expect(await screen.findByRole("button", { name: "Request re-arm" })).toBeVisible();
    expect(screen.queryByText(/Status unavailable/)).not.toBeInTheDocument();
  });
});

describe("in-flight reads and replies", () => {
  function gate<T>() {
    let release!: (value: T) => void;
    const promise = new Promise<T>((resolve) => { release = resolve; });
    return { promise, release };
  }

  it("keeps the panel, its lookup input and focus while a history lookup loads", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await screen.findByRole("button", { name: "Request re-arm" });
    const pending = gate<Response>();
    const plain = r.fetchMock.getMockImplementation()!;
    r.fetchMock.mockImplementation(async (input, init) =>
      String(input).includes("request_id=") ? pending.promise : plain(input, init));
    await user.click(screen.getByText("Request history and guidance"));
    const input = screen.getByLabelText("Look up request ID");
    await user.type(input, "request-original");
    await user.click(screen.getByRole("button", { name: "Look up request" }));

    expect(screen.getByRole("heading", { name: /Resolve control/ })).toBeVisible();
    expect(screen.getByLabelText("Look up request ID")).toHaveValue("request-original");
    expect(screen.getByRole("button", { name: "Request re-arm" })).toBeDisabled();

    await act(async () => {
      pending.release(controlResponse(availableControl({ requested_id: "request-original", requested_intent: rearm({ state: "completed" }) })));
    });
    expect(await screen.findByRole("region", { name: "Requested historical re-arm" })).toBeVisible();
    expect(screen.getByLabelText("Look up request ID")).toHaveValue("request-original");
  });

  it("keeps the panel and focuses its heading when a result is closed while the fresh read loads", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    r.setCommand(() => jsonResponse(commandResult("accepted", availableControl({ current_intent: rearm() })), OUTCOME_STATUS.accepted));
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    await screen.findByRole("heading", { name: "Command recorded" });
    const pending = gate<Response>();
    r.fetchMock.mockImplementation(async () => pending.promise);
    await user.click(screen.getByRole("button", { name: "Close result" }));

    expect(screen.getByRole("heading", { name: /Resolve control/ })).toHaveFocus();
    await act(async () => { pending.release(controlResponse(availableControl({ current_intent: rearm() }))); });
  });

  it("offers a way back when a lookup's read reports unavailable authority", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await screen.findByRole("button", { name: "Request re-arm" });
    const plain = r.fetchMock.getMockImplementation()!;
    r.fetchMock.mockImplementation(async (input, init) =>
      String(input).includes("request_id=") ? controlResponse(unavailableControl) : plain(input, init));
    await user.click(screen.getByText("Request history and guidance"));
    await user.type(screen.getByLabelText("Look up request ID"), "request-original");
    await user.click(screen.getByRole("button", { name: "Look up request" }));

    await user.click(await screen.findByRole("button", { name: "Clear request lookup" }));
    expect(await screen.findByRole("button", { name: "Request re-arm" })).toBeEnabled();
  });

  it("looks up an identifier exactly as typed, including padding and astral characters", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await screen.findByRole("button", { name: "Request re-arm" });
    await user.click(screen.getByText("Request history and guidance"));
    const astral = " " + "😀".repeat(300);
    fireEvent.change(screen.getByLabelText("Look up request ID"), { target: { value: astral } });
    await user.click(screen.getByRole("button", { name: "Look up request" }));

    await waitFor(() => expect(r.fetchMock.mock.calls.some(([url]) =>
      String(url).endsWith(`request_id=${encodeURIComponent(astral)}`))).toBe(true));
  });

  it("does not pull focus away from where the operator went while a command was in flight", async () => {
    const user = userEvent.setup();
    const r = router("demo", 77);
    r.queueControl(availableControl());
    const reply = gate<Response>();
    r.setCommand(() => reply.promise);
    render(<ResolveControls deployment="demo" issue={77} heldForHuman={false} onChanged={vi.fn()} />);
    await openRequestRearmReview(user);
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    await screen.findByRole("heading", { name: "Sending resolve command…" });
    await user.click(screen.getByText("Request history and guidance"));
    const input = screen.getByLabelText("Look up request ID");
    input.focus();

    await act(async () => { reply.release(jsonResponse(commandResult("accepted", availableControl({ current_intent: rearm() })), OUTCOME_STATUS.accepted)); });
    await screen.findByRole("heading", { name: "Command recorded" });
    expect(input).toHaveFocus();
  });
});
