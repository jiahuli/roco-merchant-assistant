"""数据渠道（Provider）层。

设计要点
--------
* 官方接口优先：`official` 渠道默认地址为空，一旦用户填入官方地址就会最先被调用；
  官方不提供该接口时自动跳过，不会阻塞。
* 地址可替换：所有 base_url 都来自 config.json，界面里可直接改。
* 多渠道切换：界面下拉框切换；勾选"自动降级"时，当前渠道失败会按顺序尝试下一个。
* 结构自适应：真实世界里的接口字段五花八门，这里用 candidate-key 的方式做映射，
  因此接「自定义接口」时不必改代码。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import netdirect
from config import Config
from models import MerchantItem, MerchantSnapshot
from rarity import infer_rarity

CN_TZ = timezone(timedelta(hours=8))          # 北京时间
ROUND_HOURS = (8, 12, 16, 20)
OPEN_HOUR, CLOSE_HOUR = 8, 24

# 常见的字段名候选，用于把任意 JSON 映射成统一模型
_NAME_KEYS = ("name", "goods_name", "goodsName", "item_name", "itemName", "title", "label")
_PRICE_KEYS = ("price", "raw_price", "rawPrice", "cost", "goods_price", "sell_price", "money")
_LIMIT_KEYS = ("limit", "buy_limit", "buyLimit", "quota", "num", "count", "stock", "max_buy")
_IMAGE_KEYS = ("image", "icon", "img", "pic", "image_url", "imageUrl", "icon_url", "iconUrl", "picture")
_CATEGORY_KEYS = ("category", "type", "kind", "classify", "goods_type")
_DESC_KEYS = ("description", "desc", "intro", "detail", "note", "remark")
_ITEM_LIST_PATHS = (
    "items", "goods_list", "goodsList", "data.items", "data.goods_list",
    "data.list", "data.goods", "data.records", "list", "result.list", "data",
)

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _first_value(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    match = re.search(r"-?\d[\d,]*", text)
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _dig(payload: Any, path: str) -> Any:
    node = payload
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _find_items(payload: Any) -> list[dict[str, Any]]:
    """从任意 JSON 里找出商品数组。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for path in _ITEM_LIST_PATHS:
        node = _dig(payload, path)
        if isinstance(node, list) and node and isinstance(node[0], dict):
            return node
    # 兜底：找第一个"元素为字典的列表"
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def build_items(raw_items: Iterable[dict[str, Any]]) -> list[MerchantItem]:
    items: list[MerchantItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        name = _first_value(raw, _NAME_KEYS)
        if not name:
            continue
        name = str(name).strip()
        if name in seen:
            continue
        seen.add(name)
        price = _to_int(_first_value(raw, _PRICE_KEYS))
        limit = _to_int(_first_value(raw, _LIMIT_KEYS))
        image = _first_value(raw, _IMAGE_KEYS) or ""
        category = str(_first_value(raw, _CATEGORY_KEYS) or "")
        desc = str(_first_value(raw, _DESC_KEYS) or "")
        items.append(
            MerchantItem(
                name=name,
                price=price,
                limit=limit,
                icon_url=str(image),
                category=category,
                description=desc,
                rarity=infer_rarity(name, price, category),
            )
        )
    return items


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):  # 毫秒/秒时间戳
        stamp = float(value)
        if stamp > 1e11:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, CN_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("T", " ").replace("Z", "")
    text = text.split("+")[0].strip()          # 去掉时区后缀
    text = text.split(".")[0].strip()          # 去掉毫秒
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


def round_index_at(moment: datetime) -> int:
    """08:00 / 12:00 / 16:00 / 20:00 分别是第 1..4 轮。"""
    hour = moment.hour
    index = 0
    for order, start in enumerate(ROUND_HOURS, start=1):
        if hour >= start:
            index = order
    return index


def is_open_at(moment: datetime) -> bool:
    return OPEN_HOUR <= moment.hour < CLOSE_HOUR


def next_refresh_at(moment: datetime) -> datetime:
    """下一轮刷新时间。"""
    for start in ROUND_HOURS:
        candidate = moment.replace(hour=start, minute=0, second=0, microsecond=0)
        if candidate > moment:
            return candidate
    return (moment + timedelta(days=1)).replace(hour=ROUND_HOURS[0], minute=0, second=0, microsecond=0)


