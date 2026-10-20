"""系统托盘图标（纯 ctypes 调 Shell_NotifyIcon，无第三方依赖）。

设计
----
* 在**独立线程**里创建消息专用窗口并跑 `GetMessage` 循环：
  Tk 的 mainloop 有自己的事件源，把托盘窗口放同线程容易互相卡住；
  独立线程最干净，且窗口句柄不跨线程使用。
* 托盘回调（左键 / 右键菜单）在托盘线程触发，**绝不能直接碰 Tk 控件**，
  所以这里只做 `on_command(name)` 回调，由主程序丢进队列、在主线程消费。
* 监听 `TaskbarCreated` 消息：资源管理器重启后自动把图标加回去，
  否则任务栏刷新一次图标就消失了。
"""
from __future__ import annotations

import ctypes
import os
import threading
import traceback
from ctypes import wintypes
from typing import Callable, Optional

WM_APP = 0x8000
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_STATE = 0x00000008
NIF_INFO = 0x00000010
NIF_SHOWTIP = 0x00000080

NIIF_INFO = 0x00000001
NIIF_USER = 0x00000004

NOTIFYICON_VERSION_4 = 4
NIN_BALLOONUSERCLICK = WM_APP + 5

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512
SM_CXSMICON = 49
SM_CYSMICON = 50
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
MF_GRAYED = 0x00000001
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


_types_declared = False


def _declare_types() -> None:
    """显式声明所有用到的 Win32 API 签名。

    这一步不是可选的：64 位下句柄/基址是 8 字节，ctypes 默认按 32 位 int
    转换，`GetModuleHandleW` 的返回值会被截断成负数，再传给
    `CreateWindowExW` 就直接 OverflowError。
    源码运行时模块基址恰好在低位所以看不出来，打包成 exe 后基址在高位必崩。
    """
    global _types_declared
    if _types_declared or os.name != "nt":
        return
    _types_declared = True

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]

    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]

    user32.DefWindowProcW.restype = _LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]

    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.PostQuitMessage.restype = None
    user32.PostQuitMessage.argtypes = [ctypes.c_int]

    user32.GetMessageW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = _LRESULT
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]

    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                   ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, ctypes.c_void_p]
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                  wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]

    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                          ctypes.POINTER(_NOTIFYICONDATAW)]


class MenuItem:
    """右键菜单项。"""

    def __init__(self, label: str = "", command: str = "", checked: bool = False,
                 separator: bool = False, enabled: bool = True) -> None:
        self.label = label
        self.command = command
        self.checked = checked
        self.separator = separator
        self.enabled = enabled


