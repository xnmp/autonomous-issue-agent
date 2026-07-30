import {
  Children,
  cloneElement,
  isValidElement,
  useCallback,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type ReactElement,
  type ReactNode,
} from "react";

export interface TableColumn {
  key: string;
  label: ReactNode;
  className?: string;
  width?: number;
  minWidth?: number;
}

export type SortDirection = "asc" | "desc";

export interface TableSort {
  key: string;
  direction: SortDirection;
}

const STORAGE_KEY = "issue-agent.dashboard.column-widths";
const ORDER_STORAGE_KEY = "issue-agent.dashboard.column-order";
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

function storedOrder(key: string): string[] {
  try {
    const all = JSON.parse(localStorage.getItem(ORDER_STORAGE_KEY) ?? "{}") as Record<string, string[]>;
    return Array.isArray(all[key]) ? all[key] : [];
  } catch {
    return [];
  }
}

function persistOrder(key: string, order: string[]) {
  try {
    const all = JSON.parse(localStorage.getItem(ORDER_STORAGE_KEY) ?? "{}") as Record<string, string[]>;
    localStorage.setItem(ORDER_STORAGE_KEY, JSON.stringify({ ...all, [key]: order }));
  } catch {
    // Reordering remains available for this session when storage is disabled.
  }
}

function reconcileOrder(saved: readonly string[], active: readonly string[]): string[] {
  const activeKeys = new Set(active);
  const known = saved.filter((key, index) => activeKeys.has(key) && saved.indexOf(key) === index);
  const knownKeys = new Set(known);
  return [...known, ...active.filter((key) => !knownKeys.has(key))];
}

function reorderRowCells(
  children: ReactNode,
  sourceKeys: readonly string[],
  orderedKeys: readonly string[],
): ReactNode {
  return Children.map(children, (child) => {
    if (!isValidElement(child)) return child;
    const element = child as ReactElement<{ children?: ReactNode }>;
    if (element.type === "tr") {
      const cells = Children.toArray(element.props.children);
      if (cells.length !== sourceKeys.length) return element;
      const cellsByKey = new Map(sourceKeys.map((key, index) => [key, cells[index]]));
      return cloneElement(element, undefined, orderedKeys.map((key) => cellsByKey.get(key)));
    }
    if (element.props.children === undefined) return element;
    return cloneElement(
      element,
      undefined,
      reorderRowCells(element.props.children, sourceKeys, orderedKeys),
    );
  });
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
  const [order, setOrder] = useState<string[]>(() => storedOrder(storageKey));
  const [dragging, setDragging] = useState<string | null>(null);
  const draggedColumn = useRef<string | null>(null);
  const sourceKeys = useMemo(() => columns.map((column) => column.key), [columns]);
  const orderedKeys = useMemo(() => reconcileOrder(order, sourceKeys), [order, sourceKeys]);
  const columnsByKey = useMemo(
    () => new Map(columns.map((column) => [column.key, column])),
    [columns],
  );
  const orderedColumns = useMemo(
    () => orderedKeys.flatMap((key) => {
      const column = columnsByKey.get(key);
      return column ? [column] : [];
    }),
    [columnsByKey, orderedKeys],
  );
  const minWidthFor = useCallback(
    (column: TableColumn) => column.minWidth ?? MIN_COLUMN_WIDTH,
    [],
  );
  const widthFor = useCallback(
    (column: TableColumn) => Math.max(
      minWidthFor(column),
      widths[column.key] ?? column.width ?? MIN_COLUMN_WIDTH,
    ),
    [minWidthFor, widths],
  );
  const tableWidth = orderedColumns.reduce((total, column) => total + widthFor(column), 0);

  const setWidth = useCallback((column: TableColumn, width: number) => {
    setWidths((current) => {
      const next = {
        ...current,
        [column.key]: Math.max(minWidthFor(column), Math.round(width)),
      };
      persistWidths(storageKey, next);
      return next;
    });
  }, [minWidthFor, storageKey]);

  const beginResize = (event: PointerEvent<HTMLSpanElement>, column: TableColumn) => {
    event.preventDefault();
    event.stopPropagation();
    const originX = event.clientX;
    const originWidth = widthFor(column);
    const move = (next: globalThis.PointerEvent) => setWidth(
      column,
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
    event.stopPropagation();
    if (event.altKey) return;
    event.preventDefault();
    setWidth(
      column,
      widthFor(column) + (event.key === "ArrowRight" ? 12 : -12),
    );
  };

  const moveColumn = useCallback((from: string, to: string) => {
    if (from === to) return;
    setOrder((current) => {
      const activeKeys = new Set(sourceKeys);
      const base = [
        ...current.filter((key, index) => current.indexOf(key) === index),
        ...sourceKeys.filter((key) => !current.includes(key)),
      ];
      const activeOrder = base.filter((key) => activeKeys.has(key));
      const fromIndex = activeOrder.indexOf(from);
      const toIndex = activeOrder.indexOf(to);
      if (fromIndex < 0 || toIndex < 0) return current;
      activeOrder.splice(fromIndex, 1);
      activeOrder.splice(toIndex, 0, from);
      let activeIndex = 0;
      const next = base.map((key) => (
        activeKeys.has(key) ? activeOrder[activeIndex++]! : key
      ));
      persistOrder(storageKey, next);
      return next;
    });
  }, [sourceKeys, storageKey]);

  const moveColumnFromKeyboard = (
    event: KeyboardEvent<HTMLTableCellElement>,
    column: TableColumn,
  ) => {
    if (!event.altKey || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) return;
    event.preventDefault();
    const index = orderedKeys.indexOf(column.key);
    const target = orderedKeys[index + (event.key === "ArrowLeft" ? -1 : 1)];
    if (target) moveColumn(column.key, target);
  };

  return (
    <table
      className={`tbl resizable-table ${className}`.trim()}
      style={{ width: tableWidth }}
    >
      <caption className="sr-only">
        Columns are resizable and rearrangeable. Drag a column header to move it,
        or focus it and use Alt plus the left or right arrow key.
        {orderedKeys.length > 0 ? ` Columns: ${orderedKeys.join(", ")}.` : ""}
      </caption>
      <colgroup>
        {orderedColumns.map((column) => (
          <col
            key={column.key}
            style={{ width: widthFor(column) }}
          />
        ))}
      </colgroup>
      <thead>
        <tr>
          {orderedColumns.map((column) => (
            <th
              key={column.key}
              className={column.className}
              data-column-key={column.key}
              data-dragging={dragging === column.key || undefined}
              draggable
              tabIndex={0}
              title="Drag to rearrange column"
              onDragStart={() => {
                draggedColumn.current = column.key;
                setDragging(column.key);
              }}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                if (draggedColumn.current) moveColumn(draggedColumn.current, column.key);
              }}
              onDragEnd={() => {
                draggedColumn.current = null;
                setDragging(null);
              }}
              onKeyDown={(event) => moveColumnFromKeyboard(event, column)}
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
                aria-valuemin={minWidthFor(column)}
                aria-valuenow={widthFor(column)}
                tabIndex={0}
                onPointerDown={(event) => beginResize(event, column)}
                onKeyDown={(event) => resizeFromKeyboard(event, column)}
              />
            </th>
          ))}
        </tr>
      </thead>
      {reorderRowCells(children, sourceKeys, orderedKeys)}
    </table>
  );
}
