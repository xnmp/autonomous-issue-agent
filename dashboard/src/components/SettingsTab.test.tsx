import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SettingRow, SettingsPayload, SettingWrite } from "../api/types";
import { SettingsTab } from "./SettingsTab";

function row(over: Partial<SettingRow> & Pick<SettingRow, "key" | "kind" | "group">): SettingRow {
  return {
    label: over.key, help: "", value: "", source: "default", default: "", choices: null, runtime: null,
    effective: null, ...over,
  };
}

/** A stand-in backend that keeps what it is sent, refusing what the daemons would. */
function fakeBackend() {
  const rows: Record<string, SettingRow> = {
    FIX_DISPATCH_CONCURRENCY: row({ key: "FIX_DISPATCH_CONCURRENCY", kind: "int", group: "dispatch",
                                    label: "Fix legs at once", value: "2", source: "repo", default: "1" }),
    ISSUE_PICKUP: row({ key: "ISSUE_PICKUP", kind: "choice", group: "dispatch", label: "Issue pickup",
                        value: "ready", choices: ["ready", "open"] }),
    FIX_MODEL_LOW: row({ key: "FIX_MODEL_LOW", kind: "route", group: "routes", label: "Tier low",
                         value: "claude:sonnet:low", source: "shared" }),
  };
  const writes: SettingWrite[] = [];
  const payload = (): SettingsPayload => ({
    settings: Object.values(rows), runtimes: ["claude", "codex"],
    efforts: { claude: ["low", "medium", "high"], codex: ["low", "medium", "high", "xhigh"] },
    models: { claude: ["opus", "sonnet"], codex: ["gpt-6-luna"] },
    files: { repo: "/h/config/repos/demo.env", shared: "/h/config/shared.env" }, warnings: [],
  });
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method !== "POST") return json(payload());
    const write = JSON.parse(String(init.body)) as SettingWrite;
    writes.push(write);
    const target = rows[write.key];
    if (!target || (write.key === "FIX_DISPATCH_CONCURRENCY" && Number(write.value) > 16)) {
      return json({ ok: false, detail: "refused, demo would be invalid: FIX_DISPATCH_CONCURRENCY must be 1..16",
                    warnings: [] }, 400);
    }
    rows[write.key] = write.value === null
      ? { ...target, value: target.default, source: "default" }
      : { ...target, value: write.value ?? "", source: write.scope };
    return json({ ok: true, detail: `${write.key} saved; applies at the next tick`, warnings: [] });
  }));
  return writes;
}

describe("SettingsTab", () => {
  it("saves an edited value and then shows it as the deployment's own", async () => {
    const writes = fakeBackend();
    render(<SettingsTab repo="demo-save" />);
    const input = await screen.findByLabelText("Fix legs at once");
    const editor = input.closest(".settings-row") as HTMLElement;

    await userEvent.clear(input);
    await userEvent.type(input, "4");
    await userEvent.click(within(editor).getByRole("button", { name: "Save" }));

    expect(writes).toEqual([{ key: "FIX_DISPATCH_CONCURRENCY", value: "4", scope: "repo" }]);
    expect(await screen.findByText(/applies at the next tick/)).toBeTruthy();
    expect(await screen.findByDisplayValue("4")).toBeTruthy();
  });

  it("shows the server's reason when a value is refused and keeps the draft for correction", async () => {
    fakeBackend();
    render(<SettingsTab repo="demo-refuse" />);
    const input = await screen.findByLabelText("Fix legs at once");
    const editor = input.closest(".settings-row") as HTMLElement;

    await userEvent.clear(input);
    await userEvent.type(input, "999");
    await userEvent.click(within(editor).getByRole("button", { name: "Save" }));

    expect(await screen.findByText(/must be 1\.\.16/)).toBeTruthy();
    // still unsaved: the draft differs from the deployment's value, which did not change
    expect((screen.getByLabelText("Fix legs at once") as HTMLInputElement).value).toBe("999");
    expect(within(editor).getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("writes to shared.env when that scope is chosen and unsets back to the default", async () => {
    const writes = fakeBackend();
    render(<SettingsTab repo="demo-shared" />);
    await screen.findByLabelText("Issue pickup");

    await userEvent.selectOptions(screen.getByLabelText("Save to"), "shared");
    await userEvent.selectOptions(screen.getByLabelText("Issue pickup"), "open");
    const pickup = screen.getByLabelText("Issue pickup").closest(".settings-row") as HTMLElement;
    await userEvent.click(within(pickup).getByRole("button", { name: "Save" }));
    await screen.findByText(/ISSUE_PICKUP saved/);

    const concurrency = screen.getByLabelText("Fix legs at once").closest(".settings-row") as HTMLElement;
    await userEvent.click(within(concurrency).getByRole("button", { name: "Unset" }));

    expect(writes).toEqual([
      { key: "ISSUE_PICKUP", value: "open", scope: "shared" },
      { key: "FIX_DISPATCH_CONCURRENCY", value: null, scope: "shared" },
    ]);
    expect(await screen.findByDisplayValue("1")).toBeTruthy();
  });

  it("edits a route by runtime, model and effort and sends the composed spec", async () => {
    const writes = fakeBackend();
    render(<SettingsTab repo="demo-route" />);
    const model = await screen.findByLabelText("Tier low");
    const editor = model.closest(".settings-row") as HTMLElement;

    await userEvent.selectOptions(within(editor).getByLabelText("Runtime"), "codex");
    await userEvent.clear(within(editor).getByLabelText("Tier low"));
    await userEvent.type(within(editor).getByLabelText("Tier low"), "gpt-6-luna");
    await userEvent.selectOptions(within(editor).getByLabelText("Effort"), "xhigh");
    await userEvent.click(within(editor).getByRole("button", { name: "Save" }));

    expect(writes).toEqual([{ key: "FIX_MODEL_LOW", value: "codex:gpt-6-luna:xhigh", scope: "repo" }]);
  });
});
