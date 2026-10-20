r"""配置管理：所有可替换项都在这里，落盘为 JSON，UI 可改。

配置文件位置（首次启动自动生成）：
    Windows: %APPDATA%\RocoMerchant\config.json
    macOS/Linux: ~/.roco_merchant/config.json

===== API 地址如何替换 =====
方式一（推荐）：程序右上角「设置」→「数据渠道」→ 修改「接口地址」→ 保存。
方式二：直接编辑上面的 config.json，改 providers.<渠道id>.base_url。
方式三：新增渠道 —— 在 providers 里加一条，然后「数据渠道」里选「自定义接口」并填入同样的地址。
"""
from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_NAME = "RocoMerchant"
APP_TITLE = "远行商人助手"
APP_VERSION = "1.2.0"

# ---------------------------------------------------------------------------
# 各数据渠道的默认定义。base_url 全部可被用户在界面里覆盖。
# ---------------------------------------------------------------------------
DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "official": {
        "label": "官方公开 API",
        "base_url": "",  # 官方目前未开放该接口，留空即自动跳过
        "api_key": "",
        "note": "腾讯/魔方官方目前没有公开远行商人 API。若日后上线，把地址填在此处，"
                "程序会优先走官方接口；留空则自动跳过。",
        "official": True,
    },
    "rkw": {
        "label": "社区镜像 · 洛克 Wiki",
        "base_url": "https://rocokingdomworld.org/api/merchant",
        "api_key": "",
        "note": "免费、免 Key，字段最全（价格 / 限购 / 图标 / 轮次 / 下次刷新），默认渠道。",
        "official": False,
    },
    "yxsr": {
        "label": "社区精简 · 资源卡",
        "base_url": "https://yxsr.vercel.app/api/merchant",
        "api_key": "",
        "note": "免费免 Key，但只返回商品名，不含价格与图标，作为极简兜底。",
        "official": False,
    },
    "shallow": {
        "label": "洛克魔法书（需自备 Key）",
        "base_url": "https://wegame.shallow.ink/api/v1/games/rocom/merchant/info",
        "api_key": "",
        "note": "第三方付费服务，需在请求头带 Bearer Key；未填 Key 时自动跳过。",
        "official": False,
    },
    "custom": {
        "label": "自定义接口",
        "base_url": "",
        "api_key": "",
        "note": "任何返回 JSON 的接口都可以接。程序会自动识别 items / goods_list / data.list "
                "等常见结构，并按 name / price / limit / image / category 等字段名做映射。",
        "official": False,
    },
}

QUERY_TIME_PRESETS: dict[str, list[str]] = {
    "默认（刷新后 5 分钟）": ["08:05", "12:05", "16:05", "20:05"],
    "整点刷新时": ["08:00", "12:00", "16:00", "20:00"],
    "每 2 小时": ["08:05", "10:05", "12:05", "14:05", "16:05", "18:05", "20:05", "22:05"],
}


def resource_path(relative: str) -> Path:
    """定位随程序分发的资源：源码运行取脚本目录，打包运行取解包目录。"""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent
    return root / relative


def app_dir() -> Path:
    """配置与缓存根目录。"""
    base = os.environ.get("APPDATA")
    path = Path(base) / APP_NAME if (base and os.name == "nt") else Path.home() / ".roco_merchant"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_dir() / "config.json"


def state_path() -> Path:
    return app_dir() / "state.json"


def default_cache_dir() -> Path:
    """默认图标缓存目录。"""
    path = app_dir() / "cache" / "icons"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """兼容旧调用：默认缓存目录。"""
    return default_cache_dir()


def resolve_icon_cache_dir(config: "Config") -> tuple[Path, str]:
    """按配置解析图标缓存目录。

    返回 (目录, 提示信息)。自定义目录不可用（不存在/无权限/只读）时
    自动回退到默认目录，并给出可展示的提示，绝不因配置错误而崩溃。
    """
    raw = (getattr(config, "icon_cache_path", "") or "").strip()
    fallback = default_cache_dir()
    if not raw:
        return fallback, ""

    try:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = app_dir() / path
        path.mkdir(parents=True, exist_ok=True)
        # 实际写一次，确认目录可写（不靠 os.access，NTFS/网络盘上它会骗人）
        probe = path / ".write_test"
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        try:
            probe.unlink()
        except OSError:
            pass  # 删不掉也无所谓，说明能写就行
        return path, ""
    except (OSError, ValueError) as exc:
        return fallback, f"自定义缓存目录不可用（{type(exc).__name__}），已回退默认位置"


