"""Windows 主题变更推送监听（替代轮询）。

原理
----
`RegNotifyChangeKeyValue` 对 `HKCU\\...\\Themes\\Personalize` 键挂一次异步通知：
系统改「应用模式」（或该键下任何值）时会触发事件，线程醒来后比对
`AppsUseLightTheme` 的真实取值——**只有真的翻转了才回调**，壁纸等无关变更被过滤。
通知是一次性的，醒来后重新挂一次，循环往复。

回调发生在监听线程：调用方必须自己投递回主线程（本项目走既有结果泵的队列）。
相比每 8 秒读一次注册表的轮询：空闲零唤醒、改主题即时生效。
"""
from __future__ import annotations

import ctypes
import threading
import winreg
from typing import Callable, Optional

_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_VALUE = "AppsUseLightTheme"

_REG_NOTIFY_CHANGE_LAST_SET = 0x00000004
_KEY_NOTIFY = 0x0004  # KEY_QUERY_VALUE | KEY_NOTIFY
_INFINITE = 0xFFFFFFFF

_advapi32 = ctypes.WinDLL("advapi32")
_kernel32 = ctypes.WinDLL("kernel32")

_RegOpenKeyExW = _advapi32.RegOpenKeyExW
_RegNotifyChangeKeyValue = _advapi32.RegNotifyChangeKeyValue
_RegCloseKey = _advapi32.RegCloseKey
_CreateEventW = _kernel32.CreateEventW
_WaitForSingleObjectEx = _kernel32.WaitForSingleObjectEx
_CloseHandle = _kernel32.CloseHandle
_SetEvent = _kernel32.SetEvent


def _prefers_light() -> Optional[bool]:
    """读当前「应用模式」；读不到返回 None（保持上次判断，不误触发）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE) as key:
            value, _type = winreg.QueryValueEx(key, _VALUE)
            return bool(value)
    except OSError:
        return None


class ThemeChangeListener:
    """后台线程挂注册表通知；`AppsUseLightTheme` 真实变化时调用 `on_change`。"""

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._thread: Optional[threading.Thread] = None
        self._stop_event = None       # Win32 event，用于优雅停止
        self._reg_handle = None
        self._last_light: Optional[bool] = _prefers_light()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event = _CreateEventW(None, True, False, None)
        self._thread = threading.Thread(
            target=self._listen, name="theme-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """触发停止事件并等待线程退出（阻塞至多 2 秒）。"""
        if not self._started:
            return
        self._started = False
        if self._stop_event:
            _SetEvent(self._stop_event)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    # ------------------------------------------------------------------ 线程
    def _listen(self) -> None:
        while self._started:
            key = self._open_key()
            if key is None:
                # 打不开键（权限/策略）：退避后重试，绝不让监听线程带崩进程
                if self._wait_stop(30.0):
                    return
                continue
            self._reg_handle = key
            event = _CreateEventW(None, False, False, None)  # auto-reset
            armed = _RegNotifyChangeKeyValue(
                ctypes.c_void_p(key), False, _REG_NOTIFY_CHANGE_LAST_SET,
                ctypes.c_void_p(event), True,
            )
            if not armed:
                _RegCloseKey(key)
                self._reg_handle = None
                _CloseHandle(event)
                if self._wait_stop(30.0):
                    return
                continue

            # 同时等"注册表变化"和"停止信号"
            handles = (ctypes.c_void_p * 2)(ctypes.c_void_p(event),
                                            ctypes.c_void_p(self._stop_event))
            wait_result = _WaitForSingleObjectEx(handles, _INFINITE, False)

            _CloseHandle(event)
            _RegCloseKey(key)
            self._reg_handle = None

            if wait_result != 0:            # 0 = WAIT_OBJECT_0（注册表变了）
                return                       # 停止信号或等待失败 → 退出线程
            if not self._started:
                return

            if self._value_really_changed():
                try:
                    self._on_change()
                except Exception:            # noqa: BLE001 - 回调异常不拖垮监听
                    pass

    def _value_really_changed(self) -> bool:
        """通知可能由壁纸等无关变更触发：只有 AppsUseLightTheme 真翻转才回调。"""
        current = _prefers_light()
        if current is None or current == self._last_light:
            return False
        self._last_light = current
        return True

    def _open_key(self) -> Optional[int]:
        handle = ctypes.c_void_p()
        if _RegOpenKeyExW(
            0x80000001,                       # HKEY_CURRENT_USER
            _PERSONALIZE, 0,
            _KEY_NOTIFY, ctypes.byref(handle),
        ) == 0:
            return handle.value
        return None

    def _wait_stop(self, seconds: float) -> bool:
        """等待停止信号；返回 True 表示该退出了。"""
        result = _WaitForSingleObjectEx(
            ctypes.c_void_p(self._stop_event), int(seconds * 1000), False)
        return result == 0                    # 0 = 停止事件被触发
