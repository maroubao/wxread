"""本机指令脚本：确认它真的产出一条能被 worker 解开的密文指令。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from wxread import crypto

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("control_local", ROOT / "scripts" / "control_local.py")


@pytest.fixture()
def mod(monkeypatch):
    module = importlib.util.module_from_spec(SPEC)
    sys.modules["control_local"] = module
    SPEC.loader.exec_module(module)
    return module


def test_dispatches_openable_instruction(mod, monkeypatch):
    captured = {}
    public, secret = crypto.new_keypair()

    monkeypatch.setattr(mod, "load_public_key", lambda *a, **k: (public, 1))
    monkeypatch.setattr(
        mod, "dispatch", lambda session, repo, workflow, inputs, ref: captured.update(inputs=inputs, workflow=workflow)
    )

    code = mod.main(
        [
            "import",
            "--repo",
            "owner/repo",
            "--token",
            "t",
            "--wait",
            "0",
            "--payload-json",
            json.dumps({"curl": "curl 'https://weread.qq.com/web/book/read'", "label": "主号"}),
        ]
    )

    assert code == 0
    assert captured["workflow"] == "control.yml"
    assert captured["inputs"]["control"] == "instruction"
    envelope = json.loads(captured["inputs"]["instruction"])
    instruction = crypto.unseal(envelope, secret)
    assert instruction["action"] == "import"
    assert instruction["session_id"] == captured["inputs"]["session_id"]
    assert instruction["payload"]["label"] == "主号"
    assert "curl" in instruction["payload"]
    # reply_key 必须是本次新生成的公钥，worker 用它加密回执
    assert crypto.b64d(instruction["reply_key"])


def test_receipt_is_decrypted_with_reply_key(mod):
    _, secret = crypto.new_keypair()
    receipt = {"ok": True, "accounts": ["abc123"]}
    env = crypto.seal(receipt, crypto.keypair_from_secret(secret)[0])
    assert crypto.unseal(env, secret)["accounts"] == ["abc123"]


def test_curl_file_is_wrapped_into_payload(mod, monkeypatch, tmp_path):
    """F12 复制出来的 cURL 是纯文本，不能当 JSON 喂给 --payload-file。"""
    captured = {}
    public, secret = crypto.new_keypair()
    monkeypatch.setattr(mod, "load_public_key", lambda *a, **k: (public, 1))
    monkeypatch.setattr(
        mod, "dispatch", lambda session, repo, workflow, inputs, ref: captured.update(inputs=inputs)
    )
    curl = tmp_path / "curl.txt"
    curl.write_text("curl 'https://weread.qq.com/web/book/read' -H 'Cookie: wr_vid=1; wr_skey=2'", encoding="utf-8")

    code = mod.main(
        [
            "import",
            "--repo",
            "owner/repo",
            "--token",
            "t",
            "--wait",
            "0",
            "--curl-file",
            str(curl),
            "--label",
            "主号",
            "--target-count",
            "30",
        ]
    )

    assert code == 0
    instruction = crypto.unseal(json.loads(captured["inputs"]["instruction"]), secret)
    assert instruction["payload"]["label"] == "主号"
    assert instruction["payload"]["target_count"] == 30
    assert "wr_skey=2" in instruction["payload"]["curl"]


def test_empty_curl_file_is_rejected(mod, monkeypatch, tmp_path):
    public, _ = crypto.new_keypair()
    monkeypatch.setattr(mod, "load_public_key", lambda *a, **k: (public, 1))
    empty = tmp_path / "curl.txt"
    empty.write_text("   \n", encoding="utf-8")

    with pytest.raises(SystemExit, match="空"):
        mod.main(["import", "--repo", "owner/repo", "--token", "t", "--wait", "0", "--curl-file", str(empty)])
