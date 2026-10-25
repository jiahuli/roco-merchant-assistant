"""功能自测：核心逻辑 + 关键交互。"""
import importlib
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk

from contextlib import contextmanager


@contextmanager
def frozen_clock(hour: int = 20, minute: int = 30):
    """把"现在"钉在营业时段内，避免自测结果随时间漂移。

    一批渲染 / 货架 / 换货校验用例隐含假设"商人正在营业"（入口是
    `is_open_at(datetime.now(CN_TZ))`）。原来在 00:00–08:00 跑，这批用例
    会全线失败（"初始两件都在在售区 → 0/0"），看着像回归，实际是打烊。

    这里只替换相关模块里的 `datetime`，**不改任何断言**；真正的打烊逻辑
    由 `test_midnight_shelf`、`test_render_states` 用显式快照覆盖，不受影响。
    """
    import datetime as _dt

    fixed = _dt.datetime(2026, 10, 25, hour, minute, 0)

    class _Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.replace(tzinfo=tz) if tz is not None else fixed

        @classmethod
        def utcnow(cls):
            return fixed

    patched = []
    for name in ("roco.ui.app_window", "roco.core.rounds", "roco.core.freshness",
                 "roco.net.providers", "roco.net.parsing", "roco.core.history",
                 "roco.core.purchases", "roco.ui.history_panel"):
        try:
            module = importlib.import_module(name)
        except Exception:      # noqa: BLE001 - 缺模块不该让自测起不来
            continue
        if hasattr(module, "datetime"):
            patched.append((module, module.datetime))
            module.datetime = _Frozen
    try:
        yield
    finally:
        for module, original in patched:
            module.datetime = original

from roco.platform import autostart
from roco.config import Config, config_path, sanitize_times, state_path
from roco.core.models import MerchantItem, MerchantSnapshot
from roco.core.numerals import to_chinese_number
from roco.net.providers import ProviderError, build_provider, fetch_snapshot
from roco.core.rounds import next_refresh_at, round_index_at, is_open_at
from roco.core.rarity import infer_rarity
from roco.services.scheduler import Scheduler, format_countdown

PASS, FAIL = "PASS", "FAIL"


@contextmanager
def preserve_app_files():
    """自测会实例化真实窗口，而窗口退出时会写 config/state。

    这里先备份这两个文件，跑完原样还原——否则测试改过的开关
    （比如 close_to_tray=false）会留在用户真实配置里，看起来很像是程序 bug。
    """
    backups = {}
    # 新增的 history / purchases / items 三个文件同样是"用户真实数据"，
    # 测试会往里写历史与勾选，不备份就会污染用户目录。
    extra = [config_path().parent / name
             for name in ("history.json", "purchases.json", "items.json")]
    for path in (config_path(), state_path(), *extra):
        try:
            backups[path] = path.read_bytes() if path.exists() else None
        except OSError:
            backups[path] = None

    # 注册表里的开机自启也要还原：测试一旦触发保存逻辑就可能写进去
    autostart_before = autostart.current_command() if autostart.is_supported() else ""

    try:
        yield
    finally:
        for path, data in backups.items():
            try:
                if data is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_bytes(data)
            except OSError:
                pass
        if autostart.is_supported():
            try:
                if autostart_before:
                    autostart.enable()
                else:
                    autostart.disable()
            except Exception:
                pass
results = []


def check(name, condition, detail=""):
    results.append((PASS if condition else FAIL, name, detail))


def test_numerals():
    cases = {
        3200000: "三百二十万", 1000: "一千", 36000: "三万六千", 6000: "六千",
        1000000: "一百万", 10005: "一万零五", 100000000: "一亿", 0: "零", 10: "十",
    }
    bad = {k: (to_chinese_number(k), v) for k, v in cases.items() if to_chinese_number(k) != v}
    check("中文数字转换", not bad, str(bad))


def test_rarity():
    check("棱镜球=传说", infer_rarity("棱镜球", 3200000).level == 5)
    check("国王球=传说", infer_rarity("国王球", 800000).level == 5)
    check("黑晶琉璃=优良", infer_rarity("黑晶琉璃", 1000, "炼金材料").level == 2)
    check("神奇的蛋=稀有", infer_rarity("神奇的蛋", 36000, "精灵蛋").level == 3)
    check("高价兜底", infer_rarity("未知道具", 1500000).level == 5)


def test_time_parsing():
    check("时间清洗", sanitize_times(["8:05", " 12:05 ", "25:00", "abc"]) == ["08:05", "12:05"])
    check("时间排序去重", sanitize_times(["20:05", "08:05", "08:05"]) == ["08:05", "20:05"])


def test_rounds():
    from datetime import datetime
    from roco.core.rounds import CN_TZ
    moment = datetime(2026, 10, 20, 13, 30, tzinfo=CN_TZ)
    check("轮次计算", round_index_at(moment) == 2, str(round_index_at(moment)))
    check("下次刷新", next_refresh_at(moment).hour == 16, str(next_refresh_at(moment)))
    check("营业时间", is_open_at(moment) is True)
    check("打烊时间", is_open_at(moment.replace(hour=3)) is False)


def test_scheduler():
    root = tk.Tk()
    root.withdraw()
    fired = []
    sched = Scheduler(root, ["08:05", "12:05"], on_fire=lambda: fired.append(1))
    from datetime import datetime
    nxt = sched.next_run(datetime(2026, 10, 20, 13, 0))
    check("下次执行时间", nxt.strftime("%H:%M") == "08:05" and nxt.day == 21, str(nxt))
    check("倒计时格式", len(format_countdown(nxt, datetime(2026, 10, 20, 13, 0))) == 8)

    # 直接驱动一次节拍，确认到点会触发
    sched.times = [datetime.now().strftime("%H:%M")]
    sched._running = True
    sched._tick()
    check("到点触发", len(fired) == 1, f"fired={len(fired)}")
    sched._tick()
    check("同分钟不重复触发", len(fired) == 1, f"fired={len(fired)}")
    sched.stop()
    root.destroy()


def test_provider_fallback():
    config = Config()
    config.provider = "official"          # 官方地址为空 → 应自动降级
    config.auto_fallback = True
    try:
        snapshot, errors = fetch_snapshot(config)
        ok = snapshot is not None and snapshot.source_id in ("onebiji", "yxsr")
        check("官方未配置时自动降级", ok, f"来源={snapshot.source_id} 错误={errors[:1]}")
        check("降级留痕", bool(errors), str(errors[:1]))
    except ProviderError as exc:
        check("官方未配置时自动降级", False, str(exc))


def test_provider_bad_url():
    config = Config()
    config.provider = "custom"
    config.auto_fallback = False
    config.request_timeout = 5
    config.providers["custom"]["base_url"] = "https://127.0.0.1:9/none.json"
    try:
        fetch_snapshot(config)
        check("坏地址应抛错", False, "居然成功了")
    except ProviderError as exc:
        check("坏地址应抛错", True, str(exc).splitlines()[0][:50])


def test_notification_dedupe():
    from roco.ui.app_window import MerchantApp
    root = tk.Tk()
    root.withdraw()
    config = Config.load()
    app = MerchantApp(root, config)

    alerts = []
    app.notifier.alert_special = lambda items: alerts.append(len(items))
    app.config.notify_enabled = True
    app.config.notify_min_level = 4

    snapshot = MerchantSnapshot(
        items=[MerchantItem("棱镜球", 3200000, 2, rarity=infer_rarity("棱镜球", 3200000))],
        source_id="test", source_label="测试", status="open", round_index=3,
        started_at="2026-10-20 16:00", next_refresh="2026-10-20 20:00",
    )
    app._maybe_notify(snapshot)
    check("首次发现特殊物品要提醒", len(alerts) == 1, f"alerts={alerts}")

    app._maybe_notify(snapshot)
    check("同轮次同商品不重复提醒", len(alerts) == 1, f"alerts={alerts}")

    snapshot.items.append(MerchantItem("国王球", 880000, 3, rarity=infer_rarity("国王球", 880000)))
    app._maybe_notify(snapshot)
    check("新增特殊物品再提醒", len(alerts) == 2 and alerts[-1] == 1, f"alerts={alerts}")

    snapshot2 = MerchantSnapshot(
        items=list(snapshot.items), status="open", round_index=4,
        started_at="2026-10-20 20:00", next_refresh="2026-10-21 00:00",
    )
    app._maybe_notify(snapshot2)
    check("换轮后重新提醒", len(alerts) == 3, f"alerts={alerts}")

    app.scheduler.stop()
    app.loader.shutdown()
    app.pool.shutdown(wait=False)
    root.destroy()


def test_render_states():
    from roco.ui.app_window import MerchantApp
    root = tk.Tk()
    root.withdraw()
    config = Config.load()
    app = MerchantApp(root, config)

    closed = MerchantSnapshot(items=[], status="closed", round_index=0,
                              next_refresh="2026-10-21 08:00")
    try:
        app._render(closed)
        check("打烊态渲染", True)
    except Exception as exc:
        check("打烊态渲染", False, f"{type(exc).__name__}: {exc}")

    open_empty = MerchantSnapshot(items=[], status="open", round_index=2)
    try:
        app._render(open_empty)
        check("空货架渲染", True)
    except Exception as exc:
        check("空货架渲染", False, f"{type(exc).__name__}: {exc}")

    from roco.services.cache import IconCache
    app.scheduler.stop()
    app.loader.shutdown()
    app.pool.shutdown(wait=False)
    root.destroy()


def test_icon_cache():
    from roco.services.cache import IconCache
    from roco.config import cache_dir
    cache = IconCache(cache_dir(), enabled=True)
    url = "https://patchwiki.biligame.com/images/rocom/thumb/b/bc/khe20j8lwkt1bg9g4b0gbnsff48g93c.png/100px-100628.png"
    first = cache.download(url)
    check("图标首次下载落盘", first is not None and first.exists())
    hits_before, _ = cache.stats()
    second = cache.download(url)
    hits_after, _ = cache.stats()
    check("二次请求走缓存", second == first and hits_after > hits_before)
    path = cache.download(url)
    check("缓存文件名稳定", path is not None and path == first)


def test_cache_path():
    """图标缓存目录：自定义 / 非法回退 / 迁移。"""
    import tempfile
    from pathlib import Path

    from roco.services.cache import IconCache, migrate_cache
    from roco.config import Config, default_cache_dir, resolve_icon_cache_dir

    config = Config()
    path, warning = resolve_icon_cache_dir(config)
    check("默认缓存目录", path == default_cache_dir() and not warning, str(path))

    with tempfile.TemporaryDirectory() as folder:
        config.icon_cache_path = folder
        path, warning = resolve_icon_cache_dir(config)
        check("自定义缓存目录生效", path == Path(folder) and not warning, str(path))

        # 迁移：旧目录放一张图，迁到新目录
        old = Path(folder) / "old"
        new = Path(folder) / "new"
        old.mkdir()
        new.mkdir()
        (old / "abc.png").write_bytes(b"\x89PNG" + b"0" * 40)
        copied, skipped = migrate_cache(old, new)
        check("缓存迁移", copied == 1 and (new / "abc.png").exists(), f"{copied}/{skipped}")

        cache = IconCache(new)
        check("迁移后可直接命中", cache.lookup("x" * 5) is None and cache.count() == 1)

    config.icon_cache_path = str(Path(default_cache_dir()) / "图标" / "缓存")
    resolved, warning = resolve_icon_cache_dir(config)
    check("中文路径可用", resolved.exists() and not warning, str(resolved))
    try:
        resolved.rmdir()
        resolved.parent.rmdir()
    except OSError:
        pass


def test_autostart():
    """开机自启：命令拼装 + 读取状态（不写注册表，避免污染系统）。"""
    from roco.platform import autostart

    command = autostart.launch_command()
    check("自启命令可拼装", bool(command) and "--autostart" in command, command[:60])
    check("自启状态可读", isinstance(autostart.is_enabled(), bool))
    enabled = autostart.is_enabled()
    ok, _msg = autostart.sync(enabled)   # 与现状对齐，不应产生新写入
    check("自启同步幂等", ok and autostart.is_enabled() == enabled)


def test_native_notify():
    """原生通知：XML 合法 + 支持判定（不发真实通知）。"""
    from xml.dom.minidom import parseString

    from roco.platform import notifier_native

    xml = notifier_native.build_xml("标题<&>", "正文 320 万")
    try:
        parseString(xml.encode("utf-8"))
        check("通知 XML 合法", True)
    except Exception as exc:
        check("通知 XML 合法", False, str(exc))

    check("特殊字符已转义", "&lt;" in xml and "&amp;" in xml, xml[:80])
    check("支持判定", isinstance(notifier_native.is_supported(), bool))


def test_single_instance():
    import time
    import uuid

    from roco.platform import single_instance as si

    # 隔离：真实互斥名可能正被用户运行中的程序占用（那说明单实例生效了，
    # 不该让自测因此变红）。这里换成一次性名字，只验证机制本身。
    suffix = ".selftest" + uuid.uuid4().hex[:8]
    si.MUTEX_NAME = si.MUTEX_NAME + suffix
    si.EVENT_NAME = si.EVENT_NAME + suffix

    first = si.acquire()
    second = si.acquire()
    check("首个实例获取成功", first is True)
    check("重复实例被拒绝", second is False)

    hits = []
    si.listen(lambda: hits.append(1))
    time.sleep(0.15)
    si.signal_existing()
    time.sleep(0.6)
    check("唤醒信号可达", len(hits) >= 1, f"hits={len(hits)}")
    si.stop_listener()


