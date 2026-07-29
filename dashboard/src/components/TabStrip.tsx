import type { ReactNode } from "react";

export interface TabDef {
  key: string;
  label: ReactNode;
  /** aria/tooltip text — `label` may be a node with badges in it */
  title: string;
  active: boolean;
  onSelect: () => void;
  onClose?: () => void;
}

/**
 * The tab strip. Persistent per-repo tabs (Overview | Issues) first, then the
 * dynamically opened closeable tabs (issues, verification) in the order they
 * were opened — never re-sorted, so a tab does not move under the cursor.
 */
export function TabStrip({ tabs }: { tabs: TabDef[] }) {
  return (
    <div className="tabstrip" role="tablist">
      {tabs.map((t) => (
        <div key={t.key} className={`tab${t.active ? " active" : ""}`}>
          <button
            className="tab-main"
            role="tab"
            aria-selected={t.active}
            title={t.title}
            onClick={t.onSelect}
          >
            {t.label}
          </button>
          {t.onClose ? (
            <button
              className="tab-close"
              title={`Close ${t.title}`}
              aria-label={`Close ${t.title}`}
              onClick={(e) => {
                e.stopPropagation();
                t.onClose?.();
              }}
            >
              ✕
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** Sub-tab pills (Issues: In flight & pending | Done | Backlog). */
export function SubTabs<T extends string>({
  value,
  options,
  labels,
  counts,
  onChange,
}: {
  value: T;
  options: readonly T[];
  labels: Record<T, string>;
  counts?: Partial<Record<T, number>>;
  onChange: (v: T) => void;
}) {
  return (
    <div className="subtabs" role="tablist">
      {options.map((o) => (
        <button
          key={o}
          role="tab"
          aria-selected={o === value}
          className={`subtab${o === value ? " active" : ""}`}
          onClick={() => onChange(o)}
        >
          {labels[o]}
          {counts?.[o] !== undefined ? (
            <span className="subtab-count num">{counts[o]}</span>
          ) : null}
        </button>
      ))}
    </div>
  );
}
