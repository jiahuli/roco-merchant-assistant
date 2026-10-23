"""商品与快照的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from numerals import format_price_chinese
from rarity import Rarity


# 一天 4 轮换货：08:00 / 12:00 / 16:00 / 20:00
TOTAL_ROUNDS = 4


@dataclass
class MerchantItem:
    """一件上架商品。"""

    name: str
    price: Optional[int] = None          # 单价（洛克贝），未知为 None
    limit: Optional[int] = None          # 限购数量
    icon_url: str = ""                   # 图标地址
    category: str = ""                   # 分类，如"炼金材料"
    description: str = ""
    rounds: tuple[int, ...] = ()         # 该商品在哪些轮次售卖；[1,2,3,4] = 全天
    rarity: Rarity = field(default_factory=lambda: Rarity(1, "普通", "#8b98ab"))

    @property
    def is_all_day(self) -> bool:
        """是否全天在售（条件球、棱镜球这类不随换货消失的商品）。

        接口用 `rounds` 字段表达售卖轮次：[1,2,3,4] 就是四轮都卖。
        轮次未知（部分渠道不返回该字段）时按"非全天"处理，避免误导。
        """
        return len(self.rounds) >= TOTAL_ROUNDS

    @property
    def rounds_text(self) -> str:
        if not self.rounds:
            return ""
        if self.is_all_day:
            return "全天在售"
        return "第 " + "、".join(str(r) for r in sorted(self.rounds)) + " 轮"

    @property
    def price_text(self) -> str:
        """中文数字价格，如 三百二十万。"""
        if self.price is None:
            return "价格未知"
        return format_price_chinese(self.price)

    @property
    def price_raw_text(self) -> str:
        """阿拉伯数字价格（千分位）。"""
        if self.price is None:
            return ""
        return f"{self.price:,} 洛克贝"

    @property
    def limit_text(self) -> str:
        return f"限购 {self.limit}" if self.limit else "不限购"

    @property
    def signature(self) -> str:
        """用于去重/比对的指纹。"""
        return f"{self.name}|{self.price}|{self.limit}"


@dataclass
class MerchantSnapshot:
    """一次查询得到的完整货架状态。"""

    items: list[MerchantItem] = field(default_factory=list)
    source_id: str = ""
    source_label: str = ""
    status: str = "unknown"              # open / closed / unknown
    round_index: Optional[int] = None
    started_at: Optional[str] = None
    next_refresh: Optional[str] = None
    position: Optional[str] = None
    fetched_at: datetime = field(default_factory=datetime.now)
    stale: bool = False
    message: str = ""
    route: dict = field(default_factory=dict)   # 直连路由信息（IP / 是否大陆 / 延迟）
    expected_round: Optional[int] = None        # 按本地时间应当处于第几轮

    @property
    def is_round_lagging(self) -> bool:
        """接口返回的还是上一轮的数据（换货后上游缓存未刷新）。

        这是"换货后新商品不出现、旧的全天商品一直在"的直接原因：
        本地已进入第 2 轮，接口还在给第 1 轮的货架。
        """
        if not self.round_index or not self.expected_round:
            return False
        return self.round_index < self.expected_round

    @property
    def all_day_count(self) -> int:
        return sum(1 for item in self.items if item.is_all_day)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def signature(self) -> str:
        """整份快照的指纹，供悬浮窗等组件判断"是否真的变了"。"""
        parts = [self.status, str(self.round_index), str(self.next_refresh)]
        parts.extend(item.signature for item in self.items)
        return "|".join(parts)

    @property
    def special_items(self) -> list[MerchantItem]:
        return [i for i in self.items if i.rarity.level >= 4]

    def above_level(self, min_level: int) -> list[MerchantItem]:
        return [i for i in self.items if i.rarity.level >= min_level]
