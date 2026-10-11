import { readRecord } from "../../lib/prefs";
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
const SORT_STORAGE_KEY = "issue-agent.dashboard.column-sort";
export const MIN_COLUMN_WIDTH = 112;

function storedWidths(key: string): Record<string, number> {
  try {
    const all = readRecord<Record<string, number>>(STORAGE_KEY);
    return all[key] ?? {};
  } catch {
    return {};
  }
}

function persistWidths(key: string, widths: Record<string, number>) {
  try {
    const all = readRecord<Record<string, number>>(STORAGE_KEY);
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...all, [key]: widths }));
  } catch {
    // Resizing remains available for this session when storage is disabled.
  }
}

function storedOrder(key: string): string[] {
  try {
    const all = readRecord<string[]>(ORDER_STORAGE_KEY);
    return Array.isArray(all[key]) ? all[key] : [];
  } catch {
    return [];
  }
}

function persistOrder(key: string, order: string[]) {
  try {
    const all = readRecord<string[]>(ORDER_STORAGE_KEY);
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

/**
 * A remembered sort is only honoured when it still names a column this table
 * can sort by *and currently shows*: column sets change between releases, and
 * a stale key would silently sort every row by `undefined` — an unsorted table
 * that claims to be sorted. A key naming a column the reader has hidden is the
 * same failure wearing a different hat: the rows come back in an order with no
 * visible explanation and no header carrying the sort indicator. Anything
 * unparseable falls back to the caller's initial sort.
 */
function storedSort(
  id: string | undefined,
  sortableKeys: readonly string[],
  fallback: TableSort,
  visibleKeys?: ReadonlySet<string>,
): TableSort {
  if (!id) return fallback;
  try {
    const all = readRecord<unknown>(SORT_STORAGE_KEY);
    const saved = all[id] as { key?: unknown; direction?: unknown } | undefined;
    if (
      !saved
      || typeof saved.key !== "string"
      || !sortableKeys.includes(saved.key)
      || (visibleKeys !== undefined && !visibleKeys.has(saved.key))
    ) {
      return fallback;
    }
    return { key: saved.key, direction: saved.direction === "desc" ? "desc" : "asc" };
  } catch {
    return fallback;
  }
}

function persistSort(id: string | undefined, sort: TableSort) {
  if (!id) return;
  try {
    const all = readRecord<TableSort>(SORT_STORAGE_KEY);
    localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify({ ...all, [id]: sort }));
  } catch {
    // Sorting remains available for this session when storage is disabled.
  }
}

/**
 * `storageId` is the stable per-table identity (the same string the table
 * passes as `storageKey`); omit it and the sort is session-only. `visibleKeys`
 * is the set of column keys the table is rendering right now; omit it and a
 * remembered sort is only checked against what the table can sort by.
 */
export function useSortedRows<T>(
  rows: readonly T[],
  values: Record<string, (row: T) => SortValue>,
  initial: TableSort,
  storageId?: string,
  visibleKeys?: ReadonlySet<string>,
) {
  const [sort, setSort] = useState<TableSort>(
    () => storedSort(storageId, Object.keys(values), initial, visibleKeys),
  );
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
  // Persistence belongs to the event, not to the state updater: React may call
  // an updater more than once for one interaction (StrictMode does so on
  // purpose), and a reducer that writes to storage is not a pure function of
  // its input.
  const toggleSort = useCallback((key: string) => {
    const next: TableSort = sort.key === key
      ? { key, direction: sort.direction === "asc" ? "desc" : "asc" }
      : { key, direction: "asc" };
    setSort(next);
    persistSort(storageId, next);
  }, [sort, storageId]);
  return { rows: sorted, sort, onSort: toggleSort };
}

export function ResizableTable({
  storageKey,
  columns,
  children,
  className = "",
  ariaLabel,
  sort,
  onSort,
}: {
  storageKey: string;
  columns: TableColumn[];
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
  sort?: TableSort;
  onSort?: (key: string) => void;
}) {
  const [widths, setWidths] = useState<Record<string, number>>(() => storedWidths(storageKey));
  const [order, setOrder] = useState<string[]>(() => storedOrder(storageKey));
  const [dragging, setDragging] = useState<string | null>(null);
  // A pointer drag fires faster than React re-renders, so the handlers need the
  // latest value without waiting for a commit. These mirror the state so the
  // handler — not the state updater — owns both the next value and its write to
  // storage: an updater that persists is not a pure function of its input, and
  // React is free to call it twice for one interaction.
  const widthsRef = useRef(widths);
  const orderRef = useRef(order);
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
    const next = {
      ...widthsRef.current,
      [column.key]: Math.max(minWidthFor(column), Math.round(width)),
    };
    widthsRef.current = next;
    setWidths(next);
    persistWidths(storageKey, next);
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
    const current = orderRef.current;
    const activeKeys = new Set(sourceKeys);
    const base = [
      ...current.filter((key, index) => current.indexOf(key) === index),
      ...sourceKeys.filter((key) => !current.includes(key)),
    ];
    const activeOrder = base.filter((key) => activeKeys.has(key));
    const fromIndex = activeOrder.indexOf(from);
    const toIndex = activeOrder.indexOf(to);
    if (fromIndex < 0 || toIndex < 0) return;
    activeOrder.splice(fromIndex, 1);
    activeOrder.splice(toIndex, 0, from);
    let activeIndex = 0;
    const next = base.map((key) => (
      activeKeys.has(key) ? activeOrder[activeIndex++]! : key
    ));
    orderRef.current = next;
    setOrder(next);
    persistOrder(storageKey, next);
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
      style={{ width: tableWidth, minWidth: "100%" }}
      aria-label={ariaLabel}
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
