"""`f50sms` 命令行入口。

设计原则：

* 只读命令默认不需要密码——F50 固件的 `cmd=...` 接口只校验 `Referer`；
* 输出默认为「人看着舒服」，重定向到管道时自动退化成纯文本；
* 任何数据格式（json/csv/…）都只输出数据本身，不掺杂提示语。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import __version__
from .client import DEFAULT_HOST, F50Client
from .config import config_path, load_config, resolve_settings, save_config
from .errors import F50Error, F50NotFoundError
from .labels import classify_number, describe_number, normalize_number
from .models import TAG_ALIASES, TAG_LABELS, Message
from . import pager
from .render import (
    EXPORTERS,
    Cell,
    Palette,
    pad,
    render_message_card,
    render_message_table,
    render_summary,
    render_table,
    render_thread,
    render_thread_table,
    sender_label,
    summarize,
    terminal_width,
    text_width,
    to_json,
    truncate,
)

PROG = "f50sms"

LIST_FORMATS = ("table", "plain", "json", "jsonl", "csv", "markdown")
SHOW_FORMATS = ("card", "plain", "json")
THREAD_FORMATS = ("chat", "table", "plain", "json", "jsonl", "csv", "markdown")
THREADS_FORMATS = ("table", "json", "csv", "markdown")
STATUS_FORMATS = ("table", "json")

DATA_FORMATS = {"json", "jsonl", "ndjson", "csv", "markdown", "md", "txt", "text"}


# ---------------------------------------------------------------------------
# argparse 脚手架
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """让错误信息走我们自己的退出码。"""

    def error(self, message: str):  # pragma: no cover - argparse 内部调用
        self.print_usage(sys.stderr)
        raise SystemExitWithMessage(2, f"{PROG}: 参数错误：{message}")


class SystemExitWithMessage(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", metavar="URL", help="路由器后台地址（默认 http://192.168.0.1）")
    common.add_argument("--password", metavar="PW", help="后台密码（只读通常不需要）")
    common.add_argument("--timeout", type=float, metavar="SEC", help="请求超时秒数（默认 10）")
    common.add_argument("--config", metavar="FILE", help="配置文件路径")
    common.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="彩色输出策略（默认 auto：仅终端）",
    )
    common.add_argument("--width", type=int, metavar="N", help="输出宽度，默认取终端宽度")
    _add_pager_options(common)
    common.add_argument("-q", "--quiet", action="store_true", help="不输出汇总行等提示信息")
    common.add_argument("-v", "--verbose", action="store_true", help="输出调试信息到 stderr")

    config_common = argparse.ArgumentParser(add_help=False)
    config_common.add_argument("--config", metavar="FILE", help="配置文件路径")
    config_common.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto",
        help="彩色输出策略（默认 auto：仅终端）",
    )
    config_common.add_argument("--width", type=int, metavar="N", help="输出宽度，默认取终端宽度")
    _add_pager_options(config_common)
    config_common.add_argument("-q", "--quiet", action="store_true", help="不输出汇总行等提示信息")
    config_common.add_argument("-v", "--verbose", action="store_true", help="输出调试信息到 stderr")

    parser = _Parser(
        prog=PROG,
        description="中兴 F50 随身 WiFi 短信命令行工具（只读）",
        epilog=(
            "示例：\n"
            f"  {PROG} list -u                  # 只看未读\n"
            f"  {PROG} list --search 验证码      # 搜索正文\n"
            f"  {PROG} show 57                  # 查看某条详情\n"
            f"  {PROG} thread 10010             # 查看与 10010 的会话\n"
            f"  {PROG} export -f csv -o sms.csv  # 导出全部为 CSV\n"
            f"  {PROG} watch                   # 实时监听新短信\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    # list -----------------------------------------------------------------
    list_parser = subparsers.add_parser(
        "list", aliases=["ls"], parents=[common], help="列出短信（默认命令）",
        description="列出短信，可按未读/号码/关键字/时间过滤。",
    )
    _add_filter_options(list_parser)
    _add_format_option(list_parser, LIST_FORMATS, default=None)

    # show -----------------------------------------------------------------
    show_parser = subparsers.add_parser(
        "show", parents=[common], help="查看单条短信详情",
        description="按 ID 查看短信详情；也可以用 --from 取某个号码的最新一条。",
    )
    show_parser.add_argument("id", nargs="?", help="短信 ID（list 命令里的 ID 列）")
    show_parser.add_argument("--from", dest="from_number", metavar="号码", help="取该号码的最新一条")
    _add_format_option(show_parser, SHOW_FORMATS, default="card")

    # thread ---------------------------------------------------------------
    thread_parser = subparsers.add_parser(
        "thread", aliases=["chat"], parents=[common], help="查看与某个号码的完整会话",
        description="按时间正序列出与某个号码的全部来往短信。",
    )
    thread_parser.add_argument("number", help="号码（可只写后 8 位）")
    _add_format_option(thread_parser, THREAD_FORMATS, default=None)

    # threads --------------------------------------------------------------
    threads_parser = subparsers.add_parser(
        "threads", parents=[common], help="按号码汇总会话",
        description="把所有短信按号码归并，输出会话列表。",
    )
    threads_parser.add_argument("-n", "--limit", type=int, default=0, metavar="N", help="只显示前 N 个会话")
    threads_parser.add_argument("-u", "--unread", action="store_true", help="只显示含未读的会话")
    _add_format_option(threads_parser, THREADS_FORMATS, default=None)

    # count ----------------------------------------------------------------
    count_parser = subparsers.add_parser(
        "count", parents=[common], help="输出未读/总数（方便写脚本）",
        description="默认输出未读数，适合放进状态栏或脚本。",
    )
    count_parser.add_argument("-a", "--all", action="store_true", help="输出总数而不是未读数")
    count_parser.add_argument("--json", action="store_true", help="以 JSON 输出统计信息")

    # status ---------------------------------------------------------------
    status_parser = subparsers.add_parser(
        "status", parents=[common], help="设备 / SIM / 短信存储状态",
        description="显示网络、信号、号码与短信容量等信息。",
    )
    _add_format_option(status_parser, STATUS_FORMATS, default="table")

    # export ---------------------------------------------------------------
    export_parser = subparsers.add_parser(
        "export", parents=[common], help="导出短信为文件",
        description="把短信导出为 JSON / JSONL / CSV / Markdown / 纯文本。",
    )
    _add_filter_options(export_parser)
    _add_format_option(export_parser, ("json", "jsonl", "csv", "markdown", "txt"), default="json")
    export_parser.add_argument("-o", "--out", metavar="FILE", help="输出文件（默认写到 stdout）")
    export_parser.add_argument("--include-raw", action="store_true", help="JSON 中附带路由器原始字段")

    # watch ----------------------------------------------------------------
    watch_parser = subparsers.add_parser(
        "watch", parents=[common], help="轮询并实时显示新短信",
        description="持续轮询，只打印新到达的短信；Ctrl-C 退出。",
    )
    watch_parser.add_argument("-i", "--interval", type=float, default=15.0, metavar="SEC", help="轮询间隔秒数（默认 15）")
    watch_parser.add_argument("--once", action="store_true", help="只检查一次就退出（适合放 cron）")
    watch_parser.add_argument("--backlog", type=int, default=0, metavar="N", help="启动时先显示最近 N 条")

    # config ---------------------------------------------------------------
    config_parser = subparsers.add_parser(
        "config", parents=[config_common], help="查看或保存配置",
        description="配置文件默认在 ~/.config/f50-sms/config.json（权限 600）。",
    )
    config_sub = config_parser.add_subparsers(dest="config_action", metavar="动作")
    config_sub.add_parser("show", parents=[config_common], help="显示当前生效的配置")
    config_sub.add_parser("path", parents=[config_common], help="显示配置文件路径")
    set_parser = config_sub.add_parser("set", parents=[config_common], help="保存配置项")
    set_parser.add_argument("--host", metavar="URL", help="路由器地址")
    set_parser.add_argument("--password", metavar="PW", help="后台密码")
    set_parser.add_argument("--timeout", type=float, metavar="SEC", help="超时秒数")
    config_sub.add_parser("unset", parents=[config_common], help="清空配置文件")

    # doctor ---------------------------------------------------------------
    subparsers.add_parser(
        "doctor", parents=[common], help="自检：连通性、登录态、接口可用性",
        description="逐项检查网络、接口和短信功能，出问题时先跑这个。",
    )

    return parser


def _add_pager_options(parser: argparse.ArgumentParser) -> None:
    """分页选项：默认 auto，做成互斥开关，语义与 gh 一致。"""

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--pager", dest="pager", action="store_const", const="always", default="auto",
        help="强制用分页器显示（默认内容超过一屏才分页）",
    )
    group.add_argument(
        "--no-pager", dest="pager", action="store_const", const="never",
        help="关闭分页，直接输出",
    )


def _add_format_option(parser: argparse.ArgumentParser, choices: Sequence[str], default: Optional[str]) -> None:
    parser.add_argument(
        "-f", "--format", dest="format", choices=list(choices), default=default,
        help="输出格式：" + " / ".join(choices),
    )


def _add_filter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-u", "--unread", action="store_true", help="只看未读")
    parser.add_argument(
        "--tag", action="append", metavar="标签",
        help="按状态过滤：未读/已读/已发送/发送失败/草稿，可重复或用逗号分隔",
    )
    parser.add_argument("--from", dest="from_number", metavar="号码", help="只看该号码（支持后 8 位匹配）")
    parser.add_argument("-s", "--search", metavar="关键字", help="正文包含关键字（大小写不敏感）")
    parser.add_argument("--regex", metavar="正则", help="正文匹配正则表达式")
    parser.add_argument("--since", metavar="时间", help="起始时间：2026-10-01 / 2026-10-01 08:00 / 7d / 24h")
    parser.add_argument("--until", metavar="时间", help="结束时间，格式同 --since")
    parser.add_argument("-n", "--limit", type=int, default=0, metavar="N", help="最多显示 N 条（默认全部）")
    parser.add_argument("-r", "--reverse", action="store_true", help="按时间正序（旧的在前）")
    parser.add_argument("--full", action="store_true", help="内容不截断")


# ---------------------------------------------------------------------------
# 过滤 / 时间
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_time_spec(text: str, *, now: Optional[datetime] = None) -> datetime:
    """把 `2026-10-01` / `2026-10-01 08:00` / `7d` / `today` 解析成时间。"""

    raw = (text or "").strip()
    if not raw:
        raise ValueError("空的时间表达式")
    lowered = raw.lower()
    now = now or datetime.now()
    if lowered in ("today", "今天"):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if lowered in ("yesterday", "昨天"):
        day = now - timedelta(days=1)
        return day.replace(hour=0, minute=0, second=0, microsecond=0)
    match = _RELATIVE_RE.match(lowered)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        return now - timedelta(seconds=amount * _UNIT_SECONDS[unit])
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
        "%m-%d %H:%M",
        "%m-%d",
    ):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=now.year)
        return parsed
    raise ValueError(f"无法识别的时间：{text!r}")


def _naive(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment.replace(tzinfo=None)


def expand_tags(values: Optional[Sequence[str]]) -> Optional[set]:
    """把 `--tag` 参数展开成 tag 集合；`all` 或空表示不过滤。"""

    if not values:
        return None
    result: set = set()
    for value in values:
        for piece in str(value).replace("，", ",").split(","):
            token = piece.strip()
            if not token:
                continue
            if token.lower() in ("all", "全部", "*"):
                return None
            if token in TAG_LABELS:
                result.add(token)
                continue
            mapped = TAG_ALIASES.get(token.lower()) or TAG_ALIASES.get(token)
            if mapped is None:
                raise F50NotFoundError(
                    f"无法识别的状态标签：{token}",
                    hint="可用值：" + "、".join(TAG_LABELS.values()),
                )
            result.add(mapped)
    return result or None


def filter_messages(messages: Sequence[Message], args: argparse.Namespace) -> List[Message]:
    """按命令行条件过滤消息列表。"""

    selected = list(messages)
    if getattr(args, "unread", False):
        selected = [m for m in selected if m.is_unread]

    tags = expand_tags(getattr(args, "tag", None))
    if tags is not None:
        selected = [m for m in selected if m.tag in tags]

    from_number = getattr(args, "from_number", None)
    if from_number:
        target = normalize_number(from_number)
        tail = re.sub(r"\D", "", target)[-8:]
        def matches(m: Message) -> bool:
            current = re.sub(r"\D", "", m.number)
            return current.endswith(tail) if tail else False
        selected = [m for m in selected if matches(m)]

    search = getattr(args, "search", None)
    if search:
        needle = search.lower()
        selected = [
            m for m in selected
            if needle in m.content.lower() or needle in m.number.lower()
        ]

    pattern = getattr(args, "regex", None)
    if pattern:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise F50NotFoundError(f"正则表达式有误：{exc}") from exc
        selected = [m for m in selected if compiled.search(m.content or "")]

    since = getattr(args, "since", None)
    if since:
        try:
            threshold = parse_time_spec(since)
        except ValueError as exc:
            raise F50NotFoundError(str(exc)) from exc
        selected = [
            m for m in selected
            if (_naive(m.timestamp) or datetime.min) >= threshold
        ]

    until = getattr(args, "until", None)
    if until:
        try:
            threshold = parse_time_spec(until)
        except ValueError as exc:
            raise F50NotFoundError(str(exc)) from exc
        selected = [
            m for m in selected
            if (_naive(m.timestamp) or datetime.min) <= threshold
        ]

    if getattr(args, "reverse", False):
        selected = list(reversed(selected))

    limit = getattr(args, "limit", 0) or 0
    if limit > 0:
        selected = selected[:limit]
    return selected


def thread_label(items: Sequence[Message], number: str) -> str:
    """会话标题：优先已知号码名，其次出现最多的正文签名。"""

    described = describe_number(number)
    if described and described not in ("106 行业短信", "手机号"):
        return described
    counts: Dict[str, int] = {}
    for message in items:
        if message.signature:
            counts[message.signature] = counts.get(message.signature, 0) + 1
    if counts:
        best = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        return best
    return described or number


def build_threads(messages: Sequence[Message]) -> List[Dict[str, Any]]:
    """按号码归并成会话列表（新的在前）。"""

    buckets: Dict[str, List[Message]] = {}
    for message in messages:
        key = normalize_number(message.number) or message.number
        buckets.setdefault(key, []).append(message)

    threads: List[Dict[str, Any]] = []
    for key, items in buckets.items():
        items.sort(key=lambda m: m.sort_key)
        latest = items[-1]
        label = thread_label(items, latest.number)
        threads.append({
            "number": latest.number,
            "key": key,
            "label": label,
            "count": len(items),
            "unread": sum(1 for m in items if m.is_unread),
            "latest_time": latest.timestamp_text,
            "latest_content": latest.content,
            "latest_id": latest.numeric_id or 0,
            "messages": items,
        })
    threads.sort(key=lambda t: t["latest_id"], reverse=True)
    return threads


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


class Output:
    """负责颜色、分页与最终写出。

    分页策略与 `gh` 一致：默认 `auto`，只在标准输出是终端**且**内容
    超过一屏时才启动分页器；`--pager` 强制分页，`--no-pager` 关闭。
    """

    def __init__(self, args: argparse.Namespace, *, stream: Any = None) -> None:
        force: Optional[bool]
        if args.color == "always":
            force = True
        elif args.color == "never":
            force = False
        else:
            force = None
        self.stream = stream if stream is not None else sys.stdout
        self.palette = Palette.auto(self.stream, force=force)
        self.width = args.width or terminal_width()
        self.pager_mode = getattr(args, "pager", "auto")
        self.height = pager.terminal_height()
        self.quiet = args.quiet

    # -- 输出 --------------------------------------------------------------

    def emit(self, text: str) -> None:
        payload = text if text.endswith("\n") else text + "\n"
        if self._should_page(payload):
            command = pager.resolve_pager_command()
            if pager.run_pager(payload, command, stdout=self.stream):
                return
        self.stream.write(payload)

    def _should_page(self, payload: str) -> bool:
        if self.pager_mode == "never":
            return False
        if not self._is_tty():
            return False
        if self.pager_mode == "always":
            return True
        return pager.needs_paging(payload, self.height)

    def _is_tty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except Exception:  # pragma: no cover - 极端环境
            return False

    def note(self, text: str, *, stream: Any = None) -> None:
        if self.quiet:
            return
        stream = stream or sys.stderr
        stream.write(text + "\n")


def _resolve_format(args: argparse.Namespace, requested: Optional[str], human_default: str) -> str:
    if requested:
        return requested
    if sys.stdout.isatty():
        return human_default
    return "plain" if human_default in ("table", "card", "chat") else human_default


def render_messages(messages: Sequence[Message], fmt: str, out: Output, *, full: bool = False) -> str:
    if fmt == "table":
        return render_message_table(messages, palette=out.palette, width=out.width, full=full)
    if fmt == "plain":
        lines = []
        for m in messages:
            lines.append("\t".join([
                m.id,
                f"{m.timestamp_text} {m.offset_text}".strip(),
                m.status_label,
                m.number,
                sender_label(m),
                " ".join((m.content or "").split()),
            ]))
        return "\n".join(lines)
    if fmt == "json":
        return to_json(messages)
    if fmt == "jsonl":
        return EXPORTERS["jsonl"](messages)
    if fmt == "csv":
        return EXPORTERS["csv"](messages)
    if fmt == "markdown":
        return EXPORTERS["markdown"](messages)
    raise F50NotFoundError(f"未知输出格式：{fmt}")


# ---------------------------------------------------------------------------
# 各子命令
# ---------------------------------------------------------------------------


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
        if args.password:
            updates["password"] = args.password
        if args.timeout is not None:
            updates["timeout"] = args.timeout
        if not updates:
            raise F50NotFoundError("没有提供要保存的配置项。", hint="例如 f50sms config set --password 'xxx'")
        saved = save_config(updates, args.config)
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

    # 2. 登录态
    logged = client.check_login()
    if logged is True:
        lines.append(ok("登录状态      已登录"))
    elif logged is False:
        lines.append(warn("登录状态      未登录（只读命令通常仍可用）"))
        if settings.password:
            try:
                client.login()
                lines.append(ok("尝试登录      成功"))
            except F50Error as exc:
                lines.append(fail(f"尝试登录      失败：{exc}"))
        else:
            lines.append("              未提供密码，跳过登录（用 --password 或 config set --password）")
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


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    _ensure_stdio()
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        argv = ["list"]
    elif argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["list"] + argv

    try:
        args = parser.parse_args(argv)
    except SystemExitWithMessage as exc:
        print(exc.message, file=sys.stderr)
        return exc.code

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
    try:
        handler = COMMANDS[args.command]
        return handler(client, args, out)
    except F50Error as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover - 管道被提前关闭
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    finally:
        client.close()


def _opt(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    """读取可能不存在的参数（`config` 子命令没有 --host/--password/--timeout）。"""

    return getattr(args, name, default)


def _ensure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    # Windows 控制台以外的环境一般无需额外处理。


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
