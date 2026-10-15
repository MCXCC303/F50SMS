"""终端渲染：中日韩宽度感知的表格、卡片与各种导出格式。

关键点是 **显示宽度**：中文/全角字符占两列，用 `len()` 对齐一定会歪。
这里用 :mod:`unicodedata` 的 East Asian Width 属性精确计算。
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .humanize import humanize_delta
from .labels import classify_number, describe_number
from .models import Message

# ---------------------------------------------------------------------------
# 宽度工具
# ---------------------------------------------------------------------------


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if ch in ("\u200b", "\ufeff"):
        return 0
    return 1


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，超长时追加省略号。"""

    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    ellipsis_width = text_width(ellipsis)
    budget = max(0, width - ellipsis_width)
    out: List[str] = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格。"""

    filler = max(0, width - text_width(text))
    if align == "right":
        return " " * filler + text
    if align == "center":
        left = filler // 2
        return " " * left + text + " " * (filler - left)
    return text + " " * filler


def terminal_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:  # pragma: no cover - 极端环境
        return default


# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_cyan": "96",
    "bright_white": "97",
}


class Palette:
    """极简 ANSI 颜色封装；关闭时所有方法都返回原文。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    @classmethod
    def auto(cls, stream: Optional[Any] = None, *, force: Optional[bool] = None) -> "Palette":
        if force is not None:
            return cls(force)
        if os.environ.get("NO_COLOR"):
            return cls(False)
        stream = stream if stream is not None else sys.stdout
        try:
            is_tty = bool(stream.isatty())
        except Exception:  # pragma: no cover
            is_tty = False
        if os.environ.get("TERM", "") in ("dumb", ""):
            is_tty = False
        return cls(is_tty)

    def style(self, text: str, *names: str) -> str:
        if not self.enabled or not names:
            return text
        params = ";".join(_CODES[name] for name in names if name in _CODES)
        if not params:
            return text
        return f"\x1b[{params}m{text}\x1b[0m"

    # 便捷方法
    def bold(self, text: str) -> str:
        return self.style(text, "bold")

    def dim(self, text: str) -> str:
        return self.style(text, "dim")

    def red(self, text: str) -> str:
        return self.style(text, "red")

    def green(self, text: str) -> str:
        return self.style(text, "green")

    def yellow(self, text: str) -> str:
        return self.style(text, "yellow")

    def blue(self, text: str) -> str:
        return self.style(text, "blue")

    def cyan(self, text: str) -> str:
        return self.style(text, "cyan")

    def magenta(self, text: str) -> str:
        return self.style(text, "magenta")


def status_style(message: Message, palette: Palette) -> str:
    """unread=bold yellow, read=dim, sent=green, failed=red, draft=cyan."""

    label = message.status_label
    if message.is_unread:
        return palette.style(label, "bold", "bright_yellow")
    if message.is_sent:
        return palette.green(label)
    if message.is_send_failed:
        return palette.red(label)
    if message.is_draft:
        return palette.cyan(label)
    return palette.dim(label)


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    """表格单元：`text` 用于计算宽度，`style` 是可选的颜色函数。"""

    text: str
    style: Optional[Any] = None  # Callable[[str], str]

    def render(self, padded: str) -> str:
        return self.style(padded) if self.style else padded


def _as_cell(value: Any) -> Cell:
    return value if isinstance(value, Cell) else Cell(str(value))


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    aligns: Optional[Sequence[str]] = None,
    palette: Optional[Palette] = None,
    max_widths: Optional[Sequence[Optional[int]]] = None,
    separator: str = "  ",
    header_line: bool = True,
) -> str:
    """渲染一张对齐的文本表（最后一列不补尾随空格）。"""

    palette = palette or Palette(False)
    aligns = list(aligns or ["left"] * len(headers))
    cells_rows = [[_as_cell(c) for c in row] for row in rows]

    header_widths = [text_width(h) for h in headers]
    # A column is never narrower than its own header, otherwise the header row,
    # the rule, and the data would disagree about where the column ends.
    limits: List[Optional[int]] = list(max_widths or [])
    limits += [None] * (len(header_widths) - len(limits))
    limits = [
        None if limit is None else max(limit, header_widths[i])
        for i, limit in enumerate(limits)
    ]

    widths = list(header_widths)
    for row in cells_rows:
        for index, cell in enumerate(row):
            if index >= len(widths):
                continue
            if limits[index] is not None:
                cell.text = truncate(cell.text, limits[index])
            widths[index] = max(widths[index], text_width(cell.text))
    if any(limit is not None for limit in limits):
        widths = [
            min(width, limits[i]) if limits[i] is not None else width
            for i, width in enumerate(widths)
        ]

    out: List[str] = []
    if header_line:
        last_index = len(headers) - 1
        header_cells = [
            palette.bold(h if i == last_index else pad(h, widths[i]))
            for i, h in enumerate(headers)
        ]
        out.append(separator.join(header_cells))
        out.append(separator.join(palette.dim("─" * widths[i]) for i in range(len(headers))))

    for row in cells_rows:
        parts: List[str] = []
        last = len(row) - 1
        for index, cell in enumerate(row):
            if index == last:
                parts.append(cell.render(cell.text))
            else:
                parts.append(cell.render(pad(cell.text, widths[index], aligns[index])))
        out.append(separator.join(parts))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 短信列表 / 卡片
