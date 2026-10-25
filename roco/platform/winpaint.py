"""批量构建时冻结重绘，让一大片新控件**一次性**出现在屏幕上。

为什么需要
----------
Tk 每个控件都是独立的 Win32 子窗口，`grid`/`pack` 之后由 Windows 逐个发送
`WM_PAINT`。实测本机布局成本约 **1.4ms/控件**（连空 Frame 都一样），所以
20 张商品卡片（约 260 个控件）光是"算位置 + 首次绘制"就要 400ms 上下。
这段时间里 Windows 会把"已画好的那部分"逐帧推上屏：用户先看到一堆空白卡片
边框，随后文字一块块出现、图标再一块块补上——就是历史面板展开时那种
"分步显示 + 闪烁"。

做法
----
构建前给顶层窗口发 `WM_SETREDRAW(FALSE)`，Windows 会把该窗口（含子窗口）的
绘制请求压住；构建与布局照常进行、时间照付，但一个像素都不上屏。构建完成后
`WM_SETREDRAW(TRUE)` + `RedrawWindow(RDW_INVALIDATE|RDW_ERASE|RDW_ALLCHILDREN|
RDW_UPDATENOW)`，一次把整棵控件树画出来。

实测（20 张卡片、本机 2560×1440@200Hz）：不冻结 456ms（其中首绘 46ms，且分散在
多帧里）；冻结后 271ms，解冻那一帧 16ms —— 用户只看到"展开前"和"展开后"两个状态。

⚠️ 这不是 `WS_EX_COMPOSITED`
---------------------------
坑表里那条死锁是"给可见窗口设**扩展样式** `WS_EX_COMPOSITED`，DWM 合成管线与
Tk 的同步几何计算互相等待，`update()` 永不返回"。`WM_SETREDRAW` 只是让窗口
丢弃绘制消息，不碰合成管线；实测冻结期间 `update()` 依旧 0.2ms 返回。
即便如此，仍然：只在**一次构建**的范围内使用、必须 `try/finally` 恢复，
绝不在里面跑事件循环或等待网络。
"""
from __future__ import annotations

import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes

_WM_SETREDRAW = 0x000B
_GA_ROOT = 2
_RDW_INVALIDATE = 0x0001
_RDW_ERASE = 0x0004
_RDW_ALLCHILDREN = 0x0080
_RDW_UPDATENOW = 0x0100

_user32 = ctypes.WinDLL("user32") if os.name == "nt" else None
# 同一次构建可能出现嵌套（展开→重建里又包一层）：用深度计数，只有最外层才真正解冻
_depths: dict[int, int] = {}


def _declare_types() -> None:
    if _user32 is None:
        return
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
    _user32.SendMessageW.restype = wintypes.LPARAM
    _user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                     ctypes.c_void_p, wintypes.UINT]
    _user32.RedrawWindow.restype = wintypes.BOOL


try:
    _declare_types()
except Exception:  # noqa: BLE001 - 声明失败只是退化成"不冻结"，不影响功能
    _user32 = None


def supported() -> bool:
    return _user32 is not None


def toplevel_hwnd(widget) -> int | None:
    """控件所属顶层窗口句柄；取不到返回 None。"""
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


@contextmanager
def frozen_redraw(widget):
    """在这个上下文里冻结整窗重绘，退出时一次性画完。

    可嵌套：只有最外层退出时才恢复重绘（否则内层先解冻，外层还在建控件，
    又会看到半成品）。非 Windows 或拿不到句柄时退化为空操作。
    """
    hwnd = toplevel_hwnd(widget)
    if hwnd is None:
        yield
        return
    depth = _depths.get(hwnd, 0)
    if depth == 0:
        try:
            _user32.SendMessageW(wintypes.HWND(hwnd), _WM_SETREDRAW, 0, 0)
        except Exception:  # noqa: BLE001 - 冻结失败就当没冻结，照常构建
            yield
            return
    _depths[hwnd] = depth + 1
    try:
        yield
    finally:
        depth = _depths.get(hwnd, 1) - 1
        if depth > 0:
            _depths[hwnd] = depth
            return
        _depths.pop(hwnd, None)
        # 无论构建是否抛异常，都必须恢复，否则窗口会一直不重绘（假死）
        try:
            _user32.SendMessageW(wintypes.HWND(hwnd), _WM_SETREDRAW, 1, 0)
            _user32.RedrawWindow(
                wintypes.HWND(hwnd), None, None,
                _RDW_INVALIDATE | _RDW_ERASE | _RDW_ALLCHILDREN | _RDW_UPDATENOW,
            )
        except Exception:  # noqa: BLE001
            pass


def frozen_depth(widget) -> int:
    """当前冻结层数（自测用：确认嵌套只在最外层解冻）。"""
    hwnd = toplevel_hwnd(widget)
    return _depths.get(hwnd, 0) if hwnd else 0
