# -*- coding: utf-8 -*-
"""Tests for the --sort and --filter expression languages."""
from __future__ import annotations

import base64
import unittest
from datetime import datetime, timedelta, timezone

from f50_sms.errors import F50NotFoundError
from f50_sms.expressions import (
    apply_filter,
    apply_sort,
    parse_filter,
    parse_sort,
)
from f50_sms.models import Message

CST = timezone(timedelta(hours=8))


def make(message_id, content, number="10010", when=None, tag="0"):
    when = when or datetime(2026, 1, 1, 12, 0, 0, tzinfo=CST)
    payload = {
        "id": str(message_id),
        "number": number,
        "content": base64.b64encode(content.encode()).decode(),
        "date": when.strftime("%y,%m,%d,%H,%M,%S") + ",+0800",
        "tag": tag,
        "draft_group_id": "",
    }
    return Message.from_api(payload)


class SortParsingTests(unittest.TestCase):
    def test_single_key(self):
        terms = parse_sort(["TIME"])
        self.assertEqual(len(terms), 1)
        self.assertTrue(terms[0].descending)  # TIME defaults to newest first

    def test_multiple_keys_in_order(self):
        terms = parse_sort(["READ,SOURCE,TIME"])
        self.assertEqual([t.key.name for t in terms], ["READ", "SOURCE", "TIME"])

    def test_per_key_direction(self):
        terms = parse_sort(["TIME:asc", "SOURCE:desc"])
        self.assertFalse(terms[0].descending)
        self.assertTrue(terms[1].descending)

    def test_alias_resolution(self):
        self.assertEqual(parse_sort(["DATE"])[0].key.name, "TIME")
        self.assertEqual(parse_sort(["sender"])[0].key.name, "SOURCE")

    def test_repeated_flag_accumulates(self):
        terms = parse_sort(["READ", "SOURCE"])
        self.assertEqual([t.key.name for t in terms], ["READ", "SOURCE"])

    def test_empty_means_no_sort(self):
        self.assertEqual(parse_sort(None), [])
        self.assertEqual(parse_sort([]), [])

    def test_unknown_key_raises(self):
        with self.assertRaises(F50NotFoundError):
            parse_sort(["BOGUS"])

    def test_unknown_direction_raises(self):
        with self.assertRaises(F50NotFoundError):
            parse_sort(["TIME:sideways"])


class SortApplyTests(unittest.TestCase):
    def test_time_descending_default(self):
        old = make(1, "a", when=datetime(2026, 1, 1, tzinfo=CST))
        new = make(2, "b", when=datetime(2026, 6, 1, tzinfo=CST))
        result = apply_sort([old, new], parse_sort(["TIME"]))
        self.assertEqual([m.id for m in result], ["2", "1"])

    def test_time_ascending(self):
        old = make(1, "a", when=datetime(2026, 1, 1, tzinfo=CST))
        new = make(2, "b", when=datetime(2026, 6, 1, tzinfo=CST))
        result = apply_sort([new, old], parse_sort(["TIME:asc"]))
        self.assertEqual([m.id for m in result], ["1", "2"])

    def test_read_puts_unread_first(self):
        read = make(1, "a", tag="0")
        unread = make(2, "b", tag="1")
        result = apply_sort([read, unread], parse_sort(["READ"]))
        self.assertEqual([m.id for m in result], ["2", "1"])

    def test_multi_key_respects_priority(self):
        # Two senders, two times: SOURCE is the primary key.
        a_old = make(1, "x", number="10010", when=datetime(2026, 1, 1, tzinfo=CST))
        a_new = make(2, "y", number="10010", when=datetime(2026, 6, 1, tzinfo=CST))
        b = make(3, "z", number="10086", when=datetime(2026, 3, 1, tzinfo=CST))
        terms = parse_sort(["SOURCE", "TIME"])
        result = apply_sort([b, a_old, a_new], terms)
        # The source label decides first: "china mobile" sorts before
        # "china unicom". Within one source, TIME is newest first.
        self.assertEqual([m.id for m in result], ["3", "2", "1"])

    def test_no_terms_keeps_input_order(self):
        items = [make(1, "a"), make(2, "b")]
        self.assertEqual([m.id for m in apply_sort(items, [])], ["1", "2"])