# ---------------------------------------------------------------------------


def sender_label(message: Message) -> str:
    """来源：优先正文签名，其次号码注释，最后为空。"""

    if message.signature:
        return message.signature
    return describe_number(message.number)


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return truncate(flat, limit) if limit > 0 else flat


def render_message_table(
    messages: Sequence[Message],
    *,
    palette: Optional[Palette] = None,
    width: Optional[int] = None,
    full: bool = False,
    time_style: str = "relative",
) -> str:
    """The message list table, with a gh-style uppercase header."""

    palette = palette or Palette(False)
    total_width = width or terminal_width()

    headers = ["ID", "TIME", "SOURCE", "NUMBER", "STATE", "TEXT"]
    time_width = 19 if time_style == "absolute" else 22
    fixed = 2 + time_width + 2 + 12 + 2 + 14 + 2 + 4 + 2 + 4
    content_width = max(12, total_width - fixed)
    if full:
        content_width = 0

    rows: List[List[Any]] = []
    for message in messages:
        content = message.content if full else _clip(message.content, content_width)
        if time_style == "absolute":
            when = message.timestamp_text or message.raw_date
        else:
            when = humanize_delta(message.timestamp) or message.raw_date
        rows.append([
            Cell("#" + message.short_id, palette.dim),
            Cell(when, None),
            Cell(truncate(sender_label(message), 12), None),
            Cell(truncate(message.number, 14), None),
            Cell(message.status_label, lambda text, m=message: status_style(m, palette)),
            Cell(content, None),
        ])

    aligns = ["right", "left", "left", "left", "left", "left"]
    max_widths: List[Optional[int]] = [None, time_width, 12, 14, None, content_width or None]
    return render_table(
        headers,
        rows,
        aligns=aligns,
        palette=palette,
        max_widths=max_widths,
    )


def render_message_card(message: Message, *, palette: Optional[Palette] = None, width: Optional[int] = None) -> str:
    """单条短信的详情卡片。"""

    palette = palette or Palette(False)
    inner = max(40, min(width or terminal_width(), 100)) - 4

    rows: List[tuple[str, str]] = [
        ("Time", f"{message.timestamp_text} {message.offset_text}".strip() or message.raw_date or "—"),
    ]
    sender = sender_label(message)
    rows.append(("From" if message.is_inbox else "To",
                 f"{message.number}" + (f" · {sender}" if sender else "")))
    rows.append(("State", message.status_label))
    if message.group_id:
        rows.append(("Group", message.group_id))
    if message.received_all is False:
        rows.append(("Note", "long SMS not fully received"))

    label_width = max(text_width(label) for label, _ in rows) + 2
    title = f"#{message.short_id}" if message.id else "Message"
    dashes = max(0, inner - text_width(title) - 1)
    top = "╭─ " + palette.bold(title) + " " + "─" * dashes + "╮"
    bottom = "╰" + "─" * (inner + 2) + "╯"

    lines = [top]
    for label, value in rows:
        text = pad(label, label_width) + truncate(value, inner - label_width)
        lines.append("│ " + text + " " * max(0, inner - text_width(text)) + " │")
    lines.append("├" + "─" * (inner + 2) + "┤")
    for line in (message.content or "(empty)").splitlines() or [""]:
        for chunk in _wrap(line, inner):
            lines.append("│ " + pad(chunk, inner) + " │")
    lines.append(bottom)
    return "\n".join(lines)


def _wrap(text: str, width: int) -> List[str]:
    """按显示宽度折行（保留原有空行）。"""

    if width <= 0:
        return [text]
    if not text:
        return [""]
    lines: List[str] = []
    current = ""
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > width:
            lines.append(current)
            current, used = "", 0
        current += ch
        used += w
    lines.append(current)
    return lines


def render_thread(thread: Sequence[Message], *, palette: Optional[Palette] = None, width: Optional[int] = None) -> str:
    """按时间正序渲染一段会话。"""

    palette = palette or Palette(False)
    total = width or terminal_width()
    bubble_width = max(24, min(total - 14, 72))
    lines: List[str] = []
    for message in thread:
        who = "me" if message.is_outgoing else (sender_label(message) or message.number)
        head = f"{message.timestamp_text}  {who}"
        if message.is_unread:
            head += "  " + palette.style("unread", "bold", "bright_yellow")
        if message.is_send_failed:
            head += "  " + palette.red("send failed")
        if message.is_draft:
            head += "  " + palette.cyan("draft")
        lines.append(palette.dim(head))
        for chunk in _wrap(message.content or "", bubble_width):
            lines.append("  " + chunk)
        lines.append("")
    return "\n".join(lines).rstrip()


