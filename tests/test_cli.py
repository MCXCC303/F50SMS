"""cli 模块的单元测试：过滤、时间解析、会话归并，以及端到端（假路由器）。"""

from __future__ import annotations

import base64
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from f50_sms import cli
from f50_sms.client import F50Client
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
    def __init__(self, payload=None, **kwargs):
        super().__init__("http://192.0.2.1", **kwargs)
        self.payload = payload if payload is not None else SAMPLE_PAYLOAD

    def _request_json(self, path, params, post=False):
        cmd = params.get("cmd", "")
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
        if cmd == "loginfo":
            return {"loginfo": "fail"}
        if "modem_main_state" in cmd:
            return {"modem_main_state": "modem_init_complete", "network_type": "5G",
                    "network_provider": "示例运营商", "signalbar": "5", "lte_rsrp": "-96"}
        return {cmd: ""}

    def ping(self):
        return 0.01


def run_cli(argv, client=None):
    """执行 CLI，返回 (退出码, stdout, stderr)。"""

    client = client or FakeClient()
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "F50Client", return_value=client):
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class TimeSpecTests(unittest.TestCase):
    def test_absolute_date(self):
        self.assertEqual(cli.parse_time_spec("2026-10-01"), datetime(2026, 10, 1))

    def test_absolute_datetime(self):
        self.assertEqual(cli.parse_time_spec("2026-10-01 08:30"), datetime(2026, 10, 1, 8, 30))

    def test_relative_days(self):
        now = datetime(2026, 10, 15, 12, 0, 0)
        self.assertEqual(cli.parse_time_spec("7d", now=now), now - timedelta(days=7))

    def test_relative_hours(self):
        now = datetime(2026, 10, 15, 12, 0, 0)
        self.assertEqual(cli.parse_time_spec("24h", now=now), now - timedelta(hours=24))

    def test_today(self):
        now = datetime(2026, 10, 15, 12, 34, 56)
        self.assertEqual(cli.parse_time_spec("today", now=now), datetime(2026, 10, 15))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            cli.parse_time_spec("刚才")


class TagExpansionTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(cli.expand_tags(["未读"]), {"1"})
        self.assertEqual(cli.expand_tags(["unread"]), {"1"})
        self.assertEqual(cli.expand_tags(["已发送,草稿"]), {"2", "4"})

    def test_all_means_no_filter(self):
        self.assertIsNone(cli.expand_tags(["all"]))

    def test_unknown_raises(self):
        with self.assertRaises(Exception):
            cli.expand_tags(["莫名其妙"])


class LabelTests(unittest.TestCase):
    def test_known_number(self):
        self.assertEqual(describe_number("10010"), "中国联通")

    def test_enterprise(self):
        self.assertEqual(describe_number("10690955998"), "106 行业短信")

    def test_mobile(self):
        self.assertEqual(describe_number("+8613800138000"), "手机号")

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
        result = cli.filter_messages(self.messages, self._args(unread=True))
        self.assertEqual([m.id for m in result], ["3"])

    def test_from_number_tail_match(self):
        result = cli.filter_messages(self.messages, self._args(from_number="10010"))
        self.assertEqual({m.number for m in result}, {"10010"})

    def test_search(self):
        result = cli.filter_messages(self.messages, self._args(search="验证码"))
        self.assertEqual([m.id for m in result], ["3"])

    def test_regex(self):
        result = cli.filter_messages(self.messages, self._args(regex="余额"))
        self.assertEqual([m.id for m in result], ["2"])

    def test_since(self):
        result = cli.filter_messages(self.messages, self._args(since="2026-10-01"))
        self.assertEqual([m.id for m in result], ["3", "2"])

    def test_limit_and_reverse(self):
        result = cli.filter_messages(self.messages, self._args(limit=2, reverse=True))
        self.assertEqual([m.id for m in result], ["1", "2"])


class ThreadTests(unittest.TestCase):
    def test_build_threads(self):
        messages = [Message.from_api(p) for p in SAMPLE_PAYLOAD]
        threads = cli.build_threads(messages)
        numbers = [t["number"] for t in threads]
        self.assertEqual(numbers, ["10690955998", "10010"])
        by_number = {t["number"]: t for t in threads}
        self.assertEqual(by_number["10010"]["count"], 2)
        self.assertEqual(by_number["10690955998"]["unread"], 1)

    def test_thread_label_prefers_known_number(self):
        messages = [Message.from_api(SAMPLE_PAYLOAD[1])]
        self.assertEqual(cli.thread_label(messages, "10010"), "中国联通")

    def test_thread_label_falls_back_to_signature(self):
        messages = [Message.from_api(SAMPLE_PAYLOAD[0])]
        self.assertEqual(cli.thread_label(messages, "10690955998"), "示例银行")


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
        self.assertEqual(code, 5)
        self.assertIn("999", err)

    def test_thread(self):
        code, out, _ = run_cli(["thread", "10010", "-f", "plain"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_threads(self):
        code, out, _ = run_cli(["threads", "--color", "never"])
        self.assertEqual(code, 0)
        self.assertIn("中国联通", out)

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
            self.assertIn("已导出", err)

    def test_doctor(self):
        code, out, _ = run_cli(["doctor", "--color", "never"])
        self.assertEqual(code, 0)
        self.assertIn("接口连通", out)

    def test_bad_time_spec_exits_nonzero(self):
        code, _, err = run_cli(["list", "--since", "刚才"])
        self.assertEqual(code, 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
