import { useState } from "react";
import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import type { SettingRow, SettingsPayload, SettingWriteResult } from "../api/types";
import { formatRoute, parseRoute, type RouteParts } from "../features/settings/routeSpec";
import { Chip } from "./ui/Chip";
import { ErrorBanner, SectionTitle, SkeletonRows, WarningList } from "./ui/Feedback";

const POLL_MS = 30_000;

type Scope = "repo" | "shared";

const GROUPS: { id: SettingRow["group"]; title: string }[] = [
  { id: "routes", title: "Model routes" },
  { id: "dispatch", title: "Dispatch" },
  { id: "caps", title: "Spend caps" },
];

const SOURCE_LABEL: Record<SettingRow["source"], string> = {
  repo: "repo overlay",
  shared: "shared.env",
  default: "default",
};

interface Outcome {
  key: string;
  ok: boolean;
  detail: string;
  warnings: string[];
}

/**
 * The deployment's live knobs (ADR 0007 amendment). A save edits one file —
 * this repo's overlay or shared.env — and the running daemons apply it at
 * their next tick; nothing restarts. The server validates every write with the
 * daemons' own parser and refuses what they would refuse.
 */
export function DeploymentSettings({ repo }: { repo: string }) {
  const settings = usePolled(`settings:${repo}`, (signal) => api.settings(repo, signal), POLL_MS);
  const [scope, setScope] = useState<Scope>("repo");
  const [saving, setSaving] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const data = settings.data;

  const save = async (key: string, value: string | null) => {
    setSaving(key);
    setOutcome(null);
    try {
      const result: SettingWriteResult = await api.setSetting(repo, { key, value, scope });
      setOutcome({ key, ok: result.ok, detail: result.detail, warnings: result.warnings ?? [] });
    } catch (e) {
      setOutcome({ key, ok: false, detail: e instanceof Error ? e.message : String(e), warnings: [] });
    } finally {
      setSaving(null);
      settings.refresh();
    }
  };

  return (
    <div className="settings">
      <SectionTitle
        right={
          <label className="settings-scope">
            Save to{" "}
            <select value={scope} onChange={(e) => setScope(e.target.value as Scope)} aria-label="Save to">
              <option value="repo">this repo's overlay</option>
              <option value="shared">shared.env (every deployment)</option>
            </select>
          </label>
        }
      >
        Live settings
      </SectionTitle>
      <p className="faint settings-intro">
        Applied by the running daemons at their next tick, no restart. In-flight legs keep the
        settings they started with.
        {data ? <> Writing <span className="mono">{data.files[scope]}</span>.</> : null}
      </p>
      {settings.error ? <ErrorBanner message={settings.error} onRetry={settings.refresh} stale={!!data} /> : null}
      <WarningList warnings={data?.warnings} />
      {outcome ? <OutcomeBanner outcome={outcome} /> : null}
      {settings.loading ? <SkeletonRows what="settings" rows={6} /> : null}
      {data
        ? GROUPS.map((group) => (
            <section key={group.id} className="panel settings-group">
              <div className="panel-hd"><span className="panel-title">{group.title}</span></div>
              <div className="panel-bd">
                {data.settings
                  .filter((row) => row.group === group.id)
                  .map((row) => (
                    <SettingEditor
                      // A value changed on disk remounts the editor with it.
                      key={`${row.key}:${row.source}:${row.value}`}
                      row={row}
                      payload={data}
                      busy={saving !== null}
                      onSave={(value) => void save(row.key, value)}
                    />
                  ))}
              </div>
            </section>
          ))
        : null}
    </div>
  );
}

