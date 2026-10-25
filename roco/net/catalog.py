"""远行商人「可能售卖什么」的清单，供按物品通知使用。

为什么不能只靠 Wiki
--------------------
2026-10 实测：BWIKI（`wiki.biligame.com/rocom`）**没有**远行商人的轮换货架，
`商店一览` 里 `#2002 远行商人` 只登记了 1 件常驻商品；正文提到"远行商人"的
条目只有 3 个。而商人每 4 小时换一次货，还会上"可可果球"这类限时活动道具，
Wiki 必然滞后。

所以清单是**四路合并**的，缺哪一路都不致命：

| 来源 | 作用 |
|---|---|
| `data/merchant_items.json`（随包内置） | 离线可用，首次打开选择器就有东西 |
| Wiki 实时刷新（手动触发） | 补齐 Wiki 已收录的条目与图标 |
| **本机观察**（每次查询自动） | 只要商品上过架就进入清单，Wiki 没收录也能选到 |
| 手动添加 | 兜底：新活动道具名字已知但还没上架 |

图标一律“挑小的”：同一个物品可能同时有电商页 100px 缩略图和 Wiki 原图，
`prefer_small_icon()` 会优先选带缩略尺寸的地址，省流量也省磁盘。
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Iterable, Optional

from roco.net import netdirect
from roco.config import app_dir, resource_path
from roco.core.models import MerchantSnapshot
from roco.core.rounds import now_cn

WIKI_API = "https://wiki.biligame.com/rocom/api.php"
WIKI_REFERER = "https://wiki.biligame.com/rocom/"
MERCHANT_SHOP_ID = "shop-2002"      # 商店一览里「远行商人」那一行
MAX_ITEMS = 600                      # 清单上限，防止无限增长

SOURCE_SEED = "内置"
SOURCE_WIKI = "Wiki"
SOURCE_SHELF = "本机"
SOURCE_MANUAL = "手动"

# 带这些片段的地址是缩略图（服务端已经压过），优先使用
_SMALL_ICON_HINTS = ("/100px-", "/64px-", "/48px-", "/thumb/", "thumb/")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RocoMerchantAssistant/1.0"


@dataclass
class CatalogItem:
    name: str
    icon: str = ""
    source: str = ""
    category: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "icon": self.icon,
                "source": self.source, "category": self.category}

    @classmethod
    def from_dict(cls, data: dict) -> Optional["CatalogItem"]:
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        return cls(name=name, icon=str(data.get("icon") or ""),
                   source=str(data.get("source") or ""),
                   category=str(data.get("category") or ""))


def seed_path() -> Path:
    return resource_path("data/merchant_items.json")


def cache_path() -> Path:
    return app_dir() / "items.json"


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _read_items(path: Path) -> tuple[list[CatalogItem], list[str], str]:
    """返回 (条目, 已隐藏的名字, 更新时间)。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], [], ""
    if not isinstance(raw, dict):
        return [], [], ""
    items = [CatalogItem.from_dict(x) for x in raw.get("items") or []
             if isinstance(x, dict)]
    hidden = [str(x) for x in raw.get("hidden") or [] if x]
    return [i for i in items if i], hidden, str(raw.get("updated_at") or "")


