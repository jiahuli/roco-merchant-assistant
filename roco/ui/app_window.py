"""主窗口（视图层）：标题区 / 工具栏 / 商品货架 / 状态栏。

1.9 重构后的职责边界：本类只负责**画**与窗口/托盘/悬浮窗/设置面板的生命周期；
取数编排、换货校验重试、历史落盘、通知决策等数据生命周期全部在
`app_controller.AppController`（见其模块 docstring 的视图协议）。
`selftest` 依赖的内部钩子（_render/_watcher/_loading 等）以属性转发的
方式保持原名可用。
"""
from __future__ import annotations

import queue
from datetime import datetime, timedelta
from tkinter import messagebox
import tkinter as tk

from roco.services.cache import ICON_SIZE, IconCache, ImageLoader, has_pillow, migrate_cache
from roco import applog
from roco.platform import autostart
from roco.core import history
from roco.platform import monitors
from roco.net import netdirect
from roco.core import purchases
from roco.platform import tray as tray_module
from roco.platform.memory import trim_working_set
from roco.platform.theme_listener import ThemeChangeListener
from roco.platform import win11_style
from roco.config import (Config, resolve_history_dir, resolve_icon_cache_dir,
                    resource_path)
from roco.core.freshness import CODE_ROUND_LAG, CODE_SHELF_SHRANK, format_delay
from roco.core.models import MerchantSnapshot
from roco.platform.notifier import Notifier
from roco.net.providers import fetch_snapshot
from roco.core.rounds import CN_TZ, is_open_at, next_refresh_at
from roco.core.rarity import RARITY_TABLE
from roco.services.scheduler import Scheduler, format_countdown, format_delta
from roco.ui.app_controller import AppController
from roco.ui.floating_window import FloatingWindow
from roco.ui.history_panel import HistoryPanel
from roco.ui.settings_dialog import SettingsDialog
from roco.ui import theme
from roco.ui.theme import mono_font, ui_font
from roco.ui.widgets import ItemCard, PillButton, ScrollFrame, StatBlock, StatusPill


