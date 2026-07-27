"""Run the Claude SDK runner against a NON-Anthropic Anthropic-compatible
endpoint, by injecting a Claude Code *profile* directory's environment.

A "profile dir" is what the `CLAUDE_CONFIG_DIR` convention produces: a directory
holding `settings.json` (with an `env` block naming the base URL and the model
aliases), a `.env` with the provider key, and that profile's own state. Pointing
the fixer at GLM (Z.ai) needs nothing more than that env — the wire protocol is
Anthropic's, so `AgentRunner` is reused verbatim.

Two reasons the env must be injected explicitly rather than left to the CLI:

  * the daemon runs SDK sessions with `setting_sources=["project"]`, so a
    profile's `settings.json` is never loaded — including its `env` block. That
    exclusion is DESIRED (an interactive profile's hooks/plugins have no place in
    a headless daemon); only the env is wanted, so only the env is taken.
  * `ANTHROPIC_API_KEY` must be actively BLANKED. Inherited from the launching
    shell it would let the CLI fall back to Anthropic credentials and bill a
    real Anthropic account for what was configured as a GLM run.

Secrets discipline: the auth token passes through this module, so no function
here ever puts an env VALUE into a log line or an exception message — key names
and file paths only.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass

from ..domain.agent_task import AgentRuntime, AgentTask
from ..domain.outcome import AgentResult

# Provider key as it appears in a Z.ai profile's `.env`, mapped onto the header
# the Anthropic-compatible endpoint authenticates with.
_PROVIDER_KEY = "ZAI_API_KEY"
_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"


def parse_dotenv(text: str) -> dict[str, str]:
    """KEY=VALUE lines → dict. Deliberately NOT a shell: no interpolation, no
    command substitution, no `export` semantics beyond stripping the word — a
    profile's `.env` is read for its key, never executed. Blank lines, comments
    and unparseable lines are skipped; surrounding single/double quotes are
    stripped from the value."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def claude_profile_env(settings_file: str) -> dict[str, str]:
    """Environment to inject so the SDK subprocess talks to the profile's
    endpoint instead of Anthropic's.

    `settings_file` is the profile's settings.json; its parent directory is the
    profile dir. Raises ValueError (naming the PATH, never a value) when the file
    is missing, unreadable, or not a JSON object.
    """
    path = os.path.abspath(os.path.expanduser(settings_file.strip()))
    try:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        raise ValueError(f"profile settings file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"profile settings file is not valid JSON: {path} ({exc.msg})") from None
    except OSError as exc:
        raise ValueError(f"profile settings file is unreadable: {path} ({exc.strerror})") from None
    if not isinstance(settings, dict):
        raise ValueError(f"profile settings file must contain a JSON object: {path}")

    profile_dir = os.path.dirname(path)
    env: dict[str, str] = {}
    block = settings.get("env")
    if isinstance(block, dict):
        # str→str only: the CLI subprocess env cannot carry a number or a nested
        # object, and coercing one would invent a value the operator never wrote.
        env.update({k: v for k, v in block.items() if isinstance(k, str) and isinstance(v, str)})

    env["CLAUDE_CONFIG_DIR"] = profile_dir

    dotenv_path = os.path.join(profile_dir, ".env")
    try:
        with open(dotenv_path, encoding="utf-8") as fh:
            dotenv = parse_dotenv(fh.read())
    except OSError:
        # No `.env` is legitimate: the key may already be exported in the shell
        # that launched the daemons. Absent here simply means "nothing to add".
        dotenv = {}
    if dotenv.get(_PROVIDER_KEY):
        env[_AUTH_TOKEN] = dotenv[_PROVIDER_KEY]

    # Blank, not absent: SDK options.env MERGES onto os.environ, so deleting the
    # key here would let an inherited ANTHROPIC_API_KEY survive. An empty string
    # overrides it and forces the endpoint's own auth to be used.
    env["ANTHROPIC_API_KEY"] = ""
    return env


def endpoint_host(env: dict[str, str]) -> str:
    """Host of the injected base URL, for a startup log line. Never a secret."""
    url = env.get("ANTHROPIC_BASE_URL", "")
    return url.split("://", 1)[-1].split("/", 1)[0] if url else "(default)"


@dataclass(frozen=True)
class EnvInjectingRuntime:
    """An `AgentRuntime` decorator that adds a fixed env to every task.

    A decorator, not a runner variant: swapping the endpoint is purely an
    environment concern, so `AgentRunner` stays unaware of it and the same
    composition works under `JournalingRuntime` (which must wrap this, so glm
    runs are ledgered like any other).

    The task's OWN env wins on conflict — TEST_CMD and friends are per-dispatch
    facts a profile must never be able to shadow.
    """

    inner: AgentRuntime
    env: dict[str, str]

    async def run(self, task: AgentTask) -> AgentResult:
        return await self.inner.run(dataclasses.replace(task, env={**self.env, **task.env}))