def test_floating_window():
    from roco.ui.floating_window import HEADER_H, FloatingWindow

    root = tk.Tk()
    root.withdraw()
    config = Config.load()
    config.floating_topmost = True
    window = FloatingWindow(root, config)
    window.update()

    snapshot = MerchantSnapshot(
        items=[
            MerchantItem("黑晶琉璃", 1000, 100, rarity=infer_rarity("黑晶琉璃", 1000)),
            MerchantItem("棱镜球", 3200000, 2, rarity=infer_rarity("棱镜球", 3200000)),
        ],
        status="open", round_index=3, next_refresh="2026-10-20 20:00",
    )
    window.set_snapshot(snapshot)
    window.update()
    expanded = window.winfo_height()
    check("悬浮窗渲染商品", len(window._rows) == 2, f"rows={len(window._rows)}")

    window.toggle_collapse()
    window.update()
    collapsed = window.winfo_height()
    check("悬浮窗可收起", collapsed <= HEADER_H + 4 < expanded, f"{collapsed} / {expanded}")

    window.toggle_collapse()
    window.update()
    check("悬浮窗可展开", window.winfo_height() >= expanded - 2)

    window.toggle_topmost()
    check("置顶可切换", window._topmost is False and config.floating_topmost is False)

    window.update_countdown("01:02:03")
    check("倒计时写入", "01:02:03" in window.countdown_label.cget("text"))

    window.destroy()
    root.destroy()


def test_config_roundtrip():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "config.json"
        config = Config()
        config.icon_cache_path = "D:/icons"
        config.autostart = True
        config.floating_enabled = True
        config.floating_topmost = False
        config.notify_native = True
        config.save(path)

        loaded = Config.load(path)
        check("新字段可持久化",
              loaded.icon_cache_path == "D:/icons" and loaded.autostart is True
              and loaded.floating_enabled is True and loaded.floating_topmost is False)
        check("老配置向后兼容",
              loaded.query_times == config.query_times and loaded.provider == "onebiji")

        # 换货重取节奏：可配置 + 越界/脏数据自动夹回安全区间
        config.recheck_delay_seconds = 900
        config.recheck_max_attempts = 5
        config.save(path)
        loaded = Config.load(path)
        check("重取节奏可持久化",
              loaded.recheck_delay_seconds == 900 and loaded.recheck_max_attempts == 5,
              f"{loaded.recheck_delay_seconds}/{loaded.recheck_max_attempts}")

        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["recheck_delay_seconds"] = "五分钟"
        raw["recheck_max_attempts"] = 99
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        loaded = Config.load(path)
        check("脏配置回落默认值", loaded.recheck_delay_seconds == 300,
              str(loaded.recheck_delay_seconds))
        check("越界次数被夹到上限", loaded.recheck_max_attempts == 10,
              str(loaded.recheck_max_attempts))

        # 收尾方式：可选值 + 白名单兜底 + 慢速参数范围
        config.recheck_give_up_policy = "slow"
        config.recheck_slow_interval_seconds = 1200
        config.recheck_slow_max_attempts = 5
        config.save(path)
        loaded = Config.load(path)
        check("收尾方式可持久化",
              loaded.recheck_give_up_policy == "slow"
              and loaded.recheck_slow_interval_seconds == 1200
              and loaded.recheck_slow_max_attempts == 5,
              f"{loaded.recheck_give_up_policy}/{loaded.recheck_slow_interval_seconds}"
              f"/{loaded.recheck_slow_max_attempts}")

        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["recheck_give_up_policy"] = "retry_forever"
        raw["recheck_slow_interval_seconds"] = 10
        raw["recheck_slow_max_attempts"] = 999
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        loaded = Config.load(path)
        check("未知收尾方式回落 stop", loaded.recheck_give_up_policy == "stop",
              loaded.recheck_give_up_policy)
        check("慢速间隔夹到下限", loaded.recheck_slow_interval_seconds == 300,
              str(loaded.recheck_slow_interval_seconds))
        check("慢速次数夹到上限", loaded.recheck_slow_max_attempts == 20,
              str(loaded.recheck_slow_max_attempts))


def test_notify_fallback_chain():
    """原生成功时不弹浮窗；原生失败才兜底。"""
    from roco.platform.notifier import Notifier

    root = tk.Tk()
    root.withdraw()
    config = Config()
    config.notify_native = True
    config.notify_toast = True
    config.notify_sound = False
    config.notify_flash = False
    config.webhook_url = ""

    notifier = Notifier(root, config)
    notifier._native_ok = True
    calls = {"native": 0, "toast": 0}
    notifier._send_native = lambda *a, **k: (calls.__setitem__("native", calls["native"] + 1) or True)
    notifier._show_toast = lambda *a, **k: calls.__setitem__("toast", calls["toast"] + 1)

    item = MerchantItem("棱镜球", 3200000, 2, rarity=infer_rarity("棱镜球", 3200000))
    notifier.alert_special([item])
    check("原生成功不弹浮窗", calls == {"native": 1, "toast": 0}, str(calls))

    notifier._send_native = lambda *a, **k: False
    notifier.alert_special([item])
    check("原生失败用浮窗兜底", calls["toast"] == 1, str(calls))

    config.notify_native = False
    notifier.alert_special([item])
    # 关闭原生后根本不该尝试原生，浮窗计数 +1
    check("关闭原生后走浮窗", calls["native"] == 1 and calls["toast"] == 2, str(calls))

    root.destroy()


def test_monitor_math():
    """多屏边界：用合成显示器验证，不依赖真实硬件。"""
    from roco.platform.monitors import (Monitor, best_monitor, clamp_drag, default_position,
                          is_visible_on, virtual_bounds)

    left = Monitor(-1920, 0, 1920, 1080, -1920, 0, 1920, 1040)          # 副屏在左
    primary = Monitor(0, 0, 2560, 1440, 0, 0, 2560, 1392, True)         # 主屏
    right = Monitor(2560, 0, 1920, 1080, 2560, 0, 1920, 1080)           # 副屏在右
    screens = [left, primary, right]

    check("虚拟桌面覆盖三屏", virtual_bounds(screens) == (-1920, 0, 6400, 1440),
          str(virtual_bounds(screens)))

    # 关键回归：旧实现用主屏宽度 2560 卡边界，x=3200 会被拉回 2480
    x, y = clamp_drag(screens, 3200, 300, 302, 200)
    check("可拖到右侧副屏", (x, y) == (3200, 300), f"{(x, y)}")

    x, y = clamp_drag(screens, -1500, 200, 302, 200)
    check("可拖到左侧副屏（负坐标）", (x, y) == (-1500, 200), f"{(x, y)}")

    x, y = clamp_drag(screens, 99999, 99999, 302, 200)
    check("拖出虚拟桌面被拦住", x == -1920 + 6400 - 80, f"x={x}")

    x, y = clamp_drag(screens, -99999, -99999, 302, 200)
    check("左上越界被拦住", x == -1920 - (302 - 80), f"x={x}")

    check("副屏位置判定为可见", is_visible_on(screens, 3000, 300, 302, 200))
    check("完全在屏外的位置判为不可见", not is_visible_on(screens, 9000, 300, 302, 200))

    # 原生行为：副屏比主屏矮，落在 y=1300 的"空白区"也照样停住，不吸附
    x, y = clamp_drag(screens, 3000, 1300, 302, 200)
    check("空白区允许停留（不吸附）", (x, y) == (3000, 1300), f"{(x, y)}")

    px, py = default_position(screens, 302, 200)
    check("默认位置在主屏右上", px > 2000 and py < 100, f"{(px, py)}")
    check("命中右上副屏", best_monitor(screens, 3000, 300, 302, 200) is right)


def test_floating_multimonitor():
    """真实多屏：副屏落位 + 拖动 + 位置持久化。"""
    from roco.platform.monitors import enum_monitors
    from roco.ui.floating_window import FloatingWindow

    screens = enum_monitors()
    root = tk.Tk()
    root.withdraw()
    config = Config.load()
    config.floating_geometry = ""

    window = FloatingWindow(root, config)
    window.update()

    if len(screens) < 2:
        check("多屏集成（当前只有一块屏，跳过）", True)
        window.destroy()
        root.destroy()
        return

    second = screens[1]
    target_x = second.x + 120
    target_y = second.y + 140

    # 模拟拖到第二块屏（旧实现会在这里被夹回主屏）
    window._monitors = screens
    window._drag_offset = (0, 0)

    class _Event:
        x_root = target_x
        y_root = target_y

    window._drag_move(_Event())
    window._drag_end()
    window.update()
    check("悬浮窗可拖到副屏", window.winfo_x() == target_x, f"x={window.winfo_x()} 期望 {target_x}")

    window.save_geometry()
    saved = config.floating_geometry
    window.destroy()

    # 重新创建：位置应从副屏恢复，而不是回到主屏
    again = FloatingWindow(root, config)
    again.update()
    check("重启后位置仍在副屏", again.winfo_x() == target_x,
          f"x={again.winfo_x()} saved={saved}")
    again.destroy()
    config.floating_geometry = ""
    root.destroy()


def test_logging():
    """日志：落盘、尾部读取、错误过滤、目录回退、查看器可渲染。"""
    import tempfile
    from pathlib import Path

    from roco import applog
    from roco.config import Config

    config = Config()
    path = applog.setup(config)
    check("日志文件已创建", path.exists(), str(path))

    applog.info("自测写入一条信息：%s", "hello")
    applog.error("自测写入一条错误：%s", "boom")
    content = applog.tail(200)
    check("日志可写可读", "自测写入一条信息" in content and "自测写入一条错误" in content)

    errors = applog.tail(200, errors_only=True)
    check("只看错误能过滤", "自测写入一条错误" in errors and "自测写入一条信息" not in errors,
          errors[-80:])

    with tempfile.TemporaryDirectory() as folder:
        config.log_dir = folder
        resolved, warning = applog.resolve_log_dir(config)
        check("自定义日志目录生效", resolved == Path(folder) and not warning, str(resolved))

    config.log_dir = str(Path(applog.default_log_dir()) / "不存在" / "子目录")
    resolved, warning = applog.resolve_log_dir(config)
    check("日志目录可自动创建", resolved.exists() and not warning, str(resolved))

    # 恢复默认，避免污染后续运行
    applog.setup(Config())

    # 查看器能正常渲染（不显示图片，纯文本）
    from roco.ui.log_viewer import LogViewer

    root = tk.Tk()
    root.withdraw()
    viewer = LogViewer(root)
    viewer.update()
    text = viewer.text.get("1.0", "end")
    check("日志查看器可渲染", "日志已启动" in text or len(text) > 0, f"len={len(text)}")
    viewer.destroy()
    root.destroy()


def test_tk_error_hook():
    """界面回调异常要落到日志里，而不是消失。"""
    from roco import applog

    applog.setup(Config())
    root = tk.Tk()
    root.withdraw()
    caught = []
    applog.install_tk_hook(root, lambda name, msg: caught.append((name, msg)))

    try:
        raise ValueError("模拟界面异常")
    except ValueError:
        import sys

        root.report_callback_exception(*sys.exc_info())

    check("界面异常被记录", "界面回调异常" in applog.tail(80)
          and "模拟界面异常" in applog.tail(80))
    check("界面异常有回调通知", caught and caught[0][0] == "ValueError", str(caught))
    root.destroy()


def test_rounds_parsing():
    """rounds 字段：解析、全天判定、按轮次过滤。"""
    from roco.core.models import MerchantItem, TOTAL_ROUNDS
    from roco.net.parsing import _to_rounds
    from roco.core.rounds import filter_by_round

    check("列表解析", _to_rounds([1, 2, 3, 4]) == (1, 2, 3, 4))
    check("去重排序", _to_rounds([3, 1, 1]) == (1, 3))
    check("字符串解析", _to_rounds("1,2,4") == (1, 2, 4))
    check("JSON 字符串解析", _to_rounds("[2,3]") == (2, 3))
    check("越界值被丢弃", _to_rounds([0, 5, 2]) == (2,))
    check("空值安全", _to_rounds(None) == () and _to_rounds("") == ())

    all_day = MerchantItem("条件球", 3000, 100, rounds=(1, 2, 3, 4))
    one_round = MerchantItem("血脉药", 5000, 10, rounds=(2,))
    unknown = MerchantItem("神秘商品", 100, 1)
    check("四轮商品算全天", all_day.is_all_day and len(all_day.rounds) == TOTAL_ROUNDS)
    check("单轮商品不算全天", not one_round.is_all_day)
    check("无 rounds 不算全天", not unknown.is_all_day)
    check("全天文案", all_day.rounds_text == "全天在售", all_day.rounds_text)
    check("单轮文案", one_round.rounds_text == "第 2 轮", one_round.rounds_text)

    kept, dropped = filter_by_round([all_day, one_round, unknown], 1)
    names = [i.name for i in kept]
    check("过滤掉不属于本轮的", [i.name for i in dropped] == ["血脉药"], str([i.name for i in dropped]))
    check("全天商品保留", "条件球" in names, str(names))
    check("轮次未知的保留", "神秘商品" in names, str(names))

    kept, dropped = filter_by_round([all_day, one_round], 2)
    check("第 2 轮保留血脉药", "血脉药" in [i.name for i in kept] and not dropped)


