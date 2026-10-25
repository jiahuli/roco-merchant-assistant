# 远行商人助手 · 洛克王国：世界

> ⚠️ **本项目代码由 AI 生成**（含架构设计、全部源码、打包配置与文档）。
> 人类维护者**只做了运行测试与需求验收**，没有手工编写业务逻辑。
> 因此代码风格可能带有明显的"生成痕迹"，重构前建议先读一遍 `README` 与 `selftest.py`。

一个 Windows 桌面工具：定时查询《洛克王国：世界》远行商人的货架，展示图标 / 珍惜度 / 中文价格，遇到稀有物品用 **Windows 系统通知**提醒，可开置顶悬浮窗常驻查看，关闭窗口后驻留系统托盘继续工作。

纯 `tkinter` 手绘界面，不依赖 PyQt / Electron / 浏览器内核；除可选的 Pillow 外**没有第三方依赖**。成品是 13 MB 左右的单文件 exe，目标机器无需安装 Python。

- 语言 / 运行时：Python 3.9+
- 平台：Windows 10 / 11
- 许可证：[MIT](LICENSE)

---

> 想接手开发或让 AI 继续改这个项目？先读 **[docs/development.md](docs/development.md)**：
> 文件职责、数据流、轮次模型、踩过的坑、改动指引都在那一份里。

## 功能

### 定时查询
- 默认 **08:05 / 12:05 / 16:05 / 20:05**（商人 08:00 / 12:00 / 16:00 / 20:00 换货，留 5 分钟给接口刷新）
- 时间点可自定义，内置三套预设；手动刷新（`F5`）
- 顶部实时倒计时到下一轮换货；打烊时段（00:00–08:00）显示下次出摊时间

### 货架展示
- **全天 / 本轮区分**：接口用 `rounds` 字段标注售卖轮次，`[1,2,3,4]` 即四轮都卖（条件球、
  棱镜球这类不随换货消失）。卡片上有「全天」角标，货架统计也会写明「其中 N 件全天在售」，
  避免把"换货后还在"误判成没刷新
- **换货校验**：接口有缓存，刚过 08/12/16/20 点时可能仍返回上一轮货架。程序会比对
  接口轮次与本地时间应有的轮次，落后就按 30/60/120/180/240 秒重试，拿到新一轮才停
- **次数用完后可选**：常规重取用尽时，可以选「放弃本轮」（默认，等下次定时查询）或
  「转入慢速续试」（按 30 分钟间隔继续悄悄试，直到换货）。范围与影响见开发手册第 7 章
- **假刷新识别**：有的渠道把轮次刷成了新一轮、货架内容却还是旧的，或者压根不返回轮次
  字段（只能拿本地时间兜底）。这类情况下轮次比对看不出问题，界面会卡在「两件全天商品」
  上。程序改为比对货架内容本身：只剩全天商品、且与上次完全相同时，按可配置的间隔
  （默认 5 分钟、最多 3 次）自动重取；本轮商品突然全消失也按同样方式重取
- 图标：两级缓存（磁盘 + 内存），同一商品多次查询不重复请求
- 珍惜度：五档（普通 / 优良 / 稀有 / 史诗 / 传说），由关键词 → 分类 → 价格三层规则推断
- 价格：中文读法，如 `3200000` → **三百二十万**，同时保留 `3,200,000` 便于核对
- 限购数量、分类、悬停显示物品描述

### 通知
- 走 WinRT Toast，**落在 Windows 通知中心**，带「查看货架 / 忽略」按钮
- 点「查看货架」通过自定义协议 `rocomesh://` 回到本程序；配合单实例机制，会把已在运行的窗口叫到前台而不是再开一个
- 备用通道：应用内浮窗（仅当原生通知发不出去时出现，不会双份提醒）
- 叠加：系统提示音、任务栏闪烁；可选 Webhook 外部推送（Bark / 飞书 / 企业微信机器人）
- 去重：以「轮次 + 商品」为键持久化，同一轮同一物品只提醒一次

