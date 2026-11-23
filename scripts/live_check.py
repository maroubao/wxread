"""真机自检：用扫码得到的 cookie 验证 renewal 与读接口的守卫行为。

只读、不落盘（除 stdout 外无副作用）：
1. `renew()` 是否真的从 Set-Cookie 拿到新 wr_skey；
2. 没有 read_payload 时 `read_once()` 是否按 PayloadInvalid 拦下（防止拿半套参数打服务端）。
"""

import json
import sys

sys.path.insert(0, ".")

from wxread import reader as rd

COOKIES = json.load(open(".qr-cookies.json", encoding="utf-8"))
account = {
    "cookies": COOKIES["cookies"],
    "headers": COOKIES.get("headers") or {},
    "read_payload": {},
}


def fake_account():
    from wxread.accounts import Account

    return Account.from_dict(
        {
            "schema_version": 1,
            "id": "livecheck0001",
            "label": "live",
            "cookies": account["cookies"],
            "headers": account["headers"],
            "read_payload": account["read_payload"],
            "target_count": 1,
            "status": "ok",
        }
    )


def main() -> int:
    acc = fake_account()
    transport = rd.RequestsTransport()

    print("[live] renew...")
    try:
        new_key = rd.Reader(acc, transport).renew()
        same = new_key == acc.cookies.get("wr_skey")
        print(f"[live] renew ok, skey len={len(new_key)}, changed={not same}")
    except rd.ReadError as exc:
        print(f"[live] renew failed: {type(exc).__name__}: {exc}")
        return 1

    print("[live] run() without payload (expect L3 payload_invalid, no network)...")
    outcome = rd.Reader(acc, transport).run()
    if outcome.level == "L3" and outcome.reason == "payload_invalid" and outcome.read_count == 0:
        print(f"[live] guard ok: {outcome.reason} exit={outcome.exit_code} notes={outcome.notes}")
    else:
        print(f"[live] guard MISSING: {outcome}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
