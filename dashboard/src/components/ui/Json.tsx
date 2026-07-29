import { useState, type ReactNode } from "react";

/** Pretty-printed JSON, scroll-contained so a fat event never widens a table. */
export function JsonBlock({ value }: { value: unknown }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? "null";
  } catch {
    text = String(value);
  }
  return <pre className="json">{text}</pre>;
}

/** Uncontrolled disclosure with a consistent chevron. */
export function Collapsible({
  title,
  right,
  defaultOpen = false,
  children,
}: {
  title: ReactNode;
  right?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`collapsible${open ? " open" : ""}`}>
      <button className="collapsible-hd" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="chev" aria-hidden="true">
          ▸
        </span>
        <span className="collapsible-title">{title}</span>
        {right ? <span className="spacer" /> : null}
        {right}
      </button>
      {open ? <div className="collapsible-bd">{children}</div> : null}
    </div>
  );
}
