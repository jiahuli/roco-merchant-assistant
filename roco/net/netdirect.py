"""直连网络层：不经过任何代理软件，直接与目标服务器建立 TLS 连接。

要解决的三个层次（对应 shellclash 这类路由器端代理的劫持链路）
--------------------------------------------------------------
1. **HTTP 代理劫持**：`urllib` 默认会读取 `http_proxy/https_proxy` 环境变量
   与 WinINET 注册表代理（Clash 常见做法是把系统代理设成 127.0.0.1:7890）。
   → `install()` 剥掉这些变量并让 `urllib.request.getproxies()` 恒返回空，
     全进程任何 `urlopen` 都不会再走代理。

2. **DNS 劫持（fake-ip）**：Clash 在 fake-ip 模式下把域名解析成
   `198.18.0.0/15` 里的假地址，连接必然落到 Clash 的监听端口。
   → 用自己的 DoH 解析（DNSPod / AliDNS，均在中国大陆），拿到**真实 IP**；
     再按真实 IP 建连，同时保持 TLS 的 SNI 与 Host 头不变，证书校验照旧。

3. **透明代理（TProxy / REDIRECT）**：路由器在 netfilter 层就把 TCP 改写了，
   客户端无法在报文层面单方面绕过——这是协议事实，不是实现缺陷。
   能做的是让它**没有被转发的理由**：目标 IP 落在中国大陆时，Clash 的
   `GEOIP,CN,DIRECT` 规则会放行直连；否则必然被送去节点。
   → `is_cn_ip()` 用 APNIC 分配表判定目标是否在大陆，`diagnose()` 直接
     报出真实出口国家与 Cloudflare colo，让"有没有被接管"可测而非靠猜。
   → `clash_rules()` 生成链路层面的 DIRECT 规则，供路由器侧兜底。

本模块不依赖任何第三方库。
"""

from __future__ import annotations

import bisect
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PROXY_ENV_KEYS = (
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY",
)

# Clash 的 fake-ip 默认段是 198.18.0.0/16；RFC 2544 的 198.18.0.0/15 整段都
# 不可能是公网真实地址，出现即说明 DNS 被代理软件接管了。
FAKE_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("0.0.0.0/8"),
)

# DoH 引导地址：**必须写死 IP**。
# 如果先解析 doh.pub 再去连，解析这一步本身就会被 fake-ip 劫持，等于没绕开。
# 这些 IP 来自运营商长期不变的权威 DNS 服务：
#   doh.pub       → 腾讯 DNSPod（1.12.12.12 / 120.53.53.53，中国大陆）
#   223.5.5.5     → 阿里 AliDNS（中国大陆）
DOH_ENDPOINTS: tuple[dict[str, Any], ...] = (
    {
        "name": "DNSPod",
        "host": "doh.pub",
        "ips": ("1.12.12.12", "120.53.53.53"),
        "path": "/dns-query?name={name}&type=A",
    },
    {
        "name": "AliDNS",
        "host": "223.5.5.5",
        "ips": ("223.5.5.5",),
        "path": "/resolve?name={name}&type=A",
    },
)

DEFAULT_TIMEOUT = 15
USER_AGENT = "RocoMerchantAssistant/1.4 (+desktop; direct-tls)"

_installed = False
_install_lock = threading.Lock()
_dns_cache: dict[str, tuple[float, list[str]]] = {}
_dns_lock = threading.Lock()
DNS_TTL = 600          # 秒：域名真实 IP 缓存时长


class DirectError(RuntimeError):
    """直连失败（DNS 不可用、TLS 失败、连接被拒等）。"""


