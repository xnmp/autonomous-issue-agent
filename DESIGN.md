---
name: Issue Agent Operator Console
description: A dense topology board for supervising autonomous issue lifecycles.
colors:
  primary: "#8c98ff"
  primary-muted: "#7382f4"
  neutral-canvas: "#080b12"
  neutral-panel: "#0f1622"
  neutral-elevated: "#151f2e"
  neutral-ink: "#edf1f7"
  neutral-muted: "#9aa6b6"
  neutral-selection: "#253052"
  success: "#4fca9a"
  success-bright: "#69ddb0"
  attention: "#e9b35a"
  attention-bright: "#f6c773"
  danger: "#f06475"
  danger-bright: "#ff7d8d"
  metadata: "#aa82dc"
  metadata-bright: "#c09aef"
  link: "#6fbee8"
  link-bright: "#8bd0f4"
typography:
  headline:
    fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif"
    fontSize: "32px"
    fontWeight: 720
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  title:
    fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif"
    fontSize: "18px"
    fontWeight: 620
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  body:
    fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif"
    fontSize: "11px"
    fontWeight: 620
    lineHeight: 1.2
    letterSpacing: "0.075em"
  machine:
    fontFamily: "ui-monospace, SFMono-Regular, Cascadia Code, Consolas, Liberation Mono, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
rounded:
  xs: "4px"
  sm: "6px"
  md: "10px"
  lg: "12px"
  pill: "999px"
spacing:
  hairline: "2px"
  xs: "4px"
  sm: "6px"
  md: "8px"
  lg: "12px"
  xl: "16px"
  2xl: "20px"
  3xl: "24px"
  4xl: "32px"
components:
  button-default:
    backgroundColor: "{colors.neutral-elevated}"
    textColor: "{colors.neutral-ink}"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
    height: "30px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.neutral-muted}"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
    height: "30px"
  chip-neutral:
    backgroundColor: "color-mix(in srgb, #9aa6b6 10%, transparent)"
    textColor: "{colors.neutral-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
    height: "22px"
  panel:
    backgroundColor: "{colors.neutral-panel}"
    textColor: "{colors.neutral-ink}"
    rounded: "{rounded.md}"
    padding: "12px"
  input-filter:
    backgroundColor: "{colors.neutral-panel}"
    textColor: "{colors.neutral-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "4px 8px"
  tab-active:
    backgroundColor: "color-mix(in srgb, #8c98ff 14%, transparent)"
    textColor: "{colors.neutral-ink}"
    typography: "{typography.body}"
    rounded: "0"
    padding: "0 12px"
    height: "55px"
---

# Design System: Issue Agent Operator Console

## Overview

**Creative North Star: "The Version-Control Topology Board"**

Issue Agent is a compact, dark operations surface whose visual model is a live execution graph rather than a collection of generic dashboard tiles. Matte graphite and navy layers create a calm working field; one restrained periwinkle route line carries navigation, selection, and lifecycle progress through it.

The interface is deliberately dense, crisp, and operational. Attention, daemon health, active work, ownership, and the next intervention lead; aggregate metrics and historical trends remain quieter and lower in the reading order. Detail is progressively disclosed without removing the surrounding execution context.

**Key Characteristics:**

- Matte graphite/navy tonal layering with fine, low-contrast borders.
- Periwinkle topology lines and active markers used as a route, not decoration.
- Red, amber, green, violet, and cyan reserved for stable semantic roles.
- Compact square controls and dense tables built for repeated scanning.
- Monospace restricted to machine data; human language stays sans-serif.

## Colors

The default palette is a cool graphite terminal world: quiet neutrals carry structure while bright colors act as sparse, redundant state signals. Selectable terminal themes may remap the raw palette, but must preserve these semantic assignments.

### Primary

- **Route Periwinkle:** The principal route, active-tab, focus, and lifecycle-progress signal.
- **Muted Periwinkle:** A lower-intensity companion for informational states and categorical series.

### Secondary

- **Healthy Mint:** Success, healthy daemon state, completed outcomes, and merge-ready actions; the bright step is used for compact foreground text.
- **Watch Amber:** Running work, blockers, warnings, and elevated utilization; the bright step is used where small text needs stronger contrast.

