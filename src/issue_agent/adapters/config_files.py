"""The operator's config files: edit one assignment, compose, publish (ADR 0007).

`set_assignment` is the pure text edit: the last top-level `KEY=` or
`export KEY=` line takes the new value, keeping its `export`, its trailing
comment and every other line; a missing key is appended. What it cannot edit
safely it refuses (`EditRefused`) rather than guess: an assignment inside an
indented block, one sharing its line with another command, a quoted value
that continues onto the next line. The rest is the I/O around it: an atomic
write that keeps the file's mode, `bin/compose-env.sh`, and publishing the
composed file to the service snapshots the installers created.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Mapping

from .atomic_file import atomic_write
from .env_file import source_env_file
from .journal import Journal

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGNMENT = re.compile(r"^(?P<indent>[ \t]*)(?P<export>export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<rest>.*)$")
_VALUE_LIMIT = 1000


class EditRefused(ValueError):
    """The edit cannot be made safely; the message says why."""


def _word_end(text: str) -> int:
    """Where the shell word at the start of `text` ends; -1 for a quote left
    open (the value continues on the next line) or shell syntax beyond one
    word (`;`, a pipe, a substitution)."""
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char in " \t":
            return i
        if char == "\\":
            i += 2
        elif char == "'" or (char == "$" and text[i + 1:i + 2] == "'"):
            start = i + (2 if char == "$" else 1)
            j = start
            while j < n and text[j] != "'":
                j += 2 if char == "$" and text[j] == "\\" else 1
            if j >= n:
                return -1
            i = j + 1
        elif char == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                return -1
            i = j + 1
        elif char in ";&|<>()`":
            return -1
        else:
            i += 1
    return min(i, n)


def _trailing(rest: str, key: str) -> str:
    """The comment after an assignment's value (with its spacing), or ""."""
    end = _word_end(rest)
    if end < 0 or rest.rstrip(" \t").endswith("\\"):
        raise EditRefused(f"{key}'s line is more than one plain assignment; edit it by hand")
    tail = rest[end:]
    if tail.strip() and not tail.lstrip(" \t").startswith("#"):
        raise EditRefused(f"{key}'s line is more than one plain assignment; edit it by hand")
    return tail if tail.strip() else ""


def assigns(text: str, key: str) -> bool:
    """Whether `text` assigns `key` on any non-comment line."""
    return any((m := _ASSIGNMENT.match(line)) is not None and m["key"] == key for line in text.splitlines())


def set_assignment(text: str, key: str, value: str | None) -> str:
    """`text` with `key` set to `value` (shell-quoted), or every top-level
    assignment of it removed when `value` is None."""
    if "\0" in text:
        raise EditRefused("the file contains NUL bytes")
    if not _NAME.fullmatch(key):
        raise EditRefused(f"{key!r} is not a variable name")
    if value is not None and (len(value) > _VALUE_LIMIT or any(c in value for c in "\r\n\0")):
        raise EditRefused(f"{key}'s value must be one line of at most {_VALUE_LIMIT} characters")
    lines = text.splitlines(keepends=True)
    hits: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = _ASSIGNMENT.match(line.rstrip("\r\n"))
        if match is not None and match["key"] == key:
            if match["indent"]:
                raise EditRefused(f"{key} is assigned inside an indented block (line {index + 1}); edit it by hand")
            hits.append((index, match))
    if value is None:
        for _index, match in hits:
            _trailing(match["rest"], key)
        dropped = {index for index, _match in hits}
        return "".join(line for index, line in enumerate(lines) if index not in dropped)
    rendered = f"{key}={shlex.quote(value)}"
    if not hits:
        return text + ("" if not text or text.endswith("\n") else "\n") + rendered + "\n"
    index, match = hits[-1]
    line = lines[index]
    ending = line[len(line.rstrip("\r\n")):]
    lines[index] = f"{match['export'] or ''}{rendered}{_trailing(match['rest'], key)}{ending}"
    return "".join(lines)


def read_text(path: str) -> str:
    """A config file as text; a file that is not UTF-8 is refused, not mangled."""
    try:
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditRefused(f"{path} is not UTF-8 text") from exc


