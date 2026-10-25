"""视图控制器（MVP 的 P）：取数编排、换货校验、落盘与通知决策。

为什么从 MerchantApp 拆出来
---------------------------
拆分前 `app_window.MerchantApp` 同时是"窗口视图"和"应用编排器"：
线程池、结果队列、新鲜度重试、state.json 读写、通知去重全挤在同一个
1400 行的类里，UI 重写时这些逻辑会被反复搬动。

拆分后的边界：
* 本模块**不创建任何 Tk 控件**；对 Tk 的唯一依赖是 `root.after/after_cancel`
  （重试排程），因此可独立测试数据生命周期；
* 视图（MerchantApp）保留全部控件与渲染，通过下述"视图协议"被本类回调。

视图协议（MerchantApp 必须提供）
--------------------------------
* `_fetch_reset_ui()`                            查询结束：复位刷新按钮
* `_present_snapshot(snapshot, errors, reason)`  渲染货架 + 状态栏来源信息
* `_on_fetch_failed(message, reason)`            查询失败的界面表现
* `_show_recheck_pending(snapshot, reason, decision)`   重取已排程的提示
* `_show_recheck_give_up(snapshot, reason, decision, quiet)`  重取放弃的提示
* `_end_floating_refresh()`                      复位悬浮窗 ⟳ 按钮
* `is_closing`（属性）                           退出中标志，重试链要停

兼容性说明：`selftest` 会在**构造前**替换 `ui.app_window.fetch_snapshot`
与 `MerchantApp.ROUND_RETRY_DELAYS`，所以刷新按钮旁的"提交取数"仍留在
视图层（用模块全局名查 `fetch_snapshot`），本类只接收结果。
"""
from __future__ import annotations

import json
import queue
from concurrent.futures import ThreadPoolExecutor

from roco import applog
from roco.net import catalog
from roco.core import history
from roco.core.freshness import (ACTION_NONE, CODE_ROUND_LAG, ShelfObservation,
                                 ShelfWatcher)
from roco.core.models import MerchantSnapshot
from roco.net.providers import ProviderError
from roco.config import state_path


