"""Unit tests for the notification daemon: argv layout, dedup, and time frames."""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from f50_sms import commands, notifier
from f50_sms.models import Message
from f50_sms.notifier import (
    APP_ICON,
    DEFAULT_ACTION,
    clipboard_command,
    copy_to_clipboard,
    notification_argv,
    send,
)
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
        "action_label": "Copy and mark read",
        "no_action": False,
        "quiet": True,
        "color": "never",
        "width": 100,
        "pager": "never",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def quiet_output() -> Output:
    return Output(notify_args(), stream=io.StringIO())


def loud_output() -> Output:
    """Output that writes notes, which go to stderr rather than the stream."""
    return Output(notify_args(quiet=False), stream=io.StringIO())


class ArgvTests(unittest.TestCase):
    """The notification layout the author settled on after testing it."""

    def test_app_name_holds_source_and_number(self):
        argv = notification_argv(make(content="【中国农业银行】账单提醒"))
        self.assertEqual(argv[0], "notify-send")
        self.assertEqual(argv[1], "--app-name=中国农业银行 10010")

    def test_the_app_icon_is_requested(self):
        argv = notification_argv(make())
        self.assertIn("-n", argv)
        self.assertEqual(argv[argv.index("-n") + 1], APP_ICON)

    def test_summary_is_the_id_and_body_is_the_full_text(self):
        # Indexed from the end so adding flags later does not shift these.
        message = make(content="【示例银行】第一行\n第二行")
        argv = notification_argv(message)
        self.assertEqual(argv[-2], "#" + message.short_id)
        self.assertIn("第二行", argv[-1])

    def test_body_is_a_single_argument(self):
        # Nothing goes through a shell, so shell syntax in an SMS stays text.
        argv = notification_argv(make(content="【x】; rm -rf / $(whoami)"))
        self.assertEqual(argv[-1], "【x】; rm -rf / $(whoami)")

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


class ActionArgvTests(unittest.TestCase):
    def test_action_adds_wait_and_the_default_action(self):
        argv = notification_argv(make(), action_label="Copy and mark read")
        self.assertIn("--wait", argv)
        self.assertIn("--action=default=Copy and mark read", argv)

    def test_without_a_label_nothing_changes(self):
        argv = notification_argv(make())
        self.assertNotIn("--wait", argv)
        self.assertFalse(any(part.startswith("--action") for part in argv))


class ClipboardTests(unittest.TestCase):
    def test_detects_an_available_writer(self):
        found = clipboard_command()
        self.assertTrue(found is None or isinstance(found, list))

    def test_reports_a_missing_writer(self):
        problem = copy_to_clipboard("x", command=["definitely-not-here"])
        self.assertIn("was not found", problem)

    def test_reports_a_failing_writer(self):
        problem = copy_to_clipboard("x", command=["false"])
        self.assertIn("exited with status", problem)

    def test_success_is_silent(self):
        self.assertIsNone(copy_to_clipboard("hello", command=["cat"]))


