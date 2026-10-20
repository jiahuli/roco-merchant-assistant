"""多显示器边界计算。

为什么需要这一层
----------------
`tkinter` 的 `winfo_screenwidth()` / `winfo_screenheight()` 只返回**主显示器**尺寸，
用它来夹取窗口坐标会导致：

* 副屏在主屏右侧时，窗口拖到 x > 主屏宽度就被拉回来；
* 副屏在主屏左侧时坐标是负数，会被"坐标非法"的判断直接拒绝；
* 存下来的位置在重启后因为"超出主屏"被丢弃，回不到副屏。

因此这里用 Win32 `EnumDisplayMonitors` 拿到每块屏的**物理矩形**与**工作区**，
再做纯函数式的边界运算（方便单测：函数都接受 monitors 列表，可注入合成数据）。

边界策略刻意与 Windows 原生一致：拖动时只按**虚拟桌面**范围夹取（允许跨屏、
允许跨越显示器边界、允许停在多屏高度不一致产生的空白处），**不做任何吸附**，
松手就停在原地。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

# 拖拽时至少保留多少像素在可见区域内，避免窗口被拖丢
MIN_VISIBLE = 80


@dataclass(frozen=True)
class Monitor:
    """一块显示器：物理矩形 + 去掉任务栏后的工作区。"""

    x: int
    y: int
    width: int
    height: int
    work_x: int
    work_y: int
    work_width: int
    work_height: int
    primary: bool = False

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def work_right(self) -> int:
        return self.work_x + self.work_width

    @property
    def work_bottom(self) -> int:
        return self.work_y + self.work_height

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(_RECT), wintypes.LPARAM
)

_MONITORINFOF_PRIMARY = 0x1


def primary_fallback() -> list[Monitor]:
    """拿不到真实显示器列表时的兜底（按 Tk 的主屏尺寸估算）。"""
    width, height = 1920, 1080
    try:
        user32 = ctypes.windll.user32
        width = user32.GetSystemMetrics(0) or width
        height = user32.GetSystemMetrics(1) or height
    except Exception:
        pass
    return [Monitor(0, 0, width, height, 0, 0, width, max(1, height - 40), True)]


def enum_monitors() -> list[Monitor]:
    """枚举所有显示器；任何异常都退化为单屏。"""
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return primary_fallback()

    found: list[Monitor] = []

    def _callback(hmon, hdc, rect_ptr, data):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            monitor = info.rcMonitor
            work = info.rcWork
            found.append(
                Monitor(
                    x=monitor.left, y=monitor.top,
                    width=monitor.right - monitor.left,
                    height=monitor.bottom - monitor.top,
                    work_x=work.left, work_y=work.top,
                    work_width=work.right - work.left,
                    work_height=work.bottom - work.top,
                    primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                )
            )
        return True

    try:
        user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_callback), 0)
    except Exception:
        return primary_fallback()

    return found or primary_fallback()


# ---------------------------------------------------------------------------
# 纯函数：便于单测
# ---------------------------------------------------------------------------
def virtual_bounds(monitors: list[Monitor]) -> tuple[int, int, int, int]:
    """所有显示器合成的虚拟桌面 (x, y, width, height)。"""
    if not monitors:
        return 0, 0, 0, 0
    left = min(m.x for m in monitors)
    top = min(m.y for m in monitors)
    right = max(m.right for m in monitors)
    bottom = max(m.bottom for m in monitors)
    return left, top, right - left, bottom - top


def primary_monitor(monitors: list[Monitor]) -> Monitor:
    for monitor in monitors:
        if monitor.primary:
            return monitor
    return monitors[0] if monitors else primary_fallback()[0]


def monitor_at(monitors: list[Monitor], x: int, y: int) -> Monitor | None:
    for monitor in monitors:
        if monitor.contains(x, y):
            return monitor
    return None


def _overlap_area(monitor: Monitor, x: int, y: int, width: int, height: int) -> int:
    left = max(x, monitor.x)
    top = max(y, monitor.y)
    right = min(x + width, monitor.right)
    bottom = min(y + height, monitor.bottom)
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def best_monitor(monitors: list[Monitor], x: int, y: int,
                 width: int, height: int) -> Monitor:
    """与给定矩形重叠面积最大的显示器；完全无重叠时取中心最近的。"""
    if not monitors:
        return primary_fallback()[0]
    best = max(monitors, key=lambda m: _overlap_area(m, x, y, width, height))
    if _overlap_area(best, x, y, width, height) > 0:
        return best
    center_x, center_y = x + width // 2, y + height // 2
    return min(
        monitors,
        key=lambda m: (m.x + m.width // 2 - center_x) ** 2 + (m.y + m.height // 2 - center_y) ** 2,
    )


def clamp_drag(monitors: list[Monitor], x: int, y: int, width: int, height: int,
               min_visible: int = MIN_VISIBLE) -> tuple[int, int]:
    """拖动过程中的坐标约束，行为对齐 Windows 原生：

    窗口可以在虚拟桌面范围内自由移动、可以跨越两块屏的边界，
    也不会被吸附到某块屏上；只保留 min_visible 像素可见，避免完全拖丢。
    """
    vx, vy, vw, vh = virtual_bounds(monitors)
    if vw <= 0 or vh <= 0:
        return x, y
    keep_x = min(min_visible, max(24, width // 2))
    keep_y = min(min_visible, max(24, height // 4))
    low_x = vx - (width - keep_x)
    high_x = vx + vw - keep_x
    low_y = vy - (height - keep_y)
    high_y = vy + vh - keep_y
    return max(low_x, min(high_x, x)), max(low_y, min(high_y, y))


def is_visible_on(monitors: list[Monitor], x: int, y: int, width: int, height: int,
                  min_ratio: float = 0.4) -> bool:
    """这块位置是否有足够的面积落在某块显示器上（用于校验持久化的位置）。"""
    if not monitors:
        return True
    rectangle_area = max(1, width * height)
    return any(_overlap_area(m, x, y, width, height) >= rectangle_area * min_ratio
               for m in monitors)


def default_position(monitors: list[Monitor], width: int,
                     height: int) -> tuple[int, int]:
    """默认位置：主屏工作区右上角，留 32/24 边距。"""
    monitor = primary_monitor(monitors)
    x = max(monitor.work_x, monitor.work_right - width - 32)
    y = monitor.work_y + 24
    return x, y
