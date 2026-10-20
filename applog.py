"""日志与错误捕获。

为什么需要
----------
打包成无控制台的 exe 后，`print` / 未捕获异常的输出全部消失，用户只会看到
"点了没反应"。所以这里把日志落盘，并把三类异常都接住：

1. `sys.excepthook`            —— 主线程未捕获异常
2. `threading.excepthook`      —— 子线程未捕获异常（托盘、图标下载都在子线程）
3. `Tk.report_callback_exception` —— 控件回调里的异常（Tk 默认只往 stderr 打）

日志位置
--------
默认 `%APPDATA%\\RocoMerchant\\logs\\app.log`，可在设置里改（`log_dir`）。
单文件滚动：单份 1 MB、保留 3 份，避免无限增长。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path

LOGGER_NAME = "roco"
LOG_FILENAME = "app.log"
_MAX_BYTES = 1024 * 1024
_BACKUP_COUNT = 3

_handler: logging.Handler | None = None
_current_dir: Path | None = None
_installed = False


def default_log_dir() -> Path:
    from config import app_dir

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
            from config import app_dir

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
        from config import APP_VERSION

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
