#!/usr/bin/env python3
"""在本机下发一条密文指令到 control.yml。

为什么需要它：网页控制台只覆盖"扫码 / 改账户 / 计划 / 渠道开关"，像
"导入 curl" 与"写入 SMTP 口令"这类必须粘贴长文本的动作没有表单，
用这个脚本从终端下发，效果与网页点按钮完全一样（同样加密、同样回执）。

用法示例：

    # 导入 curl（补全 appId/ps/pc，扫码后的账户必须做一次）
    python scripts/control_local.py import --payload-json '{"curl":"<粘贴 cURL>","label":"主号"}'

    # 也可以从文件读 curl，避免引号地狱
    python scripts/control_local.py import --payload-file import.json

    # 配邮件通知（口令只出现在密文里）
    python scripts/control_local.py notifications --payload-json \
        '{"enabled":true,"channels":["email"],"email":{"host":"smtp.qq.com","port":465,"user":"a@qq.com","password":"授权码","to":"a@qq.com"}}'

    # 看有哪些账户（改用 init 之外的只读动作时用 update/delete 前先跑一次）
    python scripts/control_local.py schedule --payload-json '{"enabled":true,"daily":"01:07","warm_hours":6}'

环境变量：GITHUB_TOKEN（fine-grained：contents 读写 + actions 读写），
         WXREAD_REPO（owner/repo），可选 WXREAD_STATE_BRANCH。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from wxread import crypto  # noqa: E402

API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "wxread-control-local",
    }


def _contents_json(resp: requests.Response) -> dict:
    text = base64.b64decode(resp.json()["content"]).decode("utf-8")
    return json.loads(text)


def load_public_key(session: requests.Session, repo: str, branch: str) -> tuple[bytes, int]:
    url = f"{API}/repos/{repo}/contents/control.json"
    resp = session.get(url, params={"ref": branch}, timeout=20)
    if resp.status_code == 404:
        raise SystemExit(
            f"状态分支 {branch} 上没有 control.json：先跑一次 control.yml（control=init）。"
        )
    resp.raise_for_status()
    data = _contents_json(resp)
    return crypto.b64d(data["public_key"]), int(data.get("key_version") or 1)


def dispatch(session: requests.Session, repo: str, workflow: str, inputs: dict, ref: str) -> None:
    url = f"{API}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    resp = session.post(url, json={"ref": ref, "inputs": inputs}, timeout=20)
    if resp.status_code == 204:
        return
    raise SystemExit(f"触发 {workflow} 失败：HTTP {resp.status_code} {resp.text[:300]}")


def wait_receipt(
    session: requests.Session, repo: str, branch: str, session_id: str, secret_key: bytes, seconds: int
) -> dict | None:
    url = f"{API}/repos/{repo}/contents/responses/{session_id}.json"
    deadline = time.time() + seconds
    while time.time() < deadline:
        resp = session.get(url, params={"ref": branch}, timeout=20)
        if resp.status_code == 200:
            envelope = _contents_json(resp)
            return crypto.unseal(envelope, secret_key)
        time.sleep(3)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机下发密文指令到 control.yml")
    parser.add_argument("action", help="init/qr/import/update/delete/schedule/notifications/test-mail/test-push")
    parser.add_argument("--repo", default=os.environ.get("WXREAD_REPO", ""), help="owner/repo")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--branch", default=os.environ.get("WXREAD_STATE_BRANCH", "wxread-state"))
    parser.add_argument("--ref", default="main", help="workflow 所在分支")
    parser.add_argument("--payload-json", default="", help="指令 payload，JSON 字符串")
    parser.add_argument("--payload-file", default="", help="从文件读 payload JSON")
    parser.add_argument("--curl-file", default="", help="从文件读 cURL 原文（import 用，自动包成 payload）")
    parser.add_argument("--label", default="", help="账户标签（配合 --curl-file）")
    parser.add_argument("--target-count", type=int, default=0, help="目标阅读次数（配合 --curl-file，默认 40）")
    parser.add_argument("--wait", type=int, default=180, help="等待回执秒数，0=不等")
    args = parser.parse_args(argv)

    if not args.repo or "/" not in args.repo:
        raise SystemExit("缺 --repo owner/repo（或设 WXREAD_REPO）")
    if not args.token:
        raise SystemExit("缺 --token（或设 GITHUB_TOKEN，需 contents + actions 写权限）")

    if args.curl_file:
        curl_text = Path(args.curl_file).read_text(encoding="utf-8").strip()
        if not curl_text:
            raise SystemExit(f"{args.curl_file} 是空的")
        payload = {"curl": curl_text}
        if args.label:
            payload["label"] = args.label
        if args.target_count:
            payload["target_count"] = args.target_count
    elif args.payload_file:
        payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    elif args.payload_json:
        payload = json.loads(args.payload_json)
    else:
        payload = {}

    session = requests.Session()
    session.headers.update(_headers(args.token))

    public_key, key_version = load_public_key(session, args.repo, args.branch)
    reply_public, reply_secret = crypto.new_keypair()
    session_id = secrets.token_hex(8)

    instruction = {
        "action": args.action,
        "session_id": session_id,
        "issued_at": int(time.time()),
        "nonce": crypto.b64e(secrets.token_bytes(12)),
        "payload": payload,
        "reply_key": crypto.b64e(reply_public),
    }
    envelope = crypto.seal(instruction, public_key)
    dispatch(
        session,
        args.repo,
        "control.yml",
        {"control": "instruction", "instruction": json.dumps(envelope), "session_id": session_id},
        args.ref,
    )
    print(f"已下发 {args.action}（session={session_id}，key_version={key_version}）")

    if args.wait <= 0:
        return 0
    receipt = wait_receipt(session, args.repo, args.branch, session_id, reply_secret, args.wait)
    if receipt is None:
        print(f"{args.wait}s 内没有回执，去 Actions 里看 control 运行日志。")
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt.get("ok") is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
