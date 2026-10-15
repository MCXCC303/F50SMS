"""Exception types raised by f50-sms.

Every exception inherits from :class:`F50Error` so the CLI only needs to catch
one base class to print a friendly message.

Exit codes: `0` success, `2` command-line usage error (argparse convention),
and `3` upward for the domain errors below, kept non-overlapping.
"""

from __future__ import annotations


class F50Error(Exception):
    """Base class for every f50-sms error."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        if self.hint:
            return f"{self.message}" + chr(10) + f"hint: {self.hint}"
        return self.message


class F50ConnectionError(F50Error):
    """Cannot reach the router (network down, timeout, DNS failure, ...)."""

    exit_code = 3


class F50AuthError(F50Error):
    """Login required, wrong password, or the login was rejected.

    `needs_password=True` means "a password would let us continue". The CLI
    uses that to prompt for the password and retry instead of failing outright.
    """

    exit_code = 4

    def __init__(self, message: str, *, hint: str | None = None, needs_password: bool = False) -> None:
        super().__init__(message, hint=hint)
        self.needs_password = needs_password


class F50ProtocolError(F50Error):
    """The router returned data we could not parse or that is out of shape."""

    exit_code = 5


class F50NotFoundError(F50Error):
    """No local result matched (e.g. the requested message id)."""

    exit_code = 6