class AppController:
    """持有全部"数据状态"：快照、加载中、重试计数、货架指纹、通知去重。"""

    # 重试链专用标记：带这个原因触发的刷新不清空重试计数
    RETRY_REASON = "换货校验重试"

    def __init__(self, root, config, notifier, history_dir, round_delays, view) -> None:
        self.root = root
        self.config = config
        self.notifier = notifier
        self.view = view
        self.history_dir = history_dir

        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fetch")
        self.results: queue.Queue = queue.Queue()
        # 货架新鲜度判定：记住上一次的结果，判断这次是不是"其实没换货"
        self.watcher = ShelfWatcher(
            round_delays=round_delays,
            delay=config.recheck_delay_seconds,
            max_attempts=config.recheck_max_attempts,
            give_up_policy=config.recheck_give_up_policy,
            slow_interval=config.recheck_slow_interval_seconds,
            slow_max_attempts=config.recheck_slow_max_attempts,
        )

        # ---- 数据状态（视图通过 MerchantApp 上的同名属性转发访问） ----
        self.loading = False
        self.round_retry = 0
        self.retry_job: str | None = None
        self.snapshot: MerchantSnapshot | None = None
        self.error_text = ""
        self.shelf_state_key: tuple[str, int | None] | None = None
        self._restore_shelf_state()

    # ------------------------------------------------------------- 配置更新
    def update_config(self, config) -> None:
        """设置面板保存后同步引用（视图的 self.config 已换成新对象）。"""
        self.config = config

    def set_history_dir(self, history_dir) -> None:
        self.history_dir = history_dir

    # ------------------------------------------------------------- 刷新链路
    def begin_refresh(self, reason: str) -> bool:
        """刷新前置状态处理；已在查询中返回 False（视图据此跳过）。

        用户主动刷新 / 定时查询会盖掉还没触发的那次重取，并重新给足预算。
        """
        if self.loading:
            return False
        if reason != self.RETRY_REASON:
            self.cancel_retry()
            self.round_retry = 0
            self.watcher.reset_attempts()
        self.loading = True
        return True

    def handle_result(self, future, reason: str) -> None:
        """后台取数结果落地：状态、日志、历史、渲染、校验、通知一条链。

        只在主线程被调用（视图的结果泵），因此可以安全回调视图方法。
        """
        self.loading = False
        self.view._fetch_reset_ui()
        try:
            snapshot, errors = future.result()
        except ProviderError as exc:
            self._handle_failure(str(exc), reason)
            return
        except Exception as exc:  # noqa: BLE001
            self._handle_failure(f"{type(exc).__name__}: {exc}", reason)
            return

        self.snapshot = snapshot
        self.error_text = ""
        route = snapshot.route or {}
        egress = route.get("egress") or {}
        lag = ""
        if snapshot.is_round_lagging:
            lag = f"·滞后(应第{snapshot.expected_round}轮)"
        dropped = getattr(snapshot, "dropped_count", 0) or 0
        if dropped:
            lag += f"·剔除旧轮 {dropped} 件"
        applog.info("查询成功 | %s | 第%s轮%s | %s 件(全天 %s) | 更新于 %s | 目标 %s(%s) 出口 %s %s",
                    snapshot.source_label, snapshot.round_index, lag,
                    len(snapshot.items), snapshot.all_day_count,
                    snapshot.fetched_at.strftime("%H:%M:%S"),
                    route.get("ip", "?"),
                    "大陆" if route.get("is_cn") else "境外",
                    egress.get("ip", "?"), egress.get("country", "?"))
        if egress.get("country") and egress.get("country") != "CN":
            applog.warning("出口不在中国大陆（%s/%s），流量已由代理接管；"
                           "建议改用大陆反代渠道或在路由器侧加 DIRECT 规则",
                           egress.get("country"), egress.get("colo", "?"))
        self.record_history(snapshot)
        self.view._present_snapshot(snapshot, errors, reason)
        self.evaluate_freshness(snapshot, reason)
        self._remember_shelf(snapshot)
        self.maybe_notify(snapshot)
        self.view._end_floating_refresh()

    def _handle_failure(self, message: str, reason: str) -> None:
        applog.error("查询失败 | %s | %s", reason, message.replace("\n", " / "))
        self.error_text = message
        self.view._on_fetch_failed(message, reason)

    # ------------------------------------------------------------- 换货校验
    def cancel_retry(self) -> None:
        if self.retry_job is not None:
            try:
                self.root.after_cancel(self.retry_job)
            except Exception:  # noqa: BLE001 - 窗口已销毁时 after_cancel 可能抛
                pass
            self.retry_job = None

    def _schedule_recheck(self, delay: float) -> None:
        """排一次重取；同一时刻只允许有一条重取链在跑。"""
        self.cancel_retry()
        try:
            self.retry_job = self.root.after(
                max(50, int(max(0.05, delay) * 1000)), self._run_recheck
            )
        except Exception:  # noqa: BLE001
            self.retry_job = None

    def _run_recheck(self) -> None:
        self.retry_job = None
        if self.view.is_closing:
            return
        if self.loading:
            # 正有一次查询在路上（用户点了刷新、或定时任务刚触发）：
            # 等它结束再来，且不消耗重取次数——否则重取链会悄无声息地断掉
            self._schedule_recheck(min(15.0, max(5.0, self.watcher.delay)))
            return
        self.view.refresh(self.RETRY_REASON)

    def evaluate_freshness(self, snapshot: MerchantSnapshot, reason: str) -> None:
        """这次的货架到底新不新鲜？不新鲜就按可配置的节奏重取。

        1.4.1 只看"接口轮次是否落后"，漏掉了两种更难发现的"假刷新"：
        `round` 已刷成新轮次但货架内容还是旧的；以及接口根本不返回 `round`、
        只能拿本地时间兜底的渠道。这两种情况下 `is_round_lagging` 恒为 False，
        程序不重试；再叠加 `filter_by_round()` 把旧轮次的商品剔掉，界面就
        只剩「全天在售」的那两件，之后要等下一个整点才再查——这就是
        "卡在两个全天售卖物品上、刷不出新物品"的成因。

        现在补上"内容比对"这条判据（见 freshness.ShelfWatcher）：货架只剩
        全天商品、且与上次完全相同时，按 `recheck_delay_seconds` 延后重取，
        最多 `recheck_max_attempts` 次。判据只在"只剩全天商品"时生效，
        带本轮商品的正常货架仍只受老逻辑约束。
        """
        self.cancel_retry()

        if not self.config.auto_recheck_after_refresh:
            self.round_retry = 0
            return

        decision = self.watcher.observe(snapshot)
        self.round_retry = decision.attempt if decision.action != ACTION_NONE else 0

        if decision.action == ACTION_NONE:
            if decision.code and decision.limit > 0:
                # 同一份内容这一轮已经放弃过：不再请求，但界面继续标注"可能没更新"，
                # 否则下一次渲染会把警告悄悄抹掉，看着像数据已经更新了
                self.view._show_recheck_give_up(snapshot, reason, decision, quiet=True)
                return
            if reason == self.RETRY_REASON:
                applog.info("换货校验结束 | 第 %s 轮 | %s 件，数据可信",
                            snapshot.round_index, len(snapshot.items))
            return

        if decision.is_give_up:
            self.view._show_recheck_give_up(snapshot, reason, decision)
            return

        self.view._show_recheck_pending(snapshot, reason, decision)
        applog.info("换货校验 | %s | 第 %s 轮 | %s 件 | %s 秒后第 %s/%s 次重取%s",
                    decision.code_text, snapshot.round_index, len(snapshot.items),
                    int(decision.delay), decision.attempt, decision.limit,
                    f"（慢速 {decision.slow_round}/{decision.slow_limit or '不限'}）"
                    if decision.slow else "")
        self._schedule_recheck(decision.delay)

    # ------------------------------------------------------------- 历史与清单
    def record_history(self, snapshot: MerchantSnapshot) -> None:
        """查询成功后落一条历史，并把本轮见过的商品并入通知清单。

        历史被关掉时只是**不记录**；关注物品清单照旧累积，否则用户重新打开历史
        会发现"这段时间见过的东西都没了"。两者是独立功能。
        """
        try:
            if self.config.history_enabled:
                history.record(snapshot, path=history.history_path(self.history_dir))
            catalog.observe(snapshot)
        except Exception as exc:      # noqa: BLE001 - 记录失败不该影响查询结果
            applog.warning("历史记录失败 | %s", exc)

    # ------------------------------------------------------------- 通知决策
    def maybe_notify(self, snapshot: MerchantSnapshot) -> None:
        """品质阈值 ∪ 关注物品；同一轮同一物品只提醒一次（state.json 去重）。"""
        from roco.platform.notifier import select_targets

        if not self.config.notify_enabled:
            return
        # 两条规则并行：品质达标照旧提醒；关注清单里的物品不看品质
        specials = select_targets(snapshot, self.config.notify_min_level,
                                  self.config.notify_items)
        if not specials:
            return

        round_key = f"{snapshot.started_at or ''}|{snapshot.round_index or 0}"
        signatures = [item.signature for item in specials]
        previous = self._load_state()

        if previous.get("round_key") == round_key:
            already = set(previous.get("signatures") or [])
            fresh = [item for item in specials if item.signature not in already]
        else:
            fresh = specials

        if not fresh:
            return
        self.notifier.alert_special(fresh)
        self._save_state({"round_key": round_key, "signatures": signatures})

    # ------------------------------------------------------------- state.json
    def _load_state(self) -> dict:
        try:
            return json.loads(state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, data: dict) -> None:
        """合并写回，避免一处写入把另一处的状态（通知去重 / 货架指纹）冲掉。"""
        merged = self._load_state()
        merged.update(data)
        try:
            state_path().write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _restore_shelf_state(self) -> None:
        """读回上次运行留下的货架指纹，冷启动也能立刻判断"这次是不是没换货"。"""
        shelf = (self._load_state() or {}).get("shelf")
        observation = ShelfObservation.from_dict(shelf)
        if observation is not None:
            self.watcher.seed(observation)
            self.shelf_state_key = (observation.content, observation.round_key)

    def _remember_shelf(self, snapshot: MerchantSnapshot) -> None:
        """把这一轮的货架指纹写盘；内容没变化就不重复写。"""
        try:
            observation = ShelfObservation.of(snapshot)
        except Exception:  # noqa: BLE001 - 落盘失败不该影响界面
            return
        if observation.count <= 0:
            return
        key = (observation.content, observation.round_key)
        if key == self.shelf_state_key:
            return
        self.shelf_state_key = key
        self._save_state({"shelf": observation.to_dict()})
