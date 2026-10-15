"""Sort and filter expressions for the message list.

Two small languages, both designed for a shell command line:

--sort  a comma-separated list of keys, most significant first::

            --sort READ,SOURCE,TIME

        Each key may carry an explicit direction::

            --sort TIME:asc
            --sort SOURCE:asc,TIME:desc

        A key's default direction is used when none is given. READ defaults to
        unread-first, TIME to newest-first.

--filter  a brace-wrapped list of conditions, all of which must hold::

            --filter "{READ: true, SOURCE: 'DeepSeek'}"
            --filter "{STATE: unread, TIME: >7d}"
            --filter "{TEXT: 'verification code', LENGTH: >40}"

        Values are booleans, quoted or bare strings, or comparisons written
        with >, >=, <, <=, = or != for the ordered keys.

The module is pure: it parses text into terms and applies them to messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import F50NotFoundError
from .labels import describe_number
from .models import TAG_ALIASES, TAG_LABELS, Message

# ---------------------------------------------------------------------------
# sort keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SortKey:
    """One sortable field, with the direction used when none is given."""

    name: str
    extract: Callable[[Message], Any]
    descending: bool
    summary: str


def _sender_key(message: Message) -> str:
    return (message.signature or describe_number(message.number) or message.number).lower()


def _time_key(message: Message) -> float:
    return message.timestamp.timestamp() if message.timestamp else 0.0


def _read_key(message: Message) -> int:
    # Unread sorts before read when ascending.
    return 0 if message.is_unread else 1


SORT_KEYS: Dict[str, SortKey] = {
    "TIME": SortKey("TIME", _time_key, True, "message time (newest first)"),
    "ID": SortKey("ID", lambda m: m.numeric_id or 0, True, "router id (newest first)"),
    "READ": SortKey("READ", _read_key, False, "unread before read"),
    "STATE": SortKey("STATE", lambda m: m.tag, False, "message state"),
    "SOURCE": SortKey("SOURCE", _sender_key, False, "sender label"),
    "NUMBER": SortKey("NUMBER", lambda m: m.number, False, "sender number"),
    "LENGTH": SortKey("LENGTH", lambda m: len(m.content or ""), True, "body length (longest first)"),
}

#: Convenience aliases so users can type either spelling.
SORT_ALIASES = {
    "DATE": "TIME",
    "WHEN": "TIME",
    "UNREAD": "READ",
    "TAG": "STATE",
    "SENDER": "SOURCE",
    "FROM": "SOURCE",
    "SIZE": "LENGTH",
    "TEXT": "LENGTH",
}


@dataclass(frozen=True)
class SortTerm:
    key: SortKey
    descending: bool

    def describe(self) -> str:
        return f"{self.key.name}:{'desc' if self.descending else 'asc'}"


def _split_top_level(text: str, separator: str = ",") -> List[str]:
    """Split on a separator that is not inside single or double quotes."""

    pieces: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            continue
        if ch == separator:
            pieces.append("".join(current))
            current = []
            continue
        current.append(ch)
    pieces.append("".join(current))
    return [p.strip() for p in pieces if p.strip()]


def parse_sort(spec: Optional[Iterable[str]]) -> List[SortTerm]:
    """Parse --sort values into ordered terms; empty input means no explicit sort."""

    if not spec:
        return []
    terms: List[SortTerm] = []
    for value in spec:
        for token in _split_top_level(str(value)):
            name, _, direction = token.partition(":")
            key_name = name.strip().upper()
            key_name = SORT_ALIASES.get(key_name, key_name)
            key = SORT_KEYS.get(key_name)
            if key is None:
                raise F50NotFoundError(
                    f"unknown sort key: {name.strip()}",
                    hint="available keys: " + ", ".join(SORT_KEYS),
                )
            descending = key.descending
            flag = direction.strip().lower()
            if flag:
                if flag in ("asc", "ascending", "+"):
                    descending = False
                elif flag in ("desc", "descending", "-"):
                    descending = True
                else:
                    raise F50NotFoundError(
                        f"unknown sort direction: {direction.strip()}",
                        hint="use :asc or :desc",
                    )
            terms.append(SortTerm(key, descending))
    return terms


def apply_sort(messages: Sequence[Message], terms: Sequence[SortTerm]) -> List[Message]:
    """Apply sort terms, most significant first.

    Python's sort is stable, so applying the least significant key first and
    walking backwards yields a correct multi-key ordering.
    """

    result = list(messages)
    if not terms:
        return result
    for term in reversed(terms):
        result.sort(key=term.key.extract, reverse=term.descending)
    return result


def describe_sort(terms: Sequence[SortTerm]) -> str:
    return ",".join(term.describe() for term in terms)


# ---------------------------------------------------------------------------
# filter expressions
# ---------------------------------------------------------------------------

_COMPARISON_RE = re.compile(r"^(>=|<=|!=|>|<|=)\s*(.+)$")
_NUMBER_RE = re.compile(r"^-?\d+$")

#: Keys whose values are compared rather than matched as text.
ORDERED_KEYS = {"TIME", "ID", "LENGTH"}

FILTER_KEYS = (
    "READ", "STATE", "SOURCE", "NUMBER", "SIGNATURE", "TEXT",
    "TIME", "ID", "LENGTH",
)

FILTER_ALIASES = {
    "UNREAD": "READ",
    "TAG": "STATE",
    "FROM": "SOURCE",
    "SENDER": "SOURCE",
    "BODY": "TEXT",
    "CONTENT": "TEXT",
    "DATE": "TIME",
    "WHEN": "TIME",
    "SIZE": "LENGTH",
}


@dataclass(frozen=True)
class Condition:
    """One KEY: VALUE test inside a --filter expression."""

    raw_key: str
    key: str
    operator: Optional[str]
    value: Any
    text: str

    def describe(self) -> str:
        return self.text


def _coerce_bool(text: str) -> Optional[bool]:
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "1"):
        return True
    if lowered in ("false", "no", "0"):
        return False
    return None


def _unquote(text: str) -> Tuple[str, bool]:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        return stripped[1:-1], True
    return stripped, False


def _parse_one_condition(token: str) -> Condition:
    raw_key, sep, raw_value = token.partition(":")
    if not sep:
        raise F50NotFoundError(
            f"filter condition needs KEY: VALUE, got {token!r}",
            hint="for example: {READ: true}",
        )
    name = raw_key.strip().upper()
    key = FILTER_ALIASES.get(name, name)
    if key not in FILTER_KEYS:
        raise F50NotFoundError(
            f"unknown filter key: {raw_key.strip()}",
            hint="available keys: " + ", ".join(FILTER_KEYS),
        )

    value_text = raw_value.strip()
    operator = None
    if key in ORDERED_KEYS:
        match = _COMPARISON_RE.match(value_text)
        if match:
            operator = match.group(1)
            value_text = match.group(2).strip()
        elif not value_text:
            raise F50NotFoundError(f"filter {name} needs a value")

    if key == "READ":
        flag = _coerce_bool(value_text)
        if flag is None:
            raise F50NotFoundError(
                f"filter READ takes true or false, got {value_text!r}",
            )
        value: Any = flag
    elif key in ORDERED_KEYS:
        value = value_text
    else:
        unquoted, _ = _unquote(value_text)
        if not unquoted:
            raise F50NotFoundError(f"filter {name} needs a value")
        value = unquoted

    return Condition(raw_key.strip(), key, operator, value, token.strip())


def parse_filter(spec: Optional[str]) -> List[Condition]:
    """Parse a --filter expression into conditions; empty input means no filter."""

    if not spec:
        return []
    text = str(spec).strip()
    if text.startswith("{"):
        if not text.endswith("}"):
            raise F50NotFoundError(
                "unbalanced braces in --filter",
                hint="write it as {KEY: value, KEY: value}",
            )
        text = text[1:-1]
    if not text.strip():
        return []
    return [_parse_one_condition(token) for token in _split_top_level(text)]


def _compare(left: Any, operator: str, right: str, key: str) -> bool:
    """Compare two values, supporting numbers and time expressions."""

    if key == "TIME":
        from .query import parse_time_spec  # local import avoids a cycle

        try:
            moment = parse_time_spec(right)
        except ValueError as exc:
            raise F50NotFoundError(str(exc)) from exc
        left_value = left.timestamp() if hasattr(left, "timestamp") else float(left or 0)
        right_value = moment.timestamp()
    else:
        left_value = left
        try:
            right_value = int(right)
        except ValueError as exc:
            raise F50NotFoundError(f"{key} compares numbers, got {right!r}") from exc

    if operator == ">":
        return left_value > right_value
    if operator == ">=":
        return left_value >= right_value
    if operator == "<":
        return left_value < right_value
    if operator == "<=":
        return left_value <= right_value
    if operator == "!=":
        return left_value != right_value
    return left_value == right_value


def _match(message: Message, condition: Condition) -> bool:
    key = condition.key
    value = condition.value

    if key == "READ":
        return message.is_unread == bool(value)
    if key == "STATE":
        states = expand_states(str(value))
        return message.tag in states
    if key == "SOURCE":
        return str(value).lower() in _sender_key(message)
    if key == "SIGNATURE":
        return str(value).lower() in (message.signature or "").lower()
    if key == "NUMBER":
        digits = re.sub(r"\D", "", str(value))
        return bool(digits) and re.sub(r"\D", "", message.number).endswith(digits)
    if key == "TEXT":
        return str(value).lower() in (message.content or "").lower()
    if key == "ID":
        return _compare(message.numeric_id or 0, condition.operator or "=", str(value), key)
    if key == "LENGTH":
        return _compare(len(message.content or ""), condition.operator or "=", str(value), key)
    if key == "TIME":
        if message.timestamp is None:
            return False
        return _compare(message.timestamp, condition.operator or ">=", str(value), key)
    return False


def expand_states(value: str) -> set:
    """Turn a state word into the matching tag set."""

    states = set()
    for piece in _split_top_level(value):
        token = piece.strip()
        if not token:
            continue
        if token in TAG_LABELS:
            states.add(token)
            continue
        mapped = TAG_ALIASES.get(token.lower())
        if mapped is None:
            raise F50NotFoundError(
                f"unknown state: {token}",
                hint="available states: " + ", ".join(TAG_LABELS.values()),
            )
        states.add(mapped)
    return states


def apply_filter(messages: Sequence[Message], conditions: Sequence[Condition]) -> List[Message]:
    """Keep only the messages satisfying every condition."""

    if not conditions:
        return list(messages)
    return [m for m in messages if all(_match(m, c) for c in conditions)]


def describe_filter(conditions: Sequence[Condition]) -> str:
    return ", ".join(c.describe() for c in conditions)
