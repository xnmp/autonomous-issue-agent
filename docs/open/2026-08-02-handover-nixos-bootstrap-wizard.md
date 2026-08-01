# Handover: fresh NixOS machine bootstrapped, setup wizard landed, daemons DOWN

Date: 2026-08-02. Predecessor: `2026-08-01-handover-keifu-batch-complete.md`.
The old machine's environment is GONE (state dirs, control store, config,
launcher) — only the git remotes survived. This session rebuilt everything on
a fresh NixOS box and turned the pain into a feature.

## TL;DR for the next session

Start the harness — that is the one deliberate leftover (operator said start
in a new session). Everything is staged: `make point-at REPO=keifu`
foreground one tick was already canaried green; config is doctor-clean;
re-triage of ~18 unlabeled keifu issues is operator-approved (≤$0.75/leg,
STEWARD_PROMOTE=0 so proposals only). Add a log monitor on
`~/.local/state/issue-agent/logs/*-keifu.log` for
ERROR/escalation/`target_stalled`/`review_pin_deferred`/`branch_refresh`
(all ~0 on a healthy run) and after one clean tick consider
`issue-agent install-service`.

## State

- **This checkout**: branch `dev`, HEAD `1f3a6e8`, clean, `make test` green
  (1400+ tests, pyrefly 0). `dev` is NOT pushed (contains the whole prior
  session batch too — pushing may need the operator if the classifier
  blocks). Fresh control store — no runs, no leases, no legacy commands.
- **Machine quirks**: see memory `nixos-deployment-machine` — no /bin/bash
  (fixed in `ac855b5`, PATH-resolve any new bash/env spawn), no sqlite3 CLI,
  codex at `~/.local/bin/codex` (chatgpt auth → synthetic rate card).
- **Config**: `config/shared.env` (0600) + `config/repos/keifu.env`
  reconstructed and doctor-clean; rationale in memory
  `keifu-deployment-config` (xnmp/xnmp-bot identities, BASE_BRANCH=chong-dev
  from merged-PR bases, cargo test/101, lint mirrors keifu CI,
  MAX_BUDGET_USD=20). Reviewer PAT source: `~/Documents/Inbox/bot_pat.txt`.

## Session commits (local dev)

- `ac855b5` — NixOS portability: PATH-resolved bash/env in Makefile,
  env_file.py (executable= keeps argv[0] contract), cli.py `_BASH`.
- `1f3a6e8` — validating setup wizard + first-run discovery. Two
  adversarial rounds (initial FAIL → delta PASS) + a final isolation round.
  Trust decisions to preserve when touching this code: non-TTY first runs
  NEVER adopt an inferred BASE_BRANCH (point-at's merge-target hard stop
  must fire) and never write discovered LINT/TYPECHECK live (commented
  suggestions only); identity-rejected reviewer tokens are discarded, never
  persisted; discovered check commands trial-run only in a disposable
  worktree with a mutating-formatter deny-list.

## Open items (unchanged priority from predecessor)

1. Start the deployment; watch rounds/PR + the three warning rates.
2. #153 runner-minted visual receipts (design constraint recorded in
   predecessor handover).
3. Bake TEST_CMD into the generated codex governor hook
   (`codex_hook.py:_gov_script`).
4. #124 reviewer-granted test-edit exemptions.
5. ADR 0012 deferred items (triggers named in predecessor).

Review residuals accepted knowingly (reviewer PASS notes): TTY Enter can
still adopt the default branch as merge target (now an explicit keystroke
with the auto-merge consequence in the prompt); `--no-verify-test-cmd` also
skips lint/typecheck trials (help says so, flag name doesn't); `--test-cmd`
alone still triggers one Haiku discovery call for lint/typecheck.