### Tertiary

- **Fault Coral:** Failures, escalations, destructive actions, and urgent attention; the bright step strengthens glyphs and labels.
- **Metadata Violet:** Steward/system provenance, model metadata, and non-primary categorical information.
- **Evidence Cyan:** Links, JSON keys, and evidence-oriented information.

### Neutral

- **Graphite Canvas:** The deepest application and expanded-detail ground.
- **Navy Panel:** The default container and table surface.
- **Raised Navy:** Menus, tooltips, controls, and hoverable elevated surfaces.
- **Cold White:** Primary text and high-contrast glyphs.
- **Steel Muted:** Secondary labels and supporting copy.
- **Selection Navy:** Selected rows, counters, and segmented-control state.

### Named Rules

**The One Route Rule.** Periwinkle connects navigation and lifecycle progress; it is not a general-purpose fill for large surfaces.

**The Semantic Redundancy Rule.** Never communicate operational state with color alone; pair it with a label, glyph, edge, or topology position.

**The Quiet History Rule.** Historical totals stay neutral at the container level; only the meaningful value receives semantic color.

## Typography

**Display Font:** System sans-serif stack
**Body Font:** System sans-serif stack
**Label/Mono Font:** System monospace stack

**Character:** A compact system-sans hierarchy keeps operational prose familiar and fast. Monospace is a precision instrument for IDs, paths, timestamps, costs, token counts, and machine values—not an atmosphere applied to all interface text.

### Hierarchy

- **Headline** (720, 32px, 1.2): Page-level view names with tight tracking.
- **Title** (620, 18px, 1.2): Issue titles and prominent local headings.
- **Body** (400, 13px, 1.45): Default interface copy and dense content.
- **Supporting** (400–520, 12px, 1.45): Table content, buttons, annotations, and secondary facts.
- **Label** (620, 10–11px, 0.075em): Uppercase panel titles, rail groups, and column headings.
- **Machine** (400, 12px, 1.45): IDs, repository names, money, durations, timestamps, models, paths, and serialized evidence.

### Named Rules

**The Machine Data Rule.** Use monospace only when the content is generated, measured, addressed, or serialized by the system.

**The Compact Hierarchy Rule.** Create hierarchy with weight, tone, tracking, and grouping before increasing type size.

## Layout

The application is a two-column shell with a persistent deployment rail and a minmax content board. The rail defaults to 248px, is user-resizable from 220px to 480px, can collapse completely, and caps at 216px below 900px. At 480px it becomes a 56px control strip while inactive closeable tabs disappear.

The main route bar is 56px high (48px on the smallest viewport). Content uses a compact 2–32px spacing scale, a 24px desktop inset, and a 1720px maximum board width without centering away operational space. Repeated collections use auto-fit grids; primary metrics resolve from four columns to two at 1080px, while topology lanes become one-column at 720px. Dense data tables preserve column geometry and scroll horizontally rather than turning rows into cards.

**The Operational Order Rule.** Attention, live health, active lifecycle, and operator actions precede metrics, spend, and trends.

**The Evidence-in-Place Rule.** Expanded evidence stays attached to its row or lifecycle stage and remains readable inside horizontal scrollers.

## Elevation & Depth

Depth is primarily tonal and structural: canvas, sunken, panel, and elevated navy layers are separated by 1px translucent borders. Shadows are reserved for floating, urgent, or overlay surfaces rather than ordinary cards.

### Shadow Vocabulary

- **Compact Float** (`0 2px 8px` with an 82%-canvas translucent shadow): Small floating controls.
- **Working Lift** (`0 10px 28px` with an 82%-canvas translucent shadow): Active-work panels and tooltips.
- **Overlay Lift** (`0 18px 48px` with an 82%-canvas translucent shadow): Menus, modals, quota cards, and toasts.

### Named Rules

**The Tonal-First Rule.** Use surface steps and hairline borders for ordinary hierarchy; a shadow means the element floats, overlays, or demands attention.

## Shapes