class MerchantApp:
    # 换货后接口缓存可能还没跟上：依次等待这些秒数重试，合计约 10 分钟
    ROUND_RETRY_DELAYS = (30, 60, 120, 180, 240)
    # 重试链专用标记：带这个原因触发的 refresh 不清空重试计数
    RETRY_REASON = AppController.RETRY_REASON

    def __init__(self, root: tk.Tk, config: Config) -> None:
        self.root = root
        self.config = config

        # 主题先于一切控件：颜色在构造时读取（theme.py 的活调色板）
        theme.apply(config.theme_mode, config.theme_accent)

        self.history_dir, self._history_warning = resolve_history_dir(config)
        if self._history_warning:
            applog.warning("历史目录 | %s", self._history_warning)
        self.cache_path, self._cache_warning = resolve_icon_cache_dir(config)
        self.cache = IconCache(self.cache_path, config.icon_cache_enabled)
        self.loader = ImageLoader(root, self.cache, ICON_SIZE)
        notifier = Notifier(root, config)
        self.scheduler = Scheduler(
            root, config.query_times, on_fire=lambda: self.refresh(reason="定时任务"),
            on_tick=self._on_tick,
        )
        # 取数编排与数据状态（快照/加载中/重试计数/货架指纹）都在 controller；
        # round_delays 在构造时读取本类属性，selftest 可在构造前替换它
        self.controller = AppController(
            root, config, notifier, self.history_dir,
            round_delays=self.ROUND_RETRY_DELAYS, view=self,
        )
        self._pump_job: str | None = None
        self._show_requests: queue.Queue = queue.Queue()
        self._tray_commands: queue.Queue = queue.Queue()
        # 主题变更推送：监听线程把"系统改了明暗设置"投进队列，结果泵消费
        self._theme_requests: queue.Queue = queue.Queue()
        self._theme_listener = ThemeChangeListener(
            on_change=lambda: self._theme_requests.put(True))
        self._theme_listener.start()
        self.tray: tray_module.TrayIcon | None = None
        self._tray_hint_shown = False
        self._quitting = False

        self.floating: FloatingWindow | None = None
        self._cards: list[ItemCard] = []
        self._bought_cards: list[ItemCard] = []
        self._bought_grid: tk.Frame | None = None
        self._bought_count: tk.Label | None = None
        self._history_panel: HistoryPanel | None = None
        self._history_expanded = False
        self._columns = 0
        self._resize_job: str | None = None
        self._countdown_target: datetime | None = None

        self._setup_window()
        self._build_ui()
        applog.install_tk_hook(self.root, self._on_ui_error)
        self._start_tray()
        applog.info("主窗口就绪 | 显示器 %s 块 | 渠道 %s | 定时 %s",
                    len(monitors.enum_monitors()), config.provider,
                    ",".join(config.query_times))
        applog.info("直连状态 | 代理剥离=%s | DoH=%s | 大陆 IP 段=%s 条 | 环境代理变量=%s",
                    "开" if config.force_direct else "关",
                    "开" if config.doh_enabled else "关",
                    netdirect.cn_range_count(),
                    ", ".join(netdirect.proxy_env_snapshot()) or "无")
        self.scheduler.start()
        self._pump_results()
        # 启动稳定后修剪一次工作集：初始化产生的临时对象不再占据任务管理器里的"内存"
        self.root.after(2500, trim_working_set)
        self.root.after(220, lambda: self.refresh(reason="启动"))
        if config.floating_enabled:
            self.root.after(400, lambda: self.toggle_floating(True, quiet=True))

    # ------------------------------------------------------------- 主题切换
    def _on_system_theme_changed(self) -> None:
        """收到系统明暗变更推送（theme_listener 只在真实翻转时投递）。"""
        if self.config.theme_mode != "system":
            return
        if theme.apply(self.config.theme_mode, self.config.theme_accent):
            applog.info("跟随系统切换主题 -> %s", theme.resolve_mode("system"))
            self._rebuild_for_theme()

    def _rebuild_for_theme(self) -> None:
        """主题变化后重建全部控件。

        控件颜色在**构造时**从 theme 读取，改调色板必须换一棵控件树；
        数据状态都在 controller / config 上，这里只重建"皮"再按当前快照重绘。
        """
        was_floating = self.floating is not None
        if self.floating is not None:
            try:
                self.floating.save_geometry()
                self.floating.destroy()
            except tk.TclError:
                pass
            self.floating = None

        for child in self.root.winfo_children():
            try:
                child.destroy()
            except tk.TclError:
                pass

        self._cards.clear()
        self._bought_cards.clear()
        self._history_panel = None
        self._grid = None
        self._bought_grid = None

        self.root.configure(bg=theme.BG)
        # 主题切换后标题栏明暗要跟着换（圆角/双缓冲幂等，重复调用无害）
        win11_style.style_toplevel(
            self.root, dark=theme.resolve_mode(self.config.theme_mode) != "light", mica=True)
        self._build_ui()

        snapshot = self.controller.snapshot
        if snapshot is not None:
            self._render(snapshot)
        else:
            self._reset_content()
            self._show_placeholder("暂无数据", "点击「立即刷新」获取本轮货架。")
        self._update_status_right()
        self._update_cache_label()
        self._update_net_status()
        self._sync_tray_tooltip()

        if was_floating and self.config.floating_enabled:
            self.toggle_floating(True, quiet=True)

    # ------------------------------------------------- controller 状态转发
    # 这些属性被 selftest 与内部协作引用；数据本体在 controller 上，
    # 这里保持原名，读写都直达 controller，避免测试与调用点跟着重构。
    @property
    def snapshot(self) -> MerchantSnapshot | None:
        return self.controller.snapshot

    @property
    def pool(self):
        return self.controller.pool

    @property
    def notifier(self) -> Notifier:
        return self.controller.notifier

    @property
    def _loading(self) -> bool:
        return self.controller.loading

    @property
    def _round_retry(self) -> int:
        return self.controller.round_retry

    @_round_retry.setter
    def _round_retry(self, value: int) -> None:
        self.controller.round_retry = value

    @property
    def _watcher(self):
        return self.controller.watcher

    @property
    def _shelf_state_key(self):
        return self.controller.shelf_state_key

    @_shelf_state_key.setter
    def _shelf_state_key(self, value) -> None:
        self.controller.shelf_state_key = value

    @property
    def _results(self) -> queue.Queue:
        return self.controller.results

    @property
    def is_closing(self) -> bool:
        return self._quitting or getattr(self, "_closing", False)

    # -------------------------------------------------------------- 结果泵
    def _pump_results(self) -> None:
        """在主线程消化后台请求结果（Tk 不允许跨线程操作控件）。"""
        if getattr(self, "_closing", False):
            return

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
        while True:
            try:
                self._theme_requests.get_nowait()
            except queue.Empty:
                break
            self._on_system_theme_changed()
        if not getattr(self, "_closing", False):
            self._pump_job = self.root.after(80, self._pump_results)

    # ------------------------------------------------------------------ 窗口
    def _setup_window(self) -> None:
        self.root.title("远行商人助手 · 洛克王国：世界")
        self.root.configure(bg=theme.BG)
        self.root.minsize(1000, 660)
        self.root.geometry(self._safe_geometry())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        icon_file = resource_path("assets/app.ico")
        if icon_file.exists():
            try:
                self.root.iconbitmap(default=str(icon_file))
            except tk.TclError:
                pass
        # Win11 质感：双缓冲防撕裂 + 圆角/系统阴影 + 标题栏明暗 + Mica（尽力而为，
        # Win10 上静默跳过）；主题切换后 _rebuild_for_theme 会带新明暗再调一次
        win11_style.style_toplevel(
            self.root, dark=theme.resolve_mode(self.config.theme_mode) != "light", mica=True)
        try:
            icon = tk.PhotoImage(width=32, height=32)
            icon.put(theme.ACCENT, to=(4, 4, 28, 28))
            icon.put(theme.SURFACE, to=(10, 10, 22, 22))
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

        tk.Frame(self.root, bg=theme.BORDER, height=1).pack(fill="x")

        self._build_toolbar()

        self._scroll = ScrollFrame(self.root, bg=theme.BG)
        self._scroll.pack(fill="both", expand=True)
        self._scroll.canvas.bind("<Configure>", self._on_canvas_resize, add="+")

        self.body = self._scroll.body
        self._grid = tk.Frame(self.body, bg=theme.BG)
        self._grid.pack(fill="both", expand=True, padx=20, pady=(2, 22))
        self._build_statusbar()

    def _build_header(self) -> None:
        # 窗口最顶部的强调色细线：一条 2px 的品牌色带，主题切换时随重建换色
        tk.Frame(self.root, bg=theme.ACCENT, height=2).pack(fill="x")
        header = tk.Frame(self.root, bg=theme.BG)
        header.pack(fill="x", padx=24, pady=(18, 14))

        left = tk.Frame(header, bg=theme.BG)
        left.pack(side="left", anchor="w")
        title_row = tk.Frame(left, bg=theme.BG)
        title_row.pack(anchor="w")
        tk.Label(
            title_row, text="远行商人", bg=theme.BG, fg=theme.TEXT, font=ui_font(21, "bold"),
        ).pack(side="left")
        tk.Label(
            title_row, text="今日货架", bg=theme.BG, fg=theme.ACCENT, font=ui_font(21, "bold"),
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            left, text="《洛克王国：世界》· 每 4 小时一刷 · 08:00 / 12:00 / 16:00 / 20:00",
            bg=theme.BG, fg=theme.MUTED, font=ui_font(9),
        ).pack(anchor="w", pady=(4, 0))

        right = tk.Frame(header, bg=theme.BG)
        right.pack(side="right", anchor="e")

        self.stat_items = StatBlock(right, "在售商品", "—", ui_font(13, "bold"), bg=theme.BG)
        self.stat_items.grid(row=0, column=0, sticky="w", padx=(0, 26), pady=(7, 0))

        self.stat_round = StatBlock(right, "当前轮次", "—", ui_font(13, "bold"), bg=theme.BG)
        self.stat_round.grid(row=0, column=1, sticky="w", padx=(0, 22), pady=(7, 0))

        tk.Frame(right, bg=theme.BORDER, width=1, height=34).grid(
            row=0, column=2, sticky="ns", padx=(0, 22), pady=(7, 0)
        )

        countdown_box = tk.Frame(right, bg=theme.BG)
        countdown_box.grid(row=0, column=3, sticky="e", padx=(0, 18), pady=(0, 0))
        self.countdown_label = tk.Label(
            countdown_box, text="--:--:--", bg=theme.BG, fg=theme.TEXT, font=mono_font(20),
        )
        self.countdown_label.pack(anchor="e")
        self.countdown_hint = tk.Label(
            countdown_box, text="距离下一轮换货", bg=theme.BG, fg=theme.MUTED, font=ui_font(8),
        )
        self.countdown_hint.pack(anchor="e")

        self.pill = StatusPill(right, bg=theme.BG)
        self.pill.grid(row=0, column=4, sticky="e", pady=(6, 0))

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.root, bg=theme.BG)
        bar.pack(fill="x", padx=24, pady=(12, 10))

        self.refresh_btn = PillButton(bar, "立即刷新", self.refresh, style="primary", width=104)
        self.refresh_btn.pack(side="left")

        tk.Label(bar, text="数据渠道", bg=theme.BG, fg=theme.MUTED, font=ui_font(8)).pack(
            side="left", padx=(14, 6)
        )
        self.channel_btn = PillButton(
            bar, self._channel_text(), self._open_channel_menu, width=186, height=34
        )
        self.channel_btn.pack(side="left")

        PillButton(bar, "设置", self.open_settings, width=72).pack(side="left", padx=(10, 0))
        self.net_btn = PillButton(
            bar, self._net_text(), self.open_network_dialog, width=92, height=34
        )
        self.net_btn.pack(side="left", padx=(6, 0))

        self.float_btn = PillButton(
            bar, self._float_text(), lambda: self.toggle_floating(), width=104, height=34
        )
        self.float_btn.pack(side="left", padx=(10, 0))

        self.notify_btn = PillButton(
            bar, self._notify_text(), self._toggle_notify, width=96, height=34
        )
        self.notify_btn.pack(side="right")

        self.cache_label = tk.Label(
            bar, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(8), anchor="e",
        )
        self.cache_label.pack(side="right", padx=(0, 14))

    def _build_statusbar(self) -> None:
        tk.Frame(self.root, bg=theme.BORDER, height=1).pack(fill="x", side="bottom")
        bar = tk.Frame(self.root, bg=theme.SURFACE)
        bar.pack(fill="x", side="bottom")
        inner = tk.Frame(bar, bg=theme.SURFACE)
        inner.pack(fill="x", padx=24, pady=8)

        self.status_left = tk.Label(
            inner, text="准备就绪", bg=theme.SURFACE, fg=theme.MUTED, font=ui_font(8), anchor="w",
        )
        self.status_left.pack(side="left")
        self.status_right = tk.Label(
            inner, text="", bg=theme.SURFACE, fg=theme.MUTED, font=ui_font(8), anchor="e",
        )
        self.status_right.pack(side="right")
        self.net_label = tk.Label(
            inner, text="", bg=theme.SURFACE, fg=theme.MUTED, font=ui_font(8), anchor="e",
        )
        self.net_label.pack(side="right", padx=(0, 14))

    # ------------------------------------------------------------------ 文案
    def _net_text(self) -> str:
        """按钮上直接显示当前出口归属，省得每次都要开面板。"""
        route = (self.snapshot.route if self.snapshot else {}) or {}
        if not route:
            return "网络：待测"
        if route.get("is_cn"):
            return "网络：大陆直连"
        return "网络：境外出口"

    def open_network_dialog(self) -> None:
        from roco.ui.network_dialog import NetworkDialog

        NetworkDialog(self.root, self.config)

    def _update_net_status(self) -> None:
        """把直连状态写进状态栏：目标是否大陆、出口落在哪。"""
        route = (self.snapshot.route if self.snapshot else {}) or {}
        if not route:
            self.net_label.configure(text="")
            self.net_btn.set_text(self._net_text())
            return

        egress = route.get("egress") or {}
        country = str(egress.get("country") or "")
        host_ip = route.get("ip") or "?"
        if country and country != "CN":
            text = f"出口 {country}/{egress.get('colo', '?')} · 已被代理转发"
            color = theme.WARNING
        elif route.get("is_cn"):
            text = f"直连 {host_ip} · 中国大陆"
            color = theme.SUCCESS
        elif country == "CN":
            text = f"直连 · 出口 {egress.get('ip', '')}"
            color = theme.SUCCESS
        else:
            text = f"直连 {host_ip} · 境外"
            color = theme.MUTED
        self.net_label.configure(text=text, fg=color)
        self.net_btn.set_text(self._net_text())

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
                    loader=self.loader,          # 共用主窗口的图标缓存
                    # 悬浮窗里的「⟳」：直接复用主窗口的取数链路，不弹前台窗口
                    on_refresh=lambda: self.refresh(reason="悬浮窗获取"),
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
                fg=theme.MUTED,
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
        if not self.controller.begin_refresh(reason):
            return
        self.refresh_btn.set_enabled(False)
        self.refresh_btn.set_text("查询中…")
        self.pill.set_state("查询中", theme.WARNING)
        self.status_left.configure(text=f"{reason} · 正在请求数据…", fg=theme.MUTED)

        future = self.pool.submit(fetch_snapshot, self.config)
        future.add_done_callback(lambda f: self._results.put((f, reason)))

    def _on_fetched(self, future, reason: str) -> None:
        self.controller.handle_result(future, reason)

    def _fetch_reset_ui(self) -> None:
        """查询结束（无论成败）：复位刷新按钮（视图协议）。"""
        self.refresh_btn.set_enabled(True)
        self.refresh_btn.set_text("立即刷新")

    def _present_snapshot(self, snapshot: MerchantSnapshot, errors, reason: str) -> None:
        """渲染货架 + 状态栏来源信息（视图协议）。"""
        self._render(snapshot, animate=True)
        # 查询链路收尾：解码/渲染产生的临时对象可以交还系统了
        trim_working_set()
        if errors:
            self.status_left.configure(
                text=f"{reason} · 已降级到「{snapshot.source_label}」（原渠道：{errors[0][:60]}）",
                fg=theme.WARNING,
            )
        else:
            self.status_left.configure(
                text=f"{reason} · 数据来源：{snapshot.source_label} · "
                     f"{snapshot.fetched_at.strftime('%H:%M:%S')} 更新",
                fg=theme.MUTED,
            )

    def _end_floating_refresh(self) -> None:
        """查询结束：复位悬浮窗的「⟳」按钮状态（窗口可能已关闭，安全跳过）。"""
        floating = self.floating
        if floating is None:
            return
        try:
            floating.set_refreshing(False)
        except tk.TclError:
            self.floating = None

    def _show_recheck_pending(self, snapshot: MerchantSnapshot, reason: str, decision) -> None:
        """重取已排程：状态栏 + 药丸 + 货架标题三处同时说明原因。"""
        delay_text = format_delay(decision.delay)
        count = len(snapshot.items)
        if decision.code == CODE_ROUND_LAG:
            detail = f"接口还在第 {snapshot.round_index} 轮"
            note = (f"⚠ 接口仍是第 {snapshot.round_index} 轮"
                    f"（应为第 {snapshot.expected_round} 轮），正在重试")
        elif decision.code == CODE_SHELF_SHRANK:
            detail = f"本轮商品全部消失，只剩 {count} 件全天商品"
            note = f"⚠ 只剩 {count} 件全天商品，疑似接口还没换货，正在重试"
        else:
            detail = f"本轮仅 {count} 件全天商品、与上次完全相同"
            note = f"⚠ 货架只剩 {count} 件全天商品，疑似接口还没换货，正在重试"

        if decision.slow:
            # 放弃后仍选了"慢速续试"：文案要说清这是低频兜底，不是正常节奏
            rounds = f"{decision.slow_round}/{decision.slow_limit or '不限'}"
            self.status_left.configure(
                text=f"{reason} · {detail}，常规重试已用完，{delay_text}后再看一眼"
                     f"（慢速 {rounds}）",
                fg=theme.WARNING,
            )
            self.pill.set_state("慢速校验", theme.WARNING)
            self._set_shelf_note(note.replace("正在重试", "会定期再看"))
            return

        self.status_left.configure(
            text=f"{reason} · {detail}，{delay_text}后重试"
                 f"（{decision.attempt}/{decision.limit}）",
            fg=theme.WARNING,
        )
        self.pill.set_state("换货校验中", theme.WARNING)
        self._set_shelf_note(note)

    def _show_recheck_give_up(self, snapshot: MerchantSnapshot, reason: str,
                              decision, quiet: bool = False) -> None:
        """重取次数用完：保留接口给的数据，但明确告诉用户"可能不是新的"。

        `quiet=True` 用于"同一份内容这一轮已放弃过"的重复渲染：只更新界面文案，
        不再重复写日志、也不排新的重取。
        """
        if decision.code == CODE_ROUND_LAG:
            if not quiet:
                applog.warning(
                    "换货校验放弃 | 接口仍为第 %s 轮（本地应为第 %s 轮），已重试 %s 次；"
                    "界面展示的是接口给的数据，可稍后手动刷新",
                    snapshot.round_index, snapshot.expected_round, decision.limit)
            text = (f"{reason} · 接口尚未换货（仍为第 {snapshot.round_index} 轮），"
                    f"已重试 {decision.limit} 次，稍后可手动刷新")
            note = f"⚠ 接口仍是第 {snapshot.round_index} 轮，数据可能未更新"
        else:
            if not quiet:
                applog.warning(
                    "换货校验放弃 | 连续 %s 次只拿到 %s 件全天商品（%s）；"
                    "如仍看不到新商品，可稍后手动刷新或检查接口缓存",
                    decision.limit, len(snapshot.items), decision.code_text)
            text = (f"{reason} · 连续 {decision.limit} 次都只有 {len(snapshot.items)} 件"
                    f"全天商品，接口可能还没换货，稍后可手动刷新")
            note = f"⚠ 只有 {len(snapshot.items)} 件全天商品，数据可能未更新"
        self.status_left.configure(text=text, fg=theme.WARNING)
        self.pill.set_state("数据滞后", theme.WARNING)
        self._set_shelf_note(note)

    def _set_shelf_note(self, text: str, fg: str = theme.WARNING) -> None:
        """货架标题右侧的小字提示；货架还没建出来时安全跳过。"""
        label = getattr(self, "_shelf_note", None)
        try:
            if label is None or not label.winfo_exists():
                return
            label.configure(text=text, fg=fg)
        except tk.TclError:
            pass

    def _on_ui_error(self, name: str, message: str) -> None:
        """界面回调里抛出的异常：状态栏提示 + 首次弹窗，堆栈已由日志钩子记录。"""
        try:
            self.status_left.configure(text=f"界面出错：{name}: {message}（详见错误日志）", fg=theme.DANGER)
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
        from roco.ui.log_viewer import LogViewer

        LogViewer(self.root)

    def _on_fetch_failed(self, message: str, reason: str) -> None:
        # 日志与 error_text 已由 controller._handle_failure 记录，这里只管界面
        self.pill.set_state("查询失败", theme.DANGER)
        self.status_left.configure(text=f"{reason} · 失败：{message.splitlines()[0][:80]}", fg=theme.DANGER)
        if self.snapshot is None:
            self._render_error(message)
        self._update_cache_label()
        self._end_floating_refresh()

    # ------------------------------------------------------------------ 渲染
    def _reset_content(self) -> None:
        """清空内容区；货架容器按需重建（clear() 会连它一起销毁）。"""
        self._scroll.clear()
        self._cards.clear()
        self._bought_cards.clear()
        self._columns = 0
        self._grid = None
        self._bought_grid = None
        self._history_panel = None

    def _ensure_grid(self) -> tk.Frame:
        if self._grid is None or not self._grid.winfo_exists():
            head = tk.Frame(self.body, bg=theme.BG)
            head.pack(fill="x", padx=26, pady=(8, 2))
            tk.Label(head, text="本轮货架", bg=theme.BG, fg=theme.TEXT, font=ui_font(10, "bold")).pack(side="left")
            self._shelf_count = tk.Label(head, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(9))
            self._shelf_count.pack(side="left", padx=(8, 0), pady=(1, 0))
            self._shelf_note = tk.Label(head, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(8))
            self._shelf_note.pack(side="right", pady=(2, 0))

            self._grid = tk.Frame(self.body, bg=theme.BG)
            self._grid.pack(fill="both", expand=True, padx=20, pady=(4, 22))
        return self._grid

    def _render(self, snapshot: MerchantSnapshot, animate: bool = False) -> None:
        self._reset_content()

        # 图标预热：磁盘命中的现在同步解码进内存，未命中的并行下载——
        # 这样下面建卡片时，绝大多数图标已就位，不再出现"逐条弹入"的闪烁
        self.loader.prewarm([item.icon_url for item in snapshot.items])

        # 跨日防御：数据源在 00:00 之后仍可能挂着昨天 20 点那一轮，
        # 甚至把 status 继续报成 open。营业时间是不用请求就能确定的事实，
        # 所以这里再兜一层——打烊后的界面绝不出现昨日商品。
        live = is_open_at(datetime.now(CN_TZ))
        items = list(snapshot.items) if live else []
        bought = self._bought_names(snapshot) if live else set()
        pending = [item for item in items if item.name not in bought]
        purchased = [item for item in items if item.name in bought]

        opened = snapshot.is_open and bool(items)
        closed_now = snapshot.status == "closed" or not live
        if opened:
            self.pill.set_state("营业中", theme.SUCCESS)
        elif closed_now:
            self.pill.set_state("已打烊", theme.MUTED)
        else:
            self.pill.set_state("暂无商品", theme.WARNING)

        self.stat_round.set_value(f"第 {snapshot.round_index} 轮" if snapshot.round_index else "—")
        self.stat_items.set_value(f"{len(items)} 件")

        # 倒计时目标：营业中盯"下一轮换货"，打烊时盯"下次出摊"
        now_cn = datetime.now(CN_TZ)
        if closed_now:
            self._countdown_target = self._next_open(now_cn)
            self.countdown_hint.configure(text="距离商人出摊")
        else:
            self._countdown_target = self._parse_refresh(snapshot)
            self.countdown_hint.configure(text="距离下一轮换货")

        # 悬浮窗也要拿到"打烊后清空"的视图，否则它还会列出昨日商品
        self._sync_floating(snapshot if live else self._closed_view(snapshot))

        if not opened:
            if closed_now:
                open_at = self._next_open(now_cn)
                day_text = "今天" if open_at.date() == now_cn.date() else "明天"
                self._show_placeholder(
                    "商人已打烊",
                    "远行商人每天 08:00 开始营业，00:00 - 08:00 不出摊。\n"
                    f"下一次出现：{day_text} {open_at.strftime('%H:%M')}\n"
                    "打烊期间不显示上一轮的货架。",
                    accent=theme.MUTED,
                )
            else:
                self._show_placeholder(
                    "本轮暂无商品",
                    "接口没有返回本轮的货架数据，可能是商人正在换货，稍后刷新试试。",
                    accent=theme.WARNING,
                )
            self._render_history(snapshot)
            self._update_status_right()
            self._update_net_status()      # 打烊时也要显示出口，否则状态栏是空的
            self._sync_tray_tooltip()
            return

        grid = self._ensure_grid()
        level = self.config.notify_min_level
        special = sum(1 for item in items if item.rarity.level >= level)
        label = RARITY_TABLE.get(level, RARITY_TABLE[4]).label
        shelf_text = f"共 {len(items)} 件"
        if special:
            shelf_text += f" · {label}及以上 {special} 件"
        all_day = sum(1 for item in items if item.is_all_day)
        if all_day:
            shelf_text += f" · 其中 {all_day} 件全天在售"
        if purchased:
            shelf_text += f" · 已购 {len(purchased)} 件"
        self._shelf_count.configure(text=shelf_text)

        if snapshot.is_round_lagging:
            self._shelf_note.configure(
                text=f"⚠ 接口仍是第 {snapshot.round_index} 轮（应为第 {snapshot.expected_round} 轮），正在重试",
                fg=theme.WARNING,
            )
        else:
            note = (f"下一轮 {self._countdown_target.strftime('%H:%M')} 换货"
                    if self._countdown_target else "")
            self._shelf_note.configure(text=note, fg=theme.MUTED)

        for index, item in enumerate(pending):
            self._cards.append(
                ItemCard(grid, item, self.loader, index, on_toggle=self._toggle_purchased)
            )

        if purchased:
            bought_grid = self._ensure_bought_grid()
            if self._bought_count is not None:
                self._bought_count.configure(text=f"{len(purchased)} 件")
            for index, item in enumerate(purchased):
                self._bought_cards.append(ItemCard(
                    bought_grid, item, self.loader, index,
                    purchased=True, on_toggle=self._toggle_purchased,
                ))

        self._layout_cards(force=True)
        if animate:
            self._animate_entrance()
        self._render_history(snapshot)
        self._update_status_right()
        self._update_cache_label()
        self._update_net_status()
        self._sync_tray_tooltip()

    # ------------------------------------------------------- 分区 / 历史
    def _animate_entrance(self) -> None:
        """卡片瀑布式入场：先全部藏起，再按 12ms/张依次显现。

        Tk 做不了逐控件透明度，但这种"有节奏的显现"把一次生硬的
        整屏替换变成了平滑过渡。只在取数驱动的渲染时启用（默认关），
        勾选已购等重绘保持瞬时，避免每次点击都闪一遍。
        """
        cards = [c for c in (*self._cards, *self._bought_cards)
                 if c.winfo_exists()]
        if len(cards) < 2:
            return
        for card in cards:
            card.grid_remove()
        interval = max(10, min(24, 240 // len(cards)))
        for index, card in enumerate(cards):
            self.root.after(index * interval, card.grid)

    def _ensure_bought_grid(self) -> tk.Frame:
        """「已购买」独立区域：与在售区分开，避免已买的还占着最显眼的位置。"""
        if self._bought_grid is None or not self._bought_grid.winfo_exists():
            head = tk.Frame(self.body, bg=theme.BG)
            head.pack(fill="x", padx=26, pady=(10, 2))
            tk.Label(head, text="已购买", bg=theme.BG, fg=theme.TEXT,
                     font=ui_font(10, "bold")).pack(side="left")
            self._bought_count = tk.Label(head, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(9))
            self._bought_count.pack(side="left", padx=(8, 0), pady=(1, 0))
            tk.Label(head, text="点「已购」可撤销", bg=theme.BG, fg=theme.MUTED,
                     font=ui_font(8)).pack(side="right", pady=(2, 0))
            self._bought_grid = tk.Frame(self.body, bg=theme.BG)
            self._bought_grid.pack(fill="both", expand=True, padx=20, pady=(2, 8))
        return self._bought_grid

    def _closed_view(self, snapshot: MerchantSnapshot) -> MerchantSnapshot:
        """给悬浮窗用的"打烊视图"：清空货架，不修改原快照。"""
        return MerchantSnapshot(
            items=[], source_id=snapshot.source_id, source_label=snapshot.source_label,
            status="closed", round_index=snapshot.round_index,
            next_refresh=snapshot.next_refresh, fetched_at=snapshot.fetched_at,
            message="打烊中，货架已清空",
        )

    def _render_history(self, snapshot: MerchantSnapshot | None = None) -> None:
        if not self.config.history_enabled:
            # 用户关掉了历史：不读盘、不建面板，界面里就彻底没有这一块
            self._history_panel = None
            return
        rounds = history.for_display(self._history_file())
        key = self._round_key(snapshot) if snapshot is not None else ""
        panel = HistoryPanel(self.body, loader=self.loader,
                             expanded=self._history_expanded,
                             on_toggle=self._on_history_toggle)
        panel.pack(fill="x", pady=(4, 18))
        panel.set_rounds(rounds, current_key=key)
        self._history_panel = panel

    def _on_history_toggle(self, expanded: bool) -> None:
        """记住展开状态：每次查询都会重绘，不记住的话一展开就又被收起来。"""
        self._history_expanded = bool(expanded)

    def _round_key(self, snapshot: MerchantSnapshot) -> str:
        moment = snapshot.fetched_at
        if moment.tzinfo is None:          # 老快照/测试桩可能是 naive 时间
            moment = moment.replace(tzinfo=CN_TZ)
        return purchases.round_key(snapshot, moment.astimezone(CN_TZ))

    def _bought_names(self, snapshot: MerchantSnapshot) -> set[str]:
        try:
            return purchases.bought_names(self._round_key(snapshot))
        except Exception:      # noqa: BLE001 - 读盘失败不该让界面打不开
            return set()

    def _toggle_purchased(self, item) -> None:
        """勾选/取消已购：落盘后整体重绘，物品自动在两个区域之间移动。"""
        if self.snapshot is None:
            return
        key = self._round_key(self.snapshot)
        try:
            state = purchases.toggle(key, item.name)
        except Exception as exc:      # noqa: BLE001
            applog.warning("购买标记失败 | %s | %s", item.name, exc)
            return
        applog.info("购买标记 | %s | %s", item.name, "已购买" if state else "已取消")
        self._render(self.snapshot)

    def _history_file(self):
        return history.history_path(self.history_dir)

    def _maybe_notify(self, snapshot: MerchantSnapshot) -> None:
        """selftest 直接调用的入口：转发到 controller。"""
        self.controller.maybe_notify(snapshot)

    def _render_error(self, message: str) -> None:
        self._reset_content()
        self._show_placeholder(
            "查询失败",
            message.strip() + "\n\n可在「设置 → 数据渠道」里更换接口地址。",
            accent=theme.DANGER,
            action=("重试", lambda: self.refresh(reason="手动刷新")),
        )

    def _show_placeholder(self, title: str, message: str, accent: str = theme.MUTED,
                          action=None) -> None:
        holder = tk.Frame(self.body, bg=theme.BG)
        holder.pack(fill="both", expand=True, pady=(50, 0))
        tk.Label(holder, text="◍", bg=theme.BG, fg=accent, font=ui_font(30)).pack()
        tk.Label(holder, text=title, bg=theme.BG, fg=theme.TEXT, font=ui_font(15, "bold")).pack(pady=(8, 0))
        tk.Label(
            holder, text=message, bg=theme.BG, fg=theme.MUTED, font=ui_font(9),
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
        if not self._cards and not self._bought_cards:
            return
        width = self._scroll.canvas.winfo_width() or 1100
        columns = max(1, min(4, (width - 40) // 340))
        if not force and columns == self._columns:
            return
        self._columns = columns

        for cards, grid in ((self._cards, self._grid),
                            (self._bought_cards, self._bought_grid)):
            if grid is None or not cards:
                continue
            for column in range(5):
                grid.columnconfigure(column, weight=1 if column < columns else 0,
                                     uniform="cards" if column < columns else "")
            for index, card in enumerate(cards):
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
                self.countdown_label.configure(text="换货中", fg=theme.WARNING)
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

    # ------------------------------------------------------------------ 交互
    def _open_channel_menu(self) -> None:
        menu = tk.Menu(
            self.root, tearoff=0, bg=theme.SURFACE_2, fg=theme.TEXT, activebackground=theme.ACCENT,
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
        self.controller.update_config(new_config)
        # 主题变了就地换皮：调色板没变时 apply 返回 False，不折腾
        if theme.apply(new_config.theme_mode, new_config.theme_accent):
            self._rebuild_for_theme()
        applog.info("设置已保存 | 渠道=%s | 定时=%s | 通知=%s | 悬浮窗=%s | 自启=%s",
                    new_config.provider, ",".join(new_config.query_times),
                    new_config.notify_enabled, new_config.floating_enabled,
                    new_config.autostart)

        # 日志目录变了就换文件，后续日志写到新位置
        # 强制直连开关变化要立刻生效/撤销
        if new_config.force_direct:
            netdirect.install()
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

        # 历史目录 / 开关
        new_history_dir, history_warning = resolve_history_dir(new_config)
        if history_warning:
            messagebox.showwarning("历史记录", history_warning, parent=self.root)
        if new_history_dir != self.history_dir:
            moved = history.migrate(self.history_dir, new_history_dir)
            applog.info("历史目录已切换 -> %s（迁移旧记录=%s）", new_history_dir, moved)
            self.history_dir = new_history_dir
            self.controller.set_history_dir(new_history_dir)
        if not new_config.history_enabled:
            applog.info("历史记录已关闭（不再记录，也不展示历史面板）")

        # 开机自启：注册表与配置对齐
        if autostart.is_supported():
            ok, message = autostart.sync(new_config.autostart)
            if not ok:
                messagebox.showwarning("开机自启", message, parent=self.root)

        # 悬浮窗
        self.toggle_floating(new_config.floating_enabled, quiet=True)
        if self.floating is not None:
            # 缓存目录可能刚被换掉：先接上新加载器，再同步图标开关
            self.floating.attach_loader(self.loader)
            self.floating.set_show_icons(new_config.floating_show_icons)
        self.float_btn.set_text(self._float_text())

        self._update_status_right()
        # 换货校验的节奏可能刚被改过：立刻生效，关掉时顺手撤掉已排程的重取
        self._watcher.configure(
            delay=new_config.recheck_delay_seconds,
            max_attempts=new_config.recheck_max_attempts,
            round_delays=self.ROUND_RETRY_DELAYS,
            give_up_policy=new_config.recheck_give_up_policy,
            slow_interval=new_config.recheck_slow_interval_seconds,
            slow_max_attempts=new_config.recheck_slow_max_attempts,
        )
        if not new_config.auto_recheck_after_refresh:
            self.controller.cancel_retry()
            self._round_retry = 0
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
        # 进托盘 = 后台常驻：把工作集还给系统，任务管理器里的占用显著下降
        self.root.after(600, trim_working_set)
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
        self.controller.cancel_retry()
        self._theme_listener.stop()
        if self._pump_job is not None:
            try:
                self.root.after_cancel(self._pump_job)
            except (tk.TclError, ValueError):
                pass
            self._pump_job = None
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
