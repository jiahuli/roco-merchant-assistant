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
- 滞后时 `MerchantApp._evaluate_freshness()` 按 `ROUND_RETRY_DELAYS = (30,60,120,180,240)` 秒重试，
  换到新轮次或重试耗尽即停。**这是 1.4.1 修的核心 bug**：之前只在固定时间点查一次，
  查到旧轮次也照样渲染，导致"换货后新商品不出现、全天商品一直在"。
- **1.4.2 补的坑**：`is_round_lagging` 依赖接口自报 `round`，有两种情况它恒为 False：
  ① `round` 已刷成新轮次、`items` 还是旧货架；② 渠道根本不返回 `round`，代码用本地时间
  兜底（`round_index_at()`），算出来的轮次永远"不落后"。这时再叠加 `filter_by_round()`
  把旧轮次的商品剔掉，货架就只剩 `rounds=(1,2,3,4)` 的全天商品（条件球 / 棱镜球），
  界面卡在这两件上，且不会触发任何重试——直到下一个定时点（4 小时后）。
  现在改由 `freshness.ShelfWatcher` 同时看**货架内容**：

  | 判据 | 触发条件 | 节奏 |
  |---|---|---|
  | `round_lag` | `is_round_lagging` | 30/60/120/180/240 秒阶梯 |
  | `all_day_only` | 只剩全天商品 + 与上次内容完全相同 | `recheck_delay_seconds`（默认 300 秒）× `recheck_max_attempts`（默认 3） |
  | `shelf_shrank` | 只剩全天商品 + 上次还有本轮商品 | 同上 |

  三条判据都要求"只剩全天商品"或"轮次明确落后"，带本轮商品的正常货架不受影响。
  上一份货架指纹会写进 `state.json` 的 `shelf` 字段（`_remember_shelf()` /
  `_restore_shelf_state()`），所以冷启动也能比对。同一份内容在同一轮次放弃过一次后
  不再重取（`ShelfWatcher._exhausted`），跨轮或内容变化后自动恢复。
  常规次数用尽后的收尾方式可选（`stop` / `slow`），取值与影响见第 7 章。

---

## 4. 文件结构与职责（约 40 个受版本控制的文件）

### 核心逻辑（项目根目录，按依赖顺序）

| 文件 | 行 | 职责 |
|---|---|---|
| `main.py` | 115 | 入口。单实例判定 → `netdirect.install()` → 日志 → 建窗口 → 监听"露面"信号 |
| `config.py` | 330 | `Config` 数据类 + `DEFAULT_PROVIDERS` + 路径解析 + 图标缓存目录校验 |
| `models.py` | 120 | `MerchantItem`（含 `rounds` / `is_all_day`）、`MerchantSnapshot`（含 `is_round_lagging`） |
| `numerals.py` | 80 | 数字 → 中文读法（`3200000` → 三百二十万），无依赖，易测 |
| `rarity.py` | 109 | 珍惜度五档推断（关键词 → 分类 → 价格三层），官方数据无此字段 |
| `providers.py` | 585 | 6 个渠道实现 + 通用 JSON 映射 + 轮次过滤 + 降级顺序。**改接口只动这里** |
| `freshness.py` | 409 | 货架新鲜度判定（轮次落后 / 只剩全天商品 / 本轮商品消失），纯状态机，不碰 Tk |
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
| `ui/app_window.py` | 1241 | 主窗口：工具栏 / 货架卡片网格 / 状态栏 / 重试与刷新编排 / 托盘命令消费 |
| `ui/settings_dialog.py` | 723 | 设置面板（定时 / 渠道 / 网络直连 / 通知 / 悬浮窗 / 缓存 / 日志 / 启动） |
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
| `tools/publish.py` | **改动同步**：自测门禁 → 白名单收集 → 提交 → 推送（可选打 tag），见第 8 章 |
| `tools/release.py` | 发布：建 Release、上传 exe、改固定名、归档旧版 |
| `tools/make_icon.py` | 纯标准库生成 `assets/app.ico`（手写 PNG + ICO 封装） |
| `sync.bat` | `tools/publish.py` 的 Windows 一键入口（自测 → 提交 → 推送） |
| `app.spec` | PyInstaller 配置。**新增数据文件必须加进 `datas`**（如 `data/cn_ipv4.txt`），否则打包后功能静默失效 |
| `selftest.py` | 233 项断言，纯标准库 + 真实窗口，**跑完会自动还原 config/state/注册表** |
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
      → _evaluate_freshness()  轮次落后 / 货架停滞则排程重试
      → _remember_shelf()      货架指纹落盘（冷启动也能比对）
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

## 7. 可配置项与选择机制

原则：**能选择的地方都给选择，但默认值必须等于"原来的行为"**。
老 `config.json` 不需要迁移，升级后表现不变；想更激进或更省流量再自己改。

### 7.1 换货校验相关（最常调的一组）

