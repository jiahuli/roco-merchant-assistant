"""关注物品选择器：按名字挑要提醒的商品。

为什么不是"按品质"就够
----------------------
品质筛选是粗粒度的：想盯"国王球"的人，不该被同品质的其它球一起通知；
反过来，某个普通品质的活动道具（比如限时上架的"可可果球"）也永远进不了
品质阈值。所以这里给一份"点名清单"，与品质筛选**并行**生效：
品质达标的照旧提醒，清单里的物品无条件提醒。

清单本身是可更新的（`catalog.py`）：Wiki 能抓多少抓多少，抓不到的靠
本机观察与手动添加补，避免"新道具上架了但选不到"。
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, simpledialog
from typing import Optional

from roco.net import catalog
from roco.services.cache import IconCache, ImageLoader
from roco.ui import theme
from roco.platform import win11_style
from roco.ui.theme import ui_font
from roco.ui.widgets import PillButton, ScrollFrame

ICON_PX = 24


class ItemPickerDialog(tk.Toplevel):
    """返回值放在 `result`：None = 取消，list[str] = 选中的物品名。"""

    def __init__(self, master: tk.Misc, selected: list[str],
                 cache: Optional[IconCache] = None) -> None:
        super().__init__(master, bg=theme.BG)
        self.title("选择要提醒的物品")
        self.configure(bg=theme.BG)
        self.resizable(False, False)
        self.transient(master)
        self.result: Optional[list[str]] = None
        self._selected = {str(x) for x in selected if str(x).strip()}
        self._rows: list[tuple[str, dict]] = []
        self._busy = False

        self._loader: Optional[ImageLoader] = None
        if cache is not None:
            self._loader = ImageLoader(self, cache, ICON_PX)

        width, height = 580, 640
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

        self.withdraw()          # 先隐藏再建控件，建完才显示，消除白底一闪
        self._build()
        self._reload()
        self.update_idletasks()
        self.deiconify()
        win11_style.style_toplevel(self, dark=theme.is_dark())
        self.grab_set()
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        head = tk.Frame(self, bg=theme.BG)
        head.pack(fill="x", padx=20, pady=(16, 6))
        tk.Label(head, text="关注这些物品", bg=theme.BG, fg=theme.TEXT,
                 font=ui_font(14, "bold")).pack(side="left")
        tk.Label(head, text="与「提醒等级」并行生效", bg=theme.BG, fg=theme.MUTED,
                 font=ui_font(8)).pack(side="left", padx=(8, 0), pady=(5, 0))

        search_row = tk.Frame(self, bg=theme.BG)
        search_row.pack(fill="x", padx=20, pady=(6, 4))
        self._keyword = tk.StringVar()
        entry = tk.Entry(
            search_row, textvariable=self._keyword, bg=theme.SURFACE_2, fg=theme.TEXT,
            insertbackground=theme.ACCENT, relief="flat", highlightthickness=1,
            highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT, font=ui_font(10), bd=0,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._keyword.trace_add("write", lambda *_: self._reload())
        entry.focus_set()

        self._count_label = tk.Label(search_row, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(8))
        self._count_label.pack(side="left", padx=(10, 0))

        self._scroll = ScrollFrame(self, bg=theme.BG)
        self._scroll.pack(fill="both", expand=True, padx=20, pady=(4, 6))

        self._status = tk.Label(self, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(8), anchor="w")
        self._status.pack(fill="x", padx=20)

        footer = tk.Frame(self, bg=theme.BG)
        footer.pack(fill="x", padx=20, pady=12)
        PillButton(footer, "确定", self._save, style="primary", width=90).pack(side="right")
        PillButton(footer, "取消", self._cancel, width=74).pack(side="right", padx=(0, 8))
        PillButton(footer, "手动添加", self._add_manual, style="subtle", width=90).pack(
            side="left", padx=(0, 8))
        self._wiki_btn = PillButton(footer, "从 Wiki 更新", self._update_wiki,
                                    style="subtle", width=110)
        self._wiki_btn.pack(side="left")

    # ------------------------------------------------------------------ 列表
    def _reload(self) -> None:
        keyword = self._keyword.get().strip().lower()
        items = catalog.load()
        if keyword:
            items = [i for i in items if keyword in i.name.lower()]

        body = self._scroll.body
        for child in body.winfo_children():
            child.destroy()
        self._rows.clear()

        if not items:
            tk.Label(
                body, text="清单里没有匹配的物品。\n可以点左下角「从 Wiki 更新」或「手动添加」。",
                bg=theme.BG, fg=theme.MUTED, font=ui_font(9), justify="left", anchor="w",
            ).pack(fill="x", pady=12, padx=8)
        for index, item in enumerate(items):
            self._rows.append((item.name, self._build_row(body, item)))

        self._count_label.configure(
            text=f"已选 {len(self._selected)} 件 / 共 {len(catalog.load())} 件"
        )
        updated = catalog.updated_text()
        self._status.configure(text=f"{updated} · 本机见过的商品会自动加入清单")

    def _build_row(self, master: tk.Misc, item) -> dict:
        chosen = item.name in self._selected
        row = tk.Frame(master, bg=theme.SURFACE, cursor="hand2")
        row.pack(fill="x", pady=1)

        check = tk.Label(row, text="☑" if chosen else "☐", bg=theme.SURFACE,
                         fg=theme.ACCENT if chosen else theme.MUTED, font=ui_font(12))
        check.pack(side="left", padx=(6, 6))

        icon_box = None
        if self._loader is not None and item.icon:
            icon_box = tk.Canvas(row, width=ICON_PX, height=ICON_PX, bg=theme.SURFACE,
                                 highlightthickness=0, bd=0)
            icon_box.pack(side="left", padx=(0, 8), pady=3)
            self._loader.load(item.icon, lambda photo, box=icon_box: self._draw_icon(box, photo))

        name = tk.Label(row, text=item.name, bg=theme.SURFACE,
                        fg=theme.TEXT if chosen else theme.TEXT_DIM, font=ui_font(9),
                        anchor="w")
        name.pack(side="left", fill="x", expand=True)

        if item.source:
            tk.Label(row, text=item.source, bg=theme.SURFACE, fg=theme.MUTED,
                     font=ui_font(8)).pack(side="right", padx=(0, 8))

        widgets = {k: v for k, v in
                   (("row", row), ("check", check), ("name", name), ("icon", icon_box))
                   if v is not None}
        for widget in widgets.values():
            widget.bind("<Button-1>", lambda _e, n=item.name: self._toggle(n))
        return widgets

    @staticmethod
    def _draw_icon(box: tk.Canvas, photo) -> None:
        if photo is None:
            return
        try:
            box.create_image(ICON_PX / 2, ICON_PX / 2, image=photo)
            box._photo = photo          # 防止被 GC 回收
        except tk.TclError:
            pass

    def _toggle(self, name: str) -> None:
        row = next((r for n, r in self._rows if n == name), None)
        if name in self._selected:
            self._selected.discard(name)
            chosen = False
        else:
            self._selected.add(name)
            chosen = True
        if row:
            row["check"].configure(text="☑" if chosen else "☐",
                                   fg=theme.ACCENT if chosen else theme.MUTED)
            row["name"].configure(fg=theme.TEXT if chosen else theme.TEXT_DIM)
        self._count_label.configure(
            text=f"已选 {len(self._selected)} 件 / 共 {len(catalog.load())} 件"
        )

    # ------------------------------------------------------------------ 动作
    def _update_wiki(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._wiki_btn.set_enabled(False)
        self._status.configure(text="正在从 Wiki 抓取清单…（约 3~10 秒）")

        def work() -> None:
            try:
                total, message = catalog.refresh_from_wiki()
            except Exception as exc:      # noqa: BLE001 - 网络异常不该关掉对话框
                total, message = 0, f"更新失败：{exc}"
            self.after(0, lambda: self._after_wiki(total, message))

        threading.Thread(target=work, daemon=True).start()

    def _after_wiki(self, total: int, message: str) -> None:
        self._busy = False
        try:
            self._wiki_btn.set_enabled(True)
            self._status.configure(text=message)
            self._reload()
        except tk.TclError:
            pass

    def _add_manual(self) -> None:
        name = simpledialog.askstring("手动添加", "输入物品名（与游戏内一致）：",
                                      parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if catalog.add_manual(name):
            self._selected.add(name)
            self._status.configure(text=f"已加入清单并勾选：{name}")
            self._reload()
        else:
            messagebox.showinfo("已存在", f"「{name}」已经在清单里了。", parent=self)

    def _save(self) -> None:
        self.result = sorted(self._selected)
        self._close()

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        if self._loader is not None:
            try:
                self._loader.shutdown()
            except Exception:      # noqa: BLE001 - 关闭失败不该阻塞窗口销毁
                pass
            self._loader = None
        self.destroy()
