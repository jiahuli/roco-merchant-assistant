# 开发交接说明（给接手的 AI / 开发者）

读完这一份就能动手，不必翻历史对话。项目总量约 7600 行 Python，**零第三方依赖**（打包用 PyInstaller 除外）。

---

## 1. 一句话定位

Windows 桌面工具：定时抓取《洛克王国：世界》远行商人货架，展示图标 / 珍惜度 / 中文价格，稀有物品弹系统通知。
数据来自**第三方社区接口**（官方无公开 API），接口地址全部可替换。

---

## 2. 数据源真相（先记住这四条，少走很多弯路）

1. **没有官方 API**。腾讯/魔方未开放该接口，`providers.official` 只是预留位，地址为空时自动跳过。
2. 默认渠道 `rkw` = `https://rocokingdomworld.org/api/merchant`，免费免 Key，字段最全。
3. **各渠道实测归属**（2026-10 实测，用 `netdirect.diagnose()` 可复现）：

   | 渠道 | 真实 IP | 大陆 | 延迟 | 备注 |
   |---|---|---|---|---|
   | `rkw` | `104.21.43.145` / `172.67.180.76`（Cloudflare） | 否 | 419–590ms | 默认 |
   | `yxsr` | `64.29.17.195`（Vercel） | 否 | — | 仅商品名 |
   | `shallow` | `124.226.74.51`（广西电信） | **是** | **140ms** | 需自备 Key |
   | `cn_mirror` | 用户自建 | 是 | 取决于部署 | 见 `docs/cn-mirror.md` |

4. **接口返回体关键字段**（`rkw`）：
   ```
   status         "open" | "closed"          （00:00–08:00 打烊）
   round          当前轮次 1..4
   expectedRound  按时间应处于第几轮         ← 与 round 不等 = 上游缓存未刷新
   stale          上游自报是否过期
   startedAtBeijing / nextRefreshBeijing     "YYYY-MM-DD HH:MM:SS"（北京时间）
   items[]        当前货架                   每项含 name/price/limit/image/category/rounds
   goods_list[]   与 items 同构（当前实现只读 items）
   ```

---

## 3. 时间与轮次模型（bug 高发区）

- 一天 **4 轮**，08:00 / 12:00 / 16:00 / 20:00 换货；00:00–08:00 打烊。
- `ROUND_HOURS = (8, 12, 16, 20)`（`providers.py`）；`TOTAL_ROUNDS = 4`（`models.py`），
  两者必须一致，`selftest.test_rounds_parsing` 会校验。
- 每件商品带 `rounds` 元组：`(1,2,3,4)` = **全天在售**（条件球、棱镜球这类）；
  `(3,)` = 只在第 3 轮卖。判定入口是 `MerchantItem.is_all_day`。
- **`snapshot.is_round_lagging`**：`round_index < expected_round` 即为"接口还在上一轮"。
- 滞后时 `MerchantApp._check_round_lag()` 按 `ROUND_RETRY_DELAYS = (30,60,120,180,240)` 秒重试，
  换到新轮次或重试耗尽即停。**这是 1.4.1 修的核心 bug**：之前只在固定时间点查一次，
  查到旧轮次也照样渲染，导致"换货后新商品不出现、全天商品一直在"。

---

## 4. 文件结构与职责（40 个受版本控制的文件）

### 核心逻辑（项目根目录，按依赖顺序）

