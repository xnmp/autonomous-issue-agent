import {
  useCallback,
  useMemo,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from "react";

export interface TableColumn {
  key: string;
  label: ReactNode;
  className?: string;
  width?: number;
}

const STORAGE_KEY = "issue-agent.dashboard.column-widths";
const MIN_WIDTH = 72;

function storedWidths(key: string): Record<string, number> {
  try {
    const all = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") as Record<string, Record<string, number>>;
    return all[key] ?? {};
  } catch {
    return {};
  }
}

function persistWidths(key: string, widths: Record<string, number>) {
  try {
    const all = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") as Record<string, Record<string, number>>;
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...all, [key]: widths }));
  } catch {
    // Resizing remains available for this session when storage is disabled.
  }
}

export function ResizableTable({
  storageKey,
  columns,
  children,
  className = "",
}: {
  storageKey: string;
  columns: TableColumn[];
  children: ReactNode;
  className?: string;
}) {
  const [widths, setWidths] = useState<Record<string, number>>(() => storedWidths(storageKey));
  const activeColumns = useMemo(() => columns.map((column) => column.key), [columns]);

  const setWidth = useCallback((column: string, width: number) => {
    setWidths((current) => {
      const next = { ...current, [column]: Math.max(MIN_WIDTH, Math.round(width)) };
      persistWidths(storageKey, next);
      return next;
    });
  }, [storageKey]);

  const beginResize = (event: PointerEvent<HTMLSpanElement>, column: string) => {
    event.preventDefault();
    event.stopPropagation();
    const th = event.currentTarget.closest("th");
    if (!th) return;
    const originX = event.clientX;
    const originWidth = th.getBoundingClientRect().width;
    const move = (next: globalThis.PointerEvent) => setWidth(column, originWidth + next.clientX - originX);
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.classList.remove("is-resizing-column");
    };
    document.body.classList.add("is-resizing-column");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  };

  const resizeFromKeyboard = (
    event: KeyboardEvent<HTMLSpanElement>,
    column: TableColumn,
  ) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const th = event.currentTarget.closest("th");
    const measured = th?.getBoundingClientRect().width ?? 0;
    const current = widths[column.key] ?? (measured > 0 ? measured : column.width) ?? MIN_WIDTH;
    setWidth(column.key, current + (event.key === "ArrowRight" ? 12 : -12));
  };

  return (
    <table className={`tbl resizable-table ${className}`.trim()}>
      <caption className="sr-only">
        Columns are resizable. Use the separator handles or arrow keys.
        {activeColumns.length > 0 ? ` Columns: ${activeColumns.join(", ")}.` : ""}
      </caption>
      <colgroup>
        {columns.map((column) => (
          <col
            key={column.key}
            style={{ width: widths[column.key] ?? column.width }}
          />
        ))}
      </colgroup>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column.key} className={column.className} data-column-key={column.key}>
              <span className="column-heading">{column.label}</span>
              <span
                className="column-resizer"
                role="separator"
                aria-label={`Resize ${typeof column.label === "string" ? column.label : column.key} column`}
                aria-orientation="vertical"
                aria-valuemin={MIN_WIDTH}
                aria-valuenow={widths[column.key] ?? column.width ?? MIN_WIDTH}
                tabIndex={0}
                onPointerDown={(event) => beginResize(event, column.key)}
                onKeyDown={(event) => resizeFromKeyboard(event, column)}
              />
            </th>
          ))}
        </tr>
      </thead>
      {children}
    </table>
  );
}
