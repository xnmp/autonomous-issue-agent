import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ReviewControl, ReviewDaemonDecision, ReviewDispatch, ReviewGate } from "../../api/types";
import { reviewRetryKey } from "./retry-journal";
import { ReviewControls } from "./ReviewControls";
import {
  AS_OF, DECIDED_AT, decisionGate, pendingRearm, REVIEW_OUTCOME_STATUS, reviewAbsent, reviewBinding, reviewCeilingGate,
  reviewControl, reviewControlResponse, reviewDaemon, reviewInactive, reviewJson, reviewMigrationRequired, reviewResult,
  reviewRetryGate, reviewUnavailable,
} from "./test-fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

/** Routes the PR's review-control GET (a queue that repeats its last entry) and command POST. */
function router(pr = 12) {
  const reads: (() => Response)[] = [];
  let index = 0;
  const posts: unknown[] = [];
  let onCommand: (body: unknown) => Response = () => { throw new Error("no command handler installed"); };
  const path = `/api/repos/demo/pulls/${pr}/review-control`;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === `${path}/commands`) {
      const body: unknown = JSON.parse(String(init.body));
      posts.push(body);
      return onCommand(body);
    }
    if (url.pathname === path) {
      const reply = reads[Math.min(index, reads.length - 1)];
      index += 1;
      if (!reply) throw new Error("no control response queued");
      return reply();
    }
    throw new Error(`unrouted fetch: ${url.pathname}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    posts,
    queue(control: ReviewControl) { reads.push(() => reviewControlResponse(control)); },
    queueRaw(reply: () => Response) { reads.push(reply); },
    command(handler: (body: unknown) => Response) { onCommand = handler; },
  };
}

function show(control: ReviewControl) {
  const r = router();
  r.queue(control);
  const view = render(<ReviewControls deployment="demo" pr={12} />);
  return { r, view };
}

const gateItem = (text: RegExp | string) => screen.getByText(text).closest("li") as HTMLElement;

describe("statuses", () => {
  it("stays quiet once the family reads inactive", async () => {
    const { view } = show(reviewInactive);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(view.container).toBeEmptyDOMElement();
  });

  it("explains an absent workflow instead of hiding the panel the legacy refusal points at", async () => {
    show(reviewAbsent);
    expect(await screen.findByRole("heading", { name: /Review control PR #12/ })).toBeVisible();
    expect(screen.getByText(/has not recorded a review workflow for this pull request/)).toHaveTextContent(/nothing to re-arm/);
    expect(screen.queryByRole("button", { name: /re-arm|Authorize|Continue/ })).not.toBeInTheDocument();
  });

  it("explains that a migration-required pull request has no lever", async () => {
    show(reviewMigrationRequired);
    expect(await screen.findByText(/Review rejection control is not activated/)).toBeVisible();
    expect(screen.getByText(/resolve family is active/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /re-arm/ })).not.toBeInTheDocument();
  });

  it("shows an unavailable authority as unavailable, never as empty", async () => {
    show(reviewUnavailable);
    expect(await screen.findByText(/Review control unavailable: control store unreadable/)).toBeVisible();
  });

  it("shows an unreadable status as unavailable", async () => {
    const r = router();
    r.queueRaw(() => new Response("not json", { status: 500 }));
    render(<ReviewControls deployment="demo" pr={12} />);
    expect(await screen.findByText(/Status unavailable/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /re-arm/ })).not.toBeInTheDocument();
  });
});

describe("gate causes and their actions", () => {
  it("lists every counted review of a ceiling and offers the re-arm with its effect", async () => {
    const user = userEvent.setup();
    show(reviewControl());
    expect(await screen.findByText("Review rejection ceiling")).toBeVisible();
    expect(screen.getByText(/2 counted bot rejections \/ 2 allowed/)).toBeVisible();
    await user.click(screen.getByText("Counted reviews (2)"));
    expect(screen.getByText("review-0")).toBeVisible();
    expect(screen.getByText("review-1")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Request re-arm" }));
    expect(screen.getByText(/Authorizes dismissal of exactly the 2 counted bot reviews/)).toBeVisible();
  });

  it("shows a refused re-arm disabled with its reason, and a huge counted set in full", async () => {
    const reason = "70 counted reviews exceed the 63-dismissal bound of one re-arm";
    show(reviewControl({ gates: [reviewCeilingGate({ actions: [{ state: "refused", operation: "request_rearm", reason }] }, 70)] }));
    const button = await screen.findByRole("button", { name: "Request re-arm" });
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(`Unavailable: ${reason}`);
    expect(screen.getByText(/One re-arm can dismiss at most 63 reviews/, { selector: "p" })).toBeInTheDocument();
    expect(screen.getAllByText(/^review-\d+$/)).toHaveLength(70);
  });

  it("marks a contested hold and keeps the lever", async () => {
    const gate = reviewCeilingGate();
    const contested = { ...gate, cause: { ...gate.cause, hold: { state: "contested", restores: 3, restore_limit: 3 } } } as ReviewGate;
    show(reviewControl({ gates: [contested] }));
    expect(await screen.findByText("Hold contested.")).toBeVisible();
    expect(screen.getByText(/after 3 restorations \(limit 3\)/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Request re-arm" })).toBeEnabled();
  });

  it.each([
    ["exemption_trailers", "Commit-trailer exemptions are unreadable"],
    ["review_history", "The complete review history is unreadable"],
    ["prewrite_read", "A command’s pre-write read is unavailable"],
    ["rearm_observation", "The pending re-arm’s reconciliation reads keep failing"],
    ["other", "Required evidence is unavailable"],
  ] as const)("names the unavailable %s evidence and offers no command", async (evidence, label) => {
    show(reviewControl({ gates: [reviewCeilingGate({
      gate_id: `evidence-${evidence}`, actions: [],
      cause: { kind: "evidence_unavailable", evidence, source: "a read", unresolved_since: "2026-09-26T09:00:00+00:00", failed_reads: 4, last_error: "timeout" },
    })] }));
    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.getByText(/No operator command applies/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /re-arm|retry/i })).not.toBeInTheDocument();
  });

  it.each([
    ["escalation_hold", "Escalation hold exhausted", /Apply the planned labels by hand/],
    ["ceiling_release", "Ceiling release relabel refused", /Correct the labels on GitHub/],
  ] as const)("explains a refused %s projection and its exits", async (projection, title, exit) => {
    show(reviewControl({ gates: [reviewCeilingGate({
      gate_id: `refused-${projection}`, actions: [],
      cause: { kind: "review_projection_refused", projection, command_id: "relabel-1", generation: 2, reason: "hold labels changed" },
    })] }));
    expect(await screen.findByText(title)).toBeVisible();
    expect(screen.getByText(exit)).toBeVisible();
  });

  it("degrades one gate whose own records disagree and leaves its sibling's lever", async () => {
    const broken = reviewRetryGate({
      cause: { kind: "facts_unavailable", underlying: "retry_authorization_required", reason: "two open grants name this gate" },
      actions: [{ state: "refused", operation: "authorize_retry", reason: "the hold's grant records disagree" }],
    });
    show(reviewControl({ gates: [broken, reviewCeilingGate()] }));
    expect(await screen.findByText("Hold facts unreadable")).toBeVisible();
    expect(screen.getByText(/records for this retry authorization hold disagree: two open grants name this gate/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Authorize one retry" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Request re-arm" })).toBeEnabled();
  });

  it("shows an unreadable restoration record without touching the gate's lever", async () => {
    const gate = reviewCeilingGate();
    const unreadable = { ...gate, cause: { ...gate.cause, hold: { state: "unreadable", reason: "restore count malformed" } } } as ReviewGate;
    show(reviewControl({ gates: [unreadable] }));
    expect(await screen.findByText(/hold-restoration record is unreadable \(restore count malformed\)/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Request re-arm" })).toBeEnabled();
  });

  it("refuses every command on a pull request the daemon recorded merged, with the server's reason", async () => {
    const reason = "the review daemon found this pull request merged (as of 2026-09-26T10:00:00+00:00); nothing applies";
    show(reviewControl({
      daemon: reviewDaemon({ kind: "terminal", state: "merged" }),
      gates: [reviewCeilingGate({ actions: [{ state: "refused", operation: "request_rearm", reason }] })],
    }));
    const button = await screen.findByRole("button", { name: "Request re-arm" });
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(`Unavailable: ${reason}`);
    expect(screen.getByText(/found this pull request merged: it dispatches no review, and no command applies/)).toBeVisible();
  });

  it("keeps the lever of a pull request the daemon recorded closed, saying acceptance refuses while it stays closed", async () => {
    const effect = "Dismisses 2 counted reviews. The review daemon found this pull request closed (as of 2026-09-26T10:00:00+00:00); acceptance re-reads GitHub and refuses while it stays closed.";
    show(reviewControl({
      daemon: reviewDaemon({ kind: "terminal", state: "closed" }),
      gates: [reviewCeilingGate({ actions: [{ state: "offered", operation: "request_rearm", effect }] })],
    }));
    expect(await screen.findByRole("button", { name: "Request re-arm" })).toBeEnabled();
    expect(screen.getByText(/found this pull request closed: it dispatches no review while it stays closed/)).toHaveTextContent(
      /commands stay available for a reopen; accepting a re-arm, a continuation or a retry grant re-reads GitHub and refuses while it stays closed/);
    expect(screen.queryByText(/no command applies/)).not.toBeInTheDocument();
  });

  it("shows remote uncertainty without a lever", async () => {
    show(reviewControl({ gates: [reviewCeilingGate({
      gate_id: "uncertain", actions: [],
      cause: { kind: "remote_uncertain", command_id: "cmd-9", unresolved_since: "2026-09-26T09:00:00+00:00", failed_reads: 2, last_error: "response lost" },
    })] }));
    expect(await screen.findByText("Remote outcome uncertain")).toBeVisible();
    expect(screen.getByText(/response lost/)).toBeVisible();
  });

  it("shows a waiting grant and refuses a second one with its reason", async () => {
    show(reviewControl({ gates: [reviewRetryGate({
      pending_retry: { authorization_id: "grant-7", command_id: "command-12" },
      cause: { kind: "retry_authorization_required", command_id: "command-12", absence_evidence_id: "absence-12", retry_authorizable: false },
      actions: [{ state: "refused", operation: "authorize_retry", reason: "authorization grant-7 is already waiting for the executor" }],
    })] }));
    expect(await screen.findByText(/One retry authorized, waiting for the executor/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Authorize one retry" })).toBeDisabled();
    expect(screen.getByText(/grant-7 is already waiting/)).toBeVisible();
  });

  it("offers the continuation of a stopped request", async () => {
    show(reviewControl({ gates: [decisionGate()] }));
    expect(await screen.findByText("Re-arm needs a decision")).toBeVisible();
    expect(screen.getByText(/the head, labels or holds it was decided against changed/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Continue stopped re-arm" })).toBeEnabled();
  });
});

describe("intent, dispatch and continuation", () => {
  it("shows each pending obligation with its own progress", async () => {
    show(reviewControl({
      gates: [reviewCeilingGate({ state: "rearming", actions: [{ state: "offered", operation: "cancel_rearm", effect: "Stops request-12." }] })],
      current_intent: pendingRearm(),
    }));
    const list = await screen.findByRole("list", { name: "Re-arm obligations" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringMatching(/Dismiss counted review review-0 verified/),
      expect.stringMatching(/Dismiss counted review review-1 pending/),
      expect.stringMatching(/Relabel: remove needs-human-review, add ai-pr-open pending/),
    ]);
    expect(screen.getByRole("button", { name: "Cancel pending re-arm" })).toBeEnabled();
  });

  it("marks an obligation whose recorded progress is unreadable", async () => {
    const intent = pendingRearm();
    const commands = intent.state === "pending" ? intent.commands : [];
    show(reviewControl({
      gates: [reviewCeilingGate({ state: "rearming", actions: [] })],
      current_intent: pendingRearm({ commands: [{ ...commands[0]!, status: "unavailable" }, ...commands.slice(1)] }),
    }));
    const list = await screen.findByRole("list", { name: "Re-arm obligations" });
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent(/progress unreadable \(its recorded facts disagree\)/);
  });

  it("shows a completed and a stopped request with their outcomes", async () => {
    const r = router();
    r.queue(reviewControl({
      gates: [], workflow_held: false, daemon: reviewDaemon({ kind: "owed", review: "re_review" }),
      current_intent: { state: "completed", request_id: "request-12", gate_id: "ceiling-12", generation: 3, verified_commands: ["dismiss-0"] },
      requested_id: "request-11",
      requested_intent: { state: "cancelled", request_id: "request-11", gate_id: "ceiling-12", generation: 2, reason: "operator_cancelled", authorized_reviews: [], verified_commands: [], retired_commands: ["dismiss-x"] },
    }));
    render(<ReviewControls deployment="demo" pr={12} />);
    expect(await screen.findByText(/Every obligation is verified/)).toBeVisible();
    expect(screen.getByText(/Stopped because an operator cancelled it/)).toBeVisible();
    expect(screen.getByText(/A fresh review is owed and passed this tick’s dispatch admission; paid admission \(the spend cap\) still decides when it runs/)).toBeVisible();
  });

  it.each<[ReviewDispatch, RegExp]>([
    [{ kind: "terminal", state: "closed" }, /found this pull request closed: it dispatches no review while it stays closed/],
    [{ kind: "terminal", state: "merged" }, /found this pull request merged: it dispatches no review, and no command applies/],
    [{ kind: "not_admitted", holds: ["needs-human-review", "ai-parked"] }, /holds outside the review workflow: needs-human-review; ai-parked\. No review-control command changes them/],
    [{ kind: "withheld", review: "re_review", reason: "fix_claim", detail: "a fix run still holds this pull request's issue (40s)" },
      /A fresh review is owed, withheld by this tick’s dispatch admission: a fix run holds this pull request \(a fix run still holds this pull request's issue \(40s\)\)\. Each tick decides again/],
    [{ kind: "withheld", review: "initial", reason: "reserved", detail: "target 4 is already reserved" },
      /An initial review is owed, withheld by this tick’s dispatch admission: its dispatch target is already reserved/],
    [{ kind: "in_flight", review: "re_review" },
      /A fresh review is running now: this pull request’s own dispatch still holds its reservation, so no second review is bought/],
    [{ kind: "deferred", reason: "unproductive-round counter unreadable" }, /has not decided: unproductive-round counter unreadable/],
    [{ kind: "held", gate_ids: ["a", "b"] }, /held by 2 gates/],
    [{ kind: "submission_unresolved", command_ids: ["s"] }, /1 review submission whose outcome is not yet settled/],
    [{ kind: "release_projection_pending", command_id: "r" }, /ceiling release relabel to be verified/],
    [{ kind: "owed", review: "initial" }, /An initial review is owed/],
    [{ kind: "not_owed", reason: "the head was already reviewed" }, /No review is owed: the head was already reviewed/],
  ])("renders the daemon's recorded dispatch %#, dated as of its last confirmation", async (dispatch, text) => {
    show(reviewControl({ daemon: reviewDaemon(dispatch) }));
    const note = await screen.findByText(text);
    expect(note.closest(".resolve-status-note")).toHaveTextContent(`Review daemon, as of ${AS_OF}`);
  });

  it.each<[ReviewDaemonDecision, RegExp]>([
    [{ state: "unrecorded" }, /has not recorded a dispatch decision for this pull request yet/],
    [{ state: "unreadable", reason: "payload malformed" }, /recorded decision is unreadable \(payload malformed\); dispatch is not shown/],
  ])("never invents a dispatch the daemon did not record %#", async (daemon, text) => {
    show(reviewControl({ daemon }));
    expect(await screen.findByText(text)).toBeVisible();
    expect(screen.queryByText(/is owed|No review is owed/)).not.toBeInTheDocument();
  });

  it("shows the reviewed baseline and the facts and read the daemon decided from", async () => {
    const user = userEvent.setup();
    show(reviewControl({
      continuation: { kind: "reviewed_baseline", head_sha: "b".repeat(40), patch_id: "p", reviewed_generation: 2, source: { kind: "bound_submission", command_id: "submit-3", generation: 2 } },
      daemon: reviewDaemon({ kind: "not_admitted", holds: ["ai-parked"] }, {
        read: "departed_read",
        pull_request: { head_sha: "c".repeat(40), state: "open", labels: ["ai-parked"], external_holds: ["ai-parked"] },
      }),
    }));
    await user.click(await screen.findByText(/Review baseline, observation/));
    expect(screen.getByText(/Reviewed baseline: head/)).toHaveTextContent("harness submission submit-3");
    const facts = screen.getByText(/Decided at/);
    expect(facts).toHaveTextContent(`Decided at ${DECIDED_AT} from its own read of this pull request after it left the review read, last confirmed unchanged at ${AS_OF}`);
    expect(facts).toHaveTextContent(/head cccccccccccc, labels ai-parked, with holds a re-arm keeps: ai-parked/);
    expect(facts).toHaveTextContent("not a live GitHub check");
  });

  it("says a failed daemon read recorded no facts", async () => {
    const user = userEvent.setup();
    show(reviewControl({ daemon: reviewDaemon({ kind: "deferred", reason: "read failed" }, { pull_request: null }) }));
    await user.click(await screen.findByText(/Review baseline, observation/));
    expect(screen.getByText(/Decided at/)).toHaveTextContent("Its read of the pull request failed, so no facts are recorded.");
    expect(screen.getByText(/Decided at/)).not.toHaveTextContent(/consecutive/);
  });

  const failure = {
    failed_reads: 6, reason: "502 Bad Gateway", first_failed_at: "2026-09-26T10:15:00+00:00",
    last_failed_at: "2026-09-26T18:00:00+00:00",
  };

  it("says how many of the daemon's own reads have failed and that retries are bounded", async () => {
    const user = userEvent.setup();
    show(reviewControl({ daemon: reviewDaemon({ kind: "deferred", reason: "read failed" }, {
      read: "departed_read", pull_request: null, read_failure: failure,
    }) }));
    await user.click(await screen.findByText(/Review baseline, observation/));
    const facts = screen.getByText(/Decided at/);
    expect(facts).toHaveTextContent(
      "Its own read has failed 6 consecutive times since 2026-09-26T10:15:00+00:00 (latest at 2026-09-26T18:00:00+00:00: 502 Bad Gateway). Retries back off to at most one every six hours.");
    expect(facts).not.toHaveTextContent(/last successful read/);
  });

  it("keeps a closed pull request shown closed, as of its last good read, while its reads fail", async () => {
    const user = userEvent.setup();
    show(reviewControl({ daemon: reviewDaemon({ kind: "terminal", state: "closed" }, {
      read: "departed_read", read_failure: failure,
    }) }));
    const note = await screen.findByText(/found this pull request closed/);
    expect(note.closest(".resolve-status-note")).toHaveTextContent(`Review daemon, as of ${AS_OF}`);
    await user.click(screen.getByText(/Review baseline, observation/));
    expect(screen.getByText(/Decided at/)).toHaveTextContent(
      /It read the pull request closed at head .*; the facts above are from its last successful read\. Retries back off/);
  });
});

describe("commands", () => {
  it("records a re-arm as durable intent, retaining it until the server answers", async () => {
    const user = userEvent.setup();
    const r = router();
    r.queue(reviewControl());
    const pending = reviewControl({ current_intent: pendingRearm(), gates: [reviewCeilingGate({ state: "rearming", actions: [] })] });
    r.command(() => {
      expect(sessionStorage.getItem(reviewRetryKey("demo", 12))).not.toBeNull();
      return reviewJson(reviewResult("accepted", pending, ""), REVIEW_OUTCOME_STATUS.accepted);
    });
    render(<ReviewControls deployment="demo" pr={12} />);
    await user.click(await screen.findByRole("button", { name: "Request re-arm" }));
    await user.selectOptions(screen.getByLabelText("Guidance for the next paid review"), "set");
    await user.type(screen.getByLabelText("New guidance"), "Check the parser.");
    await user.click(screen.getByRole("button", { name: "Review command" }));
    expect(screen.getByRole("heading", { name: "Review command" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    expect(await screen.findByRole("heading", { name: "Command recorded" })).toBeVisible();
    expect(r.posts).toEqual([expect.objectContaining({
      operation: "request_rearm", expected: reviewBinding, gate_id: "ceiling-12", guidance: { kind: "set", text: "Check the parser." },
    })]);
    expect(sessionStorage.getItem(reviewRetryKey("demo", 12))).toBeNull();
  });

  it("keeps an unconfirmed command and retries exactly the same one", async () => {
    const user = userEvent.setup();
    const r = router();
    r.queue(reviewControl({ gates: [reviewRetryGate()] }));
    let replies = 0;
    r.command(() => {
      replies += 1;
      return replies === 1
        ? reviewJson(reviewResult("unavailable", reviewUnavailable, "database is locked"), REVIEW_OUTCOME_STATUS.unavailable)
        : reviewJson(reviewResult("replayed", reviewControl({ gates: [] }), ""), REVIEW_OUTCOME_STATUS.replayed);
    });
    render(<ReviewControls deployment="demo" pr={12} />);
    await user.click(await screen.findByRole("button", { name: "Authorize one retry" }));
    await user.click(screen.getByRole("button", { name: "Review command" }));
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    expect(await screen.findByRole("heading", { name: "Command outcome not confirmed" })).toBeVisible();
    expect(sessionStorage.getItem(reviewRetryKey("demo", 12))).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry same command" }));
    expect(await screen.findByText(/Original command replayed/)).toBeVisible();
    expect(r.posts).toHaveLength(2);
    expect(r.posts[0]).toEqual(r.posts[1]);
    expect(r.posts[0]).toMatchObject({ operation: "authorize_retry", command_id: "command-12", evidence_id: "absence-12" });
  });

  it("shows a server refusal as not accepted, with its reason", async () => {
    const user = userEvent.setup();
    const r = router();
    r.queue(reviewControl());
    r.command(() => reviewJson(reviewResult("conflict", reviewControl(), "workflow revision changed"), REVIEW_OUTCOME_STATUS.conflict));
    render(<ReviewControls deployment="demo" pr={12} />);
    await user.click(await screen.findByRole("button", { name: "Request re-arm" }));
    await user.click(screen.getByRole("button", { name: "Review command" }));
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    expect(await screen.findByRole("heading", { name: "Command not accepted" })).toBeVisible();
    expect(screen.getByText(/workflow revision changed/)).toBeVisible();
  });

  it("never sends a command prepared against a status that has since changed", async () => {
    const user = userEvent.setup();
    const r = router();
    r.queue(reviewControl());
    r.queue(reviewControl({ expected: { ...reviewBinding, version: 6 } }));
    render(<ReviewControls deployment="demo" pr={12} />);
    await user.click(await screen.findByRole("button", { name: "Request re-arm" }));
    await user.click(screen.getByRole("button", { name: "Review command" }));
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Confirm command" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Confirm command" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/status changed after this command was prepared/);
    expect(r.posts).toEqual([]);
  });

  it("requires a reason to cancel", async () => {
    const user = userEvent.setup();
    const r = router();
    r.queue(reviewControl({
      gates: [reviewCeilingGate({ state: "rearming", actions: [{ state: "offered", operation: "cancel_rearm", effect: "Stops request-12." }] })],
      current_intent: pendingRearm(),
    }));
    render(<ReviewControls deployment="demo" pr={12} />);
    await user.click(await screen.findByRole("button", { name: "Cancel pending re-arm" }));
    await user.click(screen.getByRole("button", { name: "Review command" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a reason for cancellation.");
    expect(gateItem("Review rejection ceiling")).toBeInTheDocument();
    expect(r.posts).toEqual([]);
  });
});