class TrayIcon:
    """托盘图标 + 右键菜单。

    `menu_provider` 每次右键时被调用，返回当前菜单项列表，
    这样勾选状态能反映程序最新状态。
    """

    def __init__(self, tooltip: str, icon_path: str,
                 menu_provider: Callable[[], list[MenuItem]],
                 on_command: Callable[[str], None]) -> None:
        self.tooltip = tooltip[:127]
        self.icon_path = icon_path
        self.menu_provider = menu_provider
        self.on_command = on_command

        self.hwnd: Optional[int] = None
        self.hicon: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._wndproc_ref = None      # 必须持引用，否则回调被 GC 回收会崩
        self._class_name = f"RocoMerchantTray_{os.getpid()}"
        self._v4 = False
        self._taskbar_created = 0
        self._lock = threading.Lock()
        self._callback_message = WM_APP + 1

    # ------------------------------------------------------------------ 生命周期
    def start(self, timeout: float = 3.0) -> bool:
        if os.name != "nt":
            return False
        if self._thread and self._thread.is_alive():
            return True
        _declare_types()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout) and self.hwnd is not None

    def stop(self) -> None:
        self._stop.set()
        if self.hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        # 线程的 finally 里已经 NIM_DELETE 过，这里清掉句柄让 available 变为 False
        self.hwnd = None
        self.hicon = None

    @property
    def available(self) -> bool:
        return self.hwnd is not None

    # ------------------------------------------------------------------ 线程主体
    def _run(self) -> None:
        try:
            self._create_window()
            if not self.hwnd:
                self._log("CreateWindowExW 返回空句柄")
                return
            self._add_icon()
            self._ready.set()
            self._message_loop()
        except Exception:
            # 打包成无控制台 exe 后看不到标准错误，落一份日志便于排查
            self._log(traceback.format_exc())
            self._ready.set()
        finally:
            self._remove_icon()

    @staticmethod
    def _log(text: str) -> None:
        try:
            import applog

            applog.error("托盘初始化失败:\n%s", text.rstrip())
        except Exception:
            pass

    def _create_window(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self._wndproc_ref = _WNDPROC(self._wndproc)

        wc = _WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hIcon = 0
        wc.hCursor = 0
        wc.hbrBackground = 0
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name

        user32.RegisterClassW(ctypes.byref(wc))

        # 64 位下句柄是 8 字节，ctypes 默认按 int 返回会截断，必须显式声明
        _declare_types()
        # 用隐藏的顶层窗口（WS_POPUP）而不是 HWND_MESSAGE：
        # TaskbarCreated 是广播消息，消息专用窗口收不到，图标在资源管理器重启后就没了
        self.hwnd = user32.CreateWindowExW(
            0, self._class_name, self._class_name, 0x80000000,  # WS_POPUP
            0, 0, 0, 0, None, None, wc.hInstance, None
        )
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    def _load_icon(self) -> int:
        user32 = ctypes.windll.user32
        cx = user32.GetSystemMetrics(SM_CXSMICON) or 16
        cy = user32.GetSystemMetrics(SM_CYSMICON) or 16
        if self.icon_path and os.path.exists(self.icon_path):
            handle = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, cx, cy,
                                       LR_LOADFROMFILE)
            if handle:
                return handle
        # MAKEINTRESOURCE(32512) = 默认应用图标
        return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _fill_icon_data(self, flags: int) -> _NOTIFYICONDATAW:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = self._callback_message
        data.hIcon = self.hicon or 0
        data.szTip = self.tooltip
        return data

    def _add_icon(self) -> None:
        shell = ctypes.windll.shell32
        self.hicon = self._load_icon()
        data = self._fill_icon_data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        ok = shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        if ok:
            data.uVersion = NOTIFYICON_VERSION_4
            self._v4 = bool(shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data)))

    def _remove_icon(self) -> None:
        if not self.hwnd:
            return
        try:
            data = self._fill_icon_data(0)
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        except Exception:
            pass

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):      # WM_QUIT / 出错
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------------ 窗口过程
    def _wndproc(self, hwnd, msg, wparam, lparam):
        user32 = ctypes.windll.user32
        try:
            if msg == self._callback_message:
                event = lparam & 0xFFFF          # v4 与旧版都能用
                if event in (WM_LBUTTONUP, WM_CONTEXTMENU):
                    if event == WM_CONTEXTMENU:
                        self._show_menu()
                    else:
                        self._emit("show")
                elif event == WM_RBUTTONUP:
                    self._show_menu()
                elif event == NIN_BALLOONUSERCLICK:
                    self._emit("show")
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                # 资源管理器重启：把图标加回去
                self._add_icon()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        try:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    # ------------------------------------------------------------------ 菜单
    def _show_menu(self) -> None:
        try:
            items = self.menu_provider() or []
        except Exception:
            items = []

        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        mapping: dict[int, str] = {}
        command_id = 1000
        for item in items:
            if item.separator:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            flags = MF_STRING
            if item.checked:
                flags |= MF_CHECKED
            if not item.enabled:
                flags |= MF_GRAYED
            command_id += 1
            mapping[command_id] = item.command
            user32.AppendMenuW(menu, flags, command_id, item.label)

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # 先拿到前台，否则点菜单外面不会自动收起
        user32.SetForegroundWindow(self.hwnd)
        chosen = user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
            point.x, point.y, 0, self.hwnd, None
        )
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)

        command = mapping.get(chosen)
        if command:
            self._emit(command)

    def _emit(self, command: str) -> None:
        try:
            self.on_command(command)
        except Exception:
            pass

    # ------------------------------------------------------------------ 气泡 / 提示
    def set_tooltip(self, text: str) -> None:
        self.tooltip = text[:127]
        if not self.hwnd:
            return
        data = self._fill_icon_data(NIF_TIP | NIF_SHOWTIP)
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def balloon(self, title: str, message: str) -> bool:
        """用系统气泡提示（比自绘浮窗更"原生"，适合"已最小化到托盘"这类告知）。"""
        if not self.hwnd:
            return False
        data = self._fill_icon_data(NIF_INFO)
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        data.dwInfoFlags = NIIF_INFO
        with self._lock:
            return bool(ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_MODIFY, ctypes.byref(data)
            ))
