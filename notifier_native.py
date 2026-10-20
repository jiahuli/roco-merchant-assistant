"""Windows 原生通知（Toast，落在系统通知中心）。

为什么这么实现
--------------
Python 的 `winsdk` / `winrt-*` 在 3.14 上还没有现成轮子，自己编译代价太高；
而 Windows 10/11 自带的 WinRT Toast API 可以通过 PowerShell 直接调用。
于是这里用「`-EncodedCommand` 内联脚本 + 临时 XML 文件」的方式发送：

* 不落 .ps1 文件 —— 绕过"禁止运行脚本"的执行策略（很多机器默认 Restricted）；
* XML 写临时文件再用 `[IO.File]::ReadAllText` 读入 —— 彻底避免中文引号、
  特殊字符在命令行里的转义地狱；
* `-WindowStyle Hidden` + `CREATE_NO_WINDOW` —— 不闪黑框。

通知点"查看货架"时通过自定义 URI 协议回到本程序（见 `register_identity`），
再由单实例机制交给已经在跑的窗口（见 `single_instance.py`）。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

AUMID = "RocoMerchant.Assistant"
DISPLAY_NAME = "远行商人助手"
URI_SCHEME = "rocomesh"
SHOW_URI = f"{URI_SCHEME}://show"

_CREATE_NO_WINDOW = 0x08000000
_last_error = ""


def powershell_path() -> Path:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def is_supported() -> bool:
    return os.name == "nt" and powershell_path().exists()


def last_error() -> str:
    return _last_error


# ---------------------------------------------------------------------------
# 注册应用身份
# ---------------------------------------------------------------------------
def _exe_path() -> str:
    """用于注册表里的可执行文件路径。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" "{Path(__file__).resolve().parent / "main.py"}"'


def _ensure_value(key, name: str, value: str) -> bool:
    """仅在值不同时写入，避免每次启动都改注册表。"""
    import winreg

    try:
        old, _ = winreg.QueryValueEx(key, name)
        if str(old) == value:
            return False
    except OSError:
        pass
    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    return True


def register_identity(icon_path: str = "") -> bool:
    """注册 AppUserModelID 与 URI 协议，返回是否有改动。

    * AppUserModelId → 通知中心里显示的名字与图标
    * rocomesh://    → 点击通知里的按钮时回到本程序
    """
    if os.name != "nt":
        return False
    import winreg

    changed = False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\AppUserModelId\{AUMID}") as key:
            changed |= _ensure_value(key, "DisplayName", DISPLAY_NAME)
            icon = icon_path
            if not icon and getattr(sys, "frozen", False):
                icon = sys.executable
            if icon:
                changed |= _ensure_value(key, "IconUri", icon)

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{URI_SCHEME}") as key:
            changed |= _ensure_value(key, "", f"URL:{DISPLAY_NAME}")
            changed |= _ensure_value(key, "URL Protocol", "")

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{URI_SCHEME}\shell\open\command") as key:
            changed |= _ensure_value(key, "", _exe_path())
    except OSError:
        return False
    return changed


# ---------------------------------------------------------------------------
# 发送通知
# ---------------------------------------------------------------------------
def build_xml(title: str, body: str, *, launch: str = SHOW_URI,
              button: str = "查看货架", silent: bool = False) -> str:
    audio = '<audio silent="true"/>' if silent else ""
    actions = (
        "<actions>"
        f'<action content="{escape(button)}" activationType="protocol" '
        f'arguments="{escape(launch)}"/>'
        '<action content="忽略" activationType="system" arguments="dismiss"/>'
        "</actions>"
    )
    return (
        f'<toast activationType="protocol" launch="{escape(launch)}" duration="long">'
        "<visual><binding template=\"ToastGeneric\">"
        f"<text>{escape(title)}</text>"
        f"<text>{escape(body)}</text>"
        "</binding></visual>"
        f"{audio}{actions}"
        "</toast>"
    )


# 注意：
#   * 不要加 -WindowStyle Hidden —— 配合 CREATE_NO_WINDOW 会挂死（实测 8s 超时）
#   * 不要设 [Console]::OutputEncoding —— 无控制台时它会额外阻塞约 5s
#   * 反正走 -EncodedCommand（UTF-16LE），输出只有 ASCII 的 'OK'
_PS_TEMPLATE = """
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml([System.IO.File]::ReadAllText('__XML__', [System.Text.Encoding]::UTF8))
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('__AUMID__').Show($toast)
Write-Output 'OK'
"""


def send(title: str, body: str, *, silent: bool = False, timeout: int = 20) -> bool:
    """发送一条原生通知；失败返回 False 且不抛异常。"""
    global _last_error
    if not is_supported():
        _last_error = "当前系统不支持 Windows 通知"
        return False

    xml = build_xml(title, body, silent=silent)
    xml_file = None
    try:
        handle, xml_file = tempfile.mkstemp(prefix="roco_toast_", suffix=".xml")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(xml)

        script = _PS_TEMPLATE.replace("__XML__", xml_file.replace("'", "''")) \
                             .replace("__AUMID__", AUMID)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

        completed = subprocess.run(
            [str(powershell_path()), "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode == 0 and b"OK" in completed.stdout:
            _last_error = ""
            return True
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", "replace")
        _last_error = detail.strip().splitlines()[-1] if detail.strip() else "未知错误"
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        _last_error = f"{type(exc).__name__}: {exc}"
    finally:
        if xml_file:
            try:
                os.unlink(xml_file)
            except OSError:
                pass
    return False


def send_test() -> tuple[bool, str]:
    """设置里的"发一条测试通知"。"""
    ok = send("通知测试", "出现稀有物品时，就会像这样提醒你。")
    if ok:
        return True, "已发送，请查看系统通知中心"
    return False, _last_error or "发送失败"
