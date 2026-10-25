"""可复用界面控件：按钮、徽章、商品卡片、滚动容器、气泡提示。"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from roco.services.cache import ICON_SIZE, ImageLoader
from roco.platform import monitors
from roco.core.models import MerchantItem
from roco.ui import theme
from roco.ui.theme import lighten, mix, round_rect, ui_font


def _parent_bg(widget: tk.Misc) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return theme.BG


# 滚轮动画每帧走过的视口比例（15ms/帧 ≈ 每秒 3 屏）
_SCROLL_SPEED = 0.045


# ---------------------------------------------------------------------------
# 圆角按钮
# ---------------------------------------------------------------------------
class PillButton(tk.Canvas):
    """圆角按钮，三种样式：primary / ghost / subtle。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Optional[Callable[[], None]] = None,
        style: str = "ghost",
        width: int = 104,
        height: int = 34,
        bg: Optional[str] = None,
        font_size: int = 10,
        radius: Optional[int] = None,
    ) -> None:
        super().__init__(
            master, width=width, height=height, bg=bg or _parent_bg(master),
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self._text = text
        self._command = command
        self._style = style
        self._radius = radius if radius is not None else min(10, height // 2)
        self._font = ui_font(font_size, "bold" if style == "primary" else "normal")
        self._hover = False
        self._enabled = True
        # 悬停颜色过渡：t 从 0（常态）到 1（悬停），动画步进
        self._hover_t = 0.0
        self._anim_job: Optional[str] = None

        self._shape = None
        self._label = None
        self._draw()

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", self._on_configure)

    # ---------------- 绘制 ----------------
    def _colors(self) -> tuple[str, str, str]:
        """按悬停进度 t 返回 (填充色, 文字色, 描边色)。"""
        t = self._hover_t
        if not self._enabled:
            return theme.SURFACE_2, theme.MUTED, theme.BORDER
        if self._style == "primary":
            fill = mix(theme.ACCENT, lighten(theme.ACCENT, 0.10), t)
            return fill, "#ffffff", ""
        fill = mix(theme.SURFACE_2, theme.SURFACE_3, t)
        fg = mix(theme.TEXT_DIM, theme.TEXT, t)
        if self._style == "subtle":
            return fill, fg, theme.BORDER
        outline = mix(theme.BORDER, theme.BORDER_HOVER, t)
        return fill, fg, outline

    def _palette(self) -> tuple[str, str, str]:
        """兼容旧调用：返回当前进度下的颜色。"""
        return self._colors()

    def _draw(self) -> None:
        self.delete("all")
        fill, fg, outline = self._colors()
        width = int(self["width"])
        height = int(self["height"])
        round_rect(
            self, 1, 1, width - 1, height - 1, self._radius,
            fill=fill, outline=outline or fill, width=1,
        )
        self.create_text(
            width / 2, height / 2, text=self._text, fill=fg, font=self._font,
        )

    # ---------------- 事件 ----------------
    def _animate_hover(self) -> None:
        """悬停过渡：每 24ms 走 35% 剩余距离，约 3-4 帧完成，观感是渐变而非跳变。"""
        self._anim_job = None
        target = 1.0 if self._hover else 0.0
        diff = target - self._hover_t
        if abs(diff) < 0.05:
            self._hover_t = target
            self._draw()
            return
        self._hover_t += diff * 0.35
        self._draw()
        self._anim_job = self.after(24, self._animate_hover)

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        if self._enabled:
            self._animate_hover()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._animate_hover()

    def _on_press(self, _event=None) -> None:
        if not self._enabled:
            return
        fill, _fg, _o = self._palette()
        self.delete("all")
        round_rect(self, 1, 1, int(self["width"]) - 1, int(self["height"]) - 1,
                   self._radius, fill=mix(fill, theme.BG, 0.18), outline="", width=0)
        self.create_text(int(self["width"]) / 2, int(self["height"]) / 2,
                         text=self._text, fill=theme.TEXT, font=self._font)

    def _on_release(self, _event=None) -> None:
        self._draw()
        if self._enabled and self._command:
            self._command()

    def _on_configure(self, event) -> None:
        self._draw()

    # ---------------- API ----------------
    def set_text(self, text: str) -> None:
        self._text = text
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()


# ---------------------------------------------------------------------------
# 珍惜度徽章
# ---------------------------------------------------------------------------
class RarityBadge(tk.Canvas):
    def __init__(self, master: tk.Misc, item: MerchantItem, bg: Optional[str] = None) -> None:
        color = theme.RARITY_COLORS.get(item.rarity.level, theme.MUTED)
        text = item.rarity.label
        font = ui_font(8, "bold")
        width = 16 + len(text) * 12
        super().__init__(
            master, width=width, height=20, bg=bg or _parent_bg(master),
            highlightthickness=0, bd=0,
        )
        round_rect(self, 0, 2, width, 19, 9, fill=mix(color, theme.BG, 0.78), outline="", width=0)
        self.create_text(width / 2, 10, text=text, fill=color, font=font)


# ---------------------------------------------------------------------------
# 商品卡片
# ---------------------------------------------------------------------------
class ItemCard(tk.Frame):
    """一行商品：图标 / 名称 / 珍惜度 / 中文价格 / 限购。"""

    def __init__(self, master: tk.Misc, item: MerchantItem, loader: ImageLoader,
                 index: int = 0, purchased: bool = False,
                 on_toggle: Optional[Callable[[MerchantItem], None]] = None) -> None:
        super().__init__(
            master, bg=theme.SURFACE, highlightbackground=theme.BORDER, highlightthickness=1, bd=0,
        )
        self.item = item
        self.loader = loader
        self.purchased = purchased
        self._on_toggle = on_toggle
        self._photo: Optional[tk.PhotoImage] = None

        # 左侧珍惜度色条（已购买时压暗，避免抢注意力）
        tk.Frame(self, bg=theme.MUTED if purchased else theme.RARITY_COLORS.get(item.rarity.level, theme.MUTED),
                 width=3).pack(side="left", fill="y")

        inner = tk.Frame(self, bg=theme.SURFACE)
        inner.pack(side="left", fill="both", expand=True, padx=12, pady=10)

        # 图标
        self.icon = tk.Canvas(
            inner, width=ICON_SIZE, height=ICON_SIZE, bg=theme.SURFACE,
            highlightthickness=0, bd=0,
        )
        self.icon.pack(side="left", anchor="n")
        round_rect(self.icon, 0, 0, ICON_SIZE, ICON_SIZE, 12,
                   fill=theme.SURFACE_2, outline=mix(theme.BORDER, theme.SURFACE, 0.2), width=1)
        self._placeholder = self.icon.create_text(
            ICON_SIZE / 2, ICON_SIZE / 2, text=item.name[:1],
            fill=theme.MUTED, font=ui_font(16, "bold"),
        )

        # 文字区
        body = tk.Frame(inner, bg=theme.SURFACE)
        body.pack(side="left", fill="both", expand=True, padx=(12, 0))
        head = tk.Frame(body, bg=theme.SURFACE)
        head.pack(fill="x")

        # 已购买勾选：只用一个小标签，不引入新的边框/布局层级
        if on_toggle is not None:
            normal_bg = theme.SURFACE_2
            self.toggle_btn = tk.Label(
                head,
                text="✓ 已购" if purchased else "○ 标记",
                bg=mix(theme.SUCCESS, theme.SURFACE, 0.82) if purchased else normal_bg,
                fg=theme.SUCCESS if purchased else theme.TEXT_DIM,
                font=ui_font(9, "bold"), padx=12, pady=5, cursor="hand2",
            )
            self.toggle_btn.pack(side="right", padx=(0, 8), pady=(0, 1))
            self.toggle_btn.bind("<Button-1>", self._toggle_purchased)
            # 悬停反馈：让它看起来是可点的按钮，而不是一行说明文字
            self.toggle_btn.bind(
                "<Enter>", lambda _e: self.toggle_btn.configure(
                    bg=theme.SURFACE_3 if not self.purchased
                    else mix(theme.SUCCESS, theme.SURFACE, 0.7))
            )
            self.toggle_btn.bind(
                "<Leave>", lambda _e: self.toggle_btn.configure(
                    bg=normal_bg if not self.purchased
                    else mix(theme.SUCCESS, theme.SURFACE, 0.82))
            )
            Tooltip(self.toggle_btn,
                    "取消已购标记" if purchased else "标记为已购买，会移到下方「已购买」区域")

        # 全天在售角标：解释"为什么换货后这件还在"，避免误认为没刷新
        if item.is_all_day:
            tk.Label(
                head, text="全天", bg=mix(theme.ACCENT, theme.SURFACE, 0.82), fg=theme.ACCENT,
                font=ui_font(8, "bold"), padx=6, pady=1,
            ).pack(side="right", padx=(0, 6))

        RarityBadge(head, item, bg=theme.SURFACE).pack(side="right")
        self.name_label = tk.Label(
            head, text=item.name, bg=theme.SURFACE,
            fg=theme.MUTED if purchased else theme.TEXT, font=ui_font(11, "bold"),
            anchor="w", justify="left",
        )
        self.name_label.pack(side="left", fill="x", expand=True)

        meta_text = " · ".join(x for x in (item.category, item.limit_text) if x)
        tk.Label(
            body, text=meta_text or "—", bg=theme.SURFACE, fg=theme.MUTED, font=ui_font(8),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        price_row = tk.Frame(body, bg=theme.SURFACE)
        price_row.pack(fill="x", pady=(7, 0))
        tk.Label(
            price_row, text=item.price_text, bg=theme.SURFACE,
            fg=theme.MUTED if purchased else theme.MONEY,
            font=ui_font(16, "bold"), anchor="w",
        ).pack(side="left")
        if item.price_raw_text:
            tk.Label(
                price_row, text=item.price_raw_text, bg=theme.SURFACE, fg=theme.MUTED,
                font=ui_font(8), anchor="w",
            ).pack(side="left", padx=(8, 0), pady=(5, 0))

        # 悬停高亮：只改描边色（带渐变动画），避免子控件底色错位
        self._hovered = False
        self._border_t = 0.0              # 描边色过渡进度 0（常态）→ 1（悬停）
        self._border_job: Optional[str] = None
        for widget in self._walk(self):
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")

        if item.description:
            Tooltip(self, f"{item.name}\n{item.description}", delay=520)

        self.load_icon()

    @staticmethod
    def _walk(widget: tk.Misc):
        yield widget
        for child in widget.winfo_children():
            yield from ItemCard._walk(child)

    def _toggle_purchased(self, _event=None) -> str:
        """点击「标记 / 已购」；实际状态由主窗口统一落盘并重绘。"""
        if self._on_toggle is not None:
            self._on_toggle(self.item)
        return "break"

    def _on_enter(self, _event=None) -> None:
        if not self._hovered:
            self._hovered = True
            self._animate_border(1.0)

    def _on_leave(self, event=None) -> None:
        if event is not None:
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
            while widget is not None:
                if widget is self:
                    return
                widget = getattr(widget, "master", None)
        self._hovered = False
        self._animate_border(0.0)

    def _animate_border(self, target: float) -> None:
        """描边色渐变：24ms/帧走 40% 剩余距离，两三帧完成，鼠标扫过是"亮起"而不是"跳变"。"""
        if self._border_job is not None:
            try:
                self.after_cancel(self._border_job)
            except tk.TclError:
                pass
            self._border_job = None

        def step() -> None:
            self._border_job = None
            if not self.winfo_exists():
                return
            diff = target - self._border_t
            try:
                if abs(diff) < 0.08:
                    self._border_t = target
                    self.configure(
                        highlightbackground=mix(theme.BORDER, theme.BORDER_HOVER, target))
                    return
                self._border_t += diff * 0.4
                self.configure(
                    highlightbackground=mix(theme.BORDER, theme.BORDER_HOVER, self._border_t))
            except tk.TclError:
                return
            self._border_job = self.after(24, step)

        step()

    # 图标异步加载（命中缓存则立即显示）
    def load_icon(self) -> None:
        if not self.item.icon_url:
            return
        self.loader.load(self.item.icon_url, self._apply_icon)

    def _apply_icon(self, photo: Optional[tk.PhotoImage]) -> None:
        if photo is None:
            return
        self._photo = photo
        if getattr(self, "_placeholder", None):
            self.icon.delete(self._placeholder)
            self._placeholder = None
        try:
            self.icon.create_image(ICON_SIZE / 2, ICON_SIZE / 2, image=photo)
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 轻量气泡提示
# ---------------------------------------------------------------------------
class Tooltip:
    def __init__(self, widget: tk.Misc, text: str, delay: int = 500) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after: Optional[str] = None
        self._window: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        if self._window is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        window = tk.Toplevel(self.widget)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        shell = tk.Frame(window, bg=theme.BORDER)
        shell.pack()
        label = tk.Label(
            shell, text=self.text, bg=theme.SURFACE_2, fg=theme.TEXT_DIM, font=ui_font(9),
            justify="left", wraplength=330, padx=10, pady=8,
        )
        label.pack(padx=1, pady=1)
        window.update_idletasks()
        width = window.winfo_width()
        height = window.winfo_height()
        # 按气泡所在的那块屏做边界修正：用主屏宽度会在副屏右侧把气泡甩到主屏上
        try:
            screens = monitors.enum_monitors()
            target = monitors.best_monitor(screens, x, y, width, height)
            if x + width > target.work_right - 12:
                x = target.work_right - width - 12
            x = max(target.work_x + 8, x)
            if y + height > target.work_bottom - 12:
                # 下方放不下就翻到控件上方
                y = y - height - self.widget.winfo_height() - 12
                y = max(target.work_y + 8, y)
        except Exception:
            pass
        window.geometry(f"+{x}+{y}")
        self._window = window

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


# ---------------------------------------------------------------------------
# 滚动容器（自绘细滚动条）
# ---------------------------------------------------------------------------
class ScrollFrame(tk.Frame):
    def __init__(self, master: tk.Misc, bg: str = theme.BG) -> None:
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.bar = tk.Canvas(self, width=8, bg=bg, highlightthickness=0, bd=0)
        self.bar.pack(side="right", fill="y")

        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self._thumb = None
        self._last_thumb = None          # 上次绘制滚动条时的 (first,last,height)，去重用
        self._scroll_target: Optional[float] = None
        self._scroll_job: Optional[str] = None
        self._drag_origin = 0
        self._drag_top = 0.0
        self.bar.bind("<Button-1>", self._on_bar_press)
        self.bar.bind("<B1-Motion>", self._on_bar_drag)
        self.bar.bind("<Enter>", lambda _e: self._draw_thumb())
        self._sync_mousewheel()

    # ---------------- 布局同步 ----------------
    def _on_body_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._draw_thumb()

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._draw_thumb()

    def _on_scroll(self, first: str, last: str) -> None:
        self._first, self._last = float(first), float(last)
        self._draw_thumb()

    # ---------------- 滚动条 ----------------
    def _draw_thumb(self) -> None:
        if not hasattr(self, "_first"):
            return
        first, last = self._first, self._last
        height = self.bar.winfo_height()
        # 位置没变就不重绘：滚轮动画期间这个回调每帧都来，重画圆角多边形是纯浪费
        signature = (round(first, 3), round(last, 3), height)
        if signature == self._last_thumb:
            return
        self._last_thumb = signature
        self.bar.delete("all")
        if last - first >= 0.999 or height <= 1:
            return
        top = max(0, first * height)
        bottom = min(height, last * height)
        if bottom - top < 26:
            bottom = top + 26
        self._thumb = round_rect(
            self.bar, 2, top + 2, 6, bottom - 2, 2,
            fill=mix(theme.SURFACE_3, theme.BORDER_HOVER, 0.35), outline="", width=0
        )

    def _on_bar_press(self, event) -> None:
        self._cancel_scroll_anim()   # 用户手动拖动时停掉滚轮动画，避免抢位置
        height = self.bar.winfo_height() or 1
        self._drag_origin = event.y
        self._drag_top = getattr(self, "_first", 0.0) * height

    def _on_bar_drag(self, event) -> None:
        height = self.bar.winfo_height() or 1
        if height <= 26:
            return
        target = (self._drag_top + (event.y - self._drag_origin)) / height
        self.canvas.yview_moveto(max(0.0, min(1.0, target)))

    # ---------------- 滚轮（缓动动画） ----------------
    def _sync_mousewheel(self) -> None:
        def on_wheel(event) -> None:
            if not self._pointer_inside() or not self._content_overflows():
                return
            # 旧实现一格滚 2 个单位 ≈ 20% 视口、瞬间到位——这正是"滑动卡顿"的观感来源。
            # 现在把每格输入累积成目标位置，用 after 动画小步逼近，观感是连续平滑的。
            step = (-1 if event.delta > 0 else 1) * 0.18
            current = self._scroll_target if self._scroll_target is not None \
                else self.canvas.yview()[0]
            self._scroll_target = max(0.0, min(1.0, current + step))
            self._start_scroll_anim()

        # 绑到所属 Toplevel：只在本窗口内生效，窗口销毁时自动解除，不会残留
        self.winfo_toplevel().bind("<MouseWheel>", on_wheel, add="+")

    def _content_overflows(self) -> bool:
        first, last = self.canvas.yview()
        return last - first < 0.999

    def _start_scroll_anim(self) -> None:
        if self._scroll_job is None:
            self._animate_scroll()

    def _animate_scroll(self) -> None:
        self._scroll_job = None
        if self._scroll_target is None:
            return
        first = self.canvas.yview()[0]
        diff = self._scroll_target - first
        if abs(diff) < 0.001:
            self.canvas.yview_moveto(self._scroll_target)
            self._scroll_target = None
            return
        # 匀速为主 + 末段轻缓动：按比例走但限幅——起停都跟手，没有"追不上/拖尾巴"
        # 的滞留感。0.045 视口/帧（15ms）≈ 每秒 3 屏，一格滚轮 0.18 视口约 4 帧完成。
        step = max(-_SCROLL_SPEED, min(_SCROLL_SPEED, diff * 0.6))
        self.canvas.yview_moveto(first + step)
        self._scroll_job = self.after(15, self._animate_scroll)

    def _cancel_scroll_anim(self) -> None:
        if self._scroll_job is not None:
            try:
                self.after_cancel(self._scroll_job)
            except tk.TclError:
                pass
            self._scroll_job = None
        self._scroll_target = None

    def _pointer_inside(self) -> bool:
        try:
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            return False
        while widget is not None:
            if widget is self:
                return True
            widget = getattr(widget, "master", None)
        return False

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def scroll_to_top(self) -> None:
        self._cancel_scroll_anim()
        self.canvas.yview_moveto(0)


# ---------------------------------------------------------------------------
# 顶部统计块
# ---------------------------------------------------------------------------
class StatBlock(tk.Frame):
    def __init__(self, master: tk.Misc, label: str, value: str,
                 value_font=None, bg: Optional[str] = None) -> None:
        bg = bg or _parent_bg(master)
        super().__init__(master, bg=bg)
        self.value_label = tk.Label(
            self, text=value, bg=bg, fg=theme.TEXT, font=value_font or ui_font(13, "bold"), anchor="w",
        )
        self.value_label.pack(anchor="w")
        tk.Label(
            self, text=label, bg=bg, fg=theme.MUTED, font=ui_font(8), anchor="w",
        ).pack(anchor="w", pady=(1, 0))

    def set_value(self, text: str, color: str = theme.TEXT) -> None:
        self.value_label.configure(text=text, fg=color)


class StatusPill(tk.Canvas):
    """营业中 / 已打烊 状态胶囊。"""

    def __init__(self, master: tk.Misc, bg: Optional[str] = None) -> None:
        bg = bg or _parent_bg(master)
        super().__init__(master, width=104, height=28, bg=bg, highlightthickness=0, bd=0)
        self._bg = bg
        self.set_state("查询中", theme.MUTED)

    def set_state(self, text: str, color: str) -> None:
        self.delete("all")
        width = max(84, 26 + self._text_width(text) + 22)
        self.configure(width=width, height=28)
        round_rect(self, 1, 1, width - 1, 27, 13, fill=mix(color, self._bg, 0.82),
                   outline="", width=0)
        self.create_oval(14, 11, 22, 19, fill=color, outline="")
        self.create_text((28 + width - 8) / 2, 14, text=text, fill=color,
                         font=ui_font(9, "bold"))

    @staticmethod
    def _text_width(text: str) -> int:
        """粗略估算：中文按 13px，其余按 7px。"""
        return sum(13 if ord(ch) > 127 else 7 for ch in text)
