"""render 模块的单元测试：重点验证中日韩宽度对齐。"""

from __future__ import annotations

import json
import re
import unittest

from f50_sms.models import Message
from f50_sms.query import build_threads
from f50_sms.render import (
    Palette,
    first_line,
    flatten,
    pad,
    render_message_detail,
    render_message_table,
    render_table,
    render_thread_table,
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

    def test_column_is_never_narrower_than_its_header(self):
        # A max width below the header width would otherwise let the header
        # overflow its column and push the rule and the data out of line.
        text = render_table(["STATE"], [["unread"]], max_widths=[4], palette=Palette(False))
        header, rule, data = text.splitlines()
        self.assertEqual(text_width(header), text_width(rule))
        self.assertEqual(text_width(rule), text_width(data))
        self.assertEqual(text_width(header), text_width("STATE"))


class MessageTableTests(unittest.TestCase):
    def test_render_without_colour(self):
        out = render_message_table([make()], palette=Palette(False))
        self.assertIn("10010", out)
        self.assertIn("unread", out)
        self.assertIn("示例", out)


class PreviewTests(unittest.TestCase):
    """The TEXT column shows one body line, whole, with no ellipsis."""

    MULTI = "第一行内容\n第二行内容\n第三行内容"

    def test_first_line_helper(self):
        self.assertEqual(first_line(self.MULTI), "第一行内容")
        self.assertEqual(first_line("  前后有空白  "), "前后有空白")

    def test_first_line_skips_leading_blanks(self):
        self.assertEqual(first_line("\n\n  真正的第一行\n后面"), "真正的第一行")

    def test_first_line_of_empty_body(self):
        self.assertEqual(first_line(""), "")
        self.assertEqual(first_line(None), "")

    def test_flatten_keeps_every_character(self):
        self.assertEqual(flatten("a\n b\n\nc"), "a b c")

    def test_table_shows_the_first_line_in_full(self):
        row = render_message_table([make(content=self.MULTI)], palette=Palette(False)).splitlines()[2]
        self.assertIn("第一行内容", row)
        self.assertNotIn("第二行", row)
        self.assertNotIn("\u2026", row)

    def test_table_does_not_clip_a_long_first_line(self):
        body = "长" * 400
        row = render_message_table([make(content=body)], palette=Palette(False)).splitlines()[2]
        self.assertIn(body, row)
        self.assertNotIn("\u2026", row)

    def test_full_shows_the_whole_body_flattened(self):
        row = render_message_table(
            [make(content=self.MULTI)], palette=Palette(False), full=True,
        ).splitlines()[2]
        for part in ("第一行内容", "第二行内容", "第三行内容"):
            self.assertIn(part, row)
        self.assertNotIn("\u2026", row)

    def test_source_is_never_clipped(self):
        # SOURCE comes from the 【...】 signature, and Chinese names run well
        # past the old 12-column cap.
        label = "燃气集团股份有限公司九龙坡分公司"
        row = render_message_table(
            [make(content="【" + label + "】节日提醒")], palette=Palette(False)
        ).splitlines()[2]
        self.assertIn(label, row)
        self.assertNotIn("\u2026", row)

    def test_number_is_never_clipped(self):
        row = render_message_table(
            [make(number="10684540390010457")], palette=Palette(False)
        ).splitlines()[2]
        self.assertIn("10684540390010457", row)
        self.assertNotIn("\u2026", row)

    def test_two_long_short_codes_stay_distinguishable(self):
        left = render_message_table(
            [make("1", number="10684540390010457")], palette=Palette(False)
        ).splitlines()[2]
        right = render_message_table(
            [make("2", number="10680389390010457")], palette=Palette(False)
        ).splitlines()[2]
        self.assertIn("10684540390010457", left)
        self.assertIn("10680389390010457", right)

    def test_thread_table_keeps_the_label_whole(self):
        label = "燃气集团股份有限公司九龙坡分公司"
        message = make(number="106929350212261")
        threads = build_threads([message])
        threads[0]["label"] = label
        row = render_thread_table(threads, palette=Palette(False)).splitlines()[2]
        self.assertIn(label, row)
        self.assertIn("106929350212261", row)
        self.assertNotIn("\u2026", row)

    def test_thread_preview_uses_the_first_line(self):
        threads = build_threads([make(content=self.MULTI)])
        row = render_thread_table(threads, palette=Palette(False)).splitlines()[2]
        self.assertIn("第一行内容", row)
        self.assertNotIn("第二行", row)

class MessageDetailTests(unittest.TestCase):
    """The detail view is a plain KEY<TAB>value flow with no borders."""

    def test_header_fields_are_tab_separated(self):
        text = render_message_detail(make())
        lines = text.splitlines()
        self.assertEqual(lines[0].split("\t")[0], "ID")
        self.assertTrue(lines[0].split("\t")[1].startswith("#"))
        self.assertEqual(lines[1].split("\t"), ["TIME", "2026-10-13 08:30:55 +08:00"])
        self.assertEqual(lines[2].split("\t"), ["NUMBER", "10010"])
        self.assertEqual(lines[3].split("\t")[0], "SOURCE")
        self.assertEqual(lines[4].split("\t"), ["STATE", "unread"])

    def test_message_body_follows_its_marker(self):
        text = render_message_detail(make(content="hello body"))
        lines = text.splitlines()
        index = lines.index("MESSAGE:")
        self.assertEqual(lines[index + 1], "hello body")

    def test_no_box_drawing_characters(self):
        text = render_message_detail(make())
        for ch in "╭╮╰╯│─├┤":
            self.assertNotIn(ch, text)

    def test_long_body_is_not_wrapped_or_truncated(self):
        body = "很长的内容" * 40
        text = render_message_detail(make(content=body))
        self.assertIn(body, text)

    def test_optional_fields_appear_only_when_set(self):
        self.assertNotIn("GROUP", render_message_detail(make()))
        self.assertIn("GROUP", render_message_detail(make(draft_group_id="7")))


class GhStyleHeaderTests(unittest.TestCase):
    """The list header mimics the GitHub CLI: uppercase, underlined per column."""

    @staticmethod
    def _columns(line):
        return [part for part in line.split("  ") if part]

    def test_header_is_uppercase(self):
        out = render_message_table([make()], palette=Palette(False))
        header = out.splitlines()[0]
        for name in ("ID", "TIME", "SOURCE", "NUMBER", "STATE", "TEXT"):
            self.assertIn(name, header)

    def test_underline_matches_each_column_width(self):
        out = render_message_table([make()], palette=Palette(False))
        header, rule = out.splitlines()[0], out.splitlines()[1]
        header_columns = self._columns(header)
        rule_columns = self._columns(rule)
        self.assertEqual(len(header_columns), len(rule_columns))
        for head, line in zip(header_columns, rule_columns):
            self.assertEqual(set(line), {"\u2500"}, f"underline should be a rule: {line!r}")
            self.assertGreaterEqual(text_width(line), text_width(head))

    def test_short_id_column_carries_a_hash_prefix(self):
        out = render_message_table([make()], palette=Palette(False))
        row = out.splitlines()[2]
        self.assertTrue(row.startswith("#"))

    # -- the underlined variant, used whenever styles are available --------

    @staticmethod
    def _strip(text):
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def test_styled_header_is_underlined_instead_of_ruled(self):
        out = render_message_table([make()], palette=Palette(True))
        lines = out.splitlines()
        self.assertIn("\x1b[4m", lines[0])
        self.assertNotIn("\u2500", out)
        # The line after the header is data, not a rule.
        self.assertTrue(self._strip(lines[1]).startswith("#"))

    def test_underline_covers_each_column_width(self):
        # gh pads every header cell, including the last, so the underline runs
        # the full width of its column. Fixed widths make this measurable:
        # column 0 is 4 wide ("1234"), column 1 is 5 wide ("xxxxx").
        text = render_table(["AB", "CD"], [["1234", "xxxxx"]], palette=Palette(True))
        header = self._strip(text.splitlines()[0])
        self.assertEqual(header[:4], "AB  ")
        self.assertEqual(header[6:], "CD   ")

    def test_plain_header_is_not_padded_on_the_last_cell(self):
        text = render_table(["AB", "CD"], [["1234", "xxxxx"]], palette=Palette(False))
        header = text.splitlines()[0]
        self.assertEqual(header, "AB    CD")

    def test_plain_header_falls_back_to_a_rule(self):
        out = render_message_table([make()], palette=Palette(False))
        self.assertNotIn("\x1b[", out)
        self.assertIn("\u2500", out.splitlines()[1])

    def test_row_count_is_the_same_either_way(self):
        plain = render_message_table([make()], palette=Palette(False)).splitlines()
        styled = render_message_table([make()], palette=Palette(True)).splitlines()
        self.assertEqual(len(plain), len(styled) + 1)

    def test_relative_time_by_default(self):
        message = make()
        out = render_message_table([message], palette=Palette(False))
        self.assertIn("ago", out)
        self.assertNotIn("2026-10-13 08:30:55", out)

    def test_absolute_time_on_request(self):
        out = render_message_table(
            [make()], palette=Palette(False), time_style="absolute",
        )
        self.assertIn("2026-10-13 08:30:55", out)


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
