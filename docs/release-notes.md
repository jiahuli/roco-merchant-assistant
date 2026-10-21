## 本次更新

### 新增：直连网络层（绕开 shellclash 等代理）

程序现在默认不允许任何代理接管流量：

- **剥离 HTTP 代理**：启动即清空 `http_proxy` / `https_proxy` / `all_proxy`，
  并让 `urllib.getproxies()` 恒返回空——全进程（含图标下载）不再走系统代理或 Clash 的 7890 端口。
- **绕开 DNS 劫持**：Clash 的 fake-ip 会把域名解析成 `198.18.x.x`，连接必然落到代理端口。
  程序改用内置 DoH（腾讯 DNSPod / 阿里 AliDNS，**引导 IP 写死**）取真实 IP，
  再按真实 IP 建连，TLS 的 SNI 与证书校验仍按域名进行。
- **出口可验证**：每次查询会校验服务器侧看到的真实出口，状态栏常驻显示
  `直连 x.x.x.x · 中国大陆` 或 `出口 US/LAX · 已被代理转发`。
- **诊断面板**：工具栏「网络」→ 逐渠道列出系统 DNS、DoH 真实 IP、建连目标归属、真实出口国家。

### 新增：大陆渠道优先排序

实测发现 `shallow`（洛克魔法书）服务器本身就在中国大陆（`124.226.74.51`，140ms），
而 `rkw` 在 Cloudflare（419–590ms 且出口在 US/LAX）。
开启「优先大陆渠道」后，降级顺序会按渠道所在地重排，大陆渠道优先尝试。

### 新增：大陆反代渠道

`cn_mirror` —— 填自己的境内反代地址后优先使用。目标 IP 在中国大陆时，
Clash 的 `GEOIP,CN,DIRECT` 会放行直连，流量不再绕 VPN 节点。
部署方式（Nginx / 云函数）见 `docs/cn-mirror.md`。

### 新增：直连规则生成

「设置 → 网络直连 → 复制直连规则」生成 Clash/shellclash 的 DIRECT 规则片段，
含域名与 DoH 引导 IP。需粘在代理规则之前（Clash 首条命中生效）。

### 说明：做不到的那一层

路由器端的 TProxy / REDIRECT 在 netfilter 层改写 TCP，客户端无法在报文层绕过。
实测：即使按真实 IP 直连并保持正确 SNI，Cloudflare 看到的出口仍是节点 IP
（`ip=208.87.242.163 colo=LAX`）。所以「不被代理接管」的真正解法是
**让目标落在中国大陆**——这也是本版把「大陆反代」做成一等渠道的原因。

### 其它

- 新增 5499 条中国大陆 IPv4 段（APNIC 分配表），`tools/build_cn_ranges.py` 可重新生成
- 自测 95 → 127 项
- 修复：打烊时状态栏不显示出口信息

## 下载

- `RocoMerchantAssistant.exe` —— 单文件绿色版，Windows 10/11 直接双击运行
- 未做代码签名，SmartScreen 可能提示「未知发布者」，选择「仍要运行」

MIT License
