"""明文密钥扫描：CI 与本地共用，退出码 1 表示发现疑似泄漏。

规则（宁可误报，不可漏报）：
- `wr_skey` / `wr_vid` 直接出现在非测试文件里；
- GitHub token（ghp_/github_pat_）、Slack token、私钥块；
- 名叫 `.qr-cookies.json` / `qr-latest.png` 的产物被提交。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "qr-out", "docs/design"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip"}

PATTERNS = [
    (re.compile(r"wr_skey\s*[=:]\s*['\"]?[A-Za-z0-9%_\-]{6,}"), "疑似明文 wr_skey"),
    (re.compile(r"wr_vid\s*[=:]\s*['\"]?\d{6,}"), "疑似明文 wr_vid"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub PAT"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "GitHub fine-grained PAT"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
]

# 测试夹具与文档里的示例值允许出现（它们是假数据 / 已脱敏）
ALLOW_PREFIXES = ("tests/", "docs/")
ALLOW_SUFFIXES = {".md"}

FORBIDDEN_FILES = {".qr-cookies.json", "qr-latest.png", "qr-scan.png"}


def git_files() -> list[Path] | None:
    """已跟踪 + 未忽略的新文件；不在 git 仓库里返回 None。

    这样本地与 CI 的口径一致：被 .gitignore 掉的产物（.qr-cookies.json 等）本来就不会进仓库，
    扫描器不该因为它还躺在本机就报错。
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [ROOT / name for name in out.decode("utf-8", "replace").split("\0") if name]


def iter_files() -> list[Path]:
    listed = git_files()
    if listed is not None:
        return [path for path in listed if path.is_file()]
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def main() -> int:
    problems: list[str] = []
    for path in iter_files():
        rel = path.relative_to(ROOT).as_posix()
        if path.name in FORBIDDEN_FILES:
            problems.append(f"{rel}: 不该提交的本地产物")
            continue
        if rel.startswith(ALLOW_PREFIXES) or path.suffix in ALLOW_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, label in PATTERNS:
                if pattern.search(line):
                    problems.append(f"{rel}:{lineno}: {label}")
    if problems:
        print("发现疑似明文密钥：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"扫描完成：{len(iter_files())} 个文件，未发现明文密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