def next_open_at(moment: datetime) -> datetime:
    """商人下次出现时间（打烊时段用）。"""
    if is_open_at(moment):
        return moment
    return moment.replace(hour=OPEN_HOUR, minute=0, second=0, microsecond=0) + (
        timedelta(days=1) if moment.hour >= CLOSE_HOUR else timedelta(0)
    )


# ---------------------------------------------------------------------------
# 渠道基类
# ---------------------------------------------------------------------------
class ProviderError(RuntimeError):
    pass


class BaseProvider:
    id = "base"
    default_label = "渠道"

    def __init__(self, conf: dict[str, Any], timeout: int = 15) -> None:
        self.conf = conf or {}
        self.timeout = timeout
        self.route: dict[str, Any] = {}

    # -- 便于子类读取 --
    @property
    def base_url(self) -> str:
        return str(self.conf.get("base_url") or "").strip()

    @property
    def api_key(self) -> str:
        return str(self.conf.get("api_key") or "").strip()

    @property
    def label(self) -> str:
        return str(self.conf.get("label") or self.default_label)

    @property
    def preferred_ip(self) -> str:
        """手动指定的建连 IP（绕过 DNS/CDN 归属不理想时用）。"""
        return str(self.conf.get("preferred_ip") or "").strip()

    def is_configured(self) -> bool:
        return bool(self.base_url)

    # -- HTTP --
    def _request(self, url: str, headers: dict[str, str] | None = None,
                 use_doh: bool = True) -> Any:
        """直连请求：不走任何代理，DoH 取真实 IP，TLS 校验照旧。

        路由信息挂在 `self.route` 上，供界面显示"出口是否被代理接管"。
        """
        merged = {
            "User-Agent": "RocoMerchantAssistant/1.4 (+desktop; direct-tls)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if headers:
            merged.update(headers)
        try:
            payload, trace = netdirect.fetch_json(
                url, merged, timeout=self.timeout,
                use_doh=use_doh, prefer_ip=self.preferred_ip,
            )
        except netdirect.DirectError as exc:
            raise ProviderError(str(exc)) from exc
        self.route = trace
        return payload

    def fetch(self) -> MerchantSnapshot:  # pragma: no cover - 由子类实现
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 官方渠道：结构未知，交给通用解析；地址由用户填写
# ---------------------------------------------------------------------------
class OfficialProvider(BaseProvider):
    id = "official"
    default_label = "官方公开 API"

    def fetch(self) -> MerchantSnapshot:
        payload = self._request(self.base_url, self._headers())
        return snapshot_from_payload(payload, self.id, self.label)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


# ---------------------------------------------------------------------------
# 社区镜像：字段最全
# ---------------------------------------------------------------------------
class RkwProvider(BaseProvider):
    id = "rkw"
    default_label = "社区镜像 · 洛克王国世界 Wiki"

    def fetch(self) -> MerchantSnapshot:
        payload = self._request(self.base_url)
        if not isinstance(payload, dict):
            raise ProviderError("返回结构异常")

        items = build_items(_find_items(payload))
        status_raw = str(payload.get("status") or "").lower()
        if status_raw in ("open", "closed"):
            status = status_raw
        else:
            status = STATUS_OPEN if is_open_at(now_cn()) else STATUS_CLOSED

        next_refresh = _parse_dt(payload.get("nextRefreshBeijing") or payload.get("next_refresh_time"))
        started = _parse_dt(payload.get("startedAtBeijing") or payload.get("refresh_time"))
        moment = now_cn()
        return MerchantSnapshot(
            items=items,
            source_id=self.id,
            source_label=self.label,
            status=status,
            round_index=_to_int(payload.get("round") or payload.get("current_round")) or round_index_at(moment),
            started_at=started.strftime("%Y-%m-%d %H:%M") if started else None,
            next_refresh=(next_refresh or next_refresh_at(moment)).strftime("%Y-%m-%d %H:%M"),
            position=payload.get("position") or payload.get("merchant_pos"),
            fetched_at=moment,
            stale=bool(payload.get("stale")),
        )


# ---------------------------------------------------------------------------
# 社区精简：只有商品名
# ---------------------------------------------------------------------------
class YxsrProvider(BaseProvider):
    id = "yxsr"
    default_label = "社区精简 · 远行商人资源卡"

    def fetch(self) -> MerchantSnapshot:
        payload = self._request(self.base_url)
        items = build_items(_find_items(payload))
        moment = now_cn()
        refresh_stamp = None
        if isinstance(payload, dict):
            refresh_stamp = _parse_dt(payload.get("updateTimeBeijing"))
        return MerchantSnapshot(
            items=items,
            source_id=self.id,
            source_label=self.label,
            status=STATUS_OPEN if is_open_at(moment) else STATUS_CLOSED,
            round_index=round_index_at(moment),
            started_at=refresh_stamp.strftime("%Y-%m-%d %H:%M") if refresh_stamp else None,
            next_refresh=next_refresh_at(moment).strftime("%Y-%m-%d %H:%M"),
            fetched_at=moment,
            message="该渠道不含价格与图标",
        )


# ---------------------------------------------------------------------------
# 洛克魔法书：需自带 Key
# ---------------------------------------------------------------------------
class ShallowProvider(BaseProvider):
    id = "shallow"
    default_label = "洛克魔法书（需 Key）"

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def fetch(self) -> MerchantSnapshot:
        if not self.api_key:
            raise ProviderError("未配置 API Key，请到「设置 → 数据渠道」填写")
        payload = self._request(self.base_url, {"Authorization": f"Bearer {self.api_key}"})
        snapshot = snapshot_from_payload(payload, self.id, self.label)
        if not snapshot.items:
            raise ProviderError("接口返回为空（常见原因：Key 无效或额度用尽）")
        return snapshot


# ---------------------------------------------------------------------------
# 自定义接口
# ---------------------------------------------------------------------------
class CnMirrorProvider(BaseProvider):
    """自建的大陆反向代理：把社区镜像的数据转发到国内服务器上。

    目标 IP 落在中国大陆，路由器端分流规则会放行直连，流量不经过 VPN 节点；
    同时链路只有 PC → 国内服务器 → Cloudflare，少一跳跨境。
    """

    id = "cn_mirror"
    default_label = "大陆反代 · 自建"

    def fetch(self) -> MerchantSnapshot:
        payload = self._request(self.base_url)
        snapshot = snapshot_from_payload(payload, self.id, self.label)
        if not snapshot.items:
            raise ProviderError("未能从返回值里识别出商品列表（反代是否只转发了首页？）")
        return snapshot


class CustomProvider(BaseProvider):
    id = "custom"
    default_label = "自定义接口"

    def fetch(self) -> MerchantSnapshot:
        headers: dict[str, str] = {}
        raw_headers = self.conf.get("headers") or ""
        if isinstance(raw_headers, dict):
            headers.update({str(k): str(v) for k, v in raw_headers.items()})
        elif isinstance(raw_headers, str) and raw_headers.strip():
            try:
                parsed = json.loads(raw_headers)
                if isinstance(parsed, dict):
                    headers.update({str(k): str(v) for k, v in parsed.items()})
            except json.JSONDecodeError as exc:
                raise ProviderError("自定义请求头不是合法 JSON") from exc
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")

        payload = self._request(self.base_url, headers)
        snapshot = snapshot_from_payload(payload, self.id, self.label)
        if not snapshot.items:
            raise ProviderError("未能从返回值里识别出商品列表")
        return snapshot


def snapshot_from_payload(payload: Any, source_id: str, label: str) -> MerchantSnapshot:
    """把未知结构的 JSON 尽力映射成快照。"""
    moment = now_cn()
    meta = payload if isinstance(payload, dict) else {}
    data = meta.get("data") if isinstance(meta.get("data"), dict) else meta

    status_raw = str(
        _first_value(meta, ("status", "state", "merchant_status")) or ""
    ).lower()
    if status_raw in ("open", "opened", "active", "1", "true"):
        status = STATUS_OPEN
    elif status_raw in ("closed", "close", "inactive", "0", "false"):
        status = STATUS_CLOSED
    else:
        status = STATUS_OPEN if is_open_at(moment) else STATUS_CLOSED

    next_refresh = _parse_dt(
        _first_value(meta, ("nextRefreshBeijing", "next_refresh_time", "nextRefresh", "next_refresh"))
    ) or _parse_dt(_first_value(data, ("next_refresh_time", "nextRefresh", "next_refresh")))
    started = _parse_dt(
        _first_value(meta, ("startedAtBeijing", "refresh_time", "started_at", "refreshTime"))
    )

    return MerchantSnapshot(
        items=build_items(_find_items(payload)),
        source_id=source_id,
        source_label=label,
        status=status,
        round_index=_to_int(_first_value(meta, ("round", "current_round", "roundIndex")))
        or round_index_at(moment),
        started_at=started.strftime("%Y-%m-%d %H:%M") if started else None,
        next_refresh=(next_refresh or next_refresh_at(moment)).strftime("%Y-%m-%d %H:%M"),
        position=_first_value(meta, ("position", "merchant_pos", "location")),
        fetched_at=moment,
    )


# ---------------------------------------------------------------------------
# 工厂 + 自动降级
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, type[BaseProvider]] = {
    "official": OfficialProvider,
    "cn_mirror": CnMirrorProvider,
    "rkw": RkwProvider,
    "yxsr": YxsrProvider,
    "shallow": ShallowProvider,
    "custom": CustomProvider,
}


