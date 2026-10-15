"""Relative time wording, following the style used by the GitHub CLI.

A list of timestamps is far easier to scan as "about 3 hours ago" than as a
wall of full dates, so list output uses this by default while the detail view
keeps the exact timestamp.

Only the phrasing is implemented here; nothing in this module touches the
network or the terminal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_WEEK = 7 * _DAY
_MONTH = 30 * _DAY
_YEAR = 365 * _DAY


def _plural(amount: int, unit: str) -> str:
    return f"{amount} {unit}" if amount == 1 else f"{amount} {unit}s"


def _bucket(seconds: float) -> str:
    """Pick the unit for a non-negative duration, coarse to fine."""

    if seconds < _MINUTE:
        return ""
    if seconds < _HOUR:
        return _plural(int(seconds // _MINUTE), "minute")
    if seconds < _DAY:
        return _plural(int(seconds // _HOUR), "hour")
    if seconds < _WEEK:
        return _plural(int(seconds // _DAY), "day")
    if seconds < _MONTH:
        return _plural(int(seconds // _WEEK), "week")
    if seconds < _YEAR:
        return _plural(int(seconds // _MONTH), "month")
    return _plural(int(seconds // _YEAR), "year")


def humanize_delta(moment: Optional[datetime], *, now: Optional[datetime] = None) -> str:
    """Render a timestamp relative to now, or an empty string if there is none."""

    if moment is None:
        return ""
    if now is None:
        now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()

    seconds = (now - moment).total_seconds()
    if seconds < 0:
        phrase = _bucket(-seconds)
        return "in about " + phrase if phrase else "in a moment"

    phrase = _bucket(seconds)
    return "about " + phrase + " ago" if phrase else "just now"


def humanize_seconds(seconds: float) -> str:
    """Compact duration such as 1h 05m, used for watch intervals and uptime."""

    seconds = int(seconds)
    if seconds < _MINUTE:
        return f"{seconds}s"
    if seconds < _HOUR:
        return f"{seconds // _MINUTE}m {seconds % _MINUTE:02d}s"
    return f"{seconds // _HOUR}h {seconds % _HOUR // _MINUTE:02d}m"