# ---------------------------------------------------------------------------
# 1. 关掉全进程的代理
# ---------------------------------------------------------------------------
def install() -> list[str]:
    """剥离代理环境变量，并让 urllib 永不使用系统代理。

    返回被移除的变量名列表（已移除过则返回空列表）。
    """
    global _installed
    with _install_lock:
        removed = [k for k in PROXY_ENV_KEYS if k in os.environ]
        for key in removed:
            os.environ.pop(key, None)

        # ProxyHandler 在构造时才调用 getproxies()，因此改这里对所有
        # urlopen / build_opener 都生效，能一并管住第三方库。
        if urllib.request.getproxies is not _empty_proxies:
            urllib.request.getproxies = _empty_proxies  # type: ignore[assignment]
        _installed = True
        return removed


def _empty_proxies() -> dict[str, str]:
    return {}


def installed() -> bool:
    return _installed


def proxy_env_snapshot() -> dict[str, str]:
    """当前进程里残留的代理环境变量（诊断用，应在 install() 之前取）。"""
    return {k: os.environ[k] for k in PROXY_ENV_KEYS if k in os.environ}


# ---------------------------------------------------------------------------
# 2. 中国大陆 IP 段
# ---------------------------------------------------------------------------
_cn_starts: list[int] = []
_cn_ends: list[int] = []
_cn_loaded = False
_cn_lock = threading.Lock()


def _load_cn_ranges() -> None:
    global _cn_starts, _cn_ends, _cn_loaded
    if _cn_loaded:
        return
    with _cn_lock:
        if _cn_loaded:
            return
        starts: list[int] = []
        ends: list[int] = []
        try:
            from roco.config import resource_path

            path = resource_path("data/cn_ipv4.txt")
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        net = ipaddress.ip_network(line, strict=False)
                    except ValueError:
                        continue
                    if net.version != 4:
                        continue
                    starts.append(int(net.network_address))
                    ends.append(int(net.broadcast_address))
        except Exception:
            starts, ends = [], []
        order = sorted(range(len(starts)), key=starts.__getitem__)
        _cn_starts = [starts[i] for i in order]
        _cn_ends = [ends[i] for i in order]
        _cn_loaded = True


def is_cn_ip(ip: str) -> bool:
    """目标 IP 是否属于中国大陆网段（依据 APNIC 分配表）。"""
    try:
        value = int(ipaddress.IPv4Address(ip))
    except (ValueError, ipaddress.AddressValueError):
        return False
    _load_cn_ranges()
    index = bisect.bisect_right(_cn_starts, value) - 1
    return index >= 0 and value <= _cn_ends[index]


def cn_range_count() -> int:
    """已加载的大陆网段条数；为 0 说明数据文件没随包分发。"""
    _load_cn_ranges()
    return len(_cn_starts)


