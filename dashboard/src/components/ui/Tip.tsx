/**
 * One fixed-position tooltip for the whole app, driven by plain event
 * handlers. The native `title` card sits behind the browser's ~1s hover
 * delay, which is an eternity when scanning a row of chips; this shows in
 * ~80ms. `position: fixed` keeps it out of the tables' overflow clipping.
 *
 * The text rides `data-tip` (also the test hook). `aria-label` is added only
 * for elements with no visible text of their own (dots, glyphs): on a chip
 * whose label is already visible it would REPLACE the accessible name with
 * the whole tooltip, renaming every button that contains one.
 */

let tip: HTMLDivElement | null = null;

function element(): HTMLDivElement {
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "tip";
    tip.setAttribute("role", "presentation");
    document.body.appendChild(tip);
    // Anything that moves the page moves the anchor out from under the tip.
    window.addEventListener("scroll", hideTip, true);
  }
  return tip;
}

export function hideTip(): void {
  tip?.classList.remove("tip-on");
}

function showTip(target: HTMLElement, text: string): void {
  const node = element();
  node.textContent = text;
  const rect = target.getBoundingClientRect();
  const gap = 6;
  const viewportPadding = 8;
  const bounds = node.getBoundingClientRect();
  const halfWidth = bounds.width / 2;
  const anchorCenter = rect.left + rect.width / 2;
  const left = Math.max(
    viewportPadding + halfWidth,
    Math.min(window.innerWidth - viewportPadding - halfWidth, anchorCenter),
  );
  const roomAbove = rect.top - viewportPadding;
  const roomBelow = window.innerHeight - rect.bottom - viewportPadding;
  const above = roomAbove >= bounds.height + gap
    || (roomBelow < bounds.height + gap && roomAbove > roomBelow);

  node.style.left = `${Math.round(left)}px`;
  if (above) {
    node.style.top = `${Math.round(Math.max(viewportPadding + bounds.height, rect.top - gap))}px`;
    node.classList.add("tip-above");
    node.classList.remove("tip-below");
  } else {
    node.style.top = `${Math.round(Math.min(
      window.innerHeight - viewportPadding - bounds.height,
      rect.bottom + gap,
    ))}px`;
    node.classList.add("tip-below");
    node.classList.remove("tip-above");
  }
  node.classList.add("tip-on");
}

/** Spread onto any element: `<span {...tipProps(text)}>`. Pass
 * `{ labelled: true }` ONLY for elements with no visible text, where the tip
 * doubles as the accessible name. */
export function tipProps(
  text: string | null | undefined,
  opts?: { labelled?: boolean },
): {
  onMouseEnter?: (event: { currentTarget: HTMLElement }) => void;
  onMouseLeave?: () => void;
  onFocus?: (event: { currentTarget: HTMLElement }) => void;
  onBlur?: () => void;
  "data-tip"?: string;
  "aria-label"?: string;
} {
  if (!text) return {};
  return {
    onMouseEnter: (event) => showTip(event.currentTarget, text),
    onMouseLeave: hideTip,
    onFocus: (event) => showTip(event.currentTarget, text),
    onBlur: hideTip,
    "data-tip": text,
    ...(opts?.labelled ? { "aria-label": text } : {}),
  };
}
