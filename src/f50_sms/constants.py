"""Program-wide constants and tiny shared helpers.

Kept in their own module so both the CLI entry point and the command handlers
can import them without creating a circular dependency.
"""

from __future__ import annotations

import getpass
import os

#: The command name shown in usage and error messages.
PROG = "f50sms"

#: Sentinel for "config set --password" with no value: prompt for the password.
PROMPT_PASSWORD = object()

#: Reserved for argparse usage errors, kept clear of the domain exit codes.
EXIT_USAGE = 2


def can_prompt() -> bool:
    """Whether we may prompt on stdin: it must be a terminal and not disabled."""

    if os.environ.get("F50_SMS_NO_PROMPT"):
        return False
    try:
        import sys

        return bool(sys.stdin.isatty())
    except Exception:  # pragma: no cover - defensive
        return False


def prompt_password(prompt: str = "Router password: ") -> str:
    """Read a password without echoing; returns an empty string if not interactive."""

    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print("", flush=True)
        return ""
    except Exception:  # pragma: no cover - no usable terminal
        return ""
