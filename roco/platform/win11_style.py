"""Windows 11 视觉增强：圆角窗口、标题栏明暗、Mica 背景。

全部能力"尽力而为"：Win11 上生效，Win10 / 老构建上 DWM 调用静默失败，
界面照常工作——绝不能因为装饰失败影响功能。

⚠️ 历史教训（别再踩）：曾尝试给窗口加 `WS_EX_COMPOSITED` 双缓冲来消除滚动撕裂，
实测发现它对**可见窗口 + Tk 的 `update()`** 是死锁组合——Tk 的同步几何计算与
DWM 合成管线互相等待，`update()` 永不返回（悬浮窗/日志查看器两处自测卡死实锤）。
Tk 下不要碰窗口扩展样式；撕裂只能靠减少重绘（动画步进、滚动条去重）缓解。

这里的三个 DWM 属性调用都是"设完即忘"的窗口元数据，不参与绘制管线，安全：
1. `DWMWA_WINDOW_CORNER_PREFERENCE`（Win11）：圆角 + 系统投影（阴影层次）。
2. `DWMWA_USE_IMMERSIVE_DARK_MODE`（Win10 20H1+）：标题栏明暗跟随主题。
3. `DWMWA_SYSTEMBACKDROP_TYPE`（Win11 22H2+）：Mica 云母。Tk 客户区不透明，
   材质主要显现在标题栏区——诚实说不是全窗口亚克力，但这是 Tk 无侵入的上限。

字体已是微软雅黑 UI（Win11 中文系统字体）；Fluent 图标字形与被测试断言的文案
（⟳ / ✓ 等）冲突，保持文本按钮不动。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

# DWM 窗口属性号（dwmapi.h）
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19   # Win20H1 预览版
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20       # Win10 20H1+ / Win11
_DWMWA_WINDOW_CORNER_PREFERENCE = 33      # Win11
_DWMWA_SYSTEMBACKDROP_TYPE = 38           # Win11 22H2+
_DWMWCP_ROUND = 2                          # 圆角（系统按 DPI 调半径）
_DWMSBT_MAINWINDOW = 2                     # Mica
_DWMSBT_TRANSIENTWINDOW = 3                # Acrylic

_dwmapi = ctypes.WinDLL("dwmapi")
_user32 = ctypes.WinDLL("user32")


def _root_hwnd(widget) -> int | None:
    """Tk 控件 → 所属顶层 Win32 窗口句柄；取不到返回 None。"""
    try:
        hwnd = widget.winfo_id()
    except Exception:  # noqa: BLE001
        return None
    if not hwnd:
        return None
    root = _user32.GetAncestor(wintypes.HWND(hwnd), _GA_ROOT)
    return root or hwnd


_GA_ROOT = 2


def _set_attr(hwnd: int, attribute: int, value: int) -> bool:
    """DwmSetWindowAttribute 的一次封装；不支持时返回 False。"""
    try:
        result = _dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(attribute),
            ctypes.byref(ctypes.c_int(value)), ctypes.c_uint(4),
        )
        return result == 0
    except Exception:  # noqa: BLE001
        return False


def apply_fluent_style(widget, dark: bool) -> None:
    """圆角窗口（自带系统阴影）+ 标题栏明暗跟随主题。"""
    hwnd = _root_hwnd(widget)
    if not hwnd:
        return
    _set_attr(hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND)
    # 新旧两个属性号都试：20 在正式版生效，19 覆盖个别预览版构建
    if not _set_attr(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0):
        _set_attr(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, 1 if dark else 0)


def apply_mica_backdrop(widget, acrylic: bool = False) -> bool:
    """Mica / Acrylic 背景材质（Win11 22H2+；老系统返回 False 静默跳过）。

    Tk 客户区不透明，材质主要呈现在标题栏区。
    """
    hwnd = _root_hwnd(widget)
    if not hwnd:
        return False
    backdrop = _DWMSBT_TRANSIENTWINDOW if acrylic else _DWMSBT_MAINWINDOW
    return _set_attr(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, backdrop)


def style_toplevel(widget, dark: bool = True, mica: bool = False,
                   acrylic: bool = False) -> None:
    """一站式：圆角 + 标题栏明暗（+ 材质）。任何一步失败都不影响功能。"""
    apply_fluent_style(widget, dark)
    if mica or acrylic:
        apply_mica_backdrop(widget, acrylic=acrylic)