def is_fake_ip(ip: str) -> bool:
    """是否是代理软件伪造的地址（Clash fake-ip 等）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    return any(addr in net for net in FAKE_NETWORKS)


# ---------------------------------------------------------------------------
# 3. TLS 直连（按 IP 建连，保持 SNI）
# ---------------------------------------------------------------------------
def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # 走系统根证书：企业/学校环境可能装有自签 CA，这里不去绕过校验，
    # 确保"能连上"的同时证书链仍然是被信任的（防中间人）。
    context.load_default_certs()
    return context


def _open_socket(ip: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=timeout)


def _http_exchange(
    host: str,
    ip: str,
    path: str,
    *,
    port: int = 443,
    scheme: str = "https",
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, dict[str, str], bytes]:
    """向 ip:port 发包，但 TLS 的 SNI 与 Host 头都用 host。"""
    merged = {"Host": host, "User-Agent": USER_AGENT,
              "Accept": "*/*", "Connection": "close"}
    if headers:
        merged.update(headers)

    sock = _open_socket(ip, port, timeout)
    try:
        if scheme == "https":
            # 关键：server_hostname 用域名，证书校验针对域名做；socket 连的是 IP。
            sock = _ssl_context().wrap_socket(sock, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock
        conn.request("GET", path or "/", headers=merged)
        response = conn.getresponse()
        response_headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, response_headers, response.read()
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 4. DoH 解析
# ---------------------------------------------------------------------------
def _doh_query(endpoint: dict[str, Any], host: str, timeout: float) -> list[str]:
    path = endpoint["path"].format(name=urllib.parse.quote(host, safe=""))
    last_error: Exception | None = None
    for ip in endpoint["ips"]:
        try:
            status, _, body = _http_exchange(
                endpoint["host"], ip, path,
                headers={"Accept": "application/dns-json"},
                timeout=timeout,
            )
            if status != 200:
                last_error = DirectError(f"{endpoint['name']} 返回 HTTP {status}")
                continue
            data = json.loads(body.decode("utf-8", errors="replace"))
            answers = data.get("Answer") or []
            ips = [str(a.get("data")) for a in answers if a.get("type") == 1]
            ips = [ip for ip in ips if _valid_ipv4(ip) and not is_fake_ip(ip)]
            if ips:
                return ips
            last_error = DirectError(f"{endpoint['name']} 未返回 A 记录")
        except Exception as exc:  # noqa: BLE001 - 逐个端点尝试
            last_error = exc
    raise DirectError(str(last_error) if last_error else "DoH 查询失败")


def _valid_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


def system_resolve(host: str) -> list[str]:
    """系统 DNS 的解析结果（可能已被 fake-ip 劫持，仅用于诊断对比）。"""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except OSError:
        return []
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


def resolve(host: str, *, use_doh: bool = True, timeout: float = 8.0) -> list[str]:
    """解析域名到真实 IPv4 列表，优先 DoH，失败再退回系统 DNS。"""
    if _valid_ipv4(host):
        return [host]

    key = host.lower()
    if use_doh:
        with _dns_lock:
            cached = _dns_cache.get(key)
        if cached and time.time() - cached[0] < DNS_TTL:
            return list(cached[1])

    ips: list[str] = []
    if use_doh:
        for endpoint in DOH_ENDPOINTS:
            try:
                ips = _doh_query(endpoint, host, timeout)
                break
            except Exception:
                continue

    if not ips:
        ips = [ip for ip in system_resolve(host) if not is_fake_ip(ip)]

    if ips:
        with _dns_lock:
            _dns_cache[key] = (time.time(), list(ips))
    return ips


def clear_dns_cache() -> None:
    with _dns_lock:
        _dns_cache.clear()


# ---------------------------------------------------------------------------
# 5. 请求入口
# ---------------------------------------------------------------------------
def split_url(url: str) -> tuple[str, str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise DirectError(f"不支持的协议：{parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise DirectError(f"地址里没有主机名：{url}")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, host, path, port


def _pick_ip(ips: Iterable[str], prefer_ip: str = "") -> str:
    """选一个建连目标：显式指定 > 大陆 > 第一个。"""
    candidates = list(ips)
    if prefer_ip and prefer_ip in candidates:
        return prefer_ip
    for ip in candidates:
        if is_cn_ip(ip):
            return ip
    return candidates[0]


def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    use_doh: bool = True,
    prefer_ip: str = "",
    max_redirects: int = 3,
) -> tuple[int, bytes, dict[str, Any]]:
    """直连 GET。返回 (状态码, 响应体, 路由信息)。"""
    scheme, host, path, port = split_url(url)
    ips = resolve(host, use_doh=use_doh, timeout=min(timeout, 8.0))
    if not ips:
        raise DirectError(f"无法解析域名：{host}")

    chosen = _pick_ip(ips, prefer_ip)
    started = time.time()
    status, body = 0, b""
    for _ in range(max_redirects + 1):
        try:
            status, response_headers, body = _http_exchange(
                host, chosen, path, port=port, scheme=scheme,
                headers=headers, timeout=timeout,
            )
        except ssl.SSLError as exc:
            raise DirectError(f"TLS 校验失败（可能被中间人拦截）：{exc}") from exc
        except socket.timeout as exc:
            raise DirectError(f"连接超时：{host}（{timeout}s）") from exc
        except OSError as exc:
            raise DirectError(f"无法连接 {host}（{chosen}）：{exc}") from exc

        if status not in (301, 302, 303, 307, 308):
            break
        location = response_headers.get("location", "")
        if not location.startswith(("http://", "https://")):
            break          # 相对跳转不做处理，交给调用方看状态码
        location_host = urllib.parse.urlsplit(location).hostname or ""
        if location_host and location_host != host:
            # 跳到别的主机要重新解析并校验，不在直连层做隐式跟随
            break
        scheme, host, path, port = split_url(location)
        chosen = _pick_ip(resolve(host, use_doh=use_doh, timeout=min(timeout, 8.0)), prefer_ip)

    trace = {
        "host": host,
        "scheme": scheme,
        "ip": chosen,
        "ips": ips,
        "is_cn": is_cn_ip(chosen),
        "latency_ms": int((time.time() - started) * 1000),
        "via_doh": bool(use_doh),
    }
    return status, body, trace


def fetch_json(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    use_doh: bool = True,
    prefer_ip: str = "",
) -> tuple[Any, dict[str, Any]]:
    status, body, trace = http_get(
        url, headers, timeout=timeout, use_doh=use_doh, prefer_ip=prefer_ip
    )
    if status >= 400:
        raise DirectError(f"HTTP {status}：{url}")
    try:
        return json.loads(body.decode("utf-8", errors="replace")), trace
    except json.JSONDecodeError as exc:
        raise DirectError("返回内容不是合法 JSON，可能被网关拦截或需要鉴权") from exc


def fetch_bytes(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    use_doh: bool = True,
) -> tuple[bytes, dict[str, Any]]:
    status, body, trace = http_get(url, headers, timeout=timeout, use_doh=use_doh)
    if status >= 400:
        raise DirectError(f"HTTP {status}：{url}")
    return body, trace


# ---------------------------------------------------------------------------
# 6. 出口诊断
# ---------------------------------------------------------------------------
def egress_of(host: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """探测访问该主机时的**真实出口**。

    借助 Cloudflare 的 `/cdn-cgi/trace`：它会回显服务端看到的客户端 IP 与国家。
    该接口只存在于 Cloudflare 后面的站点；非 CF 站点返回空结果，不算错误。
    """
    try:
        status, body, trace = http_get(
            f"https://{host}/cdn-cgi/trace", timeout=timeout
        )
    except DirectError as exc:
        return {"ok": False, "error": str(exc)}
    if status != 200:
        # 只有 Cloudflare 后面的站点才有 /cdn-cgi/trace，其它站点 404 属正常
        return {"ok": False, "error": "该站点没有出口回显"}

    text = body.decode("utf-8", errors="replace")
    data = {}
    for line in text.strip().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value
    return {
        "ok": "ip" in data,
        "ip": data.get("ip", ""),
        "country": data.get("loc", ""),
        "colo": data.get("colo", ""),
        "tls": data.get("tls", ""),
        "latency_ms": trace.get("latency_ms", 0),
        "ip_is_cn": is_cn_ip(data.get("ip", "")),
    }


def diagnose(host: str, *, use_doh: bool = True, timeout: float = 10.0) -> dict[str, Any]:
    """完整诊断：DNS 是否被劫持、目标是否在大陆、出口落在哪个国家。"""
    system_ips = system_resolve(host)
    fake_hijacked = any(is_fake_ip(ip) for ip in system_ips) or not system_ips

    doh_ips: list[str] = []
    doh_error = ""
    if use_doh:
        try:
            doh_ips = resolve(host, use_doh=True, timeout=min(timeout, 8.0))
        except DirectError as exc:
            doh_error = str(exc)

    target_ip = _pick_ip(doh_ips or [ip for ip in system_ips if not is_fake_ip(ip)] or ["0.0.0.0"])
    egress = egress_of(host, timeout=min(timeout, 8.0)) if doh_ips else {"ok": False}

    verdict, advice = _verdict(fake_hijacked, doh_ips, target_ip, egress)

    return {
        "host": host,
        "system_ips": system_ips,
        "fake_ip_hijacked": fake_hijacked,
        "doh_ips": doh_ips,
        "doh_error": doh_error,
        "target_ip": target_ip,
        "target_is_cn": is_cn_ip(target_ip),
        "egress": egress,
        "verdict": verdict,
        "advice": advice,
    }


def _verdict(
    fake_hijacked: bool,
    doh_ips: list[str],
    target_ip: str,
    egress: dict[str, Any],
) -> tuple[str, list[str]]:
    advice: list[str] = []
    country = str(egress.get("country") or "")
    colo = str(egress.get("colo") or "")
    egress_ip = str(egress.get("ip") or "")

    if not doh_ips:
        return "DNS 解析失败", ["检查网络连通性；可在设置里关闭 DoH 后重试"]

    if country and country.upper() != "CN":
        verdict = f"已经被代理接管：出口 {egress_ip}（{country} {colo}）"
        advice.append(
            "目标不在中国大陆，路由器端的透明代理会强制转发到节点。"
            "客户端无法在报文层绕过，需要在路由器侧为域名加 DIRECT 规则，"
            "或改用中国大陆的接口地址。"
        )
        if fake_hijacked:
            advice.append("系统 DNS 返回了 fake-ip，已自动改用 DoH 取真实 IP。")
        return verdict, advice

    if country == "CN":
        verdict = f"直连中国大陆出口：{egress_ip}"
        if fake_hijacked:
            advice.append("系统 DNS 被 fake-ip 劫持，已通过 DoH 取得真实 IP 并直连。")
        return verdict, advice

    verdict = "直连（该站点未提供出口回显）"
    if is_cn_ip(target_ip):
        advice.append("目标位于中国大陆，路由器分流规则通常放行直连。")
    else:
        advice.append("目标不在中国大陆，可能被路由器代理转发，建议在路由器侧加 DIRECT 规则。")
    if fake_hijacked:
        advice.append("系统 DNS 返回了 fake-ip，已改用 DoH 取真实 IP。")
    return verdict, advice


# ---------------------------------------------------------------------------
# 7. 路由器侧分流规则（唯一能对抗 TProxy 的手段）
# ---------------------------------------------------------------------------
def clash_rules(hosts: Iterable[str]) -> str:
    """生成 Clash / shellclash 的 DIRECT 规则片段。

    shellclash 的规则是从上往下第一条命中即生效，所以这些行必须插在
    任何 `MATCH` / `GEOIP` / `IP-CIDR` 代理规则**之前**。
    """
    lines = ["rules:", "  # ↓ 放在所有代理规则之前，确保本程序直连"]
    for host in dict.fromkeys(h for h in hosts if h):
        lines.append(f"  - DOMAIN-SUFFIX,{host},DIRECT")
        lines.append(f"  - DOMAIN,{host},DIRECT")
    lines.append("  # DoH 引导地址也要直连，否则解析这一步就会被劫持")
    for endpoint in DOH_ENDPOINTS:
        lines.append(f"  - DOMAIN,{endpoint['host']},DIRECT")
        for ip in endpoint["ips"]:
            lines.append(f"  - IP-CIDR,{ip}/32,DIRECT,no-resolve")
    return "\n".join(lines) + "\n"


def default_hosts(providers: dict[str, Any]) -> list[str]:
    """从渠道配置里提取所有需要直连的主机名。"""
    hosts: list[str] = []
    for conf in (providers or {}).values():
        base = str((conf or {}).get("base_url") or "").strip()
        if not base:
            continue
        try:
            host = urllib.parse.urlsplit(base).hostname
        except ValueError:
            host = None
        if host and host not in hosts:
            hosts.append(host)
    return hosts
