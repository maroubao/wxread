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
from wxread.state import utcnow_iso  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="qr-latest.png")
    parser.add_argument("--wait", type=float, default=qr.DEFAULT_TIMEOUT)
    parser.add_argument("--label", default="扫码号")
    parser.add_argument("--cookies-out", default=".qr-cookies.json")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--show", action="store_true", help="有显示器时用有头浏览器")
    args = parser.parse_args()

    page = qr.ChromeLoginPage(headless=not args.show)
    page.open()
    out = pathlib.Path(args.out)
    shots = {"count": 0}

    def on_qr(shot: qr.QrShot) -> None:
        shots["count"] += 1
        out.write_bytes(shot.png)
        print(f"[qr] #{shots['count']} {shot.fingerprint} {len(shot.png)}B -> {out}", flush=True)

    try:
        result = qr.wait_for_login(page, timeout=args.wait, on_qr=on_qr, poll=2.0)
        print(f"[qr] {qr.describe(result)}", flush=True)
        if not result.ok:
            return 1
        cookies = page.cookies()
        payload = {
            "cookies": cookies,
            "headers": {"User-Agent": page.user_agent()},
            "label": args.label,
            "at": utcnow_iso(),
        }
        pathlib.Path(args.cookies_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[qr] cookie 已写 {args.cookies_out}（wr_vid={cookies.get('wr_vid')}, wr_skey 长度={len(cookies.get('wr_skey',''))}）")
        return 0
    finally:
        page.close()
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
