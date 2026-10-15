/**
 * A model route as the harness parses it (`domain/routing.parse_model_route`):
 * `runtime:model[:effort]`. The model may itself contain colons; only a final
 * segment that names one of the runtime's efforts is the effort.
 */

export interface RouteParts {
  runtime: string;
  model: string;
  effort: string;
}

export const EMPTY_ROUTE: RouteParts = { runtime: "", model: "", effort: "" };

export function parseRoute(spec: string, efforts: Record<string, string[]>): RouteParts {
  const text = spec.trim();
  const colon = text.indexOf(":");
  if (!text || colon < 0) return { ...EMPTY_ROUTE, model: text };
  const runtime = text.slice(0, colon);
  const rest = text.slice(colon + 1);
  const last = rest.lastIndexOf(":");
  const tail = last >= 0 ? rest.slice(last + 1) : "";
  if (last >= 0 && (efforts[runtime] ?? []).includes(tail)) {
    return { runtime, model: rest.slice(0, last), effort: tail };
  }
  return { runtime, model: rest, effort: "" };
}

/** The env value for `parts`; no runtime means "unset, inherit". */
export function formatRoute(parts: RouteParts): string | null {
  const runtime = parts.runtime.trim();
  if (!runtime) return null;
  const effort = parts.effort.trim();
  return `${runtime}:${parts.model.trim()}${effort ? `:${effort}` : ""}`;
}
