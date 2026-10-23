"""货架"新鲜度"判定：这次拿到的到底是不是换货之后的新货架。

为什么单独一层
--------------
1.4.1 只在 `round_index < expected_round`（接口自报轮次落后）时才重试。
但上游还有两种更难发现的"假刷新"：

1. `round` 字段已经刷成新轮次，货架内容却还是上一轮的；
2. 接口根本不返回 `round`（精简渠道），代码只能用本地时间兜底，
   算出来的轮次永远"不落后"。

这两种情况下 `is_round_lagging` 恒为 False，程序认为数据正常、不重试；
再叠加 `filter_by_round()` 把不属于本轮的旧商品剔掉，界面上就只剩下
「全天在售」的那几件（条件球 / 棱镜球）——这就是"卡在两件全天商品上、
刷不出新物品"的成因。

因此判定不能只看轮次，还要看**货架内容本身**：

* `ALL_DAY_ONLY`  本次只剩全天商品，且与上一次完全相同 → 上游没换货；
* `SHELF_SHRANK`  本次只剩全天商品，上次还有本轮商品 → 本轮商品被
                  `filter_by_round()` 剔掉了（轮次判断有偏）；
* `ROUND_LAG`     接口自报轮次落后（1.4.1 的老判据，保留）。

三条判据都要求"货架只剩全天商品"或"轮次明确落后"才成立，所以正常的
换货（带本轮商品）不会被误判，也就不会扰动既有刷新路径。

判定过程是纯状态机，不碰 Tk / 网络，可以直接单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

STATUS_OPEN = "open"

# 结论
ACTION_NONE = "none"          # 数据可信，什么都不用做
ACTION_RETRY = "retry"        # 延迟后重取
ACTION_GIVE_UP = "give_up"    # 重取次数用尽，保留当前数据并提示用户

# 判据
CODE_ROUND_LAG = "round_lag"
CODE_ALL_DAY_ONLY = "all_day_only"
CODE_SHELF_SHRANK = "shelf_shrank"

CODE_TEXT = {
    CODE_ROUND_LAG: "接口轮次落后",
    CODE_ALL_DAY_ONLY: "货架与上次完全相同",
    CODE_SHELF_SHRANK: "本轮商品消失",
}

# 默认：换货后接口缓存没跟上时，依次等这些秒数再查（合计约 10 分钟）
DEFAULT_ROUND_DELAYS: tuple[float, ...] = (30.0, 60.0, 120.0, 180.0, 240.0)
DEFAULT_DELAY = 300.0          # 内容未变时的默认延后：5 分钟
DEFAULT_MAX_ATTEMPTS = 3       # 内容未变时最多重取几次


def content_signature(items: Iterable) -> str:
    """货架内容指纹：与顺序、轮次、刷新时间无关，只反映"卖了什么"。

    商品顺序偶尔会变、`next_refresh` 也会随接口漂移，这些都不该影响
    "内容是否变过"的判断，所以排序后再拼。
    """
    return "|".join(sorted(str(getattr(item, "signature", item)) for item in items))


@dataclass(frozen=True)
class ShelfObservation:
    """一次查询看到的货架状态（只留判定需要的字段）。"""

    content: str
    count: int
    all_day_only: bool
    round_index: Optional[int] = None
    expected_round: Optional[int] = None
    source_id: str = ""

    @classmethod
    def of(cls, snapshot) -> "ShelfObservation":
        items = list(getattr(snapshot, "items", []) or [])
        return cls(
            content=content_signature(items),
            count=len(items),
            # 只有"每件都是全天在售"才算全天货架；轮次未知的渠道一律不算，
            # 否则没有 rounds 字段的接口会被误判成"只剩全天商品"
            all_day_only=bool(items) and all(bool(getattr(i, "is_all_day", False)) for i in items),
            round_index=getattr(snapshot, "round_index", None),
            expected_round=getattr(snapshot, "expected_round", None),
            source_id=str(getattr(snapshot, "source_id", "") or ""),
        )

    @property
    def round_key(self) -> Optional[int]:
        """去重用的轮次键：优先用"应该在第几轮"，没有就用接口自报的。"""
        return self.expected_round or self.round_index

    def to_dict(self) -> dict:
        """落盘用：重启后还能拿上一次的货架做比对。"""
        return {
            "content": self.content,
            "count": self.count,
            "all_day_only": self.all_day_only,
            "round_index": self.round_index,
            "expected_round": self.expected_round,
            "source_id": self.source_id,
        }

    @classmethod
    def from_dict(cls, raw) -> Optional["ShelfObservation"]:
        """从 state.json 还原；字段缺失或类型不对就当作没有历史（不抛异常）。"""
        if not isinstance(raw, dict):
            return None
        content = raw.get("content")
        if not isinstance(content, str) or not content:
            return None
        try:
            count = int(raw.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return None

        def _int_or_none(key: str) -> Optional[int]:
            value = raw.get(key)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            content=content,
            count=count,
            all_day_only=bool(raw.get("all_day_only")),
            round_index=_int_or_none("round_index"),
            expected_round=_int_or_none("expected_round"),
            source_id=str(raw.get("source_id") or ""),
        )


@dataclass(frozen=True)
class Recheck:
    """一次判定给出的下一步动作。"""

    action: str = ACTION_NONE
    code: str = ""
    delay: float = 0.0
    attempt: int = 0            # 已重取次数（不含本次）
    limit: int = 0              # 允许的最大重取次数

    @property
    def should_retry(self) -> bool:
        return self.action == ACTION_RETRY

    @property
    def is_give_up(self) -> bool:
        return self.action == ACTION_GIVE_UP

    @property
    def code_text(self) -> str:
        return CODE_TEXT.get(self.code, "")


class ShelfWatcher:
    """记住上一次看到的货架，判断本次是否"其实没换货"。

    用法：每次查询成功后调用一次 `observe(snapshot)`，按返回的 `Recheck`
    决定是否排程重取。用户主动刷新 / 定时查询时调用 `reset_attempts()`，
    让每次换货都有完整的重取预算。

    反骚扰：同一份内容 + 同一轮次已经放弃过一次，就不再重复重取
    （`_exhausted`），跨轮或内容变化后自动恢复。
    """

    def __init__(
        self,
        round_delays: Iterable[float] = DEFAULT_ROUND_DELAYS,
        delay: float = DEFAULT_DELAY,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._round_delays: tuple[float, ...] = (30.0,)
        self._delay: float = DEFAULT_DELAY
        self._max_attempts: int = DEFAULT_MAX_ATTEMPTS
        self._attempt = 0
        self._last: Optional[ShelfObservation] = None
        self._exhausted: Optional[tuple[str, Optional[int]]] = None
        self.configure(delay=delay, max_attempts=max_attempts, round_delays=round_delays)

    # ---------------------------------------------------------------- 配置
    def configure(
        self,
        delay: float | None = None,
        max_attempts: int | None = None,
        round_delays: Iterable[float] | None = None,
    ) -> None:
        """热更新节奏；非法值一律夹到可用区间，绝不抛异常打断查询。"""
        if round_delays is not None:
            cleaned = []
            for raw in round_delays:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    cleaned.append(value)
            self._round_delays = tuple(cleaned) or (30.0,)
        if delay is not None:
            try:
                value = float(delay)
            except (TypeError, ValueError):
                value = DEFAULT_DELAY
            # 下限只防"0 秒风暴"，业务下限（30 秒）由 Config / 设置界面把关
            self._delay = max(0.05, min(3600.0, value))
        if max_attempts is not None:
            try:
                value = int(max_attempts)
            except (TypeError, ValueError):
                value = DEFAULT_MAX_ATTEMPTS
            self._max_attempts = max(0, min(10, value))

    @property
    def delay(self) -> float:
        return self._delay

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def round_delays(self) -> tuple[float, ...]:
        return self._round_delays

    @property
    def attempt(self) -> int:
        return self._attempt

    def reset_attempts(self) -> None:
        """新一轮换货：清空重取计数，但保留"上一次内容"用于比对。"""
        self._attempt = 0

    def seed(self, observation: Optional[ShelfObservation]) -> None:
        """用上次运行留下的货架当历史，冷启动也能立刻发现"没换货"。"""
        if observation is not None:
            self._last = observation
            self._attempt = 0

    def forget(self) -> None:
        """彻底忘掉历史（渠道切换 / 退出）。"""
        self._attempt = 0
        self._last = None
        self._exhausted = None

    # ---------------------------------------------------------------- 判定
    def observe(self, snapshot) -> Recheck:
        obs = ShelfObservation.of(snapshot)
        previous, self._last = self._last, obs

        # 只有"营业中且有商品"才谈得上新鲜不新鲜
        if str(getattr(snapshot, "status", "")) != STATUS_OPEN or not obs.count:
            self._attempt = 0
            return Recheck()

        same_source = previous is not None and previous.source_id == obs.source_id
        identical = same_source and previous.content == obs.content
        # 内容变了（或换了渠道）就是新证据，重取预算重新给
        if not identical:
            self._attempt = 0

        if not obs.all_day_only:
            # 带本轮商品的货架：只认"轮次落后"这一条老判据，新启发式不介入
            self._exhausted = None

        lagging = bool(getattr(snapshot, "is_round_lagging", False))
        shrank = same_source and not previous.all_day_only and previous.count > obs.count
        if lagging:
            code, limit = CODE_ROUND_LAG, len(self._round_delays)
        elif obs.all_day_only and identical:
            code, limit = CODE_ALL_DAY_ONLY, self._max_attempts
        elif obs.all_day_only and shrank:
            # 上次还有本轮商品，这次只剩全天商品：多半是旧轮次被过滤掉了
            code, limit = CODE_SHELF_SHRANK, self._max_attempts
        else:
            self._attempt = 0
            return Recheck()

        if limit <= 0:
            # 次数配成 0 = 关闭这条启发式（轮次判据仍有各自的上限）
            self._attempt = 0
            return Recheck(code=code, limit=limit)

        if code != CODE_ROUND_LAG and self._exhausted == (obs.content, obs.round_key):
            # 同一内容、同一轮次已经放弃过，别反复骚扰接口
            self._attempt = 0
            return Recheck(code=code, limit=limit)

        self._attempt += 1
        if self._attempt > limit:
            if code != CODE_ROUND_LAG:
                self._exhausted = (obs.content, obs.round_key)
            return Recheck(
                action=ACTION_GIVE_UP, code=code,
                delay=0.0, attempt=self._attempt, limit=limit,
            )

        if code == CODE_ROUND_LAG:
            delay = self._round_delays[min(self._attempt - 1, len(self._round_delays) - 1)]
        else:
            delay = self._delay
        return Recheck(
            action=ACTION_RETRY, code=code,
            delay=delay, attempt=self._attempt, limit=limit,
        )


def format_delay(seconds: float) -> str:
    """把秒数说成人话：45 秒 / 5 分钟 / 1 分 30 秒。"""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "稍"
    if total < 60:
        return f"{max(1, total)} 秒"
    minutes, rest = divmod(total, 60)
    if not rest:
        return f"{minutes} 分钟"
    return f"{minutes} 分 {rest} 秒"