def _write(path: Path, items: Iterable[CatalogItem], hidden: Iterable[str]) -> None:
    payload = {
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "items": [i.to_dict() for i in items][:MAX_ITEMS],
        "hidden": sorted({h for h in hidden}),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def merge(*groups: Iterable[CatalogItem]) -> list[CatalogItem]:
    """按名称合并；后面的来源优先补空字段，但不覆盖已有的图标。"""
    merged: dict[str, CatalogItem] = {}
    for group in groups:
        for item in group:
            if not item.name:
                continue
            current = merged.get(item.name)
            if current is None:
                merged[item.name] = CatalogItem(item.name, item.icon, item.source, item.category)
                continue
            if not current.icon and item.icon:
                current.icon = item.icon
            if not current.category and item.category:
                current.category = item.category
            if item.source and item.source != SOURCE_SEED:
                current.source = item.source
    return sorted(merged.values(), key=lambda i: i.name)


def load(path: Path | None = None) -> list[CatalogItem]:
    """内置 + 本机缓存合并后的清单（含用户隐藏过滤）。"""
    cache = path or cache_path()
    seed_items, _, _ = _read_items(seed_path())
    cached, hidden, _ = _read_items(cache)
    items = merge(seed_items, cached)
    if hidden:
        blocked = set(hidden)
        items = [i for i in items if i.name not in blocked]
    return items


def last_updated(path: Path | None = None) -> str:
    _, _, stamp = _read_items(path or cache_path())
    return stamp


def is_empty(path: Path | None = None) -> bool:
    return not load(path)


# ---------------------------------------------------------------------------
# 图标：优先小体积
# ---------------------------------------------------------------------------
def prefer_small_icon(candidates: Iterable[str]) -> str:
    """在多个候选地址里优先挑缩略图；没有缩略图就按原顺序取第一个。"""
    urls = [str(u).strip() for u in candidates if str(u or "").strip()]
    for url in urls:
        if any(hint in url for hint in _SMALL_ICON_HINTS):
            return url
    return urls[0] if urls else ""


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def save(items: Iterable[CatalogItem], path: Path | None = None,
         hidden: Iterable[str] = ()) -> None:
    _write(path or cache_path(), items, hidden)


def observe(snapshot: MerchantSnapshot, path: Path | None = None) -> int:
    """把本轮实际看到的商品并入清单，返回新增条数。

    这是"可可果球"这类活动道具能被选到的主要途径：只要它上过一次架，
    下次打开选择器就在列表里了，不必等 Wiki 更新。
    """
    target = path or cache_path()
    cached, hidden, _ = _read_items(target)
    existing = {i.name: i for i in cached}
    added = 0
    changed = False
    for item in snapshot.items:
        name = (item.name or "").strip()
        if not name or name in hidden:
            continue
        icon = prefer_small_icon([item.icon_url])
        current = existing.get(name)
        if current is None:
            cached.append(CatalogItem(name=name, icon=icon, source=SOURCE_SHELF,
                                      category=item.category))
            added += 1
            changed = True
        elif icon and not current.icon:
            current.icon = icon
            changed = True
    if changed:
        _write(target, merge(cached), hidden)
    return added


def add_manual(name: str, path: Path | None = None) -> bool:
    """手动添加一个物品名；已存在返回 False。"""
    name = (name or "").strip()
    if not name:
        return False
    target = path or cache_path()
    cached, hidden, _ = _read_items(target)
    if any(i.name == name for i in cached) or name in hidden:
        return False
    cached.append(CatalogItem(name=name, source=SOURCE_MANUAL))
    _write(target, merge(cached), hidden)
    return True


def remove(name: str, path: Path | None = None) -> None:
    """从清单里移除（内置条目靠 hidden 列表记住，否则下次合并又会回来）。"""
    target = path or cache_path()
    cached, hidden, _ = _read_items(target)
    cached = [i for i in cached if i.name != name]
    if name not in hidden:
        hidden.append(name)
    _write(target, merge(cached), hidden)


# ---------------------------------------------------------------------------
# Wiki 抓取（解析函数独立出来，方便用固定 HTML 单测）
# ---------------------------------------------------------------------------
_ROW = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)
_ROW_CLASS = re.compile(r'<tr\b[^>]*class="([^"]*)"', re.S)
_ROW_ID = re.compile(r'id="(shop-\d+)"')
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S)
_IMG = re.compile(r'<img[^>]+src="([^"]+)"', re.S)
_TAG = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    return unescape(_TAG.sub(" ", html)).replace("\xa0", " ").strip()


