"""中兴 F50（ZTE UFI 固件）`/goform/` 接口客户端。

协议逆向结果
============

请求格式（所有请求都必须带 `Referer`，否则路由器返回
`{"Error": "none secure connection"}`）::

    GET  /goform/goform_get_cmd_process?isTest=false&cmd=<名字>[&multi_data=1]
    POST /goform/goform_set_cmd_process   （表单：isTest=false&goformId=<动作>&...）

登录
----

1. `cmd=LD` 取一次性随机串（`{"LD":"19ab27ac-..."}`）。
2. 计算 `password = SHA256( SHA256(明文密码) + LD )`，取 **大写十六进制**。
3. `POST goformId=LOGIN&password=<上面的值>`，成功返回 `{"result":0}`。
   失败：`3`=密码错误，`2`=已有用户登录，其它=失败。
4. 服务端回 `Set-Cookie: JSESSIONID=...; Path=/goform`。

关于鉴权
--------

`Referer` 只是第一道门，**读取短信同样需要有效的登录会话**。会话失效后
路由器不返回错误，而是给出**静默的空响应**，这是最容易踩的坑：

========================  ==========================  ============================
请求                      会话有效                    会话失效
========================  ==========================  ============================
`cmd=loginfo`            `{"loginfo":"ok"}`         `{"loginfo":"no"}`
`cmd=sms_data_total`     `{"messages":[...]}`       `{}`（没有 messages 字段）
`cmd=sms_capacity_info`  `{"sms_nv_total":"150",…}`  `{}`
`cmd=network_type,…`    字段齐全                    只回一部分非敏感字段
========================  ==========================  ============================

因此不能把「字段缺失」当成「没有数据」，否则会安静地报告 0 条短信。
本模块的策略是：

1. 数据命令之前先用 `cmd=loginfo` 确认会话，必要时auto login；
2. 响应缺少应有字段时判定为会话失效，重新登录后重试一次；
3. 重试后仍缺字段，再查一次 `loginfo`：确认signed in才接受「确实为空」，
   否则抛 :class:`F50AuthError`，绝不返回静默的空结果。
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .errors import F50AuthError, F50ConnectionError, F50Error, F50ProtocolError
from .models import Message, merge_messages

DEFAULT_HOST = "http://192.168.0.1"
DEFAULT_TIMEOUT = 10.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

GET_PATH = "/goform/goform_get_cmd_process"
SET_PATH = "/goform/goform_set_cmd_process"

#: 设备状态里比较有用的一批字段。
STATUS_CMDS = (
    "modem_main_state",
    "network_type",
    "network_provider",
    "signalbar",
    "lte_rsrp",
    "rssi",
    "Z5g_rsrp",
    "Z5g_SINR",
    "wan_active_band",
    "wan_lte_ca",
    "ppp_status",
    "wan_ipaddr",
    "opms_wan_mode",
    "msisdn",
    "sim_msisdn",
    "iccid",
    "imsi",
    "imei",
    "cr_version",
    "wa_version",
    "web_version",
    "hardware_version",
    "pin_status",
)


def sha256_upper(text: str) -> str:
    """网页端 `paswordAlgorithmsCookie` 的等价实现：大写十六进制 SHA-256。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


#: 少数命令的响应字段名与命令名不同，需要显式声明。
#: 未列出的命令按「命令名即响应字段名」处理。
RESPONSE_KEYS: Dict[str, tuple] = {
    "sms_data_total": ("messages",),
    "sms_status_rpt_data": ("messages",),
    "sms_capacity_info": ("sms_nv_total",),
    "pbm_data_total": ("pbm_data",),
}


def expected_keys(cmd: str) -> List[str]:
    """推断一条 `cmd` 的响应里应当出现哪些字段。

    会话失效时路由器会漏掉这些字段，因此它们同时充当鉴权探针。
    """

    special = RESPONSE_KEYS.get(cmd)
    if special:
        return list(special)
    return [name.strip() for name in cmd.split(",") if name.strip()]


