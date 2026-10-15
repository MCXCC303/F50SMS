"""Command handlers for every f50sms subcommand.

Each handler takes the connected client, the parsed namespace, and an Output,
and returns a process exit code. Parsing lives in cli.py; shared selection and
rendering helpers live in query.py and output.py.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .constants import PROG, prompt_password as _prompt_password
from .client import F50Client
from .config import config_path, load_config, resolve_settings, save_config
from .errors import F50Error, F50NotFoundError
from .labels import normalize_number
from .models import Message, resolve_ref
from .notifier import notification_argv, send as send_notification
from .output import DATA_FORMATS, Output, render_messages, resolve_format as _resolve_format
from .query import (
    build_threads,
    filter_messages,
    message_moment,
    parse_time_spec,
    router_now,
)
from .render import (
    EXPORTERS,
    pad,
    render_message_detail,
    render_stat_block,
    render_thread,
    render_thread_table,
    sender_label,
    summarize,
    text_width,
    to_json,
    truncate,
)

# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _opt(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    """Read a possibly-absent option (the config command has no --host etc.)."""

    return getattr(args, name, default)

def _time_style(args: argparse.Namespace) -> str:
    """List output shows relative times by default, like the GitHub CLI."""

    return getattr(args, "time", None) or "relative"


def _load_messages(client: F50Client, args: argparse.Namespace) -> List[Message]:
    return client.fetch_messages()


def cmd_list(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    messages = filter_messages(_load_messages(client, args), args)
    fmt = _resolve_format(args, args.format, "table")
    if not messages:
        if fmt in DATA_FORMATS:
            out.emit("[]" if fmt == "json" else "")
        else:
            out.note("no messages matched.")
        return 0
    body = render_messages(
        messages, fmt, out, full=args.full, time_style=_time_style(args),
    )
    if args.stat and fmt == "table":
        # Table view only: the block would look like a data row in plain/csv.
        body = render_stat_block(summarize(messages)) + "\n\n" + body
    out.emit(body)
    return 0


def cmd_view(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    """View one message: by reference, by sender, or the latest one."""

    messages = _load_messages(client, args)
    target: Optional[Message] = None
    if not args.id and not args.from_number:
        # Like gh, an unqualified view falls back to the most relevant item.
        if not messages:
            raise F50NotFoundError("the mailbox is empty.")
        ordered = sorted(messages, key=lambda m: m.sort_key)
        unread = [m for m in ordered if m.is_unread]
        target = (unread or ordered)[-1]
    elif args.id:
        hits = resolve_ref(messages, str(args.id))
        if not hits:
            raise F50NotFoundError(
                f"no message matching {args.id}.",
                hint=f"run {PROG} list to see the available ids.",
            )
        if len(hits) > 1:
            candidates = ", ".join(m.short_id for m in hits[:8])
            raise F50NotFoundError(
                f"{args.id} is ambiguous: it matches {len(hits)} messages ({candidates}).",
                hint="use more characters of the id to disambiguate.",
            )
        target = hits[0]
    else:
        tail = re.sub(r"\D", "", args.from_number)[-8:]
        candidates = [m for m in messages if re.sub(r"\D", "", m.number).endswith(tail)]
        if not candidates:
            raise F50NotFoundError(f"no messages from or to {args.from_number}.")
        candidates.sort(key=lambda m: m.sort_key)
        target = candidates[-1]

    fmt = args.format
    if fmt == "json":
        out.emit(to_json([target]))
    elif fmt == "plain":
        out.emit(messages_plain([target]))
    else:
        out.emit(render_message_detail(target))

    # Marking happens after rendering so the STATE line reports the state we
    # actually read, not the state we just caused.
    if target.is_unread and getattr(args, "set_as_read", False):
        _mark_read(client, target, out)
    return 0


def _mark_read(client: F50Client, message: Message, out: Output) -> bool:
    """Ask the router to mark a message read and confirm it actually happened.

    The write answers with an empty body, so the message tag afterwards is the
    only trustworthy signal.
    """

    try:
        client.mark_read([message.id])
    except F50Error as exc:
        out.note(f"could not mark {message.short_id} as read: {exc}")
        return False

    state = client.message_is_read(message.id)
    if state is True:
        out.note(f"marked {message.short_id} as read")
        return True
    if state is False:
        out.note(f"{message.short_id} is still unread on the router")
        return False
    out.note(f"asked the router to mark {message.short_id} as read, could not confirm")
    return False


def messages_plain(messages: Sequence[Message]) -> str:
    lines = []
    for m in messages:
        lines.append("\t".join([
            m.short_id, f"{m.timestamp_text} {m.offset_text}".strip(), m.status_label,
            m.number, sender_label(m), " ".join((m.content or "").split()),
        ]))
    return "\n".join(lines)


def cmd_thread_view(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    messages = _load_messages(client, args)
    tail = re.sub(r"\D", "", args.number)[-8:]
    if not tail:
        raise F50NotFoundError(f"not a usable number: {args.number!r}")
    # Trailing digits can collide, so prefer an exact match first.
    exact = [m for m in messages if normalize_number(m.number) == normalize_number(args.number)]
    selected = exact or [m for m in messages if re.sub(r"\D", "", m.number).endswith(tail)]
    if not selected:
        raise F50NotFoundError(f"no messages with {args.number}.")
    selected.sort(key=lambda m: m.sort_key)

    fmt = _resolve_format(args, args.format, "chat")
    if fmt == "chat":
        out.emit(render_thread(selected, palette=out.palette, width=out.width))
    elif fmt in ("table",):
        out.emit(render_message_table(selected, palette=out.palette, full=True))
    else:
        out.emit(render_messages(selected, fmt, out, full=True))
    return 0


def cmd_thread_list(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    messages = _load_messages(client, args)
    threads = build_threads(messages)
    if args.unread:
        threads = [t for t in threads if t["unread"]]
    if args.limit and args.limit > 0:
        threads = threads[: args.limit]

    fmt = _resolve_format(args, args.format, "table")
    if fmt == "json":
        payload = [
            {k: v for k, v in t.items() if k != "messages"} | {"latest_id": t["latest_id"]}
            for t in threads
        ]
        out.emit(json.dumps(payload, ensure_ascii=False, indent=2))
    elif fmt == "csv":
        import csv as _csv
        import io as _io
        buffer = _io.StringIO()
        writer = _csv.writer(buffer, lineterminator="\n")
        writer.writerow(["number", "label", "count", "unread", "latest_time", "latest_content"])
        for t in threads:
            writer.writerow([t["number"], t["label"], t["count"], t["unread"],
                             t["latest_time"], t["latest_content"]])
        out.emit(buffer.getvalue().rstrip("\n"))
    elif fmt == "markdown":
        lines = ["| Number | Source | Count | Unread | Last Time |", "| --- | --- | --- | --- | --- |"]
        for t in threads:
            lines.append(f"| {t['number']} | {t['label']} | {t['count']} | {t['unread']} | {t['latest_time']} |")
        out.emit("\n".join(lines))
    else:
        if not threads:
            out.note("no conversations.")
            return 0
        out.emit(render_thread_table(threads, palette=out.palette))
        if not args.quiet:
            out.note(f"{len(threads)} conversations")
    return 0


def cmd_count(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    messages = _load_messages(client, args)
    stats = summarize(messages)
    if args.json:
        out.emit(json.dumps(stats, ensure_ascii=False))
        return 0
    out.emit(str(stats["messages"] if args.all else stats["unread"]))
    return 0


def cmd_status(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    status = client.fetch_status()
    capacity = client.fetch_capacity()
    unread = client.fetch_unread_count()

    if args.format == "json":
        payload = dict(status)
        payload.pop("raw", None)
        payload["sms"] = {
            "unread": unread,
            "total": capacity["nv_used"] + capacity["sim_used"],
            "capacity": capacity["nv_total"] + capacity["sim_total"],
            "device_used": capacity["nv_used"],
            "device_capacity": capacity["nv_total"],
        }
        out.emit(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    palette = out.palette
    signal = status.get("signalbar") or "-"
    rows = [
        ("Modem", status.get("modem_main_state") or "-"),
        ("Network", f"{status.get('network_type') or '-'} · {status.get('network_provider') or '-'}"),
        ("Signal", f"{signal}/5" + (f" · RSRP {status['rsrp']} dBm" if status.get("rsrp") else "")),
        ("Band", status.get("band") or "-"),
        ("Link", status.get("ppp_status") or "-"),
        ("WAN IP", status.get("wan_ip") or "-"),
        ("Number", status.get("phone_number") or "-"),
        ("ICCID", status.get("iccid") or "-"),
        ("IMEI", status.get("imei") or "-"),
        ("Firmware", f"{status.get('firmware') or '-'} / {status.get('hardware') or '-'}"),
        ("Unread", palette.style(str(unread), "bold", "bright_yellow") if unread else "0"),
        ("SMS store", f"{capacity['nv_used']}/{capacity['nv_total']} (device)"
                      + (f" · {capacity['sim_used']}/{capacity['sim_total']} (SIM)" if capacity["sim_total"] else "")),
    ]
    label_width = max(text_width(label) for label, _ in rows)
    lines = [
        pad(label, label_width) + "  " + value
        for label, value in rows
    ]
    out.emit("\n".join(lines))
    return 0


def cmd_export(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    messages = filter_messages(_load_messages(client, args), args)
    fmt = args.format
    if fmt == "json":
        text = to_json(messages, include_raw=args.include_raw)
    elif fmt == "jsonl":
        text = EXPORTERS["jsonl"](messages, include_raw=args.include_raw)
    elif fmt == "csv":
        text = EXPORTERS["csv"](messages)
    elif fmt == "markdown":
        text = EXPORTERS["markdown"](messages)
    else:
        text = EXPORTERS["txt"](messages)

    if args.out:
        path = Path(args.out).expanduser()
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
        out.note(f"exported {len(messages)} messages to {path}")
    else:
        out.emit(text)
    return 0


def cmd_watch(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    interval = max(2.0, float(args.interval))
    known = {m.id for m in client.fetch_messages()}
    last_id = max((m.numeric_id or 0 for m in client.fetch_messages()), default=0)

    if args.backlog > 0:
        backlog = [m for m in client.fetch_messages()][: args.backlog]
        for message in reversed(backlog):
            out.emit(render_message_detail(message))
            out.emit("")
    out.note(f"watching for new messages (every {interval:g}s, Ctrl-C to stop)...")

    while True:
        time.sleep(interval)
        try:
            messages = client.fetch_messages()
        except F50Error as exc:
            out.note(f"read failed: {exc}")
            continue
        fresh = [m for m in messages if m.id not in known]
        if fresh:
            fresh.sort(key=lambda m: m.sort_key)
            for message in fresh:
                known.add(message.id)
                out.emit(render_message_detail(message))
                out.emit("")
            last_id = max([last_id] + [m.numeric_id or 0 for m in fresh])
        if args.once:
            return 0


def cmd_notify(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    """Watch for new messages and raise a desktop notification for each."""

    interval = max(2.0, float(args.interval))
    messages = _load_messages(client, args)

    # Identity is the content hash, not the router's message id: those ids are
    # dense slot numbers that get reused once a message is deleted, so a daemon
    # keyed on them would silently skip a recycled one.
    cutoff = None
    if args.since:
        cutoff = parse_time_spec(args.since, now=router_now(messages))
    notified = {
        message.digest
        for message in messages
        if cutoff is None or not _reached(message, cutoff)
    }
    out.note(f"watching for new messages to notify (every {interval:g}s, Ctrl-C to stop)...")

    while True:
        _notify_new(messages, notified, args, out)
        if args.once:
            return 0
        time.sleep(interval)
        try:
            messages = _load_messages(client, args)
        except F50Error as exc:
            out.note(f"read failed: {exc}")


def _notify_new(
    messages: Sequence[Message],
    notified: set,
    args: argparse.Namespace,
    out: Output,
) -> List[Message]:
    """Notify for messages not yet announced, and record them as announced.

    Membership is by content hash rather than arrival order or timestamp: the
    router can hand over a message stamped before ones already seen, because
    the timestamp says when it was sent and not when it reached us, and that
    message still deserves a notification.
    """

    fresh = [message for message in messages if message.digest not in notified]
    fresh.sort(key=lambda message: message.sort_key)
    for message in fresh:
        notified.add(message.digest)
        _raise_notification(message, args, out)
    return fresh


def _reached(message: Message, cutoff: datetime) -> bool:
    """Whether a message is at or after a cutoff on the router's clock."""

    moment = message_moment(message)
    return moment is not None and moment >= cutoff