def write_text(path: str, text: str, *, expected: str) -> None:
    """Replace the file atomically, keeping its mode (shared.env is 0600) and
    writing through a symlink to its target. Refused when the file no longer
    holds `expected` (what the caller read): a hand edit made since is never
    overwritten."""
    target = os.path.realpath(path)
    if read_text(target) != expected:
        raise EditRefused(f"{path} changed since it was read; reload and try again")
    atomic_write(target, text, mode=stat.S_IMODE(os.stat(target).st_mode))


def sourced(paths: tuple[str, ...], inherited: Mapping[str, str]) -> dict[str, str]:
    """The environment the files compose to, sourced in order like
    `bin/compose-env.sh` (later files win)."""
    env = {key: value for key, value in inherited.items() if key != "AGENT_ENV"}
    for path in paths:
        env.update(source_env_file(path, env, assigned_only=True))
    return env


def compose(harness_root: str, name: str, environ: Mapping[str, str]) -> str:
    """Run `bin/compose-env.sh <name>`; "" on success, else why it failed."""
    try:
        done = subprocess.run([shutil.which("bash") or "/bin/bash", os.path.join(harness_root, "bin", "compose-env.sh"),
                               name], env=dict(environ), cwd=harness_root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"compose-env.sh could not run: {exc}"
    return "" if done.returncode == 0 else f"compose-env.sh failed: {done.stderr.strip()[-400:]}"


def compose_staged(harness_root: str, name: str, shared: str, overlay: str, environ: Mapping[str, str],
                   scratch: str) -> tuple[str, str]:
    """Run `bin/compose-env.sh <name>` on candidate source texts in `scratch`
    (a private directory) instead of the real config: `(composed path, "")`,
    or `("", why it failed)`. Nothing a daemon reads is touched."""
    for script in ("compose-env.sh", "point-at.sh"):
        os.makedirs(os.path.join(scratch, "bin"), exist_ok=True)
        shutil.copy(os.path.join(harness_root, "bin", script), os.path.join(scratch, "bin", script))
    os.makedirs(os.path.join(scratch, "config", "repos"), exist_ok=True)
    for path, text in ((os.path.join(scratch, "config", "shared.env"), shared),
                       (os.path.join(scratch, "config", "repos", f"{name}.env"), overlay)):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    if problem := compose(scratch, name, environ):
        return "", problem
    return os.path.join(scratch, "config", f".active.{name}.env"), ""


def _service_id(name: str) -> str:
    """The installers' canonical deployment id (install-*.sh `canonical_deployment_id`)."""
    return re.sub(r"[^a-z0-9._-]", "-", name.lower()).strip("-") or "target"


def service_snapshots(name: str, home: str) -> tuple[str, ...]:
    """The private config copies installed services read (systemd, launchd)
    for this deployment, where they exist."""
    paths = (os.path.join(home, ".config", "issue-agent", f"{name}.env"),
             os.path.join(home, "Library", "Application Support", "issue-agent", f"{_service_id(name)}.env"))
    return tuple(path for path in paths if os.path.isfile(path))


def publish(active: str, snapshots: tuple[str, ...], environ: Mapping[str, str]) -> list[str]:
    """Copy the composed file to each service snapshot, as install-service
    does; one whose REPO or TARGET_DIR differs belongs to another deployment
    and is left alone. Returns a problem per snapshot not published."""
    problems: list[str] = []
    try:
        with open(active, "rb") as handle:
            content = handle.read()
        mine = source_env_file(active, environ, assigned_only=True)
    except (OSError, ValueError) as exc:
        return [f"{active} unreadable: {exc}"]
    for path in snapshots:
        try:
            theirs = source_env_file(path, environ, assigned_only=True)
            if any(theirs.get(key, "") != mine.get(key, "") for key in ("REPO", "TARGET_DIR")):
                problems.append(f"{path} belongs to another REPO/TARGET_DIR; not updated")
                continue
            atomic_write(os.path.realpath(path), content, mode=0o600)
        except (OSError, ValueError) as exc:
            problems.append(f"{path} not updated: {exc}")
    return problems


def journal_change(state_dir: str, fields: Mapping[str, object]) -> None:
    """One `settings_change` line in the deployment's fix journal."""
    if state_dir:
        Journal(os.path.join(state_dir, "journal.jsonl")).emit("settings_change", fields)