| 配置键 | 界面位置 | 可选范围 | 默认 | 选后对流程的影响 |
|---|---|---|---|---|
| `query_times` | 设置 → 定时查询 | 任意 `HH:MM` 列表 | 08:05 / 12:05 / 16:05 / 20:05 | 只决定"多久查一次"；换货校验的重取是**追加**请求，不改定时点 |
| `auto_recheck_after_refresh` | 同上 | 开 / 关 | 开 | 关 = 查到什么就显示什么，一次都不重取（最省流量，最容易看到旧数据） |
| `recheck_delay_seconds` | 同上 | 30–3600 秒 | 300（5 分钟） | 只剩全天商品且内容与上次相同时，隔多久重取；越小越及时、请求越多 |
| `recheck_max_attempts` | 同上 | 0–10 次 | 3 | 0 = 关闭"内容比对"判据；轮次判据（30/60/120/180/240 秒）不受影响 |
| `recheck_give_up_policy` | 同上 | `stop` / `slow` | `stop` | 常规次数用尽后的收尾方式，见 7.2 |
| `recheck_slow_interval_seconds` | 同上 | 300–3600 秒 | 1800（30 分钟） | 仅 `slow` 生效：慢速续试间隔 |
| `recheck_slow_max_attempts` | 同上 | 0–20 次 | 0（不限） | 仅 `slow` 生效：慢速续试上限，用尽后回到 `stop` 的表现 |

渠道、网络直连、通知、悬浮窗、图标缓存、日志、自启这些配置项与换货校验**正交**：
改它们不会改变校验判据，也不会触发额外的界面分支（完整清单见 README「主要字段」）。

### 7.2 收尾方式：`stop` 与 `slow` 的区别

两条路只在"常规重取次数用尽"这一个点上分叉，前面的判据、阶梯、次数完全相同。

| 策略 | 后续行为 | 每轮请求量 | 界面表现 |
|---|---|---|---|
| `stop`（默认，= 1.4.2 行为） | 本轮不再自动请求，等下一个定时点或用户手动刷新 | 3 次（默认值下） | 药丸「数据滞后」，货架标题「⚠ 只有 N 件全天商品，数据可能未更新」 |
| `slow` | 每 `slow_interval` 再悄悄试一次，换轮 / 内容变化 / 慢速上限用尽 / 手动刷新即停 | 3 + ≤N 次（`N` = 慢速上限，0 为不限，每轮最多 2 次/小时量级） | 药丸「慢速校验」，状态栏「常规重试已用完，30 分钟后再看一眼（慢速 1/不限）」 |

选择怎么生效（一条链，无隐藏状态）：

```
设置面板 _policy_var / _slow_*_var
  → _save() 校验并写 Config
  → MerchantApp._apply_settings() 调 ShelfWatcher.configure(give_up_policy=..., slow_interval=..., slow_max_attempts=...)
  → 下次 observe() 就按新策略返回 Recheck
```

切换策略不会污染状态机：`_exhausted`（同一内容"已放弃"标记）只在 `stop` 策略下生效，
切到 `slow` 后自动让位给 `_slow_max` 计数；内容变化或换轮时两个计数一起清零。

### 7.3 新增一个可配置项的固定套路

1. `config.Config` 加字段（**给默认值，且默认 = 现有行为**）；
2. `Config.load()` 做类型清洗：布尔 `bool()`、数字用 `clamp_int()` 夹取、枚举走白名单，非法值一律回落默认；
3. `ui/settings_dialog` 加控件 + `_save()` 赋值（范围校验在这里给用户看得懂的提示）；
4. `README` 配置表 + 本文档 7.1 各补一行，`selftest.py` 加一条"脏值回落 + 范围夹取"断言。

---

## 8. 改动同步上传（GitHub）

**"更新成功"的定义 = 自测通过 + 变更已推到 `origin/main`**，两步都做完才算完。
一条命令走完：

```bat
sync.bat                                      :: 自测 -> 提交 -> 推送
sync.bat -m "fix(x): 修好了" --tag v1.4.3      :: 指定提交信息，顺便打标签
python tools/publish.py --dry-run             :: 只看会提交哪些文件
python tools/publish.py --push-only           :: 只推送已有提交（网络恢复后用）
```

四步顺序固定，不可跳过：

| 步骤 | 做什么 | 失败怎么办 |
|---|---|---|
| 1. 自测 | 自动挑一个带 `tkinter` 的解释器跑 `selftest.py`（会打印版本与路径），必须"失败 0 项" | 修好再跑；确需跳过用 `--skip-tests`（会在输出里留警告） |
| 2. 收集 | 解析 `git status --porcelain`，按白名单筛出可同步文件并打印 | `--dry-run` 先看一眼，发现误入的产物就补进排除表 |
| 3. 提交 | `git add` 白名单文件 → `git commit -m`（缺省自动生成 `chore(sync): 同步代码、文档改动（vX.Y.Z） 日期`） | 无实际改动会直接退出，不会产生空提交 |
| 4. 推送 | `git push origin <当前分支>` → 打印远端最新短 hash | 非快进先 `git pull --rebase`；凭据过期按 git 自身提示重新登录 |

