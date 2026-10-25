"""配色、字体与通用绘制工具。

1.9 起支持多主题：浅色 / 深色 / 纯黑（OLED）/ 跟随系统，外加自定义强调色。
关键设计：颜色常量（BG、SURFACE…）是**活值**——`apply()` 会重绑定本模块的
全局变量，消费方必须用 `theme.BG` 属性访问（构造时取值），**不许**
`from theme import BG`（那样拿到的是导入瞬间的旧值，切主题后不会变）。
控件颜色在构造时读取，所以切主题后需要重建控件树（见 app_window._rebuild_for_theme）。
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

# ---------------- 调色板定义 ----------------
# 每套调色板必须含全部键名；新增颜色键时三套都要补齐
PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "BG": "#0f1319",          # 窗口底色
        "SURFACE": "#161c26",     # 卡片
        "SURFACE_2": "#1d2532",   # 卡内块 / 输入框
        "SURFACE_3": "#232c3b",   # 悬停
        "BORDER": "#263041",
        "BORDER_HOVER": "#3a4a63",
        "TEXT": "#e8eef7",
        "TEXT_DIM": "#aeb9c9",
        "MUTED": "#7d8a9e",
        "ACCENT": "#4f8cff",
        "SUCCESS": "#3ddc97",
        "MONEY": "#f5c66b",       # 价格用的暖金色
        "DANGER": "#ff6b6b",
        "WARNING": "#ffb454",
    },
    "light": {
        "BG": "#f2f4f8",
        "SURFACE": "#ffffff",
        "SURFACE_2": "#eaeef4",
        "SURFACE_3": "#dde4ee",
        "BORDER": "#d3dae4",
        "BORDER_HOVER": "#a9b8cd",
        "TEXT": "#1b2330",
        "TEXT_DIM": "#46516b",
        "MUTED": "#8391a8",
        "ACCENT": "#2e6ae0",
        "SUCCESS": "#149d68",
        "MONEY": "#a8730a",
        "DANGER": "#d64545",
        "WARNING": "#b06f00",
    },
    "oled": {
        "BG": "#000000",          # OLED 纯黑：熄屏与黑边融为一体
        "SURFACE": "#0b0d10",
        "SURFACE_2": "#13161b",
        "SURFACE_3": "#1c2027",
        "BORDER": "#20242c",
        "BORDER_HOVER": "#39414f",
        "TEXT": "#e8eef7",
        "TEXT_DIM": "#aeb9c9",
        "MUTED": "#7d8a9e",
        "ACCENT": "#4f8cff",
        "SUCCESS": "#3ddc97",
        "MONEY": "#f5c66b",
        "DANGER": "#ff6b6b",
        "WARNING": "#ffb454",
    },
}

# 主题模式下拉里展示的顺序与文案
THEME_MODES = ("dark", "light", "oled", "system")
THEME_MODE_LABELS = {
    "dark": "深色",
    "light": "浅色",
    "oled": "纯黑（OLED）",
    "system": "跟随系统",
}

# 珍惜度配色（与 rarity.py 保持一致，各主题通用）
RARITY_COLORS = {
    1: "#8b98ab",
    2: "#3ddc97",
    3: "#4f9bff",
    4: "#c97bff",
    5: "#ff8a3d",
}

# ---------------- 活调色板（消费方用 theme.X 访问） ----------------
BG = PALETTES["dark"]["BG"]
SURFACE = PALETTES["dark"]["SURFACE"]
SURFACE_2 = PALETTES["dark"]["SURFACE_2"]
SURFACE_3 = PALETTES["dark"]["SURFACE_3"]
BORDER = PALETTES["dark"]["BORDER"]
BORDER_HOVER = PALETTES["dark"]["BORDER_HOVER"]
TEXT = PALETTES["dark"]["TEXT"]
TEXT_DIM = PALETTES["dark"]["TEXT_DIM"]
MUTED = PALETTES["dark"]["MUTED"]
ACCENT = PALETTES["dark"]["ACCENT"]
SUCCESS = PALETTES["dark"]["SUCCESS"]
MONEY = PALETTES["dark"]["MONEY"]
DANGER = PALETTES["dark"]["DANGER"]
WARNING = PALETTES["dark"]["WARNING"]

# 当前生效状态：用于判断"要不要重建界面"
_active_mode = "dark"
_active_accent = ""


def _accent_dim(accent: str) -> str:
    """强调色的暗部（旧调色板里的 ACCENT_DIM），由强调色推导。"""
    return mix(accent, "#000000", 0.42)


def system_prefers_light() -> bool:
    """读 Windows「应用模式」注册表；读不到（非 Windows / 被策略拦）按深色。"""
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows 存在，延迟导入

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _type = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return bool(value)
    except Exception:  # noqa: BLE001 - 注册表不可读时不影响界面
        return False


def resolve_mode(mode: str) -> str:
    """把配置里的主题模式解析成实际调色板名（system → 注册表）。"""
    if mode == "system":
        return "light" if system_prefers_light() else "dark"
    return mode if mode in PALETTES else "dark"


def apply(mode: str = "dark", accent: str = "") -> bool:
    """应用主题：重绑定本模块的颜色全局变量。

    返回 True 表示生效的调色板/强调色发生了变化（调用方需要重建控件树）。
    accent 传空串 = 用调色板默认强调色。
    """
    global BG, SURFACE, SURFACE_2, SURFACE_3, BORDER, BORDER_HOVER
    global TEXT, TEXT_DIM, MUTED, ACCENT
    global _active_mode, _active_accent

    resolved = resolve_mode(mode)
    accent = normalize_accent(accent) or PALETTES[resolved]["ACCENT"]
    if resolved == _active_mode and accent == _active_accent:
        return False

    palette = dict(PALETTES[resolved])
    palette["ACCENT"] = accent
    BG = palette["BG"]
    SURFACE = palette["SURFACE"]
    SURFACE_2 = palette["SURFACE_2"]
    SURFACE_3 = palette["SURFACE_3"]
    BORDER = palette["BORDER"]
    BORDER_HOVER = palette["BORDER_HOVER"]
    TEXT = palette["TEXT"]
    TEXT_DIM = palette["TEXT_DIM"]
    MUTED = palette["MUTED"]
    ACCENT = palette["ACCENT"]
    _active_mode = resolved
    _active_accent = accent
    return True


def current_signature(mode: str, accent: str) -> tuple[str, str]:
    """当前配置会解析成什么（不真正应用），供"变了才重建"的判断。"""
    return resolve_mode(mode), normalize_accent(accent) or PALETTES[resolve_mode(mode)]["ACCENT"]


def is_dark() -> bool:
    """当前生效的调色板是不是暗系（浅色=False，深色/纯黑=True）。"""
    return _active_mode != "light"


def normalize_accent(accent: str) -> str:
    """强调色清洗：接受 #rgb / #rrggbb，其余返回空串（表示用默认）。"""
    value = (accent or "").strip()
    if not value:
        return ""
    if not value.startswith("#"):
        value = "#" + value
    body = value[1:]
    if len(body) == 3 and all(ch in "0123456789abcdefABCDEF" for ch in body):
        return "#" + "".join(ch + ch for ch in body).lower()
    if len(body) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in body):
        return value.lower()
    return ""


# ---------------- 字体 ----------------
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


# ---------------- 绘制工具 ----------------
def round_rect_points(x1: float, y1: float, x2: float, y2: float,
                      radius: float, step: int = 10) -> list[float]:
    """圆角矩形的顶点序列（平滑多边形用）。

    单独拆出来是为了让"同一形状反复变位置"的动画（滚动条拇指）能用
    `canvas.coords(item, *points)` 原地更新，而不是 delete("all") 后重建——
    重建会先擦成底色，高刷屏上每一帧都能看见那一下闪。
    """
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points: list[float] = []
    corners = (
        (x2 - radius, y1 + radius, -90),
        (x2 - radius, y2 - radius, 0),
        (x1 + radius, y2 - radius, 90),
        (x1 + radius, y1 + radius, 180),
    )
    for cx, cy, start in corners:
        for offset in range(0, 91, step):
            angle = math.radians(start + offset)
            points.extend((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
               radius: float, **kwargs) -> int:
    """在 Canvas 上画圆角矩形（用平滑多边形逼近，边缘干净）。"""
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    if radius <= 0:
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)
    return canvas.create_polygon(round_rect_points(x1, y1, x2, y2, radius),
                                 smooth=True, **kwargs)


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