def build_provider(pid: str, config: Config) -> BaseProvider:
    cls = _REGISTRY.get(pid, CustomProvider)
    return cls(config.provider_conf(pid), timeout=config.request_timeout)


def available_providers(config: Config) -> list[tuple[str, str, bool]]:
    """返回 (id, 名称, 是否已配置)。"""
    result = []
    for pid in config.provider_order():
        provider = build_provider(pid, config)
        result.append((pid, provider.label, provider.is_configured()))
    return result


_REGION_RANK = {"cn": 0, "": 1, "overseas": 2}


def _region_rank(config: Config, pid: str) -> int:
    """区域排序权重：大陆 0 < 未知 1 < 境外 2。"""
    region = str(config.provider_conf(pid).get("region") or "")
    return _REGION_RANK.get(region, 1)


def provider_regions(config: Config) -> dict[str, str]:
    """供界面展示：渠道 id → 区域标签。"""
    return {
        pid: str(config.provider_conf(pid).get("region") or "")
        for pid in config.provider_order()
    }


def fetch_snapshot(config: Config) -> tuple[MerchantSnapshot, list[str]]:
    """按配置拉取数据，返回 (快照, 错误信息列表)。"""
    order: list[str] = []
    primary = config.provider
    order.append(primary)
    if config.auto_fallback:
        rest = [pid for pid in config.provider_order() if pid != primary]
        if config.prefer_cn_region:
            # 大陆渠道优先尝试：目标 IP 在境内时路由器不会转发到代理节点，链路更短更稳
            rest = sorted(rest, key=lambda pid: _region_rank(config, pid))
        order.extend(rest)

    errors: list[str] = []
    for pid in order:
        provider = build_provider(pid, config)
        if not provider.is_configured():
            if pid == primary:
                errors.append(f"{provider.label}：未配置接口地址")
            continue
        try:
            snapshot = provider.fetch()
        except ProviderError as exc:
            errors.append(f"{provider.label}：{exc}")
            continue
        except Exception as exc:  # 兜底，避免单渠道异常拖垮界面
            errors.append(f"{provider.label}：{type(exc).__name__} {exc}")
            continue
        if not snapshot.items and snapshot.status == STATUS_OPEN:
            errors.append(f"{provider.label}：本轮暂无商品数据")
            continue
        snapshot.source_id = pid
        snapshot.source_label = provider.label
        snapshot.route = dict(getattr(provider, "route", {}) or {})

        # 出口校验：多一次轻量请求，但能把"是否被代理接管"变成可测的事实
        if config.verify_egress:
            snapshot.route["egress"] = netdirect.egress_of(
                snapshot.route.get("host", ""), timeout=6
            ) if snapshot.route.get("host") else {"ok": False}
        return snapshot, errors

    raise ProviderError("\n".join(errors) if errors else "没有可用的数据渠道，请到设置里填写接口地址")
