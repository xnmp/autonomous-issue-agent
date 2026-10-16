"""Validate GitHub author metadata before granting authorship (ADR 0001)."""
from ..domain.identity import Account


def parse_account(raw: object) -> Account | None:
    if not isinstance(raw, dict):
        return None
    login = raw.get("login")
    kind = raw.get("__typename", raw.get("type"))
    # gh's JSON author projection uses is_bot instead of the GraphQL typename.
    if kind is None and type(raw.get("is_bot")) is bool:
        kind = "Bot" if raw["is_bot"] else "User"
    if kind not in ("User", "Bot") or not isinstance(login, str) or len(login) > 44:
        return None
    try:
        return Account(kind, login.removesuffix("[bot]") if kind == "Bot" else login)
    except ValueError:
        return None
