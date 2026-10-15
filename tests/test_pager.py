"""pager 模块的单元测试：命令解析、阈值判断与回退行为。"""

from __future__ import annotations

import unittest
from unittest import mock

from f50_sms import pager


class ResolveCommandTests(unittest.TestCase):
    def test_default_is_less(self):
        self.assertEqual(pager.resolve_pager_command({}), ["less"])

    def test_pager_env(self):
        self.assertEqual(pager.resolve_pager_command({"PAGER": "more"}), ["more"])

    def test_f50_pager_wins_over_pager(self):
        env = {"F50_PAGER": "more", "PAGER": "less"}
        self.assertEqual(pager.resolve_pager_command(env), ["more"])

    def test_arguments_are_split(self):
        env = {"PAGER": "less -S --quit-if-one-screen"}
        self.assertEqual(
            pager.resolve_pager_command(env),
            ["less", "-S", "--quit-if-one-screen"],
        )

    def test_disabled_values(self):
        for value in ("", "cat", "CAT", "none", "off", "  "):
            self.assertIsNone(pager.resolve_pager_command({"PAGER": value}), value)

    def test_broken_quoting_is_ignored(self):
        self.assertIsNone(pager.resolve_pager_command({"PAGER": "less 'unclosed"}))


class LessFlagsTests(unittest.TestCase):
    def test_adds_raw_control(self):
        self.assertEqual(pager.with_less_flags(["less"]), ["less", "-R"])

    def test_keeps_existing_raw_control(self):
        self.assertEqual(pager.with_less_flags(["less", "-R"]), ["less", "-R"])
        self.assertEqual(pager.with_less_flags(["less", "-FRX"]), ["less", "-FRX"])

    def test_absolute_path_is_recognised(self):
        self.assertEqual(pager.with_less_flags(["/usr/bin/less"]), ["/usr/bin/less", "-R"])

    def test_other_pagers_untouched(self):
        self.assertEqual(pager.with_less_flags(["more"]), ["more"])


class EnvironmentTests(unittest.TestCase):
    def test_defaults_less_options(self):
        self.assertEqual(pager.pager_environment({})["LESS"], pager.LESS_DEFAULT_OPTIONS)

    def test_user_less_options_win(self):
        self.assertEqual(pager.pager_environment({"LESS": "-S"})["LESS"], "-S")


class ThresholdTests(unittest.TestCase):
    def test_short_output_needs_no_pager(self):
        self.assertFalse(pager.needs_paging("a\nb", 2))

    def test_exactly_one_screen_needs_no_pager(self):
        self.assertFalse(pager.needs_paging("a\nb\n", 2))

    def test_taller_than_screen_pages(self):
        self.assertTrue(pager.needs_paging("a\nb\nc", 2))

    def test_trailing_newline_does_not_add_a_line(self):
        self.assertFalse(pager.needs_paging("a\n", 1))

    def test_zero_height_always_pages(self):
        self.assertTrue(pager.needs_paging("a", 0))


class AvailabilityTests(unittest.TestCase):
    def test_finds_real_program(self):
        self.assertTrue(pager.is_available(["cat"]))

    def test_missing_program(self):
        self.assertFalse(pager.is_available(["definitely-not-a-real-pager-xyz"]))

    def test_empty_command(self):
        self.assertFalse(pager.is_available(None))
        self.assertFalse(pager.is_available([]))


class RunPagerTests(unittest.TestCase):
    def test_returns_false_when_program_missing(self):
        self.assertFalse(pager.run_pager("hello", ["definitely-not-a-real-pager-xyz"]))

    def test_returns_false_on_empty_command(self):
        self.assertFalse(pager.run_pager("hello", []))

    def test_pipes_text_into_program(self):
        completed = mock.Mock()
        completed.communicate.return_value = (b"", b"")
        with mock.patch.object(pager.subprocess, "Popen", return_value=completed) as popen:
            ok = pager.run_pager("hello\n", ["cat"])
        self.assertTrue(ok)
        popen.assert_called_once()
        completed.communicate.assert_called_once_with(b"hello\n")

    def test_start_failure_falls_back(self):
        with mock.patch.object(pager.subprocess, "Popen", side_effect=OSError("boom")):
            self.assertFalse(pager.run_pager("hello", ["cat"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
