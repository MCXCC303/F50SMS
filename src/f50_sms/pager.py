"""长输出交给分页器，短输出直接打印。

行为对齐 `gh` 命令行工具：

* 分页器命令按 `F50_PAGER` > `PAGER` > `less` 的顺序解析；
* 只在标准输出是终端时分页，重定向到管道时永远直接输出；
* 内容不足一屏时不分页（连 `less` 都不会启动）；
* `less` 自动补 `-R` 保留 ANSI 颜色、补 `-S` 让长行横切而非折行，
  并默认 `LESS=FRX`；用户自己写了 `-S` 或 `+S` 则以用户的为准；
* 把分页器设为 `cat` 或空串即可关闭分页。

本模块只负责「怎么分页」，不关心输出内容，便于单独测试。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Dict, List, Mapping, Optional, Sequence

#: 环境变量优先级，靠前的优先。
PAGER_ENV_VARS = ("F50_PAGER", "PAGER")

#: 找不到任何配置时使用的分页器。
DEFAULT_PAGER = "less"

#: 分页器被显式设成这些值时视为「不要分页」。
DISABLED_VALUES = ("", "cat", "none", "off")

#: 使用 less 时，若用户没有自行设置 LESS，则套用这套选项：
#: F = 不足一屏直接退出，R = 解释 ANSI 颜色，X = 退出时不清屏。
#: S（长行横切而非折行）通过命令行参数补，见 with_less_flags。
LESS_DEFAULT_OPTIONS = "FRX"


def resolve_pager_command(env: Optional[Mapping[str, str]] = None) -> Optional[List[str]]:
    """解析出分页器命令；返回 `None` 表示不分页。

    只做解析，不检查可执行文件是否存在（那一步交给 :func:`is_available`）。
    """

    env = os.environ if env is None else env
    raw: Optional[str] = None
    for name in PAGER_ENV_VARS:
        value = env.get(name)
        if value is not None:
            raw = value
            break
    if raw is None:
        raw = DEFAULT_PAGER

    raw = raw.strip()
    if raw.lower() in DISABLED_VALUES:
        return None
    try:
        command = shlex.split(raw)
    except ValueError:
        return None
    return command or None


#: Single-letter less options we add when the user has not chosen for us.
#: R = show ANSI colours, S = chop long lines instead of wrapping them.
LESS_ADDED_FLAGS = ("R", "S")

#: Long spellings of the same options, so an explicit long form also counts.
LESS_LONG_OPTIONS = {
    "R": ("--raw-control-chars",),
    "S": ("--chop-long-lines",),
}


def _has_less_option(arguments: Sequence[str], letter: str) -> bool:
    """Whether the arguments already set, or explicitly clear, a less option.

    Both the short forms (-S, +S) and the long spellings are recognised, so a
    user who writes +S to turn chopping back off keeps that choice.
    """

    for arg in arguments[1:]:
        if arg.startswith(("-", "+")) and letter in arg[1:]:
            return True
        for long_name in LESS_LONG_OPTIONS.get(letter, ()):
            if arg == long_name or arg.startswith(long_name + "="):
                return True
    return False


def with_less_flags(command: Sequence[str]) -> List[str]:
    """Add the less options we rely on unless the user already picked.

    -R keeps ANSI colours readable. -S chops long lines rather than wrapping
    them, which holds the shape of a long message body together and lets the
    reader scroll sideways for the rest.
    """

    result = list(command)
    if not result or os.path.basename(result[0]) != "less":
        return result
    missing = "".join(
        letter for letter in LESS_ADDED_FLAGS if not _has_less_option(result, letter)
    )
    if missing:
        result.append("-" + missing)
    return result


def pager_environment(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """构造子进程环境；用户没设 `LESS` 时套用 :data:`LESS_DEFAULT_OPTIONS`。"""

    base_env = dict(os.environ if env is None else env)
    base_env.setdefault("LESS", LESS_DEFAULT_OPTIONS)
    return base_env


def is_available(command: Optional[Sequence[str]]) -> bool:
    """分页器可执行文件是否存在。"""

    if not command:
        return False
    program = command[0]
    if os.path.sep in program:
        return os.path.isfile(program) and os.access(program, os.X_OK)
    return shutil.which(program) is not None


def terminal_height(default: int = 24) -> int:
    try:
        return shutil.get_terminal_size((80, default)).lines
    except Exception:  # pragma: no cover - 极端环境
        return default


def count_rows(text: str, width: int, measure=None) -> int:
    """How many terminal rows the text occupies once it wraps.

    A single message body can be one very long line, so counting newlines
    understates the height badly. Pass a `measure` that returns the display
    width of a string (double width for CJK) to account for wrapping.

    Without a width the newline count is returned.
    """

    measure = measure or len
    if not text:
        return 0
    body = text[:-1] if text.endswith("\n") else text
    if width <= 0:
        return body.count("\n") + 1
    rows = 0
    for line in body.split("\n"):
        rows += max(1, (measure(line) + width - 1) // width)
    return rows


def needs_paging(text: str, height: int, width: int = 0, measure=None) -> bool:
    """Whether the text is taller than one screen."""

    if height <= 0:
        return True
    return count_rows(text, width, measure) > height


def run_pager(
    text: str,
    command: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    stdout=None,
) -> bool:
    """把文本喂给分页器；成功返回 `True`。

    分页器缺失或启动失败时返回 `False`，由调用方回退到直接输出。
    """

    if not command:
        return False
    final_command = with_less_flags(command)
    if not is_available(final_command):
        return False

    stream = stdout if stdout is not None else sys.stdout
    try:
        process = subprocess.Popen(
            final_command,
            stdin=subprocess.PIPE,
            env=pager_environment(env),
        )
    except (OSError, ValueError):
        return False
    try:
        process.communicate(text.encode("utf-8", "replace"))
    except (BrokenPipeError, OSError):  # pragma: no cover - 用户中途按 q 退出
        return True
    finally:
        stream.flush()
    return True
