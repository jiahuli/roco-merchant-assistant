"""已购买标记：按「日期 + 轮次 + 物品名」记住买过的东西。

换货后标记自动失效——下一轮同样的商品如果又上架，应该重新提醒，
而不是继承上一轮的勾选。所以键里必须带轮次。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from roco.config import app_dir
from roco.core.models import MerchantSnapshot
from roco.core.rounds import ROUND_HOURS, now_cn

MAX_KEYS = 60        # 保留最近 60 个轮次键（约 15 天）


def purchases_path() -> Path:
    return app_dir() / "purchases.json"


def round_key(snapshot: MerchantSnapshot, now: datetime | None = None) -> str:
    """`2026-10-25#3`：同一轮内共享一份勾选状态。"""
    moment = now or now_cn()
    date = moment.date().isoformat()
    index = snapshot.round_index or 0
    if not 1 <= index <= len(ROUND_HOURS):
        index = 0
    return f"{date}#{index}"


def load(path: Path | None = None) -> dict[str, set[str]]:
    target = path or purchases_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    marks = raw.get("marks") if isinstance(raw, dict) else None
    if not isinstance(marks, dict):
        return {}
    return {
        str(key): {str(name) for name in names}
        for key, names in marks.items() if isinstance(names, list)
    }


def _write(data: dict[str, set[str]], path: Path | None = None) -> None:
    target = path or purchases_path()
    # 只保留最近的键，避免文件随使用时间无限增长
    keys = sorted(data)[-MAX_KEYS:]
    payload = {"marks": {k: sorted(data[k]) for k in keys if data[k]}}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass


def is_bought(key: str, name: str, path: Path | None = None) -> bool:
    return name in load(path).get(key, set())


def bought_names(key: str, path: Path | None = None) -> set[str]:
    return load(path).get(key, set())


def toggle(key: str, name: str, path: Path | None = None) -> bool:
    """切换勾选，返回切换后的状态（True = 已购买）。"""
    data = load(path)
    names = data.setdefault(key, set())
    if name in names:
        names.discard(name)
        bought = False
    else:
        names.add(name)
        bought = True
    if not names:
        data.pop(key, None)
    _write(data, path)
    return bought


def mark(key: str, name: str, bought: bool = True, path: Path | None = None) -> None:
    data = load(path)
    names = data.setdefault(key, set())
    if bought:
        names.add(name)
    else:
        names.discard(name)
    if not names:
        data.pop(key, None)
    _write(data, path)


def clear_round(key: str, path: Path | None = None) -> None:
    data = load(path)
    if data.pop(key, None) is not None:
        _write(data, path)


def count(key: str, path: Path | None = None) -> int:
    return len(bought_names(key, path))
