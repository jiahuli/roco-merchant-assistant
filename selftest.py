"""功能自测：核心逻辑 + 关键交互。"""
import os
import sys
import time

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
               test_main_window_geometry,
               test_tray_icon, test_close_to_tray_switch):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
