"""网络与出口诊断面板。

把"流量有没有被代理接管"从猜变成可测：逐渠道显示系统 DNS、DoH 真实 IP、
目标是否在大陆、以及服务器侧看到的真实出口国家。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from typing import Callable, Optional

from roco.net import netdirect
from roco.config import Config
from roco.ui import theme
from roco.platform import win11_style
from roco.ui.theme import mono_font, ui_font
from roco.ui.widgets import PillButton


class NetworkDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, config: Config,
                 on_apply: Optional[Callable[[], None]] = None) -> None:
        super().__init__(master)
        self.config_data = config
        self._on_apply = on_apply
        self._results: queue.Queue = queue.Queue()
        self._running = False

        self.title("网络与出口诊断")
        self.configure(bg=theme.BG)
        self.transient(master)
        self.resizable(False, False)
        self.withdraw()

        width, height = 700, 620
        self.update_idletasks()
        px = master.winfo_rootx() + (master.winfo_width() - width) // 2
        py = master.winfo_rooty() + max(20, (master.winfo_height() - height) // 3)
        self.geometry(f"{width}x{height}+{max(0, px)}+{max(0, py)}")

        self._build(width, height)
        # 先隐藏再建控件，建完才显示：消除 Toplevel 默认白底的一闪
        self.update_idletasks()
        self.deiconify()
        win11_style.style_toplevel(self, dark=theme.is_dark())
        self.grab_set()
        self.after(60, self.run_check)

    # ------------------------------------------------------------------ 布局
    def _build(self, width: int, height: int) -> None:
        header = tk.Frame(self, bg=theme.BG)
        header.pack(fill="x", padx=24, pady=(18, 8))
        tk.Label(header, text="网络与出口诊断", bg=theme.BG, fg=theme.TEXT,
                 font=ui_font(13, "bold")).pack(anchor="w")
        tk.Label(
            header,
            text="检查代理是否接管流量、DNS 是否被 fake-ip 劫持、真实出口落在哪个国家。",
            bg=theme.BG, fg=theme.MUTED, font=ui_font(8), anchor="w",
        ).pack(anchor="w", pady=(4, 0))

        self._status = tk.Label(header, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(8), anchor="w")
        self._status.pack(anchor="w", pady=(8, 0))

        body = tk.Frame(self, bg=theme.SURFACE, highlightbackground=theme.BORDER, highlightthickness=1)
        body.pack(fill="both", expand=True, padx=24, pady=(4, 0))

        self.text = tk.Text(
            body, bg=theme.SURFACE_2, fg=theme.TEXT_DIM, font=mono_font(9), bd=0,
            highlightthickness=0, padx=14, pady=12, wrap="word", height=20,
        )
        self.text.pack(fill="both", expand=True, padx=1, pady=1)
        self.text.tag_configure("head", foreground=theme.TEXT, font=ui_font(9, "bold"))
        self.text.tag_configure("ok", foreground=theme.SUCCESS)
        self.text.tag_configure("warn", foreground=theme.WARNING)
        self.text.tag_configure("bad", foreground=theme.DANGER)
        self.text.tag_configure("dim", foreground=theme.MUTED)
        self.text.configure(state="disabled")

        footer = tk.Frame(self, bg=theme.BG)
        footer.pack(fill="x", padx=24, pady=14)
        self._check_btn = PillButton(footer, "重新检测", self.run_check, width=96, height=32)
        self._check_btn.pack(side="left")
        PillButton(footer, "复制直连规则", self._copy_rules, style="subtle",
                   width=112, height=32).pack(side="left", padx=(8, 0))
        PillButton(footer, "关闭", self.destroy, style="subtle", width=76,
                   height=32).pack(side="right")

        self._hint = tk.Label(
            footer, text="", bg=theme.BG, fg=theme.MUTED, font=ui_font(8), anchor="e",
        )
        self._hint.pack(side="right", padx=(0, 12))

    # ------------------------------------------------------------------ 检测
    def run_check(self) -> None:
        if self._running:
            return
        self._running = True
        self._status.configure(text="正在检测…", fg=theme.MUTED)
        self._check_btn.set_enabled(False)
        self._write([("dim", "正在解析并测试各渠道…\n")])

        hosts = netdirect.default_hosts(self.config_data.providers)
        threading.Thread(target=self._worker, args=(hosts,), daemon=True).start()
        self.after(120, self._drain)

    def _worker(self, hosts: list[str]) -> None:
        lines: list[tuple[str, str]] = []
        proxy_env = netdirect.proxy_env_snapshot()

        lines.append(("head", "代理状态\n"))
        if proxy_env:
            lines.append(("warn", f"  发现代理环境变量（已剥离）：{', '.join(proxy_env)}\n"))
        else:
            lines.append(("ok", "  未发现代理环境变量\n"))
        import urllib.request

        alive = urllib.request.getproxies()
        if alive:
            lines.append(("bad", f"  系统代理仍然生效：{alive}\n"))
        else:
            lines.append(("ok", "  系统代理已被禁用（getproxies 返回空）\n"))
        lines.append(("dim", f"  真实 IP 库：{'已加载' if netdirect.is_cn_ip('223.5.5.5') else '未加载'}\n"))
        lines.append(("dim", "  DoH：DNSPod / AliDNS（中国大陆，引导 IP 已内置）\n\n"))

        if not hosts:
            lines.append(("warn", "所有渠道都没有填写接口地址，无从检测。\n"))
            self._results.put(("done", lines))
            return

        for host in hosts:
            lines.append(("head", f"目标：{host}\n"))
            try:
                report = netdirect.diagnose(
                    host, use_doh=self.config_data.doh_enabled
                )
            except Exception as exc:  # noqa: BLE001
                lines.append(("bad", f"  检测失败：{type(exc).__name__} {exc}\n\n"))
                continue

            system_ips = report["system_ips"] or ["(无)"]
            if report["fake_ip_hijacked"]:
                lines.append(("warn", f"  系统 DNS   ：{', '.join(system_ips)}  ← fake-ip，已被劫持\n"))
            else:
                lines.append(("dim", f"  系统 DNS   ：{', '.join(system_ips)}\n"))

            if report["doh_ips"]:
                lines.append(("ok", f"  DoH 解析   ：{', '.join(report['doh_ips'])}\n"))
            else:
                lines.append(("bad", f"  DoH 解析   ：失败 {report['doh_error']}\n"))

            zone = "中国大陆" if report["target_is_cn"] else "境外"
            tag = "ok" if report["target_is_cn"] else "warn"
            lines.append((tag, f"  建连目标   ：{report['target_ip']}（{zone}）\n"))

            egress = report["egress"]
            if egress.get("ok"):
                country = egress.get("country", "?")
                tag = "ok" if country == "CN" else "bad"
                lines.append((tag,
                    f"  真实出口   ：{egress.get('ip')}  {country} / {egress.get('colo', '?')}"
                    f"  {egress.get('latency_ms', 0)}ms\n"))
            else:
                lines.append(("dim", f"  真实出口   ：未取到（{egress.get('error', '该站点无回显')}）\n"))

            verdict_tag = "bad" if "接管" in report["verdict"] else "ok"
            lines.append((verdict_tag, f"  结论       ：{report['verdict']}\n"))
            for advice in report["advice"]:
                lines.append(("dim", f"  建议       ：{advice}\n"))
            lines.append(("dim", "\n"))

        self._results.put(("done", lines))

    def _drain(self) -> None:
        try:
            _kind, lines = self._results.get_nowait()
        except queue.Empty:
            self.after(120, self._drain)
            return
        self._write(lines)
        self._running = False
        self._check_btn.set_enabled(True)
        blocked = any(tag == "bad" for tag, _ in lines)
        if blocked:
            self._status.configure(text="检测完成：流量正在被代理接管，见下方建议", fg=theme.DANGER)
        else:
            self._status.configure(text="检测完成：未发现代理接管", fg=theme.SUCCESS)

    def _write(self, lines: list[tuple[str, str]]) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for tag, chunk in lines:
            self.text.insert("end", chunk, tag)
        self.text.configure(state="disabled")

    # ------------------------------------------------------------------ 规则
    def _copy_rules(self) -> None:
        hosts = netdirect.default_hosts(self.config_data.providers)
        rules = netdirect.clash_rules(hosts)
        self.clipboard_clear()
        self.clipboard_append(rules)
        self._hint.configure(text="分流规则已复制到剪贴板", fg=theme.SUCCESS)
        self._clear_hint_later()

    def _clear_hint_later(self, delay: int = 2600) -> None:
        """过一会儿清掉提示；面板先关掉时静默跳过，不落进 Tk 错误钩子。"""

        def clear() -> None:
            try:
                if self._hint.winfo_exists():
                    self._hint.configure(text="")
            except tk.TclError:
                pass

        try:
            self.after(delay, clear)
        except tk.TclError:
            pass
