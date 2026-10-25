"""内置日志查看器：看错误日志、打开日志目录、清空日志。"""
from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from typing import Optional

from roco import applog
from roco.ui import theme
from roco.platform import win11_style
from roco.ui.theme import round_rect, ui_font
from roco.ui.widgets import PillButton


class _SlimScrollbar(tk.Canvas):
    """细滚动条：绑定到 Text 的 yview，配色跟主界面一致。"""

    def __init__(self, master: tk.Misc, target: tk.Text, bg: str = theme.SURFACE) -> None:
        super().__init__(master, width=10, bg=bg, highlightthickness=0, bd=0)
        self.target = target
        self._first, self._last = 0.0, 1.0
        self._drag_origin = 0
        self._drag_top = 0.0
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<Configure>", lambda _e: self.redraw())

    def set_view(self, first: str, last: str) -> None:
        self._first, self._last = float(first), float(last)
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        height = self.winfo_height()
        if height <= 1 or self._last - self._first >= 0.999:
            return
        top = max(0.0, self._first * height)
        bottom = min(float(height), self._last * height)
        if bottom - top < 24:
            bottom = top + 24
        round_rect(self, 2, top + 2, 8, bottom - 2, 3, fill=theme.SURFACE_3, outline="", width=0)

    def _on_press(self, event) -> None:
        height = self.winfo_height() or 1
        self._drag_origin = event.y
        self._drag_top = self._first * height

    def _on_drag(self, event) -> None:
        height = self.winfo_height() or 1
        if height <= 24:
            return
        target = (self._drag_top + (event.y - self._drag_origin)) / height
        self.target.yview_moveto(max(0.0, min(1.0, target)))


class LogViewer(tk.Toplevel):
    """只读日志窗口。"""

    def __init__(self, master: tk.Misc, log_path: Optional[Path] = None) -> None:
        super().__init__(master, bg=theme.BG)
        self.title("错误日志 · 远行商人助手")
        self.configure(bg=theme.BG)
        self.minsize(720, 460)
        self._path = Path(log_path) if log_path else applog.log_file()
        self._errors_only = tk.BooleanVar(value=False)

        width, height = 880, 580
        try:
            x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            self.geometry(f"{width}x{height}")

        self.withdraw()          # 先隐藏再建控件，建完才显示，消除白底一闪
        self._build()
        self.reload()
        self.update_idletasks()
        self.deiconify()
        win11_style.style_toplevel(self, dark=theme.is_dark())

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        header = tk.Frame(self, bg=theme.BG)
        header.pack(fill="x", padx=18, pady=(16, 8))

        tk.Label(header, text="错误日志", bg=theme.BG, fg=theme.TEXT, font=ui_font(14, "bold")).pack(side="left")
        self.path_label = tk.Label(
            header, text=str(self._path), bg=theme.BG, fg=theme.MUTED, font=ui_font(8), anchor="w",
        )
        self.path_label.pack(side="left", padx=(12, 0), pady=(4, 0))

        bar = tk.Frame(self, bg=theme.BG)
        bar.pack(fill="x", padx=18, pady=(0, 8))

        self.error_only_box = tk.Checkbutton(
            bar, text="只看错误", variable=self._errors_only, command=self.reload,
            bg=theme.BG, fg=theme.TEXT_DIM, activebackground=theme.BG, activeforeground=theme.TEXT,
            selectcolor=theme.SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
        )
        self.error_only_box.pack(side="left")

        PillButton(bar, "刷新", self.reload, width=68, height=28, font_size=9).pack(
            side="left", padx=(14, 0)
        )
        PillButton(bar, "打开所在文件夹", self.open_folder, width=120, height=28, font_size=9).pack(
            side="left", padx=(8, 0)
        )
        PillButton(bar, "清空日志", self.clear, style="subtle", width=88, height=28,
                   font_size=9).pack(side="left", padx=(8, 0))

        self.size_label = tk.Label(bar, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(8))
        self.size_label.pack(side="right")

        body = tk.Frame(self, bg=theme.BORDER)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        inner = tk.Frame(body, bg=theme.SURFACE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        self.text = tk.Text(
            inner, bg=theme.SURFACE, fg=theme.TEXT_DIM, insertbackground=theme.ACCENT, relief="flat",
            bd=0, highlightthickness=0, wrap="none", font=("Consolas", 9),
            padx=10, pady=8, spacing1=1, spacing3=1,
        )
        self.text.pack(side="left", fill="both", expand=True)
        self.scrollbar = _SlimScrollbar(inner, self.text, bg=theme.SURFACE)
        self.scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self.text.configure(yscrollcommand=self.scrollbar.set_view)
        self.scrollbar.configure()
        self.text.tag_configure("error", foreground=theme.DANGER)
        self.text.tag_configure("warning", foreground="#ffb454")
        self.text.bind("<Key>", lambda _e: "break")   # 只读
        self.text.bind("<MouseWheel>", self._on_wheel)

        self.status = tk.Label(
            self, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(8), anchor="w",
        )
        self.status.pack(fill="x", padx=18, pady=(0, 10))

    # ------------------------------------------------------------------ 行为
    def _on_wheel(self, event) -> None:
        self.text.yview_scroll(-1 if event.delta > 0 else 1, "units")
        self.scrollbar.redraw()
        return "break"

    def reload(self) -> None:
        content = applog.tail(max_lines=1500, errors_only=bool(self._errors_only.get()))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if content.strip():
            self.text.insert("1.0", content)
            # 简单着色：ERROR / theme.WARNING 行
            for index, line in enumerate(content.splitlines(), start=1):
                if " ERROR " in line:
                    self.text.tag_add("error", f"{index}.0", f"{index}.end")
                elif " theme.WARNING " in line:
                    self.text.tag_add("warning", f"{index}.0", f"{index}.end")
        else:
            self.text.insert("1.0", "（暂无日志）\n\n出现异常时，这里会记录完整堆栈。")
        self.text.configure(state="disabled")
        self.text.see("end")

        exists = self._path.exists()
        size = self._path.stat().st_size if exists else 0
        lines = content.count("\n") + (1 if content else 0)
        self.size_label.configure(text=f"{size / 1024:.1f} KB · {lines} 行")
        self.status.configure(
            text=f"日志位置：{self._path}" + ("" if exists else "（文件尚未生成）")
        )

    def open_folder(self) -> None:
        folder = self._path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606 - 打开资源管理器
        except Exception as exc:
            self.status.configure(text=f"打开失败：{exc}")

    def clear(self) -> None:
        try:
            if self._path.exists():
                with open(self._path, "w", encoding="utf-8") as handle:
                    handle.write("")
            applog.info("日志已被手动清空")
        except Exception as exc:
            self.status.configure(text=f"清空失败：{exc}")
            return
        self.reload()
        self.status.configure(text="日志已清空")