| 文件 | 行 | 职责 |
|---|---|---|
| `main.py` | 115 | 入口。单实例判定 → `netdirect.install()` → 日志 → 建窗口 → 监听"露面"信号 |
| `config.py` | 304 | `Config` 数据类 + `DEFAULT_PROVIDERS` + 路径解析 + 图标缓存目录校验 |
| `models.py` | 119 | `MerchantItem`（含 `rounds` / `is_all_day`）、`MerchantSnapshot`（含 `is_round_lagging`） |
| `numerals.py` | 80 | 数字 → 中文读法（`3200000` → 三百二十万），无依赖，易测 |
| `rarity.py` | 109 | 珍惜度五档推断（关键词 → 分类 → 价格三层），官方数据无此字段 |
| `providers.py` | 583 | 6 个渠道实现 + 通用 JSON 映射 + 轮次过滤 + 降级顺序。**改接口只动这里** |
| `netdirect.py` | 589 | 直连网络层：剥代理 / DoH / 按 IP 直连 / CN 段判定 / 出口诊断 / Clash 规则生成 |
| `cache.py` | 275 | 图标两级缓存（磁盘 `sha1(url).png` + 内存 `PhotoImage`）+ 目录迁移 |
| `scheduler.py` | 124 | 1 秒节拍，到点触发；`next_run()` 计算下一次 |
| `notifier.py` | 321 | 通知编排：原生优先 → 浮窗兜底 → 提示音/闪烁/Webhook，去过重 |
| `notifier_native.py` | 195 | WinRT Toast（PowerShell `-EncodedCommand` 内联，**不能落 .ps1**） |
| `tray.py` | 473 | 系统托盘（纯 ctypes `Shell_NotifyIcon`，独立线程消息循环） |
| `monitors.py` | 216 | 多显示器枚举与边界纯函数（`EnumDisplayMonitors`） |
| `autostart.py` | 101 | 开机自启（`HKCU\...\Run`） |
| `single_instance.py` | 106 | 命名互斥 + 命名事件唤醒（`rocomesh://` 回跳落点） |
| `applog.py` | 224 | 日志（1MB×3 滚动）+ 三类异常钩子（`sys` / `threading` / Tk 回调） |

### 界面（`ui/`）

| 文件 | 行 | 职责 |
|---|---|---|
| `ui/app_window.py` | 1082 | 主窗口：工具栏 / 货架卡片网格 / 状态栏 / 重试与刷新编排 / 托盘命令消费 |
| `ui/settings_dialog.py` | 600 | 设置面板（定时 / 渠道 / 网络直连 / 通知 / 悬浮窗 / 缓存 / 日志 / 启动） |
| `ui/floating_window.py` | 392 | 置顶悬浮窗（拖动 / 收起 / 图标开关 / 多屏记忆） |
| `ui/network_dialog.py` | 198 | 网络与出口诊断面板 |
| `ui/log_viewer.py` | 185 | 日志查看器 |
| `ui/widgets.py` | 490 | 自绘控件：`PillButton` / `ScrollFrame` / `StatBlock` / `ItemCard` / `RarityBadge` / `Tooltip` |
| `ui/theme.py` | 115 | 颜色与字体（深色主题常量） |

### 数据与工具

| 文件 | 职责 |
|---|---|
| `data/cn_ipv4.txt` | 5499 条中国大陆 IPv4 段（APNIC 分配表，83KB），`is_cn_ip()` 用它 |
| `tools/build_cn_ranges.py` | 重新生成上表（`--source` 可用本地文件离线生成，结果可复现） |
| `tools/release.py` | 发布：建 Release、上传 exe、改固定名、归档旧版 |
| `tools/make_icon.py` | 纯标准库生成 `assets/app.ico`（手写 PNG + ICO 封装） |
| `app.spec` | PyInstaller 配置。**新增数据文件必须加进 `datas`**（如 `data/cn_ipv4.txt`），否则打包后功能静默失效 |
| `selftest.py` | 157 项断言，纯标准库 + 真实窗口，**跑完会自动还原 config/state/注册表** |
| `docs/cn-mirror.md` | 自建大陆反代（Nginx / 云函数两套配置） |
| `README.md` | 用户视角文档 |
| `LICENSE` | MIT |

---

## 5. 一次查询的完整链路

```
scheduler 到点 / 用户点刷新
  → MerchantApp.refresh()                     （_loading 防重入，重试计数归零）
  → 线程池 fetch_snapshot(config)
      → 按 provider 顺序试：official → cn_mirror → rkw → yxsr → shallow → custom
      → BaseProvider._request()  →  netdirect.fetch_json()
            → DoH 取真实 IP（绕 fake-ip）→ 按 IP 建 TLS 连接（SNI 保持域名）
      → RkwProvider.fetch() 解析 status/round/expectedRound/items
            → build_items() 解析 rounds
            → filter_by_round() 剔除明确不属于本轮的条目
      → verify_egress 时附一次 /cdn-cgi/trace 结果
  → 回主线程 _on_fetched()
      → _render()            卡片 + 统计 + 滞后提示
      → _check_round_lag()   轮次落后则排程重试
      → _maybe_notify()      达到珍惜度阈值且未推过 → 通知
```

结果通过 `queue.Queue` 回主线程（**Tk 不允许跨线程操作控件**），由 `_pump_results()` 每 80ms 消费。

---

## 6. 配置与运行态文件

