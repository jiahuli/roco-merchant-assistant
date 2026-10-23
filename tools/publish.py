"""一键把"这次改好的文件"同步到 GitHub。

为什么要这个脚本
----------------
手动 `git add -A` 有两个坑：

1. 会顺手把 `dist/` 里的 exe、`__pycache__`、临时 log 带上去，仓库越推越胖；
   `.github/workflows/` 还需要额外的 `workflow` scope，推不上会整次失败。
2. 忘了跑自测就推，等于把"没验证过"当成"更新成功"。

所以把流程固定成四步，顺序不可跳过：**自测 → 收集可同步文件 → 提交 → 推送**。

用法
----
    python tools/publish.py                          # 自动生成提交信息
    python tools/publish.py -m "fix(x): 修好了"       # 指定提交信息
    python tools/publish.py --tag v1.4.3             # 顺便打注释标签并推送
    python tools/publish.py --dry-run                # 只打印会提交哪些文件
    python tools/publish.py --skip-tests             # 明确跳过自测（不推荐）
    python tools/publish.py --push-only              # 只推送已有的本地提交

注意：自测需要 tkinter。若 `python` 指向的解释器没有 tkinter，
请用完整路径运行（见 README「自测」一节）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 可以随代码同步的文件类型
INCLUDE_SUFFIXES = {
    ".py", ".md", ".txt", ".bat", ".spec", ".json", ".yml", ".yaml",
    ".ico", ".png", ".cfg", ".toml", ".gitignore", ".gitattributes",
}
# 永远不推的目录前缀（构建产物 / 缓存 / 需要额外权限的 CI）
EXCLUDE_PREFIXES = (
    "dist/", "build/", "__pycache__/", ".workbuddy/", ".github/",
    "logs/", ".venv/", "venv/", "data/cache/",
)
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".exe", ".zip", ".log", ".tmp", ".bak"}
# 单个文件的上限：超过基本就是误入的产物，别推
MAX_BYTES = 20 * 1024 * 1024


# ---------------------------------------------------------------------------
# 纯函数：便于自测
# ---------------------------------------------------------------------------
def normalize(path: str) -> str:
    """统一成正斜杠相对路径，Windows 下 git 输出可能是反斜杠。

    注意不能写成 `lstrip("./")`：那会按字符集剥离，把 `.github/` 变成
    `github/`，排除规则就漏了。
    """
    rel = path.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def is_publishable(path: str, size: int = 0) -> bool:
    """这个改动文件该不该跟着代码一起上传。"""
    rel = normalize(path)
    if not rel:
        return False
    lowered = rel.lower()
    if any(lowered.startswith(prefix) for prefix in EXCLUDE_PREFIXES):
        return False
    suffix = Path(lowered).suffix
    if suffix in EXCLUDE_SUFFIXES:
        return False
    if suffix not in INCLUDE_SUFFIXES and Path(lowered).name not in INCLUDE_SUFFIXES:
        return False
    return size <= MAX_BYTES


def build_commit_message(changes: list[str], version: str = "",
                         now: datetime | None = None) -> str:
    """没给 -m 时用的兜底提交信息：说清同步了哪几类文件。"""
    now = now or datetime.now()
    kinds = []
    if any(p.endswith(".py") for p in changes):
        kinds.append("代码")
    if any(p.endswith(".md") for p in changes):
        kinds.append("文档")
    if any(p.endswith((".bat", ".yml", ".yaml", ".spec")) for p in changes):
        kinds.append("构建")
    if not kinds:
        kinds.append("文件")
    scope = "、".join(kinds)
    ver = f"（v{version}）" if version else ""
    return f"chore(sync): 同步{scope}改动{ver} {now:%Y-%m-%d}"


def read_version() -> str:
    """从 config.py 读 APP_VERSION，读不到就返回空（不让同步因此失败）。"""
    path = ROOT / "config.py"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("APP_VERSION"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# git 封装
# ---------------------------------------------------------------------------
def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_PAGINATION="off", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} 失败：{message}")
    return result


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"


def collect_changes() -> tuple[list[str], list[str]]:
    """返回 (可同步, 跳过) 两组相对路径。"""
    raw = git("status", "--porcelain").stdout.splitlines()
    keep: list[str] = []
    skipped: list[str] = []
    for line in raw:
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:]
        if " -> " in path:            # 重命名：只跟踪新路径
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        full = ROOT / path
        size = full.stat().st_size if full.exists() and full.is_file() else 0
        (keep if is_publishable(path, size) else skipped).append(path)
    return keep, skipped


def has_tkinter(python: str) -> bool:
    """这个解释器能不能跑自测（自测要建真实窗口）。"""
    try:
        result = subprocess.run([python, "-c", "import tkinter"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def pick_test_python() -> str:
    """挑一个带 tkinter 的解释器跑自测。

    现实里 `python` / `py -3` 常常指向没勾 tcl/tk 的安装，直接跑 selftest 会
    ImportError。这里先认当前解释器，再按 `py -0p` 列出的版本逐个试，
    找到就用，找不到返回空串交给调用方提示。
    """
    if has_tkinter(sys.executable):
        return sys.executable
    try:
        listed = subprocess.run(["py", "-0p"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (listed.stdout or "").splitlines():
        parts = line.strip().split()
        if not parts or not parts[-1].lower().endswith(".exe"):
            continue
        candidate = parts[-1]
        if candidate != sys.executable and has_tkinter(candidate):
            return candidate
    return ""


def run_selftest(python: str) -> bool:
    print(f"→ 跑自测（{Path(python).name} selftest.py）…")
    result = subprocess.run(
        [python, "selftest.py"], cwd=str(ROOT), capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    tail = [line for line in (result.stdout or "").splitlines() if line.strip()][-3:]
    for line in tail:
        print("   " + line)
    if result.returncode != 0 and not tail:
        err_lines = (result.stderr or "").strip().splitlines()
        print("   " + (err_lines[-1] if err_lines else "（无输出）"))
    return result.returncode == 0


def remote_head(branch: str) -> str:
    result = git("ls-remote", "origin", f"refs/heads/{branch}", check=False)
    parts = result.stdout.split()
    return parts[0][:12] if parts else "未获取到"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把本次改动同步到 GitHub")
    parser.add_argument("-m", "--message", default="", help="提交信息（缺省自动生成）")
    parser.add_argument("--tag", default="", help="顺便打注释标签并推送，如 v1.4.3")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不提交")
    parser.add_argument("--skip-tests", action="store_true", help="跳过自测（不推荐）")
    parser.add_argument("--push-only", action="store_true", help="只推送已有提交")
    args = parser.parse_args(argv)

    version = read_version()
    branch = current_branch()

    if args.push_only:
        print(f"→ 只推送：origin/{branch}")
        git("push", "origin", branch)
        print(f"✔ 已推送，远端最新 {remote_head(branch)}")
        return 0

    keep, skipped = collect_changes()
    if not keep:
        print("没有需要同步的改动（工作区干净，或改动都在排除列表里）。")
        if skipped:
            print("已跳过：" + "、".join(skipped[:8]))
        return 0

    print(f"→ 待同步 {len(keep)} 个文件（v{version or '?'}）")
    for path in keep:
        print("   + " + path)
    if skipped:
        print("→ 跳过：" + "、".join(skipped[:10]) + (" …" if len(skipped) > 10 else ""))

    if args.dry_run:
        print("→ --dry-run：未提交、未推送。")
        return 0

    if not args.skip_tests:
        test_python = pick_test_python()
        if not test_python:
            print("✘ 找不到带 tkinter 的 Python，自测无法运行。")
            print("  请安装带 tcl/tk 组件的 Python，或在明确知情时加 --skip-tests。")
            return 2
        if not run_selftest(test_python):
            print("✘ 自测未通过，已中止同步（修好后再跑一次）。")
            return 1
        print("✔ 自测通过")
    else:
        print("⚠ 已按 --skip-tests 跳过自测")

    message = args.message.strip() or build_commit_message(keep, version)
    git("add", "--", *keep)
    staged = git("diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        print("没有实际可提交的内容（可能都是删除后又恢复的空改动）。")
        return 0
    git("commit", "-m", message)
    commit = git("rev-parse", "--short", "HEAD").stdout.strip()
    print(f"✔ 已提交 {commit}：{message.splitlines()[0]}")

    print(f"→ 推送到 origin/{branch} …")
    git("push", "origin", branch)
    print(f"✔ 已推送，远端最新 {remote_head(branch)}")

    if args.tag:
        tag = args.tag if args.tag.startswith("v") else "v" + args.tag
        exists = git("tag", "--list", tag).stdout.strip()
        if exists:
            print(f"⚠ 标签 {tag} 已存在，跳过打标签（推送未受影响）")
            return 0
        git("tag", "-a", tag, "-m", tag)
        git("push", "origin", tag)
        print(f"✔ 已打标签并推送 {tag}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"✘ {exc}")
        print("  常见原因：没有网络、凭据过期、远端有新的提交（先 git pull --rebase）。")
        raise SystemExit(1)
