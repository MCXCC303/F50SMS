"""数据模型：把路由器原始 JSON 解析成可读的 :class:`Message`。

协议要点（由 F50 网页端 js/service.js 逆向得到）：

* `GET /goform/goform_get_cmd_process?cmd=sms_data_total&page=0&data_per_page=500`
  `&mem_store=1&tags=10&order_by=order by id desc`
  返回 `{"messages": [{id, number, content, date, tag, draft_group_id,
  received_all_concat_sms}, ...]}`。
* `content` 是 **base64(UTF-8)**。
* `date` 形如 `26,10,13,08,30,55,+0800`，即 年(2 位),月,日,时,分,秒,时区。
* `tag` 的含义：0 已读、1 未读、2 已发送、3 发送失败、4 草稿。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# tag 语义
# ---------------------------------------------------------------------------

TAG_READ = "0"
TAG_UNREAD = "1"
TAG_SENT = "2"
TAG_SEND_FAILED = "3"
TAG_DRAFT = "4"

TAG_LABELS: Dict[str, str] = {
    TAG_READ: "已读",
    TAG_UNREAD: "未读",
    TAG_SENT: "已发送",
    TAG_SEND_FAILED: "发送失败",
    TAG_DRAFT: "草稿",
}

#: tag 别名，供 CLI 的 --tag 选项使用（大小写不敏感）。
TAG_ALIASES: Dict[str, str] = {
    "read": TAG_READ,
    "已读": TAG_READ,
    "unread": TAG_UNREAD,
    "未读": TAG_UNREAD,
    "new": TAG_UNREAD,
    "sent": TAG_SENT,
    "已发送": TAG_SENT,
    "failed": TAG_SEND_FAILED,
    "发送失败": TAG_SEND_FAILED,
    "draft": TAG_DRAFT,
    "草稿": TAG_DRAFT,
}

#: 短信正文开头的签名，例如「【中国农业银行】王先生…」。
SIGNATURE_RE = re.compile(r"^[\s\u3000]*[【\[]([^】\]]{1,30})[】\]][\s\u3000]*")

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------


def decode_content(raw: Optional[str]) -> str:
    """把路由器返回的 base64 正文解码成文本。

    若内容不是合法的 base64（例如某些固件直接给明文），则原样返回。
    """

    if not raw:
        return ""
    text = raw.strip()
    if not text:
        return ""
    padded = text + "=" * (-len(text) % 4)
    blob: Optional[bytes] = None
    try:
        blob = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return raw
    if blob is None:
        return raw
    for encoding in ("utf-8", "gb18030", "utf-16-be"):
        try:
            decoded = blob.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_text(decoded):
            return decoded
    # 解出来是二进制噪声，说明原本就不是 base64。
    return raw


def _looks_like_text(value: str) -> bool:
    """粗略判断解码结果是不是正常文本（控制字符占比很低）。"""

    if not value:
        return False
    bad = sum(1 for ch in value if ord(ch) < 32 and ch not in "\t\r\n")
    return bad / len(value) < 0.05


def _parse_offset(text: str) -> Optional[timezone]:
    match = _OFFSET_RE.match(text.strip())
    if not match:
        return None
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    if sign == "-":
        delta = -delta
    return timezone(delta)


def parse_router_date(text: Optional[str]) -> Optional[datetime]:
    """解析 `26,10,13,08,30,55,+0800` 形式的日期。

    无法解析时返回 `None`（而不是抛异常），以免单条脏数据毁掉整次导出。
    """

    if not text:
        return None
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) < 6:
        return None
    try:
        year2, month, day, hour, minute, second = (int(p) for p in parts[:6])
    except ValueError:
        return None
    year = 2000 + year2 if year2 < 70 else 1900 + year2
    tzinfo = _parse_offset(parts[6]) if len(parts) > 6 and parts[6] else None
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=tzinfo)
    except ValueError:
        return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip() in ("1", "true", "True")


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """一条短信。"""

    id: str
    number: str
    content: str
    timestamp: Optional[datetime]
    raw_date: str
    tag: str
    group_id: str = ""
    received_all: Optional[bool] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    # -- 构造 --------------------------------------------------------------

    @classmethod
    def from_api(cls, payload: Dict[str, Any]) -> "Message":
        tag = str(payload.get("tag", "")).strip()
        return cls(
            id=str(payload.get("id", "")).strip(),
            number=str(payload.get("number", "")).strip(),
            content=decode_content(payload.get("content")),
            timestamp=parse_router_date(payload.get("date")),
            raw_date=str(payload.get("date", "") or ""),
            tag=tag,
            group_id=str(payload.get("draft_group_id", "") or ""),
            received_all=_coerce_bool(payload.get("received_all_concat_sms")),
            raw=dict(payload),
        )

    # -- 状态判断 ----------------------------------------------------------

    @property
    def is_unread(self) -> bool:
        return self.tag == TAG_UNREAD

    @property
    def is_read(self) -> bool:
        return self.tag == TAG_READ

    @property
    def is_inbox(self) -> bool:
        return self.tag in (TAG_READ, TAG_UNREAD)

    @property
    def is_sent(self) -> bool:
        return self.tag == TAG_SENT

    @property
    def is_send_failed(self) -> bool:
        return self.tag == TAG_SEND_FAILED

    @property
    def is_draft(self) -> bool:
        return self.tag == TAG_DRAFT

    @property
    def is_outgoing(self) -> bool:
        return self.tag in (TAG_SENT, TAG_SEND_FAILED)

    @property
    def status_label(self) -> str:
        return TAG_LABELS.get(self.tag, f"未知({self.tag})" if self.tag else "未知")

    @property
    def signature(self) -> str:
        """正文开头的【签名】，没有则返回空串。"""

        match = SIGNATURE_RE.match(self.content or "")
        return match.group(1).strip() if match else ""

    @property
    def body(self) -> str:
        """去掉开头【签名】之后的正文。"""

        if not self.content:
            return ""
        match = SIGNATURE_RE.match(self.content)
        return self.content[match.end():].strip() if match else self.content

    # -- 排序 / 序列化 -----------------------------------------------------

    @property
    def numeric_id(self) -> Optional[int]:
        try:
            return int(self.id)
        except (TypeError, ValueError):
            return None

    @property
    def sort_key(self):
        return (self.numeric_id if self.numeric_id is not None else -1,
                self.timestamp.timestamp() if self.timestamp else 0.0)

    @property
    def timestamp_text(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.timestamp else ""

    @property
    def offset_text(self) -> str:
        if not self.timestamp or self.timestamp.tzinfo is None:
            return ""
        offset = self.timestamp.utcoffset()
        if offset is None:
            return ""
        total = int(offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "number": self.number,
            "content": self.content,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "timestamp_text": self.timestamp_text,
            "raw_date": self.raw_date,
            "tag": self.tag,
            "status": self.status_label,
            "signature": self.signature,
            "is_unread": self.is_unread,
            "group_id": self.group_id,
        }
        if self.received_all is not None:
            data["received_all"] = self.received_all
        if include_raw:
            data["raw"] = self.raw
        return data


def merge_messages(items: list[dict]) -> list[Message]:
    """把 `sms_data_total` 返回的原始字典合并成消息列表。

    同一条长短信在该固件上可能被拆成多行（id 相同、内容为分片），
    这里按 id 归并并保留内容最长的那一行——与官方网页端的做法一致。
    返回值按 id / 时间倒序（新的在前）。
    """

    merged: Dict[str, Dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id", "")).strip()
        if not mid:
            continue
        previous = merged.get(mid)
        if previous is None:
            merged[mid] = item
            order.append(mid)
        else:
            if len(str(item.get("content") or "")) > len(str(previous.get("content") or "")):
                merged[mid] = item

    messages = [Message.from_api(merged[mid]) for mid in order]
    messages.sort(key=lambda m: m.sort_key, reverse=True)
    return messages
