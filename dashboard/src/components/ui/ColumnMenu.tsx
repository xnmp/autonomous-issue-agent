import { useEffect, useId, useRef, useState } from "react";

export interface ColumnChoice {
  key: string;
  label: string;
}

export function ColumnMenu({
  columns,
  visible,
  onChange,
}: {
  columns: ColumnChoice[];
  visible: ReadonlySet<string>;
  onChange: (key: string, shown: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const popupId = useId();

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  return (
    <div className="column-menu" ref={root}>
      <button
        className="btn btn-xs btn-ghost"
        aria-expanded={open}
        aria-controls={popupId}
        onClick={() => setOpen((current) => !current)}
      >
        Columns <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div
          className="column-menu-popover panel"
          id={popupId}
          role="group"
          aria-label="Visible columns"
        >
          {columns.map((column) => (
            <label
              className="column-menu-item"
              key={column.key}
            >
              <input
                type="checkbox"
                checked={visible.has(column.key)}
                onChange={(event) => onChange(column.key, event.target.checked)}
              />
              <span>{column.label}</span>
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
}
