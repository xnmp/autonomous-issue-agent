"""Configured remote identity shared by publication and upstream observation."""

from urllib.parse import urlparse


def remote_identity(remote_url: str) -> tuple[str, str]:
    raw = remote_url.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        path = parsed.path
    elif "@" in raw and ":" in raw:
        authority, path = raw.split(":", 1)
        host = authority.rsplit("@", 1)[-1]
    else:
        raise ValueError("Git URL is not a remote repository URL")
    parts = tuple(part for part in path.strip("/").split("/") if part)
    if len(parts) != 2 or not host:
        raise ValueError("Git URL has no owner/repository identity")
    owner, name = parts
    return host, f"{owner}/{name.removesuffix('.git')}"


def require_repository_remote(remote_url: str, repository: str, approved_host: str) -> None:
    host, resolved_repository = remote_identity(remote_url)
    if host.lower() != approved_host.lower() or resolved_repository.lower() != repository.lower():
        raise ValueError("Git remote URL does not match the configured repository")


def require_safe_fetch_url(remote_url: str) -> None:
    """A captured fetch endpoint may enter argv and diagnostic output.

    HTTPS credentials belong in Git's credential helper, never this value.
    SSH user names are transport identity; passwords and URL parameters are
    not supported. Origin alias rereads cannot prove which endpoint was fetched.
    """
    if (not isinstance(remote_url, str) or not remote_url or len(remote_url) > 4096
            or remote_url != remote_url.strip() or remote_url.startswith("-")
            or any(ord(char) < 32 or ord(char) == 127 for char in remote_url)):
        raise ValueError("fetch URL must be a bounded, nonempty endpoint")
    remote_identity(remote_url)
    if "://" in remote_url:
        parsed = urlparse(remote_url)
        if parsed.scheme not in {"http", "https", "ssh", "git"}:
            raise ValueError("fetch URL uses an unsupported transport")
        if parsed.query or parsed.fragment:
            raise ValueError("fetch URL parameters are not supported")
        if parsed.password is not None or (parsed.scheme != "ssh" and parsed.username is not None):
            raise ValueError("fetch URL must not contain credentials; use a Git credential helper")
    else:
        authority, _ = remote_url.split(":", 1)
        if authority.count("@") != 1 or any(char in remote_url for char in "?#"):
            raise ValueError("fetch URL requires a plain SSH endpoint")
