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
        <div
          key={t.key}
          role="presentation"
          className={`tab${t.active ? " active" : ""}${t.onClose ? " tab-closeable" : ""}`}
        >
          <button
            className="tab-main"
            role="tab"
            aria-selected={t.active}
            title={t.title}
            aria-keyshortcuts={t.onClose ? "Delete" : undefined}
            onClick={t.onSelect}
            onKeyDown={(e) => {
              if (t.onClose && (e.key === "Delete" || e.key === "Backspace")) {
                e.preventDefault();
                t.onClose();
              }
            }}
          >
            {t.label}
          </button>
          {/* A tablist may only contain tabs, so the pointer affordance is not
              a control of its own: assistive tech and keyboards close the tab
              with Delete on the tab itself. */}
          {t.onClose ? (
            <span
              className="tab-close"
              aria-hidden="true"
              title={`Close ${t.title}`}
              onClick={(e) => {
                e.stopPropagation();
                t.onClose?.();
              }}
            >
              <Icon name="close" size={13} />
            </span>
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
