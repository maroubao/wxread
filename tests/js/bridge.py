"""浏览器/服务端加密互通测试的桥：只做编解码，验证两边字节级一致。

用法（由 tests/js/crypto.test.mjs 调用）：
    python tests/js/bridge.py mkfixtures   # 输出 fixture JSON 到 stdout
    python tests/js/bridge.py verify       # 从 stdin 读 JSON，校验 JS 侧产物
"""

from __future__ import annotations

import base64
import json
import sys

sys.path.insert(0, ".")

from wxread import crypto  # noqa: E402

OWNER_REPO = "owner/repo"


def mkfixtures() -> dict:
    key = crypto.new_state_key()
    ctx = crypto.context(OWNER_REPO, 1)
    payload = {"text": "中文值", "n": 1, "nested": {"b": [2, 3], "a": None}}
    public_key, secret_key = crypto.new_keypair()
    return {
        "owner_repo": OWNER_REPO,
        "ctx": ctx.decode("utf-8"),
        "key_b64": crypto.b64e(key),
        "envelope": crypto.encrypt_state(payload, key, ctx, 1),
        "public_key_b64": crypto.b64e(public_key),
        "secret_key_b64": crypto.b64e(secret_key),
        "payload": payload,
    }


def verify(raw: str) -> dict:
    data = json.loads(raw)
    key = base64.b64decode(data["key_b64"])
    ctx = data["ctx"].encode("utf-8")
    state = crypto.decrypt_state(data["js_state_env"], key, ctx)
    sealed = crypto.unseal(data["js_seal_env"], base64.b64decode(data["secret_key_b64"]))
    return {"state": state, "sealed": sealed}


def main(argv: list[str]) -> int:
    # Windows 控制台默认 cp936：显式 UTF-8，否则中文 fixture 会变成乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    if not argv:
        print("usage: bridge.py mkfixtures|verify", file=sys.stderr)
        return 2
    if argv[0] == "mkfixtures":
        json.dump(mkfixtures(), sys.stdout, ensure_ascii=False)
        return 0
    if argv[0] == "verify":
        print(json.dumps(verify(sys.stdin.read()), ensure_ascii=False))
        return 0
    print(f"unknown command {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
