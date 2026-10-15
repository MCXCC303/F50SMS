"""集成测试：起一个假路由器，走真实的 HTTP 与 Cookie 路径。

单元测试里的 `_request_json` 被换掉了，覆盖不到 `Referer`、表单编码、
`Set-Cookie` 这些东西。这里用一个本地 HTTP 服务复刻真机的鉴权行为，
把 `F50Client` 完整地跑一遍。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from f50_sms.client import F50Client
from f50_sms.errors import F50AuthError

PASSWORD = "p@ss word"
LD = "ld-abc-123"
SESSION_COOKIE = "JSESSIONID"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


MESSAGES = [
    {"id": "2", "number": "10010", "content": b64("【示例】第二条"),
     "date": "26,10,02,09,00,00,+0800", "tag": "1"},
    {"id": "1", "number": "10010", "content": b64("【示例】第一条"),
     "date": "26,10,01,09,00,00,+0800", "tag": "0"},
]


def sha256_upper(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


class FakeRouterHandler(BaseHTTPRequestHandler):
    """复刻真机：无 Referer 报错；未登录给静默空响应；登录校验加盐摘要。"""

    protocol_version = "HTTP/1.1"
    sessions: set = set()
    log: list = []
    require_referer = True
    referer_seen: list = []

    # -- 工具 --------------------------------------------------------------

    def log_message(self, *args):  # 静音
        pass

    def _send(self, payload, status=200, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", f"{cookie};Path=/goform;HttpOnly")
        self.end_headers()
        self.wfile.write(body)

    def _logged_in(self) -> bool:
        cookie = self.headers.get("Cookie") or ""
        return any(f"{SESSION_COOKIE}={token}" in cookie for token in self.sessions)

    def _params(self):
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    # -- 路由 --------------------------------------------------------------

    def do_GET(self):
        type(self).referer_seen.append(self.headers.get("Referer"))
        if self.require_referer and not self.headers.get("Referer"):
            return self._send({"Error": "none secure connection"})
        params = self._params()
        cmd = params.get("cmd", "")
        type(self).log.append(("GET", cmd))

        if cmd == "LD":
            return self._send({"LD": LD})
        if cmd == "RD":
            return self._send({"RD": "rd-token"})
        if cmd == "loginfo":
            return self._send({"loginfo": "ok" if self._logged_in() else "no"})

        if not self._logged_in():
            names = [n for n in cmd.split(",") if n]
            return self._send({names[-1]: ""} if len(names) > 1 else {})

        if cmd == "sms_data_total":
            return self._send({"messages": MESSAGES})
        if cmd == "sms_capacity_info":
            return self._send({"sms_nv_total": "150", "sms_nv_rev_total": "2",
                               "sms_nv_send_total": "0", "sms_nv_draftbox_total": "0",
                               "sms_sim_total": "0", "sms_sim_rev_total": "0",
                               "sms_sim_send_total": "0", "sms_sim_draftbox_total": "0"})
        if cmd == "sms_unread_num,sms_dev_unread_num,sms_sim_unread_num":
            return self._send({"sms_unread_num": "1", "sms_dev_unread_num": "1",
                               "sms_sim_unread_num": "0"})
        names = [n for n in cmd.split(",") if n]
        return self._send({name: "" for name in names})

    def do_POST(self):
        type(self).referer_seen.append(self.headers.get("Referer"))
        if self.require_referer and not self.headers.get("Referer"):
            return self._send({"Error": "none secure connection"})
        params = self._params()
        action = params.get("goformId", "")
        type(self).log.append(("POST", action))

        if action == "LOGIN":
            expected = sha256_upper(sha256_upper(PASSWORD) + LD)
            if params.get("password") != expected:
                return self._send({"result": "3"})
            token = f"tok{len(self.sessions) + 1}"
            type(self).sessions.add(token)
            return self._send({"result": "0"}, cookie=f"{SESSION_COOKIE}={token}")
        return self._send({"result": "success"})


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeRouterHandler.sessions = set()
        FakeRouterHandler.log = []
        FakeRouterHandler.referer_seen = []
        FakeRouterHandler.require_referer = True
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouterHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.host = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeRouterHandler.sessions = set()
        FakeRouterHandler.log = []
        FakeRouterHandler.referer_seen = []

    # -- 请求构造 ----------------------------------------------------------

    def test_every_request_carries_referer(self):
        with F50Client(self.host, password=PASSWORD) as client:
            client.fetch_messages()
        self.assertTrue(FakeRouterHandler.referer_seen)
        self.assertTrue(all(ref for ref in FakeRouterHandler.referer_seen))

    def test_error_payload_is_reported_as_auth_error(self):
        # 会话正常，但数据命令回 {"Error": ...}，必须冒泡成鉴权错误而不是空结果。
        original = FakeRouterHandler.do_GET

        def error_for_messages(handler):
            if handler._params().get("cmd") == "sms_data_total":
                type(handler).log.append(("GET", "sms_data_total"))
                return handler._send({"Error": "none secure connection"})
            return original(handler)

        FakeRouterHandler.do_GET = error_for_messages
        try:
            with F50Client(self.host, password=PASSWORD) as client:
                with self.assertRaises(F50AuthError) as ctx:
                    client.fetch_messages()
            self.assertIn("none secure connection", str(ctx.exception))
        finally:
            FakeRouterHandler.do_GET = original

    # -- 鉴权 --------------------------------------------------------------

    def test_login_then_read(self):
        with F50Client(self.host, password=PASSWORD) as client:
            messages = client.fetch_messages()
        self.assertEqual([m.id for m in messages], ["2", "1"])
        actions = [entry for entry in FakeRouterHandler.log if entry[0] == "POST"]
        self.assertEqual(actions, [("POST", "LOGIN")])

    def test_wrong_password_is_reported(self):
        with F50Client(self.host, password="nope") as client:
            with self.assertRaises(F50AuthError) as ctx:
                client.fetch_messages()
        self.assertTrue(ctx.exception.needs_password)

    def test_missing_password_is_reported_without_attempting_login(self):
        with F50Client(self.host) as client:
            with self.assertRaises(F50AuthError) as ctx:
                client.fetch_messages()
        self.assertTrue(ctx.exception.needs_password)
        self.assertNotIn(("POST", "LOGIN"), FakeRouterHandler.log)

    def test_session_is_reused_across_calls(self):
        with F50Client(self.host, password=PASSWORD) as client:
            client.fetch_messages()
            client.fetch_capacity()
            client.fetch_unread_count()
        logins = [e for e in FakeRouterHandler.log if e == ("POST", "LOGIN")]
        self.assertEqual(len(logins), 1)

    def test_capacity_and_unread_after_login(self):
        with F50Client(self.host, password=PASSWORD) as client:
            capacity = client.fetch_capacity()
            unread = client.fetch_unread_count()
        self.assertEqual(capacity["nv_total"], 150)
        self.assertEqual(capacity["nv_used"], 2)
        self.assertEqual(unread, 1)

    def test_present_but_empty_fields_are_not_treated_as_expiry(self):
        # 假路由器对未知字段回空串，但字段是在的；这不该触发重新登录。
        with F50Client(self.host, password=PASSWORD) as client:
            status = client.fetch_status()
            self.assertEqual(status["network_type"], "")
        logins = [e for e in FakeRouterHandler.log if e == ("POST", "LOGIN")]
        self.assertEqual(len(logins), 1)

    def test_empty_box_is_not_an_auth_failure(self):
        original = FakeRouterHandler.do_GET

        def empty_messages(handler):
            if handler._params().get("cmd") == "sms_data_total":
                type(handler).log.append(("GET", "sms_data_total"))
                return handler._send({"messages": []})
            return original(handler)

        FakeRouterHandler.do_GET = empty_messages
        try:
            with F50Client(self.host, password=PASSWORD) as client:
                self.assertEqual(client.fetch_messages(), [])
        finally:
            FakeRouterHandler.do_GET = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
