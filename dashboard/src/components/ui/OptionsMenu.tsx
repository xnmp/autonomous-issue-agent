import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { DeploymentSettings } from "../DeploymentSettings";
import { Icon } from "./Icon";

export const DASHBOARD_THEMES = [
  ["cosmic-dusk", "Operator"],
  ["hacker", "Hacker"],
  ["monokai", "Monokai"],
  ["ayu-mirage", "Ayu Mirage"],
  ["gruvbox", "Gruvbox"],
  ["horizon-dark", "Horizon Dark"],
  ["nord", "Nord"],
  ["tokyo-night", "Tokyo Night"],
  ["catppuccin-latte", "Catppuccin Latte (light)"],
] as const;

export type DashboardTheme = typeof DASHBOARD_THEMES[number][0];

export interface DashboardOptions {
  showPerRepoQuotas: boolean;
  showDurationSeconds: boolean;
  showOverviewIssueHealthTrend: boolean;
  theme: DashboardTheme;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function DashboardOptionsForm({
  options,
  onChange,
}: {
  options: DashboardOptions;
  onChange: (options: DashboardOptions) => void;
}) {
  return (
    <div className="options-form" role="group" aria-label="Dashboard options">
      <label className="options-theme">
        <span>Theme</span>
        <select
          value={options.theme}
          onChange={(event) => onChange({
            ...options,
            theme: event.target.value as DashboardTheme,
          })}
        >
          {DASHBOARD_THEMES.map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </label>
      <fieldset className="options-section">
        <legend>Overview</legend>
        <label className="column-menu-item">
          <input
            type="checkbox"
            checked={options.showOverviewIssueHealthTrend}
            onChange={(event) => onChange({
              ...options,
              showOverviewIssueHealthTrend: event.target.checked,
            })}
          />
          <span>Issue health trend</span>
        </label>
      </fieldset>
      <label className="column-menu-item">
        <input
          type="checkbox"
          checked={options.showPerRepoQuotas}
          onChange={(event) => onChange({ ...options, showPerRepoQuotas: event.target.checked })}
        />
        <span>Show per-repo quotas</span>
      </label>
      <label className="column-menu-item">
        <input
          type="checkbox"
          checked={options.showDurationSeconds}
          onChange={(event) => onChange({
            ...options,
            showDurationSeconds: event.target.checked,
          })}
        />
        <span>Show seconds in durations</span>
      </label>
    </div>
  );
}

/**
 * The one settings surface: dashboard display options plus the selected
 * deployment's live harness settings. A modal dialog, because the deployment
 * form is too large for a popover. Escape, the close button and a backdrop
 * click close it; focus moves in on open, is held inside while open, and the
 * opener restores it on close.
 */
function SettingsDialog({
  options,
  onChange,
  repo,
  onClose,
}: {
  options: DashboardOptions;
  onChange: (options: DashboardOptions) => void;
  repo: string | null;
  onClose: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    panel.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeRef.current();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const items = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) return;
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === panel.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        ref={panel}
        className="settings-dialog panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-dialog-title"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="panel-hd">
          <span className="panel-title" id="settings-dialog-title">Settings</span>
          <button className="btn btn-xs btn-ghost" aria-label="Close settings" onClick={onClose}>
            <Icon name="close" />
          </button>
        </div>
        <div className="settings-dialog-body">
          <section className="settings-dialog-section" aria-label="Dashboard">
            <h2 className="settings-dialog-heading">Dashboard</h2>
            <DashboardOptionsForm options={options} onChange={onChange} />
          </section>
          <section className="settings-dialog-section" aria-label="Deployment">
            <h2 className="settings-dialog-heading">
              Deployment{repo ? <> · <span className="mono">{repo}</span></> : null}
            </h2>
            {repo ? (
              <DeploymentSettings repo={repo} />
            ) : (
              <p className="faint">Select a deployment to edit its live settings.</p>
            )}
          </section>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function OptionsMenu({
  options,
  onChange,
  repo,
  collapsed = false,
}: {
  options: DashboardOptions;
  onChange: (options: DashboardOptions) => void;
  repo: string | null;
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);

  const close = () => {
    setOpen(false);
    trigger.current?.focus();
  };

  return (
    <div className="options-menu">
      <button
        ref={trigger}
        className={`rail-btn options-trigger${collapsed ? " icon-only" : ""}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label="Settings"
        title="Settings"
        onClick={() => setOpen(true)}
      >
        <span className="rail-btn-mark settings-icon" aria-hidden="true"><Icon name="settings" /></span>
        {!collapsed ? <span>Settings</span> : null}
      </button>
      {open ? <SettingsDialog options={options} onChange={onChange} repo={repo} onClose={close} /> : null}
    </div>
  );
}