def _raise_notification(message: Message, args: argparse.Namespace, out: Output) -> bool:
    """Show one notification, or print the command under --dry-run."""

    argv = notification_argv(message, program=args.notify_send)
    if args.dry_run:
        out.emit(" ".join(shlex.quote(part) for part in argv))
        return True
    problem = send_notification(argv)
    if problem is None:
        out.note(f"notified {message.short_id} ({sender_label(message)})")
        return True
    out.note(f"could not notify for {message.short_id}: {problem}")
    return False


def cmd_config(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    action = args.config_action or "show"
    path = config_path(args.config)
    if action == "path":
        out.emit(str(path))
        return 0
    if action == "unset":
        if path.exists():
            path.unlink()
            out.note(f"deleted {path}")
        else:
            out.note("no config file to delete.")
        return 0
    if action == "set":
        updates = {}
        if args.host:
            updates["host"] = args.host
        if args.timeout is not None:
            updates["timeout"] = args.timeout
        if not updates:
            raise F50NotFoundError(
                "nothing to save.",
                hint=f"for example: {PROG} config set --host http://192.168.0.1",
            )
        saved = _write_config(updates, args.config, path)
        out.note(f"wrote {saved}")
        for key, value in updates.items():
            out.note(f"  {key} = {value}")
        return 0

    # show
    settings = resolve_settings(
        host=_opt(args, "host"),
        password=_opt(args, "password"),
        timeout=_opt(args, "timeout"),
        set_as_read=_opt(args, "set_as_read"),
        config_file=args.config,
    )
    stored = load_config(args.config)
    rows = [
        ("Config file", str(path) + ("" if path.exists() else " (not present)")),
        ("host", f"{settings.host}    ← {settings.sources.get('host', '')}"),
        ("password", ("******" if settings.password else "(not set)") + f"    ← {settings.sources.get('password', '')}"),
        ("timeout", f"{settings.timeout:g}s    ← {settings.sources.get('timeout', '')}"),
        ("set_as_read", f"{str(settings.set_as_read).lower()}    ← {settings.sources.get('set_as_read', '')}"),
    ]
    if stored:
        rows.append(("Keys in file", ", ".join(sorted(stored))))
    label_width = max(text_width(label) for label, _ in rows)
    out.emit("\n".join(pad(label, label_width) + "  " + value for label, value in rows))
    return 0


def _write_config(updates: Dict[str, Any], config_file: Optional[str], path: Path) -> Path:
    """Write settings, turning a filesystem error into a friendly message."""

    try:
        return save_config(updates, config_file)
    except OSError as exc:
        raise F50NotFoundError(
            f"cannot write the config file {path}: {exc.strerror or exc}",
            hint="choose a writable path (--config), or use the environment variables instead.",
        ) from exc


def cmd_auth_login(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    """Store the router password after verifying it actually works."""

    path = config_path(args.config)
    secret = args.password
    if not secret:
        secret = _prompt_password("Router password: ")
    if not secret:
        raise F50NotFoundError("no password entered; cancelled.")

    # Verify before saving so a typo does not get persisted.
    client.password = secret
    client.close()
    client.login()
    _write_config({"password": secret}, args.config, path)
    out.note(f"saved the password to {path}")
    out.note("the session is live; later runs will reuse it until it expires.")
    return 0


def cmd_auth_status(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    """Report where the password comes from and whether a session is live."""

    settings = resolve_settings(
        host=_opt(args, "host"),
        password=_opt(args, "password"),
        timeout=_opt(args, "timeout"),
        config_file=args.config,
    )
    path = config_path(args.config)
    stored = load_config(args.config)
    session = client.check_login()

    rows = [
        ("Config file", str(path) + ("" if path.exists() else " (not present)")),
        ("Host", settings.host),
        ("Password", ("stored in " + str(path)) if stored.get("password")
            else ("from " + settings.sources.get("password", "nowhere")) if settings.password
            else "not set"),
        ("Session", {True: "signed in", False: "not signed in"}.get(session, "unknown")),
    ]
    label_width = max(text_width(label) for label, _ in rows)
    out.emit(chr(10).join(pad(label, label_width) + "  " + value for label, value in rows))
    if not settings.password and session is not True:
        out.note(f"run {PROG} auth login to store a password.")
    return 0


def cmd_auth_logout(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    """Forget the stored password, leaving other settings alone."""

    path = config_path(args.config)
    stored = load_config(args.config)
    if not stored.get("password"):
        out.note("no stored password to remove.")
        return 0
    del stored["password"]
    try:
        if stored:
            save_config(stored, args.config, replace=True)
        else:
            # Nothing left to keep, so drop the file entirely.
            path.unlink(missing_ok=True)
    except OSError as exc:
        raise F50NotFoundError(
            f"cannot update the config file {path}: {exc.strerror or exc}",
        ) from exc
    out.note(f"removed the stored password from {path}")
    return 0


def cmd_doctor(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    palette = out.palette
    ok = lambda text: palette.green("✔ " + text)      # noqa: E731
    warn = lambda text: palette.yellow("! " + text)   # noqa: E731
    fail = lambda text: palette.red("✘ " + text)      # noqa: E731

    lines: List[str] = [f"Target        {client.host}"]
    settings = resolve_settings(
        host=_opt(args, "host"),
        password=_opt(args, "password"),
        timeout=_opt(args, "timeout"),
        config_file=args.config,
    )
    lines.append(f"Config file   {settings.config_file}")

    # 1. reachability
    try:
        elapsed = client.ping()
        lines.append(ok(f"API reachable  cmd=RD round trip {elapsed * 1000:.0f} ms"))
    except F50Error as exc:
        lines.append(fail(f"API unreachable {exc}"))
        out.emit("\n".join(lines))
        return 1

    # 2. Session state. Reading SMS needs one too, so report all three cases honestly.
    lines.append(f"Password from {settings.sources.get('password', '')}")
    logged = client.check_login()
    if logged is True:
        lines.append(ok("Signed in     session still valid, no password needed"))
    elif logged is False:
        lines.append(warn("Signed in     no, not signed in"))
        if settings.password:
            try:
                client.login()
                lines.append(ok("Auto login    success"))
            except F50Error as exc:
                lines.append(fail(f"Auto login    failed: {exc}"))
        else:
            lines.append(fail("Auto login    no password available"))
            lines.append("              supply it with --password, F50_SMS_PASSWORD or "
                         f"{PROG} auth login")
    else:
        lines.append(warn("Signed in     unknown (loginfo gave no answer)"))

    # 3. SMS features
    try:
        flags = client.fetch_flags()
        lines.append(ok(f"SMS ready     modem={flags.get('modem_main_state') or '?'}"))
    except F50Error as exc:
        lines.append(fail(f"SMS ready     read failed: {exc}"))

    try:
        capacity = client.fetch_capacity()
        lines.append(ok(f"SMS store     device {capacity['nv_used']}/{capacity['nv_total']}"
                        f", SIM {capacity['sim_used']}/{capacity['sim_total']}"))
    except F50Error as exc:
        lines.append(fail(f"SMS store     read failed: {exc}"))

    try:
        messages = client.fetch_messages()
        stats = summarize(messages)
        lines.append(ok(f"SMS read      {stats['messages']} messages, {stats['unread']} unread, "
                        f"{stats['threads']} conversations"))
        if messages:
            newest = messages[0]
            lines.append(f"              latest: #{newest.id} {newest.timestamp_text} "
                         f"{newest.number} {truncate(newest.content, 30)}")
    except F50Error as exc:
        lines.append(fail(f"SMS read      failed: {exc}"))

    out.emit("\n".join(lines))
    return 0


#: Handler registry. Nested commands are keyed "group action", mirroring the
#: command layout of the GitHub CLI.
COMMANDS = {
    "list": cmd_list,
    "view": cmd_view,
    "thread list": cmd_thread_list,
    "thread view": cmd_thread_view,
    "count": cmd_count,
    "status": cmd_status,
    "export": cmd_export,
    "watch": cmd_watch,
    "notify": cmd_notify,
    "auth login": cmd_auth_login,
    "auth status": cmd_auth_status,
    "auth logout": cmd_auth_logout,
    "config show": cmd_config,
    "config path": cmd_config,
    "config set": cmd_config,
    "config unset": cmd_config,
    "doctor": cmd_doctor,
}

#: Single-word aliases, each mapping onto the first token of a real command.
COMMAND_ALIASES = {
    "ls": "list",
    "show": "view",
    "chat": "thread",
    "threads": "thread",
}

#: Command groups whose second word selects an action.
COMMAND_GROUPS = ("thread", "auth", "config")

#: Actions available inside each group.
COMMAND_ACTIONS = {
    "thread": ("list", "view"),
    "auth": ("login", "status", "logout"),
    "config": ("show", "path", "set", "unset"),
}

#: Action assumed when a group is invoked without one.
COMMAND_DEFAULT_ACTION = {
    "thread": "list",
    "auth": "status",
    "config": "show",
}

#: Every word accepted as the first argument on the command line.
TOP_LEVEL_COMMANDS = frozenset(
    {key for key in COMMANDS if " " not in key}
    | set(COMMAND_GROUPS)
    | set(COMMAND_ALIASES)
)