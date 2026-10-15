"""cli 模块的单元测试：过滤、时间解析、会话归并，以及端到端（假路由器）。"""

from __future__ import annotations

import argparse
import base64
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from f50_sms import cli, constants, output, pager, query
from f50_sms.client import F50Client
from f50_sms.errors import (
    F50AuthError,
    F50ConnectionError,
    F50Error,
    F50NotFoundError,
    F50ProtocolError,
)
from f50_sms.labels import classify_number, describe_number, normalize_number
from f50_sms.models import Message


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


SAMPLE_PAYLOAD = [
    {
        "id": "3", "number": "10690955998", "tag": "1", "date": "26,10,13,08,30,55,+0800",
        "content": b64("【示例银行】您的验证码是 123456。"), "draft_group_id": "",
    },
    {
        "id": "2", "number": "10010", "tag": "0", "date": "26,10,01,09,00:00".replace(":", ","),
        "content": b64("【示例联通】余额提醒"), "draft_group_id": "",
    },
    {
        "id": "1", "number": "10010", "tag": "0", "date": "26,04,12,14,23,30,+0800",
        "content": b64("公益短信：示例内容"), "draft_group_id": "",
    },
]


class FakeClient(F50Client):
    """默认会话已建立，因此不需要密码。"""

    def __init__(self, payload=None, **kwargs):
        kwargs.setdefault("password", "s3cret")
        super().__init__("http://192.0.2.1", **kwargs)
        # Copy, so a test that marks a message read cannot leak into the next.
        source = payload if payload is not None else SAMPLE_PAYLOAD
        self.payload = [dict(item) for item in source]
        self.login_attempts = 0
        self.read_requests = []

    def _request_json(self, path, params, post=False):
        cmd = params.get("cmd", "")
        if post:
            if params.get("goformId") == "SET_MSG_READ":
                self.read_requests.append(dict(params))
                marked = str(params.get("msg_id", ""))
                for item in self.payload:
                    if f"{item['id']};" in marked:
                        item["tag"] = "0"
                return {}
            self.login_attempts += 1
            return {"result": "0"}
        if cmd == "Language,cr_version,wa_inner_version":
            return {"Language": "en", "wa_inner_version": "inner-v1", "cr_version": "comp-v1"}
        if cmd == "sms_data_total":
            return {"messages": self.payload}
        if cmd == "sms_capacity_info":
            return {"sms_nv_total": "150", "sms_nv_rev_total": "3", "sms_nv_send_total": "0",
                    "sms_nv_draftbox_total": "0", "sms_sim_total": "0", "sms_sim_rev_total": "0",
                    "sms_sim_send_total": "0", "sms_sim_draftbox_total": "0"}
        if "sms_unread_num" in cmd:
            return {"sms_unread_num": "1", "sms_dev_unread_num": "1", "sms_sim_unread_num": "0"}
        if cmd == "RD":
            return {"RD": "x"}
        if cmd == "LD":
            return {"LD": "ld-token"}
        if cmd == "loginfo":
            return {"loginfo": "ok"}
        if "modem_main_state" in cmd:
            return {"modem_main_state": "modem_init_complete", "network_type": "5G",
                    "network_provider": "示例运营商", "signalbar": "5", "lte_rsrp": "-96"}
        return {cmd: ""}

    def ping(self):
        return 0.01


class NoPagerFormatTests(unittest.TestCase):
    """--no-pager also skips the pipe-friendly format fallback."""

    def _list(self, argv):
        client = FakeClient()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "F50Client", return_value=client):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
        return code, out.getvalue()

    def test_piped_output_still_falls_back_to_plain(self):
        # redirect_stdout gives a non-tty stream, i.e. the pipe case.
        code, out = self._list(["list", "-n", "1"])
        self.assertEqual(code, 0)
        self.assertIn("\t", out)
        self.assertNotIn("\u2500", out)

    def test_no_pager_keeps_the_table(self):
        code, out = self._list(["list", "-n", "1", "--no-pager"])
        self.assertEqual(code, 0)
        self.assertIn("ID", out)
        self.assertIn("\u2500", out)
        self.assertNotIn("\t", out)

    def test_explicit_format_still_wins(self):
        code, out = self._list(["list", "-n", "1", "--no-pager", "-f", "plain"])
        self.assertEqual(code, 0)
        self.assertIn("\t", out)
        self.assertNotIn("\u2500", out)

    def test_no_pager_does_not_force_colour(self):
        # Colour would inject escapes into a pipeline the caller owns.
        code, out = self._list(["list", "-n", "1", "--no-pager"])
        self.assertNotIn("\x1b[", out)


