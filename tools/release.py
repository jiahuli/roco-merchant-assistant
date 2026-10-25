"""发布脚本：把 exe 作为 Release 资产上传，并归档旧版本。

设计要点
--------
* **仓库里不存 exe**：二进制一旦入库，每发一版 git 历史就多一份 14 MB blob。
  这里改成"只走 Release 资产"。
* **最新版用固定文件名** `RocoMerchantAssistant.exe`，这样
  `/releases/latest/download/RocoMerchantAssistant.exe` 永远是当前版本的直链，
  README 只需要写一次链接。
* **旧版本改名归档**：被取代之后，资产重命名为
  `RocoMerchantAssistant-v{tag}.exe`，下载下来能一眼看出是哪一版。
* 版本号的唯一来源是 `config.APP_VERSION`，tag 固定为 `v{版本}`，
  GitHub 的 `/releases/latest` 按 **semver 排序 tag**（排除 draft / prerelease），
  所以 tag 必须严格递增。

用法
----
    python tools/release.py                  # 用 dist 里的 exe 发布当前版本
    python tools/release.py --draft          # 先发草稿
    python tools/release.py --notes-file notes.md
    python tools/release.py --archive-only   # 只做旧版资产归档，不发布
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STABLE_ASSET = "RocoMerchantAssistant.exe"
DEFAULT_EXE = ROOT / "dist" / "远行商人助手.exe"


def version() -> str:
    sys.path.insert(0, str(ROOT))
    from roco.config import APP_VERSION

    return APP_VERSION


# gh 在 Windows 上不一定进 PATH（默认装在 Program Files 下），
# 先按 PATH 找，再退回常见安装位置；都找不到才报错。
_GH_CANDIDATES = (
    "gh",
    r"C:\Program Files\GitHub CLI\gh.exe",
    r"C:\Program Files (x86)\GitHub CLI\gh.exe",
    str(Path.home() / "AppData" / "Local" / "GitHubCLI" / "gh.exe"),
)
_gh_binary: str | None = None


def gh_binary() -> str:
    global _gh_binary
    if _gh_binary:
        return _gh_binary
    for candidate in _GH_CANDIDATES:
        try:
            probe = subprocess.run([candidate, "--version"], capture_output=True,
                                   text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            _gh_binary = candidate
            return candidate
    raise RuntimeError(
        "找不到 gh（GitHub CLI）：请安装后重新打开终端，"
        "或把 gh.exe 所在目录加入 PATH。下载：https://cli.github.com/"
    )


def gh(*args: str, check: bool = True) -> str:
    binary = gh_binary()
    try:
        result = subprocess.run([binary, *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"无法执行 gh（{binary}）：{exc}") from exc
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def repo_name() -> str:
    return gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")


def release_of(tag: str) -> dict | None:
    raw = gh("api", f"repos/{repo_name()}/releases/tags/{tag}", check=False)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    # 404 时 gh 会把 {"message": "Not Found"} 打到 stdout，不能当成"已存在"
    if not isinstance(data, dict) or "id" not in data:
        return None
    return data


def all_releases() -> list[dict]:
    raw = gh("api", f"repos/{repo_name()}/releases?per_page=100")
    return json.loads(raw) if raw else []


def archive_previous(keep_tag: str) -> None:
    """把非当前版本的固定名资产改名成带版本号的形式。"""
    changed = 0
    for release in all_releases():
        tag = release.get("tag_name") or ""
        if tag == keep_tag:
            continue
        for asset in release.get("assets") or []:
            if asset["name"] != STABLE_ASSET:
                continue
            target = f"RocoMerchantAssistant-{tag}.exe"
            gh("api", "-X", "PATCH",
               f"repos/{repo_name()}/releases/assets/{asset['id']}",
               "-f", f"name={target}")
            print(f"  归档 {tag}: {STABLE_ASSET} -> {target}")
            changed += 1
    if not changed:
        print("  无需归档（没有其它版本占用固定名）")


def tag_exists(tag: str) -> bool:
    result = subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                            capture_output=True, text=True)
    return result.returncode == 0


def publish(asset: Path, draft: bool, notes_file: Path | None) -> str:
    ver = version()
    tag = f"v{ver}"
    if not asset.exists():
        raise SystemExit(f"找不到 exe：{asset}\n先运行 build_exe.bat，或用 --asset 指定路径")

    own = release_of(tag)
    if own is None:
        args = ["release", "create", tag, str(asset),
                "--title", f"{tag} · 远行商人助手"]
        if tag_exists(tag):
            args.append("--verify-tag")          # CI 里 tag 先推送再建 Release
        else:
            args += ["--target", "main"]         # 本地发布时自动建 tag
        if notes_file and Path(notes_file).exists():
            args += ["--notes-file", str(notes_file)]
        else:
            args += ["--notes", f"自动发布 {tag}。代码由 AI 生成，人类仅测试与验收。"]
        args.append("--draft" if draft else "--latest")
        gh(*args)
        print(f"已创建 Release {tag}")
    else:
        # 重发同名版本：先删掉占着固定名的旧资产，再上传
        for old in own.get("assets") or []:
            if old["name"].startswith("RocoMerchantAssistant"):
                gh("api", "-X", "DELETE",
                   f"repos/{repo_name()}/releases/assets/{old['id']}")
        gh("release", "upload", tag, str(asset))
        print(f"已更新 Release {tag} 的资产")

    # gh 用的是本地文件名（中文），这里统一校正成固定名
    refreshed = release_of(tag) or {}
    for item in refreshed.get("assets") or []:
        if item["name"] != STABLE_ASSET:
            gh("api", "-X", "PATCH",
               f"repos/{repo_name()}/releases/assets/{item['id']}",
               "-f", f"name={STABLE_ASSET}")
            print(f"  资产重命名 {item['name']} -> {STABLE_ASSET}")
    return tag


def main() -> int:
    parser = argparse.ArgumentParser(description="发布 exe 到 GitHub Release")
    parser.add_argument("--asset", type=Path, default=DEFAULT_EXE, help="exe 路径")
    parser.add_argument("--notes-file", type=Path, default=None)
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--archive-only", action="store_true")
    args = parser.parse_args()

    ver = version()
    tag = f"v{ver}"
    print(f"版本 {ver} -> tag {tag}")

    if args.archive_only:
        archive_previous(tag)
        return 0

    tag = publish(args.asset, args.draft, args.notes_file)
    archive_previous(tag)

    owner = repo_name()
    print("\n当前下载直链（永远指向最新版）：")
    print(f"  https://github.com/{owner}/releases/latest/download/{STABLE_ASSET}")
    print(f"历史版本：https://github.com/{owner}/releases/tag/<tag>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
