"""配色、字体与通用绘制工具（深色主题）。"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

# ---------------- 调色板 ----------------
BG = "#0f1319"          # 窗口底色
SURFACE = "#161c26"     # 卡片
SURFACE_2 = "#1d2532"   # 卡内块 / 输入框
SURFACE_3 = "#232c3b"   # 悬停
BORDER = "#263041"
BORDER_HOVER = "#3a4a63"
TEXT = "#e8eef7"
TEXT_DIM = "#aeb9c9"
MUTED = "#7d8a9e"
ACCENT = "#4f8cff"
ACCENT_DIM = "#2f5aa8"
SUCCESS = "#3ddc97"
MONEY = "#f5c66b"        # 价格用的暖金色
DANGER = "#ff6b6b"
WARNING = "#ffb454"

# 珍惜度配色（与 rarity.py 保持一致）
RARITY_COLORS = {
    1: "#8b98ab",
    2: "#3ddc97",
    3: "#4f9bff",
    4: "#c97bff",
    5: "#ff8a3d",
}

_FAMILY_CACHE: str | None = None


def ui_family(root: tk.Misc | None = None) -> str:
    """挑一个系统里存在的中文无衬线字体。"""
    global _FAMILY_CACHE
    if _FAMILY_CACHE:
        return _FAMILY_CACHE
    preferred = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans SC", "Segoe UI")
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        available = set()
    for name in preferred:
        if name in available:
            _FAMILY_CACHE = name
            return name
    _FAMILY_CACHE = "TkDefaultFont"
    return _FAMILY_CACHE


def ui_font(size: int = 10, weight: str = "normal") -> tuple:
    return (ui_family(), size, weight)


def mono_font(size: int = 22, weight: str = "bold") -> tuple:
    for name in ("Consolas", "Cascadia Mono", "Segoe UI Mono"):
        try:
            if name in set(tkfont.families()):
                return (name, size, weight)
        except tk.TclError:
            break
    return (ui_family(), size, weight)


def round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
               radius: float, **kwargs) -> int:
    """在 Canvas 上画圆角矩形（用平滑多边形逼近，边缘干净）。"""
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    if radius <= 0:
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)
    points: list[float] = []
    corners = (
        (x2 - radius, y1 + radius, -90),
        (x2 - radius, y2 - radius, 0),
        (x1 + radius, y2 - radius, 90),
        (x1 + radius, y1 + radius, 180),
    )
    for cx, cy, start in corners:
        for step in range(0, 91, 10):
            angle = math.radians(start + step)
            points.extend((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return canvas.create_polygon(points, smooth=True, **kwargs)


def lighten(color: str, amount: float = 0.12) -> str:
    """把 #rrggbb 往白色方向提亮。"""
    try:
        value = color.lstrip("#")
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return color
    r = min(255, int(r + (255 - r) * amount))
    g = min(255, int(g + (255 - g) * amount))
    b = min(255, int(b + (255 - b) * amount))
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(color_a: str, color_b: str, ratio: float = 0.5) -> str:
    """按比例混合两色，用于把珍惜度色压成低调的底色。"""
    try:
        a = color_a.lstrip("#")
        b = color_b.lstrip("#")
        ra, ga, ba = (int(a[i:i + 2], 16) for i in (0, 2, 4))
        rb, gb, bb = (int(b[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return color_a
    ratio = max(0.0, min(1.0, ratio))
    r = int(ra * (1 - ratio) + rb * ratio)
    g = int(ga * (1 - ratio) + gb * ratio)
    bl = int(ba * (1 - ratio) + bb * ratio)
    return f"#{r:02x}{g:02x}{bl:02x}"
