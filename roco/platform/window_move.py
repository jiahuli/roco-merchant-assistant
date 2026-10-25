"""把窗口拖动交给 Windows 的系统移动循环。

为什么不能自己搬
----------------
`<B1-Motion>` 是按**输入事件**触发的：高回报率鼠标每秒能送来数百个移动事件。
而 Tk 的每一层控件都是独立的 Win32 窗口，`wm geometry` 把整窗搬一次要重排、
重绘所有子窗口。本机实测（25 个子控件、2560×1440@200Hz）单次移动 + 事件泵
约 **9.6 ms**，而 200Hz 的帧预算只有 **5 ms**——一个事件还没画完，下几个又到了。
结果是同一帧里父窗口已经移到新位置、子控件还停在旧位置，合成出来就是
"撕开"的半成品画面（画面撕裂）。

做法
----
发一条 `WM_NCLBUTTONDOWN(HTCAPTION)`，把拖动**交还给系统**：系统移动循环搬的
是整个窗口（父子窗口原子移动），应用一次绘制都不参与，因此与刷新率彻底解耦。

两个必须注意的点
----------------
1. `ReleaseCapture()` 要先调。Tk 在 `<ButtonPress-1>` 里已经 `SetCapture`，
   不释放的话系统移动循环收不到鼠标消息，窗口会被"粘住"。
2. 这个调用是**同步阻塞**的：系统在自己的消息循环里跑到松手才返回。
   这是有意设计——拖动期间本来就不需要应用绘制；返回后我们再做边界校正与落盘。

非 Windows 或缺句柄时 `begin_drag()` 返回 False，调用方应回落到节流版的手动拖动。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2
_GA_ROOT = 2
_SM_CXDRAG = 68
_SM_CYDRAG = 69

_supported = os.name == "nt"
_user32 = ctypes.WinDLL("user32") if _supported else None


def _declare_types() -> None:
    """ctypes 不声明 argtypes 会按 c_int 截断 64 位句柄（项目踩过的坑）。"""
    if _user32 is None:
        return
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.ReleaseCapture.argtypes = []
    _user32.ReleaseCapture.restype = wintypes.BOOL
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
    _user32.SendMessageW.restype = wintypes.LPARAM
    _user32.GetDoubleClickTime.argtypes = []
    _user32.GetDoubleClickTime.restype = wintypes.UINT
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int


try:
    _declare_types()
except Exception:  # noqa: BLE001 - 声明失败只影响拖动，不该让程序起不来
    _user32 = None


def supported() -> bool:
    """当前平台能否把拖动交给系统。"""
    return _user32 is not None


def hwnd_of(widget) -> int | None:
    """Tk 控件 → 所属顶层窗口句柄；取不到返回 None。"""
    if _user32 is None:
        return None
    try:
        hwnd = widget.winfo_id()
    except Exception:  # noqa: BLE001 - 窗口已销毁
        return None
    if not hwnd:
        return None
    try:
        root = _user32.GetAncestor(wintypes.HWND(hwnd), _GA_ROOT)
    except Exception:  # noqa: BLE001
        return None
    return int(root or hwnd) or None


def begin_drag(widget) -> bool:
    """进入系统移动循环；松手后返回。

    返回 False 表示没能交给系统（非 Windows / 句柄取不到 / 调用异常），
    调用方应回落到自己的拖动实现。返回 True 只代表"调用发生了"，
    是否真的移动了要看调用后的窗口坐标。
    """
    if _user32 is None:
        return False
    hwnd = hwnd_of(widget)
    if not hwnd:
        return False
    try:
        _user32.ReleaseCapture()
        _user32.SendMessageW(wintypes.HWND(hwnd), _WM_NCLBUTTONDOWN, _HTCAPTION, 0)
        return True
    except Exception:  # noqa: BLE001 - 系统循环异常也不能让界面卡死
        return False


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
