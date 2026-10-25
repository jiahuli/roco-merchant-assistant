"""轮次与营业时间模型：全项目唯一的时间事实来源。

商人每天 08:00 / 12:00 / 16:00 / 20:00 换货（第 1–4 轮），00:00–08:00 打烊。
营业时间是"不请求就能确定的本地事实"——上游接口的缓存常在换货后挂着旧货架、
打烊后仍报 open，因此一切轮次/营业判断都以这里的本地计算为准，不信接口
（见 docs/local/development.md 第 3 章「时间与轮次模型」）。

为什么从 providers 拆出来：history / purchases / catalog 等 core 模块需要
`ROUND_HOURS` 与 `now_cn`；若继续放在网络层的 providers 里，core 就反向依赖
net，分层被破坏。现在依赖方向恢复为 net/core → 本模块，本模块只依赖 models。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from roco.core.models import MerchantItem

CN_TZ = timezone(timedelta(hours=8))          # 北京时间
ROUND_HOURS = (8, 12, 16, 20)
OPEN_HOUR, CLOSE_HOUR = 8, 24

# 快照的营业状态。打烊判定以本地时间为准，不信上游的 status 字段
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_UNKNOWN = "unknown"


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


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


def filter_by_round(
    items: list[MerchantItem], round_index: int | None
) -> tuple[list[MerchantItem], list[MerchantItem]]:
    """剔除明确不属于本轮的条目，返回 (保留, 剔除)。

    正常情况下接口只返回本轮货架，这里是防御：一旦上游把旧轮次商品
    和全天商品混在一起返回，界面上就会出现"早该下架的还在"。
    轮次未知（rounds 为空）的条目一律保留，宁多不少。

    注意：round_index 为 0（打烊时段）时原样返回全部——打烊清空由
    snapshot 构造处负责（见 parsing.snapshot_from_items / snapshot_from_payload），
    这里不做隐式清空。
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
