## Hook Layer: tree-sitter Bash Governor

### Why tree-sitter (and where its value actually is)

A `match`-on-substring hook is wrong twice: it fires for unrelated commands that merely
*contain* `git commit` in an argument, and it misses governed commands hidden in compound
structures. Both were demonstrated against the real parser:

| Command | substring match | tree-sitter |
|---------|-----------------|-------------|
| `git add -A && git commit -m "x"` | sees the whole string | extracts the `git commit` node |
| `echo "git commit later"` | **false positive** (blocks an echo) | no command node named `git commit` |
| `foo=$(git commit -m "sneaky")` | depends on anchoring | extracts the nested `git commit` |
| `(cd sub && git commit -m x)` | brittle | extracts it from the subshell |

But routing is the *lesser* benefit. The decisive reason to parse is **bypass detection**.
A string matcher cannot reliably tell `git commit -m x` from `git -c core.hooksPath=/dev/null commit -m x`
or `git commit --no-verify`, especially when chained. The governor's real job is to make the
gate **non-circumventable**: it refuses any governed git/gh invocation that disables hooks,
redirects `core.hooksPath`, or uses `--no-verify`, no matter how it's nested. Lint/typecheck
*routing* could be done by a native git hook (cleaner inputs); detecting an agent trying to
slip past the gate cannot.

### bash_governor.py

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter>=0.22", "tree-sitter-bash>=0.23"]
# ///
"""
PreToolUse(Bash) governor. Reads the hook JSON on stdin, parses .tool_input.command
into an AST, finds every *command* node (including inside &&/||/;, pipelines, subshells,
command substitution, and heredoc-redirected commands), and routes governed commands to
the appropriate checker. Exit 0 = allow, exit 2 = block (stderr shown to the agent).

Determinism note: this never executes the command. It only inspects structure.
"""
import json, os, subprocess, sys
from tree_sitter import Language, Parser
import tree_sitter_bash as tsb

PARSER = Parser(Language(tsb.language()))
HOOK_DIR = os.path.dirname(os.path.abspath(__file__))

def block(msg: str):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(2)

def text(n) -> str:
    return n.text.decode("utf-8", "replace")

def arg_value(n) -> str:
    """Logical string value of an argument node (unwraps quotes / concatenation)."""
    if n.type == "string":
        return "".join(text(c) for c in n.children if c.type == "string_content")
    if n.type == "raw_string":
        return text(n).strip("'")
    if n.type == "concatenation":
        return "".join(arg_value(c) for c in n.children)
    return text(n)

def extract_commands(src: str):
    """Return a list of token-lists, one per command node found anywhere in the tree."""
    tree = PARSER.parse(bytes(src, "utf-8"))
    cmds = []
    def visit(n):
        if n.type == "command":
            words = []
            for c in n.children:
                if c.type == "command_name":
                    words.append(arg_value(c.children[0]) if c.children else text(c))
                elif c.type in ("word", "string", "raw_string", "concatenation", "number"):
                    words.append(arg_value(c))
                # redirects / file_redirect deliberately ignored for arg purposes
            if words:
                cmds.append(words)
        for c in n.children:
            visit(c)
    visit(tree.root_node)
    return cmds

# ── bypass detection (runs on the FULL command tokens, not the trimmed rest) ─
def assert_no_bypass(full_words, kind):
    joined = " ".join(full_words)
    if "--no-verify" in full_words or (kind == "git-commit" and "-n" in full_words):
        # `git commit -n` is shorthand for --no-verify; block it on commit.
        block("Blocked: --no-verify / -n disables commit hooks. The gate is mandatory.")
    for i, w in enumerate(full_words):
        if w == "-c" and i + 1 < len(full_words) and "hookspath" in full_words[i + 1].lower():
            block("Blocked: overriding core.hooksPath bypasses the gate.")
    if "core.hooksPath" in joined or "HUSKY=0" in joined:
        block("Blocked: attempt to disable the hook path detected.")

