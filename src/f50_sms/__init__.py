"""f50-sms —— 中兴 F50 随身 WiFi 短信命令行工具。

只读读取路由器 `/goform/` 接口中的短信，并提供格式化的查看与导出。

典型用法::

    from f50_sms import F50Client

    with F50Client(host="http://192.168.0.1") as client:
        for message in client.fetch_messages():
            print(message.timestamp_text, message.number, message.content)
"""

from __future__ import annotations

from .client import F50Client
from .errors import (
    F50AuthError,
    F50ConnectionError,
    F50Error,
    F50NotFoundError,
    F50ProtocolError,
)
from .models import Message

__version__ = "0.1.0"

__all__ = [
    "F50Client",
    "Message",
    "F50Error",
    "F50ConnectionError",
    "F50AuthError",
    "F50ProtocolError",
    "F50NotFoundError",
    "__version__",
]