def test_round_lag_detection():
    """换货滞后判定：接口轮次落后本地时即为滞后。"""
    from roco.core.models import MerchantItem, MerchantSnapshot

    item = MerchantItem("条件球", 3000, 100, rounds=(1, 2, 3, 4))
    lagging = MerchantSnapshot(items=[item], status="open", round_index=1, expected_round=2)
    synced = MerchantSnapshot(items=[item], status="open", round_index=2, expected_round=2)
    unknown = MerchantSnapshot(items=[item], status="open", round_index=None, expected_round=2)

    check("接口落后判定为滞后", lagging.is_round_lagging)
    check("轮次一致不算滞后", not synced.is_round_lagging)
    check("轮次未知不误判", not unknown.is_round_lagging)
    check("全天件数统计", lagging.all_day_count == 1, str(lagging.all_day_count))

    # 真实结构：把接口返回体喂给解析器，验证轮次与过滤同时生效
    from roco.net.parsing import snapshot_from_payload

    payload = {
        "status": "open",
        "round": 1,
        "expectedRound": 2,
        "items": [
            {"name": "条件球", "price": 3000, "limit": 100, "rounds": [1, 2, 3, 4]},
            {"name": "旧轮商品", "price": 100, "limit": 1, "rounds": [1]},
        ],
    }
    snap = snapshot_from_payload(payload, "test", "测试渠道")
    names = [i.name for i in snap.items]
    check("解析出 expectedRound", snap.expected_round == 2, str(snap.expected_round))
    check("解析出当前轮次", snap.round_index == 1, str(snap.round_index))
    check("滞后被识别", snap.is_round_lagging)
    check("全天商品保留在结果里", "条件球" in names, str(names))


def test_round_retry_flow():
    """端到端：接口一直返回旧轮次时会自动重试；换到新轮次后停止。"""
    import time

    import roco.ui.app_window as aw
    from roco.core.models import MerchantItem, MerchantSnapshot

    def make_snapshot(round_no: int):
        items = [MerchantItem("条件球", 3000, 100, rounds=(1, 2, 3, 4))]
        if round_no >= 2:
            # 换货后货架正常带着本轮商品（只剩全天商品会被"内容比对"判据
            # 认为可疑，那是另一条测试覆盖的场景）
            items.append(MerchantItem("血脉药", 100, 1, rounds=(2,)))
        return MerchantSnapshot(
            items=items,
            status="open",
            round_index=round_no,
            expected_round=2,
            source_label="测试渠道",
            fetched_at=datetime.now(),
        )

    state = {"calls": 0, "current": 1}

    def fake_fetch(_config):
        state["calls"] += 1
        return make_snapshot(state["current"]), []

    original_fetch = aw.fetch_snapshot
    original_delays = aw.MerchantApp.ROUND_RETRY_DELAYS
    # 把重试间隔压到 0.05 秒，测试不必真等 30 秒
    aw.fetch_snapshot = fake_fetch
    aw.MerchantApp.ROUND_RETRY_DELAYS = (0.4, 0.4)
    try:
        root = tk.Tk()
        root.withdraw()
        config = Config()
        config.notify_enabled = False
        config.verify_egress = False
        app = aw.MerchantApp(root, config)

        def note_text():
            label = getattr(app, "_shelf_note", None)
            return label.cget("text") if label is not None else ""

        seen_status: list[str] = []

        def pump_until(condition, timeout=20):
            end = time.time() + timeout
            while time.time() < end:
                root.update()
                time.sleep(0.01)
                try:
                    seen_status.append(app.status_left.cget("text"))
                except tk.TclError:
                    pass
                if condition():
                    return True
            return False

        ok = pump_until(lambda: state["calls"] >= 2)
        check("滞后时自动重试", ok, f"calls={state['calls']}")
        check("重试计数递增", app._round_retry >= 1, str(app._round_retry))
        # 状态栏在两次重试之间会短暂显示"还在第 1 轮，N 秒后重试"，逐步采样才抓得到
        lag_status = [t for t in seen_status if "第 1 轮" in t and "重试" in t]
        check("状态栏提示换货校验", bool(lag_status),
              " / ".join(dict.fromkeys(seen_status[-6:]))[:160])
        check("货架提示滞后", "应为第 2 轮" in note_text(), note_text())

        # 模拟上游完成换货
        state["current"] = 2
        app.refresh(reason="手动刷新")
        ok2 = pump_until(lambda: not app._round_retry and state["calls"] >= 3)
        check("换货后停止重试", ok2, f"retry={app._round_retry} calls={state['calls']}")
        check("换货后不再提示滞后", "应为" not in note_text(), note_text())
        check("换货后倒计时恢复",
              "全天在售" in app._shelf_count.cget("text"),
              app._shelf_count.cget("text"))

        app.quit_app()
        try:
            root.destroy()
        except tk.TclError:
            pass
    finally:
        aw.fetch_snapshot = original_fetch
        aw.MerchantApp.ROUND_RETRY_DELAYS = original_delays


def test_shelf_freshness_rules():
    """1.4.2 新判据：只剩全天商品 + 内容未变 → 判定为"其实没换货"。"""
    from roco.core.freshness import (ACTION_GIVE_UP, ACTION_RETRY, CODE_ALL_DAY_ONLY,
                           CODE_ROUND_LAG, CODE_SHELF_SHRANK, ShelfWatcher)

    def snap(items, rnd=2, exp=2, status="open", src="onebiji"):
        return MerchantSnapshot(items=items, status=status, round_index=rnd,
                                expected_round=exp, source_id=src)

    def ball():
        return MerchantItem("条件球", 3000, 100, rounds=(1, 2, 3, 4))

    def prism():
        return MerchantItem("棱镜球", 5000, 10, rounds=(1, 2, 3, 4))

    def round_item(name, rnd):
        return MerchantItem(name, 100, 1, rounds=(rnd,))

    watcher = ShelfWatcher(round_delays=(30, 60), delay=300, max_attempts=2)
    first = watcher.observe(snap([ball(), prism()]))
    check("首次拿到全天货架不重取", first.action != ACTION_RETRY, str(first))

    second = watcher.observe(snap([ball(), prism()]))
    check("内容未变触发重取", second.should_retry and second.code == CODE_ALL_DAY_ONLY, str(second))
    check("默认延后 5 分钟", second.delay == 300.0, str(second.delay))
    check("重取次数从 1 开始", second.attempt == 1, str(second.attempt))

    third = watcher.observe(snap([ball(), prism()]))
    check("第二次仍未变继续重取", third.should_retry and third.attempt == 2, str(third))
    exhausted = watcher.observe(snap([ball(), prism()]))
    check("超过上限转为放弃", exhausted.action == ACTION_GIVE_UP, str(exhausted))
    again = watcher.observe(snap([ball(), prism()]))
    check("放弃后同一内容不再重复重取", again.action != ACTION_RETRY, str(again))

    fresh = watcher.observe(snap([ball(), prism(), round_item("血脉药", 2)]))
    check("出现本轮商品即视为可信", fresh.action != ACTION_RETRY, str(fresh))

    # 货架"缩水"：上次还有本轮商品，这次只剩全天商品 → 多半是旧轮次被过滤掉了
    shrink_watcher = ShelfWatcher(round_delays=(30, 60), delay=300, max_attempts=3)
    shrink_watcher.observe(snap([ball(), prism(), round_item("血脉药", 2)]))
    shrank = shrink_watcher.observe(snap([ball(), prism()]))
    check("本轮商品消失也判为可疑", shrank.should_retry and shrank.code == CODE_SHELF_SHRANK,
          str(shrank))

    # 轮次判据保持原样：阶梯间隔、条数上限
    lag_watcher = ShelfWatcher(round_delays=(30, 60), delay=300, max_attempts=3)
    lag1 = lag_watcher.observe(snap([ball(), round_item("旧药", 1)], rnd=1, exp=2))
    lag2 = lag_watcher.observe(snap([ball(), round_item("旧药", 1)], rnd=1, exp=2))
    lag3 = lag_watcher.observe(snap([ball(), round_item("旧药", 1)], rnd=1, exp=2))
    check("轮次落后走原有阶梯", lag1.delay == 30 and lag2.delay == 60 and lag3.action == ACTION_GIVE_UP,
          f"{lag1.delay}/{lag2.delay}/{lag3.action}")
    check("轮次判据标识不变", lag1.code == CODE_ROUND_LAG, lag1.code)

    # 关闭 / 边界
    off = ShelfWatcher(max_attempts=0)
    off.observe(snap([ball()]))
    check("次数配 0 关闭内容判据", not off.observe(snap([ball()])).should_retry)

    closed = ShelfWatcher()
    closed.observe(snap([ball()], status="closed"))
    check("打烊不重取", not closed.observe(snap([ball()], status="closed")).should_retry)

    empty = ShelfWatcher()
    empty.observe(snap([]))
    check("空货架不重取", not empty.observe(snap([])).should_retry)

    switched = ShelfWatcher()
    switched.observe(snap([ball()], src="onebiji"))
    check("换渠道后不误判为未变",
          not switched.observe(snap([ball()], src="custom")).should_retry)

    unknown = ShelfWatcher()
    unknown.observe(snap([MerchantItem("神秘商品", 100, 1)]))
    check("无 rounds 字段不误判全天货架",
          not unknown.observe(snap([MerchantItem("神秘商品", 100, 1)])).should_retry)

    from roco.core.freshness import format_delay
    check("延迟文案", format_delay(300) == "5 分钟" and format_delay(45) == "45 秒"
          and format_delay(90) == "1 分 30 秒",
          f"{format_delay(300)}/{format_delay(45)}/{format_delay(90)}")

    # 复现被卡住的现场：接口 round 已刷成第 2 轮，items 还是第 1 轮的货架。
    # filter_by_round() 把 rounds=[1] 的旧商品剔掉，只剩两件全天商品，
    # 而 is_round_lagging 为 False —— 1.4.1 会认为一切正常，从此不再重试。
    from roco.net.parsing import snapshot_from_payload

    stale_payload = {
        "status": "open",
        "round": 2,
        "expectedRound": 2,
        "items": [
            {"name": "条件球", "price": 3000, "limit": 100, "rounds": [1, 2, 3, 4]},
            {"name": "棱镜球", "price": 5000, "limit": 10, "rounds": [1, 2, 3, 4]},
            {"name": "旧轮药水", "price": 100, "limit": 1, "rounds": [1]},
        ],
    }
    stale = snapshot_from_payload(stale_payload, "onebiji", "测试渠道")
    check("卡住的现场：只剩两件全天商品",
          len(stale.items) == 2 and stale.all_day_count == 2, str([i.name for i in stale.items]))
    check("剔除数被记录（日志可追溯）", stale.dropped_count == 1, str(stale.dropped_count))
    check("轮次判据看不出问题（1.4.1 的盲区）", not stale.is_round_lagging)

    stuck = ShelfWatcher(delay=300, max_attempts=2)
    first_see = stuck.observe(stale)
    second_see = stuck.observe(snapshot_from_payload(stale_payload, "onebiji", "测试渠道"))
    check("首见不重取", first_see.action != ACTION_RETRY, str(first_see))
    check("连见两次完全相同 → 触发延后重取",
          second_see.should_retry and second_see.code == CODE_ALL_DAY_ONLY
          and second_see.delay == 300, str(second_see))

    healed = {
        "status": "open", "round": 2, "expectedRound": 2,
        "items": [
            {"name": "条件球", "price": 3000, "limit": 100, "rounds": [1, 2, 3, 4]},
            {"name": "棱镜球", "price": 5000, "limit": 10, "rounds": [1, 2, 3, 4]},
            {"name": "血脉药", "price": 8000, "limit": 2, "rounds": [2]},
        ],
    }
    check("接口恢复后不再重取",
          not stuck.observe(snapshot_from_payload(healed, "onebiji", "测试渠道")).should_retry)


def test_all_day_recheck_flow():
    """端到端：卡在两件全天商品上时，程序自己会按配置延后重取，且能收敛。"""
    import roco.ui.app_window as aw
    from roco.core.freshness import ShelfObservation, ShelfWatcher

    def make_snapshot(with_round_item: bool):
        items = [MerchantItem("条件球", 3000, 100, rounds=(1, 2, 3, 4)),
                 MerchantItem("棱镜球", 5000, 10, rounds=(1, 2, 3, 4))]
        if with_round_item:
            items.append(MerchantItem("血脉药", 100, 1, rounds=(2,)))
        return MerchantSnapshot(items=items, status="open", round_index=2,
                                expected_round=2, source_label="测试渠道",
                                fetched_at=datetime.now())

    state = {"calls": 0, "fresh": False}

    def fake_fetch(_config):
        state["calls"] += 1
        return make_snapshot(state["fresh"]), []

    original_fetch = aw.fetch_snapshot
    aw.fetch_snapshot = fake_fetch
    try:
        root = tk.Tk()
        root.withdraw()
        config = Config()
        config.notify_enabled = False
        config.verify_egress = False
        config.recheck_delay_seconds = 0.4      # 生产默认 300 秒，测试压到即时
        config.recheck_max_attempts = 2
        app = aw.MerchantApp(root, config)
        # 清掉上一轮测试 / 真实使用留下的货架指纹，保证"首次观测"干净
        app._watcher.forget()
        app._shelf_state_key = None

        def note_text():
            label = getattr(app, "_shelf_note", None)
            try:
                return label.cget("text") if label is not None and label.winfo_exists() else ""
            except tk.TclError:
                return ""

        def pill_text():
            try:
                for item in app.pill.find_all():
                    if app.pill.type(item) == "text":
                        return app.pill.itemcget(item, "text")
            except tk.TclError:
                pass
            return ""

        seen: list[str] = []

        def pump_until(condition, timeout=20):
            end = time.time() + timeout
            while time.time() < end:
                root.update()
                time.sleep(0.01)
                try:
                    seen.append(app.status_left.cget("text"))
                except tk.TclError:
                    pass
                if condition():
                    return True
            return False

        ok = pump_until(lambda: state["calls"] >= 1 and not app._loading)
        checked_first = ok and not app._round_retry
        check("首次观测不做判断", checked_first, f"calls={state['calls']} retry={app._round_retry}")

        # 第二次拿到完全相同的两件全天商品：从这里开始程序应自己重取，不用人点
        app.refresh(reason="手动刷新")
        ok = pump_until(lambda: state["calls"] >= 3)
        check("全天货架自动重取", ok, f"calls={state['calls']}")
        check("重取没有依赖用户操作", app._round_retry >= 1, str(app._round_retry))

        ok2 = pump_until(lambda: pill_text() == "数据滞后")
        check("重取用尽后标记数据滞后", ok2, f"pill={pill_text()} calls={state['calls']}")
        check("状态栏说明只剩全天商品",
              any("全天商品" in t for t in seen), " / ".join(dict.fromkeys(seen[-6:]))[:160])
        check("货架提示疑似未换货", "全天商品" in note_text(), note_text())
        calls_after_give_up = state["calls"]

        # 放弃后同一内容不再无限重取
        pump_until(lambda: False, timeout=1.2)
        check("放弃后不再空转", state["calls"] == calls_after_give_up,
              f"{calls_after_give_up} -> {state['calls']}")

        # 上游恢复：出现本轮商品，界面回到正常状态
        state["fresh"] = True
        app.refresh(reason="手动刷新")
        ok3 = pump_until(lambda: app.snapshot is not None and len(app.snapshot.items) == 3
                         and not app._round_retry)
        check("新商品出现后恢复", ok3, app._shelf_count.cget("text"))
        check("恢复后清掉可疑提示", "⚠" not in note_text(), note_text())

        # 货架指纹落盘 → 冷启动也能凭它判断"其实没换货"
        try:
            shelf = json.loads(state_path().read_text(encoding="utf-8")).get("shelf")
        except (OSError, json.JSONDecodeError):
            shelf = None
        check("货架指纹已落盘", isinstance(shelf, dict) and bool(shelf.get("content")), str(shelf))
        cold = ShelfWatcher(delay=300, max_attempts=1)
        stale_obs = ShelfObservation.of(make_snapshot(False))
        cold.seed(ShelfObservation.from_dict(stale_obs.to_dict()))
        check("冷启动凭上次货架即可判定",
              cold.observe(make_snapshot(False)).should_retry)

        app.quit_app()
        try:
            root.destroy()
        except tk.TclError:
            pass
    finally:
        aw.fetch_snapshot = original_fetch


