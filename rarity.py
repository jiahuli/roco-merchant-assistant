"""珍惜度（稀有度）判定。

官方接口没有直接给出"珍惜度"字段，这里用三层规则推断，优先级从高到低：
1. 关键词：按道具名的稀缺程度人工归档（棱镜球、国王球这类硬通货优先级最高）；
2. 分类：按 category 归类（精灵蛋 / 捕捉道具 / 材料 …）；
3. 价格：用单价兜底，越贵越可能稀有。
最终取"关键词/分类"与"价格"两者的较高值，避免贵的普通材料被低估。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Rarity:
    level: int      # 1=普通 2=优良 3=稀有 4=史诗 5=传说
    label: str
    color: str      # 卡片主色 / 徽章底色

    @property
    def is_special(self) -> bool:
        return self.level >= 4


RARITY_TABLE: dict[int, Rarity] = {
    1: Rarity(1, "普通", "#8b98ab"),
    2: Rarity(2, "优良", "#3ddc97"),
    3: Rarity(3, "稀有", "#4f9bff"),
    4: Rarity(4, "史诗", "#c97bff"),
    5: Rarity(5, "传说", "#ff8a3d"),
}

# 关键词规则：越靠前越优先匹配（长词在前，避免"蛋"抢先吃掉"炫彩精灵蛋"）
_KEYWORD_RULES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (5, ("棱镜", "国王球", "炫彩", "传说", "至宝")),
    (4, ("首领血脉", "首领秘药", "秘药", "大师球", "异色", "血脉", "限定", "纪念")),
    (3, ("神奇的蛋", "精灵蛋", "高级咕噜球", "体力药剂", "技能石", "洗练", "天赋")),
    (2, ("咕噜球", "果", "药", "结晶", "琉璃", "碧玺", "花", "草", "菇", "耳", "矿", "晶")),
)

# 分类规则
_CATEGORY_RULES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (5, ("传说", "至宝")),
    (4, ("精灵蛋", "秘药", "血脉")),
    (3, ("捕捉", "球类", "药剂")),
    (2, ("喂养", "材料", "炼金", "养成")),
)

# 价格兜底区间（洛克贝）
_PRICE_RULES: tuple[tuple[int, int], ...] = (
    (1_000_000, 5),
    (300_000, 4),
    (50_000, 3),
    (8_000, 2),
)


def _match_keyword(name: str) -> Optional[int]:
    for level, words in _KEYWORD_RULES:
        for word in words:
            if word in name:
                return level
    return None


def _match_category(category: str) -> Optional[int]:
    if not category:
        return None
    for level, words in _CATEGORY_RULES:
        for word in words:
            if word in category:
                return level
    return None


def _match_price(price: Optional[int]) -> Optional[int]:
    if not price:
        return None
    for threshold, level in _PRICE_RULES:
        if price >= threshold:
            return level
    return 1


def infer_rarity(name: str, price: Optional[int] = None, category: str = "") -> Rarity:
    """推断珍惜度。"""
    name = (name or "").strip()
    name_level = _match_keyword(name)
    if name_level is None:
        name_level = _match_category(category or "")
    price_level = _match_price(price)

    level = max(x for x in (name_level, price_level, 1) if x is not None)
    return RARITY_TABLE.get(level, RARITY_TABLE[1])


def rarity_by_level(level: int) -> Rarity:
    return RARITY_TABLE.get(max(1, min(5, int(level))), RARITY_TABLE[1])


RARITY_CHOICES: tuple[str, ...] = tuple(r.label for r in RARITY_TABLE.values())


def level_from_label(label: str) -> int:
    for level, rarity in RARITY_TABLE.items():
        if rarity.label == label:
            return level
    return 4
