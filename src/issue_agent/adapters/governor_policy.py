"""Shared Bash command governor policy.

This module owns the pure command parsing/classification and bypass-detection
policy used by both the shell hook adapter and the in-process SDK/Codex adapter.
It performs no checker execution and no filesystem I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Language, Parser
import tree_sitter_bash as tsb

PARSER = Parser(Language(tsb.language()))

GUARDED_GIT_SUBCMDS = ("config", "push")

# These argument node types carry a literal string value that an agent could
# hide a flag in. ansi_c_string ($'...') was a real bypass — it MUST be unwrapped.
_VALUE_NODE_TYPES = (
    "word", "string", "raw_string", "ansi_c_string", "concatenation", "number",
)


# ── AST helpers ──────────────────────────────────────────────────────────────
def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _deescape(s: str) -> str:
    """Remove lone (shell) backslash escapes: bash turns `--no\\-verify` into
    `--no-verify` at runtime, so the governor must compare against the
    de-escaped form. A doubled backslash collapses to a single one."""
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(s[i + 1])  # drop the backslash, keep the escaped char
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def arg_value(node) -> str:
    """Logical string value of an argument node (unwraps quotes/concatenation/
    ANSI-C quoting and shell backslash escapes).

    `"git commit"`, `'git commit'`, `git" "commit`, `$'git commit'`, and
    `--no\\-verify` all collapse to their runtime string so bypass detection
    cannot be hidden behind quoting or escaping.
    """
    t = node.type
    if t == "string":
        # double-quoted: keep only literal string_content chunks
        parts = [_text(c) for c in node.children if c.type == "string_content"]
        return "".join(parts) if parts else _text(node).strip('"')
    if t == "raw_string":
        return _text(node).strip("'")
    if t == "ansi_c_string":
        # $'...' — strip the $'' wrapper. (We don't interpret \n etc.; the goal
        # is to recover flag spellings like $'--no-verify', not escape sequences.)
        return _text(node)[2:-1] if _text(node).startswith("$'") else _text(node)
    if t in ("concatenation", "word") and node.children:
        return "".join(arg_value(c) for c in node.children)
    # Leaf word/number/other: de-escape shell backslashes.
    return _deescape(_text(node))


def _command_parts(cmd_node) -> tuple[list[str], list[str]]:
    """Return (words, env) for a `command` node.

    ``words`` is the name+args token list (quotes unwrapped) used for
    classification. ``env`` is the list of variable-assignment prefixes
    (e.g. ``HUSKY=0``) which classification must ignore but bypass detection
    must see.
    """
    words: list[str] = []
    env: list[str] = []
    for c in cmd_node.children:
        if c.type == "variable_assignment":
            env.append(_text(c))
        elif c.type == "command_name":
            inner = c.children[0] if c.children else c
            words.append(arg_value(inner))
        elif c.type in _VALUE_NODE_TYPES:
            words.append(arg_value(c))
        # redirects / file_redirect / heredoc bodies intentionally ignored
    return words, env


def extract_commands(src: str) -> list[list[str]]:
    """Return one token-list per `command` node found anywhere in the tree."""
    return [words for words, _env in extract_commands_with_env(src)]


def extract_commands_with_env(src: str) -> list[tuple[list[str], list[str]]]:
    """Like ``extract_commands`` but also returns each command's env prefixes."""
    tree = PARSER.parse(bytes(src, "utf-8"))
    out: list[tuple[list[str], list[str]]] = []

    def visit(node):
        if node.type == "command":
            words, env = _command_parts(node)
            if words:
                out.append((words, env))
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return out


# ── classification & bypass detection ────────────────────────────────────────
@dataclass(frozen=True)
class Governed:
    kind: str               # one of CHECKERS keys, "git-config", or "git-push"
    full_words: list[str]   # whole command (for bypass detection)
    checker_args: list[str]  # subcommand-onward slice the checker wants
    extra_env: tuple[str, ...] = ()  # env KEY=VAL recovered from an `env` wrapper


def _basename(cmd: str) -> str:
    """Trailing path component of a command name, so `/usr/bin/git` → `git`.
    Command-substitution forms like `$(which git)` are returned verbatim (they
    cannot be statically resolved); the caller treats those conservatively."""
    return cmd.rsplit("/", 1)[-1]


