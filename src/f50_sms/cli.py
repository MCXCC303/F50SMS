"""f50sms command-line entry point.

Responsibilities kept here:
  * build the argparse tree and dispatch to a handler in commands.py
  * resolve connection settings (host / password / timeout)
  * the top-level try/except that maps domain errors onto process exit codes

Design notes:
  * Reading SMS requires a live session; when the session dies the router
    returns silent empty payloads, so the client re-authenticates. If only a
    password is missing we prompt once and retry.
  * Output is human-friendly by default and degrades to plain text when piped.
  * Long terminal output is handed to a pager; piped output never is.
  * Data formats (json/csv/...) emit data only, with no human summary mixed in.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Any, Optional, Sequence

from . import __version__
from .client import F50Client
from .commands import COMMANDS
from .config import resolve_settings
from .errors import F50AuthError, F50Error
from .output import Output

PROG = "f50sms"

#: Sentinel for "config set --password" with no value: prompt for the password.
PROMPT_PASSWORD = object()

#: Reserved for argparse usage errors, kept clear of the domain exit codes.
EXIT_USAGE = 2

LIST_FORMATS = ("table", "plain", "json", "jsonl", "csv", "markdown")
SHOW_FORMATS = ("card", "plain", "json")
THREAD_FORMATS = ("chat", "table", "plain", "json", "jsonl", "csv", "markdown")
THREADS_FORMATS = ("table", "json", "csv", "markdown")
STATUS_FORMATS = ("table", "json")


# ---------------------------------------------------------------------------
# argparse scaffolding
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """Route argparse errors through our own exit code."""

    def error(self, message: str):  # pragma: no cover - called by argparse
        self.print_usage(sys.stderr)
        raise SystemExitWithMessage(EXIT_USAGE, f"{PROG}: argument error: {message}")


class SystemExitWithMessage(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_parser() -> argparse.ArgumentParser:
    # 子命令也挂一份全局选项，但默认值用 SUPPRESS：写了的生效，没写的不会
    # 把根解析器已经解析出来的值覆盖成 None。
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, suppress_defaults=True)

    config_common = argparse.ArgumentParser(add_help=False)
    _add_global_options(config_common, suppress_defaults=True, credentials=False)

    parser = _Parser(
        prog=PROG,
        description="中兴 F50 随身 WiFi 短信命令行工具（只读）",
        epilog=(
            "示例：\n"
            f"  {PROG} list -u                  # 只看未读\n"
            f"  {PROG} list --search 验证码      # 搜索正文\n"
            f"  {PROG} show 57                  # 查看某条详情\n"
            f"  {PROG} thread 10010             # 查看与 10010 的会话\n"
            f"  {PROG} export -f csv -o sms.csv  # 导出全部为 CSV\n"
            f"  {PROG} watch                   # 实时监听新短信\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_global_options(parser)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    # list -----------------------------------------------------------------
    list_parser = subparsers.add_parser(
        "list", aliases=["ls"], parents=[common], help="列出短信（默认命令）",
        description="列出短信，可按未读/号码/关键字/时间过滤。",
    )
    _add_filter_options(list_parser)
    _add_format_option(list_parser, LIST_FORMATS, default=None)

    # show -----------------------------------------------------------------
    show_parser = subparsers.add_parser(
        "show", parents=[common], help="查看单条短信详情",
        description="按 ID 查看短信详情；也可以用 --from 取某个号码的最新一条。",
    )
    show_parser.add_argument("id", nargs="?", help="短信 ID（list 命令里的 ID 列）")
    show_parser.add_argument("--from", dest="from_number", metavar="号码", help="取该号码的最新一条")
    _add_format_option(show_parser, SHOW_FORMATS, default="card")

    # thread ---------------------------------------------------------------
    thread_parser = subparsers.add_parser(
        "thread", aliases=["chat"], parents=[common], help="查看与某个号码的完整会话",
        description="按时间正序列出与某个号码的全部来往短信。",
    )
    thread_parser.add_argument("number", help="号码（可只写后 8 位）")
    _add_format_option(thread_parser, THREAD_FORMATS, default=None)

    # threads --------------------------------------------------------------
    threads_parser = subparsers.add_parser(
        "threads", parents=[common], help="按号码汇总会话",
        description="把所有短信按号码归并，输出会话列表。",
    )
    threads_parser.add_argument("-n", "--limit", type=int, default=0, metavar="N", help="只显示前 N 个会话")
    threads_parser.add_argument("-u", "--unread", action="store_true", help="只显示含未读的会话")
    _add_format_option(threads_parser, THREADS_FORMATS, default=None)

    # count ----------------------------------------------------------------
    count_parser = subparsers.add_parser(
        "count", parents=[common], help="输出未读/总数（方便写脚本）",
        description="默认输出未读数，适合放进状态栏或脚本。",
    )
    count_parser.add_argument("-a", "--all", action="store_true", help="输出总数而不是未读数")
    count_parser.add_argument("--json", action="store_true", help="以 JSON 输出统计信息")

    # status ---------------------------------------------------------------
    status_parser = subparsers.add_parser(
        "status", parents=[common], help="设备 / SIM / 短信存储状态",
        description="显示网络、信号、号码与短信容量等信息。",
    )
    _add_format_option(status_parser, STATUS_FORMATS, default="table")

    # export ---------------------------------------------------------------
    export_parser = subparsers.add_parser(
        "export", parents=[common], help="导出短信为文件",
        description="把短信导出为 JSON / JSONL / CSV / Markdown / 纯文本。",
    )
    _add_filter_options(export_parser)
    _add_format_option(export_parser, ("json", "jsonl", "csv", "markdown", "txt"), default="json")
    export_parser.add_argument("-o", "--out", metavar="FILE", help="输出文件（默认写到 stdout）")
    export_parser.add_argument("--include-raw", action="store_true", help="JSON 中附带路由器原始字段")

    # watch ----------------------------------------------------------------
    watch_parser = subparsers.add_parser(
        "watch", parents=[common], help="轮询并实时显示新短信",
        description="持续轮询，只打印新到达的短信；Ctrl-C 退出。",
    )
    watch_parser.add_argument("-i", "--interval", type=float, default=15.0, metavar="SEC", help="轮询间隔秒数（默认 15）")
    watch_parser.add_argument("--once", action="store_true", help="只检查一次就退出（适合放 cron）")
    watch_parser.add_argument("--backlog", type=int, default=0, metavar="N", help="启动时先显示最近 N 条")

    # config ---------------------------------------------------------------
    config_parser = subparsers.add_parser(
        "config", parents=[config_common], help="查看或保存配置",
        description="配置文件默认在 ~/.config/f50-sms/config.json（权限 600）。",
    )
    config_sub = config_parser.add_subparsers(dest="config_action", metavar="动作")
    config_sub.add_parser("show", parents=[config_common], help="显示当前生效的配置")
    config_sub.add_parser("path", parents=[config_common], help="显示配置文件路径")
    set_parser = config_sub.add_parser("set", parents=[config_common], help="保存配置项")
    set_parser.add_argument("--host", metavar="URL", help="路由器地址")
    set_parser.add_argument(
        "--password", metavar="PW", nargs="?", const=PROMPT_PASSWORD,
        help="后台密码；不写值则交互式输入（不回显）",
    )
    set_parser.add_argument("--timeout", type=float, metavar="SEC", help="超时秒数")
    config_sub.add_parser("unset", parents=[config_common], help="清空配置文件")

    # doctor ---------------------------------------------------------------
    subparsers.add_parser(
        "doctor", parents=[common], help="自检：连通性、登录态、接口可用性",
        description="逐项检查网络、接口和短信功能，出问题时先跑这个。",
    )

    return parser


def _add_global_options(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool = False,
    credentials: bool = True,
) -> None:
    """挂上全局选项。

    根解析器用真实默认值，子解析器用 `argparse.SUPPRESS`。这样
    `f50sms --password X count` 与 `f50sms count --password X` 都成立：
    写在后面的值覆盖前面的，没写的则保留根解析器解析出来的值。
    """

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress_defaults else value

    if credentials:
        parser.add_argument(
            "--host", metavar="URL", default=default(None),
            help="路由器后台地址（默认 http://192.168.0.1）",
        )
        parser.add_argument(
            "--password", metavar="PW", default=default(None),
            help="后台密码（会话失效时需要；也可用 F50_SMS_PASSWORD）",
        )
        parser.add_argument(
            "--timeout", type=float, metavar="SEC", default=default(None),
            help="请求超时秒数（默认 10）",
        )
    parser.add_argument("--config", metavar="FILE", default=default(None), help="配置文件路径")
    parser.add_argument(
        "--color", choices=("auto", "always", "never"), default=default("auto"),
        help="彩色输出策略（默认 auto：仅终端）",
    )
    parser.add_argument(
        "--width", type=int, metavar="N", default=default(None), help="输出宽度，默认取终端宽度",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--pager", dest="pager", action="store_const", const="always", default=default("auto"),
        help="强制用分页器显示（默认内容超过一屏才分页）",
    )
    group.add_argument(
        "--no-pager", dest="pager", action="store_const", const="never", default=default("auto"),
        help="关闭分页，直接输出",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", default=default(False),
        help="不输出汇总行等提示信息",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=default(False),
        help="输出调试信息到 stderr",
    )


def _add_format_option(parser: argparse.ArgumentParser, choices: Sequence[str], default: Optional[str]) -> None:
    parser.add_argument(
        "-f", "--format", dest="format", choices=list(choices), default=default,
        help="输出格式：" + " / ".join(choices),
    )


def _add_filter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-u", "--unread", action="store_true", help="只看未读")
    parser.add_argument(
        "--tag", action="append", metavar="标签",
        help="按状态过滤：未读/已读/已发送/发送失败/草稿，可重复或用逗号分隔",
    )
    parser.add_argument("--from", dest="from_number", metavar="号码", help="只看该号码（支持后 8 位匹配）")
    parser.add_argument("-s", "--search", metavar="关键字", help="正文包含关键字（大小写不敏感）")
    parser.add_argument("--regex", metavar="正则", help="正文匹配正则表达式")
    parser.add_argument("--since", metavar="时间", help="起始时间：2026-10-01 / 2026-10-01 08:00 / 7d / 24h")
    parser.add_argument("--until", metavar="时间", help="结束时间，格式同 --since")
    parser.add_argument("-n", "--limit", type=int, default=0, metavar="N", help="最多显示 N 条（默认全部）")
    parser.add_argument("-r", "--reverse", action="store_true", help="按时间正序（旧的在前）")
    parser.add_argument("--full", action="store_true", help="内容不截断")




def main(argv: Optional[Sequence[str]] = None) -> int:
    _ensure_stdio()
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        argv = ["list"]
    elif _looks_like_filters(argv):
        # f50sms -u / f50sms --search 验证码 等价于省略 list
        argv = ["list"] + argv

    try:
        args = parser.parse_args(argv)
    except SystemExitWithMessage as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except argparse.ArgumentError as exc:  # 互斥选项冲突等
        print(f"{PROG}: 参数错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    out = Output(args)
    settings = resolve_settings(
        host=_opt(args, "host"),
        password=_opt(args, "password"),
        timeout=_opt(args, "timeout"),
        config_file=args.config,
    )

    client = F50Client(settings.host, password=settings.password, timeout=settings.timeout)
    handler = COMMANDS[args.command]
    try:
        return handler(client, args, out)
    except F50AuthError as exc:
        # 只差一个密码时，交互式问一次再重试，不直接失败。
        if not exc.needs_password or client.has_password or not _can_prompt():
            print(f"{PROG}: {exc}", file=sys.stderr)
            return exc.exit_code
        secret = _prompt_password()
        if not secret:
            print(f"{PROG}: 已取消。", file=sys.stderr)
            return exc.exit_code
        client.password = secret
        client.close()
        try:
            code = handler(client, args, out)
        except F50Error as retry_exc:
            print(f"{PROG}: {retry_exc}", file=sys.stderr)
            return retry_exc.exit_code
        out.note(
            "提示：执行 "
            f"\"{PROG} config set --password\" 可以保存密码，之后不必每次输入。"
        )
        return code
    except F50Error as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover - 管道被提前关闭
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    finally:
        client.close()


def _can_prompt() -> bool:
    """能否交互式询问密码：标准输入得是终端，且没被显式禁用。"""

    if os.environ.get("F50_SMS_NO_PROMPT"):
        return False
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # pragma: no cover - 极端环境
        return False


def _prompt_password(prompt: str = "路由器后台密码: ") -> str:
    """不带回显地读取密码；无法交互时返回空串。"""

    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return ""
    except Exception:  # pragma: no cover - 没有可用终端
        return ""


#: 根解析器认识的选项，出现在首位时不应被当成筛选条件。
GLOBAL_FLAGS = frozenset({
    "-h", "--help", "--version",
    "--host", "--password", "--timeout", "--config",
    "--color", "--width", "--pager", "--no-pager",
    "-q", "--quiet", "-v", "--verbose",
})


def _looks_like_filters(argv: Sequence[str]) -> bool:
    """判断首个参数是不是筛选条件，从而省略 list 子命令。

    `f50sms -u`、`f50sms --search 验证码` 都按 list 处理；
    `f50sms --host X count` 保持原样。
    """

    if not argv:
        return False
    first = argv[0]
    if not first.startswith("-"):
        return first not in COMMANDS
    return first.split("=", 1)[0] not in GLOBAL_FLAGS


def _opt(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    """读取可能不存在的参数（`config` 子命令没有 --host/--password/--timeout）。"""

    return getattr(args, name, default)


def _ensure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    # Windows 控制台以外的环境一般无需额外处理。


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
