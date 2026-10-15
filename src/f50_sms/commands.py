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
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .client import F50Client
from .config import config_path, load_config, resolve_settings, save_config
from .errors import F50Error, F50NotFoundError
from .labels import normalize_number
from .models import Message
from .output import DATA_FORMATS, Output, render_messages, resolve_format as _resolve_format
from .query import build_threads, filter_messages
from .render import (
    EXPORTERS,
    pad,
    render_message_card,
    render_summary,
    render_thread,
    render_thread_table,
    sender_label,
    summarize,
    text_width,
    to_json,
    truncate,
)

# ---------------------------------------------------------------------------
# 各子命令
# ---------------------------------------------------------------------------


def _opt(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    """Read a possibly-absent option (the config command has no --host etc.)."""

    return getattr(args, name, default)

def _load_messages(client: F50Client, args: argparse.Namespace) -> List[Message]:
    return client.fetch_messages()


def cmd_list(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    messages = filter_messages(_load_messages(client, args), args)
    fmt = _resolve_format(args, args.format, "table")
    if not messages:
        if fmt in DATA_FORMATS:
            out.emit("[]" if fmt == "json" else "")
        else:
            out.note("没有符合条件的短信。")
        return 0
    out.emit(render_messages(messages, fmt, out, full=args.full))
    if fmt not in DATA_FORMATS and not args.quiet:
        out.note(render_summary(summarize(messages), palette=out.palette))
    return 0


def cmd_show(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    if not args.id and not args.from_number:
        raise F50NotFoundError("请给出短信 ID，或用 --from 指定号码。", hint="例如 f50sms show 57")
    messages = _load_messages(client, args)
    target: Optional[Message] = None
    if args.id:
        wanted = str(args.id).strip().lstrip("#")
        for message in messages:
            if message.id == wanted:
                target = message
                break
        if target is None:
            raise F50NotFoundError(
                f"没有找到 ID 为 {args.id} 的短信。",
                hint="用 f50sms list 查看可用的 ID。",
            )
    else:
        tail = re.sub(r"\D", "", args.from_number)[-8:]
        candidates = [m for m in messages if re.sub(r"\D", "", m.number).endswith(tail)]
        if not candidates:
            raise F50NotFoundError(f"没有找到与 {args.from_number} 相关的短信。")
        candidates.sort(key=lambda m: m.sort_key)
        target = candidates[-1]

    fmt = args.format
    if fmt == "json":
        out.emit(to_json([target]))
    elif fmt == "plain":
        out.emit(messages_plain([target]))
    else:
        out.emit(render_message_card(target, palette=out.palette, width=out.width))
    return 0


def messages_plain(messages: Sequence[Message]) -> str:
    lines = []
    for m in messages:
        lines.append("\t".join([
            m.id, f"{m.timestamp_text} {m.offset_text}".strip(), m.status_label,
            m.number, sender_label(m), " ".join((m.content or "").split()),
        ]))
    return "\n".join(lines)


def cmd_thread(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    messages = _load_messages(client, args)
    tail = re.sub(r"\D", "", args.number)[-8:]
    if not tail:
        raise F50NotFoundError(f"无效的号码：{args.number!r}")
    # 后 8 位可能撞号，优先精确匹配。
    exact = [m for m in messages if normalize_number(m.number) == normalize_number(args.number)]
    selected = exact or [m for m in messages if re.sub(r"\D", "", m.number).endswith(tail)]
    if not selected:
        raise F50NotFoundError(f"没有与 {args.number} 相关的短信。")
    selected.sort(key=lambda m: m.sort_key)

    fmt = _resolve_format(args, args.format, "chat")
    if fmt == "chat":
        out.emit(render_thread(selected, palette=out.palette, width=out.width))
    elif fmt in ("table",):
        out.emit(render_message_table(selected, palette=out.palette, width=out.width, full=True))
    else:
        out.emit(render_messages(selected, fmt, out, full=True))
    return 0


def cmd_threads(client: F50Client, args: argparse.Namespace, out: Output) -> int:
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
        lines = ["| 号码 | 来源 | 条数 | 未读 | 最近时间 |", "| --- | --- | --- | --- | --- |"]
        for t in threads:
            lines.append(f"| {t['number']} | {t['label']} | {t['count']} | {t['unread']} | {t['latest_time']} |")
        out.emit("\n".join(lines))
    else:
        if not threads:
            out.note("没有会话。")
            return 0
        out.emit(render_thread_table(threads, palette=out.palette, width=out.width))
        if not args.quiet:
            out.note(f"共 {len(threads)} 个会话")
    return 0


def cmd_count(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    messages = _load_messages(client, args)
    stats = summarize(messages)
    if args.json:
        out.emit(json.dumps(stats, ensure_ascii=False))
        return 0
    out.emit(str(stats["total"] if args.all else stats["unread"]))
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
        ("设备状态", status.get("modem_main_state") or "—"),
        ("网络", f"{status.get('network_type') or '—'} · {status.get('network_provider') or '—'}"),
        ("信号", f"{signal}/5" + (f" · RSRP {status['rsrp']} dBm" if status.get("rsrp") else "")),
        ("频段", status.get("band") or "—"),
        ("连接", status.get("ppp_status") or "—"),
        ("WAN IP", status.get("wan_ip") or "—"),
        ("本机号码", status.get("phone_number") or "—"),
        ("ICCID", status.get("iccid") or "—"),
        ("IMEI", status.get("imei") or "—"),
        ("固件", f"{status.get('firmware') or '—'} / {status.get('hardware') or '—'}"),
        ("短信未读", palette.style(str(unread), "bold", "bright_yellow") if unread else "0"),
        ("短信容量", f"{capacity['nv_used']}/{capacity['nv_total']}（设备）"
                     + (f" · {capacity['sim_used']}/{capacity['sim_total']}（SIM）" if capacity["sim_total"] else "")),
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
        out.note(f"已导出 {len(messages)} 条短信到 {path}")
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
            out.emit(render_message_card(message, palette=out.palette, width=out.width))
            out.emit("")
    out.note(f"正在监听新短信（每 {interval:g} 秒一次，Ctrl-C 退出）…")

    while True:
        time.sleep(interval)
        try:
            messages = client.fetch_messages()
        except F50Error as exc:
            out.note(f"读取失败：{exc}")
            continue
        fresh = [m for m in messages if m.id not in known]
        if fresh:
            fresh.sort(key=lambda m: m.sort_key)
            for message in fresh:
                known.add(message.id)
                out.emit(render_message_card(message, palette=out.palette, width=out.width))
                out.emit("")
            last_id = max([last_id] + [m.numeric_id or 0 for m in fresh])
        if args.once:
            return 0


def cmd_config(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    action = args.config_action or "show"
    path = config_path(args.config)
    if action == "path":
        out.emit(str(path))
        return 0
    if action == "unset":
        if path.exists():
            path.unlink()
            out.note(f"已删除 {path}")
        else:
            out.note("配置文件不存在，无需删除。")
        return 0
    if action == "set":
        updates = {}
        if args.host:
            updates["host"] = args.host
        if args.password is PROMPT_PASSWORD:
            secret = _prompt_password("要保存的后台密码: ")
            if not secret:
                raise F50NotFoundError("没有输入密码，已取消。")
            updates["password"] = secret
        elif args.password:
            updates["password"] = args.password
        if args.timeout is not None:
            updates["timeout"] = args.timeout
        if not updates:
            raise F50NotFoundError("没有提供要保存的配置项。", hint="例如 f50sms config set --password")
        try:
            saved = save_config(updates, args.config)
        except OSError as exc:
            raise F50NotFoundError(
                f"无法写入配置文件 {path}：{exc.strerror or exc}",
                hint="换一个可写路径（--config），或者改用环境变量 F50_SMS_PASSWORD。",
            ) from exc
        out.note(f"已写入 {saved}")
        for key, value in updates.items():
            shown = "******" if key == "password" else value
            out.note(f"  {key} = {shown}")
        return 0

    # show
    settings = resolve_settings(
        host=_opt(args, "host"),
        password=_opt(args, "password"),
        timeout=_opt(args, "timeout"),
        config_file=args.config,
    )
    stored = load_config(args.config)
    rows = [
        ("配置文件", str(path) + ("" if path.exists() else "（不存在）")),
        ("host", f"{settings.host}    ← {settings.sources.get('host', '')}"),
        ("password", ("******" if settings.password else "（未设置）") + f"    ← {settings.sources.get('password', '')}"),
        ("timeout", f"{settings.timeout:g}s    ← {settings.sources.get('timeout', '')}"),
    ]
    if stored:
        rows.append(("文件内容", ", ".join(sorted(stored))))
    label_width = max(text_width(label) for label, _ in rows)
    out.emit("\n".join(pad(label, label_width) + "  " + value for label, value in rows))
    return 0


def cmd_doctor(client: F50Client, args: argparse.Namespace, out: Output) -> int:
    palette = out.palette
    ok = lambda text: palette.green("✔ " + text)      # noqa: E731
    warn = lambda text: palette.yellow("! " + text)   # noqa: E731
    fail = lambda text: palette.red("✘ " + text)      # noqa: E731

    lines: List[str] = [f"目标地址      {client.host}"]
    settings = resolve_settings(
        host=_opt(args, "host"),
        password=_opt(args, "password"),
        timeout=_opt(args, "timeout"),
        config_file=args.config,
    )
    lines.append(f"配置文件      {settings.config_file}")

    # 1. 连通性
    try:
        elapsed = client.ping()
        lines.append(ok(f"接口连通      cmd=RD 往返 {elapsed * 1000:.0f} ms"))
    except F50Error as exc:
        lines.append(fail(f"接口不可达    {exc}"))
        out.emit("\n".join(lines))
        return 1

    # 2. 登录态。读取短信同样需要会话，这里如实区分三种情况。
    lines.append(f"密码来源      {settings.sources.get('password', '')}")
    logged = client.check_login()
    if logged is True:
        lines.append(ok("登录状态      已登录（会话仍有效，本次无需密码）"))
    elif logged is False:
        lines.append(warn("登录状态      未登录"))
        if settings.password:
            try:
                client.login()
                lines.append(ok("自动登录      成功"))
            except F50Error as exc:
                lines.append(fail(f"自动登录      失败：{exc}"))
        else:
            lines.append(fail("自动登录      缺少密码"))
            lines.append("              用 --password、F50_SMS_PASSWORD 或 "
                         f"{PROG} config set --password 提供")
    else:
        lines.append(warn("登录状态      无法判断（loginfo 无响应）"))

    # 3. 短信功能
    try:
        flags = client.fetch_flags()
        lines.append(ok(f"短信就绪      modem={flags.get('modem_main_state') or '?'}"))
    except F50Error as exc:
        lines.append(fail(f"短信就绪      读取失败：{exc}"))

    try:
        capacity = client.fetch_capacity()
        lines.append(ok(f"存储容量      设备 {capacity['nv_used']}/{capacity['nv_total']}"
                        f"，SIM {capacity['sim_used']}/{capacity['sim_total']}"))
    except F50Error as exc:
        lines.append(fail(f"存储容量      读取失败：{exc}"))

    try:
        messages = client.fetch_messages()
        stats = summarize(messages)
        lines.append(ok(f"短信读取      {stats['total']} 条，未读 {stats['unread']}，"
                        f"会话 {stats['senders']}"))
        if messages:
            newest = messages[0]
            lines.append(f"              最新一条：#{newest.id} {newest.timestamp_text} "
                         f"{newest.number} {truncate(newest.content, 30)}")
    except F50Error as exc:
        lines.append(fail(f"短信读取      失败：{exc}"))

    out.emit("\n".join(lines))
    return 0


COMMANDS = {
    "list": cmd_list,
    "ls": cmd_list,
    "show": cmd_show,
    "thread": cmd_thread,
    "chat": cmd_thread,
    "threads": cmd_threads,
    "count": cmd_count,
    "status": cmd_status,
    "export": cmd_export,
    "watch": cmd_watch,
    "config": cmd_config,
    "doctor": cmd_doctor,
}