"""models 模块的单元测试（不联网）。"""

from __future__ import annotations

import base64
import unittest
from datetime import datetime, timedelta, timezone

from f50_sms.models import (
    TAG_DRAFT,
    TAG_READ,
    TAG_SENT,
    TAG_UNREAD,
    Message,
    decode_content,
    merge_messages,
    parse_router_date,
)


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class DecodeContentTests(unittest.TestCase):
    def test_decodes_base64_utf8(self):
        self.assertEqual(decode_content(b64("【测试】你好")), "【测试】你好")

    def test_empty_input(self):
        self.assertEqual(decode_content(""), "")
        self.assertEqual(decode_content(None), "")

    def test_plaintext_passthrough(self):
        # 含空格，不是合法 base64，应原样返回
        self.assertEqual(decode_content("hello world"), "hello world")

    def test_missing_padding_is_tolerated(self):
        raw = base64.b64encode("短".encode()).decode().rstrip("=")
        self.assertEqual(decode_content(raw), "短")

    def test_binary_noise_is_not_mistaken_for_text(self):
        noise = base64.b64encode(bytes(range(1, 20))).decode()
        self.assertEqual(decode_content(noise), noise)


class ParseDateTests(unittest.TestCase):
    def test_full_date_with_offset(self):
        parsed = parse_router_date("26,10,13,08,30,55,+0800")
        self.assertEqual(parsed, datetime(2026, 10, 13, 8, 30, 55, tzinfo=timezone(timedelta(hours=8))))

    def test_bad_input_returns_none(self):
        for value in ("", None, "not-a-date", "26,10"):
            self.assertIsNone(parse_router_date(value))

    def test_out_of_range_returns_none(self):
        self.assertIsNone(parse_router_date("26,13,45,99,99,99,+0800"))


class MessageTests(unittest.TestCase):
    def _make(self, **overrides):
        payload = {
            "id": "57",
            "number": "10690955998",
            "content": b64("【示例银行】您的验证码是 123456。"),
            "date": "26,10,13,08,30,55,+0800",
            "tag": TAG_UNREAD,
            "draft_group_id": "",
        }
        payload.update(overrides)
        return Message.from_api(payload)

    def test_fields(self):
        message = self._make()
        self.assertEqual(message.id, "57")
        self.assertEqual(message.number, "10690955998")
        self.assertEqual(message.content, "【示例银行】您的验证码是 123456。")
        self.assertEqual(message.timestamp_text, "2026-10-13 08:30:55")
        self.assertEqual(message.offset_text, "+08:00")
        self.assertTrue(message.is_unread)
        self.assertTrue(message.is_inbox)
        self.assertFalse(message.is_outgoing)

    def test_signature_and_body(self):
        message = self._make()
        self.assertEqual(message.signature, "示例银行")
        self.assertEqual(message.body, "您的验证码是 123456。")

    def test_no_signature(self):
        message = self._make(content=b64("普通短信"))
        self.assertEqual(message.signature, "")
        self.assertEqual(message.body, "普通短信")

    def test_tag_helpers(self):
        self.assertTrue(self._make(tag=TAG_READ).is_read)
        self.assertTrue(self._make(tag=TAG_SENT).is_sent)
        self.assertTrue(self._make(tag=TAG_DRAFT).is_draft)
        self.assertEqual(self._make(tag=TAG_SENT).status_label, "已发送")

    def test_to_dict_round_trip(self):
        data = self._make().to_dict()
        self.assertEqual(data["id"], "57")
        self.assertEqual(data["timestamp"], "2026-10-13T08:30:55+08:00")
        self.assertNotIn("raw", data)
        self.assertIn("raw", self._make().to_dict(include_raw=True))


class MergeMessagesTests(unittest.TestCase):
    def test_sorted_newest_first(self):
        items = [
            {"id": "1", "number": "10010", "content": b64("a"), "date": "26,01,01,00,00,00,+0800", "tag": "0"},
            {"id": "9", "number": "10010", "content": b64("b"), "date": "26,02,01,00,00,00,+0800", "tag": "0"},
        ]
        merged = merge_messages(items)
        self.assertEqual([m.id for m in merged], ["9", "1"])

    def test_duplicate_id_keeps_longest_content(self):
        items = [
            {"id": "5", "number": "10010", "content": b64("短"), "date": "26,01,01,00,00,00,+0800", "tag": "0"},
            {"id": "5", "number": "10010", "content": b64("很长的一条短信内容"), "date": "26,01,01,00,00,00,+0800", "tag": "0"},
        ]
        merged = merge_messages(items)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].content, "很长的一条短信内容")

    def test_skips_entries_without_id(self):
        merged = merge_messages([{"number": "10010", "content": b64("x")}])
        self.assertEqual(merged, [])

    def test_empty_input(self):
        self.assertEqual(merge_messages([]), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
