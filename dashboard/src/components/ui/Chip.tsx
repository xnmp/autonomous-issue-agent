import type { ReactNode } from "react";
import type { Tone } from "../../lib/semantics";

interface ChipProps {
  tone?: Tone;
  children: ReactNode;
  /** monospace + square corners — for ids, kinds, models */
  square?: boolean;
  /** running legs pulse; honoured only outside prefers-reduced-motion (CSS) */
  pulse?: boolean;
  title?: string;
  className?: string;
}

/**
 * The one badge component. Colour arrives as a semantic tone, never a hex, so
 * the mapping in tokens.css is the only place a colour decision lives.
 */
export function Chip({
  tone = "neutral",
  children,
  square,
  pulse,
  title,
  className,
}: ChipProps) {
  const cls = [
    "chip",
    `chip-${tone}`,
    square ? "chip-sq" : "",
    pulse ? "pulse" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

/** A bare status dot (sidebar role health). Always paired with a title/label —
 * colour alone is never the only channel. */
export function Dot({ tone, title }: { tone: Tone; title: string }) {
  return <span className={`dot dot-${tone}`} title={title} aria-label={title} role="img" />;
}
