"""生成 assets/app.ico（纯标准库，不需要 Pillow）。

设计：深色圆角方块 + 金色钱币，呼应主界面的暖金价格色。
16/24/32/48/64/128/256 七种尺寸，用 2 倍超采样做抗锯齿。
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
BG_TOP = (27, 36, 49)
BG_BOTTOM = (15, 19, 25)
COIN = (245, 198, 107)
COIN_EDGE = (184, 134, 58)
COIN_INNER = (255, 226, 160)


def _blend(base, top, alpha):
    return tuple(int(b + (t - b) * alpha) for b, t in zip(base, top))


def render(size: int) -> bytes:
    scale = 2
    n = size * scale
    pixels = [[(15, 19, 25) for _ in range(n)] for _ in range(n)]

    radius = n * 0.22
    margin = n * 0.02
    coin_r = n * 0.31
    center = n / 2
    ring_r = coin_r * 0.62

    for y in range(n):
        for x in range(n):
            # 圆角矩形遮罩
            dx = max(margin + radius - x, 0, x - (n - margin - radius))
            dy = max(margin + radius - y, 0, y - (n - margin - radius))
            if math.hypot(dx, dy) > radius:
                continue

            ratio = y / n
            color = _blend(BG_TOP, BG_BOTTOM, ratio * ratio)
            pixels[y][x] = color

            dist = math.hypot(x - center, y - center)
            if dist <= coin_r:
                if dist > coin_r - max(1.5, n * 0.022):
                    pixels[y][x] = COIN_EDGE
                elif dist < ring_r * 0.34:
                    pixels[y][x] = COIN_INNER
                elif dist < ring_r:
                    # 内圈留一道深色环，读起来像钱币
                    pixels[y][x] = COIN_EDGE if abs(dist - ring_r * 0.62) < n * 0.012 else COIN
                else:
                    pixels[y][x] = COIN

    # 盒式下采样
    out = bytearray(size * size * 3)
    for y in range(size):
        for x in range(size):
            r = g = b = 0
            for sy in range(scale):
                for sx in range(scale):
                    pr, pg, pb = pixels[y * scale + sy][x * scale + sx]
                    r += pr
                    g += pg
                    b += pb
            count = scale * scale
            i = (y * size + x) * 3
            out[i] = r // count
            out[i + 1] = g // count
            out[i + 2] = b // count
    return bytes(out)


def png_bytes(size: int) -> bytes:
    rgb = render(size)
    raw = b"".join(b"\x00" + rgb[y * size * 3:(y + 1) * size * 3] for y in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def write_ico(path: Path) -> None:
    images = [(size, png_bytes(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    payload = b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
        payload += data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + payload)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "assets/app.ico")
    write_ico(target)
    print(f"written {target} ({target.stat().st_size} bytes)")
