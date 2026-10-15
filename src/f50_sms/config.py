"""配置解析：命令行参数 > 环境变量 > 配置文件 > 内置默认值。

配置文件默认位于 `~/.config/f50-sms/config.json`（遵循 `XDG_CONFIG_HOME`），
写入时权限固定为 `0600`。也可以用 `F50_SMS_CONFIG` 指定别的路径。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .client import DEFAULT_HOST, DEFAULT_TIMEOUT

APP_DIR_NAME = "f50-sms"
CONFIG_FILE_NAME = "config.json"

ENV_HOST = "F50_SMS_HOST"
ENV_PASSWORD = "F50_SMS_PASSWORD"
ENV_TIMEOUT = "F50_SMS_TIMEOUT"
ENV_CONFIG = "F50_SMS_CONFIG"

KNOWN_KEYS = ("host", "password", "timeout")


def config_path(explicit: Optional[str] = None) -> Path:
    """返回配置文件路径（不保证文件存在）。"""

    if explicit:
        return Path(explicit).expanduser()
    env_path = os.environ.get(ENV_CONFIG)
    if env_path:
        return Path(env_path).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_DIR_NAME / CONFIG_FILE_NAME


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """读取配置文件；不存在或损坏时返回空字典。"""

    file_path = config_path(path)
    if not file_path.is_file():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in KNOWN_KEYS}


def save_config(
    values: Dict[str, Any],
    path: Optional[str] = None,
    *,
    replace: bool = False,
) -> Path:
    """Write settings to the config file with mode `0600`.

    By default the given keys are merged into whatever is already on disk.
    Pass `replace=True` to write exactly these keys instead, which is how a
    key gets removed.
    """

    file_path = config_path(path)
    if replace:
        current = {k: v for k, v in values.items() if k in KNOWN_KEYS and v is not None}
    else:
        current = load_config(str(file_path))
        current.update({k: v for k, v in values.items() if k in KNOWN_KEYS and v is not None})
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(file_path)
    return file_path


@dataclass
class Settings:
    """最终生效的设置，带上每个值的来源，方便 `doctor` 展示。"""

    host: str = DEFAULT_HOST
    password: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT
    config_file: Optional[Path] = None
    sources: Dict[str, str] = field(default_factory=dict)

    @property
    def has_password(self) -> bool:
        return bool(self.password)


def resolve_settings(
    *,
    host: Optional[str] = None,
    password: Optional[str] = None,
    timeout: Optional[float] = None,
    config_file: Optional[str] = None,
) -> Settings:
    """按优先级合并各处配置。"""

    values = load_config(config_file)
    path = config_path(config_file)

    settings = Settings(config_file=path)
    sources: Dict[str, str] = {}

    # host
    env_host = os.environ.get(ENV_HOST)
    if host:
        settings.host, sources["host"] = host, "command line --host"
    elif env_host:
        settings.host, sources["host"] = env_host, f"environment {ENV_HOST}"
    elif values.get("host"):
        settings.host, sources["host"] = str(values["host"]), f"config file {path}"
    else:
        settings.host, sources["host"] = DEFAULT_HOST, "built-in default"

    # password
    env_password = os.environ.get(ENV_PASSWORD)
    if password:
        settings.password, sources["password"] = password, "command line --password"
    elif env_password:
        settings.password, sources["password"] = env_password, f"environment {ENV_PASSWORD}"
    elif values.get("password"):
        settings.password, sources["password"] = str(values["password"]), f"config file {path}"
    else:
        settings.password, sources["password"] = None, "not set (not needed while the session is valid)"

    # timeout
    env_timeout = os.environ.get(ENV_TIMEOUT)
    if timeout is not None:
        settings.timeout, sources["timeout"] = float(timeout), "command line --timeout"
    elif env_timeout:
        try:
            settings.timeout, sources["timeout"] = float(env_timeout), f"environment {ENV_TIMEOUT}"
        except ValueError:
            settings.timeout, sources["timeout"] = DEFAULT_TIMEOUT, "built-in default"
    elif values.get("timeout"):
        try:
            settings.timeout, sources["timeout"] = float(values["timeout"]), f"config file {path}"
        except (TypeError, ValueError):
            settings.timeout, sources["timeout"] = DEFAULT_TIMEOUT, "built-in default"
    else:
        settings.timeout, sources["timeout"] = DEFAULT_TIMEOUT, "built-in default"

    settings.sources = sources
    return settings
