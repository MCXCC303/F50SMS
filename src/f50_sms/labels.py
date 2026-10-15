"""Lightweight, human-friendly labels for phone numbers.

Purely heuristic and offline (no lookups, no database). It exists to make lists
easier to scan:

* the signature at the start of the body ("[Bank]...") wins and is used as the
  source label;
* common short/service codes (10010, 95599, ...) resolve via a lookup table;
* 106-prefixed numbers are enterprise SMS gateways;
* 11-digit numbers starting with 1 are ordinary mobile numbers.
"""

from __future__ import annotations

import re

#: Well-known short/service codes. Organisation names are rendered in English so
#: the output reads consistently regardless of the message language.
KNOWN_NUMBERS = {
    "10086": "China Mobile",
    "1008611": "China Mobile (balance)",
    "10010": "China Unicom",
    "10011": "China Unicom (top-up)",
    "10000": "China Telecom",
    "10001": "China Telecom (self-service)",
    "10188": "Unicom Pay",
    "12306": "China Railway 12306",
    "95599": "Agricultural Bank of China",
    "95588": "ICBC",
    "95533": "China Construction Bank",
    "95566": "Bank of China",
    "95555": "China Merchants Bank",
    "95511": "Ping An",
    "95561": "Industrial Bank",
    "95595": "China Everbright Bank",
    "95559": "Bank of Communications",
    "95508": "China Guangfa Bank",
    "95528": "Shanghai Pudong Development Bank",
    "95577": "Hua Xia Bank",
    "95558": "CITIC Bank",
    "95568": "China Minsheng Bank",
    "95580": "Postal Savings Bank of China",
    "95598": "State Grid",
    "95500": "China Pacific Insurance",
    "95518": "PICC Property",
}

_MOBILE_RE = re.compile(r"^(?:\+?86)?1\d{10}$")
_DIGITS_RE = re.compile(r"\d")

#: Generic labels that carry no real information, so callers may override them.
GENERIC_LABELS = ("enterprise SMS", "mobile number", "international number")


def normalize_number(number: str) -> str:
    """Strip separators but keep a leading `+`, for comparison."""

    text = (number or "").strip()
    plus = text.startswith("+")
    digits = "".join(ch for ch in text if ch.isdigit())
    return ("+" if plus else "") + digits


def _national(number: str) -> str:
    """Normalise `+8613012345678` to `13012345678` (for table lookup only)."""

    digits = "".join(ch for ch in number if ch.isdigit())
    if number.startswith("+86") and len(digits) == 13:
        return digits[2:]
    if len(digits) == 13 and digits.startswith("86"):
        return digits[2:]
    return digits


def classify_number(number: str) -> str:
    """Return the number class: mobile/shortcode/enterprise/international/unknown."""

    raw = (number or "").strip()
    if not raw:
        return "unknown"
    national = _national(raw)
    if not national or not _DIGITS_RE.search(national):
        return "unknown"
    if national in KNOWN_NUMBERS:
        return "shortcode"
    if national.startswith("106"):
        return "enterprise"
    if _MOBILE_RE.match(national):
        return "mobile"
    if raw.startswith("+"):
        return "international"
    if len(national) <= 6:
        return "shortcode"
    return "unknown"


def describe_number(number: str) -> str:
    """Short human description for a number; empty string when unknown."""

    raw = (number or "").strip()
    if not raw:
        return ""
    national = _national(raw)
    if national in KNOWN_NUMBERS:
        return KNOWN_NUMBERS[national]
    kind = classify_number(raw)
    if kind == "enterprise":
        return "enterprise SMS"
    if kind == "mobile":
        return "mobile number"
    if kind == "international":
        return "international number"
    return ""


def short_number(number: str, *, keep: int = 6) -> str:
    """Abbreviate a long number (head + tail with an ellipsis)."""

    text = (number or "").strip()
    if len(text) <= keep + 6:
        return text
    return f"{text[:keep]}...{text[-4:]}"
