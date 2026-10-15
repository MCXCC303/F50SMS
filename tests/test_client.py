"""client 模块的单元测试：用假的传输层验证协议细节。

假路由器严格复刻真机的鉴权行为：**会话失效时不报错，而是给静默的空响应**。
"""

from __future__ import annotations

import base64
import hashlib
import unittest
from typing import Any, Dict, List, Mapping, Optional

from f50_sms.client import F50Client, expected_keys, sha256_upper
from f50_sms.errors import F50AuthError, F50ProtocolError


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


SAMPLE = [
    {"id": "2", "number": "10010", "content": b64("b"), "date": "26,02,02,10,00,00,+0800", "tag": "1"},
    {"id": "1", "number": "10010", "content": b64("a"), "date": "26,01,01,10,00,00,+0800", "tag": "0"},
]


class FakeRouter(F50Client):
    """覆写 `_request_json`，在内存里模拟路由器的鉴权与数据。

    * `session` 表示路由器当前的登录态；
    * 未登录时数据命令返回空对象，多命令只回最后一个字段（与真机一致）；
    * `dropped` 用来模拟「请求过程中会话过期」；
    * `empty_style` 决定空收件箱返回 `{"messages": []}` 还是 `{}`。
    """

    def __init__(
        self,
        *,
        pages: Optional[List[List[Dict[str, Any]]]] = None,
        ld: str = "ld-token",
        password: Optional[str] = "s3cret",
        session: bool = True,
        login_result: str = "0",
        empty_style: str = "key",
    ) -> None:
        super().__init__("http://192.0.2.1", password=password, timeout=1)
        self.pages = pages if pages is not None else [list(SAMPLE)]
        self.ld = ld
        self.session = session
        self.login_result = login_result
        self.empty_style = empty_style
        self.calls: List[tuple] = []
        self.login_attempts = 0
        self.dropped = 0
        self.extra: Dict[str, Any] = {}

    # -- 假传输层 ----------------------------------------------------------

    def _request_json(self, path: str, params: Mapping[str, Any], *, post: bool = False) -> Dict[str, Any]:
        self.calls.append((path, dict(params), post))
        if post:
            self.login_attempts += 1
            if self.login_result != "0":
                return {"result": self.login_result}
            self.session = True
            return {"result": "0"}

        cmd = str(params.get("cmd", ""))
        if cmd == "LD":
            return {"LD": self.ld}
        if cmd == "loginfo":
            return {"loginfo": "ok" if self.session else "no"}
        if not self.session:
            return self._unauthorised(cmd)
        if self.dropped:
            # 模拟会话恰好在这次请求时过期
            self.dropped -= 1
            self.session = False
            return self._unauthorised(cmd)
        return self._authorised(cmd, params)

    @staticmethod
    def _unauthorised(cmd: str) -> Dict[str, Any]:
        names = [name.strip() for name in cmd.split(",") if name.strip()]
        if len(names) > 1:
            return {names[-1]: ""}
        return {}

    def _authorised(self, cmd: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        if cmd == "sms_data_total":
            page = int(params.get("page", 0))
            if page >= len(self.pages):
                return {"messages": []}
            return {"messages": self.pages[page]}
        if cmd == "sms_capacity_info":
            return {
                "sms_nv_total": "150", "sms_sim_total": "20",
                "sms_nv_rev_total": "10", "sms_nv_send_total": "2", "sms_nv_draftbox_total": "1",
                "sms_sim_rev_total": "3", "sms_sim_send_total": "0", "sms_sim_draftbox_total": "0",
            }
        if "sms_unread_num" in cmd:
            return {"sms_unread_num": "7", "sms_dev_unread_num": "5", "sms_sim_unread_num": "2"}
        if cmd == "RD":
            return {"RD": "rd-token"}
        if cmd == "loginfo":
            return {"loginfo": "ok"}
        return self.extra.get(cmd, {cmd: ""})


class EmptyMailboxRouter(FakeRouter):
    """空收件箱返回 `{}` 的固件（字段缺失并不代表掉线）。"""

    def _authorised(self, cmd: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        if cmd == "sms_data_total":
            return {}
        return super()._authorised(cmd, params)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


class HashTests(unittest.TestCase):
    def test_sha256_upper(self):
        self.assertEqual(sha256_upper("abc"), hashlib.sha256(b"abc").hexdigest().upper())


class ExpectedKeysTests(unittest.TestCase):
    def test_single_command(self):
        self.assertEqual(expected_keys("network_type"), ["network_type"])

    def test_multi_command(self):
        self.assertEqual(expected_keys("a,b , c"), ["a", "b", "c"])

    def test_command_with_custom_response_shape(self):
        self.assertEqual(expected_keys("sms_data_total"), ["messages"])
        self.assertEqual(expected_keys("sms_capacity_info"), ["sms_nv_total"])


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


class LoginTests(unittest.TestCase):
    def test_password_digest_matches_web_ui_algorithm(self):
        router = FakeRouter(ld="abc-123", session=False)
        router.login()
        _, params, post = router.calls[-1]
        self.assertTrue(post)
        expected = sha256_upper(sha256_upper("s3cret") + "abc-123")
        self.assertEqual(params["password"], expected)
        self.assertEqual(params["goformId"], "LOGIN")
        self.assertTrue(router.logged_in)

    def test_missing_password_asks_for_one(self):
        router = FakeRouter(session=False, password=None)
        with self.assertRaises(F50AuthError) as ctx:
            router.login()
        self.assertTrue(ctx.exception.needs_password)

    def test_wrong_password_result_3(self):
        router = FakeRouter(session=False, login_result="3")
        with self.assertRaises(F50AuthError) as ctx:
            router.login()
        self.assertTrue(ctx.exception.needs_password)

    def test_duplicate_user_result_2(self):
        router = FakeRouter(session=False, login_result="2")
        with self.assertRaises(F50AuthError) as ctx:
            router.login()
        self.assertFalse(ctx.exception.needs_password)


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------


class SessionTests(unittest.TestCase):
    def test_existing_session_is_reused(self):
        router = FakeRouter(session=True)
        router.fetch_messages()
        self.assertEqual(router.login_attempts, 0)

    def test_read_logs_in_when_session_is_gone(self):
        router = FakeRouter(session=False)
        messages = router.fetch_messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(router.login_attempts, 1)

    def test_read_without_password_asks_for_one(self):
        router = FakeRouter(session=False, password=None)
        with self.assertRaises(F50AuthError) as ctx:
            router.fetch_messages()
        self.assertTrue(ctx.exception.needs_password)
        self.assertEqual(router.login_attempts, 0)

    def test_session_dropped_between_requests_recovers(self):
        router = FakeRouter(session=True)
        router.dropped = 1
        messages = router.fetch_messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(router.login_attempts, 1)

    def test_failure_after_relogin_raises(self):
        class AlwaysEmpty(FakeRouter):
            def _authorised(self, cmd, params):
                if cmd == "sms_data_total":
                    return {}
                return super()._authorised(cmd, params)

            def _probe_loginfo(self):
                # 会话永远显示未登录，模拟被顶号
                return False

        with self.assertRaises(F50AuthError):
            AlwaysEmpty(session=True).fetch_messages()

    def test_partial_multi_command_response_is_detected(self):
        router = FakeRouter(session=False)
        status = router.fetch_status()
        self.assertEqual(status["network_type"], "")  # 空会话取不到
        self.assertGreaterEqual(router.login_attempts, 1)

    def test_unknown_command_is_not_an_auth_failure(self):
        router = FakeRouter(session=True)
        data = router.get_cmd("definitely_bogus")
        self.assertEqual(data, {"definitely_bogus": ""})
        self.assertEqual(router.login_attempts, 0)


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------


class FetchTests(unittest.TestCase):
    def test_fetch_messages_single_page(self):
        router = FakeRouter(pages=[list(SAMPLE)])
        messages = router.fetch_messages()
        self.assertEqual([m.id for m in messages], ["2", "1"])

    def test_empty_mailbox_returns_no_messages(self):
        router = FakeRouter(pages=[[]])
        self.assertEqual(router.fetch_messages(), [])
        self.assertEqual(router.login_attempts, 0)

    def test_missing_key_with_healthy_session_is_not_an_error(self):
        # 有些固件空收件箱直接回 {}，不能因此索要密码。
        router = EmptyMailboxRouter(pages=[[]])
        self.assertEqual(router.fetch_messages(), [])
        self.assertEqual(router.login_attempts, 0)

    def test_pagination_stops_on_short_page(self):
        full_page = [
            {"id": str(i), "number": "10010", "content": b64("x" * i), "date": "26,01,01,00,00,00,+0800", "tag": "0"}
            for i in range(1, 4)
        ]
        router = FakeRouter(pages=[full_page, [full_page[0]]])
        messages = router.fetch_messages(page_size=3)
        self.assertEqual(len(messages), 3)
        pages_requested = [c[1]["page"] for c in router.calls if c[1].get("cmd") == "sms_data_total"]
        self.assertEqual(pages_requested, [0, 1])

    def test_pagination_stops_when_router_ignores_page_param(self):
        same = [{"id": "1", "number": "10010", "content": b64("x"), "date": "26,01,01,00,00,00,+0800", "tag": "0"}]
        router = FakeRouter(pages=[same, same, same])
        messages = router.fetch_messages(page_size=1)
        self.assertEqual(len(messages), 1)
        pages_requested = [c[1]["page"] for c in router.calls if c[1].get("cmd") == "sms_data_total"]
        self.assertEqual(pages_requested, [0, 1])

    def test_bad_messages_field_raises(self):
        class Bad(FakeRouter):
            def _authorised(self, cmd, params):
                return {"messages": "oops"}

        with self.assertRaises(F50ProtocolError):
            Bad().fetch_messages()

    def test_capacity_mapping(self):
        capacity = FakeRouter().fetch_capacity()
        self.assertEqual(capacity["nv_total"], 150)
        self.assertEqual(capacity["nv_used"], 13)
        self.assertEqual(capacity["sim_used"], 3)

    def test_unread_count(self):
        self.assertEqual(FakeRouter().fetch_unread_count(), 7)


# ---------------------------------------------------------------------------
# 杂项
# ---------------------------------------------------------------------------


class MiscTests(unittest.TestCase):
    def test_host_normalisation(self):
        self.assertEqual(F50Client("192.168.0.1").host, "http://192.168.0.1")
        self.assertEqual(F50Client("http://192.168.0.1/").host, "http://192.168.0.1")
        self.assertEqual(F50Client("https://10.0.0.1:8443").host, "https://10.0.0.1:8443")

    def test_anti_csrf_token(self):
        token = FakeRouter().anti_csrf_token()
        self.assertEqual(token, sha256_upper(sha256_upper("") + "rd-token"))

    def test_ping_does_not_need_a_session(self):
        router = FakeRouter(session=False, password=None)
        router.ping()
        self.assertEqual(router.login_attempts, 0)

    def test_check_login_is_read_only(self):
        router = FakeRouter(session=False, password=None)
        self.assertFalse(router.check_login())
        self.assertEqual(router.login_attempts, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
