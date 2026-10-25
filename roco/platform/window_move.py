"""拖动手势的平台相关常量（拖动阈值 / 双击间隔）。

⚠️ 这里曾经提供 `begin_drag()`：`ReleaseCapture()` + `SendMessageW(WM_NCLBUTTONDOWN, HTCAPTION)`
把拖动交给 Windows 的系统移动循环。1.9.1 就是用它治拖动撕裂的，**1.9.2 已彻底删除**，
因为它会让进程随机崩溃（0xc0000409 / abort），原因如下，别再把它加回来：

1. `SendMessageW` 是**阻塞**调用，ctypes 在进入它之前会 `Py_BEGIN_ALLOW_THREADS`
   （释放 GIL，当前线程的 Python 线程状态被置空）。
2. 系统移动循环在自己的模态消息循环里 `DispatchMessage`，于是 `WM_MOUSEMOVE` 被
   **同步**派发回本窗口的窗口过程；Tk 的窗口过程会就地调用 Python 绑定
   （`<B1-Motion>` → `_drag_move`）。
3. 此时该线程**没有 Python 线程状态**，重入 Python 时 CPython 直接
   `Py_FatalError`：
   `PyEval_RestoreThread: the function must be called with the GIL held ...`
   → `abort()` → 进程无 traceback、无退出日志地消失。

实测（真实悬浮窗 + 合成鼠标拖动，5 次里 3 次）：崩溃栈停在
`floating_window._system_drag` → `window_move.begin_drag` 的 `SendMessageW` 里；
用户侧表现正是"按住标题栏一动就卡住，然后程序没了"。

推论（对本项目通用）：**任何 ctypes 的阻塞式 Win32 调用，只要它会同步回调窗口过程、
进而可能进入 Python（SendMessage / RedrawWindow RDW_UPDATENOW / 系统模态循环），
都不能在 Tk 事件回调里用。** 搬窗口请走 Tk 自己的 `wm geometry`（异步排队、
无阻塞调用），并按帧合并——见 `floating_window._queue_move`。
"""
from __future__ import annotations

import ctypes
import os

_SM_CXDRAG = 68
_SM_CYDRAG = 69

_supported = os.name == "nt"
_user32 = ctypes.WinDLL("user32") if _supported else None


def _declare_types() -> None:
    """ctypes 不声明 argtypes 会按 c_int 截断返回值（项目踩过的坑）。"""
    if _user32 is None:
        return
    _user32.GetDoubleClickTime.argtypes = []
    _user32.GetDoubleClickTime.restype = ctypes.c_uint
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int


try:
    _declare_types()
except Exception:  # noqa: BLE001 - 声明失败只影响拖动阈值，不该让程序起不来
    _user32 = None


def drag_threshold() -> int:
    """走过多少像素才算"开始拖"，而不是点击 / 双击。

    取系统的拖动阈值（默认 4px）：小于它就不动窗口，
    这样标题栏的单击与双击（打开主窗口）都还能正常触发。
    """
    if _user32 is None:
        return 4
    try:
        return max(2, int(_user32.GetSystemMetrics(_SM_CXDRAG)) or 4,
                   int(_user32.GetSystemMetrics(_SM_CYDRAG)) or 4)
    except Exception:  # noqa: BLE001
        return 4


def double_click_ms() -> int:
    """系统双击间隔（毫秒），给需要自行判定双击的地方用。"""
    if _user32 is None:
        return 500
    try:
        return int(_user32.GetDoubleClickTime()) or 500
    except Exception:  # noqa: BLE001
        return 500