class SetAsReadTests(unittest.TestCase):
    """view marks an unread message read, unless told not to."""

    #: The unread message in SAMPLE_PAYLOAD, and a read one.
    UNREAD = "3"
    READ = "2"

    def _view(self, client, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "F50Client", return_value=client):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_unread_message_is_marked_and_reported(self):
        client = FakeClient()
        code, out, err = self._view(client, ["view", self.UNREAD, "--set-as-read=true"])
        self.assertEqual(code, 0)
        self.assertEqual(len(client.read_requests), 1)
        self.assertIn("marked", err)
        self.assertIn(self.UNREAD, err)

    def test_flag_false_leaves_the_message_alone(self):
        client = FakeClient()
        code, out, err = self._view(client, ["view", self.UNREAD, "--set-as-read=false"])
        self.assertEqual(code, 0)
        self.assertEqual(client.read_requests, [])
        self.assertNotIn("marked", err)

    def test_already_read_message_is_not_marked(self):
        client = FakeClient()
        code, out, err = self._view(client, ["view", self.READ, "--set-as-read=true"])
        self.assertEqual(code, 0)
        self.assertEqual(client.read_requests, [])
        self.assertNotIn("marked", err)

    def test_state_line_reports_the_state_that_was_read(self):
        # The header must show what we fetched, not what we just changed.
        client = FakeClient()
        code, out, err = self._view(client, ["view", self.UNREAD, "--set-as-read=true"])
        self.assertIn("STATE\tunread", out)
        self.assertEqual(client.payload[0]["tag"], "0")

    def test_quiet_suppresses_the_note(self):
        client = FakeClient()
        code, out, err = self._view(
            client, ["--quiet", "view", self.UNREAD, "--set-as-read=true"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(client.read_requests), 1)
        self.assertNotIn("marked", err)


class PasswordGateClient(FakeClient):
    """会话失效且没有密码，用来验证 CLI 的交互式询问与重试。"""

    def __init__(self, payload=None):
        super().__init__(payload, password=None)
        self.accepted_password: str | None = None

    def _probe_loginfo(self):
        return True if self.accepted_password else False

    def _request_json(self, path, params, post=False):
        cmd = str(params.get("cmd", ""))
        if post:
            self.accepted_password = params.get("password")
            return {"result": "0"}
        if cmd == "LD":
            return {"LD": "ld-token"}
        if not self.accepted_password:
            return {}
        return super()._request_json(path, params, post)


def run_cli(argv, client=None):
    """执行 CLI，返回 (退出码, stdout, stderr)。"""

    client = client or FakeClient()
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "F50Client", return_value=client):
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class PasswordPromptTests(unittest.TestCase):
    """缺少密码时应询问一次并自动重试。"""

    def _run(self, *, prompt_available: bool, secret: str):
        client = PasswordGateClient()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "F50Client", return_value=client), \
             mock.patch.object(cli, "_can_prompt", return_value=prompt_available), \
             mock.patch.object(constants.getpass, "getpass", return_value=secret) as prompt:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["count"])
        return code, out.getvalue(), err.getvalue(), client, prompt

    def test_prompts_and_retries(self):
        code, out, err, client, prompt = self._run(prompt_available=True, secret="hunter2")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "1")
        self.assertIsNotNone(client.accepted_password)
        prompt.assert_called_once()
        self.assertIn("auth login", err)

    def test_blank_password_aborts(self):
        code, _, err, client, _ = self._run(prompt_available=True, secret="")
        self.assertEqual(code, F50AuthError.exit_code)
        self.assertIsNone(client.accepted_password)
        self.assertIn("cancelled", err)

    def test_no_terminal_reports_instead_of_hanging(self):
        code, _, err, _, prompt = self._run(prompt_available=False, secret="hunter2")
        self.assertEqual(code, F50AuthError.exit_code)
        prompt.assert_not_called()
        self.assertIn("password is required", err)


