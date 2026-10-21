"""商品与快照的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from numerals import format_price_chinese
from rarity import Rarity


@dataclass
class MerchantItem:
    """一件上架商品。"""

    name: str
    price: Optional[int] = None          # 单价（洛克贝），未知为 None
    limit: Optional[int] = None          # 限购数量
    icon_url: str = ""                   # 图标地址
    category: str = ""                   # 分类，如"炼金材料"
    description: str = ""
    rarity: Rarity = field(default_factory=lambda: Rarity(1, "普通", "#8b98ab"))

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
