"""定时调度：按用户配置的时间点（默认 08:05 / 12:05 / 16:05 / 20:05）触发查询。

实现说明：用 Tk 的 after() 做单线程 1 秒节拍，避免额外线程带来的竞态；
真正耗时的网络请求由调用方丢到线程池里执行。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Iterable

import tkinter as tk

from roco.config import sanitize_times


class Scheduler:
    def __init__(
        self,
        root: tk.Misc,
        times: Iterable[str],
        on_fire: Callable[[], None],
        on_tick: Callable[[datetime], None] | None = None,
    ) -> None:
        self.root = root
        self.times = sanitize_times(list(times))
        self.on_fire = on_fire
        self.on_tick = on_tick
        self._fired: set[tuple[str, str]] = set()
        self._job: str | None = None
        self._running = False

    # ---------------- 生命周期 ----------------
    def set_times(self, times: Iterable[str]) -> None:
        self.times = sanitize_times(list(times))
        self._fired.clear()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._schedule()

    def stop(self) -> None:
        self._running = False
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _schedule(self) -> None:
        if not self._running:
            return
        # 对齐到下一个整秒，倒计时不会出现跳动
        delay = 1000 - datetime.now().microsecond // 1000
        self._job = self.root.after(max(50, delay), self._tick)

    def _tick(self) -> None:
        if not self._running:
            return
        now = datetime.now()
        stamp = now.strftime("%H:%M")
        if stamp in self.times:
            key = (now.strftime("%Y-%m-%d"), stamp)
            if key not in self._fired:
                self._fired.add(key)
                try:
                    self.on_fire()
                except Exception:
                    pass
        # 只保留近两天的记录，避免无限增长
        if len(self._fired) > 32:
            keep = now.strftime("%Y-%m-%d")
            self._fired = {k for k in self._fired if k[0] >= keep}

        if self.on_tick:
            try:
                self.on_tick(now)
            except Exception:
                pass
        self._schedule()

    # ---------------- 时间计算 ----------------
    def next_run(self, now: datetime | None = None) -> datetime | None:
        if not self.times:
            return None
        now = now or datetime.now()
        for stamp in self.times:
            hour, minute = (int(x) for x in stamp.split(":"))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                return candidate
        hour, minute = (int(x) for x in self.times[0].split(":"))
        return (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    def next_run_text(self, now: datetime | None = None) -> str:
        target = self.next_run(now)
        if not target:
            return "未设置"
        now = now or datetime.now()
        delta = target - now
        return f"{target.strftime('%H:%M')}（{format_delta(delta)}后）"


def format_delta(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes:02d} 分"
    if minutes:
        return f"{minutes} 分 {seconds:02d} 秒"
    return f"{seconds} 秒"


def format_countdown(target: datetime | None, now: datetime | None = None) -> str:
    if target is None:
        return "--:--:--"
    now = now or datetime.now()
    total = max(0, int((target - now).total_seconds()))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