def render_thread_table(
    threads: Sequence[Dict[str, Any]],
    *,
    palette: Optional[Palette] = None,
    width: Optional[int] = None,
) -> str:
    palette = palette or Palette(False)
    total = width or terminal_width()
    headers = ["NUMBER", "SOURCE", "COUNT", "UNREAD", "LAST TIME", "PREVIEW"]
    fixed = 17 + 2 + 14 + 2 + 5 + 2 + 4 + 2 + 19 + 2
    preview_width = max(10, total - fixed)

    rows: List[List[Any]] = []
    for thread in threads:
        label = thread.get("label") or ""
        rows.append([
            Cell(truncate(thread["number"], 17), None),
            Cell(truncate(label, 14), None),
            Cell(str(thread["count"]), None),
            Cell(str(thread["unread"]), (lambda t: palette.style(t, "bold", "bright_yellow")) if thread["unread"] else None),
            Cell(thread["latest_time"], None),
            Cell(_clip(thread["latest_content"], preview_width), None),
        ])
    aligns = ["left", "left", "right", "right", "left", "left"]
    max_widths: List[Optional[int]] = [17, 14, None, None, 19, preview_width or None]
    return render_table(headers, rows, aligns=aligns, palette=palette, max_widths=max_widths)


# ---------------------------------------------------------------------------
# 导出格式
# ---------------------------------------------------------------------------


def to_json(messages: Sequence[Message], *, include_raw: bool = False, indent: int = 2) -> str:
    payload = [m.to_dict(include_raw=include_raw) for m in messages]
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def to_jsonl(messages: Sequence[Message], *, include_raw: bool = False) -> str:
    return "\n".join(
        json.dumps(m.to_dict(include_raw=include_raw), ensure_ascii=False) for m in messages
    )


def to_csv(messages: Sequence[Message]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["id", "time", "direction", "number", "sender", "status", "content"])
    for m in messages:
        writer.writerow([
            m.short_id,
            f"{m.timestamp_text} {m.offset_text}".strip(),
            "out" if m.is_outgoing else "in",
            m.number,
            sender_label(m),
            m.status_label,
            m.content,
        ])
    return buffer.getvalue().rstrip("\n")


def to_markdown(messages: Sequence[Message], *, title: str = "F50 SMS export") -> str:
    lines = [f"# {title}", ""]
    lines.append(f"{len(messages)} messages.")
    lines.append("")
    lines.append("| ID | Time | Direction | Number | Source | State | Text |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for m in messages:
        content = m.content.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {m.short_id} | {m.timestamp_text} | {'sent' if m.is_outgoing else 'received'} "
            f"| {m.number} | {sender_label(m)} | {m.status_label} | {content} |"
        )
    return "\n".join(lines)


def to_text(messages: Sequence[Message]) -> str:
    blocks: List[str] = []
    for m in messages:
        head = f"[{m.short_id}] {m.timestamp_text} {m.status_label} {m.number}"
        sender = sender_label(m)
        if sender:
            head += f" ({sender})"
        blocks.append(head + "\n" + (m.content or ""))
    return "\n\n".join(blocks)


EXPORTERS = {
    "json": to_json,
    "jsonl": to_jsonl,
    "ndjson": to_jsonl,
    "csv": to_csv,
    "markdown": to_markdown,
    "md": to_markdown,
    "txt": to_text,
    "text": to_text,
}


# ---------------------------------------------------------------------------
# 汇总行
# ---------------------------------------------------------------------------


def summarize(messages: Sequence[Message]) -> Dict[str, int]:
    return {
        "total": len(messages),
        "unread": sum(1 for m in messages if m.is_unread),
        "read": sum(1 for m in messages if m.is_read),
        "sent": sum(1 for m in messages if m.is_sent),
        "failed": sum(1 for m in messages if m.is_send_failed),
        "draft": sum(1 for m in messages if m.is_draft),
        "senders": len({m.number for m in messages}),
        "unknown_direction": sum(
            1 for m in messages if m.tag not in ("0", "1", "2", "3", "4")
        ),
    }


def render_summary(stats: Dict[str, int], *, palette: Optional[Palette] = None) -> str:
    palette = palette or Palette(False)
    parts = [f"{palette.bold(str(stats['total']))} messages"]
    if stats["unread"]:
        parts.append(palette.style(f"{stats['unread']} unread", "bold", "bright_yellow"))
    else:
        parts.append("0 unread")
    if stats["read"]:
        parts.append(f"{stats['read']} read")
    if stats["sent"]:
        parts.append(palette.green(f"{stats['sent']} sent"))
    if stats["failed"]:
        parts.append(palette.red(f"{stats['failed']} failed"))
    if stats["draft"]:
        parts.append(palette.cyan(f"{stats['draft']} draft"))
    parts.append(f"{stats['senders']} threads")
    return " · ".join(parts)


def classify_sender(number: str) -> str:
    return classify_number(number)
