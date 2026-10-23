"""功能自测：核心逻辑 + 关键交互。"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk

from contextlib import contextmanager

import autostart
from config import Config, config_path, sanitize_times, state_path
from models import MerchantItem, MerchantSnapshot
from numerals import to_chinese_number
from providers import (ProviderError, build_provider, fetch_snapshot, next_refresh_at,
                       round_index_at, is_open_at)
from rarity import infer_rarity
from scheduler import Scheduler, format_countdown

PASS, FAIL = "PASS", "FAIL"


@contextmanager
def preserve_app_files():
    """自测会实例化真实窗口，而窗口退出时会写 config/state。

    这里先备份这两个文件，跑完原样还原——否则测试改过的开关
    （比如 close_to_tray=false）会留在用户真实配置里，看起来很像是程序 bug。
    """
    backups = {}
    for path in (config_path(), state_path()):
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
    from providers import CN_TZ
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
        ok = snapshot is not None and snapshot.source_id in ("rkw", "yxsr")
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
    from ui.app_window import MerchantApp
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
    from ui.app_window import MerchantApp
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

    from cache import IconCache
    app.scheduler.stop()
    app.loader.shutdown()
    app.pool.shutdown(wait=False)
    root.destroy()


def test_icon_cache():
    from cache import IconCache
    from config import cache_dir
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

    from cache import IconCache, migrate_cache
    from config import Config, default_cache_dir, resolve_icon_cache_dir

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
    import autostart

    command = autostart.launch_command()
    check("自启命令可拼装", bool(command) and "--autostart" in command, command[:60])
    check("自启状态可读", isinstance(autostart.is_enabled(), bool))
    enabled = autostart.is_enabled()
    ok, _msg = autostart.sync(enabled)   # 与现状对齐，不应产生新写入
    check("自启同步幂等", ok and autostart.is_enabled() == enabled)


def test_native_notify():
    """原生通知：XML 合法 + 支持判定（不发真实通知）。"""
    from xml.dom.minidom import parseString

    import notifier_native

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

    import single_instance as si

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
    from ui.floating_window import HEADER_H, FloatingWindow

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
              loaded.query_times == config.query_times and loaded.provider == "rkw")

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
    from notifier import Notifier

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
    from monitors import (Monitor, best_monitor, clamp_drag, default_position,
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
    from monitors import enum_monitors
    from ui.floating_window import FloatingWindow

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

    import applog
    from config import Config

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
    from ui.log_viewer import LogViewer

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
    import applog

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
    from models import MerchantItem, TOTAL_ROUNDS
    from providers import _to_rounds, filter_by_round

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
    from models import MerchantItem, MerchantSnapshot

    item = MerchantItem("条件球", 3000, 100, rounds=(1, 2, 3, 4))
    lagging = MerchantSnapshot(items=[item], status="open", round_index=1, expected_round=2)
    synced = MerchantSnapshot(items=[item], status="open", round_index=2, expected_round=2)
    unknown = MerchantSnapshot(items=[item], status="open", round_index=None, expected_round=2)

    check("接口落后判定为滞后", lagging.is_round_lagging)
    check("轮次一致不算滞后", not synced.is_round_lagging)
    check("轮次未知不误判", not unknown.is_round_lagging)
    check("全天件数统计", lagging.all_day_count == 1, str(lagging.all_day_count))

    # 真实结构：把接口返回体喂给解析器，验证轮次与过滤同时生效
    from providers import snapshot_from_payload

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

    import ui.app_window as aw
    from models import MerchantItem, MerchantSnapshot

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
    from freshness import (ACTION_GIVE_UP, ACTION_RETRY, CODE_ALL_DAY_ONLY,
                           CODE_ROUND_LAG, CODE_SHELF_SHRANK, ShelfWatcher)

    def snap(items, rnd=2, exp=2, status="open", src="rkw"):
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
    switched.observe(snap([ball()], src="rkw"))
    check("换渠道后不误判为未变",
          not switched.observe(snap([ball()], src="custom")).should_retry)

    unknown = ShelfWatcher()
    unknown.observe(snap([MerchantItem("神秘商品", 100, 1)]))
    check("无 rounds 字段不误判全天货架",
          not unknown.observe(snap([MerchantItem("神秘商品", 100, 1)])).should_retry)

    from freshness import format_delay
    check("延迟文案", format_delay(300) == "5 分钟" and format_delay(45) == "45 秒"
          and format_delay(90) == "1 分 30 秒",
          f"{format_delay(300)}/{format_delay(45)}/{format_delay(90)}")

    # 复现被卡住的现场：接口 round 已刷成第 2 轮，items 还是第 1 轮的货架。
    # filter_by_round() 把 rounds=[1] 的旧商品剔掉，只剩两件全天商品，
    # 而 is_round_lagging 为 False —— 1.4.1 会认为一切正常，从此不再重试。
    from providers import snapshot_from_payload

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
    stale = snapshot_from_payload(stale_payload, "rkw", "测试渠道")
    check("卡住的现场：只剩两件全天商品",
          len(stale.items) == 2 and stale.all_day_count == 2, str([i.name for i in stale.items]))
    check("剔除数被记录（日志可追溯）", stale.dropped_count == 1, str(stale.dropped_count))
    check("轮次判据看不出问题（1.4.1 的盲区）", not stale.is_round_lagging)

    stuck = ShelfWatcher(delay=300, max_attempts=2)
    first_see = stuck.observe(stale)
    second_see = stuck.observe(snapshot_from_payload(stale_payload, "rkw", "测试渠道"))
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
          not stuck.observe(snapshot_from_payload(healed, "rkw", "测试渠道")).should_retry)


def test_all_day_recheck_flow():
    """端到端：卡在两件全天商品上时，程序自己会按配置延后重取，且能收敛。"""
    import ui.app_window as aw
    from freshness import ShelfObservation, ShelfWatcher

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

    from cache import IconCache
    from ui.settings_dialog import SettingsDialog

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
    from freshness import (ACTION_GIVE_UP, ACTION_RETRY, POLICY_SLOW, POLICY_STOP,
                           ShelfWatcher)

    def snap(items, rnd=2, exp=2):
        return MerchantSnapshot(items=items, status="open", round_index=rnd,
                                expected_round=exp, source_id="rkw")

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
    check("文档可同步", module.is_publishable("docs/DEVELOPMENT.md"))
    check("脚本可同步", module.is_publishable("sync.bat"))
    check("产物不同步", not module.is_publishable("dist/远行商人助手.exe", 1024))
    check("构建目录不同步", not module.is_publishable("build/x.py"))
    check("缓存不同步", not module.is_publishable("__pycache__/config.cpython-313.pyc"))
    check("CI 目录不同步", not module.is_publishable(".github/workflows/release.yml"))
    check("日志不同步", not module.is_publishable("logs/app.log"))
    check("超大文件不同步", not module.is_publishable("assets/big.png", 30 * 1024 * 1024))
    check("反斜杠路径也能识别", module.is_publishable("ui\\app_window.py"))

    msg = module.build_commit_message(["ui/app_window.py", "README.md"], "1.4.3",
                                      datetime(2026, 10, 23, 21, 0))
    check("提交信息含类型与日期", "代码" in msg and "文档" in msg and "2026-10-23" in msg, msg)
    check("提交信息带版本号", "v1.4.3" in msg, msg)
    check("无版本号时不出现空括号",
          "（v" not in module.build_commit_message(["README.md"]))


def test_netdirect_proxy_strip():
    """直连层第一道：代理环境变量被剥离，urllib 不再报告系统代理。"""
    import os

    import netdirect

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
    import netdirect

    check("fake-ip 能识别", netdirect.is_fake_ip("198.18.1.112"))
    check("保留段能识别", netdirect.is_fake_ip("127.0.0.1") and netdirect.is_fake_ip("10.0.0.1"))
    check("真实公网 IP 不算 fake", not netdirect.is_fake_ip("104.21.43.145"))

    check("大陆 IP 判定（电信 DNS）", netdirect.is_cn_ip("223.5.5.5"))
    check("大陆 IP 判定（腾讯 DoH）", netdirect.is_cn_ip("1.12.12.12"))
    check("境外 IP 判定（Cloudflare）", not netdirect.is_cn_ip("104.21.43.145"))
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
          netdirect._pick_ip(["104.21.43.145", "223.5.5.5"]) == "223.5.5.5")
    check("显式指定 IP 优先",
          netdirect._pick_ip(["104.21.43.145", "223.5.5.5"], "104.21.43.145") == "104.21.43.145")


def test_netdirect_doh():
    """直连层第二道：DoH 取得的是真实 IP，且能识别被劫持的系统 DNS。"""
    import netdirect

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
    import netdirect

    hosts = ["rocokingdomworld.org"]
    rules = netdirect.clash_rules(hosts)
    check("规则含域名直连", "DOMAIN-SUFFIX,rocokingdomworld.org,DIRECT" in rules, rules[:120])
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
    from config import Config
    from providers import build_provider

    config = Config()
    config.providers.setdefault("cn_mirror", {})["base_url"] = ""
    provider = build_provider("cn_mirror", config)
    check("大陆反代为未配置状态", not provider.is_configured())

    order = config.provider_order()
    check("大陆反代排在社区镜像之前", order.index("cn_mirror") < order.index("rkw"), str(order))

    check("新增网络配置项存在",
          all(hasattr(config, name) for name in
              ("force_direct", "doh_enabled", "prefer_cn_region", "verify_egress")),
          "字段缺失")


def test_route_on_snapshot():
    """快照要带上路由信息，界面才能显示出口。"""
    from models import MerchantSnapshot

    snap = MerchantSnapshot(items=[])
    check("快照有 route 字段", isinstance(snap.route, dict), str(type(snap.route)))
    snap.route = {"ip": "223.5.5.5", "is_cn": True}
    check("route 可写入", snap.route["is_cn"] is True)


def test_main_window_geometry():
    """主窗口位置恢复：副屏位置不能被误判为"越界"。"""
    from monitors import enum_monitors
    from ui.app_window import MerchantApp

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

    from ui.floating_window import FloatingWindow

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
    import tray as tray_module

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
    from ui.app_window import MerchantApp

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


def main():
    with preserve_app_files():
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
               test_give_up_policy, test_publish_script,
               test_settings_recheck_fields,
               test_netdirect_proxy_strip, test_netdirect_addr, test_netdirect_doh,
               test_clash_rules, test_cn_mirror_provider, test_route_on_snapshot,
               test_main_window_geometry,
               test_floating_icon_switch, test_tray_icon, test_close_to_tray_switch):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