function OutcomeBanner({ outcome }: { outcome: Outcome }) {
  return (
    <div className={`banner ${outcome.ok ? "banner-ok" : "banner-error"}`} role="status">
      <span className="banner-msg">
        <span className="mono">{outcome.key}</span>: {outcome.detail}
        {outcome.warnings.length ? (
          <ul className="settings-warnings">
            {outcome.warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        ) : null}
      </span>
    </div>
  );
}

function SettingEditor({
  row,
  payload,
  busy,
  onSave,
}: {
  row: SettingRow;
  payload: SettingsPayload;
  busy: boolean;
  onSave: (value: string | null) => void;
}) {
  const [draft, setDraft] = useState(row.value);
  const id = `setting-${row.key}`;
  const dirty = draft !== row.value;
  return (
    <div className="settings-row">
      <div className="settings-label">
        <label htmlFor={id}>{row.label}</label>
        <span className="mono faint">{row.key}</span>
        <span className="faint settings-help">{row.help}</span>
      </div>
      <div className="settings-control">
        <Control id={id} row={row} payload={payload} value={draft} onChange={setDraft} />
        <div className="settings-meta">
          <Chip tone={row.source === "default" ? "neutral" : "running"}>{SOURCE_LABEL[row.source]}</Chip>
          {row.effective ? (
            <span className="faint" title="What a leg runs on now, after fallbacks">
              runs <span className="mono">{row.effective}</span>
            </span>
          ) : null}
          {row.default && row.source !== "default" ? (
            <span className="faint">default <span className="mono">{row.default}</span></span>
          ) : null}
        </div>
      </div>
      <div className="settings-actions">
        <button
          className="btn btn-xs btn-primary"
          disabled={busy || !dirty}
          onClick={() => onSave(draft.trim() === "" ? null : draft.trim())}
        >
          Save
        </button>
        <button
          className="btn btn-xs btn-ghost"
          disabled={busy || row.source === "default"}
          title="Remove the assignment from the chosen file; the next layer or the default applies"
          onClick={() => onSave(null)}
        >
          Unset
        </button>
      </div>
    </div>
  );
}

function Control({
  id,
  row,
  payload,
  value,
  onChange,
}: {
  id: string;
  row: SettingRow;
  payload: SettingsPayload;
  value: string;
  onChange: (value: string) => void;
}) {
  if (row.kind === "choice") {
    return (
      <select id={id} className="settings-input" value={value} onChange={(e) => onChange(e.target.value)}>
        {(row.choices ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    );
  }
  if (row.kind === "int" || row.kind === "usd") {
    return (
      <input
        id={id}
        className="settings-input settings-number"
        type="number"
        inputMode={row.kind === "int" ? "numeric" : "decimal"}
        min={row.kind === "int" ? 1 : 0}
        step={row.kind === "int" ? 1 : "any"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  if (row.kind === "model") {
    const runtime = row.runtime ?? "";
    return (
      <span className="settings-route">
        <span className="mono faint">{runtime}:</span>
        <ModelInput id={id} runtime={runtime} payload={payload} value={value} onChange={onChange} />
      </span>
    );
  }
  return <RouteControl id={id} payload={payload} value={value} onChange={onChange} />;
}

function ModelInput({
  id,
  runtime,
  payload,
  value,
  onChange,
}: {
  id: string;
  runtime: string;
  payload: SettingsPayload;
  value: string;
  onChange: (value: string) => void;
}) {
  const list = `${id}-models`;
  return (
    <>
      <input
        id={id}
        className="settings-input mono"
        list={list}
        placeholder="runtime default"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={list}>
        {(payload.models[runtime] ?? []).map((m) => <option key={m} value={m} />)}
      </datalist>
    </>
  );
}

function RouteControl({
  id,
  payload,
  value,
  onChange,
}: {
  id: string;
  payload: SettingsPayload;
  value: string;
  onChange: (value: string) => void;
}) {
  const parts = parseRoute(value, payload.efforts);
  const set = (patch: Partial<RouteParts>) => onChange(formatRoute({ ...parts, ...patch }) ?? "");
  return (
    <span className="settings-route">
      <select
        aria-label="Runtime"
        className="settings-input"
        value={parts.runtime}
        onChange={(e) => set({ runtime: e.target.value, effort: "" })}
      >
        <option value="">inherit</option>
        {payload.runtimes.map((r) => <option key={r} value={r}>{r}</option>)}
      </select>
      {parts.runtime ? (
        <>
          <ModelInput
            id={id}
            runtime={parts.runtime}
            payload={payload}
            value={parts.model}
            onChange={(model) => set({ model })}
          />
          <select
            aria-label="Effort"
            className="settings-input"
            value={parts.effort}
            onChange={(e) => set({ effort: e.target.value })}
          >
            <option value="">runtime default</option>
            {(payload.efforts[parts.runtime] ?? []).map((e) => <option key={e} value={e}>{e}</option>)}
          </select>
        </>
      ) : null}
    </span>
  );
}
