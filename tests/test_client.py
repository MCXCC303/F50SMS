"""client 模块的单元测试：用假的传输层验证协议细节。"""

from __future__ import annotations

import base64
import hashlib
import unittest
from typing import Any, Dict, List, Mapping

from f50_sms.client import F50Client, sha256_upper
from f50_sms.errors import F50AuthError, F50ProtocolError


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeRouter(F50Client):
    """覆写 `_request_json`，在内存里模拟路由器行为。"""

    def __init__(self, *, pages: List[List[Dict[str, Any]]] | None = None, ld: str = "ld-token") -> None:
        super().__init__("http://192.0.2.1", password="s3cret", timeout=1)
        self.pages = pages if pages is not None else [[]]
        self.ld = ld
        self.calls: List[tuple] = []
        self.login_attempts = 0
        self.fail_gets = 0
        self.extra: Dict[str, Any] = {}

    def _request_json(self, path: str, params: Mapping[str, Any], *, post: bool = False) -> Dict[str, Any]:
        self.calls.append((path, dict(params), post))
        if post:
            self.login_attempts += 1
            return {"result": "0"}
        cmd = params.get("cmd", "")
        if cmd == "LD":
            return {"LD": self.ld}
        if cmd == "sms_data_total":
            page = int(params.get("page", 0))
            if self.fail_gets:
                self.fail_gets -= 1
                return {"Error": "none secure connection"}
            return {"messages": self.pages[page] if page < len(self.pages) else []}
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
            return {"loginfo": "fail"}
        return self.extra.get(cmd, {cmd: ""})


class HashTests(unittest.TestCase):
    def test_sha256_upper(self):
        self.assertEqual(
            sha256_upper("abc"),
            hashlib.sha256(b"abc").hexdigest().upper(),
        )


class LoginTests(unittest.TestCase):
    def test_password_digest_matches_web_ui_algorithm(self):
        router = FakeRouter(ld="abc-123")
        router.login()
        _, params, post = router.calls[-1]
        self.assertTrue(post)
        expected = sha256_upper(sha256_upper("s3cret") + "abc-123")
        self.assertEqual(params["password"], expected)
        self.assertEqual(params["goformId"], "LOGIN")
        self.assertTrue(router.logged_in)

    def test_missing_password_raises(self):
        router = FakeRouter()
        router.password = None
        with self.assertRaises(F50AuthError):
            router.login()

    def test_wrong_password_result_3(self):
        router = FakeRouter()

        def fake(path, params, post=False):
            if params.get("cmd") == "LD":
                return {"LD": "x"}
            return {"result": "3"}

        router._request_json = fake  # type: ignore[assignment]
        with self.assertRaises(F50AuthError):
            router.login()


class FetchTests(unittest.TestCase):
    def test_fetch_messages_single_page(self):
        router = FakeRouter(pages=[[
            {"id": "2", "number": "10010", "content": b64("b"), "date": "26,02,02,10,00,00,+0800", "tag": "1"},
            {"id": "1", "number": "10010", "content": b64("a"), "date": "26,01,01,10,00,00,+0800", "tag": "0"},
        ]])
        messages = router.fetch_messages()
        self.assertEqual([m.id for m in messages], ["2", "1"])

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
            def _request_json(self, path, params, post=False):
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

    def test_auth_error_retries_with_login(self):
        router = FakeRouter(pages=[[{"id": "1", "number": "x", "content": b64("y"), "date": "26,01,01,00,00,00,+0800", "tag": "0"}]])
        router.fail_gets = 1
        messages = router.fetch_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(router.login_attempts, 1)

    def test_host_normalisation(self):
        self.assertEqual(F50Client("192.168.0.1").host, "http://192.168.0.1")
        self.assertEqual(F50Client("http://192.168.0.1/").host, "http://192.168.0.1")
        self.assertEqual(F50Client("https://10.0.0.1:8443").host, "https://10.0.0.1:8443")

    def test_anti_csrf_token(self):
        token = FakeRouter().anti_csrf_token()
        self.assertEqual(token, sha256_upper(sha256_upper("") + "rd-token"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
