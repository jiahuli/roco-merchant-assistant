## 本次更新（1.5.0）—— 仓库整理版

> **功能零变更**：本版只整理仓库结构、文档与发布流程。程序行为、配置项、数据格式与 1.4.3
> 完全一致；升级只需替换 exe，不需要迁移任何配置。
>
> **版本线**：1.4.2 / 1.4.3 是只推源码的修复版本（当时未构建 exe），
> 它们的内容（假刷新识别、收尾方式可选）都已包含在本版里。

### 目录结构调整

| 原来 | 现在 | 说明 |
|---|---|---|
| `run.bat` / `build_exe.bat` / `sync.bat`（根目录散落） | `scripts/run.bat` / `scripts/build.bat` / `scripts/sync.bat` | 可双击脚本集中管理，内部统一先回仓库根目录 |
| `version_info.txt` | `packaging/version_info.txt` | 打包期资源与运行期资源分开 |
| `docs/DEVELOPMENT.md` | `docs/development.md` | 文档统一小写 `kebab-case` |
| `.github/workflows/release.yml`（游离、未纳管） | `docs/ci/release.yml` | CI 模板随仓库分发，复制回 `.github/workflows/` 即生效 |

根目录只保留：`main.py`、运行时模块（`*.py`）、`app.spec`、`requirements.txt`、`README.md`、`LICENSE`。

### 隐私脱敏（上传前处理）

- 文档中两处**真实出口 IP**（家宽出口 / 代理出口）替换为 RFC 5737 文档示例地址
  （`203.0.113.x` / `198.51.100.x`），并标注为脱敏版本
- `autostart.py` 注释里的个人安装路径示例（`D:\Apps\...`、`C:\Python314\...`）改为通用占位
- 复查整仓：无密钥、令牌、Webhook 地址、邮箱、手机号、个人绝对路径；
  `config.json` / `state.json` / 日志 / 图标缓存全部在 `.gitignore` 内，不入库

### 清理与规范

- 移除未纳管的 `.github/` 游离目录（内容已完整存入 `docs/ci/release.yml`）
- 清理本地构建残留（`__pycache__/`、`build/`、`dist/`）
- `docs/development.md` 新增「目录与命名规范」：目录职责、命名规则（模块 `snake_case`、
  类 `PascalCase`、脚本全小写、文档 `kebab-case`），并写明两处**刻意例外**
  （运行时模块平铺在根、CI 模板放 `docs/ci/`）及其原因

### 构建与发布

- `scripts/build.bat` 一键打包（复用 `.buildenv`，PyInstaller 6.22.3）
- 版本 1.5.0；exe 文件属性版本同步为 1.5.0.0
- 自测 233 项全部通过；发版流程（构建 → tag → Release 资产）见 `docs/development.md` 第 10 章

## 下载

- `RocoMerchantAssistant.exe` —— 单文件绿色版，Windows 10/11 直接双击运行
- 未做代码签名，SmartScreen 可能提示「未知发布者」，选择「仍要运行」

MIT License
