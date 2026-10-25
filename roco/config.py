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
APP_VERSION = "1.9.1"

# ---------------------------------------------------------------------------
# 各数据渠道的默认定义。base_url 全部可被用户在界面里覆盖。
# ---------------------------------------------------------------------------
DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "official": {
        "label": "官方公开 API",
        "region": "",
        "base_url": "",  # 官方目前未开放该接口，留空即自动跳过
        "api_key": "",
        "note": "腾讯/魔方官方目前没有公开远行商人 API。若日后上线，把地址填在此处，"
                "程序会优先走官方接口；留空则自动跳过。",
        "official": True,
    },
    "cn_mirror": {
        "label": "大陆反代 · 自建",
        "region": "cn",
        "base_url": "",
        "api_key": "",
        "preferred_ip": "",
        "note": "把你自建的中国大陆反向代理地址填在这里（转发到社区镜像即可），"
                "填好后优先使用：目标 IP 在大陆，路由器分流规则会放行直连，"
                "不会绕到 VPN 节点。docs/cn-mirror.md 有一份可直接用的配置。",
        "official": False,
    },
    "onebiji": {
        "label": "社区数据 · 每日商人查询器（中文）",
        "region": "cn",
        "base_url": "https://www.onebiji.com/hykb_tools/comm/lkwgmerchant/preview.php?id=1&immgj=0",
        "api_key": "",
        "preferred_ip": "",
        "note": "中文站点，免 Key。页面直接列出今天全部 4 轮的商品：名称 / 价格 / 限购 / 图标 / "
                "分类 / 每件在哪些轮次售卖（类名 show_1..show_4），程序按轮次筛出本轮货架。"
                "老默认渠道（英文 Cloudflare 镜像）的数据其实也来自这里，且下游缓存更厚；"
                "直连本页可少一层中转，换货后更新更及时。",
        "official": False,
    },
    "bwiki": {
        "label": "哔哩哔哩 Wiki · 商店目录（无货架）",
        "region": "cn",
        # 默认留空：它没有每轮货架数据，填上地址也只会得到明确报错，属预留位。
        "base_url": "",
        "api_key": "",
        "note": "BWIKI 只有「商店一览」目录（110 家商店、1106 件商品），页面明确不列价格，"
                "也没有轮次与刷新时间，DOM 里商品也不与商店归组——无法充当货架数据源。"
                "如需核对目录，可填："
                "https://wiki.biligame.com/rocom/api.php?action=parse&page=%E5%95%86%E5%BA%97%E4%B8%80%E8%A7%88&prop=text&format=json&formatversion=2",
        "official": False,
    },
    "yxsr": {
        "label": "社区精简 · 资源卡",
        "region": "overseas",
        "base_url": "https://yxsr.vercel.app/api/merchant",
        "api_key": "",
        "note": "只返回商品名，不含价格与图标。2026-10 实测其证书已变成自签名，"
                "程序会按设计拒绝连接，仅作历史渠道保留（可在设置里改地址后启用）。",
        "official": False,
    },
    "shallow": {
        "label": "洛克魔法书（需自备 Key）",
        "region": "cn",
        "base_url": "https://wegame.shallow.ink/api/v1/games/rocom/merchant/info",
        "api_key": "",
        "note": "第三方付费服务，需在请求头带 Bearer Key；未填 Key 时自动跳过。"
                "★ 服务器在中国大陆（实测 124.226.74.x，延迟约 140ms），"
                "是现有渠道里唯一天然满足\"接口在境内\"的选项。",
        "official": False,
    },
    "custom": {
        "label": "自定义接口",
        "region": "",
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


def source_root() -> Path:
    """项目根：源码运行 = main.py 所在目录；打包运行 = PyInstaller 解包目录。

    供需要定位 main.py / 静态资源 的模块共用（自启命令、协议注册、resource_path）。
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent.parent


def resource_path(relative: str) -> Path:
    """定位随程序分发的资源：源码运行取项目根，打包运行取解包目录。"""
    return source_root() / relative


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


def _as_bool(value, default: bool = True) -> bool:
    """手工改配置时的布尔容错：`"false"` / `"0"` / `"off"` 都要认成 False。

    直接用 bool() 会把字符串 "false" 判成 True——这正是"配置改了却不生效"的典型原因。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


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


def default_history_dir() -> Path:
    """历史记录默认与配置同目录。"""
    return app_dir()


def resolve_history_dir(config: "Config") -> tuple[Path, str]:
    """解析历史记录存放目录，返回 (目录, 提示)。

    与图标缓存同款策略：相对路径按配置目录展开；目录建不出来或不可写就回退默认，
    并给出可展示的提示——历史记不下来可以接受，程序因此崩溃不行。
    """
    raw = (getattr(config, "history_dir", "") or "").strip()
    fallback = default_history_dir()
    if not raw:
        return fallback, ""

    try:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = app_dir() / path
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        try:
            probe.unlink()
        except OSError:
            pass
        return path, ""
    except (OSError, ValueError) as exc:
        return fallback, f"历史目录不可用（{type(exc).__name__}），已回退默认位置"


@dataclass
class Config:
    # 数据渠道
    provider: str = "onebiji"
    auto_fallback: bool = True          # 当前渠道失败时自动尝试下一个可用渠道
    request_timeout: int = 15

    # 网络直连（绕过 shellclash 等路由器端 / 本机代理）
    force_direct: bool = True           # 剥离代理环境变量 + 禁用系统代理
    doh_enabled: bool = True            # 用 DoH 取真实 IP，绕开 fake-ip 劫持
    prefer_cn_region: bool = True       # 优先选择中国大陆渠道
    verify_egress: bool = True          # 每次查询后校验真实出口（多一次轻量请求）

    # 换货校验：接口缓存可能落后于本地时间，查到旧轮次时自动重试
    auto_recheck_after_refresh: bool = True
    # 货架只剩全天商品、且内容与上次完全相同时的重取节奏（接口"假刷新"）
    recheck_delay_seconds: int = 300     # 延后多久再取一次（默认 5 分钟）
    recheck_max_attempts: int = 3        # 最多重取几次（0 = 关闭该判据）
    # 常规重取用尽后的收尾方式：stop = 收手等下次定时；slow = 转慢速续试
    recheck_give_up_policy: str = "stop"
    recheck_slow_interval_seconds: int = 1800   # 慢速续试间隔（默认 30 分钟）
    recheck_slow_max_attempts: int = 0          # 慢速续试上限（0 = 不限，直到换轮）

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
    # 点名关注：这些物品上架时**无视品质**提醒；与 notify_min_level 并行生效
    notify_items: list[str] = field(default_factory=list)

    # 历史记录
    history_enabled: bool = True        # 关闭后不再记录、也不再展示历史面板
    history_dir: str = ""               # 空 = 与配置同目录；否则为自定义目录（绝对或相对）

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
    # 主题：dark / light / oled / system（system 跟随 Windows「应用模式」）
    theme_mode: str = "dark"
    # 自定义强调色（#rrggbb）；空 = 用当前主题的默认强调色
    theme_accent: str = ""

    # 悬浮窗
    floating_enabled: bool = False
    floating_topmost: bool = True
    floating_geometry: str = ""
    floating_show_icons: bool = True    # 悬浮窗里是否显示物品图标

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
                for key in ("base_url", "api_key", "headers", "preferred_ip"):
                    if key in pdata:
                        merged[pid][key] = pdata[key]
            else:
                merged[pid] = pdata
        cfg.providers = merged

        # 布尔字段容错（手工改配置时可能写成 "true" / 1）
        for key in ("auto_fallback", "notify_enabled", "notify_native", "notify_toast",
                    "notify_sound", "notify_flash", "icon_cache_enabled", "autostart", "close_to_tray", "tray_enabled",
                    "force_direct", "doh_enabled", "prefer_cn_region", "verify_egress",
                    "auto_recheck_after_refresh",
                    "floating_enabled", "floating_topmost", "floating_show_icons",
                    "show_closed_message", "history_enabled"):
            setattr(cfg, key, _as_bool(getattr(cfg, key)))

        # 清洗查询时间
        cfg.query_times = sanitize_times(cfg.query_times)
        if not cfg.query_times:
            cfg.query_times = list(QUERY_TIME_PRESETS["默认（刷新后 5 分钟）"])
        # 换货重取节奏：夹到可用区间。手改配置写成 "300"/"五分钟" 都不会炸，
        # 也不会把重取打成每秒一次的洪水
        cfg.recheck_delay_seconds = clamp_int(cfg.recheck_delay_seconds, 30, 3600, 300)
        cfg.recheck_max_attempts = clamp_int(cfg.recheck_max_attempts, 0, 10, 3)
        # 收尾方式只认白名单，写错就回落默认（不认识的值不该让程序失灵）
        policy = str(cfg.recheck_give_up_policy or "").strip().lower()
        cfg.recheck_give_up_policy = policy if policy in ("stop", "slow") else "stop"
        cfg.recheck_slow_interval_seconds = clamp_int(
            cfg.recheck_slow_interval_seconds, 300, 3600, 1800)
        cfg.recheck_slow_max_attempts = clamp_int(cfg.recheck_slow_max_attempts, 0, 20, 0)

        # 历史目录：非字符串一律当成"默认位置"，避免 Path(123) 抛异常
        cfg.history_dir = (cfg.history_dir if isinstance(cfg.history_dir, str) else "").strip()

        # 关注物品：手改配置写成字符串/数字/嵌套列表都不能让程序崩
        raw_watch = cfg.notify_items
        if isinstance(raw_watch, str):
            raw_watch = [raw_watch]
        if not isinstance(raw_watch, (list, tuple)):
            raw_watch = []
        cleaned: list[str] = []
        for entry in raw_watch:
            text = str(entry).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        cfg.notify_items = cleaned[:200]

        # 1.6.0：下掉英文镜像 rkw（更新慢 + 境外 Cloudflare），默认改用中文查询器。
        # 老配置里残留的 rkw 条目与 provider=rkw 都要迁移，否则会指向一个不存在的渠道。
        cfg.providers.pop("rkw", None)
        if cfg.provider == "rkw":
            cfg.provider = "onebiji"
        if cfg.provider not in cfg.providers:
            cfg.provider = "onebiji"
        return cfg

    # ---------------- 工具 ----------------
    def provider_conf(self, pid: str) -> dict[str, Any]:
        return self.providers.get(pid, {})

    def provider_label(self, pid: str) -> str:
        return self.providers.get(pid, {}).get("label", pid)

    def provider_order(self) -> list[str]:
        """自动降级顺序：官方 → 大陆反代 → 中文查询器 → Wiki 目录 → 需 Key → 自定义。

        大陆反代排在最前：它一旦配置好，目标 IP 就在大陆，路由器端分流会放行直连。
        bwiki 虽然默认未配置，但保留在顺序里：用户填了地址就会排在 yxsr 之前，
        并给出"该渠道没有货架数据"的明确原因。
        """
        order = ["official", "cn_mirror", "onebiji", "bwiki", "yxsr", "shallow", "custom"]
        return [p for p in order if p in self.providers]


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """把配置里的数字夹进 [low, high]；类型不对就用默认值。"""
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


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