def test_settings_recheck_fields():
    """设置界面能改重取节奏，非法输入被拦下且不改动配置。"""
    import tempfile
    from pathlib import Path

    from roco.services.cache import IconCache
    from roco.ui.settings_dialog import SettingsDialog

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "config.json"
        root = tk.Tk()
        root.withdraw()
        config = Config()
        cache = IconCache(Path(folder), False)
        original_save = Config.save
        # 让对话框的"保存"落到临时文件，别动用户真实配置
        Config.save = lambda self, target=None: original_save(self, path)
        try:
            dialog = SettingsDialog(root, config, cache)
            root.update()
            check("设置里有重取间隔输入框", hasattr(dialog, "_recheck_delay_var"))
            check("重取节奏带出当前配置",
                  dialog._recheck_delay_var.get() == str(config.recheck_delay_seconds)
                  and dialog._recheck_max_var.get() == str(config.recheck_max_attempts),
                  f"{dialog._recheck_delay_var.get()}/{dialog._recheck_max_var.get()}")
            check("收尾方式默认放弃本轮", dialog._policy_var.get() == "stop",
                  dialog._policy_var.get())
            check("放弃本轮时慢速输入框被禁用",
                  str(dialog._slow_interval_entry.cget("state")) == "disabled",
                  str(dialog._slow_interval_entry.cget("state")))
            dialog._policy_var.set("slow")
            dialog._sync_slow_fields()
            check("切到慢速后输入框解禁",
                  str(dialog._slow_interval_entry.cget("state")) == "normal",
                  str(dialog._slow_interval_entry.cget("state")))

            # 非法值：拦下并保留对话框
            dialog._recheck_delay_var.set("5分钟")
            dialog._save()
            check("非法间隔被拦下",
                  bool(dialog.winfo_exists()) and "整数" in dialog._error.cget("text"),
                  dialog._error.cget("text"))

            dialog._recheck_delay_var.set("600")
            dialog._recheck_max_var.set("4")
            dialog._slow_interval_var.set("900")
            dialog._slow_max_var.set("2")
            dialog._save()
            saved = Config.load(path)
            check("重取节奏可保存并生效",
                  saved.recheck_delay_seconds == 600 and saved.recheck_max_attempts == 4,
                  f"{saved.recheck_delay_seconds}/{saved.recheck_max_attempts}")
            check("收尾方式可保存",
                  saved.recheck_give_up_policy == "slow"
                  and saved.recheck_slow_interval_seconds == 900
                  and saved.recheck_slow_max_attempts == 2,
                  f"{saved.recheck_give_up_policy}/{saved.recheck_slow_interval_seconds}"
                  f"/{saved.recheck_slow_max_attempts}")
        finally:
            Config.save = original_save
            try:
                root.destroy()
            except tk.TclError:
                pass


def test_give_up_policy():
    """收尾方式可选：stop（默认，原行为）与 slow（慢速续试），互不干扰。"""
    from roco.core.freshness import (ACTION_GIVE_UP, ACTION_RETRY, POLICY_SLOW, POLICY_STOP,
                           ShelfWatcher)

    def snap(items, rnd=2, exp=2):
        return MerchantSnapshot(items=items, status="open", round_index=rnd,
                                expected_round=exp, source_id="onebiji")

    def ball():
        return MerchantItem("条件球", 3000, 100, rounds=(1, 2, 3, 4))

    # 默认必须是 stop：不显式配置时行为与 1.4.2 完全一致
    default = ShelfWatcher(delay=300, max_attempts=2)
    check("默认收尾方式是放弃本轮", default.give_up_policy == POLICY_STOP,
          default.give_up_policy)
    default.observe(snap([ball()]))
    default.observe(snap([ball()]))
    default.observe(snap([ball()]))
    check("默认策略用尽即放弃",
          default.observe(snap([ball()])).action == ACTION_GIVE_UP)

    # slow：常规次数用完后转慢速，间隔取 slow_interval
    slow = ShelfWatcher(delay=300, max_attempts=2, give_up_policy=POLICY_SLOW,
                        slow_interval=1800, slow_max_attempts=0)
    slow.observe(snap([ball()]))            # 初次，不重取
    first = slow.observe(snap([ball()]))    # 第 1 次常规重取
    second = slow.observe(snap([ball()]))   # 第 2 次常规重取
    third = slow.observe(snap([ball()]))    # 常规用尽 → 转慢速
    check("慢速策略先走常规节奏",
          first.should_retry and not first.slow and first.delay == 300, str(first))
    check("常规次数用完后转慢速",
          third.should_retry and third.slow and third.delay == 1800, str(third))
    check("慢速轮次从 1 开始", third.slow_round == 1, str(third.slow_round))
    fourth = slow.observe(snap([ball()]))
    check("慢速可连续续试", fourth.should_retry and fourth.slow_round == 2, str(fourth))
    check("常规次数展示不因慢速而虚增", fourth.attempt == 2 and fourth.limit == 2,
          f"{fourth.attempt}/{fourth.limit}")
    check("不限次数时 slow_limit 为 0", fourth.slow_limit == 0, str(fourth.slow_limit))

    # slow + 上限：用尽后回到放弃
    limited = ShelfWatcher(delay=300, max_attempts=1, give_up_policy=POLICY_SLOW,
                           slow_interval=1800, slow_max_attempts=2)
    limited.observe(snap([ball()]))
    limited.observe(snap([ball()]))   # 常规 1
    s1 = limited.observe(snap([ball()]))
    s2 = limited.observe(snap([ball()]))
    s3 = limited.observe(snap([ball()]))
    check("慢速上限内仍续试", s1.slow and s2.slow, f"{s1.slow}/{s2.slow}")
    check("慢速上限用尽即放弃", s3.action == ACTION_GIVE_UP, str(s3))

    # 慢速期间内容变化 → 退出慢速，回到常规预算
    changed = ShelfWatcher(delay=300, max_attempts=2, give_up_policy=POLICY_SLOW,
                           slow_interval=1800, slow_max_attempts=0)
    changed.observe(snap([ball()]))
    changed.observe(snap([ball()]))
    changed.observe(snap([ball()]))
    changed.observe(snap([ball()]))
    after = changed.observe(snap([ball(), MerchantItem("血脉药", 100, 1, rounds=(2,))]))
    check("内容变化后退出慢速", after.action != ACTION_RETRY and changed.slow_count == 0,
          f"{after.action}/{changed.slow_count}")

    # 从 stop 切到 slow：不该被之前的"已放弃"标记挡住
    switched = ShelfWatcher(delay=300, max_attempts=1)
    switched.observe(snap([ball()]))
    switched.observe(snap([ball()]))
    switched.observe(snap([ball()]))          # 放弃，落 _exhausted
    switched.configure(give_up_policy=POLICY_SLOW, slow_interval=1800)
    revived = switched.observe(snap([ball()]))
    check("切到慢速后可继续续试", revived.should_retry and revived.slow, str(revived))

    # 非法策略值回落默认
    bad = ShelfWatcher(give_up_policy="whatever")
    check("非法策略回落 stop", bad.give_up_policy == POLICY_STOP, bad.give_up_policy)
    bad.configure(slow_interval=1, slow_max_attempts=999)
    check("慢速参数被夹取", bad.slow_interval == 1 and bad.max_attempts == 3,
          f"{bad.slow_interval}")


