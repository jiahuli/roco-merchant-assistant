"""主窗口：标题区 / 工具栏 / 商品货架 / 状态栏。"""
from __future__ import annotations

import json
import queue
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from tkinter import messagebox
import tkinter as tk

from cache import ICON_SIZE, IconCache, ImageLoader, has_pillow, migrate_cache
import applog
import autostart
import monitors
import tray as tray_module
from config import Config, resolve_icon_cache_dir, resource_path, state_path
from models import MerchantSnapshot
from notifier import Notifier
from providers import CN_TZ, ProviderError, fetch_snapshot, next_refresh_at
from rarity import RARITY_TABLE
from scheduler import Scheduler, format_countdown, format_delta
from ui.floating_window import FloatingWindow
from ui.settings_dialog import SettingsDialog
from ui.theme import (ACCENT, BG, BORDER, DANGER, MUTED, SUCCESS, SURFACE, SURFACE_2,
                      TEXT, TEXT_DIM, WARNING, mono_font, ui_font)
from ui.widgets import ItemCard, PillButton, ScrollFrame, StatBlock, StatusPill


class MerchantApp:
    def __init__(self, root: tk.Tk, config: Config) -> None:
        self.root = root
        self.config = config

        self.cache_path, self._cache_warning = resolve_icon_cache_dir(config)
        self.cache = IconCache(self.cache_path, config.icon_cache_enabled)
        self.loader = ImageLoader(root, self.cache, ICON_SIZE)
        self.notifier = Notifier(root, config)
        self.scheduler = Scheduler(
            root, config.query_times, on_fire=lambda: self.refresh(reason="定时任务"),
            on_tick=self._on_tick,
        )
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fetch")
        self._results: queue.Queue = queue.Queue()
        self._show_requests: queue.Queue = queue.Queue()
        self._tray_commands: queue.Queue = queue.Queue()
        self.tray: tray_module.TrayIcon | None = None
        self._tray_hint_shown = False
        self._quitting = False

        self.snapshot: MerchantSnapshot | None = None
        self.floating: FloatingWindow | None = None
        self._loading = False
        self._cards: list[ItemCard] = []
        self._columns = 0
        self._resize_job: str | None = None
        self._countdown_target: datetime | None = None
        self._error_text = ""

        self._setup_window()
        self._build_ui()
        applog.install_tk_hook(self.root, self._on_ui_error)
        self._start_tray()
        applog.info("主窗口就绪 | 显示器 %s 块 | 渠道 %s | 定时 %s",
                    len(monitors.enum_monitors()), config.provider,
                    ",".join(config.query_times))
        self.scheduler.start()
        self._pump_results()
        self.root.after(220, lambda: self.refresh(reason="启动"))
        if config.floating_enabled:
            self.root.after(400, lambda: self.toggle_floating(True, quiet=True))

    # -------------------------------------------------------------- 结果泵
    def _pump_results(self) -> None:
        """在主线程消化后台请求结果（Tk 不允许跨线程操作控件）。"""
        while True:
            try:
                future, reason = self._results.get_nowait()
            except queue.Empty:
                break
            self._on_fetched(future, reason)
        while True:
            try:
                self._show_requests.get_nowait()
            except queue.Empty:
                break
            self.show_from_background()
        while True:
            try:
                command = self._tray_commands.get_nowait()
            except queue.Empty:
                break
            self._handle_tray_command(command)
        if not getattr(self, "_closing", False):
            self.root.after(80, self._pump_results)

    # ------------------------------------------------------------------ 窗口
    def _setup_window(self) -> None:
        self.root.title("远行商人助手 · 洛克王国：世界")
        self.root.configure(bg=BG)
        self.root.minsize(1000, 660)
        self.root.geometry(self._safe_geometry())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        icon_file = resource_path("assets/app.ico")
        if icon_file.exists():
            try:
                self.root.iconbitmap(default=str(icon_file))
            except tk.TclError:
                pass
        try:
            icon = tk.PhotoImage(width=32, height=32)
            icon.put(ACCENT, to=(4, 4, 28, 28))
            icon.put(SURFACE, to=(10, 10, 22, 22))
            self.root.iconphoto(True, icon)
            self._icon = icon
        except tk.TclError:
            pass
        self.root.bind("<F5>", lambda _e: self.refresh(reason="手动刷新"))
        self.root.bind("<Control-comma>", lambda _e: self.open_settings())

    def _safe_geometry(self) -> str:
        """恢复上次的窗口大小/位置。

        用真实显示器列表做校验：只要还有足够面积落在**任意一块屏**上就沿用，
        这样放在副屏（x 可能大于主屏宽度、也可能是负数）的窗口能正确回来；
        屏幕拔掉导致完全不可见时，才回落到主屏默认位置。
        """
        default_size = (1180, 760)
        saved = (self.config.window_geometry or "").strip()
        if not saved or "+" not in saved:
            return f"{default_size[0]}x{default_size[1]}"

        try:
            size, x_text, y_text = saved.split("+")[:3]
            width = int(size.split("x")[0])
            height = int(size.split("x")[1])
            x, y = int(x_text), int(y_text)
        except (ValueError, IndexError):
            return f"{default_size[0]}x{default_size[1]}"

        screens = monitors.enum_monitors()
        if width < 1000 or height < 600:
            width, height = default_size

        # 尺寸不能超过目标屏工作区
        target = monitors.best_monitor(screens, x, y, width, height)
        width = min(width, target.work_width)
        height = min(height, target.work_height)

        if monitors.is_visible_on(screens, x, y, width, height, min_ratio=0.5):
            return f"{width}x{height}+{x}+{y}"

        # 记下的位置已经不在任何显示器上（比如副屏被拔了）→ 回到主屏居中
        primary = monitors.primary_monitor(screens)
        cx = primary.work_x + max(0, (primary.work_width - width) // 2)
        cy = primary.work_y + max(0, (primary.work_height - height) // 3)
        return f"{width}x{height}+{cx}+{cy}"

    # ------------------------------------------------------------------ 布局
    def _build_ui(self) -> None:
        self._build_header()

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        self._build_toolbar()

        self._scroll = ScrollFrame(self.root, bg=BG)
        self._scroll.pack(fill="both", expand=True)
        self._scroll.canvas.bind("<Configure>", self._on_canvas_resize, add="+")

        self.body = self._scroll.body
        self._grid = tk.Frame(self.body, bg=BG)
        self._grid.pack(fill="both", expand=True, padx=20, pady=(2, 22))
        self._build_statusbar()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=24, pady=(20, 16))

        left = tk.Frame(header, bg=BG)
        left.pack(side="left", anchor="w")
        title_row = tk.Frame(left, bg=BG)
        title_row.pack(anchor="w")
        tk.Label(
            title_row, text="远行商人", bg=BG, fg=TEXT, font=ui_font(21, "bold"),
        ).pack(side="left")
        tk.Label(
            title_row, text="今日货架", bg=BG, fg=ACCENT, font=ui_font(21, "bold"),
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            left, text="《洛克王国：世界》· 每 4 小时一刷 · 08:00 / 12:00 / 16:00 / 20:00",
            bg=BG, fg=MUTED, font=ui_font(9),
        ).pack(anchor="w", pady=(4, 0))

        right = tk.Frame(header, bg=BG)
        right.pack(side="right", anchor="e")

        self.stat_items = StatBlock(right, "在售商品", "—", ui_font(13, "bold"), bg=BG)
        self.stat_items.grid(row=0, column=0, sticky="w", padx=(0, 26), pady=(7, 0))

        self.stat_round = StatBlock(right, "当前轮次", "—", ui_font(13, "bold"), bg=BG)
        self.stat_round.grid(row=0, column=1, sticky="w", padx=(0, 22), pady=(7, 0))

        tk.Frame(right, bg=BORDER, width=1, height=34).grid(
            row=0, column=2, sticky="ns", padx=(0, 22), pady=(7, 0)
        )

        countdown_box = tk.Frame(right, bg=BG)
        countdown_box.grid(row=0, column=3, sticky="e", padx=(0, 18), pady=(0, 0))
        self.countdown_label = tk.Label(
            countdown_box, text="--:--:--", bg=BG, fg=TEXT, font=mono_font(20),
        )
        self.countdown_label.pack(anchor="e")
        self.countdown_hint = tk.Label(
            countdown_box, text="距离下一轮换货", bg=BG, fg=MUTED, font=ui_font(8),
        )
        self.countdown_hint.pack(anchor="e")

        self.pill = StatusPill(right, bg=BG)
        self.pill.grid(row=0, column=4, sticky="e", pady=(6, 0))

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=24, pady=(12, 10))

        self.refresh_btn = PillButton(bar, "立即刷新", self.refresh, style="primary", width=104)
        self.refresh_btn.pack(side="left")

        tk.Label(bar, text="数据渠道", bg=BG, fg=MUTED, font=ui_font(8)).pack(
            side="left", padx=(14, 6)
        )
        self.channel_btn = PillButton(
            bar, self._channel_text(), self._open_channel_menu, width=186, height=34
        )
        self.channel_btn.pack(side="left")

        PillButton(bar, "设置", self.open_settings, width=72).pack(side="left", padx=(10, 0))

        self.float_btn = PillButton(
            bar, self._float_text(), lambda: self.toggle_floating(), width=104, height=34
        )
        self.float_btn.pack(side="left", padx=(10, 0))

        self.notify_btn = PillButton(
            bar, self._notify_text(), self._toggle_notify, width=96, height=34
        )
        self.notify_btn.pack(side="right")

        self.cache_label = tk.Label(
            bar, text="", bg=BG, fg=MUTED, font=ui_font(8), anchor="e",
        )
        self.cache_label.pack(side="right", padx=(0, 14))

    def _build_statusbar(self) -> None:
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", side="bottom")
        bar = tk.Frame(self.root, bg=SURFACE)
        bar.pack(fill="x", side="bottom")
        inner = tk.Frame(bar, bg=SURFACE)
        inner.pack(fill="x", padx=24, pady=8)

        self.status_left = tk.Label(
            inner, text="准备就绪", bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="w",
        )
        self.status_left.pack(side="left")
        self.status_right = tk.Label(
            inner, text="", bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="e",
        )
        self.status_right.pack(side="right")

    # ------------------------------------------------------------------ 文案
    def _channel_text(self) -> str:
        label = self.config.provider_label(self.config.provider)
        if len(label) > 14:
            label = label[:13] + "…"
        return f"{label}   ▾"

    def _notify_text(self) -> str:
        return "通知：开" if self.config.notify_enabled else "通知：关"

    def _float_text(self) -> str:
        return "悬浮窗：开" if self.config.floating_enabled else "悬浮窗：关"

    # ------------------------------------------------------------------ 悬浮窗
    def toggle_floating(self, enabled: bool | None = None, quiet: bool = False) -> None:
        """开关悬浮窗并持久化。quiet=True 用于按配置恢复，不弹提示。"""
        target = (not self.config.floating_enabled) if enabled is None else bool(enabled)
        self.config.floating_enabled = target

        if target:
            if self.floating is None or not self.floating.winfo_exists():
                self.floating = FloatingWindow(
                    self.root, self.config,
                    on_close=self._on_floating_closed,
                    on_activate=self.show_from_background,
                )
            self.floating.deiconify()
            self.floating.attributes("-topmost", self.config.floating_topmost)
            if self.snapshot is not None:
                self.floating.set_snapshot(self.snapshot)
            self.floating.update_countdown(self.countdown_label.cget("text"))
        else:
            if self.floating is not None:
                try:
                    self.floating.save_geometry()
                    self.floating.destroy()
                except tk.TclError:
                    pass
            self.floating = None

        self.config.save()
        self.float_btn.set_text(self._float_text())
        if not quiet:
            self.status_left.configure(
                text="悬浮窗已开启，可拖动标题栏移动、点「–」收起" if target else "悬浮窗已关闭",
                fg=MUTED,
            )

    def _on_floating_closed(self) -> None:
        if self.floating is not None:
            try:
                self.floating.save_geometry()
                self.floating.destroy()
            except tk.TclError:
                pass
        self.floating = None
        self.config.floating_enabled = False
        self.config.save()
        self.float_btn.set_text(self._float_text())

    def request_show(self) -> None:
        """线程安全的"请露面"请求（供单实例监听线程调用）。"""
        self._show_requests.put(True)

    def show_from_background(self) -> None:
        """从通知/托盘/悬浮窗把主窗口叫到前台。"""
        self.show_window()

    # ------------------------------------------------------------------ 数据
    def refresh(self, reason: str = "手动刷新") -> None:
        if self._loading:
            return
        self._loading = True
        self.refresh_btn.set_enabled(False)
        self.refresh_btn.set_text("查询中…")
        self.pill.set_state("查询中", WARNING)
        self.status_left.configure(text=f"{reason} · 正在请求数据…", fg=MUTED)

        future = self.pool.submit(fetch_snapshot, self.config)
        future.add_done_callback(lambda f: self._results.put((f, reason)))

    def _on_fetched(self, future, reason: str) -> None:
        self._loading = False
        self.refresh_btn.set_enabled(True)
        self.refresh_btn.set_text("立即刷新")
        try:
            snapshot, errors = future.result()
        except ProviderError as exc:
            self._on_fetch_failed(str(exc), reason)
            return
        except Exception as exc:  # noqa: BLE001
            self._on_fetch_failed(f"{type(exc).__name__}: {exc}", reason)
            return

        self.snapshot = snapshot
        self._error_text = ""
        applog.info("查询成功 | %s | 第%s轮 | %s 件 | 更新于 %s",
                    snapshot.source_label, snapshot.round_index,
                    len(snapshot.items), snapshot.fetched_at.strftime("%H:%M:%S"))
        self._render(snapshot)
        if errors:
            self.status_left.configure(
                text=f"{reason} · 已降级到「{snapshot.source_label}」（原渠道：{errors[0][:60]}）",
                fg=WARNING,
            )
        else:
            self.status_left.configure(
                text=f"{reason} · 数据来源：{snapshot.source_label} · "
                     f"{snapshot.fetched_at.strftime('%H:%M:%S')} 更新",
                fg=MUTED,
            )
        self._maybe_notify(snapshot)

    def _on_ui_error(self, name: str, message: str) -> None:
        """界面回调里抛出的异常：状态栏提示 + 首次弹窗，堆栈已由日志钩子记录。"""
        try:
            self.status_left.configure(text=f"界面出错：{name}: {message}（详见错误日志）", fg=DANGER)
        except tk.TclError:
            pass
        if not getattr(self, "_ui_error_shown", False):
            self._ui_error_shown = True
            try:
                messagebox.showerror(
                    "出现异常",
                    f"{name}: {message}\n\n详细信息已写入错误日志，"
                    "可在「设置 → 错误日志」里查看。",
                    parent=self.root,
                )
            except tk.TclError:
                pass

    def open_log_viewer(self) -> None:
        from ui.log_viewer import LogViewer

        LogViewer(self.root)

    def _on_fetch_failed(self, message: str, reason: str) -> None:
        applog.error("查询失败 | %s | %s", reason, message.replace("\n", " / "))
        self._error_text = message
        self.pill.set_state("查询失败", DANGER)
        self.status_left.configure(text=f"{reason} · 失败：{message.splitlines()[0][:80]}", fg=DANGER)
        if self.snapshot is None:
            self._render_error(message)
        self._update_cache_label()

    # ------------------------------------------------------------------ 渲染
    def _reset_content(self) -> None:
        """清空内容区；货架容器按需重建（clear() 会连它一起销毁）。"""
        self._scroll.clear()
        self._cards.clear()
        self._columns = 0
        self._grid = None

    def _ensure_grid(self) -> tk.Frame:
        if self._grid is None or not self._grid.winfo_exists():
            head = tk.Frame(self.body, bg=BG)
            head.pack(fill="x", padx=26, pady=(8, 2))
            tk.Label(head, text="本轮货架", bg=BG, fg=TEXT, font=ui_font(10, "bold")).pack(side="left")
            self._shelf_count = tk.Label(head, text="", bg=BG, fg=MUTED, font=ui_font(9))
            self._shelf_count.pack(side="left", padx=(8, 0), pady=(1, 0))
            self._shelf_note = tk.Label(head, text="", bg=BG, fg=MUTED, font=ui_font(8))
            self._shelf_note.pack(side="right", pady=(2, 0))

            self._grid = tk.Frame(self.body, bg=BG)
            self._grid.pack(fill="both", expand=True, padx=20, pady=(4, 22))
        return self._grid

    def _render(self, snapshot: MerchantSnapshot) -> None:
        self._reset_content()

        opened = snapshot.is_open and bool(snapshot.items)
        if opened:
            self.pill.set_state("营业中", SUCCESS)
        elif snapshot.status == "closed":
            self.pill.set_state("已打烊", MUTED)
        else:
            self.pill.set_state("暂无商品", WARNING)

        self.stat_round.set_value(f"第 {snapshot.round_index} 轮" if snapshot.round_index else "—")
        self.stat_items.set_value(f"{len(snapshot.items)} 件")

        # 倒计时目标：营业中盯"下一轮换货"，打烊时盯"下次出摊"
        now_cn = datetime.now(CN_TZ)
        if snapshot.status == "closed":
            self._countdown_target = self._next_open(now_cn)
            self.countdown_hint.configure(text="距离商人出摊")
        else:
            self._countdown_target = self._parse_refresh(snapshot)
            self.countdown_hint.configure(text="距离下一轮换货")

        self._sync_floating(snapshot)

        if not opened:
            if snapshot.status == "closed":
                open_at = self._next_open(now_cn)
                day_text = "今天" if open_at.date() == now_cn.date() else "明天"
                self._show_placeholder(
                    "商人已打烊",
                    "远行商人每天 08:00 开始营业，00:00 - 08:00 不出摊。\n"
                    f"下一次出现：{day_text} {open_at.strftime('%H:%M')}",
                    accent=MUTED,
                )
            else:
                self._show_placeholder(
                    "本轮暂无商品",
                    "接口没有返回本轮的货架数据，可能是商人正在换货，稍后刷新试试。",
                    accent=WARNING,
                )
            self._update_status_right()
            return

        grid = self._ensure_grid()
        level = self.config.notify_min_level
        special = len(snapshot.above_level(level))
        label = RARITY_TABLE.get(level, RARITY_TABLE[4]).label
        shelf_text = f"共 {len(snapshot.items)} 件"
        if special:
            shelf_text += f" · {label}及以上 {special} 件"
        self._shelf_count.configure(text=shelf_text)
        note = f"下一轮 {self._countdown_target.strftime('%H:%M')} 换货" if self._countdown_target else ""
        self._shelf_note.configure(text=note)

        for index, item in enumerate(snapshot.items):
            card = ItemCard(grid, item, self.loader, index)
            self._cards.append(card)

        self._layout_cards(force=True)
        self._update_status_right()
        self._update_cache_label()
        self._sync_tray_tooltip()

    def _render_error(self, message: str) -> None:
        self._reset_content()
        self._show_placeholder(
            "查询失败",
            message.strip() + "\n\n可在「设置 → 数据渠道」里更换接口地址。",
            accent=DANGER,
            action=("重试", lambda: self.refresh(reason="手动刷新")),
        )

    def _show_placeholder(self, title: str, message: str, accent: str = MUTED,
                          action=None) -> None:
        holder = tk.Frame(self.body, bg=BG)
        holder.pack(fill="both", expand=True, pady=(50, 0))
        tk.Label(holder, text="◍", bg=BG, fg=accent, font=ui_font(30)).pack()
        tk.Label(holder, text=title, bg=BG, fg=TEXT, font=ui_font(15, "bold")).pack(pady=(8, 0))
        tk.Label(
            holder, text=message, bg=BG, fg=MUTED, font=ui_font(9),
            justify="center", wraplength=560,
        ).pack(pady=(8, 0))
        if action:
            label, command = action
            PillButton(holder, label, command, style="primary", width=96).pack(pady=(16, 0))

    # ------------------------------------------------------------------ 栅格
    def _on_canvas_resize(self, _event=None) -> None:
        if self._resize_job:
            try:
                self.root.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        self._resize_job = self.root.after(110, self._layout_cards)

    def _layout_cards(self, force: bool = False) -> None:
        self._resize_job = None
        if not self._cards or self._grid is None:
            return
        width = self._scroll.canvas.winfo_width() or 1100
        columns = max(1, min(4, (width - 40) // 340))
        if not force and columns == self._columns:
            return
        self._columns = columns

        for column in range(5):
            self._grid.columnconfigure(column, weight=1 if column < columns else 0,
                                       uniform="cards" if column < columns else "")
        for index, card in enumerate(self._cards):
            card.grid(
                row=index // columns, column=index % columns,
                sticky="nsew", padx=6, pady=6,
            )

    # ------------------------------------------------------------------ 时间
    @staticmethod
    def _next_open(now_cn: datetime) -> datetime:
        """下一个 08:00（严格晚于当前时刻）。"""
        candidate = now_cn.replace(hour=8, minute=0, second=0, microsecond=0)
        if candidate <= now_cn:
            candidate = candidate + timedelta(days=1)
        return candidate

    def _parse_refresh(self, snapshot: MerchantSnapshot) -> datetime:
        text = snapshot.next_refresh
        if text:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt).replace(tzinfo=CN_TZ)
                except ValueError:
                    continue
        return next_refresh_at(datetime.now(CN_TZ))

    def _sync_tray_tooltip(self) -> None:
        if self.tray is not None and self.tray.available:
            try:
                self.tray.set_tooltip(self._tray_tooltip())
            except tk.TclError:
                pass

    def _sync_floating(self, snapshot: MerchantSnapshot) -> None:
        """把最新快照推给悬浮窗；悬浮窗未开启时什么也不做。"""
        if self.floating is None:
            return
        try:
            if not self.floating.winfo_exists():
                self.floating = None
                return
            self.floating.set_snapshot(snapshot)
            self.floating.update_countdown(
                self.countdown_label.cget("text") or "--:--:--"
            )
        except tk.TclError:
            self.floating = None

    def _on_tick(self, now: datetime) -> None:
        now_cn = datetime.now(CN_TZ)
        if self._countdown_target is not None:
            remain = self._countdown_target - now_cn
            if remain.total_seconds() <= 0 and not self._loading:
                # 到点了但数据还是旧的，说明该刷新了
                self.countdown_label.configure(text="换货中", fg=WARNING)
            else:
                self.countdown_label.configure(text=format_countdown(self._countdown_target, now_cn))
        if self.floating is not None:
            try:
                self.floating.update_countdown(self.countdown_label.cget("text"))
            except tk.TclError:
                self.floating = None
        self._update_status_right(now)

    def _update_status_right(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        next_run = self.scheduler.next_run(now)
        if next_run:
            delta = format_delta(next_run - now)
            self.status_right.configure(text=f"下次自动查询 {next_run.strftime('%H:%M')} · {delta} 后")
        else:
            self.status_right.configure(text="未设置自动查询时间")

    def _update_cache_label(self) -> None:
        hits, misses = self.cache.stats()
        total = hits + misses
        text = f"图标缓存 {self.cache.count()} 张"
        if total:
            text += f" · 命中 {hits}/{total}"
        if has_pillow():
            text += " · Pillow"
        self.cache_label.configure(text=text)

    # ------------------------------------------------------------------ 通知
    def _maybe_notify(self, snapshot: MerchantSnapshot) -> None:
        if not self.config.notify_enabled:
            return
        specials = snapshot.above_level(self.config.notify_min_level)
        if not specials:
            return

        round_key = f"{snapshot.started_at or ''}|{snapshot.round_index or 0}"
        signatures = [item.signature for item in specials]
        previous = self._load_state()

        if previous.get("round_key") == round_key:
            already = set(previous.get("signatures") or [])
            fresh = [item for item in specials if item.signature not in already]
        else:
            fresh = specials

        if not fresh:
            return
        self.notifier.alert_special(fresh)
        self._save_state({"round_key": round_key, "signatures": signatures})

    def _load_state(self) -> dict:
        try:
            return json.loads(state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, data: dict) -> None:
        try:
            state_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ 交互
    def _open_channel_menu(self) -> None:
        menu = tk.Menu(
            self.root, tearoff=0, bg=SURFACE_2, fg=TEXT, activebackground=ACCENT,
            activeforeground="#ffffff", bd=0, relief="flat", font=ui_font(9),
        )
        official_ready = bool(
            str(self.config.provider_conf("official").get("base_url") or "").strip()
        )
        if not official_ready:
            menu.add_command(
                label="官方接口尚未开放，当前使用社区渠道兜底",
                state="disabled", font=ui_font(8),
            )
            menu.add_separator()
        for pid in self.config.provider_order():
            conf = self.config.provider_conf(pid)
            label = conf.get("label", pid)
            configured = bool(str(conf.get("base_url") or "").strip())
            if pid == "shallow" and not str(conf.get("api_key") or "").strip():
                configured = False
            prefix = "● " if pid == self.config.provider else "○ "
            suffix = "" if configured else "（未配置）"
            menu.add_command(
                label=f"{prefix}{label}{suffix}",
                command=lambda p=pid: self._switch_provider(p),
            )
        menu.add_separator()
        menu.add_command(label="打开设置…", command=self.open_settings)
        try:
            menu.tk_popup(self.channel_btn.winfo_rootx(),
                          self.channel_btn.winfo_rooty() + self.channel_btn.winfo_height() + 4)
        finally:
            menu.grab_release()

    def _switch_provider(self, pid: str) -> None:
        if pid == self.config.provider:
            return
        conf = self.config.provider_conf(pid)
        if not str(conf.get("base_url") or "").strip():
            messagebox.showinfo(
                "该渠道未配置",
                f"「{conf.get('label', pid)}」还没有填写接口地址。\n"
                "官方目前未开放此接口，请到设置里填入可用地址，或改用社区渠道。",
                parent=self.root,
            )
            return
        self.config.provider = pid
        self.config.save()
        self.channel_btn.set_text(self._channel_text())
        self.refresh(reason=f"切换到 {self.config.provider_label(pid)}")

    def _toggle_notify(self) -> None:
        self.config.notify_enabled = not self.config.notify_enabled
        self.config.save()
        self.notify_btn.set_text(self._notify_text())

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.root, self.config, self.cache, on_saved=self._apply_settings)
        self.root.wait_window(dialog)

    def _apply_settings(self, new_config: Config) -> None:
        old_cache_path = self.cache_path
        old_log_dir = (self.config.log_dir or "").strip()
        self.config = new_config
        applog.info("设置已保存 | 渠道=%s | 定时=%s | 通知=%s | 悬浮窗=%s | 自启=%s",
                    new_config.provider, ",".join(new_config.query_times),
                    new_config.notify_enabled, new_config.floating_enabled,
                    new_config.autostart)

        # 日志目录变了就换文件，后续日志写到新位置
        if (new_config.log_dir or "").strip() != old_log_dir:
            _, warning = applog.resolve_log_dir(new_config)
            path = applog.setup(new_config)
            if warning:
                messagebox.showwarning("日志目录", warning, parent=self.root)
            applog.info("日志目录已切换 -> %s", path)
        self.notifier.update_config(new_config)
        self.scheduler.set_times(new_config.query_times)
        self.channel_btn.set_text(self._channel_text())
        self.notify_btn.set_text(self._notify_text())

        # 缓存目录换了：迁移旧图标 + 换掉缓存与加载器
        new_path, warning = resolve_icon_cache_dir(new_config)
        self._cache_warning = warning
        if new_path != old_cache_path:
            copied, _skipped = migrate_cache(old_cache_path, new_path)
            self.loader.shutdown()
            self.cache_path = new_path
            self.cache = IconCache(new_path, new_config.icon_cache_enabled)
            self.loader = ImageLoader(self.root, self.cache, ICON_SIZE)
            if copied:
                self._cache_warning = f"已把 {copied} 张图标迁移到新目录"

        # 开机自启：注册表与配置对齐
        if autostart.is_supported():
            ok, message = autostart.sync(new_config.autostart)
            if not ok:
                messagebox.showwarning("开机自启", message, parent=self.root)

        # 悬浮窗
        self.toggle_floating(new_config.floating_enabled, quiet=True)
        self.float_btn.set_text(self._float_text())

        self._update_status_right()
        self.refresh(reason="设置已更新")

    # ------------------------------------------------------------------ 托盘
    def _start_tray(self) -> None:
        """建立托盘图标；失败则退回"关闭即退出"，不会让程序无法关闭。"""
        if not self.config.tray_enabled:
            return
        icon = resource_path("assets/app.ico")
        self.tray = tray_module.TrayIcon(
            tooltip=self._tray_tooltip(),
            icon_path=str(icon),
            menu_provider=self._tray_menu,
            on_command=lambda cmd: self._tray_commands.put(cmd),
        )
        if self.tray.start():
            applog.info("托盘图标已就绪")
        else:
            applog.warning("托盘图标创建失败，关闭窗口将直接退出")
            self.tray = None

    def _tray_tooltip(self) -> str:
        snapshot = self.snapshot
        if snapshot is None:
            return "远行商人助手 · 正在查询"
        if snapshot.status == "closed":
            return f"远行商人助手 · 已打烊 · {snapshot.items and len(snapshot.items) or 0} 件"
        countdown = ""
        try:
            countdown = self.countdown_label.cget("text")
        except tk.TclError:
            pass
        text = f"远行商人助手 · 营业中 第{snapshot.round_index or 0}轮 · {len(snapshot.items)} 件"
        return f"{text} · {countdown} 后换货" if countdown and countdown != "--:--:--" else text

    def _tray_menu(self) -> list:
        """每次右键都重建，勾选状态才能反映最新配置。"""
        item = tray_module.MenuItem
        items = [
            item("显示主窗口", "show"),
            item("立即刷新", "refresh"),
            item(separator=True),
            item("悬浮窗", "float", checked=self.config.floating_enabled),
            item("通知提醒", "notify", checked=self.config.notify_enabled),
        ]
        if autostart.is_supported():
            items.append(item("开机自启", "autostart", checked=autostart.is_enabled()))
        items.extend([
            item(separator=True),
            item("错误日志", "logs"),
            item("退出", "quit"),
        ])
        return items

    def _handle_tray_command(self, command: str) -> None:
        if command == "show":
            self.show_window()
        elif command == "refresh":
            self.refresh(reason="托盘手动刷新")
        elif command == "float":
            self.toggle_floating()
        elif command == "notify":
            self._toggle_notify()
        elif command == "autostart":
            target = not self.config.autostart
            ok, message = autostart.sync(target)
            if ok:
                self.config.autostart = target
                self.config.save()
            else:
                messagebox.showwarning("开机自启", message, parent=self.root)
        elif command == "logs":
            self.show_window()
            self.open_log_viewer()
        elif command == "quit":
            self.quit_app()

    # ------------------------------------------------------------------ 显示/隐藏
    def show_window(self) -> None:
        """把主窗口恢复并顶到前面（可能在托盘里）。"""
        try:
            if self.root.state() == "withdrawn":
                self.root.deiconify()
                applog.info("窗口已从托盘恢复")
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(320, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

    def hide_to_tray(self, save_geometry: bool = True) -> None:
        """隐藏到托盘，进程与定时任务继续跑。

        save_geometry=False 用于开机自启：那时窗口还没真正映射，
        winfo_geometry() 会返回 1x1+0+0，写进配置会把上次的位置弄丢。
        """
        if save_geometry:
            try:
                self.config.window_geometry = self.root.winfo_geometry()
                self.config.save()
            except Exception:
                pass
        self.root.withdraw()
        applog.info("窗口已隐藏到托盘")
        if self.tray is not None and not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.balloon(
                "仍在后台运行",
                "已最小化到系统托盘，定时查询照常进行。双击托盘图标可恢复窗口，右键可退出。",
            )

    # ------------------------------------------------------------------ 关闭
    def _on_close(self) -> None:
        """点 × 的行为：默认最小化到托盘；托盘不可用时才真正退出。"""
        can_tray = (
            self.config.close_to_tray
            and self.tray is not None
            and self.tray.available
        )
        if can_tray:
            self.hide_to_tray()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        """彻底退出（托盘菜单的"退出"或托盘不可用时的关闭）。"""
        if self._quitting:
            return
        applog.info("程序退出")
        self._quitting = True
        self._closing = True
        try:
            self.config.window_geometry = self.root.winfo_geometry()
            if self.floating is not None:
                try:
                    self.floating.save_geometry()
                    self.floating.destroy()
                except tk.TclError:
                    pass
                self.floating = None
            self.config.save()
        except Exception:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.scheduler.stop()
        self.loader.shutdown()
        self.pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.root.destroy()
        except tk.TclError:
            pass
