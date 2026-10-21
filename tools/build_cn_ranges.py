"""重新生成 data/cn_ipv4.txt（中国大陆 IPv4 段）。

数据源是 APNIC 的分配统计文件，属于权威原始数据，不含第三方 GeoIP 库的推断。
用法：

    python tools/build_cn_ranges.py
    python tools/build_cn_ranges.py --source /path/to/delegated-apnic-latest

生成的表被 netdirect.is_cn_ip() 使用，用来判断"目标是不是在中国大陆"——
这决定了路由器端的分流规则会不会放行直连。
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
import urllib.request
from pathlib import Path

SOURCE_URL = "https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest"
TARGET = Path(__file__).resolve().parent.parent / "data" / "cn_ipv4.txt"


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "RocoMerchant/1.4"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def parse(text: str, country: str = "CN") -> list[ipaddress.IPv4Network]:
    ranges: list[tuple[int, int]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 7 or parts[1] != country or parts[2] != "ipv4":
            continue
        start = int(ipaddress.IPv4Address(parts[3]))
        count = int(parts[4])
        ranges.append((start, start + count - 1))

    ranges.sort()
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    networks: list[ipaddress.IPv4Network] = []
    for start, end in merged:
        networks.extend(
            ipaddress.summarize_address_range(
                ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
            )
        )
    return networks


def main() -> int:
    parser = argparse.ArgumentParser(description="生成中国大陆 IPv4 段表")
    parser.add_argument("--source", help="本地 APNIC 数据文件（默认联网下载）")
    parser.add_argument("--output", default=str(TARGET))
    args = parser.parse_args()

    text = Path(args.source).read_text(encoding="utf-8", errors="replace") \
        if args.source else fetch(SOURCE_URL)
    networks = parse(text)
    if not networks:
        print("没有解析到任何网段，检查数据源", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(str(net) for net in networks) + "\n", encoding="utf-8"
    )
    print(f"已写入 {output}：{len(networks)} 条，{output.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