class ClickTests(unittest.TestCase):
    """Acting on a click: copy the body, then mark the message read."""

    def setUp(self):
        self.out = loud_output()
        self.args = notify_args()
        self.message = make("7", "【示例】点击我")

    def _run_await(self, chosen, waiting=None):
        with mock.patch.object(commands, "wait_for_action", return_value=chosen), \
             mock.patch.object(commands, "_copy_and_mark_read") as handler:
            commands._await_action(self.message, ["notify-send"], self.args, self.out, waiting)
        return handler

    def test_the_default_action_triggers_copy_and_mark(self):
        handler = self._run_await(DEFAULT_ACTION)
        handler.assert_called_once()

    def test_dismissing_does_nothing(self):
        handler = self._run_await(None)
        handler.assert_not_called()

    def test_a_different_action_does_nothing(self):
        handler = self._run_await("other")
        handler.assert_not_called()

    def test_a_missing_program_is_reported_and_the_slot_freed(self):
        waiting = threading.Semaphore(1)
        notes = io.StringIO()
        with mock.patch.object(commands, "wait_for_action", side_effect=OSError("boom")):
            with contextlib.redirect_stderr(notes):
                commands._await_action(self.message, ["x"], self.args, self.out, waiting)
        # The slot must come back, otherwise the budget leaks on every failure.
        self.assertTrue(waiting.acquire(blocking=False))
        self.assertIn("boom", notes.getvalue())

    def test_the_slot_is_released_after_a_click(self):
        waiting = threading.Semaphore(1)
        self._run_await(DEFAULT_ACTION, waiting)
        self.assertTrue(waiting.acquire(blocking=False))

    def test_copy_then_mark_read(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.message_is_read.return_value = True
        notes = io.StringIO()
        with contextlib.redirect_stderr(notes), \
             mock.patch.object(commands, "copy_to_clipboard", return_value=None) as copy, \
             mock.patch.object(commands, "F50Client", return_value=client), \
             mock.patch.object(commands, "resolve_settings") as settings:
            settings.return_value = mock.Mock(host="http://x", password="p", timeout=1.0)
            commands._copy_and_mark_read(self.message, self.args, self.out)
        copy.assert_called_once_with(self.message.content)
        client.mark_read.assert_called_once_with([self.message.id])
        self.assertIn("copied", notes.getvalue())

    def test_a_clipboard_failure_still_marks_read(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.message_is_read.return_value = True
        notes = io.StringIO()
        with contextlib.redirect_stderr(notes), \
             mock.patch.object(commands, "copy_to_clipboard", return_value="no tool"), \
             mock.patch.object(commands, "F50Client", return_value=client), \
             mock.patch.object(commands, "resolve_settings") as settings:
            settings.return_value = mock.Mock(host="http://x", password="p", timeout=1.0)
            commands._copy_and_mark_read(self.message, self.args, self.out)
        client.mark_read.assert_called_once()
        self.assertIn("could not copy", notes.getvalue())


class RaiseTests(unittest.TestCase):
    """How a notification is dispatched, with and without the action."""

    def setUp(self):
        self.out = loud_output()
        self.message = make("7", "【示例】内容")

    def test_no_action_sends_without_waiting(self):
        args = notify_args(no_action=True)
        with mock.patch.object(commands, "send_notification", return_value=None) as sent, \
             mock.patch.object(commands, "wait_for_action") as waited, \
             contextlib.redirect_stderr(io.StringIO()):
            commands._raise_notification(self.message, args, self.out, threading.Semaphore(1))
        waited.assert_not_called()
        self.assertFalse(any(p.startswith("--action") for p in sent.call_args[0][0]))

    def test_the_action_path_waits_on_a_worker_thread(self):
        seen = []

        def fake_wait(argv):
            seen.append(argv)
            return None

        with mock.patch.object(commands, "wait_for_action", fake_wait):
            commands._raise_notification(
                self.message, notify_args(), self.out, threading.Semaphore(1)
            )
            for _ in range(200):
                if seen:
                    break
                time.sleep(0.01)
        self.assertTrue(seen, "the worker never ran")
        self.assertIn("--wait", seen[0])

    def test_an_exhausted_budget_drops_the_action_but_still_notifies(self):
        notes = io.StringIO()
        with mock.patch.object(commands, "send_notification", return_value=None) as sent:
            with contextlib.redirect_stderr(notes):
                commands._raise_notification(
                    self.message, notify_args(), self.out, threading.Semaphore(0)
                )
        sent.assert_called_once()
        self.assertFalse(any(p.startswith("--action") for p in sent.call_args[0][0]))
        self.assertIn("no click action", notes.getvalue())



class WaiterDrainTests(unittest.TestCase):
    """Exiting must not strand a popup that nobody is listening to."""

    def setUp(self):
        self.out = loud_output()

    def test_nothing_pending_returns_at_once(self):
        notes = io.StringIO()
        with contextlib.redirect_stderr(notes):
            commands._finish_waiters([], self.out)
        self.assertEqual(notes.getvalue(), "")

    def test_finished_workers_are_ignored(self):
        worker = threading.Thread(target=lambda: None)
        worker.start()
        worker.join()
        notes = io.StringIO()
        with contextlib.redirect_stderr(notes):
            commands._finish_waiters([worker], self.out)
        self.assertEqual(notes.getvalue(), "")

    def test_a_live_worker_is_waited_for_and_reported(self):
        release = threading.Event()
        worker = threading.Thread(target=release.wait)
        worker.start()
        notes = io.StringIO()

        def free_it():
            time.sleep(0.2)
            release.set()

        threading.Thread(target=free_it, daemon=True).start()
        start = time.time()
        with contextlib.redirect_stderr(notes):
            commands._finish_waiters([worker], self.out)
        self.assertFalse(worker.is_alive())
        self.assertGreaterEqual(time.time() - start, 0.15)
        self.assertIn("waiting for 1 notification", notes.getvalue())

    def test_the_worker_is_recorded_for_later_draining(self):
        seen = []

        def fake_wait(argv):
            seen.append(argv)
            return None

        pending = []
        with mock.patch.object(commands, "wait_for_action", fake_wait):
            commands._raise_notification(
                make("9", "【示例】x"), notify_args(), self.out,
                threading.Semaphore(1), pending,
            )
            self.assertEqual(len(pending), 1)
            pending[0].join(timeout=5)
        self.assertTrue(seen, "the worker never ran")

    def test_no_action_records_nothing_to_wait_for(self):
        pending = []
        with mock.patch.object(commands, "send_notification", return_value=None), \
             contextlib.redirect_stderr(io.StringIO()):
            commands._raise_notification(
                make("9", "【示例】x"), notify_args(no_action=True), self.out,
                threading.Semaphore(1), pending,
            )
        self.assertEqual(pending, [])