@dataclass
class Config:
    # 数据渠道
    provider: str = "rkw"
    auto_fallback: bool = True          # 当前渠道失败时自动尝试下一个可用渠道
    request_timeout: int = 15

    # 定时查询（24 小时制 HH:MM）
    query_times: list[str] = field(default_factory=lambda: list(QUERY_TIME_PRESETS["默认（刷新后 5 分钟）"]))

    # 通知
    notify_enabled: bool = True
    notify_min_level: int = 4           # 达到该珍惜度才提醒：4=史诗 5=传说
    notify_native: bool = True          # Windows 原生通知（系统通知中心）
    notify_toast: bool = False          # 应用内浮窗（原生通知不可用时的兜底）
    notify_sound: bool = True           # 系统提示音
    notify_flash: bool = True           # 任务栏闪烁
    webhook_url: str = ""               # 可选：外部推送地址（Bark / 飞书 / 企业微信机器人等）

    # 图标缓存
    icon_cache_enabled: bool = True
    icon_cache_path: str = ""           # 空 = 默认位置；否则为自定义目录（绝对路径）
    log_dir: str = ""                   # 日志目录，空 = %APPDATA%\RocoMerchant\logs

    # 开机自启
    autostart: bool = False
    close_to_tray: bool = True          # 关闭窗口时最小化到托盘而不是退出
    tray_enabled: bool = True           # 显示系统托盘图标

    # 界面
    window_geometry: str = ""
    show_closed_message: bool = True

    # 悬浮窗
    floating_enabled: bool = False
    floating_topmost: bool = True
    floating_geometry: str = ""

    # 渠道表
    providers: dict[str, dict[str, Any]] = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_PROVIDERS)
    )

    # ---------------- 读写 ----------------
    def save(self, path: Path | None = None) -> None:
        target = path or config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        target = path or config_path()
        cfg = cls()
        if not target.exists():
            cfg.save(target)
            return cfg
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cfg

        known = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key in known and value is not None:
                setattr(cfg, key, value)

        # 渠道表合并：老配置缺的新渠道会被补齐，已改过的地址不覆盖
        merged = copy.deepcopy(DEFAULT_PROVIDERS)
        for pid, pdata in (raw.get("providers") or {}).items():
            if not isinstance(pdata, dict):
                continue
            if pid in merged:
                # 只接受用户可改的字段；label/note/official 以代码为准，升级后不会显示旧文案
                for key in ("base_url", "api_key", "headers"):
                    if key in pdata:
                        merged[pid][key] = pdata[key]
            else:
                merged[pid] = pdata
        cfg.providers = merged

        # 布尔字段容错（手工改配置时可能写成 "true" / 1）
        for key in ("auto_fallback", "notify_enabled", "notify_native", "notify_toast",
                    "notify_sound", "notify_flash", "icon_cache_enabled", "autostart", "close_to_tray", "tray_enabled",
                    "floating_enabled", "floating_topmost", "show_closed_message"):
            setattr(cfg, key, bool(getattr(cfg, key)))

        # 清洗查询时间
        cfg.query_times = sanitize_times(cfg.query_times)
        if not cfg.query_times:
            cfg.query_times = list(QUERY_TIME_PRESETS["默认（刷新后 5 分钟）"])
        if cfg.provider not in cfg.providers:
            cfg.provider = "rkw"
        return cfg

    # ---------------- 工具 ----------------
    def provider_conf(self, pid: str) -> dict[str, Any]:
        return self.providers.get(pid, {})

    def provider_label(self, pid: str) -> str:
        return self.providers.get(pid, {}).get("label", pid)

    def provider_order(self) -> list[str]:
        """自动降级顺序：官方 → 社区免费 → 需 Key → 自定义。"""
        order = ["official", "rkw", "yxsr", "shallow", "custom"]
        return [p for p in order if p in self.providers]


def sanitize_times(times: Any) -> list[str]:
    """把 ["8:05", "12:05 "] 之类的输入清洗成 ["08:05", "12:05"] 并排序去重。"""
    result: set[str] = set()
    if not isinstance(times, (list, tuple)):
        return []
    for raw in times:
        if not isinstance(raw, str):
            continue
        text = raw.strip().replace("：", ":").replace(".", ":")
        if not text or ":" not in text:
            continue
        hour_text, _, minute_text = text.partition(":")
        try:
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            result.add(f"{hour:02d}:{minute:02d}")
    return sorted(result)
