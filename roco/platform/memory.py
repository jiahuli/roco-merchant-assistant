"""内存占用控制：gc + 工作集修剪。

为什么需要
----------
Python/Tk 进程用过的页面会一直留在工作集里（任务管理器显示的"内存"），
即使数据早已释放。常驻托盘的后台工具不需要这些热页面：
`SetProcessWorkingSetSize(-1, -1)` 请求系统把它们换出——需要时缺页自动换回，
几乎零代价，却能让后台占用显著下降。

调用时机：启动稳定后、每次查询渲染完、隐藏到托盘时。

坑：部分系统的 psapi.dll 只是转发桩（导出表里没有 SetProcessWorkingSetSize），
直接从 kernel32 取（Win7+ 以 K32 前缀导出同名函数）；取不到就只 gc。
"""
from __future__ import annotations

import ctypes
import gc
import os

_trim = None          # 缓存解析出的函数指针；None 表示只 gc
_resolved = False


def _resolve_trim():
    """解析 SetProcessWorkingSetSize：kernel32 → K32 前缀 → psapi。"""
    global _trim, _resolved
    _resolved = True
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        for name in ("SetProcessWorkingSetSize", "K32SetProcessWorkingSetSize"):
            func = getattr(kernel32, name, None)
            if func is not None:
                func.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t)
                func.restype = ctypes.c_int
                _trim = func
                return
        psapi = ctypes.WinDLL("psapi")
        func = getattr(psapi, "SetProcessWorkingSetSize", None)
        if func is not None:
            func.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t)
            func.restype = ctypes.c_int
            _trim = func
    except Exception:  # noqa: BLE001 - 解析失败就退化为只 gc
        _trim = None


def trim_working_set() -> None:
    """gc 回收 + 请求系统修剪工作集（仅 Windows；其他平台只 gc）。"""
    gc.collect()
    if not _resolved:
        _resolve_trim()
    if _trim is None:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        # GetCurrentProcess() 返回伪句柄 -1；MinMax=(-1,-1) 即"尽量清空工作集"
        _trim(-1, -1, -1)
    except Exception:  # noqa: BLE001 - 修剪失败不影响任何功能
        pass
