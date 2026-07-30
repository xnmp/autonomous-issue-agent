"""Explicit environment policy for paid agent child processes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

CHILD_ENV_MANIFEST = "ISSUE_AGENT_CHILD_ENV_KEYS"

_BASE_KEYS = frozenset(
    {
        "API_TIMEOUT_MS",
        "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "COLORTERM",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

_RUNTIME_AUTH_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "ZAI_API_KEY",
    }
)

_PUBLICATION_CREDENTIAL_KEYS = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GIT_ASKPASS",
        "SSH_AGENT_PID",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
    }
)


def child_process_env(
    inherited: Mapping[str, str],
    explicit: Mapping[str, str] | None = None,
    *,
    additional_keys: Iterable[str] = (),
) -> dict[str, str]:
    """Select child variables and exclude publication credentials."""

    configured = dict(explicit or {})
    allowed = (
        _BASE_KEYS
        | _RUNTIME_AUTH_KEYS
        | frozenset(additional_keys)
        | frozenset(configured)
    ) - _PUBLICATION_CREDENTIAL_KEYS
    selected = {key: value for key, value in inherited.items() if key in allowed}
    selected.update(
        {
            key: value
            for key, value in configured.items()
            if key not in _PUBLICATION_CREDENTIAL_KEYS
        }
    )
    return selected
