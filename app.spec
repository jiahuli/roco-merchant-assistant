# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、带图标。

构建：
    scripts\\build.bat              （等价于 .buildenv\\Scripts\\pyinstaller.exe --noconfirm app.spec）

产物：
    dist\\远行商人助手.exe
"""

block_cipher = None

hidden = [
    "ui", "ui.theme", "ui.widgets", "ui.app_window",
    "ui.settings_dialog", "ui.floating_window",
    "providers", "rarity", "numerals", "cache", "scheduler",
    "notifier", "notifier_native", "autostart", "single_instance", "models", "config",
    "monitors", "tray", "netdirect",
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
