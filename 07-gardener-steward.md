## Architecture Steward (formerly the Gardener)

> **v3 rewrite.** v1/v2 made this a **stateless** weekly `claude -p` that scanned for smells and
> filed tickets. That can only react to drift *after* it happens — it is an advisor, not an
> owner. The design intent was always for this agent to be the **steward of conceptual
> integrity**: the one organ with continuity across the hundreds of local edits the fixer makes.
> A stateless scanner cannot be that. v3 promotes it to a **persistent, memory-bearing steward**.
> It still never fixes code and never auto-promotes — but it now *owns* the design model, not
> just complaints about it.

### Why it has to be stateful

Every issue is resolved by a fresh fixer with a soda-straw view (the issue + a few files + some
`CLAUDE.md`). Each optimizes locally and exits. Conceptual integrity is a property of the
*whole*, and without a steward the architecture degrades by local optimization and is
garbage-collected after the fact. To actually *own* integrity rather than report on it, the
steward needs three things a scanner lacks:

1. **Memory** — what it decided before, which suggestions were rejected and why, what the
   invariants are. Otherwise it re-suggests the same refactor every cycle and re-learns the
   codebase from scratch each run.
2. **A pre-work position** — it must be consultable *before* a non-trivial issue is worked, not
   only after. Owning design means shaping code before it exists and being able to say "that
   doesn't fit the model." A scanner that only emits tickets can't.
3. **Drift detection it acts on** — an instrument that answers "is the codebase getting worse?",
   which today *nothing in the system can answer*.

### What it owns (durable artifacts)

| Artifact | Where | Purpose |
|----------|-------|---------|
| ADRs (`docs/adr/NNNN-*.md`) | **committed** | The durable "why" behind decisions |
| `INVARIANTS.md` | **committed** | Checkable rules the fixer + reviewer must obey |
| `steward/journal.md` | state dir | What it observed/decided/rejected, with rationale |
| `steward/metrics.jsonl` | state dir | Drift time-series (the fitness instrument) |
| `steward/feedback-clusters.json` | state dir | Mined recurring review feedback + counts |

### Four responsibilities

