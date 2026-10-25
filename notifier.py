"""通知推送：Windows 原生通知优先，应用内浮窗兜底。

层级
----
1. **Windows 原生通知**（`notifier_native`）：走 WinRT Toast，落在系统通知中心，
   带「查看货架 / 忽略」按钮，点按钮可回到程序。
2. **应用内浮窗**：仅当原生通知不可用或发送失败时兜底，保证不会静默丢提醒。
3. 叠加效果：系统提示音（winsound）、任务栏闪烁（FlashWindowEx）。
4. 可选外部推送：Webhook（Bark / 飞书 / 企业微信机器人）。

原生通知的发送放在后台线程，避免解释器冷启动拖慢主线程。
"""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk
import urllib.request
from typing import Iterable

import notifier_native
from config import Config
from models import MerchantItem

# 与 ui/theme.py 保持一致（独立模块，避免循环导入）
_BG = "#161c26"
_BORDER = "#2b3547"
_TEXT = "#e8eef7"
_MUTED = "#8b98ab"
_ACCENT = "#ff8a3d"


class ToastWindow:
    """右下角浮窗提醒。"""

    WIDTH = 384
    MARGIN = 24

    def __init__(self, root: tk.Misc, title: str, body: str, accent: str = _ACCENT) -> None:
        self.root = root

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        shell = tk.Frame(self.win, bg=_BORDER)
        shell.pack(fill="both", expand=True)
        card = tk.Frame(shell, bg=_BG)
        card.pack(fill="both", expand=True, padx=1, pady=1)

        tk.Frame(card, bg=accent, width=4).pack(side="left", fill="y")

        content = tk.Frame(card, bg=_BG)
        content.pack(side="left", fill="both", expand=True, padx=14, pady=12)

        head = tk.Frame(content, bg=_BG)
        head.pack(fill="x")
        tk.Label(head, text="●", bg=_BG, fg=accent, font=("Microsoft YaHei UI", 10)).pack(side="left")
        tk.Label(
            head, text=title, bg=_BG, fg=_TEXT,
            font=("Microsoft YaHei UI", 11, "bold"), anchor="w",
        ).pack(side="left", padx=(6, 0))

        tk.Label(
            content, text=body, bg=_BG, fg=_MUTED,
            font=("Microsoft YaHei UI", 9), justify="left", anchor="w", wraplength=self.WIDTH - 64,
        ).pack(fill="x", pady=(8, 0))

        close = tk.Label(content, text="✕", bg=_BG, fg=_MUTED, font=("Microsoft YaHei UI", 9), cursor="hand2")
        close.place(relx=1.0, rely=0.0, anchor="ne")

        for widget in (self.win, card, content, head, close):
            widget.bind("<Button-1>", lambda _e: self.close())

        # 先量内容高度，再定位，避免出现空底边
        self.win.update_idletasks()
        height = max(96, self.win.winfo_reqheight())
        x, y = self._corner_position(root, height)
        self.win.geometry(f"{self.WIDTH}x{height}+{x}+{y}")

        self._closed = False
        self._fade_in()
        self.win.after(5600, self._fade_out)

    @classmethod
    def _corner_position(cls, root: tk.Misc, height: int) -> tuple[int, int]:
        """右下角位置：跟随主窗口所在的那块屏，而不是永远主显示器。"""
        try:
            import monitors

            screens = monitors.enum_monitors()
            target = None
            if root.winfo_viewable():
                target = monitors.best_monitor(
                    screens, root.winfo_rootx(), root.winfo_rooty(),
                    max(1, root.winfo_width()), max(1, root.winfo_height()),
                )
            target = target or monitors.primary_monitor(screens)
            x = target.work_right - cls.WIDTH - cls.MARGIN
            y = target.work_bottom - height - cls.MARGIN
            return max(target.work_x, x), max(target.work_y, y)
        except Exception:
            return (root.winfo_screenwidth() - cls.WIDTH - cls.MARGIN,
                    root.winfo_screenheight() - height - cls.MARGIN - 56)

    def _fade_in(self, alpha: float = 0.0) -> None:
        if self._closed:
            return
        alpha = min(0.97, alpha + 0.16)
        try:
            self.win.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if alpha < 0.97:
            self.win.after(16, lambda: self._fade_in(alpha))

    def _fade_out(self, alpha: float = 0.97) -> None:
        if self._closed:
            return
        alpha -= 0.14
        if alpha <= 0:
            self.close()
            return
        try:
            self.win.attributes("-alpha", alpha)
        except tk.TclError:
            return
        self.win.after(16, lambda: self._fade_out(alpha))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass


def play_sound() -> None:
    if os.name != "nt":
        return
    try:
        import winsound

        winsound.MessageBeep(0x00000040)  # MB_ICONASTERISK
    except Exception:
        pass


