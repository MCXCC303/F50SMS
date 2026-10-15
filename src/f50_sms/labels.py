"""给号码加一点人类可读的注释。

这里只做**启发式**判断，不联网、不查数据库，目的是让列表更容易扫读：

* 正文开头的签名（`【中国农业银行】…`）优先级最高，直接当作来源；
* 常见服务短号（10010、95599…）查表得到机构名；
* 106 开头的是行业/企业短信通道；
* 11 位 1 开头的是普通手机号。
"""

from __future__ import annotations

import re

#: 常见短号 / 服务号。只收录通用、稳定的号码。
KNOWN_NUMBERS = {
    "10086": "中国移动",
    "1008611": "中国移动·话费查询",
    "10010": "中国联通",
    "10011": "中国联通·充值",
    "10000": "中国电信",
    "10001": "中国电信·自助服务",
    "10188": "联通支付",
    "12306": "中国铁路 12306",
    "95599": "中国农业银行",
    "95588": "中国工商银行",
    "95533": "中国建设银行",
    "95566": "中国银行",
    "95555": "招商银行",
    "95511": "中国平安",
    "95561": "兴业银行",
    "95595": "中国光大银行",
    "95559": "交通银行",
    "95508": "广发银行",
    "95528": "上海浦东发展银行",
    "95577": "华夏银行",
    "95558": "中信银行",
    "95568": "中国民生银行",
    "95580": "中国邮政储蓄银行",
    "95598": "国家电网",
    "95500": "太平洋保险",
    "95518": "中国人保财险",
}

_MOBILE_RE = re.compile(r"^(?:\+?86)?1\d{10}$")
_DIGITS_RE = re.compile(r"\d")


def normalize_number(number: str) -> str:
    """去掉分隔符，保留可选的 `+` 前缀，便于比对。"""

    text = (number or "").strip()
    plus = text.startswith("+")
    digits = "".join(ch for ch in text if ch.isdigit())
    return ("+" if plus else "") + digits


def _national(number: str) -> str:
    """把 `+8613012345678` 归一化成 `13012345678`（仅用于查表）。"""

    digits = "".join(ch for ch in number if ch.isdigit())
    if number.startswith("+86") and len(digits) == 13:
        return digits[2:]
    if len(digits) == 13 and digits.startswith("86"):
        return digits[2:]
    return digits


def classify_number(number: str) -> str:
    """返回号码类别：`mobile` / `shortcode` / `enterprise` / `international` / `unknown`。"""

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
    """给一个号码返回简短说明，未知时返回空串。"""

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
    """过长号码的缩写显示（保留头尾，中间用省略号）。"""

    text = (number or "").strip()
    if len(text) <= keep + 6:
        return text
    return f"{text[:keep]}…{text[-4:]}"