class FilterParsingTests(unittest.TestCase):
    def test_boolean(self):
        conditions = parse_filter("{READ: true}")
        self.assertEqual(len(conditions), 1)
        self.assertEqual(conditions[0].key, "READ")
        self.assertIs(conditions[0].value, True)

    def test_multiple_conditions(self):
        conditions = parse_filter("{READ: true, SOURCE: 'China Unicom'}")
        self.assertEqual([c.key for c in conditions], ["READ", "SOURCE"])
        self.assertEqual(conditions[1].value, "China Unicom")

    def test_single_quotes_and_double_quotes(self):
        self.assertEqual(parse_filter('{SOURCE: "abc"}')[0].value, "abc")
        self.assertEqual(parse_filter("{SOURCE: 'abc'}")[0].value, "abc")

    def test_bare_value(self):
        self.assertEqual(parse_filter("{SOURCE: abc}")[0].value, "abc")

    def test_comparison_operator(self):
        conditions = parse_filter("{LENGTH: >40}")
        self.assertEqual(conditions[0].operator, ">")
        self.assertEqual(conditions[0].value, "40")

    def test_braces_are_optional(self):
        self.assertEqual(len(parse_filter("READ: true")), 1)

    def test_empty_expression(self):
        self.assertEqual(parse_filter(""), [])
        self.assertEqual(parse_filter(None), [])
        self.assertEqual(parse_filter("{}"), [])

    def test_comma_inside_quotes_is_not_a_separator(self):
        conditions = parse_filter("{TEXT: 'a, b'}")
        self.assertEqual(len(conditions), 1)
        self.assertEqual(conditions[0].value, "a, b")

    def test_unknown_key_raises(self):
        with self.assertRaises(F50NotFoundError):
            parse_filter("{NOPE: 1}")

    def test_missing_colon_raises(self):
        with self.assertRaises(F50NotFoundError):
            parse_filter("{READ}")

    def test_unbalanced_braces_raise(self):
        with self.assertRaises(F50NotFoundError):
            parse_filter("{READ: true")

    def test_bad_boolean_raises(self):
        with self.assertRaises(F50NotFoundError):
            parse_filter("{READ: maybe}")


class FilterApplyTests(unittest.TestCase):
    def setUp(self):
        self.unread = make(1, "verification code 123456", number="10010", tag="1")
        self.read = make(2, "balance notice", number="10086", tag="0")
        self.items = [self.unread, self.read]

    def ids(self, expr):
        return [m.id for m in apply_filter(self.items, parse_filter(expr))]

    def test_read_true(self):
        self.assertEqual(self.ids("{READ: true}"), ["1"])

    def test_read_false(self):
        self.assertEqual(self.ids("{READ: false}"), ["2"])

    def test_state_unread(self):
        self.assertEqual(self.ids("{STATE: unread}"), ["1"])

    def test_source_substring(self):
        self.assertEqual(self.ids("{SOURCE: Unicom}"), ["1"])

    def test_source_is_case_insensitive(self):
        self.assertEqual(self.ids("{SOURCE: unicom}"), ["1"])

    def test_number_tail_match(self):
        self.assertEqual(self.ids("{NUMBER: 10086}"), ["2"])

    def test_text_substring(self):
        self.assertEqual(self.ids("{TEXT: verification}"), ["1"])

    def test_length_comparison(self):
        self.assertEqual(self.ids("{LENGTH: >20}"), ["1"])

    def test_id_comparison(self):
        self.assertEqual(self.ids("{ID: >=2}"), ["2"])

    def test_time_comparison_against_an_absolute_date(self):
        # Both fixtures are dated 2026-01-01.
        self.assertEqual(self.ids("{TIME: >2025-06-01}"), ["1", "2"])
        self.assertEqual(self.ids("{TIME: <2025-06-01}"), [])

    def test_time_comparison_against_a_relative_window(self):
        # ">1d" means "newer than a day ago"; these fixtures are long past.
        self.assertEqual(self.ids("{TIME: >1d}"), [])

    def test_multiple_conditions_are_anded(self):
        self.assertEqual(self.ids("{READ: false, SOURCE: Mobile}"), ["2"])
        self.assertEqual(self.ids("{READ: true, SOURCE: Mobile}"), [])

    def test_no_conditions_keeps_everything(self):
        self.assertEqual([m.id for m in apply_filter(self.items, [])], ["1", "2"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