def test_publish_script():
    """同步脚本的纯逻辑：哪些文件该推、提交信息怎么拼。"""
    import importlib.util
    from datetime import datetime

    spec = importlib.util.spec_from_file_location(
        "publish_tool", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "tools", "publish.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    check("源码可同步", module.is_publishable("freshness.py"))
    check("公共文档可同步", module.is_publishable("docs/cn-mirror.md"))
    check("本地开发文档不同步", not module.is_publishable("docs/local/development.md"))
    check("脚本可同步", module.is_publishable("scripts/sync.bat"))
    check("产物不同步", not module.is_publishable("dist/远行商人助手.exe", 1024))
    check("构建目录不同步", not module.is_publishable("build/x.py"))
    check("缓存不同步", not module.is_publishable("__pycache__/config.cpython-313.pyc"))
    check("CI 目录不同步", not module.is_publishable(".github/workflows/release.yml"))
    check("日志不同步", not module.is_publishable("logs/app.log"))
    check("超大文件不同步", not module.is_publishable("assets/big.png", 30 * 1024 * 1024))
    check("反斜杠路径也能识别", module.is_publishable("ui\\app_window.py"))

    # 版本号用明显的假值，避免与实际发版号混在一起被误读
    msg = module.build_commit_message(["ui/app_window.py", "README.md"], "9.9.9",
                                      datetime(2026, 10, 23, 21, 0))
    check("提交信息含类型与日期", "代码" in msg and "文档" in msg and "2026-10-23" in msg, msg)
    check("提交信息带版本号", "v9.9.9" in msg, msg)
    check("无版本号时不出现空括号",
          "（v" not in module.build_commit_message(["README.md"]))


def test_provider_onebiji():
    """中文查询器：页面解析、同名多轮合并、渠道注册与老配置迁移。"""
    from roco.config import DEFAULT_PROVIDERS
    from roco.net.providers import _REGISTRY
    from roco.core.rounds import filter_by_round
    from roco.net.parsing import parse_bwiki_shops, parse_onebiji_html

    page = '''
    <ul class="shop-list">
      <li class="all_show li_show on show_1 show_2 show_3 show_4 " data-time="1792857600"
          onclick="showShopinfo('https://img/1.png','网兜球','特制咕噜球','更适合用来捕捉水系精灵。')">
        <div class="gitem"><img src="https://img/1.png" alt=""><em>限购100</em></div>
        <div class="sp-text"><p><em class="shop_name">网兜球</em></p>
          <div><em class="shop_price">价格：3000 </em></div></div>
      </li>
      <li class="all_show li_show on show_1 " data-time="1792814400"
          onclick="showShopinfo('https://img/2.png','神奇的蛋','精灵蛋','蕴含着难以捉摸的魔力。')">
        <div class="gitem"><img src="https://img/2.png" alt=""><em>限购5</em></div>
        <div class="sp-text"><p><em class="shop_name">神奇的蛋</em></p>
          <div><em class="shop_price">价格：36,000 </em></div></div>
      </li>
      <li class="all_show li_show on show_3 " data-time="1792828800"
          onclick="showShopinfo('https://img/2.png','神奇的蛋','精灵蛋','蕴含着难以捉摸的魔力。')">
        <div class="gitem"><img src="https://img/2.png" alt=""><em>限购5</em></div>
        <div class="sp-text"><p><em class="shop_name">神奇的蛋</em></p>
          <div><em class="shop_price">价格：36,000 </em></div></div>
      </li>
      <li class="all_show li_show" onclick="showShopinfo('','','','')">
        <div class="gitem"><img src="https://img/9.png" alt=""></div>
      </li>
    </ul>
    '''
    items = parse_onebiji_html(page)
    by_name = {item.name: item for item in items}
    check("解析出商品（跳过无名占位）", len(items) == 2, str([i.name for i in items]))

    ball = by_name["网兜球"]
    check("全天轮次识别", ball.rounds == (1, 2, 3, 4) and ball.is_all_day, str(ball.rounds))
    check("价格与限购", ball.price == 3000 and ball.limit == 100,
          f"{ball.price}/{ball.limit}")
    check("图标与分类",
          ball.icon_url.endswith("1.png") and ball.category == "特制咕噜球",
          f"{ball.icon_url}/{ball.category}")
    check("描述被带出", "水系精灵" in ball.description, ball.description[:20])

    egg = by_name["神奇的蛋"]
    check("同名多轮合并", egg.rounds == (1, 3), str(egg.rounds))
    check("千分位价格可解析", egg.price == 36000, str(egg.price))
    check("非全天商品不误判", not egg.is_all_day)

    kept, dropped = filter_by_round(items, 3)
    check("按轮次筛选",
          [i.name for i in kept] == ["网兜球", "神奇的蛋"] and len(dropped) == 0,
          f"{[i.name for i in kept]}/{len(dropped)}")

    # BWIKI 只能给出商店目录（这就是它不能当数据源的原因）
    shops = parse_bwiki_shops(
        '<tr class="divsort shopdex-shop-row"><td>'
        '<span class="shopdex-shop-name">远行商人</span>'
        '<span class="shopdex-shop-id">#2002</span>'
        '<span class="shopdex-shop-count">1 件</span></td></tr>')
    check("BWIKI 商店行解析", shops == [("远行商人", 1)], str(shops))

    # 渠道注册 / 默认值 / 顺序
    check("onebiji 已注册", "onebiji" in _REGISTRY)
    check("bwiki 已注册", "bwiki" in _REGISTRY)
    check("英文镜像已下线", "rkw" not in _REGISTRY and "rkw" not in DEFAULT_PROVIDERS)
    check("默认渠道是中文查询器", Config().provider == "onebiji", Config().provider)
    order = Config().provider_order()
    check("降级顺序含新渠道",
          order.index("cn_mirror") < order.index("onebiji") < order.index("bwiki") < order.index("yxsr"),
          str(order))
    check("BWIKI 默认不填地址（避免误用）",
          not Config().providers["bwiki"]["base_url"],
          Config().providers["bwiki"]["base_url"])

    # 自建反代既能转发 JSON，也能转发页面（HTML → items）
    from roco.net.providers import CnMirrorProvider

    mirror = CnMirrorProvider({"base_url": "https://mirror.example/merchant"})
    mirror._request_text = lambda url, headers=None, use_doh=True: page
    mirrored = mirror.fetch()
    # 不断言本轮保留几件：解析结果会按"当前轮次"过滤，跑在不同时间点结论不同。
    # 用"保留 + 剔除 = 解析总数"来验证解析链路，才不会随钟点飘。
    check("反代页面也能解析",
          mirrored.source_id == "cn_mirror"
          and len(mirrored.items) + mirrored.dropped_count == 2,
          f"{len(mirrored.items)}+{mirrored.dropped_count}/{mirrored.source_id}")

    # 老配置：provider=rkw + 残留 rkw 条目 → 迁移到 onebiji 并清理
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "config.json"
        raw = {"provider": "rkw",
               "providers": {"rkw": {"base_url": "https://old.example/api"}}}
        path.write_text(json.dumps(raw), encoding="utf-8")
        loaded = Config.load(path)
        check("老 provider=rkw 迁移到 onebiji", loaded.provider == "onebiji", loaded.provider)
        check("老 rkw 条目被清理", "rkw" not in loaded.providers,
              str(list(loaded.providers)))


def test_price_units():
    """价格简写单位（16w / 16万）不能被截断成 16 —— 2026-10-25 的回归用例。"""
    from roco.core.numerals import to_chinese_number
    from roco.net.parsing import _to_int, parse_onebiji_html

    cases = {
        "16w": 160000, "16W": 160000, "16万": 160000, "1.6w": 16000,
        "160,000": 160000, "3000": 3000, "价格：16w": 160000,
        "1000 万": 10_000_000, "16亿": 1_600_000_000,
    }
    bad = {key: _to_int(key) for key, want in cases.items() if _to_int(key) != want}
    check("价格单位换算", not bad, str(bad))
    check("纯数字与限购不受影响",
          _to_int("3200000") == 3200000 and _to_int("限购100") == 100,
          f"{_to_int('3200000')}/{_to_int('限购100')}")
    check("中文读法本身正确", to_chinese_number(160000) == "十六万",
          to_chinese_number(160000))

    page = '''
    <li class="all_show li_show on show_2 " onclick="showShopinfo('','国王球','条件球','')">
      <div class="gitem"><img src="https://img/k.png"><em>限购10</em></div>
      <div class="sp-text"><p><em class="shop_name">国王球</em></p>
        <div><em class="shop_price">价格：16w </em></div></div>
    </li>
    '''
    item = parse_onebiji_html(page)[0]
    check("页面 16w 解析为 160000", item.price == 160000, str(item.price))
    check("页面价格中文读法", item.price_text == "十六万", item.price_text)
    check("原样价格不被截断", item.price_raw_text == "160,000 洛克贝",
          item.price_raw_text)
    check("稀有度按真实价格推断", item.rarity.level >= 4, item.rarity.label)


def test_floating_refresh_button():
    """悬浮窗的「⟳」：不打开主窗口也能取数，状态能复位。"""
    from roco.ui.floating_window import FloatingWindow

    root = tk.Tk()
    root.withdraw()
    hits: list[int] = []
    floating = FloatingWindow(root, Config(), on_refresh=lambda: hits.append(1))
    try:
        root.update()
        check("悬浮窗有获取按钮", floating.refresh_btn.cget("text") == "⟳",
              floating.refresh_btn.cget("text"))
        floating.request_refresh()
        check("点击触发取数", hits == [1], str(hits))
        check("取数中按钮变状态", floating.refresh_btn.cget("text") == "…",
              floating.refresh_btn.cget("text"))
        check("取数中标记为真", floating.refreshing is True)
        floating.request_refresh()
        check("取数中不重复触发", hits == [1], str(hits))
        floating.set_refreshing(False)
        check("结果回来按钮复位", floating.refresh_btn.cget("text") == "⟳",
              floating.refresh_btn.cget("text"))

        floating.toggle_collapse()
        root.update()
        check("收起后按钮仍可见", bool(floating.refresh_btn.winfo_ismapped()),
              str(floating.refresh_btn.winfo_ismapped()))
        floating.request_refresh()
        check("收起后仍可取数", len(hits) == 2, str(hits))
    finally:
        floating.destroy()
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_floating_refresh_while_hidden():
    """端到端：主窗口藏在托盘里，点悬浮窗按钮也能拿到并展示最新物品。"""
    import roco.ui.app_window as aw

    state = {"calls": 0}

    def fake_fetch(_config):
        state["calls"] += 1
        return MerchantSnapshot(
            items=[MerchantItem("国王球", 160000, 10, rounds=(2,), icon_url="")],
            status="open", round_index=2, expected_round=2,
            source_label="测试渠道", fetched_at=datetime.now(),
        ), []

    original_fetch = aw.fetch_snapshot
    aw.fetch_snapshot = fake_fetch
    root = tk.Tk()
    root.withdraw()
    config = Config()
    config.notify_enabled = False
    config.verify_egress = False
    app = aw.MerchantApp(root, config)
    try:
        def pump_until(condition, timeout=20):
            end = time.time() + timeout
            while time.time() < end:
                root.update()
                time.sleep(0.01)
                if condition():
                    return True
            return False

        ok = pump_until(lambda: state["calls"] >= 1 and not app._loading)
        check("首次取数完成", ok, f"calls={state['calls']}")

        app.toggle_floating(True, quiet=True)
        root.update()
        app.hide_to_tray(save_geometry=False)
        root.update()
        check("主窗口已隐藏", not app.root.winfo_viewable())

        before = state["calls"]
        app.floating.request_refresh()
        # 等到"取数完成 + 按钮复位"：calls 在后台线程先加，回调还没跑完时不能断言
        fetched = pump_until(lambda: state["calls"] > before and not app._loading
                             and not app.floating.refreshing)
        check("隐藏时点按钮能取数", fetched, f"{before} -> {state['calls']}")
        check("没有把主窗口叫到前台", not app.root.winfo_viewable())
        check("悬浮窗按钮已复位", not app.floating.refreshing)

        def collect(widget) -> list[str]:
            texts = []
            for child in widget.winfo_children():
                try:
                    texts.append(str(child.cget("text")))
                except tk.TclError:
                    pass
                texts.extend(collect(child))
            return texts

        check("悬浮窗展示最新中文价格",
              any("十六万" in text for text in collect(app.floating)),
              str([t for t in collect(app.floating) if t][:8]))
    finally:
        aw.fetch_snapshot = original_fetch
        try:
            app.quit_app()
        except Exception:
            pass


def test_history_store():
    """历史：倒序、保留全天商品、同轮去重、跨天清理。"""
    import tempfile
    from datetime import timedelta, timezone
    from pathlib import Path

    from roco.core import history

    CN = timezone(timedelta(hours=8))
    all_day = MerchantItem("条件球", 3000, 100, rounds=(1, 2, 3, 4))

    def snapshot(round_no, names, moment):
        items = [all_day] + [MerchantItem(n, 1000 * (i + 1), 1, rounds=(round_no,))
                             for i, n in enumerate(names)]
        return MerchantSnapshot(items=items, status="open", round_index=round_no,
                                fetched_at=moment)

    def at(hour, day=25):
        return datetime(2026, 10, day, hour, 5, tzinfo=CN)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "history.json"
        # 先塞一条十天前的记录，验证后续写入时会被清理
        history.record(snapshot(1, ["十天前的货"], at(8, day=15)),
                       path=path, now=at(8, day=15))

        round_of = {8: 1, 12: 2, 16: 3, 20: 4}
        for hour, names in ((12, ["午间货"]), (20, ["晚间货"]),
                            (8, ["早间货"]), (16, ["午后货"])):
            history.record(snapshot(round_of[hour], names, at(hour)),
                           path=path, now=at(hour))

        rounds = history.load(path)
        check("历史按时间倒序", [r.round_index for r in rounds] == [4, 3, 2, 1],
              str([r.round_index for r in rounds]))
        check("历史钟点正确",
              [r.clock for r in rounds] == ["20:00", "16:00", "12:00", "08:00"],
              str([r.clock for r in rounds]))
        check("历史保留全天商品",
              all(any(i.is_all_day for i in r.items) for r in rounds))

        # 同一轮再查一次：更新内容而不是追加一条
        history.record(snapshot(4, ["晚间货", "新上架"], at(20)), path=path, now=at(20))
        rounds = history.load(path)
        check("同一轮只留一条", len(rounds) == 4, str(len(rounds)))
        check("同轮内容会被更新",
              any(i.name == "新上架" for i in rounds[0].items))

        # 打烊空快照不进历史
        closed = MerchantSnapshot(items=[all_day], status="closed", round_index=0,
                                  fetched_at=at(20))
        check("打烊快照不入历史", history.record(closed, path=path, now=at(20)) is None)

        rounds = history.load(path)
        check("超期历史被清理", all(r.date == "2026-10-25" for r in rounds),
              str({r.date for r in rounds}))
        check("清理后仍是 4 轮", len(rounds) == 4, str(len(rounds)))


def test_purchase_marks():
    """购买标记：按轮次记录、可切换、清空后归零。"""
    import tempfile
    from datetime import timedelta, timezone
    from pathlib import Path

    from roco.core import purchases

    CN = timezone(timedelta(hours=8))
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "purchases.json"
        key = "2026-10-25#3"
        check("初始未购买", not purchases.is_bought(key, "国王球", path))
        check("勾选返回新状态", purchases.toggle(key, "国王球", path) is True)
        check("勾选已落盘", purchases.is_bought(key, "国王球", path))
        check("再次点击取消", purchases.toggle(key, "国王球", path) is False)
        check("取消后不再记录", not purchases.is_bought(key, "国王球", path))

        purchases.mark(key, "网兜球", True, path)
        check("已购计数", purchases.count(key, path) == 1, str(purchases.count(key, path)))
        purchases.clear_round(key, path)
        check("清空本轮", purchases.count(key, path) == 0)

        moment = datetime(2026, 10, 25, 14, 0, tzinfo=CN)
        s3 = MerchantSnapshot(items=[], status="open", round_index=3, fetched_at=moment)
        s4 = MerchantSnapshot(items=[], status="open", round_index=4, fetched_at=moment)
        check("换轮后标记不继承",
              purchases.round_key(s3, moment) != purchases.round_key(s4, moment))


def test_catalog_store():
    """物品清单：本机观察、手动增删、图标优先小图、Wiki 页面解析。"""
    import tempfile
    from pathlib import Path

    from roco.net import catalog

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "items.json"

        check("图标优先缩略图",
              catalog.prefer_small_icon(
                  ["https://a.example/big.png", "https://a.example/100px-big.png"]
              ).endswith("100px-big.png"))
        check("没有缩略图时用第一个",
              catalog.prefer_small_icon(["https://a.example/a.png",
                                         "https://a.example/b.png"])
              == "https://a.example/a.png")
        check("空候选返回空串", catalog.prefer_small_icon([]) == "")

        shelf = MerchantSnapshot(
            items=[MerchantItem("测试果球", 8000, 1, icon_url="https://x/100px-a.png"),
                   MerchantItem("测试网兜", 3000, 100, icon_url="https://x/big.png")],
            status="open", round_index=2,
        )
        added = catalog.observe(shelf, path)
        names = {i.name for i in catalog.load(path)}
        check("本机商品自动入清单", added == 2 and {"测试果球", "测试网兜"} <= names,
              f"added={added}")
        icon = next(i for i in catalog.load(path) if i.name == "测试果球").icon
        check("入清单时用缩略图", icon.endswith("100px-a.png"), icon)
        check("内置清单含活动道具",
              "可可果球" in {i.name for i in catalog.load(path)})

        check("手动添加成功", catalog.add_manual("新活动球", path))
        check("重复添加被拒绝", not catalog.add_manual("新活动球", path))
        check("手动条目可查到", "新活动球" in {i.name for i in catalog.load(path)})
        catalog.remove("新活动球", path)
        check("移除后不再出现", "新活动球" not in {i.name for i in catalog.load(path)})

        html = (
            '<tr class="divsort shopdex-shop-row" id="shop-2002">'
            '<td colspan="4"><span class="shopdex-shop-name">远行商人</span>'
            '<span class="shopdex-shop-count">2 件</span></td></tr>'
            '<tr class="divsort"><td>x</td><td>可可果</td><td>洛克贝</td></tr>'
            '<tr class="divsort"><td>x</td><td>可可果球</td><td>洛克贝</td></tr>'
            '<tr class="divsort shopdex-shop-row" id="shop-2003">'
            '<td colspan="4"><span class="shopdex-shop-name">果子商店</span>'
            '<span class="shopdex-shop-count">1 件</span></td></tr>'
            '<tr class="divsort"><td>x</td><td>无花果</td><td>洛克贝</td></tr>'
        )
        check("只取远行商人名下的商品",
              catalog.parse_shop_items(html) == ["可可果", "可可果球"],
              str(catalog.parse_shop_items(html)))
        check("空页面不抛异常", catalog.parse_shop_items("") == [])
        check("能从条目页取图标",
              catalog.parse_page_icon('<img src="https://patchwiki.biligame.com/x.png">')
              == "https://patchwiki.biligame.com/x.png")


def test_notify_item_selection():
    """通知：品质筛选保留，关注物品无条件命中，两者取并集。"""
    from roco.platform.notifier import Notifier, select_targets
    from roco.core.rarity import RARITY_TABLE

    plain = MerchantItem("普通材料", 100, 5)
    epic = MerchantItem("史诗宝珠", 500000, 1, rarity=RARITY_TABLE[4])
    watched = MerchantItem("可可果球", 8000, 1)
    snap = MerchantSnapshot(items=[plain, epic, watched])

    names = lambda items: [i.name for i in items]
    check("无关注物品时按品质筛选",
          names(select_targets(snap, 4, [])) == ["史诗宝珠"],
          str(names(select_targets(snap, 4, []))))
    check("关注物品无视品质",
          names(select_targets(snap, 4, ["可可果球"])) == ["史诗宝珠", "可可果球"],
          str(names(select_targets(snap, 4, ["可可果球"]))))
    check("两条规则取并集不重复",
          names(select_targets(snap, 1, ["史诗宝珠"]))
          == ["普通材料", "史诗宝珠", "可可果球"],
          str(names(select_targets(snap, 1, ["史诗宝珠"]))))

    title, _body = Notifier._compose([watched], ["可可果球"])
    check("关注物品的标题不说错品质", title == "关注物品：可可果球", title)
    rare_title, _body = Notifier._compose([epic], ["可可果球"])
    check("未关注的仍按品质命名", rare_title == "史诗物品：史诗宝珠", rare_title)


def test_midnight_shelf():
    """24 点后不得再展示昨日货架（含上游仍报 open 的情况）。"""
    from datetime import timedelta, timezone

    from roco.net.parsing import snapshot_from_items, snapshot_from_payload

    CN = timezone(timedelta(hours=8))
    item = MerchantItem("国王球", 160000, 1, rounds=(1, 2, 3, 4))

    before = snapshot_from_items([item], "t", "测试",
                                moment=datetime(2026, 10, 25, 23, 59, tzinfo=CN))
    check("打烊前正常展示", before.status == "open" and len(before.items) == 1,
          f"{before.status}/{len(before.items)}")

    after = snapshot_from_items([item], "t", "测试",
                               moment=datetime(2026, 10, 26, 0, 5, tzinfo=CN))
    check("00:05 清空昨日货架", after.items == [] and after.status == "closed",
          f"{after.status}/{len(after.items)}")
    check("00:05 不再算作某一轮", after.round_index == 0, str(after.round_index))

    payload = {"status": "open", "round": 4,
               "items": [{"name": "国王球", "price": 160000}]}
    cached = snapshot_from_payload(payload, "t", "测试",
                                   moment=datetime(2026, 10, 26, 0, 30, tzinfo=CN))
    check("上游仍报 open 也按打烊处理",
          cached.items == [] and cached.status == "closed",
          f"{cached.status}/{len(cached.items)}")


def test_purchase_and_history_widgets():
    """界面：卡片勾选回调、已购样式、历史面板默认折叠。"""
    import tempfile
    from pathlib import Path

    from roco.core import history as history_mod
    from roco.services.cache import IconCache, ImageLoader
    from roco.ui.history_panel import HistoryPanel
    from roco.ui.widgets import ItemCard

    root = tk.Tk()
    root.withdraw()
    cache = IconCache(Path(tempfile.mkdtemp()) / "icons", False)
    loader = ImageLoader(root, cache)
    try:
        hits = []
        card = ItemCard(root, MerchantItem("国王球", 160000, 1), loader,
                        on_toggle=hits.append)
        card._toggle_purchased()
        check("卡片点击会触发勾选", bool(hits) and hits[0].name == "国王球")

        bought = ItemCard(root, MerchantItem("网兜球", 3000, 5), loader,
                          purchased=True, on_toggle=hits.append)
        check("已购卡片显示勾选", "已购" in bought.toggle_btn.cget("text"),
              bought.toggle_btn.cget("text"))

        panel = HistoryPanel(root)
        panel.set_rounds([
            history_mod.HistoryRound("2026-10-25", 4, (MerchantItem("A", 1, 1),)),
            history_mod.HistoryRound("2026-10-25", 3, (MerchantItem("B", 1, 1),)),
        ])
        check("历史面板默认折叠", panel._expanded is False)

        empty = HistoryPanel(root)
        empty.set_rounds([])
        empty.toggle()
        check("没有数据时不可展开", empty._expanded is False)
        check("没有数据时按钮为禁用态",
              empty._toggle_btn._text == "暂无历史" and not empty._toggle_btn._enabled,
              f"{empty._toggle_btn._text}/{empty._toggle_btn._enabled}")
        panel.toggle()
        check("展开后渲染出内容",
              panel._expanded and len(panel._body.winfo_children()) >= 2,
              str(len(panel._body.winfo_children())))

        # 有加载器时，展开的历史必须仍是"商品卡片"，不能退化成一段纯文本
        rich = HistoryPanel(root, loader=loader)
        rich.set_rounds([history_mod.HistoryRound(
            "2026-10-25", 4,
            (MerchantItem("国王球", 160000, 1), MerchantItem("网兜球", 3000, 5)),
        )])
        rich.toggle()
        root.update()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        check("展开后复用商品卡片",
              any(isinstance(w, ItemCard) for w in descendants(rich._body)))
        check("展开按钮会变成收起", rich._toggle_btn._text == "收起历史",
              rich._toggle_btn._text)
    finally:
        loader.shutdown()
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_purchase_area_flow():
    """端到端：勾选后商品离开在售区、进入「已购买」区，并落盘。"""
    import roco.ui.app_window as aw
    from roco.core import purchases

    items = [MerchantItem("国王球", 160000, 1, rounds=(2,)),
             MerchantItem("网兜球", 3000, 100, rounds=(1, 2, 3, 4))]
    snap = MerchantSnapshot(items=items, status="open", round_index=2,
                            expected_round=2, source_label="测试渠道",
                            fetched_at=datetime.now())

    original_fetch = aw.fetch_snapshot
    aw.fetch_snapshot = lambda _config: (snap, [])
    root = tk.Tk()
    root.withdraw()
    config = Config()
    config.notify_enabled = False
    config.verify_egress = False
    app = None
    try:
        app = aw.MerchantApp(root, config)
        app._watcher.forget()
        app._shelf_state_key = None

        def pump_until(condition, timeout=12):
            end = time.time() + timeout
            while time.time() < end:
                root.update()
                time.sleep(0.01)
                if condition():
                    return True
            return False

        ok = pump_until(lambda: app.snapshot is not None and len(app._cards) == 2)
        check("初始两件都在在售区", ok and not app._bought_cards,
              f"{len(app._cards)}/{len(app._bought_cards)}")

        key = app._round_key(app.snapshot)
        check("已购标记按轮次落盘", not purchases.is_bought(key, "国王球"))

        app._toggle_purchased(items[0])
        root.update()
        check("勾选后进入已购买区",
              len(app._cards) == 1 and len(app._bought_cards) == 1,
              f"{len(app._cards)}/{len(app._bought_cards)}")
        check("已购买区是国王球",
              app._bought_cards[0].item.name == "国王球",
              app._bought_cards[0].item.name)
        check("勾选状态已写入磁盘", purchases.is_bought(key, "国王球"))

        app._toggle_purchased(items[0])
        root.update()
        check("取消后回到在售区",
              len(app._cards) == 2 and not app._bought_cards,
              f"{len(app._cards)}/{len(app._bought_cards)}")
        check("取消后不再记录", not purchases.is_bought(key, "国王球"))
    finally:
        aw.fetch_snapshot = original_fetch
        if app is not None:
            try:
                purchases.clear_round(app._round_key(snap))
            except Exception:      # noqa: BLE001
                pass
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_history_settings():
    """历史开关与目录：解析、回退、迁移、配置容错。"""
    import json
    import tempfile
    from datetime import datetime
    from pathlib import Path

    from roco.core import history
    from roco.config import Config, default_history_dir, resolve_history_dir

    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "h"
        cfg = Config()
        cfg.history_dir = str(target)
        path, warning = resolve_history_dir(cfg)
        check("自定义历史目录可用", path == target and not warning, f"{path}/{warning}")
        check("目录会被自动创建", target.is_dir())

        # 拿一个"文件"当目录：必须回退默认并给出提示，而不是抛异常
        blocker = Path(folder) / "blocker.txt"
        blocker.write_text("x", encoding="utf-8")
        cfg.history_dir = str(blocker)
        path, warning = resolve_history_dir(cfg)
        check("不可用目录回退默认", path == default_history_dir() and bool(warning), warning)

        # 切换目录时迁移旧记录
        src, dst = Path(folder) / "src", Path(folder) / "dst"
        src.mkdir(); dst.mkdir()
        snap = MerchantSnapshot(
            items=[MerchantItem("国王球", 160000, 1, rounds=(2,))],
            status="open", round_index=2,
            fetched_at=datetime(2026, 10, 25, 13, 0),
        )
        history.record(snap, path=history.history_path(src),
                       now=datetime(2026, 10, 25, 13, 0))
        check("切换目录会搬走旧记录", history.migrate(src, dst))
        check("新目录能读到旧记录", bool(history.load(history.history_path(dst))))
        check("旧目录文件已移走", not history.history_path(src).exists())

        # 手改配置：字符串 "false" 必须识别为关闭（bool("false") 会是 True）
        raw = {"history_enabled": "false", "history_dir": 123}
        cfg_path = Path(folder) / "config.json"
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        loaded = Config.load(cfg_path)
        check("字符串 false 能识别为关闭", loaded.history_enabled is False,
              str(loaded.history_enabled))
        check("非字符串目录回落为空", loaded.history_dir == "", repr(loaded.history_dir))


def test_history_disabled_flow():
    """端到端：关闭历史后不写盘也不显示；重新打开后写入自定义目录。"""
    import tempfile
    from pathlib import Path

    import roco.ui.app_window as aw

    snap = MerchantSnapshot(
        items=[MerchantItem("国王球", 160000, 1, rounds=(2,))],
        status="open", round_index=2, expected_round=2,
        source_label="测试渠道", fetched_at=datetime.now(),
    )
    original_fetch = aw.fetch_snapshot
    aw.fetch_snapshot = lambda _config: (snap, [])
    root = tk.Tk()
    root.withdraw()
    app = None
    with tempfile.TemporaryDirectory() as folder:
        cfg = Config()
        cfg.history_enabled = False
        cfg.history_dir = folder
        cfg.notify_enabled = False
        cfg.verify_egress = False
        try:
            app = aw.MerchantApp(root, cfg)
            app._watcher.forget()
            app._shelf_state_key = None

            def pump_until(condition, timeout=12):
                end = time.time() + timeout
                while time.time() < end:
                    root.update()
                    time.sleep(0.01)
                    if condition():
                        return True
                return False

            ok = pump_until(lambda: app.snapshot is not None and bool(app._cards))
            check("关闭历史后仍能正常查询", ok)
            check("关闭历史后不写文件",
                  not (Path(folder) / "history.json").exists())
            check("关闭历史后没有历史面板", app._history_panel is None)

            app.config.history_enabled = True
            app.refresh(reason="手动刷新")
            wrote = pump_until(lambda: (Path(folder) / "history.json").exists())
            check("打开后写入自定义目录", wrote)
            check("打开后有历史面板", app._history_panel is not None)
        finally:
            aw.fetch_snapshot = original_fetch
            try:
                root.destroy()
            except tk.TclError:
                pass


def test_netdirect_proxy_strip():
    """直连层第一道：代理环境变量被剥离，urllib 不再报告系统代理。"""
    import os

    from roco.net import netdirect

    saved = {k: os.environ.get(k) for k in netdirect.PROXY_ENV_KEYS}
    try:
        os.environ["http_proxy"] = "http://127.0.0.1:7890"
        os.environ["https_proxy"] = "http://127.0.0.1:7890"
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"

        import urllib.request

        before = urllib.request.getproxies()
        check("剥离前确实能读到代理", bool(before), str(before))

        removed = netdirect.install()
        check("代理变量被移除", {"http_proxy", "https_proxy"} <= set(removed), str(removed))
        check("环境里不再有代理变量",
              not any(k in os.environ for k in ("http_proxy", "https_proxy")),
              str(netdirect.proxy_env_snapshot()))
        check("getproxies 返回空", urllib.request.getproxies() == {},
              str(urllib.request.getproxies()))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_netdirect_addr():
    """地址判定：fake-ip、中国大陆网段、URL 解析、IP 优选。"""
    from roco.net import netdirect

    check("fake-ip 能识别", netdirect.is_fake_ip("198.18.1.112"))
    check("保留段能识别", netdirect.is_fake_ip("127.0.0.1") and netdirect.is_fake_ip("10.0.0.1"))
    check("真实公网 IP 不算 fake", not netdirect.is_fake_ip("8.8.8.8"))

    check("大陆 IP 判定（电信 DNS）", netdirect.is_cn_ip("223.5.5.5"))
    check("大陆 IP 判定（腾讯 DoH）", netdirect.is_cn_ip("1.12.12.12"))
    check("境外 IP 判定", not netdirect.is_cn_ip("8.8.8.8"))
    check("CDN 不会误判", not netdirect.is_cn_ip("8.8.8.8"))
    check("非法输入不抛异常", not netdirect.is_cn_ip("不是 IP") and not netdirect.is_cn_ip(""))

    scheme, host, path, port = netdirect.split_url(
        "https://example.com:8443/a/b?x=1"
    )
    check("URL 解析", (scheme, host, path, port) == ("https", "example.com", "/a/b?x=1", 8443),
          f"{scheme} {host} {path} {port}")

    try:
        netdirect.split_url("ftp://x/y")
        check("拒绝非 http 协议", False)
    except netdirect.DirectError:
        check("拒绝非 http 协议", True)

    check("优先选大陆 IP",
          netdirect._pick_ip(["8.8.8.8", "223.5.5.5"]) == "223.5.5.5")
    check("显式指定 IP 优先",
          netdirect._pick_ip(["8.8.8.8", "223.5.5.5"], "8.8.8.8") == "8.8.8.8")


def test_netdirect_doh():
    """直连层第二道：DoH 取得的是真实 IP，且能识别被劫持的系统 DNS。"""
    from roco.net import netdirect

    netdirect.install()
    netdirect.clear_dns_cache()

    ips = netdirect.resolve("doh.pub", use_doh=True)
    check("DoH 能解析域名", bool(ips), str(ips))
    check("DoH 结果里没有 fake-ip", all(not netdirect.is_fake_ip(ip) for ip in ips), str(ips))
    check("DoH 结果是大陆 IP", all(netdirect.is_cn_ip(ip) for ip in ips), str(ips))

    # 缓存：第二次不应再发请求（用同一个值即可验证）
    check("解析结果被缓存", netdirect.resolve("doh.pub") == ips)

    report = netdirect.diagnose("doh.pub", timeout=10)
    check("诊断结构完整",
          {"system_ips", "doh_ips", "target_is_cn", "verdict", "advice"} <= set(report),
          str(sorted(report)))
    check("诊断认为腾讯 DoH 在大陆", report["target_is_cn"], str(report["target_ip"]))


def test_clash_rules():
    """直连规则生成：域名与 DoH 引导 IP 都要 DIRECT，且顺序在代理规则之前。"""
    from roco.net import netdirect

    hosts = ["onebiji.com"]
    rules = netdirect.clash_rules(hosts)
    check("规则含域名直连", "DOMAIN-SUFFIX,onebiji.com,DIRECT" in rules, rules[:120])
    check("规则含 DoH 引导直连", "IP-CIDR,223.5.5.5/32,DIRECT,no-resolve" in rules)
    check("规则首行是 rules:",
          rules.strip().splitlines()[0].strip() == "rules:",
          rules.splitlines()[0])
    check("空输入不报错", "rules:" in netdirect.clash_rules([]))

    extracted = netdirect.default_hosts(
        {"a": {"base_url": "https://a.example.com/x"}, "b": {"base_url": ""}}
    )
    check("能从渠道配置提取主机名", extracted == ["a.example.com"], str(extracted))


def test_cn_mirror_provider():
    """大陆反代渠道：未配置时跳过，配好后能识别为大陆。"""
    from roco.config import Config
    from roco.net.providers import build_provider

    config = Config()
    config.providers.setdefault("cn_mirror", {})["base_url"] = ""
    provider = build_provider("cn_mirror", config)
    check("大陆反代为未配置状态", not provider.is_configured())

    order = config.provider_order()
    check("大陆反代排在中文查询器之前", order.index("cn_mirror") < order.index("onebiji"), str(order))

    check("新增网络配置项存在",
          all(hasattr(config, name) for name in
              ("force_direct", "doh_enabled", "prefer_cn_region", "verify_egress")),
          "字段缺失")


def test_route_on_snapshot():
    """快照要带上路由信息，界面才能显示出口。"""
    from roco.core.models import MerchantSnapshot

    snap = MerchantSnapshot(items=[])
    check("快照有 route 字段", isinstance(snap.route, dict), str(type(snap.route)))
    snap.route = {"ip": "223.5.5.5", "is_cn": True}
    check("route 可写入", snap.route["is_cn"] is True)


def test_main_window_geometry():
    """主窗口位置恢复：副屏位置不能被误判为"越界"。"""
    from roco.platform.monitors import enum_monitors
    from roco.ui.app_window import MerchantApp

    screens = enum_monitors()
    root = tk.Tk()
    root.withdraw()
    config = Config.load()

    # 主屏右上角位置（旧实现会判为越界并丢掉）
    if len(screens) >= 2:
        second = screens[1]
        saved = f"1180x700+{second.x + 60}+{second.y + 40}"
    else:
        saved = "1180x700+40+40"
    config.window_geometry = saved

    app = MerchantApp(root, config)
    root.update()
    restored = app._safe_geometry()
    check("主窗口副屏位置可恢复", restored.startswith("1180x700"), f"{saved} -> {restored}")
    if len(screens) >= 2:
        check("恢复后仍在副屏", f"+{screens[1].x + 60}+" in restored, restored)

    # 屏幕已全部不存在的坐标 → 必须回落到主屏，而不是留在虚空里
    config.window_geometry = "1180x700+99999+99999"
    fallback = app._safe_geometry()
    check("失效坐标回落主屏", "99999" not in fallback, fallback)

    config.window_geometry = ""
    app.quit_app()


def test_floating_icon_switch():
    """悬浮窗物品图标开关：开=有图标，关=无图标且其余内容不受影响。"""

    class _StubLoader:
        """只回一个空图片，避免测试打网络。"""

        def load(self, url, callback, size=None):
            callback(None)

        def shutdown(self):
            pass

    from roco.ui.floating_window import FloatingWindow

    root = tk.Tk()
    root.withdraw()
    config = Config()
    config.floating_show_icons = True
    config.floating_geometry = ""

    snapshot = MerchantSnapshot(
        items=[
            MerchantItem("黑晶琉璃", 1000, 100, icon_url="https://example.invalid/a.png",
                         rarity=infer_rarity("黑晶琉璃", 1000)),
            MerchantItem("棱镜球", 3200000, 2, icon_url="https://example.invalid/b.png",
                         rarity=infer_rarity("棱镜球", 3200000)),
        ],
        status="open", round_index=3, next_refresh="2026-10-20 20:00",
    )

    window = FloatingWindow(root, config, loader=_StubLoader())
    window.set_snapshot(snapshot)
    window.update_countdown("01:02:03")
    window.update()

    check("默认显示物品图标", window.show_icons is True)
    check("图标控件已创建", len(window._icon_widgets) == 2, str(len(window._icon_widgets)))

    def labels_in_rows():
        texts = []
        for row in window._rows:
            for child in row.winfo_children():
                if isinstance(child, tk.Label):
                    texts.append(child.cget("text"))
        return texts

    before_labels = labels_in_rows()
    before_pos = (window.winfo_x(), window.winfo_y())
    before_rows = len(window._rows)

    window.set_show_icons(False)
    window.update()

    check("关闭后没有图标控件", len(window._icon_widgets) == 0, str(len(window._icon_widgets)))
    check("关闭后行数不变", len(window._rows) == before_rows, f"{before_rows}->{len(window._rows)}")
    check("关闭后名称与价格仍在", labels_in_rows() == before_labels, str(labels_in_rows()))
    check("关闭后仍显示倒计时", "01:02:03" in window.countdown_label.cget("text"))
    check("关闭后位置不变", (window.winfo_x(), window.winfo_y()) == before_pos,
          f"{before_pos}->{(window.winfo_x(), window.winfo_y())}")
    check("关闭后写入配置", config.floating_show_icons is False)

    window.set_show_icons(True)
    window.update()
    check("重新打开图标恢复", len(window._icon_widgets) == 2, str(len(window._icon_widgets)))
    check("恢复后名称价格一致", labels_in_rows() == before_labels)

    # 收起态也不受影响
    window.toggle_collapse()
    window.update()
    check("收起后仍正常", window.winfo_height() < 60, str(window.winfo_height()))

    window.destroy()
    root.destroy()


def test_tray_icon():
    """托盘图标：创建、菜单可生成、气泡可用、可清理。"""
    from roco.platform import tray as tray_module

    commands = []
    icon = tray_module.TrayIcon(
        "自测托盘", "assets/app.ico",
        lambda: [tray_module.MenuItem("显示主窗口", "show"),
                 tray_module.MenuItem("退出", "quit")],
        lambda cmd: commands.append(cmd),
    )
    ok = icon.start()
    check("托盘图标创建成功", ok and icon.available, f"ok={ok} hwnd={icon.hwnd}")
    check("托盘菜单可生成", len(icon.menu_provider()) == 2)
    if icon.available:
        check("托盘气泡可用", icon.balloon("自测", "这是一条自测气泡"))
    icon.stop()
    check("托盘已清理", not icon.available)


def test_close_to_tray_switch():
    """关闭行为：配置可切换，且托盘不可用时必须能真正退出（不能卡死）。"""
    from roco.ui.app_window import MerchantApp

    root = tk.Tk()
    root.withdraw()
    config = Config.load()
    app = MerchantApp(root, config)
    root.update()

    check("配置含关闭到托盘开关", isinstance(config.close_to_tray, bool))
    check("托盘已随主窗口启动", app.tray is not None and app.tray.available)

    app.hide_to_tray(save_geometry=False)
    root.update()
    check("关闭后窗口隐藏但进程仍活", root.state() == "withdrawn")

    app.show_window()
    root.update()
    check("可从托盘恢复窗口", root.state() != "withdrawn")

    # 关掉开关并撤掉托盘：必须走真正的退出分支，而不是隐藏后无法找回
    config.close_to_tray = False
    app.tray.stop()
    app.tray = None
    root.update_idletasks()
    check("无托盘时允许真正退出", app.config.close_to_tray is False)

    app.quit_app()


def test_frame_pacing():
    """帧节拍：刷新率夹取、定时器精度引用计数、动画可取消且销毁安全。"""
    from roco.platform import display
    from roco.ui import motion

    frame = display.frame_ms()
    check("帧间隔不超过 90fps", frame >= int(1000 / 90), f"frame_ms={frame}")
    check("帧间隔不超过 16ms（低刷屏不更慢）", frame <= 16, f"frame_ms={frame}")
    check("帧节拍描述含刷新率", "Hz" in display.describe(), display.describe())

    # 引用计数：成对持有/归还后不能残留（残留会让系统长期保持 1ms 定时器精度）
    display.hold_precise_timer()
    display.hold_precise_timer()
    display.release_precise_timer()
    display.release_precise_timer()
    check("定时器精度成对归还", not display.precise_timer_held())
    display.release_precise_timer()
    check("多余归还不出负数", not display.precise_timer_held())

    root = tk.Tk()
    root.withdraw()
    canvas = tk.Canvas(root, width=20, height=20)
    anim = motion.Animation(canvas)
    seen: list = []
    done: list = []
    anim.tween(120, 0.0, 1.0, seen.append, on_done=lambda: done.append(True))
    deadline = time.time() + 3
    while anim.running and time.time() < deadline:
        root.update()
        time.sleep(0.004)
    root.update()
    check("补间跑到终点", bool(done) and seen and abs(seen[-1] - 1.0) < 1e-6, f"{seen[-3:]}")
    check("补间进度单调不减", all(b >= a for a, b in zip(seen, seen[1:])))
    check("补间结束归还定时器精度", not display.precise_timer_held())

    # 动画途中销毁控件：异常会被 Tk 吞掉，表现成"界面卡住"，所以必须自动停表
    doomed = tk.Canvas(root, width=20, height=20)
    anim2 = motion.Animation(doomed)
    anim2.tween(400, 0.0, 1.0, lambda _v: None)
    doomed.destroy()
    boom = None
    try:
        for _ in range(20):
            root.update()
            time.sleep(0.004)
    except tk.TclError as exc:      # noqa: PERF203 - 这里就是要捕获
        boom = exc
    check("控件销毁后动画自动停表", boom is None and not anim2.running, str(boom))

    ticker = motion.Ticker(root)
    frames: list = []

    def step(dt: float) -> bool:
        frames.append(dt)
        return len(frames) < 3

    ticker.start(step)
    deadline = time.time() + 3
    while ticker.running and time.time() < deadline:
        root.update()
        time.sleep(0.004)
    check("连续节拍按 step 返回值停止", len(frames) >= 3, f"{len(frames)} 帧")
    check("连续节拍带真实 dt", all(dt >= 0 for dt in frames), str(frames[:3]))
    root.destroy()


def test_widget_items_reuse():
    """动画不重建 Canvas item：按钮 / 状态胶囊 / 滚动条拇指都只改属性。"""
    from roco.ui import theme
    from roco.ui.widgets import ItemCard, PillButton, ScrollFrame, StatusPill

    class _NullLoader:
        def load(self, *args, **kwargs):
            return None

        def prewarm(self, *args, **kwargs):
            return None

    theme.apply("dark", "")
    root = tk.Tk()
    root.geometry("900x600+120+120")

    button = PillButton(root, "测试按钮", style="primary")
    button.pack(pady=4)
    root.update()
    ids = button.find_all()
    button._hover = True
    button._on_enter()
    for _ in range(16):
        root.update()
        time.sleep(0.004)
    check("按钮重绘复用 item", len(ids) == 2 and button.find_all() == ids, f"{ids}")
    check("悬停后确实改了填充色",
          button.itemcget(button._shape, "fill") != theme.SURFACE_2,
          button.itemcget(button._shape, "fill"))

    pill = StatusPill(root)
    pill.pack(pady=4)
    root.update()
    width_before, pill_ids = int(pill["width"]), pill.find_all()
    pill.set_state("换货校验中", theme.WARNING)
    root.update()
    pill.set_state("已打烊", theme.MUTED)
    root.update()
    check("状态胶囊宽度固定", int(pill["width"]) == width_before, f"{width_before}")
    check("状态胶囊复用 item", pill.find_all() == pill_ids, f"{pill_ids}")
    check("状态胶囊文案已更新", pill.itemcget(pill._label, "text") == "已打烊")

    scroll = ScrollFrame(root, bg=theme.BG)
    scroll.pack(fill="both", expand=True)
    for index in range(12):
        ItemCard(
            scroll.body,
            MerchantItem(name=f"道具{index}", price=1000 * (index + 1), limit=1,
                         category="材料", description="测试", rounds=(1, 2, 3, 4)),
            _NullLoader(),
        ).pack(fill="x")
    root.update()
    thumb = scroll._thumb
    scroll.canvas.yview_moveto(0.5)
    root.update()
    scroll.canvas.yview_moveto(0.8)
    root.update()
    check("滚动条拇指复用同一 item",
          scroll._thumb == thumb and len(scroll.bar.find_all()) == 1)
    check("滚动条拇指可见", scroll.bar.itemcget(thumb, "state") == "normal",
          scroll.bar.itemcget(thumb, "state"))
    root.destroy()


def test_floating_drag_contract():
    """悬浮窗拖动：程序化入口语义不变、阈值内不搬窗、回落后仍可拖、位置落盘。"""
    from roco.platform import monitors, window_move
    from roco.ui.floating_window import FloatingWindow

    config = Config()
    config.floating_geometry = ""
    root = tk.Tk()
    root.withdraw()
    window = FloatingWindow(root, config)
    window.update()

    check("拖动阈值来自系统且非零", window_move.drag_threshold() >= 2,
          str(window_move.drag_threshold()))

    class _Event:
        def __init__(self, x: int, y: int) -> None:
            self.x_root = x
            self.y_root = y

    # 1) 程序化调用（自测与回落路径共用）：必须仍然按偏移搬窗口
    window._monitors = monitors.enum_monitors()
    window._drag_offset = (0, 0)
    window._last_pos = None
    window._drag_move(_Event(60, 40))
    window._drag_end()
    window.update()
    check("程序化 _drag_move 仍然生效",
          (window.winfo_x(), window.winfo_y()) == (60, 40),
          f"{(window.winfo_x(), window.winfo_y())}")

    # 2) 阈值内的微小抖动不搬窗口：标题栏的单击/双击必须照旧
    window._system_broken = True          # 强制走应用内路径，别真弹系统移动循环
    window._drag_start(_Event(60, 40))
    window._drag_move(_Event(62, 41))
    window.update()
    check("阈值内抖动不搬窗口", (window.winfo_x(), window.winfo_y()) == (60, 40),
          f"{(window.winfo_x(), window.winfo_y())}")
    window._drag_end()

    # 3) 超过阈值后应用内拖动仍然有效（系统路径不可用时就是靠它）
    window._drag_start(_Event(60, 40))
    window._drag_move(_Event(200, 150))
    window._drag_end()
    window.update()
    check("回落后仍可拖动", (window.winfo_x(), window.winfo_y()) == (200, 150),
          f"{(window.winfo_x(), window.winfo_y())}")
    check("拖动结束写入几何", config.floating_geometry.endswith("+200+150"),
          config.floating_geometry)

    # 4) 拖动中改尺寸先记账，松手再应用（否则窗口会被拽走）
    window._press_root = (10, 10)
    window._pending_size = None
    window._apply_size(150)
    check("拖动中改尺寸先记账", window._pending_size == 150, str(window._pending_size))
    window._press_root = None
    window._drag_offset = None
    window._finish_drag()
    window.update()
    check("松手后应用推迟的尺寸", window.winfo_height() == 150, str(window.winfo_height()))

    window.destroy()
    root.destroy()


def test_log_viewer_highlight():
    """日志查看器：ERROR 与 WARNING 行都要着色（1.9 把 WARNING 字面量误替换过）。"""
    from roco import applog
    from roco.ui import theme
    from roco.ui.log_viewer import LogViewer

    content = (
        "2026-10-25 21:00:00 INFO 一切正常\n"
        "2026-10-25 21:00:01 ERROR 出错了\n"
        "2026-10-25 21:00:02 WARNING 注意一下\n"
    )
    original = applog.tail
    applog.tail = lambda max_lines=600, errors_only=False: content   # noqa: ARG005
    theme.apply("dark", "")
    root = tk.Tk()
    root.withdraw()
    try:
        viewer = LogViewer(root)
        viewer.update()
        check("日志 ERROR 行着色", bool(viewer.text.tag_ranges("error")))
        check("日志 WARNING 行着色", bool(viewer.text.tag_ranges("warning")))
        viewer.destroy()
    finally:
        applog.tail = original
        root.destroy()


def test_scroll_pacing():
    """快速滑动：拇指拖动每帧只搬一次；帧超预算时自动降速（帧同步闭环）。"""
    from roco.ui import theme
    from roco.ui.widgets import ItemCard, ScrollFrame

    class _NullLoader:
        def load(self, *args, **kwargs):
            return None

        def prewarm(self, *args, **kwargs):
            return None

    class _Ev:
        def __init__(self, y: int) -> None:
            self.y = y

    theme.apply("dark", "")
    root = tk.Tk()
    root.geometry("900x600+160+160")
    scroll = ScrollFrame(root, bg=theme.BG)
    scroll.pack(fill="both", expand=True)
    for index in range(24):
        ItemCard(
            scroll.body,
            MerchantItem(name=f"道具{index}", price=1000 * (index + 1), limit=1,
                         category="材料", description="测试", rounds=(1, 2, 3, 4)),
            _NullLoader(),
        ).pack(fill="x")
    for _ in range(8):
        root.update()
        time.sleep(0.01)

    moves = {"n": 0}
    original = scroll.canvas.yview_moveto

    def counting(value):
        moves["n"] += 1
        return original(value)

    scroll.canvas.yview_moveto = counting
    try:
        scroll._on_bar_press(_Ev(0))
        for step in range(12):        # 模拟 1000Hz 鼠标一帧内送来的十几个事件
            scroll._on_bar_drag(_Ev(step * 4))
        scroll._on_bar_release()
        root.update()
        # 一帧内最多两次：节拍启动时的一次 + 本帧推进一步
        check("拇指拖动每帧只搬一次", moves["n"] <= 2, f"{moves['n']} 次")
    finally:
        scroll.canvas.yview_moveto = original
        scroll._cancel_scroll_anim()

    pace_before = scroll._pace
    for _ in range(3):
        scroll._adapt_pace(0.060)      # 帧被拖长到 60ms（预算才 11ms）
    check("帧超预算时自动降速", scroll._pace < pace_before, f"{scroll._pace:.2f}")
    for _ in range(12):
        scroll._adapt_pace(0.004)      # 帧回到预算内
    check("回到预算内后恢复速度", scroll._pace > 0.6, f"{scroll._pace:.2f}")

    scroll._cancel_scroll_anim()
    check("取消滚动后不再按拖动速度跑", scroll._dragging is False)
    root.destroy()


def test_winpaint_freeze():
    """冻结重绘：可嵌套、异常也恢复、解冻后事件循环照常。"""
    from roco.platform import winpaint

    root = tk.Tk()
    root.geometry("320x200+200+200")
    root.update()

    watch = winpaint.supported()
    with winpaint.frozen_redraw(root):
        first = winpaint.frozen_depth(root)
        with winpaint.frozen_redraw(root):
            nested = winpaint.frozen_depth(root)
        back_to_outer = winpaint.frozen_depth(root)
    final = winpaint.frozen_depth(root)
    check("冻结层数可嵌套", (not watch) or (first == 1 and nested == 2),
          f"{first}/{nested}")
    check("内层退出不解冻", (not watch) or back_to_outer == 1, str(back_to_outer))
    check("外层退出恢复重绘", final == 0, str(final))

    boom = False
    try:
        with winpaint.frozen_redraw(root):
            raise RuntimeError("构建失败")
    except RuntimeError:
        boom = True
    check("构建异常也恢复重绘", boom and winpaint.frozen_depth(root) == 0)

    alive = True
    try:
        root.update()          # 解冻后必须还能正常泵事件（不是 WS_EX_COMPOSITED 那种死锁）
        root.update_idletasks()
    except tk.TclError:
        alive = False
    check("解冻后事件循环照常", alive)
    root.destroy()


def test_image_loader_waiters():
    """加载中的图标：后到的回调必须排队，不能丢（历史面板图标"不出来"的根因）。"""
    from roco.services.cache import ImageLoader

    class _NoCache:
        def lookup(self, url):
            return None

        def download(self, url):
            return None

    root = tk.Tk()
    root.withdraw()
    loader = ImageLoader(root, _NoCache(), 16)
    got: list = []
    url = "https://example.invalid/a.png"
    key = f"{url}@16"
    loader._waiters[key] = []            # 模拟"已经在路上"
    loader.load(url, got.append)
    check("加载中的回调未丢弃", len(loader._waiters.get(key, [])) == 1,
          str(len(loader._waiters.get(key, []))))
    loader.load(url, got.append)
    check("多个回调都排队", len(loader._waiters.get(key, [])) == 2,
          str(len(loader._waiters.get(key, []))))
    loader._finish(url, 16, None)        # 下载失败也要唤醒，否则卡片永远等
    check("完成时唤醒全部回调", len(got) == 2, str(len(got)))
    loader.shutdown()
    root.destroy()


def test_history_expand_atomic():
    """展开历史：先预取图标、再建卡片，冻结重绘后一次性呈现，且不残留冻结。"""
    from roco.core.history import HistoryRound
    from roco.platform import winpaint
    from roco.ui import theme
    from roco.ui.history_panel import HistoryPanel

    class _RecordingLoader:
        """记录调用顺序的假加载器：prewarm 必须先于任何 load。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def prewarm(self, urls, size=None):
            self.calls.append(("prewarm", list(urls)))

        def load(self, url, on_ready, size=None):
            self.calls.append(("load", url))
            on_ready(None)

    theme.apply("dark", "")
    root = tk.Tk()
    root.geometry("1000x620+140+140")
    loader = _RecordingLoader()
    panel = HistoryPanel(root, loader=loader, expanded=False)
    panel.pack(fill="x")
    items = tuple(
        MerchantItem(name=f"道具{i}", price=1000 * (i + 1), icon_url=f"u{i}", rounds=(1,))
        for i in range(3)
    )
    panel.set_rounds([
        HistoryRound(date="2026-10-25", round_index=r, items=items) for r in (4, 3, 2)
    ])
    root.update()

    loader.calls.clear()
    panel.toggle()
    built_at_click = panel._build_index        # 点击返回时只建了第一轮
    root.update()
    kinds = [kind for kind, _value in loader.calls]
    check("展开时先预取图标", bool(kinds) and kinds[0] == "prewarm", str(kinds[:3]))
    prefetched = loader.calls[0][1] if kinds else []
    check("预取覆盖全部图标",
          len(prefetched) == 9 and set(prefetched) == {"u0", "u1", "u2"},
          f"{len(prefetched)} 个")
    check("预取发生在建卡片之前",
          "prewarm" in kinds and "load" in kinds and kinds.index("prewarm") < kinds.index("load"),
          str(kinds))

    check("点击返回时只建了第一轮", built_at_click == 1, str(built_at_click))
    check("第一轮已有内容", len(panel._body.winfo_children()) > 0,
          str(len(panel._body.winfo_children())))

    deadline = time.time() + 4
    while time.time() < deadline:
        root.update()
        time.sleep(0.02)
        if not panel._build_job:
            break
    root.update()
    check("轮次陆续补齐", panel._build_index >= 3, str(panel._build_index))
    check("展开后冻结已恢复", winpaint.frozen_depth(panel) == 0,
          str(winpaint.frozen_depth(panel)))
    panel.destroy()
    root.destroy()


def main():
    with preserve_app_files(), frozen_clock():
        _run_all()

    failed = 0
    for status, name, detail in results:
        if status == FAIL:
            failed += 1
        print(f"[{status}] {name}" + (f"  -> {detail}" if detail and status == FAIL else ""))
    print(f"\n共 {len(results)} 项，失败 {failed} 项")
    return 1 if failed else 0


def _run_all():
    for fn in (test_numerals, test_rarity, test_time_parsing, test_rounds,
               test_scheduler, test_provider_fallback, test_provider_bad_url,
               test_notification_dedupe, test_render_states, test_icon_cache,
               test_cache_path, test_autostart, test_native_notify,
               test_single_instance, test_floating_window, test_config_roundtrip,
               test_notify_fallback_chain, test_monitor_math,
               test_floating_multimonitor, test_logging, test_tk_error_hook,
               test_rounds_parsing, test_round_lag_detection, test_round_retry_flow,
               test_shelf_freshness_rules, test_all_day_recheck_flow,
               test_provider_onebiji, test_price_units,
               test_floating_refresh_button, test_floating_refresh_while_hidden,
               test_history_store, test_purchase_marks, test_catalog_store,
               test_notify_item_selection, test_midnight_shelf,
               test_purchase_and_history_widgets, test_purchase_area_flow,
               test_history_settings, test_history_disabled_flow,
               test_give_up_policy, test_publish_script,
               test_settings_recheck_fields,
               test_netdirect_proxy_strip, test_netdirect_addr, test_netdirect_doh,
               test_clash_rules, test_cn_mirror_provider, test_route_on_snapshot,
               test_main_window_geometry,
               test_floating_icon_switch, test_tray_icon, test_close_to_tray_switch,
               test_frame_pacing, test_widget_items_reuse,
               test_floating_drag_contract, test_log_viewer_highlight,
               test_scroll_pacing, test_winpaint_freeze,
               test_image_loader_waiters, test_history_expand_atomic):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
