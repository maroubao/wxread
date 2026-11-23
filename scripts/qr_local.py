"""本机扫码绑定：`python scripts/qr_local.py --out qr.png --wait 600`

跑完会把 cookie 写到本地临时文件（不入库、不 commit），供 `wxread.control.handle_import`
或本地 `import` 使用。Actions 侧走 `control.yml` 的 `qr` 指令，逻辑同源（`wxread/qr.py`）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wxread import qr  # noqa: E402
from wxread.logsafe import mask  # noqa: E402
from wxread.reader import Reader  # noqa: E402
from wxread.state import utcnow_iso  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="qr-latest.png")
    parser.add_argument("--wait", type=float, default=qr.DEFAULT_TIMEOUT)
    parser.add_argument("--label", default="扫码号")
    parser.add_argument("--cookies-out", default=".qr-cookies.json")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--show", action="store_true", help="有显示器时用有头浏览器")
    parser.add_argument("--verify-read", action="store_true", help="取参后通过Reader验证一次30秒读取")
    args = parser.parse_args()

    page = qr.ChromeLoginPage(headless=not args.show)
    out = pathlib.Path(args.out)
    shots = {"count": 0}

    def status(stage: str, message: str) -> None:
        out.with_suffix(".json").write_text(
            json.dumps({"stage": stage, "message": message, "at": time.time()}, ensure_ascii=False), encoding="utf-8"
        )

    def on_qr(shot: qr.QrShot) -> None:
        shots["count"] += 1
        out.write_bytes(shot.png)
        status("waiting", "用微信扫描当前二维码并确认登录")
        print(f"[qr] #{shots['count']} {shot.fingerprint} {len(shot.png)}B -> {out}", flush=True)

    try:
        status("starting", "正在生成新的登录二维码")
        page.open()
        result = qr.wait_for_login(page, timeout=args.wait, on_qr=on_qr, poll=2.0)
        print(f"[qr] {qr.describe(result)}", flush=True)
        if not result.ok:
            status("expired", "这轮登录等待已结束")
            return 1
        status("capturing", "登录已确认，正在自动获取阅读参数")
        print("[qr] 自动打开正文并等待成功阅读响应（最多180秒）…", flush=True)
        captured, capture_headers = page.capture_read_payload()
        cookies = page.cookies()
        headers = {"User-Agent": page.user_agent()}
        headers.update(capture_headers)
        payload = {
            "cookies": cookies,
            "headers": headers,
            "read_payload": captured,
            "label": args.label,
            "at": utcnow_iso(),
        }
        pathlib.Path(args.cookies_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[qr] cookie 已写 {args.cookies_out}（wr_vid={mask(cookies.get('wr_vid',''))}, wr_skey 长度={len(cookies.get('wr_skey',''))}）")
        print(f"[qr] read_payload 字段：{sorted(captured) or '未取齐，可使用已有登录态自动重试'}", flush=True)
        if not captured:
            status("failed", "自动取参未成功，仍可使用登录态重试")
            return 2
        if args.verify_read:
            status("verifying", "阅读参数已取齐，正在验证一次真实读取")
            account = qr.account_from_cookies(cookies, headers=headers, payload=captured, now_iso=utcnow_iso())
            account.target_count = 1
            outcome = Reader(account).run()
            print(f"[verify] read_count={outcome.read_count} level={outcome.level} reason={outcome.reason}", flush=True)
            status("verified" if outcome.read_count == 1 and outcome.exit_code == 0 else "failed", "真实读取验证通过" if outcome.read_count == 1 and outcome.exit_code == 0 else "真实读取验证失败，继续排查")
            return 0 if outcome.read_count == 1 and outcome.exit_code == 0 else 3
        status("captured", "自动获取阅读参数完成")
        return 0
    finally:
        page.close()
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