def _git_subcommand(words: list[str]) -> tuple[str, list[str]] | None:
    """Skip git global flags (incl. `-c k=v`) and return (subcommand, rest)."""
    i = 1
    while i < len(words):
        w = words[i]
        if w == "-c" and i + 1 < len(words):
            i += 2
            continue
        if w in ("-C", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(words):
            i += 2
            continue
        if w.startswith("-"):
            i += 1
            continue
        break
    if i >= len(words):
        return None
    return words[i], words[i:]


# Transparent command wrappers: these run the command that follows them, so the
# governor must "see through" them to the real command (and collect any env they
# set). `env` carries KEY=VAL pairs; exec/command/builtin/nice/nohup/setsid/stdbuf
# just prefix a command. Nesting (env env …, exec env …) is unwrapped to a fixed
# point.
_PASSTHROUGH_WRAPPERS = ("exec", "command", "builtin", "nohup", "setsid", "stdbuf", "nice", "time")


def _strip_env_wrapper(words: list[str]) -> tuple[list[str], tuple[str, ...]]:
    """Unwrap any chain of `env [flags] [KEY=VAL...]` and passthrough wrappers
    (exec/command/builtin/…) to reach the real command. Returns
    (real-cmd-onward, all recovered KEY=VAL env pairs). Iterates to a fixed point
    so `env env HUSKY=0 git …` and `exec env HUSKY=0 git …` are both unwrapped."""
    env_pairs: list[str] = []
    cur = words
    # Bound the loop defensively; real commands never nest this deep.
    for _ in range(16):
        if not cur:
            break
        base = _basename(cur[0])
        if base == "env":
            i = 1
            while i < len(cur):
                w = cur[i]
                if w in ("-i", "--ignore-environment", "-0", "--null"):
                    i += 1
                    continue
                if w in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string") and i + 1 < len(cur):
                    i += 2
                    continue
                if "=" in w and not w.startswith("-"):
                    env_pairs.append(w)
                    i += 1
                    continue
                break  # first bare word is the real command (or next wrapper)
            cur = cur[i:]
            continue
        if base in _PASSTHROUGH_WRAPPERS and len(cur) > 1:
            # skip the wrapper word and any leading -flags it takes
            j = 1
            while j < len(cur) and cur[j].startswith("-"):
                j += 1
            cur = cur[j:]
            continue
        break  # cur[0] is the real command
    return cur, tuple(env_pairs)


def classify(words: list[str]) -> Governed | None:
    """Map a command's tokens to a governed kind, or None if ungoverned."""
    if not words:
        return None

    # Unwrap `env [KEY=VAL...] git ...` so the inner command is governed and the
    # env vars reach bypass detection (env HUSKY=0 git commit was a real bypass).
    inner, extra_env = _strip_env_wrapper(words)
    if not inner:
        return None

    name0 = _basename(inner[0])

    if name0 == "git" and len(inner) > 1:
        sub = _git_subcommand(inner)
        if sub is None:
            return None
        name, rest = sub
        if name == "commit":
            return Governed("git-commit", words, rest, extra_env)
        if (name == "checkout" and ("-b" in rest or "-B" in rest)) or \
           (name == "switch" and ("-c" in rest or "-C" in rest)):
            return Governed("git-branch", words, rest, extra_env)
        if name == "config":
            # Only a hooksPath write is dangerous; let other git config through.
            if any("hookspath" in w.lower() for w in rest):
                return Governed("git-config", words, rest, extra_env)
            return None
        if name == "push":
            return Governed("git-push", words, rest, extra_env)
        return None

    if (
        name0 == "gh"
        and len(inner) >= 3
        and inner[1] == "pr"
        and inner[2] == "create"
    ):
        return Governed("gh-pr-create", words, inner, extra_env)
    return None


# Env vars (other than HUSKY=1) that disable a hook manager. Value semantics
# vary by tool, so we treat any "off"-ish value as a disable signal.
_HOOK_DISABLE_ENV = {
    "HUSKY": {"0", "false", "no", "off", "00"},      # husky v5: HUSKY=0 disables
    "HUSKY_SKIP_HOOKS": None,                          # any value (legacy v4)
    "HUSKY_GIT_PARAMS": None,
    "LEFTHOOK": {"0", "false", "no", "off"},
    "OVERCOMMIT_DISABLE": None,                        # any truthy value disables
    "PRE_COMMIT_ALLOW_NO_CONFIG": None,
    "SKIP": None,                                      # pre-commit SKIP=<hooks>
}

# Env vars that redirect/nullify git's config resolution (where core.hooksPath
# may live). Any value is suspicious; we block them on a governed git command.
_GIT_CONFIG_ENV = (
    "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
)


def _has_clustered_n(full_words: list[str]) -> bool:
    """Short-flag cluster containing `n`, e.g. `-nm`, `-an`, `-nv` — each is
    `git commit -n` (--no-verify) in disguise. Excludes long flags (`--name`)."""
    for w in full_words:
        if w.startswith("-") and not w.startswith("--") and "n" in w[1:]:
            return True
    return False


# Branches the fixer must never force-push or delete; history there is shared and
# protected server-side. The fixer only ever (force-)pushes its own
# feat|fix|chore|refactor/<n>- branch, so a force-push *targeting* one of these is
# always a mistake or an escape.
PROTECTED_BRANCHES = ("main", "master", "dev")

_FORCE_PUSH_FLAGS = ("--force", "-f", "--force-with-lease", "--force-if-includes")


@dataclass(frozen=True)
class PushArgs:
    """The structural facts a `git push …` argv carries, parsed from the AST
    tokens (never a regex over the raw string).

    `candidate_refspecs` already has the REMOTE slot removed: the first bare
    positional is the remote (`origin`) unless it carries refspec syntax ('+' or
    ':'), in which case it is a refspec even in slot 0. Everything downstream —
    the protected-branch force/delete guard AND the push gate's destination
    derivation — reads this one parse, so the two can never drift apart on what
    counts as a refspec."""

    has_force: bool
    is_delete: bool
    candidate_refspecs: tuple[str, ...]


def parse_push_args(push_words: list[str]) -> PushArgs:
    """Tokenised `git push …` args → `PushArgs`. `push_words[0]` may be the
    `push` subcommand itself (the slice `_git_subcommand` hands back)."""
    args = push_words[1:] if push_words and push_words[0] == "push" else push_words
    has_force = False
    is_delete = False
    refspecs: list[str] = []
    for w in args:
        if w in _FORCE_PUSH_FLAGS:
            has_force = True
            continue
        if w in ("--delete", "-d"):
            is_delete = True
            continue
        if w.startswith("--"):
            continue  # other long flag (e.g. --tags, --set-upstream)
        if w.startswith("-") and len(w) > 1:
            # short-flag cluster: -f folded in (-fu, -uf) is still a force push
            if "f" in w[1:]:
                has_force = True
            if "d" in w[1:]:
                is_delete = True
            continue
        # bare positional: the remote, or a refspec. The first bare word is the
        # remote (origin); subsequent bare words are refspecs. But callers may
        # omit the remote (`git push --force +main` is invalid; `git push origin
        # +main` is the form), so collect ALL bare words as candidate refspecs and
        # also test the remote slot — a protected name there is harmless (it's a
        # remote, not a ref) UNLESS it carries '+'/':' refspec syntax.
        refspecs.append(w)
    candidates = tuple(
        r for i, r in enumerate(refspecs)
        if i != 0 or r.startswith("+") or ":" in r
    )
    return PushArgs(has_force=has_force, is_delete=is_delete, candidate_refspecs=candidates)


def _refspec_destination(refspec: str) -> tuple[str, bool]:
    """(destination ref, is_plus_forced) for a refspec, WITHOUT truncating the
    ref: `+a:b`→(b,True); `+a`→(a,True); `a:b`→(b,False); `a`→(a,False), and a
    fully-qualified `refs/heads/fix/42-x` keeps its full branch name `fix/42-x`.

    Keeping the whole name matters for the push gate — `is_governed_branch`
    needs `fix/42-x`, not the trailing path component `42-x`."""
    forced = refspec.startswith("+")
    body = refspec[1:] if forced else refspec
    dst = body.split(":", 1)[1] if ":" in body else body
    return dst.removeprefix("refs/heads/"), forced


def push_destination_refs(push_words: list[str]) -> tuple[str, ...]:
    """Branch refs this `git push …` argv EXPLICITLY writes, in order, deduped.

    `()` when the argv names no refspec at all (`git push`, `git push origin`,
    `git push -u origin` with the branch omitted) — git then resolves the
    destination from the CURRENT branch via push.default, which is runtime state
    this parse cannot see, so the caller must fall back to the checked-out branch
    (and, when HEAD is detached, has no answer at all).

    This is the push gate's half of the parse that `_push_destructive_target`
    already did for the protected-branch guard — same `parse_push_args`, so
    `git push origin HEAD:refs/heads/x`, `git push --force-with-lease origin
    fix/42-x` and `git push -u origin fix/42-x` are seen identically by both.
    A bare `HEAD`/`@` destination is returned verbatim; resolving it against the
    checked-out branch is policy, and policy lives in the domain."""
    return tuple(dict.fromkeys(
        _refspec_destination(r)[0]
        for r in parse_push_args(push_words).candidate_refspecs
    ))


def _push_destructive_target(push_words: list[str]) -> str | None:
    """AST-derived (not regex): given the tokenised `git push …` args, return a
    protected branch name iff this push would force-overwrite or delete its
    history. Two structural signals, evaluated over the parsed tokens:

      * a force flag (--force / -f / --force-with-lease / clustered short flag
        containing 'f' like -fu) PLUS an explicit refspec whose destination is a
        protected branch — `git push --force origin main`,
        `git push -f origin HEAD:dev`;
      * a leading-'+' refspec (force syntax) whose destination is protected —
        `git push origin +main`, `git push origin develop:+master` is not valid;
        the '+' leads the refspec: `+<src>:<dst>` or `+<branch>`;
      * a `--delete` / `-d` of a protected branch — `git push origin --delete main`.

    Returns the offending branch, or None. A force-push to the fixer's own
    fix/feat/… branch (no protected destination) is intentionally allowed — that
    is the legitimate rebase-and-repush in the address-review loop. Pushes with
    NO explicit refspec are left to the stateful check-push.sh (the current
    branch is runtime state the governor cannot see)."""
    parsed = parse_push_args(push_words)
    for r in parsed.candidate_refspecs:
        dst, plus_forced = _refspec_destination(r)
        # `_basename` here (not in `_refspec_destination`) because the protected
        # set is bare names: `refs/heads/main` → `main`.
        if _basename(dst) in PROTECTED_BRANCHES and (
            parsed.has_force or plus_forced or parsed.is_delete
        ):
            return _basename(dst)
    # `git push --delete <remote> main` puts the branch in a bare positional that
    # we treated as a refspec above; the loop already covers it. Force-push with
    # no refspec is deferred to check-push.sh.
    return None


def bypass_reason(
    full_words: list[str], kind: str, env: list[str] | None = None,
    extra_env: tuple[str, ...] = (),
) -> str | None:
    """Return a human-readable reason if this command tries to bypass the gate.

    `env` are variable_assignment prefixes (HUSKY=0 git …); `extra_env` are env
    vars recovered from an `env KEY=VAL git …` wrapper. Both are checked.
    """
    all_env = [*(env or []), *extra_env]
    joined = " ".join([*all_env, *full_words])

    # Force-push / delete of a PROTECTED branch (main/master/dev). Parsed from the
    # AST tokens, not the raw string, so `git push --force origin main`,
    # `git push origin +main`, `git push origin --delete dev`, and clustered
    # `-fu origin main` are all caught, while a legitimate force-push of the
    # fixer's own `fix/<n>-…` branch is allowed.
    if kind == "git-push":
        inner, _ = _strip_env_wrapper(full_words)
        sub = _git_subcommand(inner) if inner and _basename(inner[0]) == "git" else None
        if sub is not None and sub[0] == "push":
            dst = _push_destructive_target(sub[1])
            if dst is not None:
                return (
                    f"force-push/delete targeting protected branch '{dst}' is not "
                    f"allowed. The fixer only pushes its own feature branch."
                )

    # --no-verify (long form) is unambiguous on both commit and push.
    if "--no-verify" in full_words:
        return "--no-verify disables commit/push hooks. The gate is mandatory."
    # Short `-n`: on `git commit` it IS --no-verify; on `git push` it is --dry-run
    # (harmless). Only treat the short form as a bypass for commit.
    if kind == "git-commit":
        if "-n" in full_words or _has_clustered_n(full_words):
            return "`-n` is shorthand for --no-verify on commit. The gate is mandatory."

    # core.hooksPath override, via `-c k=v`, `git config`, or anywhere on the line.
    for i, w in enumerate(full_words):
        if w == "-c" and i + 1 < len(full_words) and "hookspath" in full_words[i + 1].lower():
            return "overriding core.hooksPath bypasses the gate."
        if w.startswith("-c") and "hookspath" in w.lower():
            return "overriding core.hooksPath bypasses the gate."
    if kind == "git-config":
        return "writing core.hooksPath via `git config` would disable the gate."
    if "core.hookspath" in joined.lower():
        return "attempt to disable/redirect the hook path detected."

    # Env-var disable signals (hook managers + git config redirection).
    for e in all_env:
        name, sep, value = e.partition("=")
        if not sep:
            continue
        if name in _HOOK_DISABLE_ENV:
            allowed_off = _HOOK_DISABLE_ENV[name]
            if allowed_off is None or value.lower() in allowed_off:
                return f"{name}={value} disables git hooks. The gate is mandatory."
        if name in _GIT_CONFIG_ENV:
            return f"{name} redirects git's config (where core.hooksPath lives) — blocked."
        if name == "HOME":
            return "overriding HOME redirects git's global config — blocked."
        if "hookspath" in value.lower():
            return "attempt to disable/redirect the hook path detected."
    return None


def find_violations(command: str) -> list[str]:
    """Pure function used by tests: return bypass reasons for a command string.

    Does not run checkers (those touch the filesystem); only structural bypass
    detection. Returns an empty list when nothing structural is wrong.
    """
    reasons: list[str] = []
    for words, env in extract_commands_with_env(command):
        g = classify(words)
        if g is None:
            continue
        r = bypass_reason(g.full_words, g.kind, env, g.extra_env)
        if r:
            reasons.append(r)
    return reasons