### 悬浮窗
- 工具栏一键开关，默认置顶；`◉` 可切换置顶
- 按住标题栏拖动；`–` 收起成一条 34px 横条
- 位置持久化；可跨显示器拖动与记忆
- 独立窗口，不抢焦点、不阻塞主界面
- **⟳ 立即获取**：标题栏的刷新按钮，点一下直接拉最新货架，**不用打开主窗口**；
  取数中按钮变成「…」，结果回来自动刷新列表与「更新于 HH:MM」
- **物品图标开关**：设置 →「悬浮窗」→「显示物品图标」。开启时每行显示 18px 物品图标；关闭后只隐藏图标，行数、名称、价格、珍惜度圆点、倒计时全部照常，列表更紧凑。图标与主界面共用同一份缓存，不会重复下载

### 多显示器
- 用 `EnumDisplayMonitors` 枚举每块屏的物理矩形与工作区，**不再用主屏尺寸当全屏尺寸**
- 拖动行为与 Windows 原生一致：允许跨屏、允许跨越屏幕边界、允许停在多屏高度不一致的空白处，**松手不吸附**
- 副屏在左侧（负坐标）也能正确保存与还原
- 还原时若目标位置已不在任何显示器上（比如副屏被拔掉），回落到主屏

### 常驻托盘
- 点 × **只隐藏窗口**，定时查询、通知、悬浮窗继续工作
- 托盘图标：左键 / 双击恢复窗口；右键菜单含「显示主窗口 / 立即刷新 / 悬浮窗 / 通知提醒 / 开机自启 / 错误日志 / 退出」
- 首次隐藏给一次系统气泡提示
- 想恢复"关掉就退出"：设置里取消「关闭窗口时最小化到托盘」
- 托盘创建失败时自动退回"关闭即退出"，不会出现关不掉也找不到

### 错误日志
- 默认写入 `%APPDATA%\RocoMerchant\logs\app.log`，单份 1 MB、滚动保留 3 份
- 目录可在设置里改（`log_dir`），不可写时自动回退默认位置
- 内置查看器：设置 →「错误日志」→「查看错误日志」，支持**只看错误**、刷新、清空、打开所在文件夹；托盘右键菜单也有入口
- 捕获三类异常：主线程未捕获异常、子线程未捕获异常（托盘 / 图标下载）、Tk 控件回调异常（默认只往 stderr 打，打包后等于静默丢失）

### 网络直连（绕开 shellclash 等代理）

程序默认不允许任何代理接管流量，三层分别处理：

| 层次 | 手段 | 能不能做到 |
| --- | --- | --- |
| HTTP 代理（`http_proxy` / 系统代理 / Clash 7890） | 启动即清空代理变量，并让 `urllib.getproxies()` 恒返回空 | ✅ 完全可控 |
| DNS 劫持（Clash fake-ip 把域名解析成 `198.18.x.x`） | 内置 DoH（腾讯 DNSPod / 阿里 AliDNS，引导 IP 写死），取得真实 IP 后按 IP 直连，TLS 的 SNI 与证书校验仍按域名 | ✅ 完全可控 |
| 透明代理（路由器 TProxy / REDIRECT 改写 TCP） | **客户端无法解决** | ❌ 协议事实 |

第三层不是没做好，是做不到：包在你电脑上时路由器已经决定了它的去向。实测同一台机器：

```
https://myip.ipip.net   → 203.0.113.10（中国大陆，示例地址）    ← 大陆目标走直连
https://ipinfo.io       → 198.51.100.20（US，示例地址）         ← 境外目标被塞进节点
API 的 /cdn-cgi/trace    → ip=198.51.100.20 colo=LAX           ← 即使按真实 IP 直连也一样
```

> 上面是实测结果的**脱敏版本**：真实出口 IP 属于个人网络信息，这里换成 RFC 5737 文档示例段。

所以真正有效的解法是**让目标落在中国大陆**：Clash 的 `GEOIP,CN,DIRECT` 会放行。

`1.6.0` 起默认数据源本身就在境内（`onebiji` 中文查询器），这一层不再需要额外处理。
只有在这三种场景才值得自建反代——多机共享缓存、需要统一出口、或者将来要换源：
完整做法（Nginx / 云函数两套配置）见 **[docs/cn-mirror.md](docs/cn-mirror.md)**。

