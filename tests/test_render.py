"""render 模块的单元测试：重点验证中日韩宽度对齐。"""

from __future__ import annotations

import json
import unittest

from f50_sms.models import Message
from f50_sms.render import (
    Palette,
    pad,
    render_message_card,
    render_message_table,
    render_table,
    summarize,
    text_width,
    truncate,
)


def make(message_id: str = "1", content: str = "【示例】中文内容", **overrides) -> Message:
    payload = {
        "id": message_id,
        "number": overrides.pop("number", "10010"),
        "content": content,
        "date": overrides.pop("date", "26,10,13,08,30,55,+0800"),
        "tag": overrides.pop("tag", "1"),
        "draft_group_id": "",
    }
    payload.update(overrides)
    import base64
    payload["content"] = base64.b64encode(payload["content"].encode()).decode()
    return Message.from_api(payload)


class WidthTests(unittest.TestCase):
    def test_ascii_width(self):
        self.assertEqual(text_width("abc"), 3)

    def test_cjk_width(self):
        self.assertEqual(text_width("中文"), 4)
        self.assertEqual(text_width("【】"), 4)

    def test_mixed_width(self):
        self.assertEqual(text_width("ab中文"), 6)

    def test_combining_marks_are_zero_width(self):
        self.assertEqual(text_width("e\u0301"), 1)


class TruncatePadTests(unittest.TestCase):
    def test_truncate_respects_width(self):
        self.assertEqual(text_width(truncate("abcdef", 4)), 4)
        self.assertTrue(truncate("abcdef", 4).endswith("…"))

    def test_truncate_short_text_unchanged(self):
        self.assertEqual(truncate("abc", 10), "abc")

    def test_truncate_cjk(self):
        result = truncate("中文内容测试", 6)
        self.assertLessEqual(text_width(result), 6)

    def test_pad_left_right_center(self):
        self.assertEqual(pad("中", 4), "中  ")
        self.assertEqual(pad("中", 4, "right"), "  中")
        self.assertEqual(pad("中", 4, "center"), " 中 ")


class TableTests(unittest.TestCase):
    def test_no_colour_by_default(self):
        text = render_table(["a"], [["1"]])
        self.assertNotIn("\x1b[", text)

    def test_columns_align_with_cjk(self):
        text = render_table(
            ["名字", "值"],
            [["中文", "1"], ["ab", "22"]],
            palette=Palette(False),
        )
        self.assertEqual(
            text.splitlines(),
            ["名字  值", "────  ──", "中文  1", "ab    22"],
        )

    def test_rows_have_no_trailing_padding(self):
        text = render_table(["a", "b"], [["1", "长"]], palette=Palette(False))
        self.assertEqual(text.splitlines()[-1], "1  长")

    def test_max_width_truncates(self):
        text = render_table(["c"], [["x" * 50]], max_widths=[5], palette=Palette(False))
        self.assertLessEqual(text_width(text.splitlines()[-1]), 6)


class MessageTableTests(unittest.TestCase):
    def test_render_without_colour(self):
        out = render_message_table([make()], palette=Palette(False), width=120)
        self.assertIn("10010", out)
        self.assertIn("unread", out)
        self.assertIn("示例", out)

    def test_card_contains_fields(self):
        card = render_message_card(make(), palette=Palette(False), width=80)
        self.assertIn("Time", card)
        self.assertIn("From", card)
        self.assertIn("2026-10-13 08:30:55", card)
        for line in card.splitlines():
            self.assertEqual(text_width(line), text_width(card.splitlines()[0]))

    def test_card_wraps_long_content(self):
        card = render_message_card(make(content="很长的内容" * 40), palette=Palette(False), width=60)
        self.assertGreater(len(card.splitlines()), 6)


class SummaryTests(unittest.TestCase):
    def test_counts(self):
        stats = summarize([make("1", tag="1"), make("2", tag="0"), make("3", tag="2")])
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["unread"], 1)
        self.assertEqual(stats["read"], 1)
        self.assertEqual(stats["sent"], 1)


class ExportFormatTests(unittest.TestCase):
    def test_json_is_parseable(self):
        from f50_sms.render import to_json
        payload = json.loads(to_json([make()]))
        self.assertEqual(payload[0]["id"], "1")

    def test_csv_header_and_row(self):
        from f50_sms.render import to_csv
        lines = to_csv([make()]).splitlines()
        self.assertEqual(lines[0], "id,time,direction,number,sender,status,content")
        self.assertIn(",2026-10-13 08:30:55 +08:00,in,10010,", lines[1])

    def test_markdown_escapes_pipes(self):
        from f50_sms.render import to_markdown
        text = to_markdown([make(content="a|b")])
        self.assertIn("a\\|b", text)

    def test_jsonl_one_object_per_line(self):
        from f50_sms.render import to_jsonl
        lines = to_jsonl([make("1"), make("2")])
        self.assertEqual(len(lines.splitlines()), 2)
        for line in lines.splitlines():
            json.loads(line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
