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

## 功能

### 定时查询
- 默认 **08:05 / 12:05 / 16:05 / 20:05**（商人 08:00 / 12:00 / 16:00 / 20:00 换货，留 5 分钟给接口刷新）
- 时间点可自定义，内置三套预设；手动刷新（`F5`）
- 顶部实时倒计时到下一轮换货；打烊时段（00:00–08:00）显示下次出摊时间

### 货架展示
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
| `rkw` | 社区镜像（聚合自第三方工具站），字段最全：价格 / 限购 / 图标 / 轮次 / 下次刷新。**默认** | 否 |
| `yxsr` | 社区精简接口，仅商品名 | 否 |
| `shallow` | 洛克魔法书，第三方付费服务 | 是 |
| `custom` | 自定义接口，任意返回 JSON 的地址都可接入 | 视情况 |

自动降级：`official（若已填）→ rkw → yxsr → shallow（若已填 Key）→ custom`。

社区接口由第三方维护，随时可能变更。失效时改 `providers.rkw.base_url` 即可，不必改代码。

> 程序只做数据聚合展示，不修改游戏、不代登录、不涉及账号信息。

---

## 运行

### 方式一：直接跑 exe

从 [Releases](../../releases) 或仓库内 `dist/远行商人助手.exe` 下载，双击运行。

- 单文件、绿色免安装，可拷到任意 Windows 10/11 机器
- 首次启动约 1 秒（单文件解包）
- **未做代码签名**，首次运行时 SmartScreen 可能提示"未知发布者"，选择"仍要运行"即可

### 方式二：源码运行

```bat
run.bat
:: 或
python main.py
```

需要 Python 3.9+（安装时勾选 `tcl/tk and IDLE`）。可选增强：

```bat
pip install pillow
```

### 方式三：自己打包

```bat
build_exe.bat
```

脚本会自动创建 `.buildenv` 虚拟环境、安装 PyInstaller、生成图标、按 `app.spec` 打包，产物在 `dist\远行商人助手.exe`。

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
| `provider` | `rkw` | 当前数据渠道 |
| `auto_fallback` | `true` | 失败时自动降级 |
| `request_timeout` | `15` | 单次请求超时（秒） |
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
| `window_geometry` | `""` | 主窗口位置尺寸（自动记忆） |

---

## 目录结构

```
roco-merchant/
├── main.py                 # 入口：版本检查、DPI、单实例、协议参数、日志初始化
├── run.bat                 # 源码启动
├── build_exe.bat           # 一键打包
├── app.spec                # PyInstaller 配置（单文件 / 无控制台 / 图标）
├── version_info.txt        # exe 文件版本信息
├── config.py               # 配置读写、渠道表、缓存/日志目录解析、资源路径
├── providers.py            # 数据渠道与自动降级、字段自适应映射
├── models.py               # MerchantItem / MerchantSnapshot
├── numerals.py             # 阿拉伯数字 → 中文读法
├── rarity.py               # 珍惜度判定规则
├── cache.py                # 图标磁盘缓存、异步加载、目录迁移
├── scheduler.py            # 定时调度与倒计时
├── monitors.py             # 多显示器边界（EnumDisplayMonitors）
├── tray.py                 # 系统托盘（Shell_NotifyIcon，独立线程消息循环）
├── autostart.py            # 开机自启（HKCU Run 键）
├── single_instance.py      # 单实例互斥与唤醒
├── notifier.py             # 通知编排：原生优先 / 浮窗兜底 / 声音 / 闪烁 / Webhook
├── notifier_native.py      # WinRT Toast、AppUserModelID、rocomesh:// 协议注册
├── applog.py               # 日志与异常钩子
├── selftest.py             # 自测（84 项）
├── tools/make_icon.py      # 纯标准库生成 assets/app.ico
├── assets/app.ico          # 应用图标（构建 exe 用）
├── dist/                   # 打包产物
└── ui/
    ├── theme.py            # 配色、字体、圆角绘制
    ├── widgets.py          # 圆角按钮、珍惜度徽章、商品卡片、滚动容器
    ├── app_window.py       # 主窗口
    ├── floating_window.py  # 悬浮窗
    ├── log_viewer.py       # 日志查看器
    └── settings_dialog.py  # 设置面板
```

---

## 自测

```bat
python selftest.py
```

覆盖 84 项断言，包括：数字转换、珍惜度、轮次计算、定时触发与去重、渠道降级、
通知 XML 与去重、图标缓存与迁移、缓存/日志目录回退、自启命令拼装、单实例互斥与唤醒、
悬浮窗渲染与收起、多屏边界（合成三屏，含左侧负坐标副屏）、真实双屏下的副屏落位与持久化、
托盘创建与清理、关闭到托盘后仍能恢复、界面异常落盘。

自测会备份并还原 `config.json` / `state.json` 与注册表自启项，跑完不改变系统状态。

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
