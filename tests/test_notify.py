"""Unit tests for the notification daemon: argv layout, dedup, and time frames."""

from __future__ import annotations

import argparse
import base64
import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from f50_sms import commands, notifier
from f50_sms.models import Message
from f50_sms.notifier import notification_argv, send
from f50_sms.output import Output
from f50_sms.query import message_moment, parse_time_spec, router_now


def make(
    message_id: str = "1",
    content: str = "【示例】内容",
    number: str = "10010",
    date: str = "26,10,13,08,30,55,+0800",
) -> Message:
    payload = {
        "id": message_id,
        "number": number,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "date": date,
        "tag": "1",
        "draft_group_id": "",
    }
    return Message.from_api(payload)


def notify_args(**overrides) -> argparse.Namespace:
    base = {
        "interval": 2.0,
        "once": True,
        "since": None,
        "notify_send": "notify-send",
        "dry_run": False,
        "quiet": True,
        "color": "never",
        "width": 100,
        "pager": "never",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def quiet_output() -> Output:
    return Output(notify_args(), stream=io.StringIO())


class ArgvTests(unittest.TestCase):
    """The notification layout the author settled on after testing it."""

    def test_app_name_holds_source_and_number(self):
        argv = notification_argv(make(content="【中国农业银行】账单提醒"))
        self.assertEqual(argv[0], "notify-send")
        self.assertEqual(argv[1], "--app-name=中国农业银行 10010")

    def test_summary_is_the_id_and_body_is_the_full_text(self):
        message = make(content="【示例银行】第一行\n第二行")
        argv = notification_argv(message)
        self.assertEqual(argv[2], "#" + message.short_id)
        self.assertIn("第二行", argv[3])
        self.assertEqual(len(argv), 4)

    def test_body_is_a_single_argument(self):
        # Nothing goes through a shell, so shell syntax in an SMS stays text.
        argv = notification_argv(make(content="【x】; rm -rf / $(whoami)"))
        self.assertEqual(len(argv), 4)
        self.assertIn("rm -rf /", argv[3])

    def test_program_is_configurable(self):
        argv = notification_argv(make(), program="/usr/bin/dunstify")
        self.assertEqual(argv[0], "/usr/bin/dunstify")


class SendTests(unittest.TestCase):
    def test_success_returns_nothing(self):
        with mock.patch.object(notifier.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertIsNone(send(["notify-send", "a", "b"]))

    def test_nonzero_exit_reports_stderr(self):
        with mock.patch.object(notifier.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="no daemon\nmore")
            self.assertEqual(send(["notify-send", "a", "b"]), "no daemon")

    def test_missing_program_is_reported(self):
        with mock.patch.object(notifier.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIn("was not found", send(["notify-send", "a", "b"]))


class FrameTests(unittest.TestCase):
    """Times are compared on the router clock, not the host one."""

    def test_message_moment_drops_the_offset(self):
        moment = message_moment(make(date="26,10,13,08,30,55,+0800"))
        self.assertEqual(moment, datetime(2026, 10, 13, 8, 30, 55))
        self.assertIsNone(moment.tzinfo)

    def test_router_now_uses_the_message_offset(self):
        # +0800 messages mean the router clock reads eight hours ahead of UTC,
        # whichever timezone the host happens to be set to.
        now = router_now([make(date="26,10,13,08,30,55,+0800")])
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertAlmostEqual((now - utc).total_seconds(), 8 * 3600, delta=60)

    def test_router_now_falls_back_without_messages(self):
        self.assertIsNone(router_now([]).tzinfo)

    def test_relative_spec_resolves_on_the_router_clock(self):
        messages = [make(date="26,10,13,08,30,55,+0800")]
        cutoff = parse_time_spec("3d", now=router_now(messages))
        self.assertLess(cutoff, message_moment(messages[0]) + timedelta(days=1))


class DedupTests(unittest.TestCase):
    """_notify_new decides what is actually announced."""

    def setUp(self):
        self.out = quiet_output()
        self.args = notify_args(dry_run=True)

    def test_first_pass_announces_everything(self):
        messages = [make("1", "【a】一"), make("2", "【b】二")]
        fresh = commands._notify_new(messages, set(), self.args, self.out)
        self.assertEqual(len(fresh), 2)

    def test_second_pass_announces_nothing(self):
        messages = [make("1", "【a】一")]
        notified: set = set()
        commands._notify_new(messages, notified, self.args, self.out)
        self.assertEqual(commands._notify_new(messages, notified, self.args, self.out), [])

    def test_late_message_with_an_old_stamp_is_still_announced(self):
        # The SMSC timestamp says when a message was sent, so a delayed one
        # can be older than messages already seen; it must not be skipped.
        notified: set = set()
        newer = make("2", "【b】新", date="26,10,13,08,30,55,+0800")
        commands._notify_new([newer], notified, self.args, self.out)
        older = make("1", "【a】迟到", date="26,10,12,08,30,55,+0800")
        fresh = commands._notify_new([newer, older], notified, self.args, self.out)
        self.assertEqual([m.short_id for m in fresh], [older.short_id])

    def test_identical_text_from_the_same_number_is_one_identity(self):
        self.assertEqual(make("1", "【a】同样内容").digest, make("2", "【a】同样内容").digest)

    def test_announced_in_chronological_order(self):
        later = make("2", "【b】", date="26,10,13,08,30,55,+0800")
        earlier = make("1", "【a】", date="26,10,11,08,30,55,+0800")
        fresh = commands._notify_new([later, earlier], set(), self.args, self.out)
        self.assertEqual([m.short_id for m in fresh], [earlier.short_id, later.short_id])


class CommandTests(unittest.TestCase):
    def test_startup_does_not_replay_the_backlog(self):
        client = mock.Mock()
        client.fetch_messages.return_value = [make("1", "【a】旧消息")]
        out = quiet_output()
        self.assertEqual(commands.cmd_notify(client, notify_args(dry_run=True), out), 0)
        self.assertEqual(out.stream.getvalue(), "")

    def test_since_announces_recent_messages(self):
        live = make("1", "【a】刚刚", date="26,10,15,08,30,55,+0800")
        client = mock.Mock()
        client.fetch_messages.return_value = [live]
        out = quiet_output()
        code = commands.cmd_notify(
            client, notify_args(dry_run=True, since="30d", notify_send="ns"), out
        )
        self.assertEqual(code, 0)
        self.assertIn("ns", out.stream.getvalue())
        self.assertIn("#" + live.short_id, out.stream.getvalue())

    def test_since_excludes_older_messages(self):
        old = make("1", "【a】很久以前", date="26,01,01,08,30,55,+0800")
        client = mock.Mock()
        client.fetch_messages.return_value = [old]
        out = quiet_output()
        commands.cmd_notify(
            client, notify_args(dry_run=True, since="30d", notify_send="ns"), out
        )
        self.assertEqual(out.stream.getvalue(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