def flash_taskbar(root: tk.Misc, times: int = 4) -> None:
    """让任务栏图标闪烁，适合窗口在后台时提醒。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, times, 0)  # ALL | TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def post_webhook(url: str, payload: dict, timeout: int = 8) -> None:
    """后台推送，失败静默（不影响主流程）。"""

    def work() -> None:
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=timeout).read()
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()


def select_targets(snapshot, min_level: int, watch: Iterable[str] = ()) -> list[MerchantItem]:
    """选出该提醒的物品：**品质达标 ∪ 点名关注**，按名称去重并保持原顺序。

    两条规则是并集而非替代关系——保留原来的粗粒度筛选，同时允许
    "就要某一个普通品质的活动道具"这种精确诉求。
    """
    names = {str(n).strip() for n in watch if str(n).strip()}
    picked: list[MerchantItem] = []
    seen: set[str] = set()
    for item in snapshot.items:
        if item.name in seen:
            continue
        if item.rarity.level >= min_level or item.name in names:
            seen.add(item.name)
            picked.append(item)
    return picked


class Notifier:
    def __init__(self, root: tk.Misc, config: Config) -> None:
        self.root = root
        self.config = config
        self._toast: ToastWindow | None = None
        self._native_ok = notifier_native.is_supported()
        self._send_lock = threading.Lock()

    def update_config(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------
    def native_available(self) -> bool:
        return self._native_ok

    def native_status(self) -> tuple[bool, str]:
        """(可用, 说明) —— 给设置面板展示。"""
        if self._native_ok:
            return True, "系统通知中心"
        return False, notifier_native.last_error() or "当前系统不支持"

    def alert_special(self, items: Iterable[MerchantItem]) -> None:
        items = list(items)
        if not items:
            return
        config = self.config
        title, body = self._compose(items, config.notify_items)

        native_sent = False
        if config.notify_native and self._native_ok:
            native_sent = self._send_native(title, body, silent=not config.notify_sound)

        # 浮窗只在原生没发出去时兜底：原生成功就不再重复提示，
        # 否则老配置（notify_toast=True）会同时弹两遍
        if not native_sent and config.notify_toast:
            self._show_toast(title, body)

        if config.notify_sound and not native_sent:
            play_sound()  # 原生通知自带系统音，避免响两次
        if config.notify_flash:
            flash_taskbar(self.root)
        if config.webhook_url.strip():
            self._post_webhook(items, title)

    # ------------------------------------------------------------------
    @staticmethod
    def _compose(items: list[MerchantItem], watched: Iterable[str] = ()) -> tuple[str, str]:
        watched_names = {str(n).strip() for n in watched if str(n).strip()}
        if len(items) == 1:
            first = items[0]
            # 点名关注的商品可能只是"普通"品质，标题直接说"关注物品"更清楚
            prefix = "关注物品" if first.name in watched_names else f"{first.rarity.label}物品"
            title = f"{prefix}：{first.name}"
            body = f"{first.price_text}（{first.price_raw_text or '价格未知'}）· {first.limit_text}"
            if first.category:
                body += f" · {first.category}"
            return title, body

        names = "、".join(item.name for item in items[:3])
        if len(items) > 3:
            names += f" 等 {len(items)} 件"
        tail = "关注物品" if any(i.name in watched_names for i in items) else "稀有物品"
        title = f"远行商人上架了 {len(items)} 件{tail}"
        prices = " · ".join(item.price_text for item in items[:3])
        return title, f"{names}\n{prices}"

    def _send_native(self, title: str, body: str, silent: bool = False) -> bool:
        """后台线程发送，主线程立即返回。"""
        if not self._send_lock.acquire(blocking=False):
            return False
        try:
            threading.Thread(
                target=self._send_native_worker, args=(title, body, silent), daemon=True
            ).start()
            return True
        except Exception:
            self._send_lock.release()
            return False

    def _send_native_worker(self, title: str, body: str, silent: bool) -> None:
        try:
            notifier_native.send(title, body, silent=silent, timeout=15)
        finally:
            self._send_lock.release()

    def _post_webhook(self, items: list[MerchantItem], title: str) -> None:
        post_webhook(
            self.config.webhook_url.strip(),
            {
                "title": title,
                "content": "\n".join(
                    f"{i.name} · {i.rarity.label} · {i.price_text}"
                    f"（{i.price_raw_text}）· {i.limit_text}"
                    for i in items
                ),
                "items": [
                    {
                        "name": i.name,
                        "price": i.price,
                        "price_chinese": i.price_text,
                        "rarity": i.rarity.label,
                        "limit": i.limit,
                        "category": i.category,
                    }
                    for i in items
                ],
            },
        )

    def send_test(self) -> tuple[bool, str]:
        """设置里的「测试通知」。"""
        title = "通知测试"
        body = "出现稀有物品时，就会像这样提醒你。"
        if self.config.notify_native and self._native_ok:
            if notifier_native.send(title, body):
                return True, "已发送，请查看系统通知中心"
        if self.config.notify_toast:
            self._show_toast(title, body)
            return True, "原生通知不可用，已用应用内浮窗提示"
        return False, notifier_native.last_error() or "当前没有可用的通知方式"

    def _show_toast(self, title: str, body: str) -> None:
        if self._toast is not None:
            self._toast.close()
        try:
            self._toast = ToastWindow(self.root, title, body)
        except tk.TclError:
            self._toast = None
