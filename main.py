"""远行商人助手 —— 程序入口。

运行：
    python main.py                 源码运行
    远行商人助手.exe                打包后运行（含 --autostart / rocomesh:// 参数）

支持的启动参数
--------------
--autostart        由开机自启拉起（行为与正常启动一致，便于日志区分）
rocomesh://show    点系统通知里的按钮时由协议唤醒，会把已有窗口叫到前台
"""
from __future__ import annotations

import os
import sys


def fail(message: str) -> int:
    print("\n" + message + "\n")
    try:
        input("按回车键退出…")
    except (EOFError, KeyboardInterrupt):
        pass
    return 1


def main() -> int:
    if sys.version_info < (3, 9):
        return fail(
            f"当前 Python 版本为 {sys.version.split()[0]}，本程序需要 3.9 及以上。\n"
            "请到 https://www.python.org/downloads/ 安装新版后重试。"
        )

    try:
        import tkinter  # noqa: F401
    except ImportError:
        return fail(
            "当前 Python 没有包含图形界面库 tkinter。\n"
            "Windows 请重新运行安装包并勾选「tcl/tk and IDLE」；\n"
            "Linux 可执行：sudo apt install python3-tk"
        )

    base = os.path.dirname(os.path.abspath(__file__))
    if base not in sys.path:
        sys.path.insert(0, base)

    import tkinter as tk

    from roco import applog
    from roco.platform import autostart
    from roco.net import netdirect
    from roco.platform import notifier_native
    from roco.platform import single_instance
    from roco.config import Config
    from roco.ui.app_window import MerchantApp

    wants_show = any("rocomesh:" in arg.lower() for arg in sys.argv[1:])

    # 已经有实例在跑：叫醒它，然后自己退出，避免开出第二个窗口
    if not single_instance.acquire():
        single_instance.signal_existing()
        return 0

    # 让高分屏下的文字不糊
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    # 注册通知身份（AppUserModelID + rocomesh:// 协议），仅在变化时写注册表
    try:
        notifier_native.register_identity()
    except Exception:
        pass

    config = Config.load()

    # 第一件事就把代理链路断开：代理变量、系统代理、fake-ip 都在后面统一处理
    if config.force_direct:
        netdirect.install()

    # 先起日志：后面任何一步出错都能留证据（打包成无控制台 exe 后尤其重要）
    try:
        applog.setup(config)
    except Exception:
        pass  # 日志起不来也不能挡住主流程

    root = tk.Tk()
    app = MerchantApp(root, config)

    # 监听"请露面"信号（来自通知点击 / 第二次启动）
    single_instance.listen(app.request_show)
    if wants_show:
        root.after(320, app.show_from_background)
    elif autostart.is_autostart_launch() and config.close_to_tray and app.tray is not None:
        # 开机自启：直接驻留托盘，不弹窗打扰
        app.hide_to_tray(save_geometry=False)
        root.after(1500, lambda: app.tray and app.tray.balloon(
            "远行商人助手已在后台运行", "双击托盘图标可打开窗口，右键可退出。"))

    try:
        root.mainloop()
    finally:
        single_instance.stop_listener()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
