"""GitHub account identity; bot suffixes encode account type (ADR 0001)."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}")


@dataclass(frozen=True)
class Account:
    kind: Literal["User", "Bot"]
    login: str

    def __post_init__(self) -> None:
        if (self.kind not in ("User", "Bot") or not isinstance(self.login, str)
                or len(self.login) > 39 or not LOGIN.fullmatch(self.login)):
            raise ValueError("account requires a type and bounded login")
        object.__setattr__(self, "login", self.login.casefold())

    def __str__(self) -> str:
        return self.login + ("[bot]" if self.kind == "Bot" else "")


def configured_account(login: str) -> Account | None:
    """Configuration and canonical REST spelling retain the bot suffix."""
    if not isinstance(login, str) or len(login) > 128:
        return None
    login = login.strip().casefold()
    kind = "Bot" if login.endswith("[bot]") else "User"
    try:
        return Account(kind, login.removesuffix("[bot]") if kind == "Bot" else login)
    except ValueError:
        return None


def account_eq(author: Account | str | None, configured: str) -> bool:
    observed = configured_account(author) if isinstance(author, str) else author
    return isinstance(observed, Account) and observed == configured_account(configured)


def is_bot_login(login: str) -> bool:
    account = configured_account(login)
    return account is not None and account.kind == "Bot"
