"""日志与错误捕获。

为什么需要
----------
打包成无控制台的 exe 后，`print` / 未捕获异常的输出全部消失，用户只会看到
"点了没反应"。所以这里把三类异常都接住：

1. `sys.excepthook`            —— 主线程未捕获异常
2. `threading.excepthook`      —— 子线程未捕获异常（托盘、图标下载都在子线程）
3. `Tk.report_callback_exception` —— 控件回调里的异常（Tk 默认只往 stderr 打）

**还有第四类**：C 层硬崩溃（栈溢出、CRT `abort()`、段错误、`Py_FatalError`）。
它们不经过任何 Python 钩子，进程会**没有 traceback、没有退出日志**地消失，
现场只剩系统事件日志里的模块名与异常码（例如 1.9.1 的
`ucrtbase.dll + 0xc0000409`）。这种场景由 `install_crash_log()` 兜住：
把 `faulthandler` 挂到 SIGSEGV/SIGABRT/SIGFPE 等信号上，崩溃瞬间把**所有线程的
Python 栈**直接写进 `crash.log`——那是这类问题唯一的 Python 侧证据。

日志位置
--------
默认 `%APPDATA%\\RocoMerchant\\logs\\app.log`，可在设置里改（`log_dir`）。
单文件滚动：单份 1 MB、保留 3 份，避免无限增长。
崩溃现场另存 `crash.log`（追加，超过 256 KB 时归档为 `crash.log.1` 重来）。
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from pathlib import Path

LOGGER_NAME = "roco"
LOG_FILENAME = "app.log"
CRASH_FILENAME = "crash.log"
_MAX_BYTES = 1024 * 1024
_BACKUP_COUNT = 3
_MAX_CRASH_BYTES = 256 * 1024

_handler: logging.Handler | None = None
_current_dir: Path | None = None
_installed = False
_crash_stream = None


def default_log_dir() -> Path:
    from roco.config import app_dir

    path = app_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_log_dir(config=None) -> tuple[Path, str]:
    """按配置解析日志目录，返回 (目录, 提示)。

    目录不可写时回退默认位置，绝不因为日志写不了而影响主流程。
    """
    fallback = default_log_dir()
    raw = ""
    if config is not None:
        raw = (getattr(config, "log_dir", "") or "").strip()
    if not raw:
        return fallback, ""
    try:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            from roco.config import app_dir

            path = app_dir() / path
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        try:
            probe.unlink()
        except OSError:
            pass
        return path, ""
    except (OSError, ValueError) as exc:
        return fallback, f"日志目录不可用（{type(exc).__name__}），已回退默认位置"


def log_file(config=None) -> Path:
    folder = _current_dir or resolve_log_dir(config)[0]
    return folder / LOG_FILENAME


def crash_file(config=None) -> Path:
    """崩溃现场文件路径（与 app.log 同目录）。"""
    folder = _current_dir or resolve_log_dir(config)[0]
    return folder / CRASH_FILENAME


def install_crash_log(config=None) -> Path | None:
    """把 `faulthandler` 的输出接到 `logs/crash.log`（幂等），返回文件路径。

    只处理**C 层硬崩溃**：这类死亡不经过 `sys.excepthook` / Tk 钩子，所以 app.log
    里什么都留不下。装上之后，SIGSEGV / SIGABRT / SIGFPE / SIGILL / SIGBUS 触发时，
    所有线程的 Python 栈会被直接写进文件。

    三条实现要求（都踩过）：
    * 文件句柄必须**一直持有**到进程结束——关掉后 faulthandler 会写到失效的 fd 上；
    * 用追加模式，并在每次启动时写一行分隔头，才能分清是哪一次崩溃；
    * 超过 256 KB 先归档成 `crash.log.1`，不让它无限长（单实例下也不会常崩）。
    """
    global _crash_stream

    path = crash_file(config)
    if _crash_stream is not None:
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _MAX_CRASH_BYTES:
            backup = path.with_name(path.name + ".1")
            try:
                os.replace(path, backup)
            except OSError:
                pass
        stream = open(path, "a", encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        stream.write(
            f"\n===== 崩溃现场会话 | {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"| 版本 {_version()} | 进程 {os.getpid()} =====\n"
        )
        stream.flush()
        faulthandler.enable(file=stream, all_threads=True)
    except Exception:  # noqa: BLE001 - 装不上也不能影响主流程
        try:
            stream.close()
        except OSError:
            pass
        return None
    _crash_stream = stream
    return path


def crash_stream():
    """当前崩溃日志的写入流（测试用；未安装时为 None）。"""
    return _crash_stream


def setup(config=None, level: int = logging.INFO) -> Path:
    """初始化日志（幂等）。返回日志文件路径。"""
    global _handler, _current_dir, _installed

    folder, _warning = resolve_log_dir(config)
    _current_dir = folder
    path = folder / LOG_FILENAME

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if _handler is not None:
        logger.removeHandler(_handler)
        try:
            _handler.close()
        except Exception:
            pass

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    # 文本形式固定，方便"只看错误"按等级过滤
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    _handler = handler

    if not _installed:
        _install_excepthooks()
        _installed = True

    logger.info("=" * 60)
    logger.info("日志已启动 | 版本 %s | 进程 %s", _version(), os.getpid())
    logger.info("日志文件 %s", path)
    return path


def _version() -> str:
    try:
        from roco.config import APP_VERSION

        return APP_VERSION
    except Exception:
        return "unknown"


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def log_error(message: str, exc: BaseException | None = None) -> None:
    logger = get_logger()
    if exc is not None:
        logger.error(message, exc_info=(type(exc), exc, exc.__traceback__))
    else:
        logger.error(message)


def _format_exception(exc_type, exc_value, exc_tb) -> str:
    return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))


def _install_excepthooks() -> None:
    previous_hook = sys.excepthook

    def handle_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc_value, exc_tb)
            return
        get_logger().error("未捕获异常:\n%s", _format_exception(exc_type, exc_value, exc_tb))
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = handle_exception

    def handle_thread(args):
        get_logger().error(
            "线程 %s 未捕获异常:\n%s",
            getattr(args.thread, "name", "?"),
            _format_exception(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = handle_thread


def install_tk_hook(root, notify=None) -> None:
    """接管 Tk 回调异常：默认只打印到 stderr，打包后等于静默丢失。"""

    def handler(exc_type, exc_value, exc_tb):
        text = _format_exception(exc_type, exc_value, exc_tb)
        get_logger().error("界面回调异常:\n%s", text)
        if notify is not None:
            try:
                notify(exc_type.__name__, str(exc_value))
            except Exception:
                pass

    try:
        root.report_callback_exception = handler
    except Exception:
        pass


def tail(max_lines: int = 600, errors_only: bool = False) -> str:
    """读取日志尾部，供内置查看器使用。"""
    path = log_file()
    if not path.exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        return f"（读取日志失败：{exc}）"

    if errors_only:
        # 保留 ERROR/WARNING 行及其后的 traceback 片段
        kept: list[str] = []
        keeping = False
        for line in lines:
            upper = line[20:28] if len(line) > 28 else line
            is_head = " ERROR " in upper or " WARNING " in upper
            if is_head:
                keeping = True
                kept.append(line)
            elif keeping and (line.startswith(" ") or line.startswith("Traceback")
                              or line.startswith("  ")):
                kept.append(line)
            elif line.startswith("20") and len(line) > 20 and line[19] == " ":
                keeping = False
        lines = kept or lines[-1:]

    return "".join(lines[-max_lines:])


def info(message: str, *args) -> None:
    get_logger().info(message, *args)


def warning(message: str, *args) -> None:
    get_logger().warning(message, *args)


def error(message: str, *args) -> None:
    get_logger().error(message, *args)
