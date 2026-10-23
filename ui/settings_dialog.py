"""设置对话框：定时时间、接口地址、通知策略、缓存管理。"""
from __future__ import annotations

import copy
import tkinter as tk
from tkinter import filedialog
from typing import Callable, Optional

import applog
import autostart
import notifier_native
from cache import IconCache
from config import QUERY_TIME_PRESETS, Config, resolve_icon_cache_dir, sanitize_times
from rarity import RARITY_TABLE
from ui import theme
from ui.theme import (ACCENT, BG, BORDER, BORDER_HOVER, DANGER, MUTED, SURFACE,
                      SURFACE_2, TEXT, TEXT_DIM, WARNING, mix, round_rect, ui_font)
from ui.widgets import PillButton, ScrollFrame


def _entry(master: tk.Misc, textvariable: tk.StringVar, width: int = 40, show: str = "") -> tk.Entry:
    return tk.Entry(
        master, textvariable=textvariable, width=width, show=show,
        bg=SURFACE_2, fg=TEXT, insertbackground=ACCENT, relief="flat",
        highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        font=ui_font(10), bd=0,
    )


def _section(master: tk.Misc, title: str, subtitle: str = "") -> tk.Frame:
    """带竖条标题的分区容器。"""
    holder = tk.Frame(master, bg=SURFACE)
    holder.pack(fill="x", pady=(0, 14))
    head = tk.Frame(holder, bg=SURFACE)
    head.pack(fill="x", pady=(0, 10))
    tk.Frame(head, bg=ACCENT, width=3, height=14).pack(side="left", pady=(2, 0))
    tk.Label(head, text=title, bg=SURFACE, fg=TEXT, font=ui_font(11, "bold")).pack(
        side="left", padx=(8, 0)
    )
    if subtitle:
        tk.Label(head, text=subtitle, bg=SURFACE, fg=MUTED, font=ui_font(8)).pack(
            side="left", padx=(8, 0), pady=(3, 0)
        )
    return holder


class SettingsDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, config: Config, cache: IconCache,
                 on_saved: Optional[Callable[[Config], None]] = None) -> None:
        super().__init__(master, bg=BG)
        self.title("设置 · 远行商人助手")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.result: Optional[Config] = None
        self._on_saved = on_saved
        self._cache = cache
        self._draft = copy.deepcopy(config)
        self._current_pid = self._draft.provider

        width, height = 672, 872
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

        self._build()
        self.grab_set()
        self.bind("<Escape>", lambda _e: self._cancel())

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=24, pady=(16, 10))
        tk.Label(header, text="设置", bg=BG, fg=TEXT, font=ui_font(16, "bold")).pack(side="left")
        tk.Label(
            header, text="修改后点击「保存并应用」立即生效", bg=BG, fg=MUTED, font=ui_font(9),
        ).pack(side="left", padx=(10, 0), pady=(4, 0))

        self._scroll = ScrollFrame(self, bg=BG)
        self._scroll.pack(fill="both", expand=True, padx=24)
        body = self._scroll.body

        self._build_schedule(body)
        self._build_provider(body)
        self._build_network(body)
        self._build_notify(body)
        self._build_floating(body)
        self._build_cache(body)
        self._build_logging(body)
        self._build_startup(body)

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=24, pady=12)
        self._error = tk.Label(footer, text="", bg=BG, fg=DANGER, font=ui_font(9), anchor="w")
        self._error.pack(side="left")
        PillButton(footer, "保存并应用", self._save, style="primary", width=118).pack(side="right")
        PillButton(footer, "取消", self._cancel, width=82).pack(side="right", padx=(0, 10))

    # -------------------------------------------------------------- 定时查询
    def _build_schedule(self, master: tk.Misc) -> None:
        holder = _section(master, "定时查询", "24 小时制，多个时间用逗号分隔")

        row = tk.Frame(holder, bg=SURFACE)
        row.pack(fill="x")
        self._times_var = tk.StringVar(value=", ".join(self._draft.query_times))
        entry = _entry(row, self._times_var, width=44)
        entry.pack(side="left", ipady=6, padx=(0, 10))

        presets = tk.Frame(holder, bg=SURFACE)
        presets.pack(fill="x", pady=(8, 0))
        tk.Label(presets, text="快捷预设", bg=SURFACE, fg=MUTED, font=ui_font(8)).pack(
            side="left", padx=(0, 8)
        )
        for name, times in QUERY_TIME_PRESETS.items():
            PillButton(
                presets, name, lambda t=times: self._times_var.set(", ".join(t)),
                style="subtle", width=len(name) * 12 + 26, height=28, font_size=8,
            ).pack(side="left", padx=(0, 6))

        self._recheck_var = tk.BooleanVar(value=self._draft.auto_recheck_after_refresh)
        tk.Checkbutton(
            holder, text="换货后自动校验（接口还没刷新时持续重试）", variable=self._recheck_var,
            bg=SURFACE, fg=TEXT_DIM, activebackground=SURFACE, activeforeground=TEXT,
            selectcolor=SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
        ).pack(anchor="w", pady=(10, 0))

        tk.Label(
            holder,
            text="提示：商人在 08:00 / 12:00 / 16:00 / 20:00 换货，默认取整点后 5 分钟查询。"
                 "接口有缓存，刚换货时可能仍返回上一轮的货架——开启上面的选项后，"
                 "程序会按 30/60/120/180/240 秒重试，拿到新一轮数据才停下。",
            bg=SURFACE, fg=MUTED, font=ui_font(8), justify="left", wraplength=580,
        ).pack(fill="x", pady=(6, 0))

    # -------------------------------------------------------------- 数据渠道
    def _build_provider(self, master: tk.Misc) -> None:
        holder = _section(master, "数据渠道", "官方接口优先，失败可自动降级")

        self._provider_choice = tk.StringVar(value=self._current_pid)
        for pid in self._draft.provider_order():
            conf = self._draft.provider_conf(pid)
            row = tk.Frame(holder, bg=SURFACE)
            row.pack(fill="x")
            tk.Radiobutton(
                row, text=conf.get("label", pid), value=pid, variable=self._provider_choice,
                bg=SURFACE, fg=TEXT, activebackground=SURFACE, activeforeground=TEXT,
                selectcolor=SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
                command=lambda p=pid: self._select_provider(p),
            ).pack(side="left")
            ready = bool(str(conf.get("base_url") or "").strip())
            if pid == "shallow":
                ready = ready and bool(str(conf.get("api_key") or "").strip())
            if conf.get("official"):
                ready = bool(str(conf.get("base_url") or "").strip())
            tk.Label(
                row, text="已配置" if ready else "未配置", bg=SURFACE,
                fg=theme.SUCCESS if ready else MUTED, font=ui_font(8),
            ).pack(side="right", padx=(0, 10))

        self._provider_note = tk.Label(
            holder, text="", bg=SURFACE, fg=MUTED, font=ui_font(8),
            justify="left", anchor="w", wraplength=580,
        )
        self._provider_note.pack(fill="x", pady=(8, 10))

        grid = tk.Frame(holder, bg=SURFACE)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="接口地址", bg=SURFACE, fg=TEXT_DIM, font=ui_font(9)).grid(
            row=0, column=0, sticky="w", pady=4
        )
        self._url_var = tk.StringVar()
        _entry(grid, self._url_var, width=52).grid(row=0, column=1, sticky="ew", ipady=5, padx=(10, 0))

        tk.Label(grid, text="API Key", bg=SURFACE, fg=TEXT_DIM, font=ui_font(9)).grid(
            row=1, column=0, sticky="w", pady=4
        )
        self._key_var = tk.StringVar()
        _entry(grid, self._key_var, width=52).grid(row=1, column=1, sticky="ew", ipady=5, padx=(10, 0))

        self._fallback_var = tk.BooleanVar(value=self._draft.auto_fallback)
        tk.Checkbutton(
            holder, text="当前渠道失败时自动尝试其它渠道", variable=self._fallback_var,
            bg=SURFACE, fg=TEXT_DIM, activebackground=SURFACE, activeforeground=TEXT,
            selectcolor=SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
        ).pack(anchor="w", pady=(10, 0))

        self._select_provider(self._current_pid)

    def _select_provider(self, pid: str) -> None:
        # 先把上一家的输入落盘到草稿，再切换
        if self._current_pid and self._current_pid != pid:
            self._store_provider_fields(self._current_pid)
        self._current_pid = pid
        self._provider_choice.set(pid)
        conf = self._draft.provider_conf(pid)
        self._url_var.set(str(conf.get("base_url") or ""))
        self._key_var.set(str(conf.get("api_key") or ""))
        note = str(conf.get("note") or "")
        if conf.get("official"):
            note = "★ " + note if note else "★ 官方接口（如已开放请填写地址）"
        self._provider_note.configure(text=note)

    def _store_provider_fields(self, pid: str) -> None:
        conf = self._draft.providers.setdefault(pid, {})
        conf["base_url"] = self._url_var.get().strip()
        conf["api_key"] = self._key_var.get().strip()

    # -------------------------------------------------------------- 网络直连
    def _build_network(self, master: tk.Misc) -> None:
        holder = _section(master, "网络直连", "绕开 shellclash 等代理软件的劫持")

        options = tk.Frame(holder, bg=SURFACE)
        options.pack(fill="x")
        self._direct_var = tk.BooleanVar(value=self._draft.force_direct)
        self._doh_var = tk.BooleanVar(value=self._draft.doh_enabled)
        self._cn_var = tk.BooleanVar(value=self._draft.prefer_cn_region)
        self._egress_var = tk.BooleanVar(value=self._draft.verify_egress)
        for text, var in (
            ("强制直连（剥离代理）", self._direct_var),
            ("DoH 取真实 IP", self._doh_var),
            ("优先大陆渠道", self._cn_var),
            ("校验出口", self._egress_var),
        ):
            tk.Checkbutton(
                options, text=text, variable=var,
                bg=SURFACE, fg=TEXT_DIM, activebackground=SURFACE, activeforeground=TEXT,
                selectcolor=SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
            ).pack(side="left", padx=(0, 14))

        tk.Label(
            holder,
            text="强制直连：清空 http_proxy 等环境变量并禁用 system 代理，"
                 "全进程不再走任何 HTTP 代理；DoH：绕开 Clash 的 fake-ip 劫持，取得真实 IP 后按 IP 直连，"
                 "TLS 证书仍按域名校验。",
            bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="w", justify="left",
            wraplength=580,
        ).pack(fill="x", pady=(8, 0))

        tk.Label(
            holder,
            text="注意：路由器端的透明代理（TProxy / REDIRECT）在报文层改写 TCP，"
                 "客户端无法单方面绕过。目标在中国大陆时 Clash 会按 GEOIP 放行直连；"
                 "目标在境外则必然被转发到节点——此时请用「大陆反代」渠道或加下面的直连规则。",
            bg=SURFACE, fg=theme.WARNING, font=ui_font(8), anchor="w", justify="left",
            wraplength=580,
        ).pack(fill="x", pady=(6, 0))

        actions = tk.Frame(holder, bg=SURFACE)
        actions.pack(fill="x", pady=(10, 0))
        PillButton(actions, "网络诊断", self._open_network_dialog, width=96, height=28,
                   font_size=9).pack(side="left")
        PillButton(actions, "复制直连规则", self._copy_direct_rules, style="subtle",
                   width=112, height=28, font_size=9).pack(side="left", padx=(8, 0))
        self._net_hint = tk.Label(
            actions, text="", bg=SURFACE, fg=theme.SUCCESS, font=ui_font(8), anchor="e",
        )
        self._net_hint.pack(side="right")

    def _open_network_dialog(self) -> None:
        from ui.network_dialog import NetworkDialog

        NetworkDialog(self, self._draft)

    def _copy_direct_rules(self) -> None:
        import netdirect

        rules = netdirect.clash_rules(netdirect.default_hosts(self._draft.providers))
        self.clipboard_clear()
        self.clipboard_append(rules)
        self._net_hint.configure(text="已复制到剪贴板")
        self.after(2600, lambda: self._net_hint.configure(text=""))

    # -------------------------------------------------------------- 通知
    def _build_notify(self, master: tk.Misc) -> None:
        holder = _section(master, "通知提醒", "出现特殊物品时提醒")

        options = tk.Frame(holder, bg=SURFACE)
        options.pack(fill="x")
        self._notify_var = tk.BooleanVar(value=self._draft.notify_enabled)
        self._native_var = tk.BooleanVar(value=self._draft.notify_native)
        self._toast_var = tk.BooleanVar(value=self._draft.notify_toast)
        self._sound_var = tk.BooleanVar(value=self._draft.notify_sound)
        self._flash_var = tk.BooleanVar(value=self._draft.notify_flash)
        for text, var in (
            ("启用通知", self._notify_var),
            ("Windows 系统通知", self._native_var),
            ("应用内浮窗（兜底）", self._toast_var),
            ("系统提示音", self._sound_var),
            ("任务栏闪烁", self._flash_var),
        ):
            tk.Checkbutton(
                options, text=text, variable=var,
                bg=SURFACE, fg=TEXT_DIM, activebackground=SURFACE, activeforeground=TEXT,
                selectcolor=SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
            ).pack(side="left", padx=(0, 14))

        state_row = tk.Frame(holder, bg=SURFACE)
        state_row.pack(fill="x", pady=(8, 0))
        available, detail = notifier_native.is_supported(), "系统通知中心 · 支持点按回跳"
        if not available:
            detail = notifier_native.last_error() or "当前系统不支持原生通知"
        tk.Label(
            state_row, text=f"● {detail}", bg=SURFACE,
            fg=theme.SUCCESS if available else theme.WARNING, font=ui_font(8),
        ).pack(side="left")
        PillButton(
            state_row, "发送测试通知", self._test_notify, style="subtle", width=104, height=26,
            font_size=8,
        ).pack(side="right")

        level_row = tk.Frame(holder, bg=SURFACE)
        level_row.pack(fill="x", pady=(10, 0))
        tk.Label(level_row, text="提醒等级", bg=SURFACE, fg=TEXT_DIM, font=ui_font(9)).pack(
            side="left", padx=(0, 10)
        )
        self._level_var = tk.IntVar(value=self._draft.notify_min_level)
        for level in (3, 4, 5):
            rarity = RARITY_TABLE[level]
            tk.Radiobutton(
                level_row, text=f"{rarity.label}及以上" if level < 5 else "仅传说",
                value=level, variable=self._level_var,
                bg=SURFACE, fg=rarity.color, activebackground=SURFACE,
                activeforeground=rarity.color, selectcolor=SURFACE_2,
                font=ui_font(9), bd=0, highlightthickness=0,
            ).pack(side="left", padx=(0, 12))

        hook_row = tk.Frame(holder, bg=SURFACE)
        hook_row.pack(fill="x", pady=(12, 0))
        tk.Label(hook_row, text="外部推送", bg=SURFACE, fg=TEXT_DIM, font=ui_font(9)).pack(
            side="left", padx=(0, 10)
        )
        self._webhook_var = tk.StringVar(value=self._draft.webhook_url)
        _entry(hook_row, self._webhook_var, width=44).pack(side="left", fill="x", expand=True, ipady=5)
        hint_row = tk.Frame(holder, bg=SURFACE)
        hint_row.pack(fill="x", pady=(6, 0))
        tk.Label(
            hint_row, text="可选：填入 Bark / 飞书 / 企业微信机器人等 Webhook 地址，将以 JSON POST 推送。",
            bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="w", justify="left",
        ).pack(side="left")
        self._notify_hint = tk.Label(
            hint_row, text="", bg=SURFACE, fg=theme.SUCCESS, font=ui_font(8), anchor="e",
        )
        self._notify_hint.pack(side="right")

    # -------------------------------------------------------------- 悬浮窗
    def _build_floating(self, master: tk.Misc) -> None:
        holder = _section(master, "悬浮窗", "常驻前台，随手看一眼")

        options = tk.Frame(holder, bg=SURFACE)
        options.pack(fill="x")
        self._float_var = tk.BooleanVar(value=self._draft.floating_enabled)
        self._float_top_var = tk.BooleanVar(value=self._draft.floating_topmost)
        self._float_icons_var = tk.BooleanVar(value=self._draft.floating_show_icons)
        for text, var in (("启用悬浮窗", self._float_var),
                          ("总在最前", self._float_top_var),
                          ("显示物品图标", self._float_icons_var)):
            tk.Checkbutton(
                options, text=text, variable=var,
                bg=SURFACE, fg=TEXT_DIM, activebackground=SURFACE, activeforeground=TEXT,
                selectcolor=SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
            ).pack(side="left", padx=(0, 14))

        tk.Label(
            holder,
            text="拖动窗顶可移动位置；「–」收起成横条；位置会记住。"
                 "关闭「显示物品图标」后行内只保留珍惜度圆点与名称价格，更紧凑。",
            bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="w", justify="left",
            wraplength=580,
        ).pack(fill="x", pady=(6, 0))

    # -------------------------------------------------------------- 缓存
    def _build_cache(self, master: tk.Misc) -> None:
        holder = _section(master, "图标缓存", "位置可自定义，减少重复请求")

        path_row = tk.Frame(holder, bg=SURFACE)
        path_row.pack(fill="x")
        tk.Label(path_row, text="缓存目录", bg=SURFACE, fg=TEXT_DIM, font=ui_font(9)).pack(
            side="left", padx=(0, 10)
        )
        self._cache_path_var = tk.StringVar(value=self._draft.icon_cache_path)
        _entry(path_row, self._cache_path_var, width=38).pack(
            side="left", fill="x", expand=True, ipady=5
        )
        PillButton(path_row, "浏览…", self._browse_cache, style="subtle", width=68, height=28,
                   font_size=8).pack(side="left", padx=(8, 0))

        action_row = tk.Frame(holder, bg=SURFACE)
        action_row.pack(fill="x", pady=(8, 0))
        tk.Label(
            action_row, text=f"当前：{self._cache.folder}", bg=SURFACE, fg=MUTED,
            font=ui_font(8), anchor="w",
        ).pack(side="left")
        PillButton(action_row, "恢复默认", self._reset_cache_path, style="subtle",
                   width=80, height=26, font_size=8).pack(side="right")

        info_row = tk.Frame(holder, bg=SURFACE)
        info_row.pack(fill="x", pady=(8, 0))
        self._cache_info = tk.Label(
            info_row, text=self._cache_summary(), bg=SURFACE, fg=TEXT_DIM,
            font=ui_font(9), anchor="w",
        )
        self._cache_info.pack(side="left")
        self._cache_tip = tk.Label(info_row, text="", bg=SURFACE, fg=theme.SUCCESS, font=ui_font(8))
        self._cache_tip.pack(side="left", padx=(10, 0))
        PillButton(info_row, "清空缓存", self._clear_cache, style="subtle", width=92, height=26,
                   font_size=8).pack(side="right")

        tk.Label(
            holder,
            text="留空 = 默认位置。切换目录时会把已有图标一起搬过去，不必重新下载。",
            bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="w",
        ).pack(fill="x", pady=(6, 0))

    def _cache_summary(self) -> str:
        return f"已缓存 {self._cache.count()} 张，占用 {self._cache.size_mb():.2f} MB"

    def _browse_cache(self) -> None:
        folder = filedialog.askdirectory(
            parent=self, title="选择图标缓存目录",
            initialdir=self._cache_path_var.get() or str(self._cache.folder),
        )
        if folder:
            self._cache_path_var.set(folder)
            self._cache_tip.configure(text="保存后生效", fg=theme.WARNING)

    def _reset_cache_path(self) -> None:
        self._cache_path_var.set("")
        self._cache_tip.configure(text="将使用默认位置", fg=theme.MUTED)

    def _clear_cache(self) -> None:
        removed = self._cache.clear()
        self._cache_info.configure(text=self._cache_summary())
        self._cache_tip.configure(text=f"已清理 {removed} 个文件", fg=theme.SUCCESS)

    # -------------------------------------------------------------- 日志
    def _build_logging(self, master: tk.Misc) -> None:
        holder = _section(master, "错误日志", "排查问题用")

        path_row = tk.Frame(holder, bg=SURFACE)
        path_row.pack(fill="x")
        tk.Label(path_row, text="日志目录", bg=SURFACE, fg=TEXT_DIM, font=ui_font(9)).pack(
            side="left", padx=(0, 10)
        )
        self._log_dir_var = tk.StringVar(value=self._draft.log_dir)
        _entry(path_row, self._log_dir_var, width=38).pack(
            side="left", fill="x", expand=True, ipady=5
        )
        PillButton(path_row, "浏览…", self._browse_log_dir, style="subtle", width=68,
                   height=28, font_size=8).pack(side="left", padx=(8, 0))

        info_row = tk.Frame(holder, bg=SURFACE)
        info_row.pack(fill="x", pady=(8, 0))
        log_file = applog.log_file()
        size = log_file.stat().st_size / 1024 if log_file.exists() else 0
        tk.Label(
            info_row, text=f"当前：{log_file}（{size:.1f} KB）", bg=SURFACE, fg=MUTED,
            font=ui_font(8), anchor="w",
        ).pack(side="left")
        PillButton(info_row, "恢复默认", self._reset_log_dir, style="subtle", width=80,
                   height=26, font_size=8).pack(side="right")

        action_row = tk.Frame(holder, bg=SURFACE)
        action_row.pack(fill="x", pady=(8, 0))
        PillButton(action_row, "查看错误日志", self._open_log_viewer, width=110, height=28,
                   font_size=9).pack(side="left")
        PillButton(action_row, "打开日志文件夹", self._open_log_folder, style="subtle",
                   width=116, height=28, font_size=9).pack(side="left", padx=(8, 0))

        tk.Label(
            holder,
            text="程序运行异常、托盘初始化失败、界面回调报错都会写入该文件（单份 1 MB，滚动保留 3 份）。",
            bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="w",
        ).pack(fill="x", pady=(6, 0))

    def _browse_log_dir(self) -> None:
        folder = filedialog.askdirectory(
            parent=self, title="选择日志目录",
            initialdir=self._log_dir_var.get() or str(applog.log_file().parent),
        )
        if folder:
            self._log_dir_var.set(folder)

    def _reset_log_dir(self) -> None:
        self._log_dir_var.set("")

    def _open_log_viewer(self) -> None:
        from ui.log_viewer import LogViewer

        LogViewer(self)

    def _open_log_folder(self) -> None:
        import os

        try:
            folder = applog.log_file().parent
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606
        except Exception as exc:
            self._error.configure(text=f"打开日志目录失败：{exc}", fg=theme.DANGER)

    # -------------------------------------------------------------- 启动
    def _build_startup(self, master: tk.Misc) -> None:
        holder = _section(master, "启动与运行", "随系统启动")

        options = tk.Frame(holder, bg=SURFACE)
        options.pack(fill="x")
        self._autostart_var = tk.BooleanVar(value=self._draft.autostart)
        tk.Checkbutton(
            options, text="开机自动启动", variable=self._autostart_var,
            bg=SURFACE, fg=TEXT_DIM, activebackground=SURFACE, activeforeground=TEXT,
            selectcolor=SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
        ).pack(side="left")

        self._close_tray_var = tk.BooleanVar(value=self._draft.close_to_tray)
        tk.Checkbutton(
            options, text="关闭窗口时最小化到托盘", variable=self._close_tray_var,
            bg=SURFACE, fg=TEXT_DIM, activebackground=SURFACE, activeforeground=TEXT,
            selectcolor=SURFACE_2, font=ui_font(9), bd=0, highlightthickness=0,
        ).pack(side="left", padx=(18, 0))

        tk.Label(
            holder,
            text="取消勾选后，点 × 会直接退出进程；勾选时请用托盘右键菜单「退出」结束程序。",
            bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="w",
        ).pack(fill="x", pady=(6, 0))

        note = "写入当前用户注册表，无需管理员权限"
        if not autostart.is_supported():
            note = "当前系统不支持（仅 Windows）"
        elif autostart.is_enabled():
            note = "注册表中已登记，取消勾选即可关闭"
        tk.Label(
            holder, text=note, bg=SURFACE, fg=MUTED, font=ui_font(8), anchor="w",
        ).pack(fill="x", pady=(6, 0))

    def _test_notify(self) -> None:
        ok = notifier_native.send("通知测试", "出现稀有物品时，就会像这样提醒你。")
        self._notify_hint.configure(
            text="已发送，请查看系统通知中心" if ok else f"发送失败：{notifier_native.last_error()}",
            fg=theme.SUCCESS if ok else theme.DANGER,
        )

    # -------------------------------------------------------------- 保存
    def _save(self) -> None:
        times = sanitize_times([t for t in self._times_var.get().replace("，", ",").split(",")])
        if not times:
            self._error.configure(text="时间格式不对，示例：08:05, 12:05, 16:05, 20:05")
            return

        self._store_provider_fields(self._current_pid)

        draft = self._draft
        draft.query_times = times
        draft.provider = self._current_pid
        draft.auto_fallback = bool(self._fallback_var.get())
        draft.auto_recheck_after_refresh = bool(self._recheck_var.get())
        draft.force_direct = bool(self._direct_var.get())
        draft.doh_enabled = bool(self._doh_var.get())
        draft.prefer_cn_region = bool(self._cn_var.get())
        draft.verify_egress = bool(self._egress_var.get())
        draft.notify_enabled = bool(self._notify_var.get())
        draft.notify_native = bool(self._native_var.get())
        draft.notify_toast = bool(self._toast_var.get())
        draft.notify_sound = bool(self._sound_var.get())
        draft.notify_flash = bool(self._flash_var.get())
        draft.notify_min_level = int(self._level_var.get())
        draft.webhook_url = self._webhook_var.get().strip()
        draft.floating_enabled = bool(self._float_var.get())
        draft.floating_topmost = bool(self._float_top_var.get())
        draft.floating_show_icons = bool(self._float_icons_var.get())
        draft.autostart = bool(self._autostart_var.get()) if autostart.is_supported() else False
        draft.close_to_tray = bool(self._close_tray_var.get())
        draft.log_dir = self._log_dir_var.get().strip()

        # 缓存目录：顺手校验一次，无效就提示但不阻断保存
        draft.icon_cache_path = self._cache_path_var.get().strip()
        _resolved, cache_warning = resolve_icon_cache_dir(draft)
        if cache_warning:
            self._error.configure(text=cache_warning, fg=theme.WARNING)

        # 允许把"自动"渠道存成空地址（表示未配置）
        for pid, conf in draft.providers.items():
            if isinstance(conf, dict):
                conf["base_url"] = str(conf.get("base_url") or "").strip()
                conf["api_key"] = str(conf.get("api_key") or "").strip()

        draft.save()
        self.result = draft
        if self._on_saved:
            self._on_saved(draft)
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.grab_release()
        self.destroy()