不想自建的话，退路是在路由器侧加 DIRECT 规则（唯一能对抗 TProxy 的手段）：
设置 → 网络直连 → **复制直连规则**，粘到 shellclash 自定义规则里，放在代理规则之前。

界面上的验证入口：

- 工具栏「网络」按钮 → 诊断面板，逐渠道显示系统 DNS / DoH 真实 IP / 目标归属 / **真实出口国家**
- 状态栏常驻显示 `出口 US/LAX · 已被代理转发` 或 `直连 x.x.x.x · 中国大陆`

### 开机自启
- 写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，无需管理员权限
- 写入的是启动命令：exe 运行写 exe 路径，源码运行写 `pythonw.exe main.py`（不闪黑框）
- 开机自启时直接驻留托盘，不弹窗打扰

---

## 数据来源：官方没有公开 API

查证结果：腾讯 / 魔方**没有对外公开**《洛克王国：世界》远行商人的数据接口。因此程序把接口地址全部做成可替换配置，并按顺序尝试：

| 渠道 id | 说明 | 需 Key |
| --- | --- | --- |
| `official` | **官方接口预留位**，地址默认为空；填入后最优先调用 | 否 |
| `cn_mirror` | **大陆反代 · 自建**，填自己的境内反代地址；填好后优先使用，目标在大陆不会被代理转发 | 否 |
| `onebiji` | **社区数据 · 每日商人查询器（中文）**。中文站点、免 Key，页面直接列出今天 4 轮的全部商品：名称 / 价格 / 限购 / 图标 / 分类 / 每件在哪些轮次售卖。**默认渠道** | 否 |
| `bwiki` | **哔哩哔哩 Wiki · 商店目录**（预留位，默认不填地址）。只有「商店一览」目录，不列价格、没有轮次与每轮货架，**不能当数据源**；填了地址也只会得到明确报错 | 否 |
| `yxsr` | 社区精简接口，仅商品名。2026-10 实测其证书已是自签名，程序会拒绝连接，仅作历史渠道保留 | 否 |
| `shallow` | 洛克魔法书，第三方付费服务；**服务器在中国大陆**（实测 `124.226.74.x`，140ms），是现有渠道里唯一天然满足「接口在境内」的 | 是 |
| `custom` | 自定义接口，任意返回 JSON 的地址都可接入 | 视情况 |

自动降级：`official（若已填）→ cn_mirror（若已填）→ onebiji → bwiki（若已填）→ yxsr → shallow（若已填 Key）→ custom`。

开启「优先大陆渠道」后，降级顺序会按渠道所在地重排：**大陆渠道优先尝试**
（`cn_mirror` / `onebiji` / `bwiki` / `shallow`），境外渠道排在后面。显式选中的当前渠道始终排在第一位。

实测对比（同一台机器、同一时刻）：

| 渠道 | 建连目标 | 是否大陆 | 说明 |
| --- | --- | --- | --- |
| `onebiji` | 境内 CDN | **是** | 直连，少一层海外中转 |
| `shallow` | `124.226.74.51`（广西电信） | **是** | 直连，约 140ms |
| 旧的 `rkw`（已下线） | `104.21.43.145`（Cloudflare） | 否 | 419–590ms，经 VPN 节点；数据其实也来自 `onebiji`，只是多了一层缓存，换货后更慢 |

`1.6.0` 起移除了旧的英文镜像 `rkw`：它托管在境外 Cloudflare、下游缓存更厚，换货后往往要等几分钟才更新；
而它抓的正是 `onebiji` 这个中文查询器页面。直连数据源可以少一层中转，也避免"英文界面 + 境外链路"。

社区接口由第三方维护，随时可能变更。失效时改 `providers.onebiji.base_url` 即可，不必改代码。

> 程序只做数据聚合展示，不修改游戏、不代登录、不涉及账号信息。

---

## 运行

### 方式一：直接跑 exe（推荐）