def classify(words):
    """Return (kind, full_words, checker_args) or None. full_words is the whole command
    (for bypass detection); checker_args is the subcommand-onward slice the checker wants."""
    if not words:
        return None
    if words[0] == "git" and len(words) > 1:
        # Skip global flags incl. `-c k=v` to find the real subcommand.
        i = 1
        while i < len(words):
            if words[i] == "-c" and i + 1 < len(words):
                i += 2; continue
            if words[i].startswith("-"):
                i += 1; continue
            break
        if i >= len(words):
            return None
        sub, rest = words[i], words[i:]
        if sub == "commit":
            return ("git-commit", words, rest)
        if (sub == "checkout" and "-b" in rest) or (sub == "switch" and "-c" in rest):
            return ("git-branch", words, rest)
    if words[0] == "gh" and len(words) >= 3 and words[1] == "pr" and words[2] == "create":
        return ("gh-pr-create", words, words)
    return None

CHECKERS = {
    "git-branch":   "checks/check-branch-name.sh",
    "git-commit":   "checks/check-commit.sh",
    "gh-pr-create": "checks/check-pr.sh",
}

def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        sys.exit(0)  # not our concern; let other hooks/permissions handle it
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        sys.exit(0)

    governed = [c for c in (classify(cmd) for cmd in extract_commands(command)) if c]
    if not governed:
        sys.exit(0)

    for kind, full_words, checker_args in governed:
        assert_no_bypass(full_words, kind)
        script = os.path.join(HOOK_DIR, CHECKERS[kind])
        # Pass the parsed tokens as argv so the checker never re-parses the string.
        res = subprocess.run([script, *checker_args], capture_output=True, text=True)
        if res.returncode != 0:
            block(res.stdout + res.stderr)
    sys.exit(0)

if __name__ == "__main__":
    main()
```

Key properties:

- **No re-parsing in shell.** The governor hands each checker the already-tokenized argv, so
  `check-commit.sh "$@"` sees clean `git commit -m green: foo` tokens — no fragile
  re-splitting, no `grep -P`.
- **Every governed command in a compound is checked.** `a && git commit … && b` runs the
  commit checker on the commit node only.
- **Fail-open on parse errors, fail-closed on bypass.** Malformed input the parser can't
  understand is allowed (other layers handle it); a *recognized* bypass attempt is always
  blocked.

### checks/check-branch-name.sh

Receives `switch -c <name>` or `checkout -b <name>` as argv. No re-parsing.

```bash
#!/usr/bin/env bash
set -uo pipefail
# argv = subcommand onward, e.g.  switch -c fix/12-bug
name=""
for ((i=1; i<=$#; i++)); do
  case "${!i}" in
    -b|-c) j=$((i+1)); name="${!j:-}"; break ;;
  esac
done
[ -n "$name" ] || exit 0   # not a branch-creating form; allow

if [[ ! "$name" =~ ^(feat|fix)/[0-9]+- ]]; then
  echo "Branch name '$name' must match feat/<issue>-… or fix/<issue>-… (e.g. fix/42-null-token)."
  exit 1
fi
exit 0
```

### checks/check-commit.sh

Lint + typecheck always; tests are **not** run here (they belong in CI — a blocking commit
hook that runs the suite will time out and serialize the whole agent). The prefix decides
whether the *PR gate* later expects tests, not whether we run them now. Reads the commit
message from the tokenized argv (`-m`, `-F`, or heredoc `-F-`), so there is no `grep -P`.

```bash
#!/usr/bin/env bash
set -uo pipefail
# argv = commit onward, e.g.  commit -m "green: implement x"
msg=""
for ((i=1; i<=$#; i++)); do
  case "${!i}" in
    -m|--message) j=$((i+1)); msg="${!j:-}"; break ;;
    -m*)          msg="${!i#-m}"; break ;;
    --message=*)  msg="${!i#--message=}"; break ;;
  esac
done

VALID_PREFIX='^(plan|red|green|evidence|docs|chore|refactor): '
# Empty msg (e.g. -F file / -F- heredoc) → can't validate prefix here; allow,
# the PR gate re-checks the branch's commit log via git.
if [ -n "$msg" ] && ! [[ "$msg" =~ $VALID_PREFIX ]]; then
  echo "Commit message must start with one of: plan: red: green: evidence: docs: chore: refactor:"
  echo "Got: $msg"
  exit 1
fi

