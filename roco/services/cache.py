"""图标缓存 + 异步加载。

缓存策略
--------
* 磁盘：%APPDATA%\\RocoMerchant\\cache\\icons\\<sha1(url)>.png，永久有效
  （商品图标是静态资源，键里带 URL 哈希，换图自然换 key）。
* 内存：{(url, size): PhotoImage}，窗口存活期内不再重复解码。
* 并发：4 线程下载，只在主线程创建 PhotoImage（Tk 不支持跨线程）。

这样同一件商品在多次定时查询之间不会重复请求图标。
"""
from __future__ import annotations

import base64
import hashlib
import io
import queue
import threading
import urllib.error
import urllib.request

from roco.net import netdirect
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk

try:  # Pillow 可选：装了就用它做高质量缩放与圆角
    from PIL import Image, ImageDraw, ImageOps, ImageTk

    _HAS_PIL = True
except Exception:  # pragma: no cover
    _HAS_PIL = False

ICON_SIZE = 64
# 内存图标缓存上限：(url,size)→PhotoImage，一张 64px 约 16KB；
# 上限 256 张 ≈ 4MB，超出按最久未用淘汰（后台常驻不无限涨）
MEMORY_CACHE_LIMIT = 256
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RocoMerchantAssistant/1.0"


class IconCache:
    """磁盘级图标缓存。"""

    def __init__(self, folder: Path, enabled: bool = True) -> None:
        self.folder = folder
        self.enabled = enabled
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _path_for(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        return self.folder / f"{digest}.png"

    def lookup(self, url: str) -> Optional[Path]:
        if not self.enabled or not url:
            return None
        path = self._path_for(url)
        if path.exists() and path.stat().st_size > 0:
            with self._lock:
                self.hits += 1
            return path
        return None

    def download(self, url: str) -> Optional[Path]:
        """下载并落盘；已有缓存直接返回。"""
        cached = self.lookup(url)
        if cached:
            return cached
        if not url:
            return None
        with self._lock:
            self.misses += 1
        # 图标同样走直连：否则系统 DNS 的 fake-ip 会把图片请求也送进代理
        try:
            data, _route = netdirect.fetch_bytes(
                url,
                {"User-Agent": _UA, "Referer": "https://wiki.biligame.com/"},
                timeout=20,
            )
        except (netdirect.DirectError, OSError):
            return None
        if len(data) < 64:
            return None
        path = self._path_for(url)
        try:
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError:
            return None
        return path

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return self.hits, self.misses

    def size_mb(self) -> float:
        total = 0
        for item in self.folder.glob("*.png"):
            try:
                total += item.stat().st_size
            except OSError:
                continue
        return total / (1024 * 1024)

    def count(self) -> int:
        return sum(1 for _ in self.folder.glob("*.png"))

    def clear(self) -> int:
        removed = 0
        for item in self.folder.glob("*.png"):
            try:
                item.unlink()
                removed += 1
            except OSError:
                continue
        return removed


class ImageLoader:
    """异步把 URL 变成 Tk 可用的 PhotoImage。"""

    def __init__(self, root: tk.Misc, cache: IconCache, size: int = ICON_SIZE) -> None:
        self.root = root
        self.cache = cache
        self.size = size
        self._memory: dict[tuple[str, int], tk.PhotoImage] = {}
        self._pending: set[str] = set()
        # 下载/解码并发 2 足够：图标小、磁盘缓存命中率高，4 线程只是徒增栈内存
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="icon")
        self._queue: queue.Queue = queue.Queue()
        self._poll_job: str | None = None
        self._closed = False
        self._ensure_pump()

    # ---------------- 主线程 API ----------------
    def get_cached(self, url: str, size: int | None = None) -> Optional[tk.PhotoImage]:
        if not url:
            return None
        size = size or self.size
        return self._memory.get((url, size))

    def prewarm(self, urls: list[str], size: int | None = None) -> None:
        """渲染前预热：磁盘命中现在就解码进内存，未命中的并行下载。

        目的：让接下来一次渲染里所有已缓存的图标**同步就位**，
        消除"卡片先出来、图标逐个弹入"的逐条闪烁。仍在下载的少数
        图标由既有结果泵补齐（同一泵周期内批量上屏，不逐个跳）。
        """
        size = size or self.size
        for url in urls:
            if not url or (url, size) in self._memory:
                continue
            key = f"{url}@{size}"
            if key in self._pending:
                continue
            self._pending.add(key)
            path = self.cache.lookup(url)
            if path:
                try:
                    photo = self._decode(path.read_bytes(), size)
                except OSError:
                    photo = None
                if photo is not None:
                    self._memory[(url, size)] = photo
                self._pending.discard(key)
                continue
            self._executor.submit(self._prewarm_work, url, size)

    def _prewarm_work(self, url: str, size: int) -> None:
        """预热工作线程：只负责下载，解码回主线程（Tk 限制）。"""
        local = self.cache.download(url)
        data = local.read_bytes() if local else None
        if self._closed:
            return
        self._queue.put((url, size, data, None))   # on_ready=None：只进内存缓存

    def load(self, url: str, on_ready: Callable[[Optional[tk.PhotoImage]], None], size: int | None = None) -> None:
        """有缓存立刻回调；否则后台下载后回到主线程回调。"""
        size = size or self.size
        if not url:
            on_ready(None)
            return
        cached_photo = self._memory.get((url, size))
        if cached_photo is not None:
            on_ready(cached_photo)
            return

        key = f"{url}@{size}"
        if key in self._pending:
            return
        self._pending.add(key)

        path = self.cache.lookup(url)
        if path:
            photo = self._decode(path.read_bytes(), size)
            self._finish(url, size, photo, on_ready)
            return

        def work() -> None:
            local = self.cache.download(url)
            data = local.read_bytes() if local else None
            if self._closed:
                return
            # 交给主线程处理：Tk 对象只能在主线程创建
            self._queue.put((url, size, data, on_ready))

        self._executor.submit(work)

    # ---------------- 主线程泵 ----------------
    def _ensure_pump(self) -> None:
        if self._poll_job is None and not self._closed:
            self._poll_job = self.root.after(40, self._pump)

    def _pump(self) -> None:
        self._poll_job = None
        while True:
            try:
                url, size, data, on_ready = self._queue.get_nowait()
            except queue.Empty:
                break
            photo = self._decode(data, size) if data else None
            self._finish(url, size, photo, on_ready)
        self._ensure_pump()

    def _finish(self, url: str, size: int, photo, on_ready) -> None:
        self._pending.discard(f"{url}@{size}")
        if photo is not None:
            self._memory[(url, size)] = photo
            self._evict_if_needed()
        if on_ready is None:               # 预热路径：只进缓存，无需上屏回调
            return
        try:
            on_ready(photo)
        except tk.TclError:  # 窗口已销毁
            pass

    def _evict_if_needed(self) -> None:
        """内存缓存封顶：dict 保持插入序，超出上限时淘汰最旧的条目。"""
        overflow = len(self._memory) - MEMORY_CACHE_LIMIT
        if overflow <= 0:
            return
        for key in list(self._memory)[:overflow]:
            self._memory.pop(key, None)

    # ---------------- 解码 ----------------
    def _decode(self, data: bytes, size: int) -> Optional[tk.PhotoImage]:
        if not data:
            return None
        if _HAS_PIL:
            return self._decode_pil(data, size)
        return self._decode_tk(data, size)

    def _decode_pil(self, data: bytes, size: int) -> Optional[tk.PhotoImage]:
        try:
            image = Image.open(io.BytesIO(data)).convert("RGBA")
            image = ImageOps.fit(image, (size, size), Image.LANCZOS)
            mask = Image.new("L", (size * 4, size * 4), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, size * 4 - 1, size * 4 - 1), radius=size // 4 * 4, fill=255
            )
            mask = mask.resize((size, size), Image.LANCZOS)
            image.putalpha(mask)
            return ImageTk.PhotoImage(image)
        except Exception:
            return None

    def _decode_tk(self, data: bytes, size: int) -> Optional[tk.PhotoImage]:
        try:
            photo = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
        except tk.TclError:
            return None
        width = photo.width()
        if width > size:
            factor = max(1, round(width / size))
            if factor > 1:
                try:
                    photo = photo.subsample(factor, factor)
                except tk.TclError:
                    pass
        return photo

    def shutdown(self) -> None:
        self._closed = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        self._executor.shutdown(wait=False, cancel_futures=True)


def has_pillow() -> bool:
    return _HAS_PIL


def migrate_cache(source: Path, target: Path) -> tuple[int, int]:
    """把旧缓存目录里的图标搬到新目录，避免换路径后全部重新下载。

    返回 (成功数, 跳过数)。同名文件已存在则跳过（缓存键是 URL 哈希，
    同名即同一张图，没必要覆盖）。
    """
    copied = skipped = 0
    try:
        if not source.exists() or source.resolve() == target.resolve():
            return 0, 0
    except OSError:
        return 0, 0

    for item in source.glob("*.png"):
        destination = target / item.name
        if destination.exists():
            skipped += 1
            continue
        try:
            destination.write_bytes(item.read_bytes())
            copied += 1
        except OSError:
            skipped += 1
    return copied, skipped
