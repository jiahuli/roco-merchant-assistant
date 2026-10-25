"""悬浮窗：常驻前台的小窗，随时瞄一眼本轮商品。

特性
----
* 置顶：默认 `-topmost`，可在窗内一键切换
* 拖动：按住顶部标题栏拖动（位置持久化到 config.floating_geometry）
* 最小化：收起成一条 34px 的横条，只留商品数与倒计时
* 不干扰主程序：独立 Toplevel，不 grab、不抢焦点，仅被动接收数据更新
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from roco.platform import monitors
from roco.services.cache import ImageLoader
from roco.config import Config
from roco.core.models import MerchantSnapshot
from roco.ui import theme
from roco.ui.theme import round_rect, ui_font

MAX_ROWS = 5
HEADER_H = 38
ICON_PX = 18          # 行内物品图标尺寸


class FloatingWindow(tk.Toplevel):
    WIDTH = 302

    def __init__(self, master: tk.Misc, config: Config,
                 on_close: Optional[Callable[[], None]] = None,
                 on_activate: Optional[Callable[[], None]] = None,
                 loader: Optional[ImageLoader] = None,
                 on_refresh: Optional[Callable[[], None]] = None) -> None:
        super().__init__(master, bg=theme.BORDER)
        self.config_data = config
        # 与主窗口共用同一个加载器：磁盘缓存与内存缓存都能复用，不会重复请求
        self.loader = loader
        self._show_icons = bool(getattr(config, "floating_show_icons", True))
        self._icon_widgets: list[tk.Widget] = []
        self._on_close = on_close
        self._on_activate = on_activate
        # 悬浮窗自带的"立即获取"：不打开主窗口也能拉一次数据
        self._on_refresh = on_refresh
        self._refreshing = False
        self._topmost = bool(config.floating_topmost)
        self._collapsed = False
        self._rows: list[tk.Widget] = []
        self._snapshot: MerchantSnapshot | None = None
        self._countdown = "--:--:--"
        self._count_text = ""
        self._monitors = monitors.enum_monitors()
        self._placed = False

        self.overrideredirect(True)          # 无系统边框：不出现在任务栏，不抢焦点
        self.attributes("-topmost", self._topmost)
        # 不设 -alpha：悬浮窗需要不透明才看得清，半透明会把桌面内容透上来
        # ⚠️ 不要给悬浮窗上任何 Win32 扩展样式：WS_EX_COMPOSITED 会让无边框窗口的
        # 事件循环挂死（自测卡死实锤），DWM 圆角会内缩客户区与 geometry 回调打架
        self._build()
        self._restore_geometry()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        self.shell = tk.Frame(self, bg=theme.BORDER)
        self.shell.pack(fill="both", expand=True)

        self.card = tk.Frame(self.shell, bg=theme.SURFACE)
        self.card.pack(fill="both", expand=True, padx=1, pady=1)

        # ---- 标题栏（拖动区）----
        self.header = tk.Frame(self.card, bg=theme.SURFACE_2, height=HEADER_H)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.accent_bar = tk.Frame(self.header, bg=theme.ACCENT, width=3)
        self.accent_bar.pack(side="left", fill="y")

        self.title_label = tk.Label(
            self.header, text="◆ 远行商人", bg=theme.SURFACE_2, fg=theme.TEXT,
            font=ui_font(9, "bold"), anchor="w",
        )
        self.title_label.pack(side="left", padx=(9, 0))

        self.header_info = tk.Label(
            self.header, text="", bg=theme.SURFACE_2, fg=theme.MUTED, font=ui_font(8), anchor="w",
        )
        self.header_info.pack(side="left", padx=(8, 0))

        # 右侧按钮（先打包，保证不被挤掉）
        self.close_btn = self._icon_button("✕", self.close, "关闭悬浮窗")
        self.close_btn.pack(side="right", padx=(0, 6))
        self.min_btn = self._icon_button("–", self.toggle_collapse, "收起 / 展开")
        self.min_btn.pack(side="right", padx=(0, 2))
        self.pin_btn = self._icon_button("◉" if self._topmost else "○",
                                        self.toggle_topmost, "窗口置顶")
        self.pin_btn.pack(side="right", padx=(0, 2))
        # 放在最左侧（side=right 逆序打包），避免和关闭按钮挨着误点
        self.refresh_btn = self._icon_button("⟳", self.request_refresh, "立即获取最新物品")
        self.refresh_btn.pack(side="right", padx=(0, 2))

        for widget in (self.header, self.title_label, self.header_info, self.accent_bar):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Double-Button-1>", lambda _e: self._activate())

        # ---- 内容区 ----
        self.body = tk.Frame(self.card, bg=theme.SURFACE)
        self.body.pack(fill="both", expand=True, padx=0, pady=(0, 0))

        self.status_row = tk.Frame(self.body, bg=theme.SURFACE)
        self.status_row.pack(fill="x", padx=10, pady=(8, 4))

        self.status_label = tk.Label(
            self.status_row, text="等待数据…", bg=theme.SURFACE, fg=theme.MUTED, font=ui_font(9, "bold"),
            anchor="w",
        )
        self.status_label.pack(side="left")

        self.countdown_label = tk.Label(
            self.status_row, text="--:--:--", bg=theme.SURFACE, fg=theme.TEXT_DIM,
            font=ui_font(9, "bold"), anchor="e",
        )
        self.countdown_label.pack(side="right")

        self.rows_frame = tk.Frame(self.body, bg=theme.SURFACE)
        self.rows_frame.pack(fill="x", padx=10)

        self.footer = tk.Label(
            self.body, text="", bg=theme.SURFACE, fg=theme.MUTED, font=ui_font(8),
            anchor="w", justify="left",
        )
        self.footer.pack(fill="x", padx=10, pady=(6, 9))

        self._apply_size()

    def _icon_button(self, text: str, command: Callable[[], None], tip: str) -> tk.Label:
        # width=3 + 内边距：点击区域从约 18px 提到约 30px，和标题栏高度匹配
        label = tk.Label(
            self.header, text=text, bg=theme.SURFACE_2, fg=theme.MUTED, font=ui_font(11),
            width=3, padx=3, pady=2, cursor="hand2",
        )
        label.bind("<Enter>", lambda _e: label.configure(bg=theme.SURFACE_3, fg=theme.TEXT))
        label.bind("<Leave>", lambda _e: label.configure(bg=theme.SURFACE_2, fg=theme.MUTED))
        label.bind("<Button-1>", lambda _e: command())
        label._tip_text = tip  # type: ignore[attr-defined]
        return label

    # ------------------------------------------------------------------ 尺寸
    def _apply_size(self, height: Optional[int] = None) -> None:
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        if self._collapsed:
            height = HEADER_H + 2
        elif height is None:
            height = max(96, self.winfo_reqheight())
        if self._placed:
            # 只改尺寸，位置沿用当前值（不能拿 x<=0 判断"未定位"，
            # 副屏在主屏左边时 x 合法值就是负数）
            x, y = self.winfo_x(), self.winfo_y()
            x, y = monitors.clamp_drag(self._monitors, x, y, self.WIDTH, height)
        else:
            x, y = monitors.default_position(self._monitors, self.WIDTH, height)
        self.geometry(f"{self.WIDTH}x{height}+{x}+{y}")

    def _restore_geometry(self) -> None:
        """恢复上次位置；用真实显示器列表校验，支持副屏（含负坐标）。"""
        self._monitors = monitors.enum_monitors()
        saved = (self.config_data.floating_geometry or "").strip()
        x = y = None
        if saved:
            try:
                _size, x_text, y_text = saved.split("+")[:3]
                x, y = int(x_text), int(y_text)
            except (ValueError, IndexError):
                x = y = None

        self.update_idletasks()
        height = HEADER_H + 2 if self._collapsed else max(96, self.winfo_reqheight())

        if x is None or y is None or not monitors.is_visible_on(
            self._monitors, x, y, self.WIDTH, height, min_ratio=0.35
        ):
            # 没记录过，或那块屏已经不在了 → 回到主屏默认位置
            # （Windows 对完全移出显示器的窗口也是这么处理的）
            x, y = monitors.default_position(self._monitors, self.WIDTH, height)

        self.geometry(f"{self.WIDTH}x{height}+{x}+{y}")
        self._placed = True

    def save_geometry(self) -> None:
        try:
            self.config_data.floating_geometry = f"{self.WIDTH}x{self.winfo_height()}+{self.winfo_x()}+{self.winfo_y()}"
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 拖动
    def _drag_start(self, event) -> None:
        # 拖之前重新枚举：用户可能刚插拔/切换了显示器
        self._monitors = monitors.enum_monitors()
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event) -> None:
        offset = getattr(self, "_drag_offset", None)
        if not offset:
            return
        x = event.x_root - offset[0]
        y = event.y_root - offset[1]
        # 只在"整个虚拟桌面"范围内约束：允许跨屏与跨屏边界，
        # 但至少留 80px 可见，避免拖丢。绝对不能用主屏宽度来卡，
        # 否则副屏在主屏右侧时窗口会被顶回来。
        x, y = monitors.clamp_drag(self._monitors, x, y, self.WIDTH, self.winfo_height())
        self.geometry(f"+{x}+{y}")

    def _drag_end(self, _event=None) -> None:
        """松手即停在原地，不吸附、不回弹——与 Windows 原生窗口一致。"""
        self._drag_offset = None
        self.save_geometry()

    # ------------------------------------------------------------------ 交互
    def toggle_topmost(self) -> None:
        self._topmost = not self._topmost
        self.attributes("-topmost", self._topmost)
        self.pin_btn.configure(text="◉" if self._topmost else "○",
                               fg=theme.ACCENT if self._topmost else theme.MUTED)
        self.config_data.floating_topmost = self._topmost

    # ------------------------------------------------------------ 立即获取
    def request_refresh(self) -> None:
        """点「⟳」拉一次最新数据：主窗口藏在托盘里也能用。

        不自己发请求，只把动作交给主窗口（它已线程池 + 结果泵 + 去重），
        避免悬浮窗另起一套网络逻辑和重复渲染。
        """
        if self._refreshing or self._on_refresh is None:
            return
        self.set_refreshing(True)
        try:
            self._on_refresh()
        except Exception:
            self.set_refreshing(False)
            return
        # 兜底：查询可能被主窗口的 _loading 挡掉，别让按钮一直转
        try:
            self.after(8000, self.set_refreshing, False)
        except tk.TclError:
            pass

    def set_refreshing(self, refreshing: bool) -> None:
        """切换按钮状态；主窗口拿到结果后调用它复位。"""
        self._refreshing = bool(refreshing)
        text = "…" if self._refreshing else "⟳"
        try:
            if self.refresh_btn.winfo_exists():
                self.refresh_btn.configure(text=text,
                                           fg=theme.ACCENT if self._refreshing else theme.MUTED)
        except tk.TclError:
            pass

    @property
    def refreshing(self) -> bool:
        return self._refreshing

    def toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self.header_info.configure(
            text=f"{self._countdown} 后换货" if self._collapsed else self._count_text
        )
        if self._collapsed:
            self.body.pack_forget()
            self.accent_bar.configure(bg=theme.MUTED)
        else:
            self.body.pack(fill="both", expand=True)
            self.accent_bar.configure(bg=theme.ACCENT)
        self._apply_size()

    def _activate(self) -> None:
        if self._on_activate:
            self._on_activate()

    def close(self) -> None:
        self.save_geometry()
        if self._on_close:
            self._on_close()
        else:
            self.destroy()

    # ------------------------------------------------------------------ 数据
    def _clear_rows(self) -> None:
        for widget in self._rows:
            try:
                widget.destroy()
            except tk.TclError:
                pass
        self._rows.clear()
        self._icon_widgets.clear()

    def set_snapshot(self, snapshot: MerchantSnapshot) -> None:
        previous = self._snapshot
        self._snapshot = snapshot
        if previous is not None and previous.signature == snapshot.signature:
            return

        try:
            self._render(snapshot)
        except tk.TclError:
            pass

    def _render(self, snapshot: MerchantSnapshot) -> None:
        self._clear_rows()
        # 打烊后上游可能还挂着昨天 20 点那一轮，这里一律不列商品，
        # 否则悬浮窗会成为"24 点后仍显示昨日货架"的漏网之处。
        items = [] if snapshot.status == "closed" else snapshot.items[:MAX_ROWS]

        if snapshot.status == "closed":
            self.status_label.configure(text="已打烊", fg=theme.MUTED)
            self.accent_bar.configure(bg=theme.MUTED)
        elif items:
            self.status_label.configure(text=f"营业中 · 第 {snapshot.round_index or 0} 轮", fg=theme.SUCCESS)
            self.accent_bar.configure(bg=theme.ACCENT if not self._collapsed else theme.MUTED)
        else:
            self.status_label.configure(text="暂无商品", fg=theme.WARNING)

        if not items:
            tip = tk.Label(self.rows_frame, text="等待下一轮刷新", bg=theme.SURFACE, fg=theme.MUTED,
                           font=ui_font(9))
            tip.pack(fill="x", pady=10)
            self._rows.append(tip)
        else:
            for item in items:
                self._rows.append(self._make_row(item))
            if len(snapshot.items) > MAX_ROWS:
                more = tk.Label(
                    self.rows_frame, text=f"还有 {len(snapshot.items) - MAX_ROWS} 件…",
                    bg=theme.SURFACE, fg=theme.MUTED, font=ui_font(8), anchor="w",
                )
                more.pack(fill="x", pady=(2, 0))
                self._rows.append(more)

        self._count_text = f"{len(snapshot.items)} 件" if snapshot.items else ""
        self.header_info.configure(
            text=f"{self._countdown} 后换货" if self._collapsed else self._count_text
        )
        self.footer.configure(text=self._footer_text(snapshot))
        if not self._collapsed:
            self._apply_size()

    def _make_row(self, item) -> tk.Frame:
        row = tk.Frame(self.rows_frame, bg=theme.SURFACE)
        row.pack(fill="x", pady=2)

        # 物品图标（可关闭）。关掉时只是不创建这个控件，
        # 珍惜度圆点、名称、价格、倒计时等一概不受影响。
        if self._show_icons and self.loader is not None and item.icon_url:
            icon = tk.Canvas(row, width=ICON_PX, height=ICON_PX, bg=theme.SURFACE,
                             highlightthickness=0, bd=0)
            icon.create_rectangle(0, 0, ICON_PX, ICON_PX, fill=theme.SURFACE_2, outline="")
            icon.pack(side="left", padx=(0, 5), pady=(1, 0))
            self._icon_widgets.append(icon)
            self.loader.load(
                item.icon_url,
                lambda photo, canvas=icon: self._apply_row_icon(canvas, photo),
                size=ICON_PX,
            )

        dot = tk.Canvas(row, width=8, height=8, bg=theme.SURFACE, highlightthickness=0, bd=0)
        color = theme.RARITY_COLORS.get(item.rarity.level, theme.MUTED)
        dot.create_oval(1, 1, 7, 7, fill=color, outline="")
        dot.pack(side="left", pady=(4, 0))

        price = tk.Label(row, text=item.price_text, bg=theme.SURFACE, fg=theme.MONEY,
                         font=ui_font(9, "bold"), anchor="e")
        price.pack(side="right")

        name = tk.Label(row, text=item.name, bg=theme.SURFACE, fg=theme.TEXT_DIM, font=ui_font(9), anchor="w")
        name.pack(side="left", padx=(6, 6), fill="x", expand=True)

        return row

    def _apply_row_icon(self, canvas: tk.Canvas, photo) -> None:
        if photo is None:
            return
        try:
            if not canvas.winfo_exists():
                return
            canvas.delete("all")
            canvas.create_image(ICON_PX / 2, ICON_PX / 2, image=photo)
            canvas.image = photo        # 保住引用，避免被回收
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 图标开关
    def set_show_icons(self, enabled: bool) -> None:
        """开关行内物品图标；只重排列表，窗口位置与状态都不受影响。"""
        enabled = bool(enabled)
        if enabled == self._show_icons:
            return
        self._show_icons = enabled
        self.config_data.floating_show_icons = enabled
        if self._snapshot is not None:
            try:
                self._render(self._snapshot)
                self._apply_size()
            except tk.TclError:
                pass

    @property
    def show_icons(self) -> bool:
        return self._show_icons

    def attach_loader(self, loader: Optional[ImageLoader]) -> None:
        """主窗口换了图片加载器（例如改了图标缓存目录）时同步过来并重绘。"""
        self.loader = loader
        if self._snapshot is not None and self._show_icons:
            try:
                self._render(self._snapshot)
            except tk.TclError:
                pass

    @staticmethod
    def _footer_text(snapshot: MerchantSnapshot) -> str:
        if snapshot.status == "closed":
            return "商人 08:00 出摊"
        if snapshot.next_refresh:
            return f"下一轮 {snapshot.next_refresh[-5:]} 换货"
        return ""

    def update_countdown(self, text: str) -> None:
        self._countdown = text
        try:
            self.countdown_label.configure(text=text)
            if self._collapsed:
                self.header_info.configure(text=f"{text} 后换货")
        except tk.TclError:
            pass
