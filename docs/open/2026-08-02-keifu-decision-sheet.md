# keifu backlog — decisions only you can make

The steward triaged all 18 open issues. 7 are ready to build and will be
admitted automatically. These 9 are blocked on answers only you (as issue
author / maintainer) can give. Reply on the GitHub issues once the proposals
post; the steward re-triages on any issue update. My recommended defaults are
marked (R) — reply "go with the defaults" on an issue and I'll relay it.

## Pure repro-info requests (no design decision, just evidence)
- **#124** graph-line artifacts: marked screenshot/recording + steps (graph
  style, terminal size, selection/scrolling). Note: the steward's "possible
  recurrence of #119 / PR #150" flag on this issue is a false positive
  (unrelated) — I'll remove that label after it posts.
- **#125** S-shaped artifact: one concrete occurrence (screenshot, repo shape,
  terminal size, preceding actions).
- **#136** macOS mirrored-display redraw: terminal app, display mode, exact
  steps, what "messed up" looks like. Also scope: mirror-connect only, or
  disconnect/mode-switch too? (R: include disconnect + mode-switch — same
  redraw path.) Note: this box is Linux; the harness cannot verify a Mac fix.

## Product decisions
- **#34** background op timeouts: how long before fetch/pull/push count as
  stuck (R: 60s no-progress), cancel UX (R: keybinding on the op in a jobs
  view + auto-cancel at timeout), messaging (R: spinner "cancelling…", then
  one-line toast). Maintainer call: cooperative vs hard cancellation of a
  wedged libgit2 op (R: cooperative + transport timeout; hard kill only via
  worker isolation — bigger refactor, separate issue).
- **#126** graph filters: show only matches or keep ancestry context
  (R: matches + dimmed connecting ancestry), multi-filter AND/OR (R: AND),
  matching semantics (R: case-insensitive substring for message/author;
  path = directory-prefix + glob).
- **#130** Caps Lock toast: every keypress vs once per Caps-Lock session
  (R: once per session). Maintainer call: keyboard-enhancement-mode
  best-effort only, or platform-specific fallback (R: best-effort only).
- **#132** "Open PR" action: immediate create vs in-app form vs browser
  (R: open GitHub's compare/create page in browser — smallest correct slice).
- **#142** palette expansion: checkout picker local-only vs +remote
  (R: local + remote with auto-tracking local branch), settings subset
  (R: the toggles already in the Help/status-bar family, not all settings).
- **#157** remappable keybindings: conflict handling, config schema,
  migration. (R: TOML keymap section, last-wins with startup warning on
  conflicts, no migration needed for first release.) This one is big —
  consider splitting schema vs UI.
