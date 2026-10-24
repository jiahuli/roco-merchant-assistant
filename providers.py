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
from html import unescape
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
_ROUNDS_KEYS = ("rounds", "sale_rounds", "available_rounds", "round_list", "sell_rounds")
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


def _to_rounds(value: Any) -> tuple[int, ...]:
    """把 rounds 字段清洗成升序元组；无法识别返回空元组（视为未知）。"""
    if isinstance(value, (list, tuple)):
        result = []
        for raw in value:
            number = _to_int(raw)
            if number and 1 <= number <= 4:
                result.append(number)
        return tuple(sorted(set(result)))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        try:
            return _to_rounds(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            parts = [part for part in text.replace("，", ",").split(",") if part.strip()]
            return _to_rounds(parts)
    return ()


def filter_by_round(
    items: list[MerchantItem], round_index: int | None
) -> tuple[list[MerchantItem], list[MerchantItem]]:
    """剔除明确不属于本轮的条目，返回 (保留, 剔除)。

    正常情况下接口只返回本轮货架，这里是防御：一旦上游把旧轮次商品
    和全天商品混在一起返回，界面上就会出现"早该下架的还在"。
    轮次未知（rounds 为空）的条目一律保留，宁多不少。
    """
    if not round_index:
        return items, []
    kept: list[MerchantItem] = []
    dropped: list[MerchantItem] = []
    for item in items:
        if item.rounds and round_index not in item.rounds:
            dropped.append(item)
        else:
            kept.append(item)
    return kept, dropped


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
        rounds = _to_rounds(_first_value(raw, _ROUNDS_KEYS))
        items.append(
            MerchantItem(
                name=name,
                price=price,
                limit=limit,
                icon_url=str(image),
                category=category,
                description=desc,
                rounds=rounds,
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


def round_start_at(moment: datetime) -> datetime:
    """本轮开始时间（08/12/16/20 点）。

    页面类渠道不返回 started_at，用它补上：通知去重按「轮次 + 商品」计键，
    有个稳定的起点时间比留空更好排查。
    """
    index = round_index_at(moment)
    hour = ROUND_HOURS[index - 1] if index else OPEN_HOUR
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0)


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
            "User-Agent": "RocoMerchantAssistant/1.6 (+desktop; direct-tls)",
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

    def _request_text(self, url: str, headers: dict[str, str] | None = None,
                      use_doh: bool = True) -> str:
        """抓 HTML（页面即数据源的渠道用）。

        与 `_request` 共用同一套直连 / DoH / 代理剥离逻辑，
        只是不改 JSON 解析这一步。
        """
        merged = {
            "User-Agent": "RocoMerchantAssistant/1.6 (+desktop; direct-tls)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if headers:
            merged.update(headers)
        try:
            body, trace = netdirect.fetch_bytes(
                url, merged, timeout=self.timeout, use_doh=use_doh,
            )
        except netdirect.DirectError as exc:
            raise ProviderError(str(exc)) from exc
        self.route = trace
        return body.decode("utf-8", errors="replace")

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
# 中文社区数据：每日远行商人查询器（页面本身就是数据源）
# ---------------------------------------------------------------------------
_ONEBJJI_ITEM = re.compile(r'<li class="all_show[^>]*>.*?</li>', re.S)
_ONEBJJI_CLASS = re.compile(r'class="([^"]*)"')
_ONEBJJI_ROUND = re.compile(r"\bshow_([1-4])\b")
_ONEBJJI_ARGS = re.compile(r"showShopinfo\((.*?)\)", re.S)
_ONEBJJI_QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'")
_ONEBJJI_NAME = re.compile(r'class="shop_name">\s*([^<]+?)\s*<')
_ONEBJJI_PRICE = re.compile(r'class="shop_price">\s*价格[:：]\s*([0-9][0-9,]*)')
_ONEBJJI_LIMIT = re.compile(r"<em>\s*限购\s*([0-9]+)\s*</em>")
_ONEBJJI_IMG = re.compile(r'<div class="gitem"[^>]*>\s*<img[^>]+src="([^"]+)"')


def parse_onebiji_html(page: str) -> list[MerchantItem]:
    """把「每日远行商人查询器」的页面解析成商品列表（纯函数，可直接单测）。

    2026-10 实测结构，每件商品是一个 `<li class="all_show ... show_1 show_3 ">`：

    * 轮次就写在类名里（`show_N` → 第 N 轮），等价于接口的 `rounds` 字段；
    * 名称 / 分类 / 描述在 `onclick="showShopinfo('<图标>','<名称>','<分类>','<描述>')"`；
    * 价格在 `shop_price`（`价格：3000`），限购在 `<em>限购100</em>`，图标在 `<img src>`。

    同一件商品可能按轮次拆成多条（例如神奇的蛋在第 1、3 轮各一条），
    这里按名称合并 `rounds`，而不是像通用映射那样直接丢掉重名。
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for block in _ONEBJJI_ITEM.findall(page):
        class_match = _ONEBJJI_CLASS.search(block)
        rounds = tuple(sorted({
            int(n) for n in _ONEBJJI_ROUND.findall(class_match.group(1) if class_match else "")
        }))

        args: list[str] = []
        args_match = _ONEBJJI_ARGS.search(block)
        if args_match:
            args = [a.replace("\\'", "'").replace('\\"', '"').strip()
                    for a in _ONEBJJI_QUOTED.findall(args_match.group(1))]

        name = ""
        name_match = _ONEBJJI_NAME.search(block)
        if name_match:
            name = name_match.group(1)
        elif len(args) >= 2:
            name = args[1]
        name = unescape(name).strip()
        if not name:
            continue                      # 页面末尾有占位条目，没有名称

        price_match = _ONEBJJI_PRICE.search(block)
        limit_match = _ONEBJJI_LIMIT.search(block)
        image_match = _ONEBJJI_IMG.search(block)
        price = _to_int(price_match.group(1)) if price_match else None
        limit = _to_int(limit_match.group(1)) if limit_match else None
        image = image_match.group(1) if image_match else (args[0] if args else "")
        if image.startswith("//"):
            image = "https:" + image
        category = unescape(args[2]).strip() if len(args) >= 3 else ""
        description = unescape(args[3]).strip() if len(args) >= 4 else ""

        current = merged.get(name)
        if current is None:
            merged[name] = {
                "rounds": set(rounds), "price": price, "limit": limit,
                "image": image, "category": category, "description": description,
            }
            order.append(name)
            continue
        current["rounds"].update(rounds)
        for key, value in (("price", price), ("limit", limit),
                           ("image", image), ("category", category),
                           ("description", description)):
            if not current[key] and value:
                current[key] = value

    items: list[MerchantItem] = []
    for name in order:
        data = merged[name]
        items.append(
            MerchantItem(
                name=name,
                price=data["price"],
                limit=data["limit"],
                icon_url=str(data["image"] or ""),
                category=str(data["category"] or ""),
                description=str(data["description"] or ""),
                rounds=tuple(sorted(data["rounds"])),
                rarity=infer_rarity(name, data["price"], str(data["category"] or "")),
            )
        )
    return items


def snapshot_from_items(items: list[MerchantItem], source_id: str, label: str,
                        message: str = "") -> MerchantSnapshot:
    """用"已经解析好的商品列表"拼快照（页面类渠道共用）。

    页面不报 `status` / `expectedRound` / `nextRefresh`，一律按本地时间推算；
    `expected_round` 与 `round_index` 取同一个值，因此不会误触发"轮次滞后"重试。
    """
    moment = now_cn()
    round_index = round_index_at(moment)
    kept, dropped = filter_by_round(items, round_index)
    return MerchantSnapshot(
        items=kept,
        source_id=source_id,
        source_label=label,
        status=STATUS_OPEN if is_open_at(moment) else STATUS_CLOSED,
        round_index=round_index,
        expected_round=round_index,
        started_at=round_start_at(moment).strftime("%Y-%m-%d %H:%M"),
        next_refresh=next_refresh_at(moment).strftime("%Y-%m-%d %H:%M"),
        fetched_at=moment,
        dropped_count=len(dropped),
        message=message,
    )


class OnebijiProvider(BaseProvider):
    """中文社区数据：每日远行商人查询器。

    页面本身就是数据源（没有 JSON API），因此用正则解析；站点一旦改版，
    解析会得到空列表并抛错，让上层降级到下一个渠道——绝不把"没解析到"
    当成"本轮没有商品"照常渲染。
    """

    id = "onebiji"
    default_label = "社区数据 · 每日商人查询器（中文）"

    def fetch(self) -> MerchantSnapshot:
        page = self._request_text(self.base_url, {"Referer": "https://www.onebiji.com/"})
        items = parse_onebiji_html(page)
        if not items:
            raise ProviderError("页面里没有解析到商品（站点可能改版或被网关拦截）")
        return snapshot_from_items(items, self.id, self.label,
                                   "数据解析自中文查询器页面，按轮次筛选")


# ---------------------------------------------------------------------------
# 哔哩哔哩 Wiki：只有商店目录，没有每轮货架
# ---------------------------------------------------------------------------
_BWIKI_ROW = re.compile(r"<tr[^>]*>.*?</tr>", re.S)
_BWIKI_SHOP_NAME = re.compile(r'class="shopdex-shop-name">\s*([^<]+?)\s*<')
_BWIKI_SHOP_COUNT = re.compile(r'class="shopdex-shop-count">\s*([0-9]+)\s*件')


def parse_bwiki_shops(page: str) -> list[tuple[str, int]]:
    """解析「商店一览」的商店行，返回 [(商店名, 商品数)]。

    实测每个商店是一行 `<tr class="shopdex-shop-row">`，里面有
    `shopdex-shop-name` 与 `shopdex-shop-count`；商品行与商店行在 DOM 上并不关联，
    所以这里只能取到"目录里有没有远行商人、它挂了几件商品"。
    """
    shops: list[tuple[str, int]] = []
    for row in _BWIKI_ROW.findall(page):
        name_match = _BWIKI_SHOP_NAME.search(row)
        if not name_match:
            continue
        count_match = _BWIKI_SHOP_COUNT.search(row)
        shops.append((unescape(name_match.group(1)).strip(),
                      int(count_match.group(1)) if count_match else 0))
    return shops


class BwikiProvider(BaseProvider):
    """哔哩哔哩 BWIKI（可选渠道，默认不填地址）。

    它只有「商店一览」目录：不列价格、没有轮次与刷新时间，商品行也不与商店归组。
    因此本渠道**主动**给出明确错误，而不是返回一份看起来像货架的空/全量数据。
    """

    id = "bwiki"
    default_label = "哔哩哔哩 Wiki · 商店目录（无货架）"

    def fetch(self) -> MerchantSnapshot:
        payload = self._request(self.base_url)
        page = ""
        if isinstance(payload, dict):
            page = str((payload.get("parse") or {}).get("text") or "")
        if not page:
            raise ProviderError("返回结构异常：不是 MediaWiki 的 parse 结果")
        shops = parse_bwiki_shops(page)
        hits = [(name, count) for name, count in shops if "远行商人" in name]
        if not hits:
            raise ProviderError("商店目录里没有「远行商人」，Wiki 结构可能已改版")
        raise ProviderError(
            f"BWIKI 只有商店目录（{hits[0][0]} · {hits[0][1]} 件商品），"
            "不含价格 / 轮次 / 每轮货架，无法作为货架数据源；请改用中文查询器渠道"
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
        body = self._request_text(self.base_url)
        # 反代目标有两种：JSON（聚合接口）与页面（中文查询器）。
        # 先按 JSON 试，失败再按页面解析——两种都支持，配置才不用二选一。
        try:
            payload = json.loads(body.lstrip("\ufeff").strip())
        except (json.JSONDecodeError, ValueError):
            items = parse_onebiji_html(body)
            if not items:
                raise ProviderError(
                    "反代返回的既不是 JSON、也解析不出页面商品（是否只转发了首页？）") from None
            return snapshot_from_items(items, self.id, self.label,
                                       "数据解析自反代页面，按轮次筛选")
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

    round_index = _to_int(
        _first_value(meta, ("round", "current_round", "roundIndex"))
    ) or round_index_at(moment)
    expected_round = _to_int(
        _first_value(meta, ("expectedRound", "expected_round"))
    ) or round_index_at(moment)
    items, dropped = filter_by_round(build_items(_find_items(payload)), round_index)

    return MerchantSnapshot(
        items=items,
        source_id=source_id,
        source_label=label,
        status=status,
        expected_round=expected_round,
        round_index=round_index,
        started_at=started.strftime("%Y-%m-%d %H:%M") if started else None,
        next_refresh=(next_refresh or next_refresh_at(moment)).strftime("%Y-%m-%d %H:%M"),
        position=_first_value(meta, ("position", "merchant_pos", "location")),
        fetched_at=moment,
        dropped_count=len(dropped),
    )


# ---------------------------------------------------------------------------
# 工厂 + 自动降级
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, type[BaseProvider]] = {
    "official": OfficialProvider,
    "cn_mirror": CnMirrorProvider,
    "onebiji": OnebijiProvider,
    "bwiki": BwikiProvider,
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