The system uses gently clipped geometry: 4px for tight details, 6px for controls and nested containers, 10px for panels, and 12px for larger shells. Pills are limited to chips, counts, meters, and other truly capsule-shaped indicators. Lifecycle nodes and state dots are circular because they are points on a route, not because circles are a general component style.

**The Compact Control Rule.** Controls remain square-leaning and precise; do not turn buttons, inputs, or panels into pills.

## Components

### Buttons

- **Shape:** Compact rectangular control with gently clipped corners (6px) and a 30px minimum height; extra-small actions use 24px.
- **Default:** Raised-navy fill, cold-white text, and a quiet border with 6px × 12px padding.
- **Hover / Focus:** Hover increases surface and border contrast over 150ms; keyboard focus uses a 2px periwinkle ring with a 2px offset.
- **Primary / Danger / Ghost:** Primary is a mint semantic outline, danger uses coral, and ghost removes fill and border until hover. Disabled controls retain their shape at 45% opacity.

### Chips

- **Style:** A 22px capsule using a 10% semantic tint, a 32% semantic border, and matching text.
- **State:** Tone is semantic rather than decorative. Square 6px variants with monospace are reserved for IDs, kinds, models, and other machine labels; running chips may pulse with a static reduced-motion fallback.

### Cards / Containers

- **Corner Style:** Gently rounded panels (10px); nested containers usually use 6px.
- **Background:** Navy panels on the graphite canvas, with sunken layers for supporting groups and raised navy for overlays.
- **Shadow Strategy:** Flat by default; see the tonal-first rule.
- **Border:** One-pixel low-contrast strokes, strengthened for active work or intervention surfaces.
- **Internal Padding:** Usually 12px; dense headers and rows use 8–12px, while issue headers use 16–20px.

### Inputs / Fields

- **Style:** Compact navy field, 6px corners, a one-pixel strong border, and label-sized text.
- **Focus:** Border shifts to route periwinkle and gains a 2px translucent focus halo.
- **Error / Disabled:** Errors use the shared coral semantic treatment; disabled actions remain present but subdued.

### Navigation

The deployment rail uses icon-led 36px rows with stable placement, low-contrast default text, and a periwinkle-tinted selected lane. The 55px route tabs are rectangular and edge-aligned; active tabs gain a soft tint and a 2px bottom route line. Nested tabs use a compact segmented container with the same active underline language.

### Lifecycle Topology

Lifecycle paths are the signature component. Triage, Resolve, Review, CI, and Merge appear as connected circular nodes; inapplicable stages are omitted. Completed and active paths are periwinkle, failure is coral, blocking is amber, and future state is neutral. Review and CI retries appear as return arcs with explicit counts. The active node has a restrained halo pulse over 2s; reduced motion keeps the same static ring, color, label, and position.

### Tables

Tables use sticky sunken headers, uppercase tracked labels, tabular numerals, low-contrast row separators, and 40–44px operating density. Hover and focus identify the whole row. Human attention is carried by one semantic edge stripe plus explicit status text; resizable columns truncate or scroll instead of destroying row rhythm.

### Feedback

Errors, warnings, and success notices use compact full-width banners with a semantic icon tile, message, and local recovery action. Fetch failure degrades one panel and preserves stale data where possible. Empty and loading states always explain what the region is doing, and animation is removed or made static when reduced motion is requested.

## Do's and Don'ts

### Do:

- **Do** lead operational screens with required human action, live health, and active lifecycle state.
- **Do** reuse the semantic tone vocabulary and provide a label, glyph, edge, or position alongside every color signal.
- **Do** keep rows compact, numbers tabular, and long evidence progressively disclosed in place.
- **Do** preserve visible keyboard focus and reduced-motion alternatives.
- **Do** use the 2–32px spacing scale and the 4/6/10/12px corner hierarchy.

### Don't:

- **Don't** replace topology and issue lanes with a generic grid of equal-weight metric cards.
- **Don't** use periwinkle as broad decoration or let historical analytics compete with live work.
- **Don't** apply monospace to prose, navigation labels, or headings simply to make the console feel technical.
- **Don't** use pills for ordinary buttons, inputs, panels, or tabs.
- **Don't** blank the whole screen for a local fetch error or hide the reason an area is empty.
