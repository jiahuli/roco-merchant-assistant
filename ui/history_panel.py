"""历史货架面板：默认折叠，展开后按时间倒序显示每一轮卖过的东西。

为什么默认折叠
--------------
历史是"查证"用的，不是"看今天"用的。展开着会把手里的当前货架挤下去，
所以默认收起，标题上直接给出"有几轮"，需要时再点开。

排序与内容
----------
* 倒序：20:00 在最前，然后是 16 / 12 / 08（跨天则日期更近的在上）。
* **保留全天商品**：历史要还原"那一轮货架长什么样"，条件球这类全天在售的
  本来就该在，所以这里不做任何剔除。
* 展开后**复用主界面的 `ItemCard`**：同样的图标尺寸、珍惜度色条、中文价格、
  内边距与圆角，而不是退化成一段纯文本——历史是"过去的货架"，视觉上就该
  和现在的货架长得一样。
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Iterable, Optional

from cache import ImageLoader
from history import HistoryRound
from ui.theme import ACCENT, BG, BORDER, MUTED, RARITY_COLORS, TEXT, TEXT_DIM, ui_font
from ui.widgets import ItemCard, PillButton


class HistoryPanel(tk.Frame):
    """可折叠的历史列表。"""

    def __init__(self, master: tk.Misc, loader: Optional[ImageLoader] = None,
                 expanded: bool = False,
                 on_toggle: Optional[Callable[[bool], None]] = None) -> None:
        super().__init__(master, bg=BG)
        self._rounds: list[HistoryRound] = []
        self._has_data = False
        self._loader = loader
        self._expanded = False
        # 重绘时 constructor 会先于数据到达，先记住"用户想展开"，等数据来了再展开
        self._want_expanded = bool(expanded)
        self._on_toggle = on_toggle

        self.header = tk.Frame(self, bg=BG, cursor="hand2")
        self.header.pack(fill="x", padx=26, pady=(6, 2))
        self._arrow = tk.Label(self.header, text="▸", bg=BG, fg=MUTED, font=ui_font(12))
        self._arrow.pack(side="left")
        tk.Label(self.header, text="历史售卖记录", bg=BG, fg=TEXT,
                 font=ui_font(10, "bold")).pack(side="left", padx=(4, 0))
        self._meta = tk.Label(self.header, text="", bg=BG, fg=MUTED, font=ui_font(8))
        self._meta.pack(side="left", padx=(8, 0), pady=(3, 0))

        # 展开/收起做成真正的按钮：原来只有一行小字，鼠标很难点到
        self._toggle_btn = PillButton(
            self.header, "暂无历史", self.toggle, style="subtle",
            width=104, height=30, font_size=9,
        )
        self._toggle_btn.set_enabled(False)   # 有数据后才可点
        self._toggle_btn.pack(side="right", pady=(2, 2))
        self._count_hint = tk.Label(self.header, text="", bg=BG, fg=MUTED, font=ui_font(8))
        self._count_hint.pack(side="right", padx=(0, 10), pady=(3, 0))

        for widget in (self.header, self._arrow, self._meta, self._count_hint):
            widget.bind("<Button-1>", lambda _e: self.toggle())

        self._body = tk.Frame(self, bg=BG)

    # ------------------------------------------------------------------
    def set_rounds(self, rounds: Iterable[HistoryRound], current_key: str = "") -> None:
        """更新数据；折叠状态下只刷新标题，不重建列表。"""
        self._rounds = list(rounds)
        self._has_data = bool(self._rounds)
        current = f" · 当前 {current_key.split('#')[-1]} 轮" if current_key else ""
        self._meta.configure(
            text=f"{len(self._rounds)} 轮{current}" if self._has_data else "还没有记录"
        )
        self._count_hint.configure(
            text=f"共 {sum(r.count for r in self._rounds)} 件" if self._has_data else ""
        )

        # 没有历史数据时，展开控件直接禁用：点了也没内容，不该给出可点的暗示
        self._toggle_btn.set_enabled(self._has_data)
        if not self._has_data:
            self._toggle_btn.set_text("暂无历史")
            if self._expanded:
                self._collapse()
            return
        if self._want_expanded and not self._expanded:
            self.toggle()            # 数据到了，恢复用户上次的展开状态
            return
        self._toggle_btn.set_text("收起历史" if self._expanded else "展开历史")
        if self._expanded:
            self._rebuild()

    def toggle(self) -> None:
        if not self._has_data:       # 没有数据时不展开，避免弹出空面板
            return
        self._expanded = not self._expanded
        self._want_expanded = self._expanded
        if self._on_toggle is not None:
            self._on_toggle(self._expanded)
        self._arrow.configure(text="▾" if self._expanded else "▸")
        self._toggle_btn.set_text("收起历史" if self._expanded else "展开历史")
        if self._expanded:
            self._body.pack(fill="x", padx=20, pady=(2, 0))
            self._rebuild()
        else:
            self._body.pack_forget()

    def _collapse(self) -> None:
        self._expanded = False
        self._want_expanded = False
        self._arrow.configure(text="▸")
        self._body.pack_forget()
        if self._on_toggle is not None:
            self._on_toggle(False)

    # ------------------------------------------------------------------
    def _rebuild(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        if not self._rounds:
            tk.Label(self._body, text="还没有历史记录，查询成功一轮后就会出现。",
                     bg=BG, fg=MUTED, font=ui_font(9), anchor="w").pack(fill="x", pady=10)
            return

        for index, round_ in enumerate(self._rounds):
            if index:
                tk.Frame(self._body, bg=BORDER, height=1).pack(fill="x", padx=6, pady=(10, 0))
            self._build_round(round_, is_current=(index == 0))

    def _build_round(self, round_: HistoryRound, is_current: bool) -> None:
        # 标题行：与主界面「本轮货架」的排版保持一致（左标题、右计数）
        head = tk.Frame(self._body, bg=BG)
        head.pack(fill="x", padx=6, pady=(8, 2))
        tk.Label(head, text=round_.clock or "—", bg=BG, fg=TEXT,
                 font=ui_font(10, "bold")).pack(side="left")
        tk.Label(head, text=round_.date, bg=BG, fg=MUTED,
                 font=ui_font(8)).pack(side="left", padx=(6, 0), pady=(3, 0))
        if is_current:
            tk.Label(head, text="最新", bg=BG, fg=ACCENT,
                     font=ui_font(8, "bold")).pack(side="left", padx=(8, 0), pady=(3, 0))
        tk.Label(head, text=f"{round_.count} 件", bg=BG, fg=MUTED,
                 font=ui_font(8)).pack(side="right", pady=(3, 0))
        top_level = max((item.rarity.level for item in round_.items), default=1)
        tk.Label(head, text="●", bg=BG, fg=RARITY_COLORS.get(top_level, MUTED),
                 font=ui_font(8)).pack(side="right", padx=(0, 6), pady=(3, 0))

        if self._loader is None or not round_.items:
            # 没有加载器（单测/极端情况）时退回文字，但仍使用同一套字号与颜色
            names = "、".join(item.name for item in round_.items) or "—"
            prices = " · ".join(f"{i.name} {i.price_text}" for i in round_.items[:4])
            tk.Label(self._body, text=f"{names}\n{prices}" if prices else names,
                     bg=BG, fg=TEXT_DIM, font=ui_font(9), anchor="w", justify="left",
                     wraplength=880).pack(fill="x", padx=12, pady=(2, 6))
            return

        grid = tk.Frame(self._body, bg=BG)
        grid.pack(fill="x", padx=6, pady=(2, 2))
        cards = [ItemCard(grid, item, self._loader, index)
                 for index, item in enumerate(round_.items)]

        def relayout(_event=None) -> None:
            width = grid.winfo_width() or 1100
            columns = max(1, min(4, (width - 20) // 340))
            for column in range(5):
                grid.columnconfigure(
                    column, weight=1 if column < columns else 0,
                    uniform="hist" if column < columns else "",
                )
            for index, card in enumerate(cards):
                card.grid(row=index // columns, column=index % columns,
                          sticky="nsew", padx=6, pady=6)

        grid.bind("<Configure>", relayout)
        relayout()