排除规则（`tools/publish.py` 顶部的常量，改这里即可）：

| 规则 | 覆盖 | 原因 |
|---|---|---|
| `EXCLUDE_PREFIXES` | `dist/` `build/` `__pycache__/` `logs/` `.workbuddy/` `.github/` | 产物、缓存、日志不该入库；`.github/workflows` 需要额外 `workflow` scope，推它会让整次 push（含 tag）失败 |
| `EXCLUDE_SUFFIXES` | `.pyc` `.exe` `.zip` `.log` `.tmp` `.bak` | 同上 |
| `MAX_BYTES` | > 20MB 的文件 | 基本是误入的二进制 |
| `INCLUDE_SUFFIXES` | 源码 / 文档 / 构建脚本 / 图标等 | 白名单思路：不认识的不推，避免"顺手带上不该带的东西" |

两点说明：

- **为什么不 `git add -A`**：见上表第 1、2 行，`.github/` 会让 push 连带 tag 一起被拒（第 9 章「踩过的坑」有记录）。
- **同步 ≠ 发版**：本流程只同步源码与文档，不构建 exe。发版仍需
  `build_exe.bat` → `tools/release.py` → 打 tag，见第 10 章。
- **为什么不做成 git hook**：`.git/hooks/` 不进版本库，换台机器就失效；而"每次更新都同步"
  是跟着仓库走的约定。所以入口放在仓库内（`sync.bat` / `tools/publish.py`），并把
  "自测通过 + 已推送"写成完成的定义。想要强制拦截的话，本机可
  `git config core.hooksPath .githooks` 自己挂 pre-push 钩子——那属于个人配置，不入库。

---

## 9. 踩过的坑（别重犯）

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

## 10. 开发命令

```bash
python main.py                 # 跑源码（需 Python 3.9+ 且含 tkinter）
python selftest.py             # 233 项自测，应输出"失败 0 项"
python -m compileall -q .      # 语法检查
sync.bat                       # 自测 -> 提交 -> 推送 GitHub（见第 8 章）
build_exe.bat                  # 装 PyInstaller 并打包到 dist\
python tools/release.py --asset "dist/远行商人助手.exe" --notes-file docs/release-notes.md
```

发版顺序：改 `config.APP_VERSION` 与 `version_info.txt` → 提交推送 → `git tag -a vX.Y.Z` 推送 →
`tools/release.py`（会把新版资产命名成 `RocoMerchantAssistant.exe`，旧版归档为 `-v{tag}.exe`）。

---

## 11. 已知限制 / 可继续做的事

- **透明代理（路由器 TProxy）无法从客户端绕过**：这是协议事实。现有对策是让目标落在中国大陆
  （`cn_mirror` / `shallow`），或在路由器侧加 DIRECT 规则。README「网络直连」一节有完整论证。
- CI（`.github/workflows/release.yml`）已写好但**从未在真实 runner 上跑过**，需 `workflow` scope 才能推。
- 珍惜度是**推断**结果，非官方字段；`rarity.py` 里的关键词表可按实际掉落调整。
- `goods_list` 目前未使用（与 `items` 同构），如上游将来把"全天商品"只放这里，需要合并两个列表。
- 界面为固定深色主题，无浅色模式。
- 仓库仍保留历史 exe blob（v1.2/v1.3），未重写历史。

---

## 12. 改动指南（想改什么去哪）

| 想改 | 去这里 |
|---|---|
| 接口地址 / 加新渠道 | `config.DEFAULT_PROVIDERS` + `providers.py` 加类并注册进 `_REGISTRY` |
| 换货时间点 | `providers.ROUND_HOURS` + `config.QUERY_TIME_PRESETS` |
| 轮次落后的重试阶梯 | `ui/app_window.MerchantApp.ROUND_RETRY_DELAYS` |
| 货架停滞的重取节奏 | 设置 →「定时查询」→ 延后秒数 / 最多次数（`config.recheck_delay_seconds` / `recheck_max_attempts`） |
| 次数用完后的收尾方式 | 设置 →「定时查询」→ 次数用完后（`stop` / `slow`）→ `freshness.ShelfWatcher` |
| 同步/推送流程 | `tools/publish.py`（排除规则、提交信息都在文件顶部） |
| 新鲜度判据 | `freshness.ShelfWatcher.observe()` |
| 珍惜度规则 | `rarity.py` 的 `RARITY_TABLE` 与关键词表 |
| 通知内容 | `notifier._compose()` |
| 卡片外观 | `ui/widgets.ItemCard` + `ui/theme.py` 颜色 |
| 网络绕过策略 | `netdirect.py`（剥代理 / DoH / CN 段） |
