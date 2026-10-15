"""本工具可能抛出的异常类型。

所有异常都继承自 :class:`F50Error`，CLI 只需捕获这一个基类即可给出友好提示。

退出码约定：`0` 成功，`2` 命令行用法错误（沿用 argparse 的约定），
其余从 `3` 起按下面的类依次排列，互不重叠。
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

    exit_code = 3


class F50AuthError(F50Error):
    """需要登录、密码错误或登录被拒绝。

    `needs_password=True` 表示「给个密码就能继续」。CLI 会据此交互式询问
    密码并自动重试，而不是直接报错退出。
    """

    exit_code = 4

    def __init__(self, message: str, *, hint: str | None = None, needs_password: bool = False) -> None:
        super().__init__(message, hint=hint)
        self.needs_password = needs_password


class F50ProtocolError(F50Error):
    """路由器返回了无法解析或不符合预期的数据。"""

    exit_code = 5


class F50NotFoundError(F50Error):
    """没有找到符合条件的本地查询结果（例如指定的短信 ID）。"""

    exit_code = 6
