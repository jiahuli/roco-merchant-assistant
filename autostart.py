"""开机自启动（HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run）。

只写当前用户注册表，不需要管理员权限，随时可关。
写入的是"启动命令"而不是快捷方式，因此源码运行与 exe 运行都适用：
    exe  模式： "D:\\Apps\\远行商人助手.exe" --autostart
    源码 模式： "C:\\Python314\\pythonw.exe" "D:\\...\\main.py" --autostart
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RocoMerchantAssistant"
AUTOSTART_FLAG = "--autostart"


def is_supported() -> bool:
    return os.name == "nt"


def launch_command() -> str:
    """当前程序的自启动命令行（带引号）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {AUTOSTART_FLAG}'

    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe  # pythonw 不会闪黑框
    script = Path(__file__).resolve().parent / "main.py"
    return f'"{interpreter}" "{script}" {AUTOSTART_FLAG}'


def _open_run_key(writable: bool):
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_WRITE if writable else 0)
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def current_command() -> str:
    """注册表里当前登记的命令，没有则返回空串。"""
    if not is_supported():
        return ""
    import winreg

    try:
        with _open_run_key(writable=False) as key:
            value, _type = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except (FileNotFoundError, OSError):
        return ""


def is_enabled() -> bool:
    return bool(current_command().strip())


def enable() -> tuple[bool, str]:
    if not is_supported():
        return False, "当前系统不支持开机自启（仅 Windows）"
    import winreg

    try:
        with _open_run_key(writable=True) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
        return True, "已开启开机自启"
    except OSError as exc:
        return False, f"写入注册表失败：{exc}"


def disable() -> tuple[bool, str]:
    if not is_supported():
        return False, "当前系统不支持开机自启（仅 Windows）"
    import winreg

    try:
        with _open_run_key(writable=True) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True, "已关闭开机自启"
    except FileNotFoundError:
        return True, "开机自启本来就是关闭状态"
    except OSError as exc:
        return False, f"删除注册表项失败：{exc}"


def sync(enabled: bool) -> tuple[bool, str]:
    """让注册表状态与配置一致；命令路径变了也会重写。"""
    if enabled:
        if current_command().strip() == launch_command():
            return True, "开机自启已生效"
        return enable()
    if is_enabled():
        return disable()
    return True, "开机自启未开启"


def is_autostart_launch(argv: list[str] | None = None) -> bool:
    args = argv if argv is not None else sys.argv
    return AUTOSTART_FLAG in args
