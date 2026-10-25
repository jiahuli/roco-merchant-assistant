"""数据解析层：把页面 / 任意 JSON 变成 MerchantItem 与 MerchantSnapshot。

为什么从 providers 拆出来
------------------------
解析是纯函数（无网络、无渠道概念、可直接单测）。单独成模块后：

* 渠道实现（providers.py）只关心"从哪拿、按什么顺序降级"；
* 站点改版时只需改这里，selftest 的解析用例可直接回归；
* `cn_mirror` 反代与 `onebiji` 复用同一套页面解析，不再挤在一个文件里。

关键约定（与数据源结构绑定，改这里前先读 docs/local/development.md 第 2 章）
--------------------------------------------------------------------------
* `show_N` 类名 = 该商品在第 N 轮售卖；**同名商品按轮次合并 rounds**，
  不能像通用映射那样按名称去重丢弃；
* 价格可能是 `16w` / `16万` 简写：整段抓下来交给 `_to_int()`，
  用 `[0-9]` 截断会把 16 万读成 16；
* **打烊时段一律清空货架**：上游缓存在 00:00 后仍挂着昨日 20 点那轮，
  `status` 甚至还报 open，营业判定以本地时间为准（rounds.is_open_at）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from html import unescape
from typing import Any, Iterable

from roco.core.models import MerchantItem, MerchantSnapshot
from roco.core.rarity import infer_rarity
from roco.core.rounds import (CN_TZ, STATUS_CLOSED, STATUS_OPEN, filter_by_round,
                              is_open_at, next_refresh_at, now_cn, round_index_at,
                              round_start_at)

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


# ---------------------------------------------------------------------------
# 字段映射小工具
# ---------------------------------------------------------------------------
def _first_value(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


# 价格里的简写单位：数据源会用 "16w" / "16万" 表示 160000。
# 只抓数字会把它读成 16，中文读法就成了"十六"（而不是"十六万"）。
_UNIT_FACTORS: tuple[tuple[str, int], ...] = (
    ("亿", 100_000_000), ("万", 10_000), ("w", 10_000), ("W", 10_000),
    ("千", 1_000), ("k", 1_000), ("K", 1_000),
)


def _to_int(value: Any) -> int | None:
    """把字段值转成整数；支持 `万` / `w` / `亿` / `千` / `k` 等简写单位。

    `16w` → 16000、`16万` → 160000、`1.6w` → 16000；没有单位时按原样取整数。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("，", ",")
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    raw = match.group(0).replace(",", "")
    try:
        number = float(raw) if "." in raw else int(raw)
    except ValueError:
        return None
    # 单位只认紧随数字（允许中间有空格）的那一个字符
    unit = text[match.end():].lstrip()[:1]
    if unit:
        for symbol, factor in _UNIT_FACTORS:
            if unit == symbol:
                return int(round(float(number) * factor))
    return int(number)


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


# ---------------------------------------------------------------------------
# 「每日远行商人查询器」页面解析
# ---------------------------------------------------------------------------
_ONEBJJI_ITEM = re.compile(r'<li class="all_show[^>]*>.*?</li>', re.S)
_ONEBJJI_CLASS = re.compile(r'class="([^"]*)"')
_ONEBJJI_ROUND = re.compile(r"\bshow_([1-4])\b")
_ONEBJJI_ARGS = re.compile(r"showShopinfo\((.*?)\)", re.S)
_ONEBJJI_QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'")
_ONEBJJI_NAME = re.compile(r'class="shop_name">\s*([^<]+?)\s*<')
# 价格原文可能是 "3000"、"16w"、"16万"：整段交给 _to_int() 处理单位，
# 不要在这里用 [0-9] 截断，否则 "16w" 会变成 16
_ONEBJJI_PRICE = re.compile(r'class="shop_price">\s*价格[:：]\s*([^<]+)')
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


# ---------------------------------------------------------------------------
# 哔哩哔哩 Wiki「商店一览」解析
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


# ---------------------------------------------------------------------------
# 快照构造
# ---------------------------------------------------------------------------
def snapshot_from_items(items: list[MerchantItem], source_id: str, label: str,
                        message: str = "",
                        moment: datetime | None = None) -> MerchantSnapshot:
    """用"已经解析好的商品列表"拼快照（页面类渠道共用）。

    页面不报 `status` / `expectedRound` / `nextRefresh`，一律按本地时间推算；
    `expected_round` 与 `round_index` 取同一个值，因此不会误触发"轮次滞后"重试。

    **打烊时必须清空货架**：数据源页面在 00:00 之后仍挂着昨天 20:00 那一轮，
    `round_index_at()` 此时返回 0，`filter_by_round()` 见到 falsy 值会原样返回，
    于是"24 点后还显示昨日商品"。这里直接以本地营业时间为准把 `items` 清掉。
    ``moment`` 仅用于测试注入。
    """
    moment = moment or now_cn()
    open_now = is_open_at(moment)
    round_index = round_index_at(moment)
    if open_now:
        kept, dropped = filter_by_round(items, round_index)
    else:
        kept, dropped = [], list(items)
    return MerchantSnapshot(
        items=kept,
        source_id=source_id,
        source_label=label,
        status=STATUS_OPEN if open_now else STATUS_CLOSED,
        round_index=round_index,
        expected_round=round_index,
        started_at=round_start_at(moment).strftime("%Y-%m-%d %H:%M"),
        next_refresh=next_refresh_at(moment).strftime("%Y-%m-%d %H:%M"),
        fetched_at=moment,
        dropped_count=len(dropped),
        message=message if open_now else "打烊中，货架已清空",
    )


def snapshot_from_payload(payload: Any, source_id: str, label: str,
                          moment: datetime | None = None) -> MerchantSnapshot:
    """把未知结构的 JSON 尽力映射成快照。``moment`` 仅用于测试注入。"""
    moment = moment or now_cn()
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

    # 打烊时段（含 00:00–08:00）：上游缓存经常仍报 open 并继续返回昨日 20 点那一轮。
    # 营业时间是不需要请求就能确定的事实，以本地时间为准强制 closed，并把货架清空。
    open_now = is_open_at(moment)
    if not open_now:
        status = STATUS_CLOSED

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
    raw_items = build_items(_find_items(payload))
    if open_now:
        items, dropped = filter_by_round(raw_items, round_index)
    else:
        items, dropped = [], raw_items

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
        message="" if open_now else "打烊中，货架已清空",
    )