# Fast gates only. Project supplies these via env (set in AGENT.md / repo CLAUDE.md).
if [ -n "${LINT_CMD:-}" ];      then eval "$LINT_CMD"      || { echo "Lint failed.";      exit 1; }; fi
if [ -n "${TYPECHECK_CMD:-}" ]; then eval "$TYPECHECK_CMD" || { echo "Typecheck failed."; exit 1; }; fi
exit 0
```

> On the honor-system critique (v1 3.2): the prefix is still agent-chosen, but two things make
> it non-gameable in practice. (1) The PR gate (below) independently inspects the **git log**
> and the **diff** — a `green:` change mislabeled `red:` to skip CI still has to pass CI before
> merge, and a branch with zero test changes fails the diff check regardless of prefixes.
> (2) The review agent runs tests itself when `--reviewer-runs-tests` is on. The hook is a fast
> local nudge, not the security boundary; CI + review are.

### checks/check-pr.sh

The full gate, fired on `gh pr create`. It re-derives truth from git/files rather than
trusting commit prefixes, so mislabeled commits can't sneak through.

```bash
#!/usr/bin/env bash
set -uo pipefail
# argv = the gh pr create invocation tokens (title/body available, but we mostly use git)
fail() { echo "PR blocked: $1"; exit 1; }

branch=$(git rev-parse --abbrev-ref HEAD)
base="${BASE_BRANCH:-dev}"
range="origin/$base...HEAD"

# 1. Tests pass once, locally, before we open the PR (CI will re-run authoritative).
if [ -n "${TEST_CMD:-}" ]; then eval "$TEST_CMD" || fail "test suite failed"; fi

# 2. PR body carries acceptance criteria (read from the --body/--body-file token if present,
#    else from the PR template the agent filled in).
body=""
for ((i=1; i<=$#; i++)); do
  case "${!i}" in
    --body)      j=$((i+1)); body="${!j:-}" ;;
    --body-file|-F) j=$((i+1)); body="$(cat "${!j:-/dev/null}" 2>/dev/null)" ;;
  esac
done
echo "$body" | grep -qF '## Acceptance Criteria' || fail "PR body missing '## Acceptance Criteria'"

# 3. A Test Plan comment exists on the linked issue (checked by the orchestrator before
#    coding; here we just confirm the marker file the agent wrote).
[ -f .harness/test-plan-posted ] || fail "no Test Plan posted on the issue yet"

# 4. Branch type ⇒ required new test file in the diff (derive from git, not prefixes).
changed=$(git diff --name-only "$range")
case "$branch" in
  feat/*) echo "$changed" | grep -qE '(e2e|integration).*\.(spec|test)\.' \
            || fail "feat/ branch must add an e2e/integration test" ;;
  fix/*)  echo "$changed" | grep -qE '(regression|test).*\.(spec|test)\.' \
            || fail "fix/ branch must add a regression test" ;;
esac

# 5. evidence/ is NOT committed (it's gitignored; posted to the PR as a comment instead).
if echo "$changed" | grep -q '^evidence/'; then
  fail "evidence/ must not be committed — it is posted as a PR comment (see cleanup notes)"
fi

# 6. Adversarial pre-PR review verdict must exist and pass. The fix agent spawns a subagent
#    (fresh context) before this point and writes the verdict file; see Fix Agent Spec. The hook
#    only checks the signed file. (Unfakeable variant: run the adversarial claude -p HERE instead.)
verdict=$(cat .harness/adversarial-verdict 2>/dev/null || echo "")
echo "$verdict" | grep -q '^PASS' || fail "adversarial pre-PR review did not return PASS"

# 7. ADR reference: if the diff touches a module that an ADR governs, the PR body must cite it.
#    Convention: an ADR lists the paths it governs in a 'Governs:' line. Forces the encoded
#    prior (steward's design decision) to be acknowledged where it applies.
if [ -d docs/adr ]; then
  for adr in docs/adr/[0-9]*.md; do
    [ -e "$adr" ] || continue
    governs=$(grep -i '^Governs:' "$adr" | sed 's/^[Gg]overns://') || true
    [ -n "$governs" ] || continue
    for path in $governs; do
      if echo "$changed" | grep -q "$path"; then
        id=$(basename "$adr" | grep -oE '^[0-9]+')
        echo "$body" | grep -qiE "adr[ -]?$id|$(basename "$adr")" \
          || fail "diff touches $path governed by ADR $id — reference it in the PR body"
      fi
    done
  done
fi
exit 0
```

---

← Prev: [Fix Agent Plugin Structure](01-plugin-structure.md) · [Index](../../README.md) · Next: [Fix Agent Specification](03-fix-agent-spec.md)  →
