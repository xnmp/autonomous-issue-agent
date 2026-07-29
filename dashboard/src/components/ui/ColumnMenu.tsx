import { useEffect, useRef } from "react";

export interface ColumnChoice {
  key: string;
  label: string;
}

export function ColumnMenu({
  columns,
  visible,
  onChange,
  position,
  onClose,
}: {
  columns: ColumnChoice[];
  visible: ReadonlySet<string>;
  onChange: (key: string, shown: boolean) => void;
  position: { x: number; y: number } | null;
  onClose: () => void;
}) {
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!position) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) onClose();
    };
    const closeFromKeyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", closeFromKeyboard);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", closeFromKeyboard);
    };
  }, [onClose, position]);

  if (!position) return null;
  return (
    <div
      className="column-menu-popover panel"
      ref={root}
      role="menu"
      aria-label="Visible columns"
      style={{ left: position.x, top: position.y }}
    >
      <div className="column-menu-title">Visible columns</div>
      {columns.map((column) => (
        <label className="column-menu-item" key={column.key} role="menuitemcheckbox" aria-checked={visible.has(column.key)}>
          <input
            type="checkbox"
            checked={visible.has(column.key)}
            onChange={(event) => onChange(column.key, event.target.checked)}
          />
          <span>{column.label}</span>
        </label>
      ))}
    </div>
  );
}
