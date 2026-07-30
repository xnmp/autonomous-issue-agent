import { useEffect, useRef, useState } from "react";

export const DASHBOARD_THEMES = [
  ["cosmic-dusk", "Cosmic Dusk"],
  ["hacker", "Hacker"],
  ["monokai", "Monokai"],
  ["ayu-mirage", "Ayu Mirage"],
  ["gruvbox", "Gruvbox"],
  ["horizon-dark", "Horizon Dark"],
  ["nord", "Nord"],
  ["tokyo-night", "Tokyo Night"],
] as const;

export type DashboardTheme = typeof DASHBOARD_THEMES[number][0];

export interface DashboardOptions {
  showProjectedCost: boolean;
  showPerRepoQuotas: boolean;
  showDurationSeconds: boolean;
  theme: DashboardTheme;
}

export function OptionsMenu({
  options,
  onChange,
  collapsed = false,
}: {
  options: DashboardOptions;
  onChange: (options: DashboardOptions) => void;
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  return (
    <div className="options-menu" ref={root}>
      <button
        className={`rail-btn options-trigger${collapsed ? " icon-only" : ""}`}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label="Settings"
        title="Settings"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="rail-btn-mark settings-icon" aria-hidden="true">⚙</span>
        {!collapsed ? <span>Settings</span> : null}
      </button>
      {open ? (
        <div className="options-popover panel" role="group" aria-label="Dashboard options">
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
          <label className="column-menu-item">
            <input
              type="checkbox"
              checked={options.showProjectedCost}
              onChange={(event) => onChange({ ...options, showProjectedCost: event.target.checked })}
            />
            <span>Show projected cost</span>
          </label>
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
      ) : null}
    </div>
  );
}
