"""本工具可能抛出的异常类型。

所有异常都继承自 :class:`F50Error`，CLI 只需捕获这一个基类即可给出友好提示。
"""

from __future__ import annotations


class F50Error(Exception):
    """所有 f50-sms 异常的基类。"""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - 简单委托
        if self.hint:
            return f"{self.message}\n提示：{self.hint}"
        return self.message


class F50ConnectionError(F50Error):
    """无法连接到路由器（网络不可达、超时、DNS 失败等）。"""

    exit_code = 2


class F50AuthError(F50Error):
    """需要登录、密码错误或登录被拒绝。"""

    exit_code = 3


class F50ProtocolError(F50Error):
    """路由器返回了无法解析或不符合预期的数据。"""

    exit_code = 4


class F50NotFoundError(F50Error):
    """没有找到符合条件的本地查询结果（例如指定的短信 ID）。"""

    exit_code = 5
