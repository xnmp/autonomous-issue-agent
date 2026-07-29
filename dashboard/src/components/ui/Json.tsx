import { useMemo, useState, type ReactNode } from "react";

/** Pretty-printed JSON, scroll-contained so a fat event never widens a table. */
export function JsonBlock({ value }: { value: unknown }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? "null";
  } catch {
    text = String(value);
  }
  const [copied, setCopied] = useState(false);
  const tokens = useMemo(() => highlightJson(text), [text]);
  return (
    <div className="json-block">
      <div className="json-toolbar">
        <span>JSON</span>
        <button
          className="btn btn-xs btn-ghost"
          onClick={() => {
            void navigator.clipboard?.writeText(text).then(
              () => setCopied(true),
              () => setCopied(false),
            );
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="json"><code>{tokens}</code></pre>
    </div>
  );
}

function highlightJson(text: string): ReactNode[] {
  const tokenPattern =
    /("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"\s*:)|("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*")|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false)\b|\b(null)\b/g;
  const output: ReactNode[] = [];
  let cursor = 0;
  for (const match of text.matchAll(tokenPattern)) {
    const index = match.index ?? 0;
    if (index > cursor) output.push(text.slice(cursor, index));
    const className = match[1]
      ? "json-key"
      : match[2]
        ? "json-string"
        : match[3]
          ? "json-number"
          : match[4]
            ? "json-boolean"
            : "json-null";
    output.push(<span className={className} key={index}>{match[0]}</span>);
    cursor = index + match[0].length;
  }
  if (cursor < text.length) output.push(text.slice(cursor));
  return output;
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
