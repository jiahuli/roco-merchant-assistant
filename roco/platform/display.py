"""显示器刷新率与定时器精度：给界面帧节拍提供**真实**数字。

为什么需要这一层
----------------
1.9 的动画把节拍写死成 `after(15)` / `after(24)`，等于把界面锁在 ~65fps / ~42fps；
更麻烦的是动画"每帧走固定比例"（滚动 0.045 视口/帧、悬停走 35% 剩余距离/帧），
于是**帧率一变、速度就变**——在 200Hz 屏上想提高帧率，就得手改数字，改完动画快三倍。
正确做法是"给定时长、按真实帧率取帧"，这个模块负责回答两件事：

* 这块屏幕**现在是**多少 Hz（决定一帧多少毫秒）；
* 动画期间怎么让 `after()` 的精度真的能到 5–8ms（Windows 默认定时器粒度是
  15.6ms，不提升精度的话，`after(5)` 实际会在 15.6ms 后才触发）。

实现要点
--------
* `EnumDisplaySettingsW` 读每块**活动**显示器的 `dmDisplayFrequency`，
  取最大值，但**帧率封顶 90fps**：再往上肉眼几乎无收益，而 Tk 的每一帧都要
  重排重绘整棵控件树（实测搬一次内嵌 frame + 20 张卡片 ≈ 3.4ms），
  无脑吃满 200Hz 只会把 CPU 吃光、帧还不一定追得上。90fps 是这条曲线上的
  甜点：足够顺，成本可控。
* `timeBeginPeriod(1)` 是**全局**副作用（影响系统定时器精度与功耗），
  所以只在动画存续期间按引用计数持有，动画结束立刻归还。不提升精度的话，
  Windows 默认 15.6ms 的定时器粒度会把 `after(11)` 变成"约 15.6ms"（≈64fps），
  高刷屏上等于白设。
* 全部能力"尽力而为"：非 Windows、调用失败、读不到值一律回落 60Hz，
  界面照常工作，绝不因为拿不到刷新率而报错。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

# 读不到刷新率时的保守值（绝大多数显示器）
_DEFAULT_HZ = 60
# 动画帧率上限：90fps 足够顺，再高是纯烧 CPU（用户明确要求"不必吃满刷新率"）
_MAX_FPS = 90
# 帧间隔夹取：下界 = 90fps 对应的一帧，上界 ≈ 62.5Hz（低于 60Hz 的屏也不必更慢）
_MIN_FRAME_MS = max(4, int(1000 / _MAX_FPS))
_MAX_FRAME_MS = 16
# 刷新率缓存有效期：用户改分辨率/换屏后最多这么久就会被重新读取
_CACHE_TTL = 3.0

_ENUM_CURRENT_SETTINGS = -1
_DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
_CCHDEVICENAME = 32
_LF_FACESIZE = 32

_user32 = ctypes.WinDLL("user32") if os.name == "nt" else None
_timer_refs = 0           # timeBeginPeriod 的引用计数
_cache: tuple[float, int] | None = None


class _POINTL(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _DEVMODEW(ctypes.Structure):
    """devicemodeW 的显示部分。

    只声明到 `dmDisplayFrequency`：`dmSize` 必须填 `sizeof(本结构)`，
    Windows 按它判断结构版本，多声明的尾部字段用不到反而不易出错。
    """

    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * _CCHDEVICENAME),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPosition", _POINTL),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * _LF_FACESIZE),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
    ]


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


def _declare_types() -> None:
    """argtypes/restype 一次性声明：不声明会按 c_int 截断（项目踩过的坑）。"""
    if _user32 is None:
        return
    _user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                            ctypes.POINTER(_DISPLAY_DEVICEW),
                                            wintypes.DWORD]
    _user32.EnumDisplayDevicesW.restype = wintypes.BOOL
    _user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                             ctypes.POINTER(_DEVMODEW)]
    _user32.EnumDisplaySettingsW.restype = wintypes.BOOL


try:
    _declare_types()
except Exception:  # noqa: BLE001 - 声明失败只影响帧节拍，不该影响启动
    _user32 = None


def _enum_hz() -> list[int]:
    """所有活动显示器的当前刷新率（去重，升序）。"""
    if _user32 is None:
        return []
    rates: list[int] = []
    device = _DISPLAY_DEVICEW()
    device.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
    index = 0
    while _user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
        index += 1
        if not device.StateFlags & _DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        mode = _DEVMODEW()
        mode.dmSize = ctypes.sizeof(_DEVMODEW)
        ok = _user32.EnumDisplaySettingsW(device.DeviceName, _ENUM_CURRENT_SETTINGS,
                                          ctypes.byref(mode))
        if not ok or mode.dmPelsWidth <= 0:
            continue
        hz = int(mode.dmDisplayFrequency)
        if 20 <= hz <= 500 and hz not in rates:
            rates.append(hz)
    return sorted(rates)


def refresh_rate() -> int:
    """当前生效的刷新率（多屏取最快的那块）；读不到返回 60。"""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    rates = _enum_hz()
    hz = rates[-1] if rates else _DEFAULT_HZ
    _cache = (now, hz)
    return hz


def frame_ms() -> int:
    """一帧的毫秒数：按真实刷新率换算，并夹在 [90fps, ~62Hz] 之间。"""
    try:
        hz = refresh_rate() or _DEFAULT_HZ
    except Exception:  # noqa: BLE001
        hz = _DEFAULT_HZ
    hz = min(hz, _MAX_FPS)
    return max(_MIN_FRAME_MS, min(_MAX_FRAME_MS, int(round(1000.0 / hz))))


def describe() -> str:
    """给启动日志用的一行说明（排障时能一眼看出节拍算得对不对）。"""
    try:
        return f"{refresh_rate()}Hz / 每帧 {frame_ms()}ms（动画上限 {_MAX_FPS}fps）"
    except Exception:  # noqa: BLE001
        return "未知刷新率（按 60Hz 兜底）"


def invalidate() -> None:
    """丢掉缓存：换屏、改分辨率、切全屏独占之后调用。"""
    global _cache
    _cache = None


# --------------------------------------------------------------- 定时器精度
def hold_precise_timer() -> None:
    """把系统定时器精度提到 1ms（引用计数，可嵌套调用）。"""
    global _timer_refs
    if _timer_refs > 0:
        _timer_refs += 1
        return
    try:
        if ctypes.windll.winmm.timeBeginPeriod(1) == 0:
            _timer_refs = 1
            return
    except Exception:  # noqa: BLE001 - 拿不到精度只是动画粗一档
        pass
    _timer_refs = 0


def release_precise_timer() -> None:
    """归还定时器精度；只有最后一个持有者才真正调用 timeEndPeriod。"""
    global _timer_refs
    if _timer_refs <= 0:
        return
    _timer_refs -= 1
    if _timer_refs:
        return
    try:
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:  # noqa: BLE001
        pass


def precise_timer_held() -> bool:
    """当前是否持有高精度定时器（自测用）。"""
    return _timer_refs > 0
