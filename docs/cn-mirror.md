# 让接口真正落地中国大陆（大陆反代）

## 为什么需要这一步

程序已经做到三件事：

| 层次 | 手段 | 效果 |
| --- | --- | --- |
| HTTP 代理 | 清空 `http_proxy` 等变量 + `getproxies()` 恒空 | 不走 Clash 的 7890 端口 |
| DNS 劫持 | 内置 DoH（DNSPod / AliDNS，引导 IP 写死） | 绕开 fake-ip，拿到真实 IP |
| 报文改写 | 无解 | 见下 |

第三层没有客户端解法：**路由器端的 TProxy / REDIRECT 在 netfilter 里就改写了 TCP**，
数据包在离开电脑前已经被决定去哪。实测数据（同一台机器）：

```
国内回显 https://myip.ipip.net   → 180.137.128.120（中国 广西 电信）
境外回显 https://ipinfo.io       → 208.87.242.163（US, AS40676 Psychz）
API 站点 /cdn-cgi/trace          → ip=208.87.242.163 colo=LAX
```

也就是说：**去大陆的目标走直连，去境外的目标一律被塞进节点**。
所以"不被代理接管"和"接口在中国大陆"其实是同一个问题——
只要目标 IP 落在大陆，Clash 的 `GEOIP,CN,DIRECT` 就会放行，链路自然干净。

社区镜像 `rocokingdomworld.org` 托管在 Cloudflare（`104.21.43.145` / `172.67.180.76`），
境外 IP，必然被转发。想让接口在大陆，只有一条路：**自己在境内放一个反代**。

## 方案 A：Nginx 反代（推荐，一台 1 核 1G 的境内机器足够）

```nginx
# /etc/nginx/conf.d/roco.conf
proxy_cache_path /var/cache/nginx/roco levels=1:2 keys_zone=roco:10m max_size=64m inactive=10m;

server {
    listen 443 ssl;
    http2 on;
    server_name roco.example.cn;            # 换成你自己的域名

    ssl_certificate     /etc/nginx/ssl/roco.pem;
    ssl_certificate_key /etc/nginx/ssl/roco.key;

    location = /api/merchant {
        proxy_pass https://rocokingdomworld.org/api/merchant;

        # 回源时用目标站点的域名做 SNI，否则 Cloudflare 会返回 403
        proxy_set_header Host rocokingdomworld.org;
        proxy_ssl_server_name on;
        proxy_ssl_name rocokingdomworld.org;

        # 缓存 55 秒：社区接口本身就是 4 小时一轮，1 分钟缓存完全够用，
        # 既省跨境流量，也避免多个客户端同时打爆源站
        proxy_cache roco;
        proxy_cache_valid 200 55s;
        proxy_cache_lock on;
        add_header X-Cache-Status $upstream_cache_status always;

        proxy_connect_timeout 10s;
        proxy_read_timeout 20s;
    }
}
```

要点：

- 必须用**自己的域名 + 有效证书**。不要用 IP 直连 HTTPS，证书过不了，程序会按设计拒绝连接。
- `proxy_ssl_name` 不能省。少了它回源 SNI 是你自己的域名，Cloudflare 直接 403。
- 缓存时长别超过 300 秒，否则会盖过接口本身的换货节奏。
- 域名需要备案（境内服务器 80/443 的合规要求），这条没法绕。

1Panel / 宝塔用户：新建"反向代理"站点，目标填 `https://rocokingdomworld.org`，
开启"发送域名 `rocokingdomworld.org`"，再在配置里补 `proxy_ssl_name` 与缓存两行即可。

## 方案 B：腾讯云函数 / 阿里云函数（无服务器，按量计费）

```python
# 云函数入口（Python 3.9+），依赖 requests
import time
import requests

_CACHE = {"at": 0.0, "body": b"", "ctype": "application/json"}
TTL = 55

def main_handler(event, context):
    now = time.time()
    if now - _CACHE["at"] > TTL or not _CACHE["body"]:
        resp = requests.get(
            "https://rocokingdomworld.org/api/merchant",
            headers={"Host": "rocokingdomworld.org",
                     "User-Agent": "RocoMerchantMirror/1.0"},
            timeout=12,
        )
        resp.raise_for_status()
        _CACHE.update(at=now, body=resp.content,
                      ctype=resp.headers.get("Content-Type", "application/json"))
    return {
        "isBase64Encoded": False,
        "statusCode": 200,
        "headers": {"Content-Type": _CACHE["ctype"],
                    "Cache-Control": "public, max-age=55"},
        "body": _CACHE["body"].decode("utf-8"),
    }
```

挂在 API 网关的自定义域名下，记得开启"集成响应"。

## 在程序里启用

1. 主界面 → 设置 → 数据渠道 → 选「**大陆反代 · 自建**」
2. 接口地址填 `https://roco.example.cn/api/merchant`
3. 保存 → 主界面点「网络」按钮，看诊断面板：

```
目标：roco.example.cn
  系统 DNS   ：198.18.1.112  ← fake-ip，已被劫持
  DoH 解析   ：1.2.3.4
  建连目标   ：1.2.3.4（中国大陆）
  真实出口   ：180.137.120.1  CN / ——
  结论       ：直连中国大陆出口
```

看到"中国大陆"就是成功了。此时状态栏会显示 `直连 1.2.3.4 · 中国大陆`，
主界面「网络」按钮也会变成 `网络：大陆直连`。

> 反代只缓存、不落库、不二次分发，轮次与限购字段原样透传。

## 兜底：还是要用境外地址怎么办

只能在路由器侧加规则，让 Clash 放行。程序内置了生成器：
「设置 → 网络直连 → 复制直连规则」，粘到 shellclash 的自定义规则里，
**必须放在任何 `MATCH` / `GEOIP` / `IP-CIDR` 代理规则之前**（Clash 是首条命中生效）。

```yaml
rules:
  - DOMAIN-SUFFIX,rocokingdomworld.org,DIRECT
  - DOMAIN,rocokingdomworld.org,DIRECT
  - DOMAIN,doh.pub,DIRECT
  - IP-CIDR,1.12.12.12/32,DIRECT,no-resolve
  - IP-CIDR,120.53.53.53/32,DIRECT,no-resolve
  - DOMAIN,223.5.5.5,DIRECT
  - IP-CIDR,223.5.5.5/32,DIRECT,no-resolve
```

shellclash 的规则文件在 `/etc/openclash/custom/openclash_custom_rules.list`
（不同版本路径可能不同，用"自定义规则"菜单进入最稳）。

注意 DoH 那几条不能漏：解析这一步如果被劫持，后面按 IP 直连拿到的是 fake-ip，
等于白做。
