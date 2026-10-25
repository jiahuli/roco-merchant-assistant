"""单实例控制。

用途：点击系统通知里的「查看货架」时，通过 `rocomesh://` 协议会再次启动本程序。
如果已经有一个实例在跑，理想行为是让**已有窗口**跳到前台，而不是再开一个新窗口。

做法：命名互斥体判断是否已有实例；命名事件用来通知已有实例"该露面了"。
全部基于 kernel32，无需第三方库。任何一步失败都退化为"允许启动"，
不会因为单实例逻辑把用户挡在门外。
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable

MUTEX_NAME = "Local\\RocoMerchantAssistant.SingleInstance"
EVENT_NAME = "Local\\RocoMerchantAssistant.ShowEvent"

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0x00000000

_mutex_handle = None   # 必须持有引用，否则会被 GC 关闭
_event_handle = None   # 同上；主实例创建，供 signal_existing 唤醒
_listener_stop = threading.Event()


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def acquire() -> bool:
    """尝试成为唯一实例；返回 False 表示已有实例在运行。

    成为主实例时顺带创建"露面"事件，这样后续 signal_existing 不会扑空。
    """
    global _mutex_handle, _event_handle
    if os.name != "nt":
        return True
    try:
        kernel32 = _kernel32()
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        kernel32.CreateEventW.restype = wintypes.HANDLE
        _event_handle = kernel32.CreateEventW(None, False, False, EVENT_NAME)
        return True
    except Exception:
        return True


def signal_existing() -> bool:
    """叫醒已经在运行的实例。"""
    if os.name != "nt":
        return False
    try:
        kernel32 = _kernel32()
        kernel32.OpenEventW.restype = wintypes.HANDLE
        kernel32.CreateEventW.restype = wintypes.HANDLE
        handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, EVENT_NAME)
        if not handle:
            handle = kernel32.CreateEventW(None, False, False, EVENT_NAME)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def listen(callback: Callable[[], None], interval_ms: int = 400) -> threading.Thread | None:
    """后台等待"露面"信号；收到后回调（回调在子线程，调用方需自行切回主线程）。"""
    if os.name != "nt" or not _event_handle:
        return None
    kernel32 = _kernel32()
    handle = _event_handle
    _listener_stop.clear()

    def loop() -> None:
        while not _listener_stop.is_set():
            try:
                result = kernel32.WaitForSingleObject(handle, interval_ms)
            except Exception:
                return
            if result == WAIT_OBJECT_0:
                try:
                    callback()
                except Exception:
                    pass

    thread = threading.Thread(target=loop, name="single-instance-listener", daemon=True)
    thread.start()
    return thread


def stop_listener() -> None:
    _listener_stop.set()
