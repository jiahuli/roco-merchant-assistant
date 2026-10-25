"""帧节拍动画：把"每帧走固定比例"换成"给定时长、按真实帧率取帧"。

1.9 的动画是**按帧数**写的：`after(24)` 里走 35% 剩余距离、`after(15)` 里滚
0.045 个视口。这类写法的隐含假设是"每帧都是 15ms"，于是：

* 帧率被写死在 ~65fps / ~42fps，200Hz 屏上白白浪费；
* 只要有人改动节拍数字，**动画速度就跟着变**（改成 5ms 会快三倍）；
* 高刷屏上每帧的真实间隔还会抖，观感是"一顿一顿"而不是"丝滑"。

这里改成"给时长"：动画声明自己该跑多少毫秒（悬停 90ms、描边 120ms），
每一帧用 `perf_counter()` 的真实经过时间算进度，帧率高低只影响中间画几帧，
不影响快慢。帧间隔取 `display.frame_ms()`（真实刷新率，**上限 90fps**），
并在动画存续期间持有 1ms 定时器精度——否则 Windows 默认 15.6ms 的定时器粒度
会让 `after(11)` 变成 `after(15.6)`，高刷屏上等于没提升。

生命周期
--------
`Animation` 绑定控件的 `<Destroy>`，控件被销毁时自动停表。这条不是洁癖：
1.9 的悬停动画 job 从不取消，切主题重建控件树 / 关对话框时就会对已销毁的
控件调用 `after`，异常被 Tk 吞掉，表现成"界面卡住"或莫名的"界面出错"提示。
"""
from __future__ import annotations

import time
import tkinter as tk
from typing import Callable, Optional

from roco.platform import display


def frame_ms() -> int:
    """当前显示器的一帧毫秒数（夹取后的整数）。"""
    return display.frame_ms()


# ------------------------------------------------------------------ 缓动
def linear(t: float) -> float:
    return t


def ease_out_quad(t: float) -> float:
    return 1.0 - (1.0 - t) ** 2


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    if t < 0.5:
        return 4.0 * t ** 3
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


# ------------------------------------------------------------------ 一次性补间
class Animation:
    """一个控件同一时刻只有一条补间：重复触发会从当前值重新起算。"""

    def __init__(self, widget: tk.Misc) -> None:
        self.widget = widget
        self._job: Optional[str] = None
        self._stop_at: Optional[float] = None
        self._holding = False
        try:
            widget.bind("<Destroy>", self._on_destroy, add="+")
        except tk.TclError:
            pass

    # -------------------------------------------------------------- 状态
    @property
    def running(self) -> bool:
        return self._job is not None

    def _on_destroy(self, event=None) -> None:
        # 子控件的 <Destroy> 也会经 bindtags 的 toplevel 传递，这里只认自己
        if event is not None and getattr(event, "widget", None) is not self.widget:
            return
        self.stop()

    # -------------------------------------------------------------- 补间
    def tween(self, duration_ms: float, start: float, end: float,
              apply: Callable[[float], None],
              on_done: Optional[Callable[[], None]] = None,
              ease: Callable[[float], float] = ease_out_cubic) -> None:
        """在 duration_ms 内把 value 从 start 线性走到 end，每帧回调 apply(value)。"""
        self.stop()
        duration_ms = max(1.0, float(duration_ms))
        span = float(end) - float(start)

        def step() -> None:
            self._job = None
            if not self._alive():
                self._release()
                return
            elapsed = (time.perf_counter() - began) * 1000.0
            progress = min(1.0, elapsed / duration_ms)
            value = float(start) + span * ease(progress)
            try:
                apply(value)
            except tk.TclError:      # 控件在回调里被销毁
                self._release()
                return
            if progress >= 1.0:
                self._release()
                if on_done is not None:
                    try:
                        on_done()
                    except tk.TclError:
                        pass
                return
            self._job = self.widget.after(frame_ms(), step)

        began = time.perf_counter()
        self._acquire()
        step()

    def stop(self) -> None:
        """立刻停表（不再回调 apply，也不补最后一帧）。"""
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass
            self._job = None
        self._release()

    # -------------------------------------------------------------- 内部
    def _alive(self) -> bool:
        try:
            return bool(self.widget.winfo_exists())
        except tk.TclError:
            return False

    def _acquire(self) -> None:
        if not self._holding:
            display.hold_precise_timer()
            self._holding = True

    def _release(self) -> None:
        if self._holding:
            display.release_precise_timer()
            self._holding = False


# ------------------------------------------------------------------ 连续节拍
class Ticker:
    """按帧节拍反复调用 step(dt)，直到 step 返回 False 或调用 stop()。

    用于"追一个目标值"的动画（滚动），这类动画没有固定时长，
    但同样需要**按真实经过时间**推进，否则帧率一变速度就变。
    """

    def __init__(self, widget: tk.Misc) -> None:
        self.widget = widget
        self._job: Optional[str] = None
        self._holding = False
        self._last = 0.0

    @property
    def running(self) -> bool:
        return self._job is not None

    def start(self, step: Callable[[float], bool]) -> None:
        if self._job is not None:
            return
        self._last = time.perf_counter()
        self._acquire()

        def tick() -> None:
            self._job = None
            if not self._alive():
                self._release()
                return
            now = time.perf_counter()
            dt = max(0.0, now - self._last)
            self._last = now
            try:
                keep_going = step(dt)
            except tk.TclError:
                self._release()
                return
            if not keep_going:
                self._release()
                return
            self._job = self.widget.after(frame_ms(), tick)

        tick()

    def stop(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass
            self._job = None
        self._release()

    def _alive(self) -> bool:
        try:
            return bool(self.widget.winfo_exists())
        except tk.TclError:
            return False

    def _acquire(self) -> None:
        if not self._holding:
            display.hold_precise_timer()
            self._holding = True

    def _release(self) -> None:
        if self._holding:
            display.release_precise_timer()
            self._holding = False
