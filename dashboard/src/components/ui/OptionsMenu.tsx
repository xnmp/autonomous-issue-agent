import { useEffect, useRef, useState } from "react";

export interface DashboardOptions {
  showProjectedCost: boolean;
  showPerRepoQuotas: boolean;
}

export function OptionsMenu({
  options,
  onChange,
}: {
  options: DashboardOptions;
  onChange: (options: DashboardOptions) => void;
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
        className="btn btn-xs btn-ghost options-trigger"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((current) => !current)}
      >
        Options <span aria-hidden="true">⋯</span>
      </button>
      {open ? (
        <div className="options-popover panel" role="menu" aria-label="Dashboard options">
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
        </div>
      ) : null}
    </div>
  );
}
