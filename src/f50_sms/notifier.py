"""Turn new messages into desktop notifications.

The daemon loop lives in `commands.cmd_notify`; this module only knows how to
describe and run one notification, which keeps it easy to test without a
desktop session.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import List, Optional, Sequence

from .models import Message
from .render import sender_label

DEFAULT_PROGRAM = "notify-send"

#: How long a single notification may take before we give up on it.
SEND_TIMEOUT = 10.0

#: Action name that a click on the notification body activates. The desktop
#: spec reserves "default" for exactly that, and notify-send prints the chosen
#: action name on stdout, which is how the click reaches us.
DEFAULT_ACTION = "default"

#: Clipboard writers in preference order: Wayland first, then X11.
CLIPBOARD_COMMANDS = (
    ("wl-copy",),
    ("xclip", "-selection", "clipboard"),
    ("xsel", "--clipboard", "--input"),
)


def notification_argv(
    message: Message,
    *,
    program: str = DEFAULT_PROGRAM,
    action_label: Optional[str] = None,
) -> List[str]:
    """Build the notification command for one message.

    Layout: the app name carries "SOURCE NUMBER", the summary is the message id
    and the body is the full text. Each part is a separate argv element, so an
    SMS body is passed through verbatim and never parsed as shell syntax.

    With action_label the notification gains a clickable default action, which
    makes notify-send wait for the user and print the chosen action.
    """

    app_name = " ".join(part for part in (sender_label(message), message.number) if part)
    argv = [program, f"--app-name={app_name}"]
    if action_label:
        argv += ["--wait", f"--action={DEFAULT_ACTION}={action_label}"]
    argv += ["#" + message.short_id, message.content or ""]
    return argv


def wait_for_action(argv: Sequence[str]) -> Optional[str]:
    """Run a notification that offers an action; return the chosen action name.

    Blocks until the action is picked or the notification is dismissed, so it
    belongs on a worker thread rather than in the polling loop. Raises OSError
    when the program cannot be started at all.
    """

    done = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return done.stdout.strip() or None


def clipboard_command() -> Optional[List[str]]:
    """The first clipboard writer available on this machine, or None."""

    for candidate in CLIPBOARD_COMMANDS:
        if shutil.which(candidate[0]):
            return list(candidate)
    return None


def copy_to_clipboard(text: str, command: Optional[Sequence[str]] = None) -> Optional[str]:
    """Put text on the clipboard; return a problem description, or None.

    Both streams go to /dev/null on purpose: xclip forks a helper that keeps
    serving the selection, and if that helper inherits a pipe the caller waits
    on, the call never returns.
    """

    argv = list(command) if command else clipboard_command()
    if not argv:
        return "no clipboard tool found (install wl-clipboard or xclip)"
    try:
        done = subprocess.run(
            argv, input=text, text=True, timeout=SEND_TIMEOUT, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return f"{argv[0]} was not found"
    except subprocess.TimeoutExpired:
        return f"{argv[0]} did not finish within {SEND_TIMEOUT:g}s"
    except OSError as exc:
        return str(exc)
    if done.returncode != 0:
        return f"{argv[0]} exited with status {done.returncode}"
    return None


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