**1. Encode invariants → attack the shared blind spot.** The fixer and reviewer are separate
agents with separate contexts, so they *decorrelate information* — fresh eyes catch **slips**
(the author would concede it's wrong). But they're the same model weights, so they *share
priors*: a **blind spot** is a false belief both hold, and a fresh context doesn't break it
because nobody is forgetting anything — both simply believe the wrong thing. Example: the
codebase requires version-checked writes to `Order`; if the model doesn't know that, the fixer
writes a bare update and the reviewer reads it and thinks "fine, it updates the row." New
context doesn't help; only a *different prior* does. `INVARIANTS.md` **is that different prior** —
it turns an implicit blind spot into an explicit rule both agents are forced to read, moving the
knowledge *outside* the model where shared weights can't erase it. This is why the steward is
central to **correctness**, not just tidiness: its invariants are the system's main defence
against same-model blind spots. (Complement it cheaply with a *different model* in the reviewer
seat — see [Review Agent](06-review-agent.md) — to shrink the blind-spot intersection on cases
the steward hasn't encoded yet.)

**2. Drift instrumentation as a SIGNAL, not a gate.** A metric you *gate on and optimize* gets
gamed (Goodhart); a metric you *watch to detect drift* does not, because nothing is optimizing
against it. The steward records complexity, coupling, duplication, module size, and **test
mutation score** as a trend it reads — never as an auto-merge gate. The metric source is
pluggable: a bundled counting script, or an external quality service (e.g. the SonarQube MCP
tools, if present). The synthesis: **the stateful steward *is* the fitness function** — a
periodic holistic judgment ("does this still hang together?") is more Goodhart-resistant than any
number, and the metrics are simply its eyes. When a trend worsens past noise, the steward reads
the code the trend points at and either files a suggestion or encodes a new invariant.

**3. Compound learning — make the system better, not just busier.** Each fixer starts fresh from
`AGENT.md`, so if the reviewer corrects the same default on issue after issue, the fixer's
*starting prior* never updates — it re-learns every time. The steward closes this loop: it mines
closed PRs' `REQUEST_CHANGES` reasons, clusters them, and when a pattern recurs ≥ N times,
**promotes** it into a durable default — a line in `AGENT.md`, a new `INVARIANTS.md` entry, or a
lint rule. The recurring correction stops recurring.

**4. Curate docs.** Every agent is told to append "non-obvious learnings" to folder `CLAUDE.md`,
so docs inflate into noise and stop being read. The steward is the curator: it compacts and
dedupes `CLAUDE.md`/ADRs, retires stale guidance, and keeps the design docs high-signal.

### Pre-work consultation

This is the "pre-work position" made concrete — design precedes code for anything non-trivial:

- **Always-on (enforced):** the fixer must read `INVARIANTS.md` and the ADR index before coding
  (in `AGENT.md`). The PR gate checks: if the diff touches a module governed by an ADR, the PR
  body must reference that ADR. Cheap, mechanical, and it forces the encoded prior into every fix.
- **Per-issue triage (steward loop):** for each new `ai-ready` issue the steward attaches a
  **design-context comment** — the relevant ADRs, invariants, and any "here's how this should fit
  the model" guidance — *before* the fixer picks it up. The fixer treats it as authoritative
  design input (still DATA, not instructions).
- **Major changes (human-gated):** issues that imply a new abstraction or boundary change get
  `needs-design`. The steward drafts an **ADR proposal** and stops; a human approves (or edits)
  the ADR, which flips the issue to `ai-ready`. This keeps the highest-leverage human attention
  on *design intent* rather than on unblocking mechanics.

### Runtime shape: a persistent `/loop`, memory in files

Unlike the fix/review watchers (stateless shell loops spawning ephemeral `claude -p`), the
steward is a **single long-lived Claude session** kept alive by a loop — a `while true` shell
loop or the `/loop` Stop-hook technique. The critical design rule:

> **Memory lives in files, not in the session context.** Each iteration *reads* `journal.md`,
> `metrics.jsonl`, `INVARIANTS.md`, and recently merged PRs from disk, does its work, *writes*
> its conclusions back to those files, and ends the turn. Context is not the memory — the files
> are. This keeps each iteration cheap and bounded, and lets the session be killed and restarted
> with zero loss (it re-reads its journal on the next turn). It is the opposite of `--resume`
> accumulating an ever-growing transcript.

The same prompt is fed every iteration; continuity comes from the durable artifacts, not from
conversation history. Cadence is event-light and slow — the steward is not on the issue
critical path, so it can wake on an interval (hourly/daily) or after every K merges.

```bash
#!/usr/bin/env bash
# steward.sh — drives the persistent steward session. Run under launchd/systemd.
# Memory is in $SDIR + committed docs; the session context stays small by design.
set -uo pipefail
REPO="${REPO:?}"; BASE_BRANCH="${BASE_BRANCH:-dev}"
STEWARD_BOT="${STEWARD_BOT:?set STEWARD_BOT=steward-bot-login}"   # 3rd identity (read+comment)
SDIR="${STEWARD_STATE_DIR:-$HOME/.local/state/steward-agent}"
SLEEP="${STEWARD_SLEEP_SECONDS:-3600}"
mkdir -p "$SDIR"
: > "$SDIR/journal.md"  2>/dev/null || true   # created on first run only (|| true if exists)

STEWARD_SCHEMA='{"type":"object","properties":{
  "observations":{"type":"string"},
  "suggestions":{"type":"array","items":{"type":"object","properties":{
     "tier":{"type":"string","enum":["trivial","minor","major"]},
     "title":{"type":"string"},"body":{"type":"string"},
     "files":{"type":"array","items":{"type":"string"}}},"required":["tier","title","body"]}},
  "invariant_promotions":{"type":"array","items":{"type":"string"}},
  "adr_proposals":{"type":"array","items":{"type":"object","properties":{
     "issue":{"type":"integer"},"title":{"type":"string"},"body":{"type":"string"}},
     "required":["title","body"]}},
  "design_context":{"type":"array","items":{"type":"object","properties":{
     "issue":{"type":"integer"},"comment":{"type":"string"}},"required":["issue","comment"]}},
  "journal_append":{"type":"string"}
},"required":["observations","journal_append"]}'

STEWARD_PROMPT='You are the Architecture Steward — the single owner of conceptual integrity for
this codebase. You never edit code and never promote issues to ai-ready. Your memory is on disk;
read it every iteration.

Each turn:
1. Read docs/adr/*, INVARIANTS.md, ARCHITECTURE.md, and your journal.md (prior decisions +
   what you have REJECTED and why — do not re-raise rejected ideas).
2. Read PRs merged to '"$BASE_BRANCH"' since your last entry and the latest metrics line.
   Judge holistically: does the system still hang together? Did a merge degrade the model?
3. For each new ai-ready issue with no design context yet, attach a design_context comment
   (relevant ADRs/invariants). If an issue implies a new abstraction/boundary change, instead
   emit an adr_proposal and it will be labelled needs-design (human approves the ADR).
4. Mine recurring REQUEST_CHANGES reasons from merged PRs. If a correction recurs, emit an
   invariant_promotion (a one-line rule to add to INVARIANTS.md / AGENT.md).
5. Curate: note any CLAUDE.md/ADR that has become stale or noisy (as a suggestion).
6. Only emit suggestions that are NOT already open and NOT in your rejected list.
Return structured output and ALWAYS append a dated journal entry summarising what you did and
decided, so your future self has continuity.'

drive_once() {
  # Compact, file-derived context — NOT the whole repo, NOT the whole history.
  local since merged metrics
  since=$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)
  merged=$(gh pr list --repo "$REPO" --base "$BASE_BRANCH" --state merged \
    --search "merged:>=$since" --json number,title,reviews \
    --jq '[.[] | {number,title, change_requests:[.reviews[]|select(.state=="CHANGES_REQUESTED")|.body]}]' 2>/dev/null || echo '[]')
  metrics=$(tail -1 "$SDIR/metrics.jsonl" 2>/dev/null || echo '{}')
  local journal_tail; journal_tail=$(tail -120 "$SDIR/journal.md" 2>/dev/null || echo "")

  local result rc
  result=$(claude -p \
    --permission-mode bypassPermissions \
    --tools 'Read,Bash(git *),Bash(gh *),Bash(rg *),Bash(cat *),Bash(ls *)' \
    --disallowed-tools 'Edit,Write' \
    --max-budget-usd "${STEWARD_BUDGET_USD:-3.00}" \
    --output-format json --json-schema "$STEWARD_SCHEMA" \
    --append-system-prompt "$STEWARD_PROMPT" \
    "Recent merges (with change-requests): $merged
Latest metrics: $metrics
Recent journal (your memory):
$journal_tail"); rc=$?
  [ "$rc" -eq 0 ] || { echo "steward: claude rc=$rc"; return; }

  local out; out=$(jq -c '.structured_output' <<<"$result" 2>/dev/null || echo '{}')

  # Persist memory FIRST (so a crash mid-action never loses the reasoning).
  jq -r '.journal_append // ""' <<<"$out" | sed "s/^/$(date -u +%F)  /" >> "$SDIR/journal.md"

  # Suggestions → issues (never auto-promoted; human moves to ai-ready).
  jq -c '.suggestions[]?' <<<"$out" 2>/dev/null | while IFS= read -r s; do
    local t b f tier; t=$(jq -r .title <<<"$s"); b=$(jq -r .body <<<"$s")
    tier=$(jq -r .tier <<<"$s"); f=$(jq -r '(.files//[])|join(", ")' <<<"$s")
    gh issue list --repo "$REPO" --search "$t in:title" --state open --json number \
      --jq 'length' 2>/dev/null | grep -q '^0$' || continue
    gh issue create --repo "$REPO" --title "$t" --label "ai-suggestion,$tier" \
      --body "$b

**Affected files:** $f
**Tier:** $tier

---
_Posted by the Architecture Steward. A human must move this to \`ai-ready\` first._" 2>/dev/null || true
  done

  # Design context on ai-ready issues (DATA for the fixer, written as a comment).
  jq -c '.design_context[]?' <<<"$out" 2>/dev/null | while IFS= read -r d; do
    local n c; n=$(jq -r .issue <<<"$d"); c=$(jq -r .comment <<<"$d")
    gh issue comment "$n" --repo "$REPO" --body "**Design context (steward):**

$c" 2>/dev/null || true
  done

  # ADR proposals → needs-design (human-gated). Steward drafts, human approves the ADR.
  jq -c '.adr_proposals[]?' <<<"$out" 2>/dev/null | while IFS= read -r a; do
    local n t b; n=$(jq -r '.issue // empty' <<<"$a"); t=$(jq -r .title <<<"$a"); b=$(jq -r .body <<<"$a")
    [ -n "$n" ] && gh issue edit "$n" --repo "$REPO" --add-label needs-design 2>/dev/null || true
    gh issue create --repo "$REPO" --title "ADR proposal: $t" --label "needs-design,ai-suggestion" \
      --body "$b

---
_Draft ADR by the steward. A human approves/edits, commits it to docs/adr/, then promotes the work._" 2>/dev/null || true
  done

  # Invariant promotions are PROPOSED, not silently committed (they change fixer/reviewer behaviour).
  jq -r '.invariant_promotions[]?' <<<"$out" 2>/dev/null | while IFS= read -r inv; do
    [ -n "$inv" ] || continue
    gh issue create --repo "$REPO" --title "Promote invariant: ${inv:0:60}" \
      --label "ai-suggestion,minor,invariant" \
      --body "The steward observed this correction recurring. Proposed addition to INVARIANTS.md / AGENT.md:

> $inv

A human approves before it becomes a fixer/reviewer default." 2>/dev/null || true
  done
}

# Optional: refresh the drift instrument before driving (pluggable source).
collect_metrics() {
  [ -n "${METRICS_CMD:-}" ] || return 0
  local line; line=$(eval "$METRICS_CMD" 2>/dev/null || echo "")
  [ -n "$line" ] && echo "$line" >> "$SDIR/metrics.jsonl"
}

echo "steward: $REPO (identity=$STEWARD_BOT, wake every ${SLEEP}s)"
while true; do
  collect_metrics
  drive_once || echo "steward pass failed (continuing)"
  sleep "$SLEEP"
done
```

### Identity, tools, and what stays human

- **Third identity `steward-bot`** (read + comment only): keeps its issue comments and
  suggestions distinguishable from `fix-bot`/`review-bot` and out of the fixer's "did someone
  reply?" logic. Restricted tools (`--disallowed-tools Edit,Write`) make "never edits code"
  mechanical, not honour-system — same posture as the review agent.
- **The steward proposes; humans dispose** for anything that changes behaviour: ADRs and
  invariant promotions are filed as `needs-design`/`invariant` issues a human approves. The
  steward writes freely only to its *own* journal/metrics (state dir) and to issue *comments*.
  It never commits to `INVARIANTS.md`/`docs/adr/` itself — those are the durable priors that
  steer both other agents, so a human is the gate.
- **Highest-leverage human attention** lands on design intent (approving ADRs, accepting
  invariants), not on unblocking mechanics. This is the deliberate inversion of v1, where humans
  were spent on the lowest-leverage work.

### Harness self-reporting

Any agent can file an issue against `HARNESS_REPO` when a hook blocks a valid action or a
prompt confuses it, then work around it. This was sound in v1 and is unchanged, except the
example bug ("hook can't parse compound commands") is now *fixed by design* — the tree-sitter
governor handles compound/nested commands, so that class of harness bug shouldn't recur.

---

← Prev: [Review Agent](06-review-agent.md) · [Index](README.md) · Next: [Master Agent](08-master-agent.md)  →
