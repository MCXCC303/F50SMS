"""f50sms command-line entry point.

Responsibilities kept here:
  * build the argparse tree and dispatch to a handler in commands.py
  * resolve connection settings (host / password / timeout)
  * the top-level try/except that maps domain errors onto process exit codes

Design notes:
  * Reading SMS requires a live session; when the session dies the router
    returns silent empty payloads, so the client re-authenticates. If only a
    password is missing we prompt once and retry.
  * Output is human-friendly by default and degrades to plain text when piped.
  * Long terminal output is handed to a pager; piped output never is.
  * Data formats (json/csv/...) emit data only, with no human summary mixed in.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Optional, Sequence

from . import __version__
from .client import F50Client
from .commands import COMMANDS
from .config import resolve_settings
from .constants import (
    EXIT_USAGE,
    PROG,
    PROMPT_PASSWORD,
    can_prompt as _can_prompt,
    prompt_password as _prompt_password,
)
from .errors import F50AuthError, F50Error
from .output import Output

LIST_FORMATS = ("table", "plain", "json", "jsonl", "csv", "markdown")
SHOW_FORMATS = ("card", "plain", "json")
THREAD_FORMATS = ("chat", "table", "plain", "json", "jsonl", "csv", "markdown")
THREADS_FORMATS = ("table", "json", "csv", "markdown")
STATUS_FORMATS = ("table", "json")


# ---------------------------------------------------------------------------
# argparse scaffolding
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """Route argparse errors through our own exit code."""

    def error(self, message: str):  # pragma: no cover - called by argparse
        self.print_usage(sys.stderr)
        raise SystemExitWithMessage(EXIT_USAGE, f"{PROG}: argument error: {message}")


class SystemExitWithMessage(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_parser() -> argparse.ArgumentParser:
    # Subcommands carry the same global options but with SUPPRESS defaults, so
    # a value given after the subcommand overrides the root one and an omitted
    # one does not clobber the root value with None.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, suppress_defaults=True)

    config_common = argparse.ArgumentParser(add_help=False)
    _add_global_options(config_common, suppress_defaults=True, credentials=False)

    parser = _Parser(
        prog=PROG,
        description="Read-only SMS client for the ZTE F50 mobile hotspot",
        epilog=(
            "examples:\n"
            f"  {PROG} list -u                  # unread only\n"
            f"  {PROG} list --search code       # search message bodies\n"
            f"  {PROG} show 57                  # inspect one message\n"
            f"  {PROG} thread 10010             # conversation with 10010\n"
            f"  {PROG} export -f csv -o sms.csv # export everything as CSV\n"
            f"  {PROG} watch                    # follow new messages live\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_global_options(parser)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    # list -----------------------------------------------------------------
    list_parser = subparsers.add_parser(
        "list", aliases=["ls"], parents=[common], help="list messages (default command)",
        description="List messages, optionally filtered by state, number, text, or time.",
    )
    _add_filter_options(list_parser)
    _add_format_option(list_parser, LIST_FORMATS, default=None)

    # show -----------------------------------------------------------------
    show_parser = subparsers.add_parser(
        "show", parents=[common], help="show one message in detail",
        description="Show one message by id, or the latest one from a number with --from.",
    )
    show_parser.add_argument("id", nargs="?", help="message id (the ID column in list)")
    show_parser.add_argument("--from", dest="from_number", metavar="NUMBER", help="latest message from this number")
    _add_format_option(show_parser, SHOW_FORMATS, default="card")

    # thread ---------------------------------------------------------------
    thread_parser = subparsers.add_parser(
        "thread", aliases=["chat"], parents=[common], help="show the full conversation with a number",
        description="List every message exchanged with a number, oldest first.",
    )
    thread_parser.add_argument("number", help="number (the last 8 digits are enough)")
    _add_format_option(thread_parser, THREAD_FORMATS, default=None)

    # threads --------------------------------------------------------------
    threads_parser = subparsers.add_parser(
        "threads", parents=[common], help="summarise conversations by number",
        description="Group all messages by number and show one row per conversation.",
    )
    threads_parser.add_argument("-n", "--limit", type=int, default=0, metavar="N", help="show only the first N conversations")
    threads_parser.add_argument("-u", "--unread", action="store_true", help="only conversations with unread messages")
    _add_format_option(threads_parser, THREADS_FORMATS, default=None)

    # count ----------------------------------------------------------------
    count_parser = subparsers.add_parser(
        "count", parents=[common], help="print the unread (or total) count for scripts",
        description="Print the unread count by default; handy in a status bar or a script.",
    )
    count_parser.add_argument("-a", "--all", action="store_true", help="print the total instead of unread")
    count_parser.add_argument("--json", action="store_true", help="print the full breakdown as JSON")

    # status ---------------------------------------------------------------
    status_parser = subparsers.add_parser(
        "status", parents=[common], help="device, SIM, and SMS storage status",
        description="Show network, signal, number, and SMS storage information.",
    )
    _add_format_option(status_parser, STATUS_FORMATS, default="table")

    # export ---------------------------------------------------------------
    export_parser = subparsers.add_parser(
        "export", parents=[common], help="export messages to a file",
        description="Export messages as JSON, JSONL, CSV, Markdown, or plain text.",
    )
    _add_filter_options(export_parser)
    _add_format_option(export_parser, ("json", "jsonl", "csv", "markdown", "txt"), default="json")
    export_parser.add_argument("-o", "--out", metavar="FILE", help="output file (default: stdout)")
    export_parser.add_argument("--include-raw", action="store_true", help="include the router's raw fields in JSON")

    # watch ----------------------------------------------------------------
    watch_parser = subparsers.add_parser(
        "watch", parents=[common], help="poll and print new messages as they arrive",
        description="Keep polling and print only newly arrived messages; Ctrl-C to stop.",
    )
    watch_parser.add_argument("-i", "--interval", type=float, default=15.0, metavar="SEC", help="poll interval in seconds (default: 15)")
    watch_parser.add_argument("--once", action="store_true", help="check once and exit (good for cron)")
    watch_parser.add_argument("--backlog", type=int, default=0, metavar="N", help="print the most recent N messages on start")

    # config ---------------------------------------------------------------
    config_parser = subparsers.add_parser(
        "config", parents=[config_common], help="show or save settings",
        description="Settings live in ~/.config/f50-sms/config.json by default (mode 600).",
    )
    config_sub = config_parser.add_subparsers(dest="config_action", metavar="action")
    config_sub.add_parser("show", parents=[config_common], help="print the settings in effect")
    config_sub.add_parser("path", parents=[config_common], help="print the config file path")
    set_parser = config_sub.add_parser("set", parents=[config_common], help="save settings")
    set_parser.add_argument("--host", metavar="URL", help="router URL")
    set_parser.add_argument(
        "--password", metavar="PW", nargs="?", const=PROMPT_PASSWORD,
        help="router password; omit the value to be prompted (not echoed)",
    )
    set_parser.add_argument("--timeout", type=float, metavar="SEC", help="request timeout in seconds")
    config_sub.add_parser("unset", parents=[config_common], help="delete the config file")

    # doctor ---------------------------------------------------------------
    subparsers.add_parser(
        "doctor", parents=[common], help="self-check: reachability, session, SMS features",
        description="Walk the network, the API, and the SMS features; run this first when something breaks.",
    )

    return parser


def _add_global_options(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool = False,
    credentials: bool = True,
) -> None:
    """Attach the global options.

    The root parser uses real defaults; subparsers use `argparse.SUPPRESS`. That
    makes both `f50sms --password X count` and `f50sms count --password X`
    work: the later value wins, and an omitted one keeps the root's value.
    """

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress_defaults else value

    if credentials:
        parser.add_argument(
            "--host", metavar="URL", default=default(None),
            help="router web UI URL (default: http://192.168.0.1)",
        )
        parser.add_argument(
            "--password", metavar="PW", default=default(None),
            help="router password (needed when the session expires; also F50_SMS_PASSWORD)",
        )
        parser.add_argument(
            "--timeout", type=float, metavar="SEC", default=default(None),
            help="request timeout in seconds (default: 10)",
        )
    parser.add_argument("--config", metavar="FILE", default=default(None), help="path to the config file")
    parser.add_argument(
        "--color", choices=("auto", "always", "never"), default=default("auto"),
        help="colour policy (default: auto, terminal only)",
    )
    parser.add_argument(
        "--width", type=int, metavar="N", default=default(None), help="output width (default: terminal width)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--pager", dest="pager", action="store_const", const="always", default=default("auto"),
        help="always page (by default only when taller than one screen)",
    )
    group.add_argument(
        "--no-pager", dest="pager", action="store_const", const="never", default=default("auto"),
        help="never page, print directly",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", default=default(False),
        help="suppress the summary line and other notes",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=default(False),
        help="write debug output to stderr",
    )


def _add_format_option(parser: argparse.ArgumentParser, choices: Sequence[str], default: Optional[str]) -> None:
    parser.add_argument(
        "-f", "--format", dest="format", choices=list(choices), default=default,
        help="output format: " + " / ".join(choices),
    )


def _add_filter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-u", "--unread", action="store_true", help="unread only")
    parser.add_argument(
        "--tag", action="append", metavar="STATE",
        help="filter by state: read/unread/sent/failed/draft (repeat or comma-separate)",
    )
    parser.add_argument("--from", dest="from_number", metavar="NUMBER", help="only this number (last 8 digits match)")
    parser.add_argument("-s", "--search", metavar="TEXT", help="body contains TEXT (case-insensitive)")
    parser.add_argument("--regex", metavar="PATTERN", help="body matches the regular expression")
    parser.add_argument("--since", metavar="TIME", help="start time: 2026-10-01 / 2026-10-01 08:00 / 7d / 24h")
    parser.add_argument("--until", metavar="TIME", help="end time, same formats as --since")
    parser.add_argument("-n", "--limit", type=int, default=0, metavar="N", help="show at most N messages (default: all)")
    parser.add_argument("-r", "--reverse", action="store_true", help="oldest first")
    parser.add_argument("--full", action="store_true", help="do not truncate the body")


#: Flags the root parser owns; when they appear first they are not filters.
GLOBAL_FLAGS = frozenset({
    "-h", "--help", "--version",
    "--host", "--password", "--timeout", "--config",
    "--color", "--width", "--pager", "--no-pager",
    "-q", "--quiet", "-v", "--verbose",
})


def _looks_like_filters(argv: Sequence[str]) -> bool:
    """Decide whether the list subcommand can be implied.

    Bare filters such as `f50sms -u` mean list; a leading global flag such as
    `--host` leaves the command line alone.
    """

    if not argv:
        return False
    first = argv[0]
    if not first.startswith("-"):
        return first not in COMMANDS
    return first.split("=", 1)[0] not in GLOBAL_FLAGS


def _opt(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    """Read a possibly-absent option (the config command has no --host etc.)."""

    return getattr(args, name, default)


def _ensure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    # Nothing else to do for non-Windows terminals.


def main(argv: Optional[Sequence[str]] = None) -> int:
    _ensure_stdio()
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        argv = ["list"]
    elif _looks_like_filters(argv):
        # f50sms -u / f50sms --search code imply the list subcommand
        argv = ["list"] + argv

    try:
        args = parser.parse_args(argv)
    except SystemExitWithMessage as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except argparse.ArgumentError as exc:  # mutually exclusive flags and friends
        print(f"{PROG}: argument error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    out = Output(args)
    settings = resolve_settings(
        host=_opt(args, "host"),
        password=_opt(args, "password"),
        timeout=_opt(args, "timeout"),
        config_file=args.config,
    )

    client = F50Client(settings.host, password=settings.password, timeout=settings.timeout)
    handler = COMMANDS[args.command]
    try:
        return handler(client, args, out)
    except F50AuthError as exc:
        # If a password is all that is missing, prompt once and retry.
        if not exc.needs_password or client.has_password or not _can_prompt():
            print(f"{PROG}: {exc}", file=sys.stderr)
            return exc.exit_code
        secret = _prompt_password()
        if not secret:
            print(f"{PROG}: cancelled.", file=sys.stderr)
            return exc.exit_code
        client.password = secret
        client.close()
        try:
            code = handler(client, args, out)
        except F50Error as retry_exc:
            print(f"{PROG}: {retry_exc}", file=sys.stderr)
            return retry_exc.exit_code
        out.note(
            "tip: run "
            f"\"{PROG} config set --password\" to save the password for next time."
        )
        return code
    except F50Error as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover - downstream closed the pipe
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
