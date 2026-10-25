# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、带图标。

构建：
    scripts\\build.bat              （等价于 .buildenv\\Scripts\\pyinstaller.exe --noconfirm app.spec）

产物：
    dist\\远行商人助手.exe
"""

block_cipher = None

hidden = [
    "roco", "roco.ui", "roco.ui.theme", "roco.ui.widgets", "roco.ui.app_window",
    "roco.ui.settings_dialog", "roco.ui.floating_window",
    # 1.9.1：帧节拍与动画器（UI 层唯一的"平台感"依赖，漏了会退化成写死的 60fps 节拍）
    "roco.ui.motion",
    "roco.net.providers", "roco.net.netdirect", "roco.net.catalog",
    "roco.core.rarity", "roco.core.numerals", "roco.core.models",
    "roco.core.freshness", "roco.core.history", "roco.core.purchases",
    "roco.services.cache", "roco.services.scheduler",
    "roco.platform.notifier", "roco.platform.notifier_native",
    "roco.platform.autostart", "roco.platform.single_instance",
    "roco.platform.monitors", "roco.platform.tray", "roco.platform.theme_listener",
    "roco.platform.memory", "roco.platform.win11_style",
    # 1.9.1：刷新率/定时器精度、系统移动循环
    "roco.platform.display", "roco.platform.window_move",
    # 1.9.1：历史展开时冻结重绘，避免"空白卡片→文字→图标"分步出现
    "roco.platform.winpaint",
    "roco.config", "roco.applog",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/app.ico", "assets"),
           ("data/cn_ipv4.txt", "data"),
           # 关注物品的离线种子清单；漏了它 exe 里的选择器会只剩"本机见过"的条目
           ("data/merchant_items.json", "data")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "numpy", "pytest", "setuptools", "pip"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="远行商人助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 桌面程序：不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/app.ico",
    version="packaging/version_info.txt",
)
