"""Agent child environment, with ADR 0005 ancestor authority retention."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

CHILD_ENV_MANIFEST = "ISSUE_AGENT_CHILD_ENV_KEYS"

_BASE_KEYS = frozenset(
    {
        "API_TIMEOUT_MS",
        # Direct worker builds must share the operator's storage and worker limits.
        "CARGO_BUILD_JOBS",
        "CARGO_INCREMENTAL",
        "CARGO_TARGET_DIR",
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
        "PW_WORKERS",
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
        # The legs' own Claude login (`claude setup-token`); codex_runner drops it.
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "ZAI_API_KEY",
    }
)

# ADR 0005: the supervised launcher retains ISSUE_AGENT_* control-plane values;
# provider processes receive none of them.


def without_supervisor_authority(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove control-plane authority from a paid provider's environment."""

    return {
        key: value
        for key, value in environment.items()
        if not key.startswith("ISSUE_AGENT_")
    }


def child_process_env(
    inherited: Mapping[str, str],
    explicit: Mapping[str, str] | None = None,
    *,
    additional_keys: Iterable[str] = (),
) -> dict[str, str]:
    """Select child variables: the allowlist from the daemon's environment,
    plus what the harness names explicitly. Nothing inherited carries a
    publication credential; a fix leg's one token is explicit, and the leg
    sandbox unsets every GitHub token for a leg that does not publish (#413)."""

    configured = dict(explicit or {})
    allowed = _BASE_KEYS | _RUNTIME_AUTH_KEYS | frozenset(additional_keys) | frozenset(configured)
    selected = {key: value for key, value in inherited.items() if key in allowed}
    selected.update(configured)
    return selected