```
%APPDATA%\RocoMerchant\
  config.json    用户配置（与源码运行共用同一份）
  state.json     通知去重状态
  logs\app.log   滚动日志
  cache\icons\   默认图标缓存（可改）
```

修改配置项的流程：`config.py` 加字段 → `load()` 里做类型清洗 → `settings_dialog` 加控件 + `_save()` 赋值 → README 配置表补一行。

---

## 7. 踩过的坑（别重犯）

| 坑 | 现象 | 正解 |
|---|---|---|
| **git 提交含 `.github/workflows`** | token 缺 `workflow` scope，push 与 **tag 都被连带拒绝** | 不要把 workflow 放进 main 的历史；工作区保留未跟踪，或先在网页建文件 |
| **`gh api check=False` 的 404** | stdout 是 `{"message":"Not Found"}`，被当成"已存在" | 判断 `"id" in data` 才算存在 |
| **PyInstaller 漏 datas** | 打包后大陆判定恒为 False | `app.spec` 的 `datas` 加 `("data/cn_ipv4.txt","data")`，并用启动日志核验条数 |
| **`-WindowStyle Hidden` + `CREATE_NO_WINDOW`** | PowerShell 子进程稳定挂死 20s | 两者都不要加，改用 `-EncodedCommand` |
| **ctypes 未声明 `restype`** | 源码正常、**exe 崩**（64 位句柄被截断） | `tray.py` 的 `_declare_types()` 一次性声明 |
| **`self._w = width`** | 覆盖 Tk 内部路径名，界面起不来 | 改属性名 |
| **`winfo_screenwidth()`** | 只返回主屏尺寸，副屏拖不过去 | 用 `monitors.enum_monitors()` |
| **threading 里碰 Tk** | 随机崩溃 / 静默失效 | 一律 `queue` + 主线程 `after` |
| **自测污染真实配置** | 测试改过的开关留在用户 config，看着像 bug | `preserve_app_files()` 备份还原 config/state + 注册表 |
| **`rounds` 语义** | 条件球"一直在"被当成没刷新 | 它是 `(1,2,3,4)` 全天商品，正常 |

---

## 8. 开发命令

```bash
python main.py                 # 跑源码（需 Python 3.9+ 且含 tkinter）
python selftest.py             # 157 项自测，应输出"失败 0 项"
python -m compileall -q .      # 语法检查
build_exe.bat                  # 装 PyInstaller 并打包到 dist\
python tools/release.py --asset "dist/远行商人助手.exe" --notes-file docs/release-notes.md
```

发版顺序：改 `config.APP_VERSION` 与 `version_info.txt` → 提交推送 → `git tag -a vX.Y.Z` 推送 →
`tools/release.py`（会把新版资产命名成 `RocoMerchantAssistant.exe`，旧版归档为 `-v{tag}.exe`）。

---

## 9. 已知限制 / 可继续做的事

- **透明代理（路由器 TProxy）无法从客户端绕过**：这是协议事实。现有对策是让目标落在中国大陆
  （`cn_mirror` / `shallow`），或在路由器侧加 DIRECT 规则。README「网络直连」一节有完整论证。
- CI（`.github/workflows/release.yml`）已写好但**从未在真实 runner 上跑过**，需 `workflow` scope 才能推。
- 珍惜度是**推断**结果，非官方字段；`rarity.py` 里的关键词表可按实际掉落调整。
- `goods_list` 目前未使用（与 `items` 同构），如上游将来把"全天商品"只放这里，需要合并两个列表。
- 界面为固定深色主题，无浅色模式。
- 仓库仍保留历史 exe blob（v1.2/v1.3），未重写历史。

---

## 10. 改动指南（想改什么去哪）

| 想改 | 去这里 |
|---|---|
| 接口地址 / 加新渠道 | `config.DEFAULT_PROVIDERS` + `providers.py` 加类并注册进 `_REGISTRY` |
| 换货时间点 | `providers.ROUND_HOURS` + `config.QUERY_TIME_PRESETS` |
| 重试节奏 | `ui/app_window.MerchantApp.ROUND_RETRY_DELAYS` |
| 珍惜度规则 | `rarity.py` 的 `RARITY_TABLE` 与关键词表 |
| 通知内容 | `notifier._compose()` |
| 卡片外观 | `ui/widgets.ItemCard` + `ui/theme.py` 颜色 |
| 网络绕过策略 | `netdirect.py`（剥代理 / DoH / CN 段） |
