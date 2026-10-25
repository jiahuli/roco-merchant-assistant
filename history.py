"""历史货架记录：按「日期 + 轮次」保存每一轮实际卖过的东西。

为什么单独存一份
----------------
数据源只给"当前这一轮"，换货后就再也拿不到上一轮了。历史记录的价值在于
"刚才 16 点卖的是什么"——这恰恰是换货后最容易被问的问题。

几条刻意的规则
--------------
* **全天商品照存**：历史要还原"这一轮货架长什么样"，条件球、棱镜球这类
  全天在售的本来就该在。所以这里不做 `filter_by_round()`，存的是快照当时的
  `items` 原样（它已经包含了全天商品和本轮商品）。
* **一轮只留一条**：同一轮次重复查询（重试、手动刷新）只更新内容，不追加。
* **按天清理**：只保留最近 `KEEP_DAYS` 天，最多 `MAX_ROUNDS` 轮，避免文件无限增长。
* **纯标准库 + 纯函数**：不碰 Tk，可直接单测。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from config import app_dir
from models import MerchantItem, MerchantSnapshot
from providers import ROUND_HOURS, now_cn

KEEP_DAYS = 7        # 只保留最近 7 天
MAX_ROUNDS = 28      # 7 天 × 4 轮


def history_path() -> Path:
    return app_dir() / "history.json"


@dataclass(frozen=True)
class HistoryRound:
    """一轮历史货架。"""

    date: str                       # 2026-10-25（北京时间自然日）
    round_index: int                # 1..4
    items: tuple[MerchantItem, ...] = ()
    fetched_at: str = ""            # 最后一次看到这轮的时间

    @property
    def key(self) -> str:
        return f"{self.date}#{self.round_index}"

    @property
    def clock(self) -> str:
        """轮次对应的钟点，如 20:00。"""
        if 1 <= self.round_index <= len(ROUND_HOURS):
            return f"{ROUND_HOURS[self.round_index - 1]:02d}:00"
        return ""

    @property
    def label(self) -> str:
        """`10-25 20:00`，跨天也能一眼看出是哪天。"""
        return f"{self.date[5:]} {self.clock}".strip()

    @property
    def count(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "round": self.round_index,
            "fetched_at": self.fetched_at,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Optional["HistoryRound"]:
        try:
            date = str(data["date"])
            round_index = int(data["round"])
        except (KeyError, TypeError, ValueError):
            return None
        if not date or not 1 <= round_index <= len(ROUND_HOURS):
            return None
        raw_items = data.get("items") or []
        items = tuple(MerchantItem.from_dict(x) for x in raw_items
                      if isinstance(x, dict) and x.get("name"))
        return cls(date=date, round_index=round_index, items=items,
                   fetched_at=str(data.get("fetched_at") or ""))


def _sort(rounds: Iterable[HistoryRound]) -> list[HistoryRound]:
    """按时间倒序：最新的 20 点在最前，然后 16 / 12 / 08。"""
    return sorted(rounds, key=lambda r: (r.date, r.round_index), reverse=True)


def load(path: Path | None = None) -> list[HistoryRound]:
    """读取历史；文件缺失/损坏都返回空列表，绝不影响主流程。"""
    target = path or history_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("rounds") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    rounds = [HistoryRound.from_dict(x) for x in entries if isinstance(x, dict)]
    return _sort(r for r in rounds if r is not None)


def _prune(rounds: list[HistoryRound], now: datetime) -> list[HistoryRound]:
    """按天裁剪：只留最近 KEEP_DAYS 天，并限制总轮数。"""
    oldest = (now.date() - timedelta(days=KEEP_DAYS - 1)).isoformat()
    kept = [r for r in _sort(rounds) if r.date >= oldest]
    return kept[:MAX_ROUNDS]


def _write(rounds: list[HistoryRound], path: Path | None = None) -> None:
    target = path or history_path()
    payload = {"rounds": [r.to_dict() for r in rounds]}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)          # 原子替换：中途崩溃不会留下半个文件
    except OSError:
        pass


def record(
    snapshot: MerchantSnapshot,
    path: Path | None = None,
    now: datetime | None = None,
) -> Optional[HistoryRound]:
    """把一次成功查询记进历史。

    打烊、空货架、跨日"补货中"的空快照都不会被记录——否则历史里会出现
    一整条"什么都没卖"的假记录。
    """
    if snapshot.status != "open" or not snapshot.items:
        return None
    moment = now or now_cn()
    date = moment.date().isoformat()
    round_index = snapshot.round_index or 0
    if not 1 <= round_index <= len(ROUND_HOURS):
        return None

    entry = HistoryRound(
        date=date,
        round_index=round_index,
        items=tuple(snapshot.items),
        fetched_at=moment.strftime("%Y-%m-%d %H:%M:%S"),
    )
    rounds = load(path)
    existing = next((r for r in rounds if r.key == entry.key), None)
    if existing is not None and [i.signature for i in existing.items] == \
            [i.signature for i in entry.items]:
        # 内容没变就不写盘，避免每 5 分钟一次无意义 IO
        return existing
    rounds = [r for r in rounds if r.key != entry.key]
    rounds.append(entry)
    rounds = _prune(rounds, moment)
    _write(rounds, path)
    return entry


def for_display(path: Path | None = None, limit: int = MAX_ROUNDS) -> list[HistoryRound]:
    """界面用：倒序排列的历史（20 点 → 16 → 12 → 08）。"""
    return load(path)[:limit]


def clear(path: Path | None = None) -> None:
    _write([], path)