**[⬇ 下载最新版 RocoMerchantAssistant.exe](https://github.com/jiahuli/roco-merchant-assistant/releases/latest/download/RocoMerchantAssistant.exe)**

这条链接是**固定直链**，永远指向当前最新版本，不需要随版本更新。

- 单文件、绿色免安装，可拷到任意 Windows 10/11 机器
- 首次启动约 1 秒（单文件解包）
- **未做代码签名**，首次运行时 SmartScreen 可能提示"未知发布者"，选择"仍要运行"即可
- 想装旧版本：到 [Releases](https://github.com/jiahuli/roco-merchant-assistant/releases) 按 tag 取，每个版本各自保留自己的 exe

### 方式二：源码运行

```bat
scripts/run.bat
:: 或
python main.py
```

需要 Python 3.9+（安装时勾选 `tcl/tk and IDLE`）。可选增强：

```bat
pip install pillow
```

### 方式三：自己打包

```bat
scripts/build.bat
```

脚本会自动创建 `.buildenv` 虚拟环境、安装 PyInstaller、生成图标、按 `app.spec` 打包，产物在 `dist\远行商人助手.exe`。

---

## 发布与归档

仓库**不保存 exe**（`dist/` 已列入 `.gitignore`）。二进制只作为 Release 资产分发，避免每发一版就在 git 历史里多攒一个 14 MB 的 blob。

### 「最新版本」怎么判定
- 版本号唯一来源是 `config.py` 的 `APP_VERSION`，tag 固定为 `v{APP_VERSION}`
- GitHub 的 `/releases/latest` 按 **tag 的语义化版本排序**（排除 draft 与 prerelease），**不是**按发布时间
- 因此：tag 必须 `vMAJOR.MINOR.PATCH` 严格递增；正式版不要用 `--prerelease` 发布，否则 latest 会指回旧版

### 命名规则
| 场景 | 资产名 | 说明 |
| --- | --- | --- |
| 最新 Release | `RocoMerchantAssistant.exe` | 固定名，`/releases/latest/download/` 依赖它 |
| 被新版本取代后 | `RocoMerchantAssistant-v{tag}.exe` | 自动改名归档，下载后能看出是哪一版 |

改名由 `tools/release.py` 的 `archive_previous()` 完成：新建 Release 后，它会把**其它所有 Release** 里叫 `RocoMerchantAssistant.exe` 的资产重命名为带版本号的形式；当前版本始终占用固定名。

### 展示效果
- **Releases 页**：只有最新版的资产名是 `RocoMerchantAssistant.exe`，其余历史版本挂在各自 tag 下、名字带版本号
- **README**：只写一条固定直链，不随版本改动
- **仓库文件树**：没有 exe，只有源码与脚本

### 改动位置与参数
- `tools/release.py` —— 发布脚本。`STABLE_ASSET` 常量控制固定名，`archive_previous()` 控制归档；`--asset` 指定 exe 路径，`--draft` 先发草稿，`--archive-only` 只归档不发布
- `.github/workflows/release.yml` —— CI。触发条件 `on.push.tags: v*`；`permissions.contents: write` 是上传资产的前提；`env.GH_TOKEN` 取 `secrets.GITHUB_TOKEN`；`actions/checkout` 需 `fetch-depth: 0`（脚本要查 tag）
- `config.py` —— `APP_VERSION`，改这里即可发新版
- `.gitignore` —— `dist/` 行，决定了 exe 不入库

> 已发布过的旧 blob 仍留在 git 历史里，工作区删掉不会缩小 `.git`。要彻底清理需重写历史（`git filter-repo --path dist/ --invert-paths` 后强推），本项目未执行——因为会改写所有 commit hash。

---

## 配置

```
Windows : %APPDATA%\RocoMerchant\config.json
其他系统 : ~/.roco_merchant/config.json
```

同目录下还有 `state.json`（通知去重）、`logs\`（日志）、`cache\icons\`（默认图标缓存，可改）。

exe 与源码运行**共用同一份配置**。

### 主要字段

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `provider` | `onebiji` | 当前数据渠道（1.6.0 起默认中文查询器，旧值 `rkw` 会自动迁移） |
| `auto_fallback` | `true` | 失败时自动降级 |
| `request_timeout` | `15` | 单次请求超时（秒） |
| `auto_recheck_after_refresh` | `true` | 换货后接口未刷新时自动重试 |
| `recheck_delay_seconds` | `300` | 只剩全天商品且内容未变时，延后多久重取（30–3600 秒） |
| `recheck_max_attempts` | `3` | 上述情形的最大重取次数（0 = 关闭该判据，0–10） |
| `recheck_give_up_policy` | `stop` | 次数用完后：`stop` 放弃本轮 / `slow` 转慢速续试 |
| `recheck_slow_interval_seconds` | `1800` | `slow` 的续试间隔（300–3600 秒） |
| `recheck_slow_max_attempts` | `0` | `slow` 的续试上限（0 = 不限，直到换货；0–20） |
| `force_direct` | `true` | 剥离代理变量 + 禁用系统代理，全进程不走 HTTP 代理 |
| `doh_enabled` | `true` | 用 DoH 取真实 IP，绕开 fake-ip 劫持 |
| `prefer_cn_region` | `true` | 优先选择中国大陆渠道 |
| `verify_egress` | `true` | 每次查询校验真实出口（多一次轻量请求） |
| `query_times` | `["08:05","12:05","16:05","20:05"]` | 定时查询时间点 |
| `notify_enabled` | `true` | 通知总开关 |
| `notify_min_level` | `4` | 提醒阈值：3 稀有 / 4 史诗 / 5 传说 |
| `notify_native` | `true` | Windows 系统通知 |
| `notify_toast` | `true` | 允许应用内浮窗兜底（原生成功时不出现） |
| `notify_sound` / `notify_flash` | `true` | 提示音 / 任务栏闪烁 |
| `webhook_url` | `""` | 外部推送地址 |
| `icon_cache_path` | `""` | 图标缓存目录，空 = 默认 |
| `log_dir` | `""` | 日志目录，空 = `%APPDATA%\RocoMerchant\logs` |
| `autostart` | `false` | 开机自启 |
| `close_to_tray` | `true` | 关闭窗口最小化到托盘 |
| `tray_enabled` | `true` | 显示托盘图标 |
| `floating_enabled` | `false` | 悬浮窗开关 |
| `floating_topmost` | `true` | 悬浮窗置顶 |
| `floating_geometry` | `""` | 悬浮窗位置（自动记忆） |
| `floating_show_icons` | `true` | 悬浮窗是否显示物品图标 |
| `window_geometry` | `""` | 主窗口位置尺寸（自动记忆） |

---

## 目录结构

```
roco-merchant/
├── main.py                     # 入口：版本检查、DPI、单实例、协议参数、日志初始化
├── app.spec                    # PyInstaller 配置（单文件 / 无控制台 / 图标）
├── requirements.txt            # 无必装依赖；Pillow 为可选增强
├── config.py                   # 配置读写、渠道表、缓存/日志目录解析、资源路径
├── providers.py                # 数据渠道与自动降级、字段自适应映射
├── models.py                   # MerchantItem / MerchantSnapshot
├── freshness.py                # 货架新鲜度判定：轮次落后 / 只剩全天商品 / 本轮商品消失
├── numerals.py                 # 阿拉伯数字 → 中文读法
├── rarity.py                   # 珍惜度判定规则
├── cache.py                    # 图标磁盘缓存、异步加载、目录迁移
├── scheduler.py                # 定时调度与倒计时
├── monitors.py                 # 多显示器边界（EnumDisplayMonitors）
├── tray.py                     # 系统托盘（Shell_NotifyIcon，独立线程消息循环）
├── autostart.py                # 开机自启（HKCU Run 键）
├── single_instance.py          # 单实例互斥与唤醒
├── notifier.py                 # 通知编排：原生优先 / 浮窗兜底 / 声音 / 闪烁 / Webhook
├── notifier_native.py          # WinRT Toast、AppUserModelID、rocomesh:// 协议注册
├── applog.py                   # 日志与异常钩子
├── selftest.py                 # 自测（233 项）
│
├── scripts/                    # 所有可双击运行的脚本
│   ├── run.bat                 #   源码启动
│   ├── build.bat               #   一键打包 exe
│   └── sync.bat                #   一键同步到 GitHub（自测 → 提交 → 推送）
│
├── packaging/
│   └── version_info.txt        # exe 文件版本信息（被 app.spec 引用）
│
├── tools/                      # 开发期工具（不随 exe 分发）
│   ├── publish.py              #   改动同步：自测门禁 + 白名单提交 + 推送
│   ├── release.py              #   发布：上传 exe 到 Release + 归档旧版
│   ├── build_cn_ranges.py      #   重新生成 data/cn_ipv4.txt
│   └── make_icon.py            #   纯标准库生成 assets/app.ico
│
├── docs/
│   ├── development.md          # 开发交接：数据流、轮次模型、可配置项、同步流程
│   ├── cn-mirror.md            # 自建大陆反代（Nginx / 云函数两套方案）
│   ├── release-notes.md        # 当前版本的发布说明（发布时作为 Release 正文）
│   └── ci/release.yml          # CI 模板：复制到 .github/workflows/ 后生效
│
├── data/cn_ipv4.txt            # 中国大陆 IPv4 段（被打进 exe）
├── assets/app.ico              # 应用图标
├── ui/                         # 界面层
│   ├── theme.py                #   配色、字体、圆角绘制
│   ├── widgets.py              #   圆角按钮、珍惜度徽章、商品卡片、滚动容器
│   ├── app_window.py           #   主窗口
│   ├── floating_window.py      #   悬浮窗
│   ├── log_viewer.py           #   日志查看器
│   ├── network_dialog.py       #   网络与出口诊断面板
│   └── settings_dialog.py      #   设置面板
│
├── build/ dist/ .buildenv/     # 构建产物与构建环境（.gitignore，不入库）
└── LICENSE / README.md
```

---

## 自测

```bat
python selftest.py
```

覆盖 273 项断言，包括：数字转换、珍惜度、轮次计算、定时触发与去重、渠道降级、
通知 XML 与去重、图标缓存与迁移、缓存/日志目录回退、自启命令拼装、单实例互斥与唤醒、
悬浮窗渲染与收起、多屏边界（合成三屏，含左侧负坐标副屏）、真实双屏下的副屏落位与持久化、
托盘创建与清理、关闭到托盘后仍能恢复、界面异常落盘、悬浮窗图标开关（开/关后行数与文字不变）、
假刷新识别与重取收敛（内容未变 / 货架缩水 / 换渠道 / 打烊 / 慢速续试与上限）、
设置面板的重取节奏与收尾方式校验、同步脚本的白名单与提交信息拼装。

自测会备份并还原 `config.json` / `state.json` 与注册表自启项，跑完不改变系统状态。

---

## 同步到 GitHub

改完并自测通过后，用一条命令把这次变更同步上去（自测 → 白名单收集 → 提交 → 推送）：

```bat
:: 在仓库根目录执行（脚本内部会自己回到根）
scripts\sync.bat                     :: 或 python tools\publish.py
scripts\sync.bat -m "fix(x): 修好了"  :: 指定提交信息
scripts\sync.bat --tag v1.5.0        :: 顺便打标签并推送
python tools\publish.py --dry-run    :: 只看会提交哪些文件
```

脚本默认跳过 `dist/`、`build/`、`__pycache__/`、`logs/`、`.github/` 等不该入库的东西；
CI 工作流以模板形式放在 `docs/ci/release.yml`，需要时复制回 `.github/workflows/`
（推送 workflow 需要带 `workflow` 权限的 token）。规则、失败处理与"同步 ≠ 发版"的区别见
**[docs/development.md](docs/development.md) 第 8 章**。

---

## 已知限制

- **未做代码签名**：SmartScreen 会提示"未知发布者"
- 数据依赖第三方社区接口，接口变更/失效时需手动改地址
- 珍惜度是根据名称与价格的**推断**，不是官方字段，可能与实际有出入
- 仅在 Windows 10/11 上测试过；托盘与原生通知依赖 Windows API，macOS / Linux 未适配
- 高 DPI 混用（不同缩放比的多显示器）下 Tk 会被系统位图拉伸，字会略糊

---

## License

[MIT](LICENSE) © 2026 jiahuli

数据与图标版权归腾讯 / 魔方工作室及原数据提供方所有，本项目仅做个人学习与查询使用。
