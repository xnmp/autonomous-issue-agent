import type { StageId } from "../../api/types";
import { tipProps } from "./Tip";

/** The five stages, in the order an item moves through them. */
export const CANONICAL: readonly { id: StageId; label: string }[] = [
  { id: "triage", label: "Triage" },
  { id: "resolve", label: "Resolve" },
  { id: "ci", label: "CI" },
  { id: "review", label: "Review" },
  { id: "merge", label: "Merge" },
];

/** One stage of the board's rail, from the server's stage-visit fold. */
export interface RailNode {
  id: StageId;
  label: string;
  /** Rewound: reached earlier, then sent back (e.g. a CI fix re-reviewed). */
  state: "done" | "current" | "rewound" | "future";
  count: string | null;
  /** Above typical: amber, with `hotTip` saying why. */
  hot: boolean;
  hotTip: string | null;
}

export type RailTone = "fail" | "ok" | "muted" | "warn";

/** The rail in words: the current stage with its count, then every other count. */
export function railSummary(nodes: readonly RailNode[]): string {
  const current = nodes.find((node) => node.state === "current");
  const here = current ? [current.label, current.count].filter(Boolean).join(", ") : "unknown";
  const elsewhere = nodes
    .filter((node) => node.state !== "current" && node.count)
    .map((node) => `${node.label} ${node.count}`);
  return [here, ...elsewhere].join("; ");
}

interface RailProps {
  /** The server's rail; the five stages are always all drawn. */
  rail: RailNode[];
  /** The current node takes its row's group colour. */
  tone: RailTone;
  /** A leg is running: the current node pulses. */
  live: boolean;
  label?: string;
  /** For a row control that names the rail in its description. */
  id?: string;
}

/** The five-stage track, from the server's rail, with round counts. */
export function LifecycleStepper(props: RailProps) {
  return <RailStepper {...props} />;
}

function RailStepper({ rail, tone, live, label = "Stage", id }: RailProps) {
  const current = rail.find((node) => node.state === "current");
  return (
    <ol id={id} className={`lifecycle-rail rail-tone-${tone}`} aria-label={`${label}: ${railSummary(rail)}`}>
      {rail.map((node) => (
        <li
          key={node.id}
          className={`rail-stage rail-${node.state}${node.state === "current" && live ? " rail-live" : ""}`}
          aria-current={node.state === "current" ? "step" : undefined}
          {...(node.state === "rewound" && current
            ? tipProps(`Reached earlier; now back at ${current.label}`)
            : {})}
        >
          <span className="rail-node" aria-hidden="true" />
          <span className="rail-label">{node.label}</span>
          {node.count ? (
            <span
              className={`rail-count num${node.hot ? " rail-hot" : ""}`}
              {...(node.hotTip ? tipProps(node.hotTip) : {})}
            >
              {node.count}
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
