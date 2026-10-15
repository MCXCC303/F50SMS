"""Terminal output: colour, paging, and format dispatch.

Separated from the command layer so paging/colour behaviour can be unit tested
without touching the router. The paging policy mirrors gh: auto by default, only
handing output to the pager when stdout is a terminal AND the content is taller
than one screen.
"""

from __future__ import annotations

import sys
from typing import Any, Optional, Sequence

from . import pager
from .errors import F50NotFoundError
from .models import Message
from .render import (
    EXPORTERS,
    Palette,
    render_message_table,
    sender_label,
    terminal_width,
    to_json,
)

#: Formats that emit pure data, so no human summary is appended.
DATA_FORMATS = {"json", "jsonl", "ndjson", "csv", "markdown", "md", "txt", "text"}


class Output:
    """Colour, paging, and the final write to a stream."""

    def __init__(self, args: Any, *, stream: Any = None) -> None:
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
        except Exception:  # pragma: no cover - defensive
            return False

    def note(self, text: str, *, stream: Any = None) -> None:
        if self.quiet:
            return
        stream = stream or sys.stderr
        stream.write(text + "\n")


def resolve_format(args: Any, requested: Optional[str], human_default: str) -> str:
    """Fall back to a plain, pipe-friendly format when not attached to a terminal."""

    if requested:
        return requested
    if sys.stdout.isatty():
        return human_default
    return "plain" if human_default in ("table", "card", "chat") else human_default


def render_messages(messages: Sequence[Message], fmt: str, out: Output, *, full: bool = False) -> str:
    """Render a list of messages in the requested format."""

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
    raise F50NotFoundError(f"unknown output format: {fmt}")
