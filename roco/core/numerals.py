"""阿拉伯数字 -> 中文数字（读数式大写），例如 3200000 -> 三百二十万。

用于把商品价格转换成玩家读起来顺口的中文数字，而不是展示一长串 0。
"""
from __future__ import annotations

_DIGITS = "零一二三四五六七八九"
_UNITS = ("", "十", "百", "千")
_SECTIONS = ("", "万", "亿", "万亿", "亿亿")


def _section_to_cn(num: int) -> str:
    """转换 1..9999 的四位段，例如 1205 -> 一千二百零五。"""
    if num <= 0:
        return ""
    text = ""
    digits = str(num)
    length = len(digits)
    pending_zero = False
    for index, char in enumerate(digits):
        digit = int(char)
        position = length - 1 - index
        if digit == 0:
            # 记录"这里跳过了 0"，等下一个非 0 数字出现时补一个"零"
            pending_zero = bool(text)
        else:
            if pending_zero:
                text += "零"
                pending_zero = False
            text += _DIGITS[digit] + _UNITS[position]
    return text


def to_chinese_number(value: int | float | None) -> str:
    """把非负整数转成中文读法；负数与小数做温和处理。"""
    if value is None:
        return "—"
    try:
        num = int(value)
    except (TypeError, ValueError):
        return "—"
    if num == 0:
        return "零"
    if num < 0:
        return "负" + to_chinese_number(-num)

    # 拆成 4 位一段，从低位开始
    sections: list[int] = []
    rest = num
    while rest > 0:
        sections.append(rest % 10000)
        rest //= 10000
    if len(sections) > len(_SECTIONS):
        return f"{num:,}"  # 超出支持范围（>10^20）时退回阿拉伯数字

    parts: list[str] = []
    need_zero_bridge = False
    for index in range(len(sections) - 1, -1, -1):
        section = sections[index]
        if section == 0:
            # 整段为 0：若后面还有有效数字，需要一个"零"来连接
            if parts:
                need_zero_bridge = True
            continue
        chunk = _section_to_cn(section)
        if parts and (need_zero_bridge or section < 1000):
            parts.append("零")
        parts.append(chunk + _SECTIONS[index])
        need_zero_bridge = False
    text = "".join(parts)
    # "一十万" 这种读法不自然，统一成 "十万"
    if text.startswith("一十"):
        text = text[1:]
    return text


def format_price_chinese(value: int | float | None) -> str:
    """价格展示用：中文数字 + 兜底。"""
    text = to_chinese_number(value)
    return text
