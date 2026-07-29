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

export type SortDirection = "asc" | "desc";

export interface TableSort {
  key: string;
  direction: SortDirection;
}

const STORAGE_KEY = "issue-agent.dashboard.column-widths";
export const MIN_COLUMN_WIDTH = 112;

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

type SortValue = string | number | boolean | null | undefined;

function compareValues(left: SortValue, right: SortValue): number {
  if (left === null || left === undefined) return right === null || right === undefined ? 0 : 1;
  if (right === null || right === undefined) return -1;
  if (typeof left === "number" && typeof right === "number") return left - right;
  if (typeof left === "boolean" && typeof right === "boolean") return Number(left) - Number(right);
  return String(left).localeCompare(String(right), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}

export function useSortedRows<T>(
  rows: readonly T[],
  values: Record<string, (row: T) => SortValue>,
  initial: TableSort,
) {
  const [sort, setSort] = useState<TableSort>(initial);
  const sorted = useMemo(
    () => rows
      .map((row, index) => ({ row, index }))
      .sort((left, right) => {
        const compared = compareValues(values[sort.key]?.(left.row), values[sort.key]?.(right.row));
        return (sort.direction === "asc" ? compared : -compared) || left.index - right.index;
      })
      .map(({ row }) => row),
    [rows, sort, values],
  );
  const toggleSort = useCallback((key: string) => {
    setSort((current) => (
      current.key === key
        ? { key, direction: current.direction === "asc" ? "desc" : "asc" }
        : { key, direction: "asc" }
    ));
  }, []);
  return { rows: sorted, sort, onSort: toggleSort };
}

export function ResizableTable({
  storageKey,
  columns,
  children,
  className = "",
  sort,
  onSort,
}: {
  storageKey: string;
  columns: TableColumn[];
  children: ReactNode;
  className?: string;
  sort?: TableSort;
  onSort?: (key: string) => void;
}) {
  const [widths, setWidths] = useState<Record<string, number>>(() => storedWidths(storageKey));
  const activeColumns = useMemo(() => columns.map((column) => column.key), [columns]);
  const widthFor = useCallback(
    (column: TableColumn) => Math.max(
      MIN_COLUMN_WIDTH,
      widths[column.key] ?? column.width ?? MIN_COLUMN_WIDTH,
    ),
    [widths],
  );
  const tableWidth = columns.reduce((total, column) => total + widthFor(column), 0);

  const setWidth = useCallback((column: string, width: number) => {
    setWidths((current) => {
      const next = { ...current, [column]: Math.max(MIN_COLUMN_WIDTH, Math.round(width)) };
      persistWidths(storageKey, next);
      return next;
    });
  }, [storageKey]);

  const beginResize = (event: PointerEvent<HTMLSpanElement>, column: TableColumn) => {
    event.preventDefault();
    event.stopPropagation();
    const originX = event.clientX;
    const originWidth = widthFor(column);
    const move = (next: globalThis.PointerEvent) => setWidth(
      column.key,
      originWidth + next.clientX - originX,
    );
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      document.body.classList.remove("is-resizing-column");
    };
    document.body.classList.add("is-resizing-column");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
    window.addEventListener("pointercancel", stop, { once: true });
  };

  const resizeFromKeyboard = (
    event: KeyboardEvent<HTMLSpanElement>,
    column: TableColumn,
  ) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    setWidth(
      column.key,
      widthFor(column) + (event.key === "ArrowRight" ? 12 : -12),
    );
  };

  return (
    <table
      className={`tbl resizable-table ${className}`.trim()}
      style={{ width: tableWidth }}
    >
      <caption className="sr-only">
        Columns are resizable. Use the separator handles or arrow keys.
        {activeColumns.length > 0 ? ` Columns: ${activeColumns.join(", ")}.` : ""}
      </caption>
      <colgroup>
        {columns.map((column) => (
          <col
            key={column.key}
            style={{ width: widthFor(column) }}
          />
        ))}
      </colgroup>
      <thead>
        <tr>
          {columns.map((column) => (
            <th
              key={column.key}
              className={column.className}
              data-column-key={column.key}
              aria-sort={
                onSort
                  ? sort?.key === column.key
                    ? sort.direction === "asc" ? "ascending" : "descending"
                    : "none"
                  : undefined
              }
            >
              {onSort ? (
                <button
                  className="column-sort"
                  type="button"
                  onClick={() => onSort(column.key)}
                  aria-label={`Sort by ${typeof column.label === "string" ? column.label : column.key}`}
                >
                  <span className="column-heading">{column.label}</span>
                  <span className="sort-mark" aria-hidden="true">
                    {sort?.key === column.key ? (sort.direction === "asc" ? "↑" : "↓") : "↕"}
                  </span>
                </button>
              ) : (
                <span className="column-heading">{column.label}</span>
              )}
              <span
                className="column-resizer"
                role="separator"
                aria-label={`Resize ${typeof column.label === "string" ? column.label : column.key} column`}
                aria-orientation="vertical"
                aria-valuemin={MIN_COLUMN_WIDTH}
                aria-valuenow={widthFor(column)}
                tabIndex={0}
                onPointerDown={(event) => beginResize(event, column)}
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
