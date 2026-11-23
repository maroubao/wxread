"""本机生成指令密钥对：私钥只留在你机器上，永不进 CI 日志。

CI 日志对 public 仓库是全网可见的，所以私钥不能由 Actions 打印；
`control.yml` 的 `init` 只接收公钥，私钥由本脚本生成后自己存进
Secret `WXREAD_CONTROL_SK`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxread import crypto  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 wxread 指令密钥对")
    parser.add_argument("--out", help="写到这个 JSON 文件（默认只打印）")
    parser.add_argument("--quiet", action="store_true", help="只打印 public_key")
    args = parser.parse_args(argv)

    public, secret = crypto.new_keypair()
    pair = {"public_key": crypto.b64e(public), "private_key": crypto.b64e(secret)}

    if args.out:
        path = Path(args.out)
        path.write_text(json.dumps(pair, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入 {path}")

    print(f"public_key={pair['public_key']}")
    if not args.quiet:
        print(f"private_key={pair['private_key']}")
        print("")
        print("下一步：")
        print("  1) 把 private_key 存进仓库 Secret WXREAD_CONTROL_SK")
        print("  2) Actions → control → Run workflow，control=init，public_key 填上面的 public_key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
