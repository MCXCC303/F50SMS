"""Message selection: time parsing, state filters, and thread grouping.

Kept separate from the CLI layer so the selection rules can be tested and reused
without spinning up a command. All functions are pure: they take messages or
namespaces and return new collections.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from .errors import F50NotFoundError
from .expressions import apply_filter, apply_sort, parse_filter, parse_sort
from .labels import describe_number, normalize_number
from .models import TAG_ALIASES, TAG_LABELS, Message

_RELATIVE_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def message_moment(message: Message) -> Optional[datetime]:
    """The message time on the router's clock, without an offset."""

    moment = message.timestamp
    return None if moment is None else moment.replace(tzinfo=None)


def router_now(messages: Sequence[Message]) -> datetime:
    """Current wall-clock time in the router's timezone, as a naive datetime.

    The router stamps messages in its own zone and that is also how we display
    them, so any comparison against a message time has to happen in that frame.
    The host clock provides only the absolute instant; the offset is read off
    the messages, which keeps the result right whatever timezone the host uses.
    """

    offset = next(
        (
            message.timestamp.utcoffset()
            for message in messages
            if message.timestamp is not None and message.timestamp.tzinfo is not None
        ),
        None,
    )
    moment = datetime.now(timezone.utc)
    if offset is not None:
        moment = moment.astimezone(timezone(offset))
    return moment.replace(tzinfo=None)


def parse_time_spec(text: str, *, now: Optional[datetime] = None) -> datetime:
    """Parse an absolute (2026-10-01) or relative (7d, 24h, today) time expression."""

    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty time expression")
    lowered = raw.lower()
    now = now or datetime.now()
    if lowered in ("today",):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if lowered in ("yesterday",):
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
    raise ValueError(f"unrecognised time: {text!r}")


def _naive(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment.replace(tzinfo=None)


def expand_tags(values: Optional[Sequence[str]]) -> Optional[set]:
    """Expand --state values into a set of tags; "all" or empty means no filter."""

    if not values:
        return None
    result: set = set()
    for value in values:
        for piece in str(value).replace(",", ",").split(","):
            token = piece.strip()
            if not token:
                continue
            if token.lower() in ("all", "*"):
                return None
            if token in TAG_LABELS:
                result.add(token)
                continue
            mapped = TAG_ALIASES.get(token.lower()) or TAG_ALIASES.get(token)
            if mapped is None:
                raise F50NotFoundError(
                    f"unknown state: {token}",
                    hint="available states: " + ", ".join(TAG_LABELS.values()),
                )
            result.add(mapped)
    return result or None


def number_matches(message: Message, number: str) -> bool:
    """Match on the full number or its trailing digits, like the web UI does."""

    tail = re.sub(r"\D", "", number)[-8:]
    if not tail:
        return False
    return re.sub(r"\D", "", message.number).endswith(tail)


def filter_messages(messages: Sequence[Message], args: Any) -> List[Message]:
    """Apply the standard message filters read off a CLI namespace."""

    selected = list(messages)
    if getattr(args, "unread", False):
        selected = [m for m in selected if m.is_unread]

    tags = expand_tags(getattr(args, "tag", None))
    if tags is not None:
        selected = [m for m in selected if m.tag in tags]

    from_number = getattr(args, "from_number", None)
    if from_number:
        selected = [m for m in selected if number_matches(m, normalize_number(from_number))]

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
            raise F50NotFoundError(f"invalid regex: {exc}") from exc
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

    conditions = parse_filter(getattr(args, "filter", None))
    if conditions:
        selected = apply_filter(selected, conditions)

    terms = parse_sort(getattr(args, "sort", None))
    if terms:
        selected = apply_sort(selected, terms)

    if getattr(args, "reverse", False):
        selected = list(reversed(selected))

    limit = getattr(args, "limit", 0) or 0
    if limit > 0:
        selected = selected[:limit]
    return selected


def thread_label(items: Sequence[Message], number: str) -> str:
    """Title for a conversation: known short-code name first, else most common signature."""

    described = describe_number(number)
    if described and described not in ("enterprise SMS", "mobile number", "international number"):
        return described
    counts: Dict[str, int] = {}
    for message in items:
        if message.signature:
            counts[message.signature] = counts.get(message.signature, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    return described or number


def build_threads(messages: Sequence[Message]) -> List[Dict[str, Any]]:
    """Group messages by number, newest conversation first."""

    buckets: Dict[str, List[Message]] = {}
    for message in messages:
        key = normalize_number(message.number) or message.number
        buckets.setdefault(key, []).append(message)

    threads: List[Dict[str, Any]] = []
    for key, items in buckets.items():
        items.sort(key=lambda m: m.sort_key)
        latest = items[-1]
        threads.append({
            "number": latest.number,
            "key": key,
            "label": thread_label(items, latest.number),
            "count": len(items),
            "unread": sum(1 for m in items if m.is_unread),
            "latest_time": latest.timestamp_text,
            "latest_content": latest.content,
            "latest_id": latest.numeric_id or 0,
            "messages": items,
        })
    threads.sort(key=lambda t: t["latest_id"], reverse=True)
    return threads