class TimeSpecTests(unittest.TestCase):
    def test_absolute_date(self):
        self.assertEqual(query.parse_time_spec("2026-10-01"), datetime(2026, 10, 1))

    def test_absolute_datetime(self):
        self.assertEqual(query.parse_time_spec("2026-10-01 08:30"), datetime(2026, 10, 1, 8, 30))

    def test_relative_days(self):
        now = datetime(2026, 10, 15, 12, 0, 0)
        self.assertEqual(query.parse_time_spec("7d", now=now), now - timedelta(days=7))

    def test_relative_hours(self):
        now = datetime(2026, 10, 15, 12, 0, 0)
        self.assertEqual(query.parse_time_spec("24h", now=now), now - timedelta(hours=24))

    def test_today(self):
        now = datetime(2026, 10, 15, 12, 34, 56)
        self.assertEqual(query.parse_time_spec("today", now=now), datetime(2026, 10, 15))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            query.parse_time_spec("刚才")


class TagExpansionTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(query.expand_tags(["未读"]), {"1"})
        self.assertEqual(query.expand_tags(["unread"]), {"1"})
        self.assertEqual(query.expand_tags(["已发送,草稿"]), {"2", "4"})

    def test_all_means_no_filter(self):
        self.assertIsNone(query.expand_tags(["all"]))

    def test_unknown_raises(self):
        with self.assertRaises(Exception):
            query.expand_tags(["莫名其妙"])


class LabelTests(unittest.TestCase):
    def test_known_number(self):
        self.assertEqual(describe_number("10010"), "China Unicom")

    def test_enterprise(self):
        self.assertEqual(describe_number("10690955998"), "enterprise SMS")

    def test_mobile(self):
        self.assertEqual(describe_number("+8613800138000"), "mobile number")

    def test_unknown(self):
        self.assertEqual(describe_number("01010101010"), "")

    def test_classify(self):
        self.assertEqual(classify_number("10010"), "shortcode")
        self.assertEqual(classify_number("10690955998"), "enterprise")
        self.assertEqual(classify_number("13800138000"), "mobile")

    def test_normalize(self):
        self.assertEqual(normalize_number("+86 138-0013-8000"), "+8613800138000")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.messages = [Message.from_api(p) for p in SAMPLE_PAYLOAD]

    def _args(self, **kwargs):
        base = dict(unread=False, tag=None, from_number=None, search=None, regex=None,
                    since=None, until=None, limit=0, reverse=False)
        base.update(kwargs)
        return type("Args", (), base)

    def test_unread_only(self):
        result = query.filter_messages(self.messages, self._args(unread=True))
        self.assertEqual([m.id for m in result], ["3"])

    def test_from_number_tail_match(self):
        result = query.filter_messages(self.messages, self._args(from_number="10010"))
        self.assertEqual({m.number for m in result}, {"10010"})

    def test_search(self):
        result = query.filter_messages(self.messages, self._args(search="验证码"))
        self.assertEqual([m.id for m in result], ["3"])

    def test_regex(self):
        result = query.filter_messages(self.messages, self._args(regex="余额"))
        self.assertEqual([m.id for m in result], ["2"])

    def test_since(self):
        result = query.filter_messages(self.messages, self._args(since="2026-10-01"))
        self.assertEqual([m.id for m in result], ["3", "2"])

    def test_limit_and_reverse(self):
        result = query.filter_messages(self.messages, self._args(limit=2, reverse=True))
        self.assertEqual([m.id for m in result], ["1", "2"])