class F50Client:
    """与路由器通信的最小客户端。

    该类是上下文管理器，退出时会关闭底层连接池::

        with F50Client("http://192.168.0.1") as client:
            messages = client.fetch_messages()
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        *,
        password: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        host = (host or DEFAULT_HOST).strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        self.host = host
        self.password = password
        self.timeout = float(timeout)
        self.user_agent = user_agent

        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._session_ok: Optional[bool] = None
        self._ld: Optional[str] = None

    # -- 生命周期 ----------------------------------------------------------

    def __enter__(self) -> "F50Client":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """释放底层连接（urllib 没有显式连接池，这里只是清掉会话状态）。"""

        self._jar.clear()
        self._session_ok = None

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"F50Client(host={self.host!r})"

    # -- 底层 HTTP ---------------------------------------------------------

    def _request(self, path: str, params: Mapping[str, Any], *, post: bool = False) -> str:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = self.host + path
        data: Optional[bytes] = None
        if post:
            data = query.encode("utf-8")
        elif query:
            url = f"{url}?{query}"

        request = urllib.request.Request(
            url,
            data=data,
            headers={
                # 没有 Referer 时路由器会直接回 {"Error": "none secure connection"}。
                "Referer": f"{self.host}/index.html",
                "User-Agent": self.user_agent,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            method="POST" if post else "GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # 服务器有响应但状态码非 2xx
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # pragma: no cover - 读取失败时忽略
                pass
            if exc.code in (401, 403):
                raise F50AuthError(
                    f"the router refused the request (HTTP {exc.code}).",
                    hint="the session may have expired; check the password or restart the web UI.",
                ) from exc
            raise F50ProtocolError(
                f"the router returned HTTP {exc.code}: {body[:200]!r}"
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise F50ConnectionError(
                f"cannot reach the router at {self.host}: {reason}",
                hint="make sure the machine is on the F50 WiFi, or point --host at a different address.",
            ) from exc
        except socket.timeout as exc:
            raise F50ConnectionError(
                f"timed out connecting to {self.host} after {self.timeout:g}s.",
                hint="the device may be asleep; wake it and retry, or raise the limit with --timeout.",
            ) from exc

    def _request_json(self, path: str, params: Mapping[str, Any], *, post: bool = False) -> Dict[str, Any]:
        body = self._request(path, params, post=post)
        if not body.strip():
            # 写命令在无事可做时可能返回空体，视为空结果。
            return {}
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise F50ProtocolError(
                f"the router did not return JSON: {body[:200]!r}",
                hint="if the page address or port changed, point --host at the correct URL.",
            ) from exc
        if not isinstance(data, dict):
            raise F50ProtocolError(f"the router returned an unexpected JSON shape: {data!r}")
        return data

    # -- 命令封装 ----------------------------------------------------------

    def _raw_get(self, cmd: str, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """发一条只读命令，不做任何会话检查。"""

        payload: Dict[str, Any] = {"isTest": "false", "cmd": cmd}
        if "," in cmd:
            payload.setdefault("multi_data", "1")
        if params:
            payload.update(params)
        return self._request_json(GET_PATH, payload)

    @staticmethod
    def _incomplete(data: Mapping[str, Any], keys: Sequence[str]) -> bool:
        """响应是否缺少全部应有字段（即疑似会话失效）。"""

        if "Error" in data:
            return True
        if not keys:
            return False
        return not all(key in data for key in keys)

    def get_cmd(self, cmd: str, **params: Any) -> Dict[str, Any]:
        """执行一条或多条只读 `cmd`，并保证结果来自signed in的会话。

        多条命令用逗号分隔时会自动补上 `multi_data=1`。
        """

        keys = expected_keys(cmd)
        self.ensure_session()

        data = self._raw_get(cmd, params)
        if self._incomplete(data, keys):
            # 缺字段有两种可能：会话刚失效，或者本来就没有数据。
            # 先问一次 loginfo，避免把「空收件箱」误判成掉线。
            if self._probe_loginfo() is not True:
                self._session_ok = False
                self.ensure_session(force=True)
                data = self._raw_get(cmd, params)
                if self._incomplete(data, keys) and self._probe_loginfo() is not True:
                    raise F50AuthError(
                        f"still no data after login (missing fields: {', '.join(keys)}).",
                        hint="open the web UI in a browser to confirm the messages are there, then retry.",
                        needs_password=not self.has_password,
                    )

        if "Error" in data:
            raise F50AuthError(
                f"the router refused the read ({data.get('Error')}).",
                hint="confirm you are on the F50 network and the web UI has not been restarted.",
            )
        return data

    def set_cmd(self, goform_id: str, *, with_ad: bool = False, **params: Any) -> Dict[str, Any]:
        """执行一条写命令（`goformId`）。本工具目前只读，仅保留给后续扩展。"""

        payload: Dict[str, Any] = {"isTest": "false", "goformId": goform_id}
        payload.update(params)
        if with_ad:
            self.ensure_login()
            payload["AD"] = self.anti_csrf_token()
        return self._request_json(SET_PATH, payload, post=True)

    # -- 鉴权 --------------------------------------------------------------

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    @property
    def logged_in(self) -> Optional[bool]:
        """`True`/`False` 表示已知的login status，`None` 表示尚未检查。"""

        return self._session_ok

    def login(self, password: Optional[str] = None) -> bool:
        """登录后台。成功返回 `True`，密码错误抛 :class:`F50AuthError`。"""

        secret = password if password is not None else self.password
        if not secret:
            raise F50AuthError(
                "a password is required to reach the router, but none was provided.",
                hint="pass it with --password, set F50_SMS_PASSWORD, or save it once with: f50sms auth login",
                needs_password=True,
            )

        try:
            ld_data = self._request_json(GET_PATH, {"isTest": "false", "cmd": "LD"})
        except F50Error:
            raise
        ld = str(ld_data.get("LD", "") or "")
        if not ld:
            raise F50ProtocolError(
                "the router did not return a login nonce (LD).",
                hint="the firmware may be incompatible; run f50sms doctor for details.",
            )

        digest = sha256_upper(sha256_upper(secret) + ld)
        result = self._request_json(
            SET_PATH,
            {"isTest": "false", "goformId": "LOGIN", "password": digest},
            post=True,
        )
        code = str(result.get("result", ""))
        if code in ("0", "4"):
            self._session_ok = True
            self._ld = ld
            return True

        self._session_ok = False
        if code == "3":
            raise F50AuthError(
                "wrong router password.",
                hint="the password is case-sensitive; update the config if you changed it in the web UI.",
                needs_password=True,
            )
        if code == "2":
            raise F50AuthError(
                "another user is already signed in; this login was refused.",
                hint="sign out in the browser first, or try again later.",
            )
        raise F50AuthError(f"login failed (result={code or 'unknown'}).")

    def _probe_loginfo(self) -> Optional[bool]:
        """查一次 `loginfo`：`True` signed in，`False` not signed in，`None` 无法判断。"""

        try:
            info = self._raw_get("loginfo", {"multi_data": "1"})
        except F50ConnectionError:
            # 网络不通要让调用方看到真实原因，不能退化成「缺少密码」。
            raise
        except F50Error:
            return None
        value = info.get("loginfo")
        if value is None:
            return None
        return str(value) == "ok"

    def ensure_session(self, *, force: bool = False) -> None:
        """确保处于登录态；not signed in则auto login。

        会话仍有效时不会白白登录一次，因此网页端signed in的情况下无需密码。
        """

        if self._session_ok and not force:
            return
        if not force and self._probe_loginfo() is True:
            self._session_ok = True
            return
        self.login()

    def ensure_login(self) -> None:
        """`ensure_session` 的别名，保留旧名字方便调用。"""

        self.ensure_session()

    def anti_csrf_token(self) -> str:
        """Compute the `AD` field that write commands must carry.

        The web UI builds it as `SHA256(SHA256(rd0 + rd1) + RD)`. rd0 is the
        modem's inner version and rd1 the component version, both reported by
        `cmd=Language,cr_version,wa_inner_version`; RD is a fresh challenge.
        Neither version part is empty, so they have to be read, not assumed.
        """

        versions = self.get_cmd("Language,cr_version,wa_inner_version", multi_data="1")
        rd0 = str(versions.get("wa_inner_version", "") or "")
        rd1 = str(versions.get("cr_version", "") or "")
        rd = str(self.get_cmd("RD").get("RD", "") or "")
        return sha256_upper(sha256_upper(rd0 + rd1) + rd)

    def check_login(self) -> Optional[bool]:
        """返回路由器当前是否处于登录态，失败时返回 `None`。

        只做探测，不会顺手登录，`doctor` 用它汇报真实的会话状态。
        """

        return self._probe_loginfo()

    # -- 短信 --------------------------------------------------------------

    def fetch_raw_messages(
        self,
        *,
        page_size: int = 500,
        max_pages: int = 50,
        mem_store: int = 1,
        tags: int = 10,
        order_by: str = "order by id desc",
    ) -> List[Dict[str, Any]]:
        """拉取原始短信记录（已按 id 合并重复行）。

        部分固件会忽略 `page`/`data_per_page` 一次返回全部数据，
        因此这里做了「没有新数据就停止」的保护，避免死循环。
        """

        merged: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        page = 0
        while page < max_pages:
            data = self.get_cmd(
                "sms_data_total",
                page=page,
                data_per_page=page_size,
                mem_store=mem_store,
                tags=tags,
                order_by=order_by,
            )
            batch = data.get("messages", [])
            if not isinstance(batch, list):
                raise F50ProtocolError(
                    f"sms_data_total returned an unexpected messages field: {batch!r}"
                )
            if not batch:
                break

            fresh = 0
            for item in batch:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id", "")).strip()
                if not mid:
                    continue
                previous = merged.get(mid)
                if previous is None:
                    merged[mid] = item
                    order.append(mid)
                    fresh += 1
                elif len(str(item.get("content") or "")) > len(
                    str(previous.get("content") or "")
                ):
                    merged[mid] = item

            if len(batch) < page_size or fresh == 0:
                break
            page += 1

        return [merged[mid] for mid in order]

    def fetch_messages(self, **kwargs: Any) -> List[Message]:
        """拉取并解析全部短信，按时间/ID 倒序返回。"""

        return merge_messages(self.fetch_raw_messages(**kwargs))

    def fetch_capacity(self) -> Dict[str, Any]:
        """返回短信storage（设备侧与 SIM 侧）。"""

        raw = self.get_cmd("sms_capacity_info")
        integer = lambda key: _as_int(raw.get(key))  # noqa: E731 - 短小的局部工具
        nv_total = integer("sms_nv_total")
        nv_receive = integer("sms_nv_rev_total")
        nv_send = integer("sms_nv_send_total")
        nv_draft = integer("sms_nv_draftbox_total")
        sim_total = integer("sms_sim_total")
        sim_receive = integer("sms_sim_rev_total")
        sim_send = integer("sms_sim_send_total")
        sim_draft = integer("sms_sim_draftbox_total")
        return {
            "nv_total": nv_total,
            "nv_receive": nv_receive,
            "nv_send": nv_send,
            "nv_draft": nv_draft,
            "nv_used": nv_receive + nv_send + nv_draft,
            "sim_total": sim_total,
            "sim_receive": sim_receive,
            "sim_send": sim_send,
            "sim_draft": sim_draft,
            "sim_used": sim_receive + sim_send + sim_draft,
            "raw": raw,
        }

    def fetch_unread_count(self) -> int:
        """未读数量（设备侧 + SIM 侧，取路由器给出的合计值）。"""

        raw = self.get_cmd(
            "sms_unread_num,sms_dev_unread_num,sms_sim_unread_num",
            multi_data="1",
        )
        total = _as_int(raw.get("sms_unread_num"), default=None)
        if total is not None:
            return total
        return _as_int(raw.get("sms_dev_unread_num")) + _as_int(raw.get("sms_sim_unread_num"))

    def fetch_flags(self) -> Dict[str, Any]:
        """短信相关状态位（有新短信 / 有状态报告等）。"""

        raw = self.get_cmd(
            "sms_received_flag,sts_received_flag,modem_main_state,sim_status",
            multi_data="1",
        )
        return {
            "has_new_sms": str(raw.get("sms_received_flag", "")) not in ("", "0"),
            "has_status_report": str(raw.get("sts_received_flag", "")) not in ("", "0"),
            "modem_main_state": raw.get("modem_main_state", ""),
            "sim_status": raw.get("sim_status", ""),
            "raw": raw,
        }

    def fetch_status(self) -> Dict[str, Any]:
        """设备 / SIM / 网络概览，供 `f50sms status` 使用。"""

        raw = self.get_cmd(",".join(STATUS_CMDS), multi_data="1")
        return {
            "modem_main_state": raw.get("modem_main_state", ""),
            "network_type": raw.get("network_type", ""),
            "network_provider": raw.get("network_provider", ""),
            "signalbar": raw.get("signalbar", ""),
            "rsrp": raw.get("lte_rsrp") or raw.get("Z5g_rsrp") or "",
            "sinr": raw.get("Z5g_SINR", ""),
            "band": raw.get("wan_active_band", ""),
            "carrier_aggregation": raw.get("wan_lte_ca", ""),
            "ppp_status": raw.get("ppp_status", ""),
            "wan_ip": raw.get("wan_ipaddr", ""),
            "opms_wan_mode": raw.get("opms_wan_mode", ""),
            "phone_number": raw.get("sim_msisdn") or raw.get("msisdn") or "",
            "iccid": raw.get("iccid", ""),
            "imsi": raw.get("imsi", ""),
            "imei": raw.get("imei", ""),
            "firmware": raw.get("web_version", ""),
            "hardware": raw.get("hardware_version", ""),
            "pin_status": raw.get("pin_status", ""),
            "raw": raw,
        }

    # -- 便捷方法 ----------------------------------------------------------

    def ping(self) -> float:
        """返回一次 `cmd=RD` 往返耗时（秒）。

        只测连通性，不建立会话，因此不需要密码；`doctor` 靠它区分
        「网络不通」和「需要登录」两种情况。
        """

        start = time.monotonic()
        self._raw_get("RD")
        return time.monotonic() - start


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
