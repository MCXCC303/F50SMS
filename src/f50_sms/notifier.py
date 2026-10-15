"""Turn new messages into desktop notifications.

The daemon loop lives in `commands.cmd_notify`; this module only knows how to
describe and run one notification, which keeps it easy to test without a
desktop session.
"""

from __future__ import annotations

import subprocess
from typing import List, Optional, Sequence

from .models import Message
from .render import sender_label

DEFAULT_PROGRAM = "notify-send"

#: How long a single notification may take before we give up on it.
SEND_TIMEOUT = 10.0


def notification_argv(message: Message, *, program: str = DEFAULT_PROGRAM) -> List[str]:
    """Build the notification command for one message.

    Layout: the app name carries "SOURCE NUMBER", the summary is the message id
    and the body is the full text. Each part is a separate argv element, so an
    SMS body is passed through verbatim and never parsed as shell syntax.
    """

    app_name = " ".join(part for part in (sender_label(message), message.number) if part)
    return [program, f"--app-name={app_name}", "#" + message.short_id, message.content or ""]


def send(argv: Sequence[str], *, timeout: float = SEND_TIMEOUT) -> Optional[str]:
    """Run a notification command; return a problem description, or None.

    Failures are reported rather than raised: a desktop without a notification
    daemon should not take down a long-running watcher.
    """

    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return f"{argv[0]} was not found"
    except subprocess.TimeoutExpired:
        return f"{argv[0]} did not finish within {timeout:g}s"
    except OSError as exc:
        return str(exc)

    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return detail[0] if detail else f"{argv[0]} exited with status {done.returncode}"
    return None