def parse_shop_items(html: str, shop_id: str = MERCHANT_SHOP_ID) -> list[str]:
    """从「商店一览」里取出某个商店名下的商品名。

    表结构是"商店行 + 紧跟其后 N 条商品行"，`shopdex-shop-count` 写明 N。
    商店行之间没有嵌套，所以按行顺序扫一遍即可，不依赖 DOM 层级。
    """
    names: list[str] = []
    remaining = 0
    for row in _ROW.findall(html):
        row_class = _ROW_CLASS.search(row)
        is_shop = bool(row_class and "shopdex-shop-row" in row_class.group(1))
        if is_shop:
            if remaining:          # 上一家还没读完就遇到新商店：数据异常，停止
                break
            if _ROW_ID.search(row) and shop_id in row:
                count = re.search(r"shopdex-shop-count[^>]*>\s*(\d+)", row)
                remaining = int(count.group(1)) if count else 10
            continue
        if remaining <= 0:
            continue
        cells = _CELL.findall(row)
        if len(cells) < 2:
            continue
        name = _text(cells[1])
        if name:
            names.append(name)
        remaining -= 1
    # 去重保序
    return list(dict.fromkeys(names))


def parse_page_icon(html: str) -> str:
    """条目页里第一张图就是图标（patchwiki 的原图）。"""
    for url in _IMG.findall(html):
        if "patchwiki.biligame.com" in url:
            return url
    return ""


def _wiki_json(params: dict, timeout: int) -> dict:
    url = WIKI_API + "?" + urllib.parse.urlencode(params)
    payload, _trace = netdirect.fetch_json(
        url, {"User-Agent": _UA, "Referer": WIKI_REFERER}, timeout=timeout,
    )
    return payload if isinstance(payload, dict) else {}


def fetch_wiki_items(timeout: int = 20) -> list[CatalogItem]:
    """抓 Wiki：商店一览的常驻条目 + 正文提到远行商人的条目（带图标）。"""
    found: list[CatalogItem] = []

    shop = _wiki_json({"action": "parse", "page": "商店一览", "prop": "text",
                       "format": "json", "formatversion": 2}, timeout)
    html = str((shop.get("parse") or {}).get("text") or "")
    for name in parse_shop_items(html):
        found.append(CatalogItem(name=name, source=SOURCE_WIKI))

    hits = _wiki_json({"action": "query", "list": "search",
                       "srsearch": 'insource:"远行商人"', "srlimit": 50,
                       "srnamespace": 0, "format": "json", "formatversion": 2}, timeout)
    titles = [str(x.get("title") or "") for x in
              (hits.get("query") or {}).get("search") or [] if x.get("title")]

    for title in titles[:20]:
        page = _wiki_json({"action": "parse", "page": title, "prop": "text",
                           "format": "json", "formatversion": 2}, timeout)
        page_html = str((page.get("parse") or {}).get("text") or "")
        if not page_html:
            continue
        found.append(CatalogItem(name=title, icon=parse_page_icon(page_html),
                                 source=SOURCE_WIKI))
    return merge(found)


def refresh_from_wiki(path: Path | None = None, timeout: int = 20) -> tuple[int, str]:
    """把 Wiki 结果并入本机清单；返回 (清单总数, 提示文案)。

    网络失败不抛异常：清单本来就是多路合并的，抓不到就继续用现有内容。
    """
    target = path or cache_path()
    try:
        wiki_items = fetch_wiki_items(timeout=timeout)
    except Exception as exc:                     # noqa: BLE001 - 网络问题不该打断界面
        return len(load(target)), f"Wiki 抓取失败：{exc}"

    if not wiki_items:
        return len(load(target)), "Wiki 没有返回可用条目（页面结构可能已改版）"

    cached, hidden, _ = _read_items(target)
    merged = merge(cached, [i for i in wiki_items if i.name not in set(hidden)])
    added = len({i.name for i in merged}) - len({i.name for i in cached})
    _write(target, merged, hidden)
    stamp = now_cn().strftime("%H:%M")
    detail = f"Wiki 更新完成（{stamp}）：共 {len(merged)} 条"
    if added > 0:
        detail += f"，新增 {added} 条"
    return len(merged), detail


def updated_text(path: Path | None = None) -> str:
    stamp = last_updated(path)
    return f"上次更新 {stamp[5:16]}" if stamp else "尚未从 Wiki 更新"
