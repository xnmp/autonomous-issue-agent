import type { ReactNode } from "react";
import { Icon } from "./ui/Icon";

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
 * The tab strip. Persistent per-repo views come first, then dynamically opened
 * issue tabs in the order they were opened, so a tab does not move under the
 * cursor.
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
              <Icon name="close" size={13} />
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** Compact tabs for nested views such as issue task/event detail. */
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