class ThreadTests(unittest.TestCase):
    def test_build_threads(self):
        messages = [Message.from_api(p) for p in SAMPLE_PAYLOAD]
        threads = query.build_threads(messages)
        numbers = [t["number"] for t in threads]
        self.assertEqual(numbers, ["10690955998", "10010"])
        by_number = {t["number"]: t for t in threads}
        self.assertEqual(by_number["10010"]["count"], 2)
        self.assertEqual(by_number["10690955998"]["unread"], 1)

    def test_thread_label_prefers_known_number(self):
        messages = [Message.from_api(SAMPLE_PAYLOAD[1])]
        self.assertEqual(query.thread_label(messages, "10010"), "China Unicom")

    def test_thread_label_falls_back_to_signature(self):
        messages = [Message.from_api(SAMPLE_PAYLOAD[0])]
        self.assertEqual(query.thread_label(messages, "10690955998"), "示例银行")


class EndToEndTests(unittest.TestCase):
    def test_default_command_is_list(self):
        code, out, _ = run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("10690955998", out)

    def test_list_unread_plain(self):
        code, out, _ = run_cli(["list", "-u", "-f", "plain"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_list_json_is_valid(self):
        code, out, _ = run_cli(["list", "-f", "json", "--color", "never"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload), 3)

    def test_count(self):
        code, out, _ = run_cli(["count"])
        self.assertEqual((code, out.strip()), (0, "1"))

    def test_count_json(self):
        code, out, _ = run_cli(["count", "--json"])
        self.assertEqual(json.loads(out)["total"], 3)

    def test_show_card(self):
        code, out, _ = run_cli(["show", "3", "--color", "never"])
        self.assertEqual(code, 0)
        self.assertIn("123456", out)

    def test_show_missing_id(self):
        code, _, err = run_cli(["show", "999"])
        self.assertEqual(code, F50NotFoundError.exit_code)
        self.assertIn("999", err)

    def test_thread(self):
        code, out, _ = run_cli(["thread", "10010", "-f", "plain"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_threads(self):
        code, out, _ = run_cli(["threads", "--color", "never"])
        self.assertEqual(code, 0)
        self.assertIn("China Unicom", out)

    def test_status_json(self):
        code, out, _ = run_cli(["status", "-f", "json"])
        payload = json.loads(out)
        self.assertEqual(payload["network_type"], "5G")
        self.assertEqual(payload["sms"]["unread"], 1)

    def test_export_to_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.csv"
            code, _, err = run_cli(["export", "-f", "csv", "-o", str(target)])
            self.assertEqual(code, 0)
            content = target.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("id,time,direction"))
            self.assertIn("exported", err)

    def test_doctor(self):
        code, out, _ = run_cli(["doctor", "--color", "never"])
        self.assertEqual(code, 0)
        self.assertIn("API reachable", out)

    def test_bad_time_spec_exits_nonzero(self):
        code, _, err = run_cli(["list", "--since", "刚才"])
        self.assertEqual(code, F50NotFoundError.exit_code)

    def test_view_without_arguments_shows_the_newest_unread(self):
        code, out, _ = run_cli(["view", "--color", "never"])
        self.assertEqual(code, 0)
        # The fixture's only unread message.
        self.assertIn("123456", out)

    def test_thread_list(self):
        code, out, _ = run_cli(["thread", "list", "--color", "never"])
        self.assertEqual(code, 0)
        self.assertIn("China Unicom", out)

    def test_thread_view(self):
        code, out, _ = run_cli(["thread", "view", "10010", "-f", "plain"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_auth_status(self):
        code, out, _ = run_cli(["auth", "status", "--color", "never"])
        self.assertEqual(code, 0)
        self.assertIn("Session", out)

    def test_auth_login_saves_the_password(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            code, _, err = run_cli(
                ["auth", "login", "--password", "s3cret", "--config", str(target)],
            )
            self.assertEqual(code, 0)
            self.assertIn("saved the password", err)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["password"], "s3cret")

    def test_auth_logout_removes_the_password(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            target.write_text(json.dumps({"host": "http://x", "password": "p"}), encoding="utf-8")
            code, _, err = run_cli(["auth", "logout", "--config", str(target)])
            self.assertEqual(code, 0)
            self.assertIn("removed", err)
            stored = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn("password", stored)
            self.assertEqual(stored["host"], "http://x")

    def test_bare_filters_default_to_list(self):
        code, out, _ = run_cli(["-u", "-f", "plain"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)


class ExitCodeTests(unittest.TestCase):
    """退出码必须互不重叠，否则脚本没法区分失败原因。"""

    def test_codes_are_distinct(self):
        codes = [
            F50Error.exit_code,
            cli.EXIT_USAGE,
            F50ConnectionError.exit_code,
            F50AuthError.exit_code,
            F50ProtocolError.exit_code,
            F50NotFoundError.exit_code,
        ]
        self.assertEqual(len(codes), len(set(codes)), codes)
        self.assertNotIn(0, codes)

    def test_usage_error_uses_its_own_code(self):
        code, _, _ = run_cli(["list", "--definitely-not-a-flag"])
        self.assertEqual(code, cli.EXIT_USAGE)


class GlobalOptionTests(unittest.TestCase):
    """全局选项写在子命令前面或后面都应生效。"""

    def setUp(self):
        self.parser = cli.build_parser()

    def test_before_subcommand(self):
        args = self.parser.parse_args(["--password", "x", "count"])
        self.assertEqual(args.command, "count")
        self.assertEqual(args.password, "x")

    def test_after_subcommand(self):
        args = self.parser.parse_args(["count", "--password", "x"])
        self.assertEqual(args.password, "x")

    def test_later_value_wins(self):
        args = self.parser.parse_args(["--password", "a", "count", "--password", "b"])
        self.assertEqual(args.password, "b")

    def test_unset_options_keep_root_defaults(self):
        args = self.parser.parse_args(["count"])
        self.assertEqual(args.color, "auto")
        self.assertEqual(args.pager, "auto")
        self.assertFalse(args.quiet)
        self.assertIsNone(args.host)
        self.assertIsNone(args.password)

    def test_equals_form(self):
        args = self.parser.parse_args(["--host=http://10.0.0.1", "count"])
        self.assertEqual(args.host, "http://10.0.0.1")

    def test_pager_flags_conflict(self):
        with self.assertRaises((SystemExit, cli.SystemExitWithMessage, argparse.ArgumentError)):
            self.parser.parse_args(["count", "--pager", "--no-pager"])

    def test_auth_login_without_password_prompts(self):
        # No --password means the handler prompts for one.
        args = self.parser.parse_args(["auth", "login"])
        self.assertIsNone(args.password)

    def test_auth_login_with_password(self):
        args = self.parser.parse_args(["auth", "login", "--password", "secret"])
        self.assertEqual(args.password, "secret")

    def test_auth_status_accepts_the_global_password(self):
        args = self.parser.parse_args(["auth", "status", "--password", "secret"])
        self.assertEqual(args.password, "secret")

    def test_config_set_no_longer_owns_the_password(self):
        # It moved to 'auth login'; the global option still needs a value.
        with self.assertRaises((SystemExit, cli.SystemExitWithMessage, argparse.ArgumentError)):
            self.parser.parse_args(["config", "set", "--password"])


class CommandLayoutTests(unittest.TestCase):
    """The command tree follows the GitHub CLI: groups plus named actions."""

    def test_bare_filters_become_list(self):
        self.assertEqual(cli._normalise_argv(["-u"]), ["list", "-u"])
        self.assertEqual(cli._normalise_argv([]), ["list"])

    def test_group_without_action_uses_its_default(self):
        self.assertEqual(cli._normalise_argv(["thread"]), ["thread", "list"])
        self.assertEqual(cli._normalise_argv(["auth"]), ["auth", "status"])
        self.assertEqual(cli._normalise_argv(["config"]), ["config", "show"])

    def test_group_keeps_an_explicit_action(self):
        self.assertEqual(cli._normalise_argv(["thread", "view", "10010"]),
                         ["thread", "view", "10010"])

    def test_group_options_come_after_the_default_action(self):
        self.assertEqual(cli._normalise_argv(["auth", "--host", "x"]),
                         ["auth", "status", "--host", "x"])

    def test_thread_number_is_shorthand_for_view(self):
        self.assertEqual(cli._normalise_argv(["thread", "10010"]),
                         ["thread", "view", "10010"])

    def test_aliases_expand(self):
        self.assertEqual(cli._normalise_argv(["show", "57"]), ["view", "57"])
        self.assertEqual(cli._normalise_argv(["threads"]), ["thread", "list"])
        self.assertEqual(cli._normalise_argv(["chat", "10010"]),
                         ["thread", "view", "10010"])

    def test_unknown_group_action_is_reported(self):
        with self.assertRaises(cli.SystemExitWithMessage):
            cli._normalise_argv(["auth", "bogus"])

    def test_command_key_resolution(self):
        parser = cli.build_parser()
        self.assertEqual(cli._command_key(parser.parse_args(["list"])), "list")
        self.assertEqual(cli._command_key(parser.parse_args(["view"])), "view")
        self.assertEqual(cli._command_key(parser.parse_args(["thread", "view", "1"])),
                         "thread view")
        self.assertEqual(cli._command_key(parser.parse_args(["auth", "logout"])),
                         "auth logout")

    def test_every_registered_handler_is_reachable(self):
        for key in cli.COMMANDS:
            self.assertTrue(key.split(" ")[0] in cli.TOP_LEVEL_COMMANDS, key)


class DefaultCommandTests(unittest.TestCase):
    def test_filters_are_treated_as_list(self):
        self.assertTrue(cli._looks_like_filters(["-u"]))
        self.assertTrue(cli._looks_like_filters(["--search", "验证码"]))
        self.assertTrue(cli._looks_like_filters(["-n", "5"]))

    def test_global_flags_keep_their_meaning(self):
        self.assertFalse(cli._looks_like_filters(["--help"]))
        self.assertFalse(cli._looks_like_filters(["--version"]))
        self.assertFalse(cli._looks_like_filters(["--host", "x", "count"]))
        self.assertFalse(cli._looks_like_filters(["--color=never", "count"]))

    def test_subcommands_are_not_rewritten(self):
        self.assertFalse(cli._looks_like_filters(["show", "57"]))
        self.assertFalse(cli._looks_like_filters(["doctor"]))


class FakeTty(io.StringIO):
    """假装自己是终端。"""

    def isatty(self) -> bool:  # noqa: D102 - 覆盖 StringIO 的实现
        return True


class PagerWiringTests(unittest.TestCase):
    """Output 应根据模式与内容长度决定是否交给分页器。"""

    @staticmethod
    def _output(pager_mode: str, stream):
        args = type("Args", (), {
            "color": "never", "width": 100, "pager": pager_mode, "quiet": False,
        })
        return output.Output(args, stream=stream)

    def test_auto_pages_when_content_exceeds_screen(self):
        stream = FakeTty()
        out = self._output("auto", stream)
        out.height = 2
        with mock.patch.object(pager, "run_pager", return_value=True) as run:
            out.emit("a\nb\nc")
        run.assert_called_once()
        self.assertEqual(stream.getvalue(), "")

    def test_auto_skips_short_content(self):
        stream = FakeTty()
        out = self._output("auto", stream)
        out.height = 40
        with mock.patch.object(pager, "run_pager", return_value=True) as run:
            out.emit("a\nb")
        run.assert_not_called()
        self.assertEqual(stream.getvalue(), "a\nb\n")

    def test_never_disables_paging(self):
        stream = FakeTty()
        out = self._output("never", stream)
        out.height = 1
        with mock.patch.object(pager, "run_pager", return_value=True) as run:
            out.emit("a\nb\nc")
        run.assert_not_called()

    def test_always_forces_paging(self):
        stream = FakeTty()
        out = self._output("always", stream)
        with mock.patch.object(pager, "run_pager", return_value=True) as run:
            out.emit("a")
        run.assert_called_once()

    def test_non_tty_never_pages(self):
        stream = io.StringIO()
        out = self._output("auto", stream)
        out.height = 1
        with mock.patch.object(pager, "run_pager", return_value=True) as run:
            out.emit("a\nb\nc")
        run.assert_not_called()
        self.assertEqual(stream.getvalue(), "a\nb\nc\n")

    def test_falls_back_to_direct_write_when_pager_fails(self):
        stream = FakeTty()
        out = self._output("auto", stream)
        out.height = 1
        with mock.patch.object(pager, "run_pager", return_value=False):
            out.emit("a\nb\nc")
        self.assertEqual(stream.getvalue(), "a\nb\nc\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
